//! CLI subcommand dispatch.

use clap::{Parser, Subcommand};

#[derive(Parser, Debug)]
#[command(name = "pmmm", version, about = "Polymarket Market Maker")]
pub struct Cli {
    #[command(subcommand)]
    pub command: Command,
}

#[derive(Subcommand, Debug)]
pub enum Command {
    /// Print theo P(Up) for a hand-rolled scenario; useful for parity testing.
    TheoTest(TheoTestArgs),
    /// Print the loaded TheoV6Params JSON shape.
    TheoParamsShow {
        #[arg(long)]
        path: String,
    },
    /// Connect all three feeds (Pyth + Coinbase + Binance) for N seconds and
    /// print per-source first-tick-win share. The Rust equivalent of the
    /// Phase 0 Python smoke test that measured Pyth at 48%.
    FeedsTest {
        /// Comma-separated coins (default: BTC,ETH,SOL,XRP)
        #[arg(long, default_value = "BTC,ETH,SOL,XRP")]
        coins: String,
        /// Duration in seconds.
        #[arg(long, default_value_t = 30)]
        duration_s: u64,
    },
    /// Main trading loop. Defaults to shadow mode (no orders submitted).
    Run(RunArgs),
}

#[derive(clap::Args, Debug)]
pub struct RunArgs {
    /// shadow | live
    #[arg(long, default_value = "shadow")]
    pub mode: String,
    /// Comma-separated coins.
    #[arg(long, default_value = "BTC,ETH,SOL,XRP")]
    pub coins: String,
    /// Comma-separated timeframes: 5m, 15m, 1h
    #[arg(long, default_value = "5m")]
    pub timeframes: String,
    /// Audit log prefix (a `_YYYYMMDD.jsonl` suffix is appended).
    #[arg(long, default_value = "/opt/polymarket-bot/data/pricing_model/pmmm_audit")]
    pub audit_prefix: String,
    /// Optional path to a `theo_v6_current.json`. If absent, theo is None
    /// in QuoterContext and v7 still works (it uses PM mid, not theo).
    #[arg(long)]
    pub theo_params: Option<String>,
    /// Optional path to an L2 creds JSON (api_key, secret, passphrase).
    /// Required for Live mode.
    #[arg(long)]
    pub l2_creds: Option<String>,
    /// Max session duration in seconds. 0 = run forever.
    #[arg(long, default_value_t = 0u64)]
    pub duration_s: u64,
    /// Quoter strategy: `v7` (favorite-bias, legacy) or `paam` (PAAM v1
    /// mid-anchored maker, default).
    #[arg(long, default_value = "paam")]
    pub quoter: String,
}

#[derive(clap::Args, Debug)]
pub struct TheoTestArgs {
    /// Coin symbol (BTC, ETH, SOL, XRP, DOGE, BNB, HYPE).
    #[arg(long, default_value = "BTC")]
    pub coin: String,
    /// Strike price (in USD).
    #[arg(long)]
    pub strike: f64,
    /// Time remaining in seconds.
    #[arg(long, default_value_t = 60.0)]
    pub ttm: f64,
    /// Path to a theo_v6_current.json file (TheoV6Params).
    #[arg(long)]
    pub params_path: String,
    /// Path to a series JSON `[[t, price], ...]` to use for σ.
    #[arg(long)]
    pub series_path: String,
    /// Wall-clock t (seconds, matches series timestamp scale).
    #[arg(long)]
    pub t: f64,
    /// Spot price at time t.
    #[arg(long)]
    pub spot: f64,
}
