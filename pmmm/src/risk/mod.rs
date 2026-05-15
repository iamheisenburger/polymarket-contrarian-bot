//! Risk gates — position caps, max-loss kill switch, max-quote-cost,
//! consecutive-place-fails circuit breaker, volatility halt, theo-warmup
//! gate, min-bid-elapsed and max-bid-elapsed window timing.
//!
//! Direct port of the v7.0 safety stack in
//! `scripts/pricing_model/ohan_mm_v7_0.py`. Every check returns a `RiskDecision`
//! so the caller can log + react.

use std::sync::atomic::{AtomicU32, AtomicU64, Ordering};
use std::time::Duration;

use serde::{Deserialize, Serialize};

/// Hard limits configured per-session. Loaded from TOML or CLI flags.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RiskConfig {
    /// Per-(market, side) token-count cap. Fill-time enforced.
    pub max_position_tokens: f64,
    /// Per-market gross $-committed cap.
    pub max_quote_cost_usd: f64,
    /// Session cumulative loss kill — daemon exits when realized+unrealized
    /// loss exceeds this.
    pub max_loss_usd: f64,
    /// Minimum bid price (skips $0.20-$0.60 trap zone per v7.0 forensic).
    pub min_bid_price: f64,
    /// Ceiling — don't quote above this (no upside).
    pub max_bid_price: f64,
    /// Offset below mid for placement.
    pub bid_offset: f64,
    /// Volatility halt: if Binance |Δ| / mid > this over the window, cancel-all.
    pub volatility_halt_pct: f64,
    pub volatility_window_s: f64,
    /// Window timing — don't quote before t+X or after T-Y.
    pub min_market_elapsed_s: f64,
    pub max_market_elapsed_s: f64,
    /// UP+DOWN combined cost halt.
    pub updown_cost_halt: f64,
    /// Consecutive placement failures before circuit-breaker engages.
    pub place_fail_circuit: u32,
    pub place_fail_backoff_s: f64,
    /// Min PM order constraints — hard rejects.
    pub min_tokens: f64,
    pub min_notional_usd: f64,
}

impl RiskConfig {
    /// Production-safe defaults derived from v7.0 final config.
    pub fn v7_default() -> Self {
        Self {
            max_position_tokens: 5.0,
            max_quote_cost_usd: 10.0,
            max_loss_usd: 5.0,
            min_bid_price: 0.60,
            max_bid_price: 0.95,
            bid_offset: 0.03,
            volatility_halt_pct: 0.0015,
            volatility_window_s: 5.0,
            min_market_elapsed_s: 10.0,
            max_market_elapsed_s: 240.0,
            updown_cost_halt: 0.94,
            place_fail_circuit: 5,
            place_fail_backoff_s: 30.0,
            min_tokens: 5.0,
            min_notional_usd: 1.01,
        }
    }

    /// PAAM v1 defaults — same risk envelope but `min_bid_price` lowered so
    /// the two-sided maker can quote both the favorite and the underdog
    /// side. V7's 0.60 floor is for favorite-bias only.
    /// `min_notional_usd: 1.01` still requires price × size ≥ $1.01, so the
    /// effective floor at size=5 is $0.21.
    pub fn paam_default() -> Self {
        let mut c = Self::v7_default();
        c.min_bid_price = 0.05;            // PM tick-size respect
        c.max_bid_price = 0.95;            // unchanged
        c.min_market_elapsed_s = 30.0;     // matches PaamConfig
        c.max_market_elapsed_s = 295.0;    // 5s before settle
        c
    }
}

/// Result of a risk check.
#[derive(Debug, Clone, PartialEq)]
pub enum RiskDecision {
    Pass,
    Reject(&'static str, String),
}

impl RiskDecision {
    pub fn ok(&self) -> bool {
        matches!(self, RiskDecision::Pass)
    }
}

/// Runtime state for the risk stack — atomics so it's cheap to update from
/// the hot path.
#[derive(Default)]
pub struct RiskState {
    pub consecutive_place_fails: AtomicU32,
    pub place_circuit_open_until_ns: AtomicU64,
    pub kill_switch_engaged: std::sync::atomic::AtomicBool,
}

impl RiskState {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn engage_kill(&self) {
        self.kill_switch_engaged.store(true, Ordering::Release);
    }

    pub fn is_killed(&self) -> bool {
        self.kill_switch_engaged.load(Ordering::Acquire)
    }

    pub fn record_place_fail(&self, cfg: &RiskConfig, now_ns: u64) {
        let n = self
            .consecutive_place_fails
            .fetch_add(1, Ordering::AcqRel)
            + 1;
        if n >= cfg.place_fail_circuit {
            let backoff_ns =
                (cfg.place_fail_backoff_s * 1_000_000_000.0).round() as u64;
            self.place_circuit_open_until_ns
                .store(now_ns.saturating_add(backoff_ns), Ordering::Release);
        }
    }

    pub fn reset_place_fails(&self) {
        self.consecutive_place_fails.store(0, Ordering::Release);
        self.place_circuit_open_until_ns.store(0, Ordering::Release);
    }

    pub fn circuit_open(&self, now_ns: u64) -> bool {
        self.place_circuit_open_until_ns.load(Ordering::Acquire) > now_ns
    }
}

/// Single-bid risk check. Called before signing+sending a new order.
pub fn check_place(
    cfg: &RiskConfig,
    state: &RiskState,
    price: f64,
    size: f64,
    cur_delta_shares: f64,
    open_buy_cost: f64,
    free_usdc: f64,
    committed: f64,
    market_elapsed_s: f64,
    now_ns: u64,
) -> RiskDecision {
    if state.is_killed() {
        return RiskDecision::Reject("kill_switch", "session killed".into());
    }
    if state.circuit_open(now_ns) {
        return RiskDecision::Reject(
            "circuit_breaker",
            "consecutive place fails — backoff active".into(),
        );
    }
    if size < cfg.min_tokens {
        return RiskDecision::Reject(
            "min_tokens",
            format!("size {} < min {}", size, cfg.min_tokens),
        );
    }
    if price * size < cfg.min_notional_usd {
        return RiskDecision::Reject(
            "min_notional",
            format!(
                "notional ${:.4} < min ${:.4}",
                price * size,
                cfg.min_notional_usd
            ),
        );
    }
    if price < cfg.min_bid_price {
        return RiskDecision::Reject(
            "below_min_bid_price",
            format!("price {} < min {}", price, cfg.min_bid_price),
        );
    }
    if price > cfg.max_bid_price {
        return RiskDecision::Reject(
            "above_max_bid_price",
            format!("price {} > max {}", price, cfg.max_bid_price),
        );
    }
    if cur_delta_shares + size > cfg.max_position_tokens {
        return RiskDecision::Reject(
            "max_position",
            format!(
                "delta {} + {} > cap {}",
                cur_delta_shares, size, cfg.max_position_tokens
            ),
        );
    }
    if open_buy_cost + price * size > cfg.max_quote_cost_usd {
        return RiskDecision::Reject(
            "max_quote_cost",
            format!(
                "cost ${:.2} + ${:.2} > cap ${:.2}",
                open_buy_cost,
                price * size,
                cfg.max_quote_cost_usd
            ),
        );
    }
    if market_elapsed_s < cfg.min_market_elapsed_s {
        return RiskDecision::Reject(
            "min_market_elapsed",
            format!(
                "elapsed {:.1} < min {:.1}",
                market_elapsed_s, cfg.min_market_elapsed_s
            ),
        );
    }
    if market_elapsed_s > cfg.max_market_elapsed_s {
        return RiskDecision::Reject(
            "max_market_elapsed",
            format!(
                "elapsed {:.1} > max {:.1}",
                market_elapsed_s, cfg.max_market_elapsed_s
            ),
        );
    }
    let need = price * size + 0.50; // 50c safety buffer
    if need > free_usdc - committed {
        return RiskDecision::Reject(
            "insufficient_balance",
            format!(
                "need ${:.2} but free ${:.2} - committed ${:.2} = ${:.2}",
                need,
                free_usdc,
                committed,
                free_usdc - committed
            ),
        );
    }
    RiskDecision::Pass
}

/// Compute backoff Duration remaining on the place-fail circuit breaker.
pub fn circuit_remaining(state: &RiskState, now_ns: u64) -> Duration {
    let until = state.place_circuit_open_until_ns.load(Ordering::Acquire);
    if until > now_ns {
        Duration::from_nanos(until - now_ns)
    } else {
        Duration::ZERO
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cfg() -> RiskConfig {
        RiskConfig::v7_default()
    }
    fn good_ctx() -> (f64, f64, f64, f64, f64, f64, f64) {
        // price, size, cur_delta, open_cost, free, committed, elapsed
        (0.70, 5.0, 0.0, 0.0, 25.0, 0.0, 60.0)
    }

    #[test]
    fn passes_typical() {
        let s = RiskState::new();
        let (p, sz, cd, oc, free, com, el) = good_ctx();
        let r = check_place(&cfg(), &s, p, sz, cd, oc, free, com, el, 0);
        assert_eq!(r, RiskDecision::Pass);
    }

    #[test]
    fn rejects_below_min_bid() {
        let s = RiskState::new();
        let r = check_place(&cfg(), &s, 0.50, 5.0, 0.0, 0.0, 25.0, 0.0, 60.0, 0);
        match r {
            RiskDecision::Reject(reason, _) => assert_eq!(reason, "below_min_bid_price"),
            _ => panic!("should reject"),
        }
    }

    #[test]
    fn rejects_min_tokens() {
        let s = RiskState::new();
        let r = check_place(&cfg(), &s, 0.70, 3.0, 0.0, 0.0, 25.0, 0.0, 60.0, 0);
        match r {
            RiskDecision::Reject(reason, _) => assert_eq!(reason, "min_tokens"),
            _ => panic!(),
        }
    }

    #[test]
    fn rejects_max_position_at_fill_time() {
        let s = RiskState::new();
        // Already at 5 tokens; trying to add 5 more would breach.
        let r = check_place(&cfg(), &s, 0.70, 5.0, 5.0, 0.0, 25.0, 0.0, 60.0, 0);
        match r {
            RiskDecision::Reject(reason, _) => assert_eq!(reason, "max_position"),
            _ => panic!(),
        }
    }

    #[test]
    fn rejects_when_killed() {
        let s = RiskState::new();
        s.engage_kill();
        let (p, sz, cd, oc, free, com, el) = good_ctx();
        let r = check_place(&cfg(), &s, p, sz, cd, oc, free, com, el, 0);
        match r {
            RiskDecision::Reject(reason, _) => assert_eq!(reason, "kill_switch"),
            _ => panic!(),
        }
    }

    #[test]
    fn circuit_engages_after_n_fails() {
        let s = RiskState::new();
        let c = cfg();
        let now = 0u64;
        for _ in 0..c.place_fail_circuit {
            s.record_place_fail(&c, now);
        }
        assert!(s.circuit_open(now));
        // After backoff window, circuit closes (simulate time advancing).
        let after = (c.place_fail_backoff_s * 1e9) as u64 + 1;
        assert!(!s.circuit_open(after));
    }

    #[test]
    fn place_fail_reset_clears_circuit() {
        let s = RiskState::new();
        let c = cfg();
        for _ in 0..c.place_fail_circuit {
            s.record_place_fail(&c, 0);
        }
        s.reset_place_fails();
        assert!(!s.circuit_open(0));
    }
}
