//! Post-fill markout tracker.
//!
//! For every fill the strategy gets, schedule reads of the PM token mid at
//! several future horizons (5s, 30s) and emit `markout_<h>` audit events
//! carrying `(fill_price, mid_at_horizon, markout_cents, signed_markout_cents)`.
//!
//! Signed markout convention (BUY-side only — we are a maker who only buys):
//!   `signed_markout = mid_at_horizon - fill_price`
//! Positive = good for us (mid moved ABOVE the price we paid). Negative =
//! adverse selection.
//!
//! Output is purely telemetry — no state mutation, no kill-switch logic
//! here. The Quoter (or a future AS circuit breaker) reads the audit
//! distribution to decide whether to halt placements.
//!
//! Implementation: one tokio task per fill. Sleeps `tokio::time::sleep`,
//! then reads `market_ws.lock().await.latest_book(asset_id)`. Light: at our
//! current fill rate this is well under 1 task/s.

use std::sync::Arc;
use std::time::Duration;

use serde_json::json;
use tokio::sync::Mutex;
use tracing::debug;

use crate::clob::MarketWs;
use crate::persist::AuditLogger;

/// Horizons at which we measure mid-vs-fill markout.
const HORIZONS_S: &[(u64, &str)] = &[
    (5, "markout_5s"),
    (30, "markout_30s"),
];

/// Snapshot of a fill that needs markout measurement.
#[derive(Debug, Clone)]
pub struct FillSnapshot {
    pub slug: String,
    /// Binary side as audit-string ("up" or "down").
    pub side: String,
    pub asset_id: String,
    pub fill_price: f64,
    pub fill_size: f64,
    /// Local monotonic time of fill, ns.
    pub fill_ts_ns: u64,
}

/// Spawn a background task that emits markout audit events at the configured
/// horizons. Returns immediately.
pub fn schedule_markout(
    fill: FillSnapshot,
    market_ws: Arc<Mutex<MarketWs>>,
    audit: AuditLogger,
) {
    tokio::spawn(async move {
        let mut elapsed_s: u64 = 0;
        for (horizon, event_name) in HORIZONS_S {
            let wait = horizon.saturating_sub(elapsed_s);
            if wait > 0 {
                tokio::time::sleep(Duration::from_secs(wait)).await;
            }
            elapsed_s = *horizon;
            let mid = {
                let mws = market_ws.lock().await;
                mws.latest_book(&fill.asset_id).and_then(|b| b.mid())
            };
            match mid {
                Some(m) => {
                    let signed = m - fill.fill_price; // BUY-only convention
                    audit.write(
                        event_name,
                        json!({
                            "slug": fill.slug,
                            "side": fill.side,
                            "asset_id": fill.asset_id,
                            "fill_price": fill.fill_price,
                            "fill_size": fill.fill_size,
                            "fill_ts_ns": fill.fill_ts_ns,
                            "horizon_s": horizon,
                            "mid_at_horizon": m,
                            "markout_cents": (signed * 100.0).round() / 100.0,
                            "signed_markout_cents": (signed * 100.0).round() / 100.0,
                        }),
                    );
                }
                None => {
                    debug!(slug = %fill.slug, horizon, "markout: no book mid available");
                    audit.write(
                        event_name,
                        json!({
                            "slug": fill.slug,
                            "side": fill.side,
                            "asset_id": fill.asset_id,
                            "fill_price": fill.fill_price,
                            "horizon_s": horizon,
                            "mid_at_horizon": serde_json::Value::Null,
                            "skipped_reason": "no_book_mid",
                        }),
                    );
                }
            }
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn fill_snapshot_round_trip() {
        let f = FillSnapshot {
            slug: "btc-updown-5m-1700000000".into(),
            side: "up".into(),
            asset_id: "1234".into(),
            fill_price: 0.42,
            fill_size: 5.0,
            fill_ts_ns: 1_700_000_000_000_000_000,
        };
        // No-op assertion — confirms struct is constructible & Clone.
        let f2 = f.clone();
        assert_eq!(f2.fill_price, 0.42);
    }
}
