//! Price feed module — Pyth, Coinbase, Binance WS clients plus the
//! unified `SpotPulse` aggregator + `PriceFeed` trait.

pub mod binance;
pub mod coinbase;
pub mod pyth;
pub mod spot_pulse;

use async_trait::async_trait;

use crate::types::{PriceTick, Symbol};

/// Unified trait for all price feed sources (Pyth, Coinbase, Binance).
#[async_trait]
pub trait PriceFeed: Send + Sync {
    /// Human-readable source name (e.g. "Pyth").
    fn name(&self) -> &'static str;
    /// Symbols this feed supports.
    fn supported(&self) -> &[Symbol];
    /// Connect + subscribe. Returns once the underlying WS is established.
    async fn start(&mut self) -> anyhow::Result<()>;
    /// Latest cached tick for a symbol (lock-free).
    fn latest(&self, symbol: Symbol) -> Option<PriceTick>;
}

pub use binance::BinanceFeed;
pub use coinbase::CoinbaseFeed;
pub use pyth::PythFeed;
pub use spot_pulse::SpotPulse;
