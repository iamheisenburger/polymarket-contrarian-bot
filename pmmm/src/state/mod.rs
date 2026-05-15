//! Market state, position state, open-order state.
//!
//! Per-market state lives behind `ArcSwap` so strategy reads never contend
//! with the WS writer. Open orders are tracked per (slug, side) — matching
//! the Python `QuotingState` two-sided structure.

use std::collections::HashMap;
use std::sync::Arc;

use alloy_primitives::U256;
use arc_swap::ArcSwap;
use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use crate::types::{BinarySide, OrderSide, Symbol};

/// A single live order we placed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LiveOrder {
    pub order_id: String,
    pub slug: String,
    pub side_token: BinarySide, // which token (UP or DOWN) this order is on
    pub order_side: OrderSide,
    pub price: f64,
    pub size: f64,
    pub placed_ts_ns: u64,
    pub filled_size: f64,
    pub status: String, // live | matched | cancelled | failed
}

/// Per-(slug, BinarySide) quoting state — matches Python `QuotingState`.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct QuotingState {
    pub token_id: String,
    pub delta_shares: f64, // net shares of THIS side's token
    pub cash_flow: f64,
    pub avg_cost: Option<f64>,
    pub open_orders: HashMap<String, LiveOrder>,
    pub last_quote_ts_ns: u64,
    pub last_theo: Option<f64>,
}

/// Full state for one 5m/15m/1h market.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MarketState {
    pub slug: String,
    pub coin: Symbol,
    pub tf: String, // "5m", "15m", "1h"
    pub window_s: u64,
    pub market_start_ts: i64,
    /// Unix seconds when WE first discovered this market (via Gamma).
    /// PAAM uses this vs `market_start_ts` to skip "mid-window joins" —
    /// markets that were already running when we started.
    pub discovered_at_unix_s: f64,
    pub up_token_id: String,
    pub down_token_id: String,
    pub strike: f64,
    pub quoting_up: QuotingState,
    pub quoting_down: QuotingState,
    pub halted: bool, // per-market halt (max_position breach)
}

impl MarketState {
    pub fn new(
        slug: String,
        coin: Symbol,
        tf: String,
        window_s: u64,
        market_start_ts: i64,
        up_token_id: String,
        down_token_id: String,
        strike: f64,
    ) -> Self {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(market_start_ts as f64);
        Self {
            slug,
            coin,
            tf,
            window_s,
            market_start_ts,
            discovered_at_unix_s: now,
            up_token_id: up_token_id.clone(),
            down_token_id: down_token_id.clone(),
            strike,
            quoting_up: QuotingState {
                token_id: up_token_id,
                ..Default::default()
            },
            quoting_down: QuotingState {
                token_id: down_token_id,
                ..Default::default()
            },
            halted: false,
        }
    }

    /// How many seconds into its window was this market when we discovered it.
    /// PAAM skips placement on markets with `discovery_delay_s > N`.
    pub fn discovery_delay_s(&self) -> f64 {
        self.discovered_at_unix_s - self.market_start_ts as f64
    }

    pub fn quoting(&self, side: BinarySide) -> &QuotingState {
        match side {
            BinarySide::Up => &self.quoting_up,
            BinarySide::Down => &self.quoting_down,
        }
    }

    pub fn quoting_mut(&mut self, side: BinarySide) -> &mut QuotingState {
        match side {
            BinarySide::Up => &mut self.quoting_up,
            BinarySide::Down => &mut self.quoting_down,
        }
    }

    pub fn total_delta(&self) -> f64 {
        self.quoting_up.delta_shares + self.quoting_down.delta_shares
    }

    pub fn total_cash_flow(&self) -> f64 {
        self.quoting_up.cash_flow + self.quoting_down.cash_flow
    }

    /// UP+DOWN pair cost — locked profit if cost_per_pair < $1.00 minus fees.
    pub fn cost_per_pair(&self) -> Option<f64> {
        let pairs = self
            .quoting_up
            .delta_shares
            .min(self.quoting_down.delta_shares);
        if pairs <= 0.0 {
            return None;
        }
        Some((-self.quoting_up.cash_flow + -self.quoting_down.cash_flow) / pairs)
    }
}

/// Global state shared across the bot. Lock-free reads via `DashMap`;
/// writes happen from the user-WS fill handler and the registry watcher.
#[derive(Default)]
pub struct GlobalState {
    /// All currently-tracked markets, keyed by slug.
    pub markets: DashMap<String, Arc<ArcSwap<MarketState>>>,
    /// Token-id → (slug, BinarySide) lookup for user-WS dispatch.
    pub token_to_slug: DashMap<String, (String, BinarySide)>,
    /// Aggregate counters (lifetime of process).
    pub n_placed: std::sync::atomic::AtomicU64,
    pub n_cancelled: std::sync::atomic::AtomicU64,
    pub n_filled: std::sync::atomic::AtomicU64,
}

impl GlobalState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn get(&self, slug: &str) -> Option<Arc<MarketState>> {
        self.markets.get(slug).map(|e| e.value().load_full())
    }

    pub fn insert(&self, m: MarketState) {
        let slug = m.slug.clone();
        let up_id = m.up_token_id.clone();
        let down_id = m.down_token_id.clone();
        self.token_to_slug.insert(up_id, (slug.clone(), BinarySide::Up));
        self.token_to_slug.insert(down_id, (slug.clone(), BinarySide::Down));
        self.markets
            .insert(slug, Arc::new(ArcSwap::from_pointee(m)));
    }

    /// Atomic read-modify-write of one MarketState. `f` is called with the
    /// current state and returns the new state to publish.
    pub fn update<F>(&self, slug: &str, f: F) -> bool
    where
        F: FnOnce(&MarketState) -> MarketState,
    {
        if let Some(entry) = self.markets.get(slug) {
            let arc = entry.value();
            let cur = arc.load_full();
            let next = f(&cur);
            arc.store(Arc::new(next));
            true
        } else {
            false
        }
    }

    pub fn cleanup(&self, slug: &str) {
        if let Some((_, arc)) = self.markets.remove(slug) {
            let m = arc.load_full();
            self.token_to_slug.remove(&m.up_token_id);
            self.token_to_slug.remove(&m.down_token_id);
        }
    }
}

/// Used for U256-converted prices/sizes in the EIP-712 Order — typed wrapper
/// for clarity in strategy code.
#[derive(Debug, Clone, Copy)]
pub struct Wei6(pub U256);

impl Wei6 {
    /// Build from a USDC-denominated float (e.g. price * size).
    pub fn from_usd(usd: f64) -> Self {
        let scaled = (usd * 1_000_000.0).round() as u128;
        Wei6(U256::from(scaled))
    }
    pub fn from_token(tokens: f64) -> Self {
        let scaled = (tokens * 1_000_000.0).round() as u128;
        Wei6(U256::from(scaled))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn market_state_pair_cost() {
        let mut m = MarketState::new(
            "btc-updown-5m-1".into(),
            Symbol::BTC,
            "5m".into(),
            300,
            1_700_000_000,
            "up-tok".into(),
            "down-tok".into(),
            79_000.0,
        );
        // No pairs yet
        assert!(m.cost_per_pair().is_none());

        // 5 UP @ $0.50 = $2.50; 5 DOWN @ $0.45 = $2.25
        m.quoting_up.delta_shares = 5.0;
        m.quoting_up.cash_flow = -2.5;
        m.quoting_down.delta_shares = 5.0;
        m.quoting_down.cash_flow = -2.25;
        let c = m.cost_per_pair().unwrap();
        // (2.5 + 2.25) / 5 = 0.95 per pair → locked $0.05/pair profit
        assert!((c - 0.95).abs() < 1e-9, "got {c}");
        assert!(c < 1.0, "should be lockable");
    }

    #[test]
    fn global_state_lockless_update() {
        let g = GlobalState::new();
        g.insert(MarketState::new(
            "slug-1".into(),
            Symbol::BTC,
            "5m".into(),
            300,
            1_700_000_000,
            "u".into(),
            "d".into(),
            79_000.0,
        ));
        let updated = g.update("slug-1", |s| {
            let mut new = s.clone();
            new.quoting_up.delta_shares += 5.0;
            new.quoting_up.cash_flow -= 2.5;
            new
        });
        assert!(updated);
        let snap = g.get("slug-1").unwrap();
        assert_eq!(snap.quoting_up.delta_shares, 5.0);
        assert_eq!(snap.quoting_up.cash_flow, -2.5);
    }

    #[test]
    fn wei6_conversions() {
        let w = Wei6::from_usd(1.5);
        assert_eq!(w.0, U256::from(1_500_000u64));
        let w = Wei6::from_token(5.0);
        assert_eq!(w.0, U256::from(5_000_000u64));
    }
}
