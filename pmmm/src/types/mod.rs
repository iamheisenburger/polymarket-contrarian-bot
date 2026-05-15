//! Shared domain types used across modules.
//!
//! These mirror the Python types in `lib/edge_pipeline.py` (TwoSidedBook,
//! SpotPulse) and `src/signer.py` (Order) closely enough that we can do
//! one-to-one parity testing against the reference Python implementation.

use std::time::{Duration, SystemTime, UNIX_EPOCH};

use alloy_primitives::{Address, U256};
use serde::{Deserialize, Serialize};

/// Single tick of price data from any feed source.
///
/// `ts_local_ns` is the local monotonic timestamp at which we received the
/// tick — this is what we compare across feeds for first-tick-wins, NOT the
/// publish time embedded in the upstream message. Pyth specifically: the
/// `publish_time` field is when an MM published to Hermes, not when we got it.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct PriceTick {
    pub symbol: Symbol,
    pub source: FeedSource,
    pub mid: f64,
    pub ts_local_ns: u64,
}

/// Identity of which exchange/aggregator produced a tick.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Hash, Serialize, Deserialize)]
pub enum FeedSource {
    Pyth,
    Coinbase,
    Binance,
}

impl FeedSource {
    pub fn as_str(&self) -> &'static str {
        match self {
            FeedSource::Pyth => "Pyth",
            FeedSource::Coinbase => "Coinbase",
            FeedSource::Binance => "Binance",
        }
    }
}

/// A supported coin symbol.
///
/// We enumerate the supported set so misspellings are compile errors. Map to
/// per-feed wire symbols inside each feed implementation.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[allow(clippy::upper_case_acronyms)]
pub enum Symbol {
    BTC,
    ETH,
    SOL,
    XRP,
    DOGE,
    BNB,
    HYPE,
}

impl Symbol {
    pub fn as_str(&self) -> &'static str {
        match self {
            Symbol::BTC => "BTC",
            Symbol::ETH => "ETH",
            Symbol::SOL => "SOL",
            Symbol::XRP => "XRP",
            Symbol::DOGE => "DOGE",
            Symbol::BNB => "BNB",
            Symbol::HYPE => "HYPE",
        }
    }

    pub fn all() -> &'static [Symbol] {
        &[
            Symbol::BTC,
            Symbol::ETH,
            Symbol::SOL,
            Symbol::XRP,
            Symbol::DOGE,
            Symbol::BNB,
            Symbol::HYPE,
        ]
    }

    pub fn parse(s: &str) -> Option<Symbol> {
        match s.to_ascii_uppercase().as_str() {
            "BTC" => Some(Symbol::BTC),
            "ETH" => Some(Symbol::ETH),
            "SOL" => Some(Symbol::SOL),
            "XRP" => Some(Symbol::XRP),
            "DOGE" => Some(Symbol::DOGE),
            "BNB" => Some(Symbol::BNB),
            "HYPE" => Some(Symbol::HYPE),
            _ => None,
        }
    }
}

impl std::fmt::Display for Symbol {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Snapshot of the latest spot price for one coin, exposed to strategy code.
///
/// Updated atomically via `arc-swap` — readers see a consistent view.
#[derive(Debug, Clone, Copy, Serialize, Deserialize)]
pub struct SpotSnapshot {
    pub symbol: Symbol,
    pub mid: f64,
    pub source: FeedSource,
    pub ts_local_ns: u64,
    /// Local monotonic timestamp at which feeds for this symbol were warm
    /// (received their first tick). None until warmup completes.
    pub warm_at_ns: Option<u64>,
}

/// Side of a Polymarket Up/Down market.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum BinarySide {
    Up,
    Down,
}

impl BinarySide {
    pub fn as_str(&self) -> &'static str {
        match self {
            BinarySide::Up => "up",
            BinarySide::Down => "down",
        }
    }

    pub fn opposite(self) -> BinarySide {
        match self {
            BinarySide::Up => BinarySide::Down,
            BinarySide::Down => BinarySide::Up,
        }
    }
}

/// Order side at the CLOB level (buy/sell of a single ERC-1155 token).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum OrderSide {
    Buy,
    Sell,
}

impl OrderSide {
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderSide::Buy => "BUY",
            OrderSide::Sell => "SELL",
        }
    }

    pub fn as_u8(&self) -> u8 {
        match self {
            OrderSide::Buy => 0,
            OrderSide::Sell => 1,
        }
    }
}

/// Polymarket order types.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum OrderType {
    /// Good-Til-Cancelled (resting maker order).
    Gtc,
    /// Good-Til-Date (resting with expiration).
    Gtd,
    /// Fill-Or-Kill (full marketable, else reject).
    Fok,
    /// Fill-And-Kill (partial-fill marketable, remainder cancelled).
    Fak,
}

impl OrderType {
    pub fn as_str(&self) -> &'static str {
        match self {
            OrderType::Gtc => "GTC",
            OrderType::Gtd => "GTD",
            OrderType::Fok => "FOK",
            OrderType::Fak => "FAK",
        }
    }
}

/// Tick size of a market — Polymarket dynamically switches tick size based on
/// price band (0.0001 / 0.001 / 0.01 / 0.1).
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum TickSize {
    P0001, // 0.0001
    P001,  // 0.001
    P01,   // 0.01
    P1,    // 0.1
}

impl TickSize {
    pub fn as_f64(&self) -> f64 {
        match self {
            TickSize::P0001 => 0.0001,
            TickSize::P001 => 0.001,
            TickSize::P01 => 0.01,
            TickSize::P1 => 0.1,
        }
    }

    pub fn as_str(&self) -> &'static str {
        match self {
            TickSize::P0001 => "0.0001",
            TickSize::P001 => "0.001",
            TickSize::P01 => "0.01",
            TickSize::P1 => "0.1",
        }
    }
}

/// EIP-712 Order struct for Polymarket CTF Exchange **v2** (deployed
/// 2026-05-14, contract `0xE111180000d2663C0091e4f400237545B87B996B`).
///
/// **Schema change vs v1:** removed `taker`, `expiration`, `nonce`,
/// `feeRateBps`; added `timestamp`, `metadata`, `builder`. `expiration` is
/// still in the wire body but is NO LONGER part of the EIP-712 hash.
///
/// Amount fields are integer counts of the smallest unit (USDC has 6
/// decimals; conditional tokens are 6 decimals). Wire serializes amounts
/// as decimal strings; EIP-712 hash uses U256 directly.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Order {
    pub salt: U256,
    pub maker: Address,
    pub signer: Address,
    pub token_id: U256,
    pub maker_amount: U256,
    pub taker_amount: U256,
    pub side: u8,
    pub signature_type: u8,
    /// Order creation timestamp, milliseconds since unix epoch.
    pub timestamp: U256,
    /// Caller-defined metadata bytes (zero-bytes32 unless used).
    pub metadata: alloy_primitives::B256,
    /// Builder-attribution bytes (zero-bytes32 unless used).
    pub builder: alloy_primitives::B256,
    /// Expiration unix seconds (wire-only, NOT hashed). 0 = no expiration.
    pub expiration: U256,
}

/// A signed order ready to be POST'd to /order or /orders.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignedOrder {
    pub order: Order,
    /// 65-byte EIP-712 signature, hex-encoded with 0x prefix.
    pub signature: String,
    /// Address of the funder (Safe address for signature_type=2).
    pub owner: Address,
}

/// Wall-clock time in nanoseconds since epoch — used for log timestamps.
pub fn now_unix_ns() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or(Duration::ZERO)
        .as_nanos() as u64
}

/// Monotonic time in nanoseconds since process start — used for latency and
/// freshness. Uses `quanta::Instant` semantics under the hood but exposed as
/// a plain u64 for serialization.
pub fn now_mono_ns() -> u64 {
    // We pick a single global Clock once; subsequent reads are cheap.
    static CLOCK: once_cell::sync::Lazy<quanta::Clock> = once_cell::sync::Lazy::new(quanta::Clock::new);
    CLOCK.raw()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn symbol_roundtrip() {
        for s in Symbol::all() {
            assert_eq!(Some(*s), Symbol::parse(s.as_str()));
        }
    }

    #[test]
    fn binary_side_opposite() {
        assert_eq!(BinarySide::Up.opposite(), BinarySide::Down);
        assert_eq!(BinarySide::Down.opposite(), BinarySide::Up);
    }

    #[test]
    fn order_side_byte() {
        assert_eq!(OrderSide::Buy.as_u8(), 0);
        assert_eq!(OrderSide::Sell.as_u8(), 1);
    }
}
