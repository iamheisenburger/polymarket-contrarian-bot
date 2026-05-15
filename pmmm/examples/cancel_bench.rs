//! cancel-latency benchmark
//!
//! Measures the round-trip latency of a postOnly place + immediate cancel,
//! ~50 iterations. Reports p50/p95/p99/max. This is the single biggest
//! design risk identified for the PAAM strategy — if cancel-REST p95 is
//! above 200 ms during normal load, two-sided tight quoting is unviable.
//!
//! Order parameters: postOnly BUY at a price well below the current ask, so
//! the order rests as a maker quote and never matches. Size = PM minimum
//! (5 tokens). Each cycle commits ~$1-$3 of capital briefly, immediately
//! cancelled before any taker can hit it. Total wall time ≈ 10-20 s.
//!
//! Run:
//!   cd /opt/polymarket-bot && set -a && . .env && set +a && cd pmmm && \
//!     ./target/release/examples/cancel_bench BTC 5m 50
//!
//! Args: <coin> <timeframe> <iterations>

use std::env;
use std::sync::Arc;
use std::time::{Duration, Instant};

use alloy_primitives::{Address, U256};
use anyhow::{Context, Result};

use pmmm::clob::{ClobClient, ClobConfig, OrderResponse};
use pmmm::config::{load_l2_creds, EnvCreds};
use pmmm::gamma::{GammaClient, TimeFrame};
use pmmm::state::Wei6;
use pmmm::strategy::theo_p_up_at; // touch to ensure linkage
use pmmm::types::{Order, OrderSide, OrderType, Symbol};

fn pct(sorted_us: &[u64], p: f64) -> u64 {
    if sorted_us.is_empty() {
        return 0;
    }
    let idx = ((sorted_us.len() - 1) as f64 * p).round() as usize;
    sorted_us[idx]
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() -> Result<()> {
    let _ = theo_p_up_at; // suppress unused import warning
    tracing_subscriber::fmt().with_max_level(tracing::Level::INFO).init();

    let args: Vec<String> = env::args().collect();
    let coin_str = args.get(1).map(String::as_str).unwrap_or("BTC");
    let tf_str = args.get(2).map(String::as_str).unwrap_or("5m");
    let n_iters: usize = args
        .get(3)
        .and_then(|s| s.parse().ok())
        .unwrap_or(50usize);

    let coin = Symbol::parse(coin_str).context("bad coin")?;
    let tf = match tf_str {
        "5m" => TimeFrame::M5,
        "15m" => TimeFrame::M15,
        "1h" => TimeFrame::H1,
        _ => anyhow::bail!("bad tf: use 5m|15m|1h"),
    };

    let creds = EnvCreds::from_env().context("EnvCreds::from_env")?;
    let l2_path = env::var("L2_CREDS_PATH").unwrap_or_else(|_| "/tmp/l2_creds.json".into());
    let l2 = load_l2_creds(&l2_path)?;
    let cfg = ClobConfig::new(None, l2.clone(), creds.builder.clone(), creds.signer_address)?;
    let clob = Arc::new(ClobClient::new(cfg)?);

    // Discover current market.
    let gamma = GammaClient::new(None)?;
    let market = gamma
        .current_market(coin, tf)
        .await?
        .ok_or_else(|| anyhow::anyhow!("no current market for {coin_str} {tf_str}"))?;
    let (up_token_id, _down_token_id) = market.token_ids()?;
    eprintln!("market: {}", market.slug);
    eprintln!("up_token_id: {}", up_token_id);
    eprintln!("iterations: {}", n_iters);

    // Cheap bid price — far below any reasonable ask, won't match.
    // PM min notional is $1.00. 5 tokens at $0.20 = $1.00.
    let price = 0.20_f64;
    let size = 5.0_f64;
    let token_id_u256 = U256::from_str_radix(&up_token_id, 10)?;

    let mut cancel_us: Vec<u64> = Vec::with_capacity(n_iters);
    let mut place_us: Vec<u64> = Vec::with_capacity(n_iters);
    let mut place_acked: usize = 0;
    let mut cancel_acked: usize = 0;
    let mut errors: usize = 0;

    let signer = creds.signer.clone();
    let funder: Address = creds.funder_address;
    let api_key = l2.api_key.clone();

    for i in 0..n_iters {
        // Build + sign v2 order.
        let salt_u64: u64 = (rand::random::<u32>() as u64) & 0x3fff_ffff;
        let timestamp_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_millis() as u64;
        let order = Order {
            salt: U256::from(salt_u64),
            maker: funder,
            signer: signer.address(),
            token_id: token_id_u256,
            maker_amount: Wei6::from_usd(price * size).0,
            taker_amount: Wei6::from_token(size).0,
            side: OrderSide::Buy.as_u8(),
            signature_type: 2,
            timestamp: U256::from(timestamp_ms),
            metadata: alloy_primitives::B256::ZERO,
            builder: alloy_primitives::B256::ZERO,
            expiration: U256::ZERO,
        };
        let signed = signer.sign_order(&order, &api_key)?;

        // POST place.
        let t_place = Instant::now();
        // postOnly=true so the bench never accidentally takes — this was the
        // bug that turned a measurement run into 250 stuck UP tokens earlier.
        let place_res = clob.post_order(&signed, OrderType::Gtc, true).await;
        let place_dt = t_place.elapsed().as_micros() as u64;
        place_us.push(place_dt);

        let order_id: String = match place_res {
            Ok(OrderResponse { success: true, order_id, .. }) if !order_id.is_empty() => {
                place_acked += 1;
                order_id
            }
            Ok(other) => {
                eprintln!("  #{i}: place not acked: success={} status={} order_id={:?}", other.success, other.status, other.order_id);
                errors += 1;
                tokio::time::sleep(Duration::from_millis(50)).await;
                continue;
            }
            Err(e) => {
                eprintln!("  #{i}: place error: {e:?}");
                errors += 1;
                tokio::time::sleep(Duration::from_millis(50)).await;
                continue;
            }
        };

        // Tiny pause so PM has the order in the queue. 25 ms ≈ one HTTP/2 frame.
        tokio::time::sleep(Duration::from_millis(25)).await;

        // DELETE cancel — time this round-trip.
        let t_cancel = Instant::now();
        let cancel_res = clob.cancel_orders(&[order_id.clone()]).await;
        let cancel_dt = t_cancel.elapsed().as_micros() as u64;
        cancel_us.push(cancel_dt);
        match cancel_res {
            Ok(r) if !r.canceled.is_empty() => {
                cancel_acked += 1;
            }
            Ok(r) => {
                eprintln!("  #{i}: cancel: not_canceled={:?}", r.not_canceled);
            }
            Err(e) => {
                eprintln!("  #{i}: cancel error: {e:?}");
                errors += 1;
            }
        }

        // Light pause between cycles (PM nonce safety).
        tokio::time::sleep(Duration::from_millis(50)).await;
        if (i + 1) % 10 == 0 {
            eprintln!("  ... {}/{} ({} ms last)", i + 1, n_iters, cancel_dt / 1000);
        }
    }

    place_us.sort();
    cancel_us.sort();

    println!("\n=== CANCEL-LATENCY BENCHMARK ===");
    println!("market: {}", market.slug);
    println!("iters: {}  place_acked: {}  cancel_acked: {}  errors: {}",
             n_iters, place_acked, cancel_acked, errors);
    println!();
    println!("place latency (POST /order, microseconds):");
    println!("  n={}  p50={}us  p95={}us  p99={}us  max={}us",
             place_us.len(),
             pct(&place_us, 0.50),
             pct(&place_us, 0.95),
             pct(&place_us, 0.99),
             place_us.last().copied().unwrap_or(0));
    println!("place latency (milliseconds):");
    println!("  p50={:.1}ms  p95={:.1}ms  p99={:.1}ms",
             pct(&place_us, 0.50) as f64 / 1000.0,
             pct(&place_us, 0.95) as f64 / 1000.0,
             pct(&place_us, 0.99) as f64 / 1000.0);
    println!();
    println!("cancel latency (DELETE /orders, microseconds):");
    println!("  n={}  p50={}us  p95={}us  p99={}us  max={}us",
             cancel_us.len(),
             pct(&cancel_us, 0.50),
             pct(&cancel_us, 0.95),
             pct(&cancel_us, 0.99),
             cancel_us.last().copied().unwrap_or(0));
    println!("cancel latency (milliseconds):");
    println!("  p50={:.1}ms  p95={:.1}ms  p99={:.1}ms",
             pct(&cancel_us, 0.50) as f64 / 1000.0,
             pct(&cancel_us, 0.95) as f64 / 1000.0,
             pct(&cancel_us, 0.99) as f64 / 1000.0);
    println!();
    let gate_pass = pct(&cancel_us, 0.95) <= 200_000;
    if gate_pass {
        println!("PAAM viability gate (p95 cancel <= 200 ms): PASS");
    } else {
        println!("PAAM viability gate (p95 cancel <= 200 ms): FAIL");
        println!("  Recommendation: fall back to one-sided underdog quoter at theo - 6c.");
    }
    Ok(())
}
