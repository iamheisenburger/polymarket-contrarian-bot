//! Core runtime — wires SpotPulse + GammaClient + MarketWs + UserWs +
//! Quoter + Risk + ClobClient into a single event loop.
//!
//! Two modes:
//!   - **Shadow:** subscribe to feeds, run Quoter, log intents to JSONL, but
//!     do NOT submit to CLOB.
//!   - **Live:** intents go through `ClobClient.post_order` / cancel.
//!
//! The loop runs at 1Hz "decide" tick + on every market WS book event.
//! For each active market: pull current book, build `QuoterContext`, call
//! `Quoter::decide`, then `execute_intents`. State updates from User WS
//! happen on a separate task and write through to `GlobalState` via
//! `update()`.

pub mod latency;

use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use anyhow::Result;
use serde_json::json;
use tokio::sync::mpsc;
use tracing::{debug, info, warn};

use alloy_primitives::{Address, U256};
use crate::clob::{ClobClient, MarketEvent, MarketWs, UserEvent, UserWs};
use crate::feeds::SpotPulse;
use crate::gamma::{GammaClient, TimeFrame};
use crate::persist::AuditLogger;
use crate::risk::{check_place, RiskConfig, RiskDecision, RiskState};
use crate::signer::OrderSigner;
use crate::state::{GlobalState, LiveOrder, MarketState, Wei6};
use crate::strategy::{QuoteIntent, Quoter, QuoterContext, TheoV6Params};
use crate::types::{BinarySide, Order, OrderSide, OrderType, Symbol};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Shadow,
    Live,
}

pub struct Runtime {
    pub mode: Mode,
    pub coins: Vec<Symbol>,
    pub timeframes: Vec<TimeFrame>,
    pub risk: Arc<RiskConfig>,
    pub state: Arc<GlobalState>,
    pub risk_state: Arc<RiskState>,
    pub audit: AuditLogger,
    pub quoter: Arc<dyn Quoter>,
    pub spot: Arc<SpotPulse>,
    pub gamma: Arc<GammaClient>,
    pub market_ws: Arc<tokio::sync::Mutex<MarketWs>>,
    pub user_ws: Option<Arc<tokio::sync::Mutex<UserWs>>>,
    pub clob: Option<Arc<ClobClient>>,
    pub theo_params: Option<Arc<TheoV6Params>>,
    pub signer: Option<Arc<OrderSigner>>,
    pub funder: Option<Address>,
    /// L2 API key UUID. Used as the `owner` field in POST /order bodies.
    pub l2_api_key: Option<String>,
}

impl Runtime {
    /// Live order placement path: build Order, sign with EIP-712, POST.
    async fn place_live(
        &self,
        slug: &str,
        side: BinarySide,
        token_id: &str,
        price: f64,
        size: f64,
        order_type: OrderType,
        post_only: bool,
        theo_p_up_at_place: Option<f64>,
    ) -> Result<()> {
        let signer = self
            .signer
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("no signer (Live requires signer)"))?;
        let funder = self
            .funder
            .ok_or_else(|| anyhow::anyhow!("no funder (Live requires funder)"))?;
        let clob = self
            .clob
            .as_ref()
            .ok_or_else(|| anyhow::anyhow!("no clob client (Live requires clob)"))?;

        // Pre-place risk gate. Use a coarse estimate of free_usdc + committed
        // for now — accurate accounting is Phase 4 polish.
        let now_ns = crate::types::now_mono_ns();
        let decision = check_place(
            &self.risk,
            &self.risk_state,
            price,
            size,
            0.0, // cur_delta_shares — TODO read from state
            0.0, // open_buy_cost
            1000.0, // free_usdc fallback
            0.0, // committed
            60.0, // market_elapsed
            now_ns,
        );
        if let RiskDecision::Reject(reason, msg) = decision {
            self.audit.write(
                "place_rejected_risk",
                json!({"slug": slug, "reason": reason, "msg": msg, "price": price, "size": size}),
            );
            return Ok(());
        }

        let api_key = self
            .l2_api_key
            .as_deref()
            .ok_or_else(|| anyhow::anyhow!("no L2 api_key (Live requires l2_api_key)"))?;

        // Build EIP-712 Order struct.
        let token_id_u256 = U256::from_str_radix(token_id, 10)
            .map_err(|e| anyhow::anyhow!("parse token_id: {e}"))?;
        let maker_amount = Wei6::from_usd(price * size).0;
        let taker_amount = Wei6::from_token(size).0;
        // v2 Order: random salt, ms timestamp, zero metadata/builder.
        let salt_u64: u64 = (rand::random::<u32>() as u64) & 0x3fff_ffff;
        let timestamp_ms = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_millis() as u64;
        let order = Order {
            salt: U256::from(salt_u64),
            maker: funder,
            signer: signer.address(),
            token_id: token_id_u256,
            maker_amount,
            taker_amount,
            side: OrderSide::Buy.as_u8(),
            signature_type: 2,
            timestamp: U256::from(timestamp_ms),
            metadata: alloy_primitives::B256::ZERO,
            builder: alloy_primitives::B256::ZERO,
            expiration: U256::ZERO,
        };
        let signed = {
            let _g = crate::latency_span!(crate::runtime::latency::Span::OrderSign);
            signer.sign_order(&order, api_key)?
        };

        // Pre-POST audit so we can compute place_acked / place_attempted ratio.
        self.audit.write(
            "place_attempted",
            json!({
                "slug": slug,
                "side": side.as_str(),
                "price": price,
                "size": size,
                "order_type": order_type.as_str(),
                "salt": salt_u64,
            }),
        );

        // POST.
        let _g = crate::latency_span!(crate::runtime::latency::Span::ClobPost);
        let t0 = std::time::Instant::now();
        match clob.post_order(&signed, order_type, post_only).await {
            Ok(resp) => {
                let elapsed_us = t0.elapsed().as_micros() as u64;
                let acked = resp.success && !resp.order_id.is_empty();
                self.audit.write(
                    "place_result",
                    json!({
                        "slug": slug,
                        "side": side.as_str(),
                        "price": price,
                        "size": size,
                        "order_id": resp.order_id,
                        "status": resp.status,
                        "elapsed_us": elapsed_us,
                        "acked": acked,
                    }),
                );
                if acked {
                    self.audit.write(
                        "place_acked",
                        json!({
                            "slug": slug,
                            "side": side.as_str(),
                            "order_id": resp.order_id,
                            "elapsed_us": elapsed_us,
                        }),
                    );
                    self.state.n_placed.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
                    // Track in MarketState
                    let oid = resp.order_id.clone();
                    let _ = self.state.update(slug, |m| {
                        let mut new = m.clone();
                        let qs = new.quoting_mut(side);
                        qs.open_orders.insert(
                            oid.clone(),
                            LiveOrder {
                                order_id: oid.clone(),
                                slug: slug.to_string(),
                                side_token: side,
                                order_side: OrderSide::Buy,
                                price,
                                size,
                                placed_ts_ns: crate::types::now_mono_ns(),
                                filled_size: 0.0,
                                status: "live".into(),
                            },
                        );
                        // Capture theo_p_up at place-time as the AS baseline.
                        // PaamQuoter::on_spot_tick compares this to current
                        // theo_p_up to detect adverse moves.
                        if let Some(t) = theo_p_up_at_place {
                            qs.last_theo = Some(t);
                            qs.last_quote_ts_ns = crate::types::now_mono_ns();
                        }
                        new
                    });
                    self.risk_state.reset_place_fails();
                } else {
                    self.risk_state.record_place_fail(&self.risk, now_ns);
                }
            }
            Err(e) => {
                let elapsed_us = t0.elapsed().as_micros() as u64;
                self.audit.write(
                    "place_failed",
                    json!({
                        "slug": slug,
                        "side": side.as_str(),
                        "price": price,
                        "size": size,
                        "error": format!("{:?}", e),
                        "elapsed_us": elapsed_us,
                    }),
                );
                self.risk_state.record_place_fail(&self.risk, now_ns);
            }
        }
        Ok(())
    }

    /// Convert intents into either logged-only (Shadow) or real CLOB calls (Live).
    /// `theo_p_up_now` is captured by the caller (the run loop) and forwarded so
    /// place_live can stamp `last_theo` for AS baseline. None = no theo
    /// available — AS protection won't fire on this side.
    pub async fn execute_intents(
        &self,
        intents: Vec<QuoteIntent>,
        theo_p_up_now: Option<f64>,
    ) -> Result<()> {
        for intent in intents {
            match intent {
                QuoteIntent::Hold { slug, reason } => {
                    self.audit.write(
                        "intent_hold",
                        json!({"slug": slug, "reason": reason}),
                    );
                }
                QuoteIntent::Place {
                    slug,
                    side,
                    token_id,
                    price,
                    size,
                    order_type,
                    post_only,
                    reason,
                } => {
                    self.audit.write(
                        "intent_place",
                        json!({
                            "slug": slug,
                            "side": side.as_str(),
                            "token_id": token_id,
                            "price": price,
                            "size": size,
                            "order_type": order_type.as_str(),
                            "post_only": post_only,
                            "reason": reason,
                            "theo_p_up_at_place": theo_p_up_now,
                            "mode": match self.mode {
                                Mode::Shadow => "shadow",
                                Mode::Live => "live",
                            },
                        }),
                    );
                    if self.mode == Mode::Live {
                        if let Err(e) = self
                            .place_live(&slug, side, &token_id, price, size, order_type, post_only, theo_p_up_now)
                            .await
                        {
                            warn!(slug, side = side.as_str(), price, error = %e, "place_live failed");
                        }
                    }
                }
                QuoteIntent::Cancel {
                    slug,
                    side,
                    order_id,
                    reason,
                } => {
                    self.audit.write(
                        "intent_cancel",
                        json!({
                            "slug": slug,
                            "side": side.as_str(),
                            "order_id": order_id,
                            "reason": reason,
                        }),
                    );
                    if self.mode == Mode::Live {
                        if let Some(clob) = self.clob.as_ref() {
                            // TSC-instrument the cancel round-trip; Phase 1
                            // measures this distribution to gate PAAM viability.
                            let _g = crate::latency_span!(crate::runtime::latency::Span::ClobCancel);
                            let t0 = std::time::Instant::now();
                            let res = clob.cancel_orders(&[order_id.clone()]).await;
                            let elapsed_us = t0.elapsed().as_micros() as u64;
                            match res {
                                Ok(r) => {
                                    self.audit.write(
                                        "cancel_result",
                                        json!({
                                            "order_id": order_id,
                                            "n_cancelled": r.canceled.len(),
                                            "n_not_cancelled": r.not_canceled.len(),
                                            "elapsed_us": elapsed_us,
                                        }),
                                    );
                                    self.state
                                        .n_cancelled
                                        .fetch_add(r.canceled.len() as u64, std::sync::atomic::Ordering::Relaxed);
                                }
                                Err(e) => {
                                    warn!(error = ?e, order_id, "cancel failed");
                                }
                            }
                        }
                    }
                    // Remove from in-process state regardless of mode (Shadow
                    // tracks intent, Live tracks reality).
                    let _ = self.state.update(&slug, |m| {
                        let mut new = m.clone();
                        match side {
                            BinarySide::Up => {
                                new.quoting_up.open_orders.remove(&order_id);
                            }
                            BinarySide::Down => {
                                new.quoting_down.open_orders.remove(&order_id);
                            }
                        }
                        new
                    });
                }
            }
        }
        Ok(())
    }

    /// Spawn the market-discovery loop: polls Gamma every 15s for fresh
    /// (coin, tf) markets and registers them in `state`. Returns the join
    /// handle so the caller can abort on shutdown.
    pub fn spawn_market_discovery(self: &Arc<Self>) -> tokio::task::JoinHandle<()> {
        let coins = self.coins.clone();
        let tfs = self.timeframes.clone();
        let state = Arc::clone(&self.state);
        let gamma = Arc::clone(&self.gamma);
        let audit = self.audit.clone();
        let market_ws = Arc::clone(&self.market_ws);
        let spot = Arc::clone(&self.spot);
        tokio::spawn(async move {
            let mut tick = tokio::time::interval(Duration::from_secs(15));
            loop {
                tick.tick().await;
                for coin in &coins {
                    for tf in &tfs {
                        match gamma.current_market(*coin, *tf).await {
                            Ok(Some(m)) => {
                                if !m.accepting_orders || m.closed {
                                    continue;
                                }
                                if state.markets.contains_key(&m.slug) {
                                    continue;
                                }
                                let (up_id, down_id) = match m.token_ids() {
                                    Ok(x) => x,
                                    Err(e) => {
                                        warn!(slug = %m.slug, error = ?e, "skip market: bad token ids");
                                        continue;
                                    }
                                };
                                let market_start_ts: i64 = m
                                    .slug
                                    .rsplit('-')
                                    .next()
                                    .and_then(|s| s.parse().ok())
                                    .unwrap_or(0);
                                // Strike for PM crypto Up/Down = spot price
                                // AT market_start_ts. Look up SpotPulse history
                                // for the entry closest to that timestamp. If
                                // history doesn't reach back that far, fall back
                                // to current spot (best-effort).
                                let strike = strike_from_history(
                                    &spot,
                                    *coin,
                                    market_start_ts as f64,
                                );
                                let market = MarketState::new(
                                    m.slug.clone(),
                                    *coin,
                                    tf.as_str().to_string(),
                                    tf.seconds(),
                                    market_start_ts,
                                    up_id,
                                    down_id,
                                    strike,
                                );
                                audit.write(
                                    "market_selected",
                                    json!({
                                        "slug": &m.slug,
                                        "coin": coin.as_str(),
                                        "tf": tf.as_str(),
                                        "up_id": &market.up_token_id,
                                        "down_id": &market.down_token_id,
                                        "strike": strike,
                                        "market_start_ts": market_start_ts,
                                    }),
                                );
                                let ids = vec![market.up_token_id.clone(), market.down_token_id.clone()];
                                state.insert(market);
                                // Tell the market WS to start receiving books for these tokens.
                                if let Err(e) = market_ws.lock().await.subscribe_more(ids) {
                                    warn!(error = %e, slug = %m.slug, "subscribe_more failed");
                                }
                            }
                            Ok(None) => {} // no market yet
                            Err(e) => {
                                debug!(coin = %coin, tf = ?tf, error = ?e, "gamma probe failed");
                            }
                        }
                    }
                }
            }
        })
    }

    /// Spawn the user-channel WS event handler: updates state on fills + order
    /// status changes.
    pub fn spawn_user_event_handler(
        self: &Arc<Self>,
        mut rx: mpsc::UnboundedReceiver<UserEvent>,
    ) -> tokio::task::JoinHandle<()> {
        let state = Arc::clone(&self.state);
        let audit = self.audit.clone();
        let market_ws = Arc::clone(&self.market_ws);
        let our_maker_addr = self
            .funder
            .map(|a| format!("{a:#x}").to_lowercase())
            .unwrap_or_default();
        tokio::spawn(async move {
            while let Some(ev) = rx.recv().await {
                match ev {
                    UserEvent::Trade(t) => {
                        audit.write(
                            "trade_event",
                            json!({
                                "status": &t.status,
                                "id": &t.id,
                                "asset_id": &t.asset_id,
                                "side": &t.side,
                                "outcome": &t.outcome,
                                "price": &t.price,
                                "size": &t.size,
                                "tx": &t.transaction_hash,
                            }),
                        );
                        // Only CONFIRMED is ground truth (CLAUDE.md rule).
                        if t.status != "CONFIRMED" {
                            continue;
                        }
                        // PM TradeEvent: top-level fields describe the TAKER's
                        // leg (their asset_id, side, full trade size). When WE
                        // are the maker (postOnly), our actual leg lives in
                        // t.maker_orders[i] where maker == our safe address.
                        // Find OUR leg(s); fall back to taker-side fields only
                        // when we were the taker (no maker_orders match).
                        let our_legs: Vec<(String, f64, f64)> = t.maker_orders
                            .iter()
                            .filter(|mo| !our_maker_addr.is_empty()
                                && mo.maker.to_lowercase() == our_maker_addr)
                            .filter_map(|mo| {
                                let p: f64 = mo.price.parse().ok()?;
                                let s: f64 = mo.matched_amount.parse().ok()?;
                                if p > 0.0 && s > 0.0 {
                                    Some((mo.asset_id.clone(), p, s))
                                } else {
                                    None
                                }
                            })
                            .collect();
                        // Per-leg accumulator. If we're a maker, each our_leg
                        // is processed; otherwise we attribute the taker leg
                        // to ourselves.
                        let legs: Vec<(String, f64, f64, OrderSide)> = if !our_legs.is_empty() {
                            // Maker side is always BUY in PAAM v1 (we only
                            // place postOnly BUYs). PM's TradeEvent.side
                            // reports the TAKER's action, which is the
                            // opposite of ours.
                            our_legs.into_iter()
                                .map(|(a, p, s)| (a, p, s, OrderSide::Buy))
                                .collect()
                        } else {
                            let order_side = match t.side.as_str() {
                                "BUY" => OrderSide::Buy,
                                "SELL" => OrderSide::Sell,
                                _ => continue,
                            };
                            let p: f64 = t.price.parse().unwrap_or(0.0);
                            let s: f64 = t.size.parse().unwrap_or(0.0);
                            if p <= 0.0 || s <= 0.0 {
                                continue;
                            }
                            vec![(t.asset_id.clone(), p, s, order_side)]
                        };
                        for (asset_id, price, size, order_side) in legs {
                        let (slug, bin_side) = match state.token_to_slug.get(&asset_id) {
                            Some(e) => e.value().clone(),
                            None => continue,
                        };
                        state.update(&slug, |m| {
                            let mut new = m.clone();
                            let qs = new.quoting_mut(bin_side);
                            match order_side {
                                OrderSide::Buy => {
                                    qs.delta_shares += size;
                                    qs.cash_flow -= price * size;
                                    qs.avg_cost = Some(match qs.avg_cost {
                                        None => price,
                                        Some(prev) => {
                                            let prior_shares = qs.delta_shares - size;
                                            if prior_shares > 0.0 {
                                                (prev * prior_shares + price * size) / qs.delta_shares
                                            } else {
                                                price
                                            }
                                        }
                                    });
                                }
                                OrderSide::Sell => {
                                    qs.delta_shares -= size;
                                    qs.cash_flow += price * size;
                                    if qs.delta_shares <= 0.0 {
                                        qs.avg_cost = None;
                                    }
                                }
                            }
                            new
                        });
                        audit.write(
                            "fill_applied",
                            json!({
                                "slug": slug,
                                "side": bin_side.as_str(),
                                "order_side": order_side.as_str(),
                                "price": price,
                                "size": size,
                            }),
                        );
                        // Schedule markout measurement at 5s + 30s horizons.
                        // BUY-only: signed markout = mid - fill_price. Positive
                        // = good for us, negative = adverse selection.
                        if matches!(order_side, OrderSide::Buy) {
                            crate::strategy::schedule_markout(
                                crate::strategy::FillSnapshot {
                                    slug: slug.clone(),
                                    side: bin_side.as_str().to_string(),
                                        asset_id: asset_id.clone(),
                                    fill_price: price,
                                    fill_size: size,
                                    fill_ts_ns: crate::types::now_mono_ns(),
                                },
                                Arc::clone(&market_ws),
                                audit.clone(),
                            );
                        }
                        } // end for legs
                    }
                    UserEvent::Order(o) => {
                        audit.write(
                            "order_event",
                            json!({
                                "type": &o.kind,
                                "id": &o.id,
                                "asset_id": &o.asset_id,
                                "side": &o.side,
                                "price": &o.price,
                                "size": &o.size,
                                "size_matched": &o.size_matched,
                            }),
                        );
                    }
                }
            }
        })
    }

    /// Fast-path AS-cancel: invoked on every significant spot tick.
    /// Iterates markets for the moving coin, recomputes theo at this exact
    /// moment, builds a QuoterContext, and calls `quoter.on_spot_tick`.
    /// Executes any resulting cancel intents on the wire (no places here).
    async fn run_as_cancel_path(self: &Arc<Self>, coin: Symbol, spot: f64, bps_move: f64) {
        let now_unix_s = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_secs_f64())
            .unwrap_or(0.0);
        let now_mono_ns = crate::types::now_mono_ns();

        // Snapshot markets for this coin only.
        let snapshot: Vec<(String, Arc<MarketState>)> = self
            .state
            .markets
            .iter()
            .filter(|e| e.value().load_full().coin == coin)
            .map(|e| (e.key().clone(), e.value().load_full()))
            .collect();

        if snapshot.is_empty() {
            return;
        }

        self.audit.write(
            "as_tick_fired",
            json!({
                "coin": coin.as_str(),
                "spot": spot,
                "bps_move": bps_move,
                "n_markets": snapshot.len(),
            }),
        );

        for (slug, market_arc) in snapshot {
            let elapsed = now_unix_s - market_arc.market_start_ts as f64;
            let tte = market_arc.window_s as f64 - elapsed;
            if tte <= 0.0 || elapsed < 0.0 {
                continue;
            }

            // Need book to build ctx.
            let (up_book, down_book) = {
                let mws = self.market_ws.lock().await;
                let up = match mws.latest_book(&market_arc.up_token_id) {
                    Some(b) => match (b.best_bid(), b.best_ask()) {
                        (Some((b, _)), Some((a, _))) => (b, a),
                        _ => continue,
                    },
                    None => continue,
                };
                let down = match mws.latest_book(&market_arc.down_token_id) {
                    Some(b) => match (b.best_bid(), b.best_ask()) {
                        (Some((b, _)), Some((a, _))) => (b, a),
                        _ => continue,
                    },
                    None => continue,
                };
                (up, down)
            };

            // Strike. Backfill if missing.
            let strike = if market_arc.strike > 0.0 {
                market_arc.strike
            } else {
                strike_from_history(&self.spot, coin, market_arc.market_start_ts as f64)
            };
            if strike <= 0.0 {
                continue;
            }

            // Recompute theo at this tick.
            let theo_p_up = if let Some(params) = self.theo_params.as_ref() {
                let (series, keys) = self.spot.history_snapshot(coin);
                if series.len() >= 5 {
                    let t_now = (now_mono_ns as f64) / 1_000_000_000.0;
                    crate::strategy::theo::theo_p_up_at(
                        t_now, spot, strike, tte.max(1.0),
                        &series, &keys, params,
                    )
                } else {
                    None
                }
            } else {
                None
            };

            let ctx = QuoterContext {
                market: market_arc.as_ref(),
                up_book,
                down_book,
                theo_p_up,
                spot: Some(spot),
                elapsed_s: elapsed.max(0.0),
                tte_s: tte,
            };

            let cancels = self.quoter.on_spot_tick(&ctx);
            if !cancels.is_empty() {
                self.audit.write(
                    "as_cancels_emitted",
                    json!({
                        "slug": &slug,
                        "n_cancels": cancels.len(),
                        "coin": coin.as_str(),
                        "spot": spot,
                        "bps_move": bps_move,
                        "theo_p_up": theo_p_up,
                    }),
                );
                let _ = self.execute_intents(cancels, theo_p_up).await;
            }
        }
    }

    /// Book-driven AS cancel path. Fires on every PM market WS book or
    /// price_change event. Looks up which market (slug, side) owns this
    /// asset_id, computes the new book mid, and if any of our open orders
    /// on that side is now adversely positioned (priced too far above the
    /// current best bid), emit a Cancel.
    ///
    /// This catches the failure mode where PM book moves without a
    /// corresponding big spot move — the microstructure / liquidity-driven
    /// AS that bit us on 2026-05-14.
    async fn run_book_as_cancel_path(self: &Arc<Self>, asset_id: String) {
        // Look up which slug + side this asset_id belongs to.
        let (slug, side) = match self.state.token_to_slug.get(&asset_id) {
            Some(e) => e.value().clone(),
            None => return, // not a market we track
        };
        let market_arc = match self.state.markets.get(&slug) {
            Some(e) => e.value().load_full(),
            None => return,
        };
        let qs = match side {
            BinarySide::Up => &market_arc.quoting_up,
            BinarySide::Down => &market_arc.quoting_down,
        };
        // Fast-skip if we have no open orders on this side.
        if qs.open_orders.is_empty() {
            return;
        }

        // Read the fresh book for THIS side.
        let book = {
            let mws = self.market_ws.lock().await;
            match mws.latest_book(&asset_id) {
                Some(b) => b,
                None => return,
            }
        };
        let best_bid = match book.best_bid() {
            Some((p, _)) => p,
            None => return,
        };

        // Compute the "ahead of market" threshold. If our resting order
        // price exceeds the current best bid by ≥ 2c, we're a free target.
        // (We placed at bid+1c. New best bid 2c below us means market moved
        // 3c away — clear adverse signal.)
        const BOOK_AS_AHEAD_THRESHOLD: f64 = 0.02;

        let mut cancels: Vec<QuoteIntent> = Vec::new();
        for (oid, o) in qs.open_orders.iter() {
            // The "ahead of market" delta. Cancel only if we'd improve by
            // > threshold; small movements don't justify cancel churn.
            let ahead = o.price - best_bid;
            if ahead > BOOK_AS_AHEAD_THRESHOLD {
                cancels.push(QuoteIntent::Cancel {
                    slug: slug.clone(),
                    side,
                    order_id: oid.clone(),
                    reason: "paam_book_as_ahead_of_market",
                });
            }
        }
        if cancels.is_empty() {
            return;
        }
        self.audit.write(
            "book_as_cancels_emitted",
            json!({
                "slug": &slug,
                "side": side.as_str(),
                "asset_id": asset_id,
                "best_bid": best_bid,
                "n_cancels": cancels.len(),
            }),
        );
        // Execute the cancels.
        let _ = self.execute_intents(cancels, None).await;
    }

    /// Main shadow/live loop: 1Hz decision tick across all live markets.
    pub async fn run(self: Arc<Self>) -> Result<()> {
        info!(?self.mode, "runtime: starting");
        let mut tick = tokio::time::interval(Duration::from_secs(1));
        // Drift-free; uses default `MissedTickBehavior::Burst` so a long pause
        // catches us back up. For HF trading we'd prefer Skip — but Phase 3 is
        // shadow-mode, accuracy over speed.
        let mut heartbeat_count: u64 = 0;
        let mut spot_rx = self.spot.subscribe_ticks();
        let mut book_event_rx = self.market_ws.lock().await.subscribe_book_events();
        // Per-coin last-spot at which we LAST fired the AS path. Used to
        // gate on_spot_tick to significant moves (≥30 bps).
        let mut last_as_spot: std::collections::HashMap<Symbol, f64> =
            std::collections::HashMap::new();
        // Production: 30 bps. A 30bps spot move on BTC is ~$240 — that's
        // a meaningful move that shifts a 5m binary by ~10-15 cents on the
        // favorite side and warrants cancel discipline.
        const AS_TRIGGER_BPS: f64 = 30.0;

        loop {
            tokio::select! {
                biased;
                // Fast-path A: Pyth spot tick → AS check (spot-driven AS).
                tick_msg = spot_rx.recv() => {
                    match tick_msg {
                        Ok(t) => {
                            let last = match last_as_spot.get(&t.symbol).copied() {
                                Some(v) if v > 0.0 => v,
                                _ => {
                                    last_as_spot.insert(t.symbol, t.mid);
                                    continue;
                                }
                            };
                            let bps = ((t.mid - last).abs() / last) * 10000.0;
                            if bps < AS_TRIGGER_BPS {
                                continue;
                            }
                            last_as_spot.insert(t.symbol, t.mid);
                            self.run_as_cancel_path(t.symbol, t.mid, bps).await;
                            continue;
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                            warn!(lagged = n, "spot tick channel lagged");
                            continue;
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                            warn!("spot tick channel closed");
                        }
                    }
                }
                // Fast-path B: PM book event → AS check (book-driven AS).
                // This catches the failure mode where PM book moves without
                // a big spot move (microstructure / liquidity dynamics).
                book_msg = book_event_rx.recv() => {
                    match book_msg {
                        Ok(asset_id) => {
                            self.run_book_as_cancel_path(asset_id).await;
                            continue;
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Lagged(n)) => {
                            // Book events are high-volume; lagging is OK,
                            // just keep going (the next event will catch us up).
                            debug!(lagged = n, "book event channel lagged");
                            continue;
                        }
                        Err(tokio::sync::broadcast::error::RecvError::Closed) => {
                            warn!("book event channel closed");
                        }
                    }
                }
                _ = tick.tick() => {}
            }

            // Build the active-market snapshot once per tick.
            let now_unix_s = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map(|d| d.as_secs_f64())
                .unwrap_or(0.0);
            let now_mono_ns = crate::types::now_mono_ns();

            // Snapshot markets first so we never mutate the DashMap during iteration.
            // (Mutating a DashMap shard inside iter() over the same shard deadlocks.)
            let snapshot: Vec<(String, Arc<MarketState>)> = self
                .state
                .markets
                .iter()
                .map(|e| (e.key().clone(), e.value().load_full()))
                .collect();
            let mut to_cleanup: Vec<String> = Vec::new();

            // Per-market: pull book, compute theo, decide, execute.
            for (slug, market_arc) in snapshot {
                let elapsed = now_unix_s - market_arc.market_start_ts as f64;
                let tte = market_arc.window_s as f64 - elapsed;
                if tte <= 0.0 {
                    self.audit.write(
                        "market_cleanup",
                        json!({"slug": &slug, "reason": "past_expiry"}),
                    );
                    to_cleanup.push(slug);
                    continue;
                }

                let (up_book, down_book) = {
                    let mws = self.market_ws.lock().await;
                    let up = match mws.latest_book(&market_arc.up_token_id) {
                        Some(b) => match (b.best_bid(), b.best_ask()) {
                            (Some((b, _)), Some((a, _))) => (b, a),
                            _ => continue,
                        },
                        None => continue,
                    };
                    let down = match mws.latest_book(&market_arc.down_token_id) {
                        Some(b) => match (b.best_bid(), b.best_ask()) {
                            (Some((b, _)), Some((a, _))) => (b, a),
                            _ => continue,
                        },
                        None => continue,
                    };
                    (up, down)
                };

                // Theo via HAR ensemble (Phase 1 wiring). Reads SpotPulse
                // history snapshot + current strike; returns None if any σ
                // window is too sparse or params are missing.
                let spot_price = self.spot.latest(market_arc.coin).map(|t| t.mid);
                // Strike backfill: if discovery couldn't set strike (history
                // wasn't warm yet), try again now.
                let strike = if market_arc.strike > 0.0 {
                    market_arc.strike
                } else {
                    let s_bf = strike_from_history(
                        &self.spot,
                        market_arc.coin,
                        market_arc.market_start_ts as f64,
                    );
                    if s_bf > 0.0 {
                        // Persist into MarketState so next tick reads cheap.
                        let _ = self.state.update(&slug, |m| {
                            let mut new = m.clone();
                            new.strike = s_bf;
                            new
                        });
                    }
                    s_bf
                };
                let theo_p_up = match (
                    self.theo_params.as_ref(),
                    spot_price,
                    strike > 0.0,
                ) {
                    (Some(params), Some(s), true) => {
                        let _g = crate::latency_span!(crate::runtime::latency::Span::TheoCompute);
                        let (series, keys) = self.spot.history_snapshot(market_arc.coin);
                        if series.len() >= 5 {
                            let t_now = (now_mono_ns as f64) / 1_000_000_000.0;
                            crate::strategy::theo::theo_p_up_at_for_coin(
                                t_now,
                                s,
                                strike,
                                tte.max(1.0),
                                &series,
                                &keys,
                                params,
                                Some(market_arc.coin.as_str()),
                            )
                        } else {
                            None
                        }
                    }
                    _ => None,
                };

                let ctx = QuoterContext {
                    market: market_arc.as_ref(),
                    up_book,
                    down_book,
                    theo_p_up,
                    spot: spot_price,
                    elapsed_s: elapsed.max(0.0),
                    tte_s: tte,
                };

                let intents = {
                    let _g = crate::latency_span!(crate::runtime::latency::Span::QuoterDecide);
                    self.quoter.decide(&ctx)
                };
                if !intents.is_empty() {
                    self.audit.write(
                        "quoter_decide",
                        json!({
                            "slug": &slug,
                            "n_intents": intents.len(),
                            "up_book": up_book,
                            "down_book": down_book,
                            "spot": spot_price,
                            "theo_p_up": theo_p_up,
                            "strike": strike,
                            "history_len": self.spot.history_len(market_arc.coin),
                            "elapsed_s": elapsed,
                            "tte_s": tte,
                            "now_mono_ns": now_mono_ns,
                        }),
                    );
                }
                self.execute_intents(intents, theo_p_up).await?;
            }

            // Now safe to remove expired markets — iteration is finished.
            for slug in to_cleanup {
                self.state.cleanup(&slug);
            }

            // Heartbeat once per tick.
            heartbeat_count = heartbeat_count.wrapping_add(1);
            self.audit.write(
                "heartbeat",
                json!({
                    "n_markets": self.state.markets.len(),
                    "n_placed": self.state.n_placed.load(std::sync::atomic::Ordering::Relaxed),
                    "n_cancelled": self.state.n_cancelled.load(std::sync::atomic::Ordering::Relaxed),
                    "n_filled": self.state.n_filled.load(std::sync::atomic::Ordering::Relaxed),
                    "mode": match self.mode { Mode::Shadow => "shadow", Mode::Live => "live" },
                }),
            );

            // Every 30 heartbeats (~30s), emit a TSC latency snapshot. Phase 1
            // measurement gate reads cancel-REST p95 from this stream.
            if heartbeat_count % 30 == 0 {
                let spans = crate::runtime::latency::snapshot_all();
                let by_span: serde_json::Value = spans
                    .iter()
                    .map(|(span, snap)| {
                        (
                            span.as_str().to_string(),
                            json!({
                                "count": snap.count,
                                "mean_us": snap.mean_us(),
                                "max_us": snap.max_us(),
                                "buckets": snap.buckets.clone(),
                            }),
                        )
                    })
                    .collect();
                self.audit.write(
                    "latency_snapshot",
                    json!({
                        "spans": by_span,
                    }),
                );
            }
        }
    }
}

/// Resolve PM crypto Up/Down market strike by reading SpotPulse history.
/// Strike for these markets = spot price AT `market_start_ts_s`.
///
/// Algorithm:
/// 1. Snapshot the history series (ts_seconds, mid) for the coin.
/// 2. Find the entry whose `ts` is closest to `market_start_ts_s`.
/// 3. If the closest entry is within 60 s of the start, use its mid.
/// 4. Otherwise fall back to current `latest()` (best-effort; small error).
/// Returns 0.0 if no history AND no latest tick exists.
pub fn strike_from_history(
    spot: &Arc<crate::feeds::SpotPulse>,
    coin: crate::types::Symbol,
    market_start_ts_s: f64,
) -> f64 {
    let (series, _keys) = spot.history_snapshot(coin);
    if !series.is_empty() {
        let mut best: Option<(f64, f64)> = None; // (abs_dt, mid)
        for (ts, mid) in &series {
            let dt = (ts - market_start_ts_s).abs();
            match best {
                None => best = Some((dt, *mid)),
                Some((b_dt, _)) if dt < b_dt => best = Some((dt, *mid)),
                _ => {}
            }
        }
        if let Some((dt, mid)) = best {
            if dt <= 60.0 {
                return mid;
            }
        }
    }
    spot.latest(coin).map(|t| t.mid).unwrap_or(0.0)
}

/// Helper so MarketEvent receivers can be plumbed for telemetry without
/// blocking the decision loop.
pub async fn drain_market_events(mut rx: mpsc::UnboundedReceiver<MarketEvent>, audit: AuditLogger) {
    while let Some(ev) = rx.recv().await {
        match ev {
            MarketEvent::Book(b) => {
                audit.write(
                    "ws_book",
                    json!({
                        "asset_id": b.asset_id,
                        "market": b.market,
                        "best_bid": b.best_bid().map(|(p,_)| p),
                        "best_ask": b.best_ask().map(|(p,_)| p),
                    }),
                );
            }
            MarketEvent::PriceChange { market, price_changes, .. } => {
                audit.write(
                    "ws_price_change",
                    json!({"market": market, "n_changes": price_changes.len()}),
                );
            }
            MarketEvent::TickSizeChange(t) => {
                audit.write(
                    "ws_tick_size_change",
                    json!({"asset_id": t.asset_id, "new": t.new_tick_size, "old": t.old_tick_size}),
                );
            }
            MarketEvent::LastTradePrice(l) => {
                audit.write(
                    "ws_last_trade",
                    json!({"asset_id": l.asset_id, "price": l.price, "size": l.size, "side": l.side}),
                );
            }
        }
    }
}

