//! Quoter trait + V7Quoter port of `ohan_mm_v7_0.py`.
//!
//! A `Quoter` looks at the current book + spot state and returns a list of
//! `QuoteIntent`s — places, cancels, or holds. The runtime translates intents
//! to CLOB calls.
//!
//! Strategy-agnostic by design: same trait will host a future SpreadQuoter
//! (article two-sided spread collection) implementation.

use serde::{Deserialize, Serialize};

use crate::state::MarketState;
use crate::types::{BinarySide, OrderType};

/// One decision the quoter wants the runtime to execute.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub enum QuoteIntent {
    /// Place a fresh BUY at this price for this size.
    Place {
        slug: String,
        side: BinarySide,
        token_id: String,
        price: f64,
        size: f64,
        order_type: OrderType,
        /// If true, PM rejects rather than matches a marketable order.
        /// Maker strategies should set this; takers should not.
        post_only: bool,
        reason: &'static str,
    },
    /// Cancel an existing order by ID.
    Cancel {
        slug: String,
        side: BinarySide,
        order_id: String,
        reason: &'static str,
    },
    /// No-op: explicitly hold (used by `decide` to express "intentionally idle").
    Hold {
        slug: String,
        reason: &'static str,
    },
}

/// What the runtime gives the Quoter on each tick.
pub struct QuoterContext<'a> {
    pub market: &'a MarketState,
    /// Current best bid/ask on UP token.
    pub up_book: (f64, f64),
    /// Current best bid/ask on DOWN token.
    pub down_book: (f64, f64),
    /// Theo P(Up) from `strategy::theo`, if available.
    pub theo_p_up: Option<f64>,
    /// Spot price source for this coin (for telemetry).
    pub spot: Option<f64>,
    /// Seconds elapsed since market_start_ts.
    pub elapsed_s: f64,
    /// Seconds remaining until market settlement.
    pub tte_s: f64,
}

/// Common trait for all quoting strategies.
pub trait Quoter: Send + Sync {
    fn name(&self) -> &'static str;
    fn decide(&self, ctx: &QuoterContext<'_>) -> Vec<QuoteIntent>;

    /// Fast-path called on every significant spot-price tick (Pyth-driven).
    /// Returns Cancel intents only — placements stay on the 1 Hz tick path.
    /// Default impl: no-op (V7 doesn't use spot-tick AS protection).
    fn on_spot_tick(&self, _ctx: &QuoterContext<'_>) -> Vec<QuoteIntent> {
        Vec::new()
    }
}

/// V7.0 favorite-bias + hold-to-settle quoter.
///
/// Port of the active strategy in `scripts/pricing_model/ohan_mm_v7_0.py`.
/// Mechanism per memory `project_phase0_pyth_builder_wired_may14.md`:
///
/// 1. **Skip the trap zone**: refuse to quote when token_mid puts the bid
///    below MIN_BID_PRICE (default 0.60 — the empirically losing band).
/// 2. **Single bid at mid - offset**: passive 3c below mid.
/// 3. **Hold to settle**: no SELL orders. Round-trips are accidental, not
///    strategic.
/// 4. **No naked quoting**: caller (runtime) gates on theo-warmup + vol +
///    market-elapsed before calling decide().
pub struct V7Quoter {
    pub min_bid_price: f64,
    pub max_bid_price: f64,
    pub bid_offset: f64,
    pub quote_size: f64,
    pub updown_cost_halt: f64,
}

impl V7Quoter {
    pub fn new() -> Self {
        Self {
            min_bid_price: 0.60,
            max_bid_price: 0.95,
            bid_offset: 0.03,
            quote_size: 5.0,
            updown_cost_halt: 0.94,
        }
    }
}

impl Default for V7Quoter {
    fn default() -> Self {
        Self::new()
    }
}

impl Quoter for V7Quoter {
    fn name(&self) -> &'static str {
        "v7"
    }

    fn decide(&self, ctx: &QuoterContext<'_>) -> Vec<QuoteIntent> {
        let mut out: Vec<QuoteIntent> = Vec::new();
        let m = ctx.market;

        if m.halted {
            out.push(QuoteIntent::Hold {
                slug: m.slug.clone(),
                reason: "market_halted",
            });
            return out;
        }

        // UP+DOWN combined-cost halt: if we already hold pairs at >0.94 cost,
        // stop accumulating (negative-EV after fees).
        let bid_halt = match m.cost_per_pair() {
            Some(c) if c > self.updown_cost_halt => true,
            _ => false,
        };

        // Per-side analysis.
        for (side, (bid_s, ask_s), token_id, qs) in [
            (
                BinarySide::Up,
                ctx.up_book,
                m.up_token_id.clone(),
                &m.quoting_up,
            ),
            (
                BinarySide::Down,
                ctx.down_book,
                m.down_token_id.clone(),
                &m.quoting_down,
            ),
        ] {
            if bid_s <= 0.0 || ask_s <= 0.0 || ask_s <= bid_s {
                continue;
            }
            let token_mid = (bid_s + ask_s) / 2.0;

            // Compute target bid at mid - offset.
            let target_bid = (token_mid - self.bid_offset).round_to_cent();

            // Skip when bid would land below MIN_BID_PRICE (trap zone).
            if target_bid < self.min_bid_price {
                // Cancel any existing BUYs on this side — we're outside our band.
                for (oid, o) in qs.open_orders.iter() {
                    if matches!(o.order_side, crate::types::OrderSide::Buy) {
                        out.push(QuoteIntent::Cancel {
                            slug: m.slug.clone(),
                            side,
                            order_id: oid.clone(),
                            reason: "outside_band",
                        });
                    }
                }
                continue;
            }

            if target_bid > self.max_bid_price {
                continue; // overbought; no upside
            }
            if target_bid >= ask_s {
                continue; // would cross the spread
            }

            if bid_halt {
                // Cancel any open BUYs — we don't want to add more inventory.
                for (oid, o) in qs.open_orders.iter() {
                    if matches!(o.order_side, crate::types::OrderSide::Buy) {
                        out.push(QuoteIntent::Cancel {
                            slug: m.slug.clone(),
                            side,
                            order_id: oid.clone(),
                            reason: "updown_cost_halt",
                        });
                    }
                }
                continue;
            }

            // One-bid-per-side rule (v7.0): if we already have a BUY at this
            // exact price, no-op; otherwise cancel the old and place new.
            let mut has_at_target = false;
            for (oid, o) in qs.open_orders.iter() {
                if !matches!(o.order_side, crate::types::OrderSide::Buy) {
                    continue;
                }
                if (o.price - target_bid).abs() < 1e-9 {
                    has_at_target = true;
                } else {
                    out.push(QuoteIntent::Cancel {
                        slug: m.slug.clone(),
                        side,
                        order_id: oid.clone(),
                        reason: "price_drift_replace",
                    });
                }
            }
            if !has_at_target {
                out.push(QuoteIntent::Place {
                    slug: m.slug.clone(),
                    side,
                    token_id,
                    price: target_bid,
                    size: self.quote_size,
                    order_type: OrderType::Gtc,
                    // V7 was designed pre-postOnly era — its bids aim at
                    // `mid - 3c` which is always below the ask, so postOnly
                    // is a safety net rather than a behavior change.
                    post_only: true,
                    reason: "v7_passive_bid",
                });
            }
        }
        out
    }
}

trait RoundToCent {
    fn round_to_cent(self) -> f64;
}

impl RoundToCent for f64 {
    fn round_to_cent(self) -> f64 {
        (self * 100.0).round() / 100.0
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::state::MarketState;
    use crate::types::Symbol;

    fn mkt() -> MarketState {
        MarketState::new(
            "btc-updown-5m-1".into(),
            Symbol::BTC,
            "5m".into(),
            300,
            1_700_000_000,
            "tok-up".into(),
            "tok-down".into(),
            79_000.0,
        )
    }

    fn ctx<'a>(
        m: &'a MarketState,
        up: (f64, f64),
        down: (f64, f64),
    ) -> QuoterContext<'a> {
        QuoterContext {
            market: m,
            up_book: up,
            down_book: down,
            theo_p_up: None,
            spot: None,
            elapsed_s: 60.0,
            tte_s: 240.0,
        }
    }

    #[test]
    fn v7_places_passive_bid_when_one_side_is_favorite() {
        let m = mkt();
        let q = V7Quoter::default();
        // UP is favorite at 0.70, DOWN at 0.30. Bid for UP at 0.67 (>0.60 ok).
        // Bid for DOWN at 0.27 (<0.60 trap → no place, no cancel needed).
        let intents = q.decide(&ctx(&m, (0.69, 0.71), (0.29, 0.31)));
        let up_places: Vec<_> = intents
            .iter()
            .filter_map(|i| match i {
                QuoteIntent::Place { side, price, .. } if *side == BinarySide::Up => {
                    Some(*price)
                }
                _ => None,
            })
            .collect();
        let down_places: Vec<_> = intents
            .iter()
            .filter_map(|i| match i {
                QuoteIntent::Place { side, price, .. } if *side == BinarySide::Down => {
                    Some(*price)
                }
                _ => None,
            })
            .collect();
        assert_eq!(up_places, vec![0.67]);
        assert!(down_places.is_empty(), "DOWN side mid 0.30 is trap, no place");
    }

    #[test]
    fn v7_holds_when_market_halted() {
        let mut m = mkt();
        m.halted = true;
        let q = V7Quoter::default();
        let intents = q.decide(&ctx(&m, (0.69, 0.71), (0.29, 0.31)));
        assert!(matches!(intents[0], QuoteIntent::Hold { .. }));
    }

    #[test]
    fn v7_skips_both_sides_in_balanced_market() {
        let m = mkt();
        let q = V7Quoter::default();
        // BTC at strike: both UP and DOWN ≈ 0.50. Both bids at 0.47 → below trap.
        let intents = q.decide(&ctx(&m, (0.495, 0.505), (0.495, 0.505)));
        let places: Vec<_> = intents
            .iter()
            .filter(|i| matches!(i, QuoteIntent::Place { .. }))
            .collect();
        assert!(places.is_empty(), "balanced market should produce no places");
    }
}
