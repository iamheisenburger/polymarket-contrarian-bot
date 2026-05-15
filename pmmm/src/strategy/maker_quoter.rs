//! PAAM v1 — two-sided spread-capture maker.
//!
//! Per the approved plan at `~/.claude/plans/glimmering-knitting-stream.md`:
//! place `postOnly` resting BUYs on BOTH sides of each PM 5m crypto market
//! at `bid + improvement`, hold to settle, cap inventory at one fill per
//! side. The strategy is "be second-best on the book, collect uninformed
//! taker flow over time."
//!
//! Empirical justification (n=4309 V7 shadow snapshots, 2026-05-14):
//!   sum_of_bids:  median=0.99, p25=0.97, p10=0.95, p1=0.84
//!   21% of moments have sum < 0.97 (3c+ headroom after our +2c)
//!   9% have sum < 0.95 (5c+ clear edge)
//!   1.6% have sum < 0.90 (10c+ tail edge)
//! So two-sided spread capture is structurally viable but only ~10-20% of
//! the time. The `max_total_bid` gate skips placement when there's no edge.
//!
//! Edge math: when up_bid + down_bid = S < 1.00, improving each by 1c
//! gives us round-trip cost = S + 0.02. Both sides fill (rare, requires
//! uninformed flow on both): payout $1, profit (1 - S - 0.02). Only one
//! side fills (common, hold to settle): payout $0 or $1, P&L depends on
//! which side wins. The two-sided shape concentrates wins on cross-trades
//! while bounding losses to one fill per side.
//!
//! v1 explicitly defers (Phase 2/3 work):
//! - Theo-anchored pricing (current theo_v6 calibration broken, intercept
//!   dominates → theo always ~0.001). Will replace bid-improvement with
//!   theo-driven pricing once we have a calibrated theo.
//! - Pyth-tick fast cancel channel. v1 cancels via 1Hz drift-replace.
//! - AS circuit breaker. Will tune from observed markout distribution.
//!
//! v1 is intentionally NOT a directional bet (not Ohan's favorite-bias,
//! not the inverse underdog thesis). It captures spread mechanically when
//! the book offers it, skips when it doesn't.

use std::sync::Arc;

use dashmap::DashMap;
use serde::{Deserialize, Serialize};

use crate::strategy::quoter::{QuoteIntent, Quoter, QuoterContext};
use crate::types::{BinarySide, OrderType};

/// Default theo-divergence threshold for AS-cancel decisions, in P-units
/// (0.03 = 3 cents of binary-option price). When current theo on a side
/// has moved against our resting bid by ≥ this much from the last_theo
/// captured at place time, we cancel.
pub const PAAM_THEO_AS_THRESHOLD: f64 = 0.05;

/// Tunable parameters for PAAM v1.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PaamConfig {
    /// Cents added to PM's best bid as our quote. Sits us at the front of
    /// the queue without overpaying.
    pub bid_improvement: f64,
    /// Tokens per order (PM minimum = 5).
    pub quote_size: f64,
    /// Per-side bid quoting band. Below min: deep-OTM, illiquid. Above max:
    /// gamma-risk near settlement.
    pub min_bid: f64,
    pub max_bid: f64,
    /// Skip the first N seconds of each market window (book settling).
    pub min_market_elapsed_s: f64,
    /// Stop quoting in the last N seconds of the window.
    pub min_tte_s: f64,
    /// Max sum of (up_bid, down_bid) we'll quote into. If PM's two bids
    /// already sum to this much, improving each by 1c puts us at S + 0.02
    /// which is ≥ 1.00 — no edge. Empirically 21% of moments have
    /// sum < 0.97 and 9% have sum < 0.95. Default 0.95 = 9% quote rate
    /// with ≥5c edge headroom.
    pub max_total_bid: f64,
    /// PM "placeholder ask" detection: if either ask is ≥ this, the spread
    /// is dominated by stub orders and the joint book is meaningless. Skip.
    pub max_ask_placeholder: f64,
    /// If existing order's price drifts from the new target by ≥ this,
    /// cancel + replace.
    pub price_drift_replace: f64,
    /// Max position per (market, side) in tokens.
    pub max_position_tokens: f64,
    /// Theo-divergence threshold for AS-cancel on spot-tick path.
    /// 0.03 = 3 cents of binary-option price.
    pub theo_as_threshold: f64,
    /// Entry filter: if either side's mid moved by ≥ this in the last
    /// `book_volatility_window_s`, skip new placements (only allow cancels).
    /// Protects against placing during transient book states where takers
    /// hit us before we can react.
    pub book_volatility_threshold: f64,
    /// Lookback window for entry filter (seconds).
    pub book_volatility_window_s: f64,
    /// Max seconds AFTER market start before we'll quote it. If we
    /// discovered the market more than this much past its start, we treat
    /// it as a "mid-window join" and skip placements entirely (only manage
    /// any orders we already have).
    pub max_discovery_delay_s: f64,
    /// Anti-chase: if a side's best bid drifted by ≥ this many cents over
    /// `chase_window_s`, suppress new placements on that side. Catches the
    /// "favorite is rising fast, we keep bidding higher" failure mode.
    pub chase_drift_threshold: f64,
    pub chase_window_s: f64,
}

impl PaamConfig {
    pub fn v1_default() -> Self {
        Self {
            bid_improvement: 0.01,
            quote_size: 5.0,
            min_bid: 0.10,
            max_bid: 0.90,
            min_market_elapsed_s: 30.0,
            min_tte_s: 5.0,
            max_total_bid: 0.97,
            max_ask_placeholder: 0.97,
            // Bumped 0.01 → 0.03: a 1c book wiggle was triggering constant
            // cancel-replace cycles (p50 order lifetime 614ms observed in
            // the 2026-05-15 live run). 3c lets orders breathe through
            // normal chatter while still abandoning genuinely stale quotes.
            price_drift_replace: 0.03,
            max_position_tokens: 5.0,
            theo_as_threshold: PAAM_THEO_AS_THRESHOLD,
            book_volatility_threshold: 0.02,
            book_volatility_window_s: 3.0,
            max_discovery_delay_s: 30.0,
            chase_drift_threshold: 0.05,
            chase_window_s: 30.0,
        }
    }
}

/// Cached observation of a market's book + timestamp. Used by the entry
/// filter (3-sec window mid drift) and chase suppression (30-sec window
/// per-side bid drift).
#[derive(Debug, Clone, Copy)]
struct BookSnap {
    up_mid: f64,
    down_mid: f64,
    up_bid: f64,
    down_bid: f64,
    ts_ns: u64,
}

/// PAAM v1 quoter. Per-market book history kept via interior mutability
/// (Arc<DashMap>) for the entry filter's volatility check.
pub struct PaamQuoter {
    pub cfg: PaamConfig,
    /// Per-slug history of book observations for the entry filter.
    book_history: Arc<DashMap<String, Vec<BookSnap>>>,
}

impl PaamQuoter {
    pub fn new() -> Self {
        Self {
            cfg: PaamConfig::v1_default(),
            book_history: Arc::new(DashMap::new()),
        }
    }
    pub fn with_config(cfg: PaamConfig) -> Self {
        Self { cfg, book_history: Arc::new(DashMap::new()) }
    }
}

impl Default for PaamQuoter {
    fn default() -> Self {
        Self::new()
    }
}

impl Quoter for PaamQuoter {
    fn name(&self) -> &'static str {
        "paam_v1"
    }

    fn decide(&self, ctx: &QuoterContext<'_>) -> Vec<QuoteIntent> {
        let mut out: Vec<QuoteIntent> = Vec::new();
        let m = ctx.market;

        // Fix #1: Skip mid-window joins.
        // If we discovered this market more than `max_discovery_delay_s`
        // after its start, we missed the warmup phase. Don't trade it.
        // Allow cancels of any orders we have (safety) but no NEW placements.
        let mid_window_join = m.discovery_delay_s() > self.cfg.max_discovery_delay_s;

        // Window gate.
        if ctx.elapsed_s < self.cfg.min_market_elapsed_s {
            out.push(QuoteIntent::Hold {
                slug: m.slug.clone(),
                reason: "paam_warmup",
            });
            return out;
        }
        if ctx.tte_s < self.cfg.min_tte_s {
            // Cancel any open orders — we're past the safe quoting window.
            for (oid, _) in m.quoting_up.open_orders.iter() {
                out.push(QuoteIntent::Cancel {
                    slug: m.slug.clone(),
                    side: BinarySide::Up,
                    order_id: oid.clone(),
                    reason: "paam_window_close",
                });
            }
            for (oid, _) in m.quoting_down.open_orders.iter() {
                out.push(QuoteIntent::Cancel {
                    slug: m.slug.clone(),
                    side: BinarySide::Down,
                    order_id: oid.clone(),
                    reason: "paam_window_close",
                });
            }
            return out;
        }

        // Joint book gates: do this market's two bids leave us any edge?
        let (up_bid, up_ask) = ctx.up_book;
        let (down_bid, down_ask) = ctx.down_book;
        let books_sane = up_bid > 0.0 && up_ask > up_bid
            && down_bid > 0.0 && down_ask > down_bid;

        // Entry filter: observe current mids, compare to recent observations
        // in the volatility window. If either side moved ≥ threshold in the
        // window, the book is "transient" — skip NEW placements (still allow
        // cancels and drift-replaces on existing).
        let now_ns = crate::types::now_mono_ns();
        let window_ns = (self.cfg.book_volatility_window_s * 1e9) as u64;
        let chase_window_ns = (self.cfg.chase_window_s * 1e9) as u64;
        let max_window_ns = window_ns.max(chase_window_ns);
        let up_mid = if books_sane { (up_bid + up_ask) / 2.0 } else { 0.0 };
        let down_mid = if books_sane { (down_bid + down_ask) / 2.0 } else { 0.0 };
        let mut book_volatile = false;
        // Fix #3: Chase detection per side. Track min/max bid in chase window.
        let mut up_chase = false;
        let mut down_chase = false;
        if books_sane {
            let mut entry = self.book_history.entry(m.slug.clone())
                .or_insert_with(Vec::new);
            // Trim entries older than the longer of the two windows.
            entry.retain(|s| now_ns.saturating_sub(s.ts_ns) <= max_window_ns);
            // Entry filter (3-sec mid-drift) AND chase detector (30-sec
            // bid-drift) computed in one pass.
            let mut up_bid_min = up_bid;
            let mut up_bid_max = up_bid;
            let mut down_bid_min = down_bid;
            let mut down_bid_max = down_bid;
            for s in entry.iter() {
                let age = now_ns.saturating_sub(s.ts_ns);
                if age <= window_ns {
                    if (s.up_mid - up_mid).abs() >= self.cfg.book_volatility_threshold
                        || (s.down_mid - down_mid).abs() >= self.cfg.book_volatility_threshold
                    {
                        book_volatile = true;
                    }
                }
                if age <= chase_window_ns {
                    if s.up_bid < up_bid_min { up_bid_min = s.up_bid; }
                    if s.up_bid > up_bid_max { up_bid_max = s.up_bid; }
                    if s.down_bid < down_bid_min { down_bid_min = s.down_bid; }
                    if s.down_bid > down_bid_max { down_bid_max = s.down_bid; }
                }
            }
            if (up_bid_max - up_bid_min) >= self.cfg.chase_drift_threshold {
                up_chase = true;
            }
            if (down_bid_max - down_bid_min) >= self.cfg.chase_drift_threshold {
                down_chase = true;
            }
            // Record current observation.
            entry.push(BookSnap {
                up_mid, down_mid,
                up_bid, down_bid,
                ts_ns: now_ns,
            });
        }

        // Note: we DON'T skip on placeholder asks ($0.99 stub orders are
        // standard on PM 5m books — they're not informative but don't block
        // edge either since postOnly check uses our target vs the ask, and
        // target = bid + 1c is always far below 0.99).
        let _ = (up_ask, down_ask); // suppress unused warning
        let skip_reason: Option<&'static str> = if !books_sane {
            Some("paam_book_unsane")
        } else if up_bid + down_bid > self.cfg.max_total_bid {
            // Joint book sums too high — improving each by 1c gives no edge.
            Some("paam_no_edge")
        } else {
            None
        };

        // Per-side: quote both UP and DOWN independently.
        // CTF-synthetic ask: PM's matching engine cross-check uses the
        // tighter of (raw_ask, 1 - opposite_side_bid). A target above the
        // synthetic ask hits postOnly "order crosses book" even when our
        // raw_ask check passes. Observed live 2026-05-15: 6/12 placements
        // rejected for this reason.
        let synth_up_ask = up_ask.min(1.0 - down_bid).max(0.0);
        let synth_down_ask = down_ask.min(1.0 - up_bid).max(0.0);
        for (side, (bid_s, ask_s), token_id, qs) in [
            (BinarySide::Up,   (up_bid, synth_up_ask),   m.up_token_id.clone(),   &m.quoting_up),
            (BinarySide::Down, (down_bid, synth_down_ask), m.down_token_id.clone(), &m.quoting_down),
        ] {
            // Joint skip: cancel any drift and bail this side.
            if let Some(reason) = skip_reason {
                for (oid, _) in qs.open_orders.iter() {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side,
                        order_id: oid.clone(),
                        reason,
                    });
                }
                continue;
            }

            // Fix #2: Inventory cap accounts for OPEN BUYS as well as fills.
            // Without this, the bot fires N+ placements before the first
            // fill is acknowledged via user-WS, blowing through the cap.
            let open_buy_size: f64 = qs.open_orders.values()
                .filter(|o| matches!(o.order_side, crate::types::OrderSide::Buy))
                .map(|o| o.size)
                .sum();
            let effective_position = qs.delta_shares + open_buy_size;
            if effective_position >= self.cfg.max_position_tokens {
                // Don't cancel the open orders here — they're our SOLE bids.
                // Cancelling would race against fill confirmation.
                continue;
            }

            // Per-side bid band.
            if bid_s < self.cfg.min_bid || bid_s > self.cfg.max_bid {
                for (oid, _) in qs.open_orders.iter() {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side,
                        order_id: oid.clone(),
                        reason: "paam_bid_out_of_band",
                    });
                }
                continue;
            }

            // Target = best bid + 1c.
            let target_bid = ((bid_s + self.cfg.bid_improvement) * 100.0).round() / 100.0;

            // postOnly sanity: must be strictly below the ask.
            if target_bid <= 0.05 || target_bid >= ask_s {
                for (oid, _) in qs.open_orders.iter() {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side,
                        order_id: oid.clone(),
                        reason: "paam_target_invalid",
                    });
                }
                continue;
            }

            // Replace existing if drifted.
            let mut has_at_target = false;
            for (oid, o) in qs.open_orders.iter() {
                if (o.price - target_bid).abs() >= self.cfg.price_drift_replace {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side,
                        order_id: oid.clone(),
                        reason: "paam_price_drift",
                    });
                } else {
                    has_at_target = true;
                }
            }

            if !has_at_target {
                let side_chasing = match side {
                    BinarySide::Up => up_chase,
                    BinarySide::Down => down_chase,
                };
                let reason: Option<&'static str> = if mid_window_join {
                    Some("paam_mid_window_join_skip")
                } else if book_volatile {
                    Some("paam_book_volatile_skip_entry")
                } else if side_chasing {
                    Some("paam_chase_skip_entry")
                } else {
                    None
                };
                if let Some(r) = reason {
                    out.push(QuoteIntent::Hold {
                        slug: m.slug.clone(),
                        reason: r,
                    });
                } else {
                    out.push(QuoteIntent::Place {
                        slug: m.slug.clone(),
                        side,
                        token_id,
                        price: target_bid,
                        size: self.cfg.quote_size,
                        order_type: OrderType::Gtc,
                        post_only: true,
                        reason: "paam_v1_two_sided",
                    });
                }
            }
        }

        out
    }

    /// Fast-path AS protection. Called from the Pyth-tick listener whenever
    /// spot moves significantly. Reads each side's `last_theo` (captured
    /// when we placed the resting order) and compares to the CURRENT
    /// `theo_p_up` in the context. If the relevant side's theo has moved
    /// AGAINST our resting bid by ≥ `theo_as_threshold`, emit a Cancel.
    ///
    /// Convention:
    ///   - UP-side bid: cancel when `theo_p_up_now < last_theo_up - threshold`
    ///     (UP becoming less likely → our UP bid overpriced → adverse)
    ///   - DOWN-side bid: equivalently, cancel when
    ///     `(1 - theo_p_up_now) < (1 - last_theo_down) - threshold`,
    ///     i.e. when `theo_p_up_now > last_theo_down + threshold`.
    ///     (Where last_theo_down was stored as P(UP) at quote time.)
    fn on_spot_tick(&self, ctx: &QuoterContext<'_>) -> Vec<QuoteIntent> {
        let mut out: Vec<QuoteIntent> = Vec::new();
        let Some(theo_up_now) = ctx.theo_p_up else {
            return out; // no theo — can't make AS decisions
        };
        let m = ctx.market;

        // UP side: bid in trouble if theo_up dropped.
        if let Some(last_theo_up) = m.quoting_up.last_theo {
            if theo_up_now < last_theo_up - self.cfg.theo_as_threshold {
                for (oid, _o) in m.quoting_up.open_orders.iter() {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side: BinarySide::Up,
                        order_id: oid.clone(),
                        reason: "paam_as_theo_drop_up",
                    });
                }
            }
        }
        // DOWN side: bid in trouble if theo_down dropped, i.e. theo_up rose.
        if let Some(last_theo_up_when_down_placed) = m.quoting_down.last_theo {
            if theo_up_now > last_theo_up_when_down_placed + self.cfg.theo_as_threshold {
                for (oid, _o) in m.quoting_down.open_orders.iter() {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side: BinarySide::Down,
                        order_id: oid.clone(),
                        reason: "paam_as_theo_drop_down",
                    });
                }
            }
        }
        out
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::MarketState;
    use crate::types::Symbol;

    fn empty_market() -> MarketState {
        // Market_start_ts set to "now" so discovery_delay is ~0 (a
        // freshly-launched market we caught at start). Tests for
        // mid_window_join construct a separate stale-discovery market.
        let now_unix = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        MarketState::new(
            "btc-updown-5m-1700000000".into(),
            Symbol::BTC,
            "5m".into(),
            300,
            now_unix,
            "111".into(),
            "222".into(),
            0.0,
        )
    }

    /// Market that the bot discovered well after its start (mid-window join).
    fn stale_market() -> MarketState {
        let now_unix = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_secs() as i64)
            .unwrap_or(0);
        // Market started 120s ago — well past our 30s max_discovery_delay.
        MarketState::new(
            "btc-updown-5m-stale".into(),
            Symbol::BTC,
            "5m".into(),
            300,
            now_unix - 120,
            "111".into(),
            "222".into(),
            0.0,
        )
    }

    fn ctx<'a>(
        m: &'a MarketState,
        up: (f64, f64),
        down: (f64, f64),
        elapsed: f64,
        tte: f64,
    ) -> QuoterContext<'a> {
        QuoterContext {
            market: m,
            up_book: up,
            down_book: down,
            theo_p_up: None,
            spot: None,
            elapsed_s: elapsed,
            tte_s: tte,
        }
    }

    #[test]
    fn warmup_returns_hold() {
        let q = PaamQuoter::new();
        let m = empty_market();
        let intents = q.decide(&ctx(&m, (0.49, 0.51), (0.49, 0.51), 5.0, 295.0));
        assert!(matches!(intents[0], QuoteIntent::Hold { .. }));
    }

    #[test]
    fn ten_seconds_to_settle_cancels_no_places() {
        let q = PaamQuoter::new();
        let m = empty_market();
        let intents = q.decide(&ctx(&m, (0.49, 0.55), (0.45, 0.51), 297.0, 3.0));
        // No Places at all (window closed).
        assert!(!intents.iter().any(|i| matches!(i, QuoteIntent::Place { .. })));
    }

    #[test]
    fn book_with_edge_emits_both_sides() {
        let q = PaamQuoter::new();
        let m = empty_market();
        // Bids sum to 0.93 — well under max_total_bid (0.95). Asks NOT
        // placeholders. Should place both sides at bid+1c each.
        let intents = q.decide(&ctx(&m, (0.48, 0.55), (0.45, 0.52), 100.0, 200.0));
        let places: Vec<_> = intents.iter().filter(|i| matches!(i, QuoteIntent::Place { .. })).collect();
        assert_eq!(places.len(), 2, "expected one UP and one DOWN place");
        for p in &places {
            if let QuoteIntent::Place { price, post_only, side, .. } = p {
                assert!(*post_only);
                if *side == BinarySide::Up { assert_eq!(*price, 0.49); }
                if *side == BinarySide::Down { assert_eq!(*price, 0.46); }
            }
        }
    }

    #[test]
    fn no_edge_when_bids_sum_high_skips() {
        let q = PaamQuoter::new();
        let m = empty_market();
        // 0.50 + 0.48 = 0.98 > max_total_bid (0.97). Skip — no edge.
        let intents = q.decide(&ctx(&m, (0.50, 0.55), (0.48, 0.52), 100.0, 200.0));
        assert!(!intents.iter().any(|i| matches!(i, QuoteIntent::Place { .. })));
    }

    #[test]
    fn placeholder_asks_still_quote_if_bids_have_edge() {
        let q = PaamQuoter::new();
        let m = empty_market();
        // Both asks 0.99 (standard PM 5m placeholder behavior) but bids
        // sum to 0.90 — clear edge. v1 should quote anyway since postOnly
        // protects us at the wire level.
        let intents = q.decide(&ctx(&m, (0.30, 0.99), (0.60, 0.99), 100.0, 200.0));
        let places: Vec<_> = intents.iter().filter(|i| matches!(i, QuoteIntent::Place { .. })).collect();
        assert_eq!(places.len(), 2, "placeholder asks should NOT block quoting when bids leave edge");
    }

    #[test]
    fn mid_window_join_suppresses_placements() {
        let q = PaamQuoter::new();
        let m = stale_market();
        // Even though book has edge (sum 0.93), we joined late, so no Place.
        let intents = q.decide(&ctx(&m, (0.48, 0.55), (0.45, 0.52), 100.0, 200.0));
        let places: Vec<_> = intents.iter().filter(|i| matches!(i, QuoteIntent::Place { .. })).collect();
        assert_eq!(places.len(), 0, "mid-window join must suppress placements");
        // But emits Holds documenting why.
        let holds: Vec<_> = intents.iter()
            .filter_map(|i| if let QuoteIntent::Hold { reason, .. } = i { Some(*reason) } else { None })
            .collect();
        assert!(holds.iter().any(|r| *r == "paam_mid_window_join_skip"),
                "mid-window-join skip reason expected; got {:?}", holds);
    }

    #[test]
    fn open_buy_orders_count_toward_inventory_cap() {
        use crate::state::LiveOrder;
        use crate::types::OrderSide;

        let q = PaamQuoter::new();
        let mut m = empty_market();
        // Simulate that we already have 5 tokens worth of open BUY orders on UP.
        m.quoting_up.open_orders.insert(
            "0xfake".to_string(),
            LiveOrder {
                order_id: "0xfake".into(),
                slug: m.slug.clone(),
                side_token: BinarySide::Up,
                order_side: OrderSide::Buy,
                price: 0.49,
                size: 5.0,
                placed_ts_ns: 0,
                filled_size: 0.0,
                status: "live".into(),
            },
        );
        let intents = q.decide(&ctx(&m, (0.48, 0.55), (0.45, 0.52), 100.0, 200.0));
        // UP side should NOT emit a fresh Place — open buy at 5 tokens caps us.
        let up_places: Vec<_> = intents.iter().filter(|i| match i {
            QuoteIntent::Place { side, .. } => *side == BinarySide::Up,
            _ => false,
        }).collect();
        assert_eq!(up_places.len(), 0,
                   "open BUY orders must count toward inventory cap (got {} UP places)",
                   up_places.len());
    }

    #[test]
    fn chase_detector_suppresses_runaway_side() {
        // Hand-craft a book history: down_bid moved from 0.30 → 0.45 over time,
        // which exceeds chase_drift_threshold (0.05). down_chase must trip and
        // suppress new DOWN placements.
        let q = PaamQuoter::new();
        let m = empty_market();
        let slug = m.slug.clone();
        let now_ns = crate::types::now_mono_ns();
        // Seed history with two prior snaps inside chase window.
        q.book_history.insert(
            slug.clone(),
            vec![
                BookSnap {
                    up_mid: 0.50, down_mid: 0.32,
                    up_bid: 0.49, down_bid: 0.30,
                    ts_ns: now_ns.saturating_sub(15_000_000_000),  // 15s ago
                },
                BookSnap {
                    up_mid: 0.45, down_mid: 0.41,
                    up_bid: 0.44, down_bid: 0.40,
                    ts_ns: now_ns.saturating_sub(5_000_000_000),  // 5s ago
                },
            ],
        );
        // Current: down_bid jumped to 0.45 — that's 0.15 above the 0.30 min in
        // window → triggers chase.
        let intents = q.decide(&ctx(&m, (0.40, 0.55), (0.45, 0.55), 100.0, 200.0));
        let down_places: Vec<_> = intents.iter().filter(|i| match i {
            QuoteIntent::Place { side, .. } => *side == BinarySide::Down,
            _ => false,
        }).collect();
        assert_eq!(down_places.len(), 0, "chase detector must suppress DOWN placement");
        let chase_holds: Vec<_> = intents.iter().filter_map(|i|
            if let QuoteIntent::Hold { reason, .. } = i {
                if *reason == "paam_chase_skip_entry" { Some(()) } else { None }
            } else { None }
        ).collect();
        assert!(!chase_holds.is_empty(), "expected paam_chase_skip_entry hold reason");
    }

    #[test]
    fn extreme_bid_on_one_side_skips_that_side_only() {
        let q = PaamQuoter::new();
        let m = empty_market();
        // UP bid 0.05 below min_bid; DOWN bid 0.40 OK. Sum 0.45 < gate.
        // Asks 0.55 / 0.55 — not placeholder.
        let intents = q.decide(&ctx(&m, (0.05, 0.55), (0.40, 0.55), 100.0, 200.0));
        let places: Vec<_> = intents.iter().filter(|i| matches!(i, QuoteIntent::Place { .. })).collect();
        let has_up = places.iter().any(|i| matches!(i, QuoteIntent::Place { side, .. } if *side == BinarySide::Up));
        let has_down = places.iter().any(|i| matches!(i, QuoteIntent::Place { side, .. } if *side == BinarySide::Down));
        assert!(!has_up, "UP must skip (bid below min_bid)");
        assert!(has_down, "DOWN should place");
    }
}
