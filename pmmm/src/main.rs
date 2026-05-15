//! pmmm — Polymarket Market Maker binary entrypoint.
//!
//! Pure-Rust pro-tier market maker. Replaces the Python `ohan_mm_v7_0.py`
//! reference implementation. See `/Users/madmanhakim/.claude/plans/glimmering-knitting-stream.md`
//! for the full rewrite roadmap. All module code lives in the `pmmm` library
//! crate (`src/lib.rs`).

use std::collections::BTreeMap;
use std::fs;
use std::time::Duration;

use anyhow::{Context, Result};
use clap::Parser;
use tracing::info;

use pmmm::cli::{Cli, Command, RunArgs, TheoTestArgs};
use pmmm::clob::{ClobClient, ClobConfig, MarketWs, UserWs};
use pmmm::config::{load_l2_creds, EnvCreds};
use pmmm::feeds::SpotPulse;
use pmmm::gamma::{GammaClient, TimeFrame};
use pmmm::persist::AuditLogger;
use pmmm::risk::{RiskConfig, RiskState};
use pmmm::runtime::{drain_market_events, Mode, Runtime};
use pmmm::state::GlobalState;
use pmmm::strategy::{theo_p_up_at, PaamQuoter, Quoter, TheoV6Params, V7Quoter};
use pmmm::types::{FeedSource, Symbol};

#[tokio::main(flavor = "multi_thread", worker_threads = 4)]
async fn main() -> Result<()> {
    init_tracing();
    let cli = Cli::parse();
    match cli.command {
        Command::TheoTest(args) => cmd_theo_test(args),
        Command::TheoParamsShow { path } => cmd_theo_params_show(&path),
        Command::FeedsTest { coins, duration_s } => {
            cmd_feeds_test(&coins, duration_s).await
        }
        Command::Run(args) => cmd_run(args).await,
    }
}

async fn cmd_run(args: RunArgs) -> Result<()> {
    use std::sync::Arc;
    use std::time::Duration;

    // ---- parse mode + symbols + timeframes ----
    let mode = match args.mode.to_lowercase().as_str() {
        "shadow" => Mode::Shadow,
        "live" => Mode::Live,
        other => anyhow::bail!("invalid --mode: {} (use shadow|live)", other),
    };
    let coins: Vec<Symbol> = args
        .coins
        .split(',')
        .filter_map(|s| Symbol::parse(s.trim()))
        .collect();
    if coins.is_empty() {
        anyhow::bail!("no valid coins parsed from --coins");
    }
    let timeframes: Vec<TimeFrame> = args
        .timeframes
        .split(',')
        .filter_map(|s| match s.trim() {
            "5m" => Some(TimeFrame::M5),
            "15m" => Some(TimeFrame::M15),
            "1h" => Some(TimeFrame::H1),
            _ => None,
        })
        .collect();
    if timeframes.is_empty() {
        anyhow::bail!("no valid timeframes parsed from --timeframes");
    }
    info!(?mode, ?coins, ?timeframes, "pmmm run starting");

    // ---- audit log ----
    let audit_handle = AuditLogger::spawn(args.audit_prefix.clone());
    let audit = audit_handle.logger.clone();
    audit.write(
        "start",
        serde_json::json!({
            "mode": match mode { Mode::Shadow => "shadow", Mode::Live => "live" },
            "coins": coins.iter().map(|c| c.as_str()).collect::<Vec<_>>(),
            "timeframes": timeframes.iter().map(|t| t.as_str()).collect::<Vec<_>>(),
            "version": env!("CARGO_PKG_VERSION"),
        }),
    );

    // ---- optional theo params ----
    // Accept either flat (raw TheoV6Params) or calibration-output JSON with
    // params nested under `.theo_params`. The Python calibrator emits the
    // latter; lets us point --theo-params at calibration files directly.
    let theo_params = if let Some(p) = args.theo_params.as_ref() {
        let raw = fs::read_to_string(p).with_context(|| format!("read {}", p))?;
        let parsed: TheoV6Params = match serde_json::from_str::<TheoV6Params>(&raw) {
            Ok(p) => p,
            Err(_) => {
                let v: serde_json::Value = serde_json::from_str(&raw)?;
                let inner = v.get("theo_params")
                    .ok_or_else(|| anyhow::anyhow!("theo params JSON has neither flat schema nor `.theo_params` key"))?;
                serde_json::from_value(inner.clone())?
            }
        };
        info!(?parsed.horizons_s, intercept=parsed.intercept, "loaded TheoV6Params");
        Some(Arc::new(parsed))
    } else {
        None
    };

    // ---- start spot feeds ----
    let mut sp = SpotPulse::new(&coins);
    sp.start().await?;
    let spot = Arc::new(sp);

    // ---- gamma client ----
    let gamma = Arc::new(GammaClient::new(None)?);

    // ---- initial market discovery (before market WS connects) ----
    // PM's market WS won't accept re-subscribes on an existing connection
    // gracefully, so we do one upfront discovery pass and seed the asset_ids
    // before opening the WS. New markets discovered later use the dynamic
    // path but it only reliably works at fresh-connect time.
    info!("running initial market discovery via Gamma");
    let mut initial_asset_ids: Vec<String> = Vec::new();
    let mut initial_markets: Vec<pmmm::state::MarketState> = Vec::new();
    for coin in &coins {
        for tf in &timeframes {
            if let Ok(Some(m)) = gamma.current_market(*coin, *tf).await {
                if !m.accepting_orders || m.closed {
                    continue;
                }
                if let Ok((up, down)) = m.token_ids() {
                    let market_start_ts: i64 = m.slug.rsplit('-').next().and_then(|s| s.parse().ok()).unwrap_or(0);
                    let ms = pmmm::state::MarketState::new(
                        m.slug.clone(),
                        *coin,
                        tf.as_str().to_string(),
                        tf.seconds(),
                        market_start_ts,
                        up.clone(),
                        down.clone(),
                        0.0,
                    );
                    audit.write(
                        "market_selected_initial",
                        serde_json::json!({"slug": &m.slug, "coin": coin.as_str(), "tf": tf.as_str()}),
                    );
                    initial_asset_ids.push(up);
                    initial_asset_ids.push(down);
                    initial_markets.push(ms);
                }
            }
        }
    }
    info!(
        n_markets = initial_markets.len(),
        n_asset_ids = initial_asset_ids.len(),
        "initial Gamma discovery done"
    );

    // ---- market WS with initial asset_ids ----
    let mut market_ws = MarketWs::new(initial_asset_ids);
    let market_rx = market_ws.take_receiver()?;
    market_ws.start().await?;
    let market_ws = Arc::new(tokio::sync::Mutex::new(market_ws));

    // ---- live-mode creds + clob client + user WS ----
    // user_ws_rx is the receiver we'll wire into spawn_user_event_handler below.
    let mut user_ws_rx: Option<tokio::sync::mpsc::UnboundedReceiver<pmmm::clob::UserEvent>> = None;
    let (clob, user_ws, signer, funder, l2_api_key) = if mode == Mode::Live {
        let creds = EnvCreds::from_env().context("EnvCreds::from_env (need POLY_PRIVATE_KEY + POLY_SAFE_ADDRESS)")?;
        let l2_path = args
            .l2_creds
            .as_ref()
            .context("--l2-creds is required for --mode live")?;
        let l2 = load_l2_creds(l2_path)?;
        let l2_api_key = l2.api_key.clone();
        let cfg = ClobConfig::new(None, l2.clone(), creds.builder.clone(), creds.signer_address)?;
        let clob = Arc::new(ClobClient::new(cfg)?);
        let signer = Arc::new(creds.signer);
        let funder = creds.funder_address;

        // User WS subscribes to all the markets we just discovered. The
        // subscribe payload requires a `markets` list (condition IDs), but
        // for v7.0 we'll subscribe to all current markets at startup.
        // For now we subscribe with empty markets list which gets ALL user
        // events for the wallet (per PM docs, empty = subscribe to all).
        let mut uws = UserWs::new(l2, Vec::new());
        let rx = uws.take_receiver()?;
        uws.start().await?;
        user_ws_rx = Some(rx);
        let uws = Arc::new(tokio::sync::Mutex::new(uws));
        (Some(clob), Some(uws), Some(signer), Some(funder), Some(l2_api_key))
    } else {
        (None, None, None, None, None)
    };

    // ---- state + risk ----
    let state = Arc::new(GlobalState::new());
    // Risk config matches the strategy. V7 needs the 0.60 favorite-only
    // floor; PAAM needs to quote underdog side too (floor = PM tick respect).
    let risk = Arc::new(match args.quoter.as_str() {
        "v7" => RiskConfig::v7_default(),
        "paam" => RiskConfig::paam_default(),
        _ => RiskConfig::v7_default(),
    });
    info!(min_bid = risk.min_bid_price, max_bid = risk.max_bid_price, "risk config selected for quoter");
    let risk_state = Arc::new(RiskState::new());

    // Seed state with the markets discovered above.
    for m in initial_markets {
        state.insert(m);
    }

    // Build the quoter based on CLI flag.
    let quoter: Arc<dyn Quoter> = match args.quoter.as_str() {
        "v7" => {
            info!("quoter: V7Quoter (legacy favorite-bias)");
            Arc::new(V7Quoter::new())
        }
        "paam" => {
            info!("quoter: PaamQuoter v1 (two-sided spread-capture postOnly maker, sum-gate 0.95)");
            Arc::new(PaamQuoter::new())
        }
        other => anyhow::bail!("invalid --quoter: {} (use v7|paam)", other),
    };

    // ---- runtime ----
    let runtime = Arc::new(Runtime {
        mode,
        coins: coins.clone(),
        timeframes: timeframes.clone(),
        risk,
        state: Arc::clone(&state),
        risk_state,
        audit: audit.clone(),
        quoter,
        spot: Arc::clone(&spot),
        gamma: Arc::clone(&gamma),
        market_ws: Arc::clone(&market_ws),
        user_ws,
        clob,
        theo_params,
        signer,
        funder,
        l2_api_key,
    });

    // ---- background workers ----
    let discovery_task = runtime.spawn_market_discovery();
    let drain_task = tokio::spawn(drain_market_events(market_rx, audit.clone()));
    let user_task = user_ws_rx.map(|rx| runtime.spawn_user_event_handler(rx));

    // ---- main loop with optional duration limit ----
    let main_task = {
        let rt = Arc::clone(&runtime);
        tokio::spawn(async move { rt.run().await })
    };

    if args.duration_s > 0 {
        tokio::time::sleep(Duration::from_secs(args.duration_s)).await;
        info!("pmmm run: duration reached, shutting down");
        audit.write("duration_reached", serde_json::json!({"seconds": args.duration_s}));
    } else {
        tokio::signal::ctrl_c().await?;
        info!("pmmm run: shutting down on ctrl-c");
        audit.write("shutdown_signal", serde_json::json!({}));
    }
    main_task.abort();
    discovery_task.abort();
    drain_task.abort();
    if let Some(t) = user_task {
        t.abort();
    }

    audit.write("done", serde_json::json!({}));
    drop(audit_handle.logger); // close audit channel so the writer flushes
    let _ = tokio::time::timeout(Duration::from_secs(2), audit_handle.task).await;
    Ok(())
}

fn init_tracing() {
    use tracing_subscriber::{fmt, EnvFilter};
    let filter = EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info,pmmm=info"));
    fmt().with_env_filter(filter).with_target(false).init();
}

fn cmd_theo_params_show(path: &str) -> Result<()> {
    let raw = fs::read_to_string(path).with_context(|| format!("read {}", path))?;
    let params: TheoV6Params =
        serde_json::from_str(&raw).with_context(|| "parse TheoV6Params")?;
    info!(
        n_horizons = params.n_horizons(),
        intercept = params.intercept,
        sigma_floor = params.sigma_floor,
        sigma_scale = params.sigma_scale,
        coef_log_t = params.coef_log_t,
        coef_log_dist = params.coef_log_dist,
        "loaded TheoV6Params"
    );
    println!("{}", serde_json::to_string_pretty(&params)?);
    Ok(())
}

fn cmd_theo_test(args: TheoTestArgs) -> Result<()> {
    let raw = fs::read_to_string(&args.params_path)
        .with_context(|| format!("read params {}", args.params_path))?;
    let params: TheoV6Params =
        serde_json::from_str(&raw).with_context(|| "parse TheoV6Params")?;

    let series_raw = fs::read_to_string(&args.series_path)
        .with_context(|| format!("read series {}", args.series_path))?;
    let series: Vec<(f64, f64)> =
        serde_json::from_str(&series_raw).with_context(|| "parse series [[t,price],...]")?;
    let keys: Vec<f64> = series.iter().map(|(t, _)| *t).collect();

    let p = theo_p_up_at(args.t, args.spot, args.strike, args.ttm, &series, &keys, &params);

    println!(
        "{{\"coin\":\"{}\",\"spot\":{},\"strike\":{},\"ttm\":{},\"t\":{},\"theo_p_up\":{}}}",
        args.coin,
        args.spot,
        args.strike,
        args.ttm,
        args.t,
        match p {
            Some(v) => format!("{}", v),
            None => "null".to_string(),
        }
    );
    Ok(())
}

async fn cmd_feeds_test(coins_csv: &str, duration_s: u64) -> Result<()> {
    let symbols: Vec<Symbol> = coins_csv
        .split(',')
        .filter_map(|s| Symbol::parse(s.trim()))
        .collect();
    if symbols.is_empty() {
        anyhow::bail!("no valid coins parsed from {}", coins_csv);
    }
    info!(?symbols, duration_s, "starting feeds smoke test");

    let mut sp = SpotPulse::new(&symbols);
    sp.start().await?;

    // Warmup
    let warm = sp.warmup(Duration::from_secs(10)).await;
    info!(warm, "warmup done");

    // Run for duration
    tokio::time::sleep(Duration::from_secs(duration_s)).await;

    // Report
    let wins = sp.tick_wins();
    let total: u64 = wins.iter().map(|(_, _, n)| n).sum();

    // Aggregate by source
    let mut by_source: BTreeMap<FeedSource, u64> = BTreeMap::new();
    for (_, src, n) in &wins {
        *by_source.entry(*src).or_insert(0) += n;
    }

    println!();
    println!("=== Feeds smoke test ({}s) — TOTAL tick wins: {} ===", duration_s, total);
    for src in [FeedSource::Pyth, FeedSource::Coinbase, FeedSource::Binance] {
        let n = by_source.get(&src).copied().unwrap_or(0);
        let pct = if total > 0 { 100.0 * n as f64 / total as f64 } else { 0.0 };
        println!("  {:<10} {:>6} wins  ({:>5.1}%)", src.as_str(), n, pct);
    }

    println!();
    println!("=== Per-coin freshest source right now ===");
    for sym in &symbols {
        match sp.latest(*sym) {
            Some(t) => {
                let age_ms = sp.age_ns(*sym).unwrap_or(0) as f64 / 1_000_000.0;
                println!(
                    "  {}: {} @ {:.4}  age={:.0}ms",
                    sym,
                    t.source.as_str(),
                    t.mid,
                    age_ms
                );
            }
            None => println!("  {}: no tick yet", sym),
        }
    }

    Ok(())
}
