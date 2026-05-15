//! Pyth Hermes WebSocket feed.
//!
//! Direct port of `lib/pyth_ws.py`. Per memory `project_pyth_dominates_first_tick.md`,
//! Pyth wins 23-45% of first ticks across BTC/ETH/SOL/XRP/DOGE/BNB on tier-1
//! MM publishes (Jane Street, Jump, DRW, Wintermute, Virtu, GTS publishing
//! directly into Hermes). Live Phase 0 measurement confirmed **48.0%** first-
//! tick share over 30s on BTC/ETH/SOL/XRP.
//!
//! Protocol (https://docs.pyth.network/price-feeds/use-real-time-data/web-socket-api):
//!   wss://hermes.pyth.network/ws
//!   subscribe: {"type":"subscribe","ids":[hex32_no_0x_prefix,...],"verbose":false,"binary":false}
//!   updates:   {"type":"price_update","price_feed":{"id":"...","price":{"price":"...","expo":-8,"publish_time":...}}}
//!
//! We record LOCAL reception time (not publish_time) for first-tick-win
//! attribution — publish_time is when the MM emitted, not when we received.

use std::collections::HashMap;
use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use async_trait::async_trait;
use dashmap::DashMap;
use futures_util::{SinkExt, StreamExt};
use once_cell::sync::Lazy;
use serde_json::Value;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, info, warn};

use crate::feeds::PriceFeed;
use crate::types::{now_mono_ns, FeedSource, PriceTick, Symbol};

const PYTH_WS_URL: &str = "wss://hermes.pyth.network/ws";

/// Map from `Symbol` to Pyth's 32-byte feed ID (hex, no `0x` prefix).
/// Source: https://pyth.network/developers/price-feed-ids
static PYTH_IDS: Lazy<HashMap<Symbol, &'static str>> = Lazy::new(|| {
    let mut m = HashMap::new();
    m.insert(Symbol::BTC,  "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43");
    m.insert(Symbol::ETH,  "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace");
    m.insert(Symbol::SOL,  "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d");
    m.insert(Symbol::XRP,  "ec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8");
    m.insert(Symbol::DOGE, "dcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c");
    m.insert(Symbol::BNB,  "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f");
    m.insert(Symbol::HYPE, "4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b");
    m
});

/// Reverse map from id (no 0x prefix) → Symbol.
static ID_TO_SYMBOL: Lazy<HashMap<&'static str, Symbol>> = Lazy::new(|| {
    PYTH_IDS
        .iter()
        .map(|(sym, id)| (*id, *sym))
        .collect()
});

/// Pyth Hermes WebSocket feed.
///
/// Spawns a background task on `start()` that holds the WS connection,
/// dispatches `PriceTick`s into a tokio mpsc, and updates a shared lock-free
/// `DashMap<Symbol, PriceTick>` for `latest()` reads.
pub struct PythFeed {
    symbols: Vec<Symbol>,
    latest: Arc<DashMap<Symbol, PriceTick>>,
    tx: Option<mpsc::UnboundedSender<PriceTick>>,
    handle: Option<JoinHandle<()>>,
}

impl PythFeed {
    /// Build a new feed for the given symbols. Symbols not in `PYTH_IDS` are
    /// silently dropped — matches Python behavior (logs a warning).
    pub fn new(symbols: &[Symbol]) -> Self {
        let symbols: Vec<Symbol> = symbols
            .iter()
            .copied()
            .filter(|s| PYTH_IDS.contains_key(s))
            .collect();
        for s in symbols.iter() {
            debug!(symbol = %s, "Pyth: enabling");
        }
        Self {
            symbols,
            latest: Arc::new(DashMap::new()),
            tx: None,
            handle: None,
        }
    }

    /// Take ownership of the price-tick receiver. Must be called once before
    /// `start()`. Returns an unbounded receiver of all incoming ticks.
    pub fn take_receiver(&mut self) -> Result<mpsc::UnboundedReceiver<PriceTick>> {
        if self.tx.is_some() {
            return Err(anyhow!("receiver already taken"));
        }
        let (tx, rx) = mpsc::unbounded_channel::<PriceTick>();
        self.tx = Some(tx);
        Ok(rx)
    }

    /// Latest tick for one symbol, lock-free.
    pub fn latest_tick(&self, symbol: Symbol) -> Option<PriceTick> {
        self.latest.get(&symbol).map(|e| *e.value())
    }
}

#[async_trait]
impl PriceFeed for PythFeed {
    fn name(&self) -> &'static str {
        "Pyth"
    }

    fn supported(&self) -> &[Symbol] {
        &self.symbols
    }

    async fn start(&mut self) -> Result<()> {
        if self.handle.is_some() {
            return Err(anyhow!("Pyth feed already started"));
        }
        let symbols = self.symbols.clone();
        let latest = Arc::clone(&self.latest);
        let tx = self.tx.clone(); // optional broadcast sink for the aggregator

        let handle = tokio::spawn(async move {
            // Reconnect loop with exponential backoff (capped).
            let mut backoff = Duration::from_millis(500);
            loop {
                match run_session(&symbols, &latest, tx.as_ref()).await {
                    Ok(()) => {
                        info!("Pyth WS session ended cleanly; reconnecting");
                        backoff = Duration::from_millis(500);
                    }
                    Err(e) => {
                        warn!(error = %e, "Pyth WS session error; reconnecting in {:?}", backoff);
                    }
                }
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(15));
            }
        });
        self.handle = Some(handle);
        Ok(())
    }

    fn latest(&self, symbol: Symbol) -> Option<PriceTick> {
        self.latest_tick(symbol)
    }
}

impl Drop for PythFeed {
    fn drop(&mut self) {
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }
}

/// Run one WS session: subscribe, receive ticks, push them to shared state
/// and the optional broadcast channel. Returns when the session ends.
async fn run_session(
    symbols: &[Symbol],
    latest: &DashMap<Symbol, PriceTick>,
    tx: Option<&mpsc::UnboundedSender<PriceTick>>,
) -> Result<()> {
    debug!("Pyth: connecting {}", PYTH_WS_URL);
    let (ws_stream, _resp) = tokio_tungstenite::connect_async(PYTH_WS_URL)
        .await
        .with_context(|| "connect pyth hermes ws")?;
    info!("Pyth: WS connected ({} symbols)", symbols.len());

    let (mut sink, mut stream) = ws_stream.split();

    // Build subscribe payload.
    let ids: Vec<&str> = symbols.iter().filter_map(|s| PYTH_IDS.get(s).copied()).collect();
    let sub = serde_json::json!({
        "type": "subscribe",
        "ids": ids,
        "verbose": false,
        "binary": false,
    });
    sink.send(Message::Text(sub.to_string())).await?;
    debug!("Pyth: subscribed to {} ids", ids.len());

    while let Some(msg) = stream.next().await {
        let msg = msg.with_context(|| "pyth ws recv")?;
        match msg {
            Message::Text(text) => {
                if let Err(e) = handle_text_message(&text, latest, tx) {
                    debug!(error = %e, "Pyth: dropped malformed message");
                }
            }
            Message::Binary(_) => {} // verbose=false binary=false → shouldn't get binary
            Message::Ping(p) => {
                sink.send(Message::Pong(p)).await.ok();
            }
            Message::Pong(_) => {}
            Message::Close(c) => {
                info!(close = ?c, "Pyth: WS closed by server");
                return Ok(());
            }
            Message::Frame(_) => {}
        }
    }
    Ok(())
}

/// Parse one text frame and update shared state.
fn handle_text_message(
    text: &str,
    latest: &DashMap<Symbol, PriceTick>,
    tx: Option<&mpsc::UnboundedSender<PriceTick>>,
) -> Result<()> {
    let v: Value = serde_json::from_str(text).with_context(|| "parse pyth frame")?;
    let msg_type = v.get("type").and_then(|t| t.as_str()).unwrap_or("");
    if msg_type != "price_update" {
        return Ok(());
    }

    let pf = v.get("price_feed").ok_or_else(|| anyhow!("missing price_feed"))?;

    // Pyth sends id without 0x prefix; some endpoints prepend "0x" — handle both.
    let raw_id = pf.get("id").and_then(|i| i.as_str()).unwrap_or("");
    let id = raw_id.trim_start_matches("0x");
    let symbol = match ID_TO_SYMBOL.get(id) {
        Some(s) => *s,
        None => return Ok(()), // unknown id (shouldn't happen, we subscribed to fixed set)
    };

    let price = pf.get("price").ok_or_else(|| anyhow!("missing price obj"))?;
    let raw_str = price
        .get("price")
        .and_then(|p| p.as_str())
        .ok_or_else(|| anyhow!("missing price.price str"))?;
    let expo = price
        .get("expo")
        .and_then(|e| e.as_i64())
        .ok_or_else(|| anyhow!("missing price.expo"))?;
    let raw: f64 = raw_str.parse().with_context(|| "parse price.price as f64")?;
    let mid = raw * 10f64.powi(expo as i32);
    if !(mid.is_finite() && mid > 0.0) {
        return Ok(());
    }

    let tick = PriceTick {
        symbol,
        source: FeedSource::Pyth,
        mid,
        ts_local_ns: now_mono_ns(),
    };
    latest.insert(symbol, tick);
    if let Some(tx) = tx {
        let _ = tx.send(tick); // ignore closed receiver — feed is best-effort
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_map_complete() {
        for sym in Symbol::all() {
            assert!(PYTH_IDS.contains_key(sym), "missing pyth id for {sym}");
        }
        assert_eq!(PYTH_IDS.len(), ID_TO_SYMBOL.len());
    }

    #[test]
    fn parse_price_update_btc() {
        // Synthetic Pyth frame (BTC at $79,500 with expo=-8 → raw=7950000000000)
        let frame = r#"{
            "type": "price_update",
            "price_feed": {
                "id": "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "7950000000000", "expo": -8, "publish_time": 1700000000}
            }
        }"#;
        let latest = DashMap::new();
        handle_text_message(frame, &latest, None).expect("parse");
        let tick = latest.get(&Symbol::BTC).unwrap();
        assert!((tick.mid - 79500.0).abs() < 1e-6, "got {}", tick.mid);
        assert_eq!(tick.source, FeedSource::Pyth);
    }

    #[test]
    fn parse_price_update_with_0x_prefix() {
        let frame = r#"{
            "type": "price_update",
            "price_feed": {
                "id": "0xe62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
                "price": {"price": "8000000000000", "expo": -8, "publish_time": 1700000000}
            }
        }"#;
        let latest = DashMap::new();
        handle_text_message(frame, &latest, None).expect("parse");
        assert!(latest.contains_key(&Symbol::BTC));
    }

    #[test]
    fn ignores_unknown_id() {
        let frame = r#"{
            "type": "price_update",
            "price_feed": {
                "id": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
                "price": {"price": "1", "expo": 0, "publish_time": 0}
            }
        }"#;
        let latest = DashMap::new();
        handle_text_message(frame, &latest, None).expect("parse");
        assert!(latest.is_empty());
    }

    #[test]
    fn ignores_non_price_update_type() {
        let frame = r#"{"type":"heartbeat"}"#;
        let latest = DashMap::new();
        handle_text_message(frame, &latest, None).expect("parse");
        assert!(latest.is_empty());
    }
}
