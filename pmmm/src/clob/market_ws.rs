//! Polymarket market-channel WebSocket — public book + price-change feed.
//!
//! Per `docs/developers/CLOB/websocket/market-channel.md`:
//!   wss://ws-subscriptions-clob.polymarket.com/ws/market
//!   subscribe: {"type":"MARKET","asset_ids":["...","..."],"markets":[]}
//!
//! Events emitted on the receiver channel: `book`, `price_change`,
//! `tick_size_change`, `last_trade_price`.

use std::sync::Arc;
use std::time::Duration;

use anyhow::{anyhow, Context, Result};
use dashmap::DashMap;
use futures_util::{SinkExt, StreamExt};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio_tungstenite::tungstenite::Message;
use tracing::{debug, info, warn};

use crate::types::now_mono_ns;

pub const PM_MARKET_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/market";

/// One side of an order book at a given timestamp.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct OrderbookLevel {
    pub price: String,
    pub size: String,
}

/// Full book snapshot for one asset (`book` event).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct BookSnapshot {
    pub asset_id: String,
    pub market: String,
    pub timestamp: String,
    pub hash: String,
    pub bids: Vec<OrderbookLevel>,
    pub asks: Vec<OrderbookLevel>,
    #[serde(skip_serializing, default = "now_mono_ns")]
    pub ts_local_ns: u64,
}

impl BookSnapshot {
    pub fn best_bid(&self) -> Option<(f64, f64)> {
        self.bids.iter().rev().find_map(parse_lvl)
    }
    pub fn best_ask(&self) -> Option<(f64, f64)> {
        self.asks.iter().find_map(parse_lvl)
    }
    pub fn mid(&self) -> Option<f64> {
        let (b, _) = self.best_bid()?;
        let (a, _) = self.best_ask()?;
        Some((b + a) / 2.0)
    }
}

fn parse_lvl(l: &OrderbookLevel) -> Option<(f64, f64)> {
    let p: f64 = l.price.parse().ok()?;
    let s: f64 = l.size.parse().ok()?;
    Some((p, s))
}

/// One `price_change` event entry.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct PriceChange {
    pub asset_id: String,
    pub price: String,
    pub size: String,
    pub side: String,
    pub hash: String,
    pub best_bid: String,
    pub best_ask: String,
    #[serde(skip_serializing, default = "now_mono_ns")]
    pub ts_local_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TickSizeChange {
    pub asset_id: String,
    pub market: String,
    pub old_tick_size: String,
    pub new_tick_size: String,
    pub timestamp: String,
    #[serde(skip_serializing, default = "now_mono_ns")]
    pub ts_local_ns: u64,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LastTradePrice {
    pub asset_id: String,
    pub market: String,
    pub price: String,
    pub side: String,
    pub size: String,
    pub timestamp: String,
    #[serde(default)]
    pub fee_rate_bps: String,
    #[serde(skip_serializing, default = "now_mono_ns")]
    pub ts_local_ns: u64,
}

/// Unified event stream from the market channel.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event_type")]
pub enum MarketEvent {
    #[serde(rename = "book")]
    Book(BookSnapshot),
    #[serde(rename = "price_change")]
    PriceChange { market: String, price_changes: Vec<PriceChange>, timestamp: String },
    #[serde(rename = "tick_size_change")]
    TickSizeChange(TickSizeChange),
    #[serde(rename = "last_trade_price")]
    LastTradePrice(LastTradePrice),
}

/// Market WS client.
///
/// `asset_ids` is the list of ERC-1155 token IDs to subscribe to (UP+DOWN
/// tokens of all active 5m markets, typically). Use `subscribe_more` after
/// `start()` to add additional asset IDs dynamically — the runtime calls
/// this from the market-discovery loop.
pub struct MarketWs {
    asset_ids: Arc<parking_lot::RwLock<Vec<String>>>,
    latest_books: Arc<DashMap<String, BookSnapshot>>,
    tx: Option<mpsc::UnboundedSender<MarketEvent>>,
    sub_tx: Option<mpsc::UnboundedSender<Vec<String>>>,
    /// Broadcast channel of asset_ids whose book just changed. PAAM's
    /// book-AS fast-cancel path subscribes here for sub-50ms reaction to
    /// book moves (distinct from spot moves on Pyth).
    book_event_tx: tokio::sync::broadcast::Sender<String>,
    handle: Option<JoinHandle<()>>,
    url: String,
}

impl MarketWs {
    pub fn new(asset_ids: Vec<String>) -> Self {
        let (book_event_tx, _) = tokio::sync::broadcast::channel(1024);
        Self {
            asset_ids: Arc::new(parking_lot::RwLock::new(asset_ids)),
            latest_books: Arc::new(DashMap::new()),
            tx: None,
            sub_tx: None,
            book_event_tx,
            handle: None,
            url: PM_MARKET_WS_URL.to_string(),
        }
    }

    /// Subscribe to book-change events. The broadcast Sender publishes the
    /// `asset_id` of any token whose book just received an update. Consumer
    /// looks up `latest_book(asset_id)` for the new state.
    pub fn subscribe_book_events(&self) -> tokio::sync::broadcast::Receiver<String> {
        self.book_event_tx.subscribe()
    }

    /// Add more asset IDs to the subscription. Must be called after `start()`.
    /// The session will forward a new subscribe payload on the existing
    /// connection on the next event-loop iteration.
    pub fn subscribe_more(&self, new_ids: Vec<String>) -> anyhow::Result<()> {
        let mut existing = self.asset_ids.write();
        let mut added: Vec<String> = Vec::new();
        for id in &new_ids {
            if !existing.contains(id) {
                existing.push(id.clone());
                added.push(id.clone());
            }
        }
        drop(existing);
        if added.is_empty() {
            return Ok(());
        }
        if let Some(tx) = self.sub_tx.as_ref() {
            tx.send(added)
                .map_err(|e| anyhow::anyhow!("subscribe_more send: {e}"))?;
        }
        Ok(())
    }

    pub fn with_url(mut self, url: impl Into<String>) -> Self {
        self.url = url.into();
        self
    }

    /// Take the event receiver. Must be called before `start()`.
    pub fn take_receiver(&mut self) -> Result<mpsc::UnboundedReceiver<MarketEvent>> {
        if self.tx.is_some() {
            return Err(anyhow!("receiver already taken"));
        }
        let (tx, rx) = mpsc::unbounded_channel();
        self.tx = Some(tx);
        Ok(rx)
    }

    /// Latest book snapshot for one asset (lock-free read).
    pub fn latest_book(&self, asset_id: &str) -> Option<BookSnapshot> {
        self.latest_books.get(asset_id).map(|e| e.value().clone())
    }

    pub async fn start(&mut self) -> Result<()> {
        if self.handle.is_some() {
            return Err(anyhow!("MarketWs already started"));
        }
        let (sub_tx, sub_rx) = mpsc::unbounded_channel::<Vec<String>>();
        self.sub_tx = Some(sub_tx);
        let asset_ids = Arc::clone(&self.asset_ids);
        let url = self.url.clone();
        let books = Arc::clone(&self.latest_books);
        let tx = self.tx.clone();
        let book_event_tx = self.book_event_tx.clone();
        let handle = tokio::spawn(async move {
            let mut backoff = Duration::from_millis(500);
            let sub_rx = Arc::new(tokio::sync::Mutex::new(sub_rx));
            loop {
                let initial = asset_ids.read().clone();
                match run_session(&url, &initial, &books, tx.as_ref(), &book_event_tx, Arc::clone(&sub_rx)).await {
                    Ok(()) => {
                        info!("PM market WS session ended; reconnecting");
                        backoff = Duration::from_millis(500);
                    }
                    Err(e) => {
                        warn!(error = %e, "PM market WS error; reconnecting in {:?}", backoff);
                    }
                }
                tokio::time::sleep(backoff).await;
                backoff = (backoff * 2).min(Duration::from_secs(15));
            }
        });
        self.handle = Some(handle);
        Ok(())
    }
}

impl Drop for MarketWs {
    fn drop(&mut self) {
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }
}

async fn run_session(
    url: &str,
    asset_ids: &[String],
    books: &DashMap<String, BookSnapshot>,
    tx: Option<&mpsc::UnboundedSender<MarketEvent>>,
    book_event_tx: &tokio::sync::broadcast::Sender<String>,
    sub_rx: Arc<tokio::sync::Mutex<mpsc::UnboundedReceiver<Vec<String>>>>,
) -> Result<()> {
    let (ws, _resp) = tokio_tungstenite::connect_async(url)
        .await
        .with_context(|| "connect PM market ws")?;
    info!(asset_count = asset_ids.len(), "PM market WS connected");
    let (mut sink, mut stream) = ws.split();
    if !asset_ids.is_empty() {
        // NB: "assets_ids" with an S — that's the PM-server-quirk spelling,
        // not what their docs say. The Python ref bot's
        // src/websocket_client.py uses the same spelling.
        let sub = serde_json::json!({
            "type": "MARKET",
            "assets_ids": asset_ids,
        });
        sink.send(Message::Text(sub.to_string())).await?;
    }

    let mut sub_rx_guard = sub_rx.lock().await;
    loop {
        tokio::select! {
            msg = stream.next() => {
                let msg = match msg {
                    Some(m) => m.with_context(|| "pm market ws recv")?,
                    None => break,
                };
                match msg {
                    Message::Text(text) => {
                        if let Err(e) = dispatch(&text, books, tx, book_event_tx) {
                            debug!(error = %e, "PM market WS: dropped malformed message");
                        }
                    }
                    Message::Ping(p) => {
                        sink.send(Message::Pong(p)).await.ok();
                    }
                    Message::Close(c) => {
                        info!(close = ?c, "PM market WS closed by server");
                        return Ok(());
                    }
                    _ => {}
                }
            }
            Some(new_ids) = sub_rx_guard.recv() => {
                if !new_ids.is_empty() {
                    // PM uses "subscribe" operation for adds.
                    let sub = serde_json::json!({
                        "operation": "subscribe",
                        "assets_ids": new_ids,
                    });
                    info!(n = new_ids.len(), "PM market WS: subscribing more");
                    if let Err(e) = sink.send(Message::Text(sub.to_string())).await {
                        warn!(error = %e, "subscribe_more send failed");
                    }
                }
            }
        }
    }
    Ok(())
}

fn dispatch(
    text: &str,
    books: &DashMap<String, BookSnapshot>,
    tx: Option<&mpsc::UnboundedSender<MarketEvent>>,
    book_event_tx: &tokio::sync::broadcast::Sender<String>,
) -> Result<()> {
    // PM sometimes sends arrays of events on a single frame.
    let trimmed = text.trim();
    let to_process: Vec<Value> = if trimmed.starts_with('[') {
        serde_json::from_str(trimmed)?
    } else {
        vec![serde_json::from_str(trimmed)?]
    };

    for v in to_process {
        let event_type = v.get("event_type").and_then(|t| t.as_str()).unwrap_or("");
        match event_type {
            "book" => {
                let mut book: BookSnapshot = serde_json::from_value(v).context("parse book")?;
                book.ts_local_ns = now_mono_ns();
                let asset_id = book.asset_id.clone();
                books.insert(asset_id.clone(), book.clone());
                if let Some(tx) = tx {
                    let _ = tx.send(MarketEvent::Book(book));
                }
                // Broadcast for AS fast-cancel path. Ignore failure (no subs OK).
                let _ = book_event_tx.send(asset_id);
            }
            "price_change" => {
                let market = v.get("market").and_then(|m| m.as_str()).unwrap_or("").to_string();
                let timestamp = v.get("timestamp").and_then(|t| t.as_str()).unwrap_or("").to_string();
                let changes: Vec<PriceChange> = serde_json::from_value(
                    v.get("price_changes").cloned().unwrap_or(serde_json::json!([])),
                )
                .context("parse price_changes")?;
                // Broadcast each affected asset_id so the AS listener can
                // re-check open orders against the new book state.
                for ch in &changes {
                    let _ = book_event_tx.send(ch.asset_id.clone());
                }
                if let Some(tx) = tx {
                    let _ = tx.send(MarketEvent::PriceChange {
                        market,
                        price_changes: changes,
                        timestamp,
                    });
                }
            }
            "tick_size_change" => {
                let ts: TickSizeChange = serde_json::from_value(v).context("parse tick_size_change")?;
                if let Some(tx) = tx {
                    let _ = tx.send(MarketEvent::TickSizeChange(ts));
                }
            }
            "last_trade_price" => {
                let ltp: LastTradePrice = serde_json::from_value(v).context("parse last_trade_price")?;
                if let Some(tx) = tx {
                    let _ = tx.send(MarketEvent::LastTradePrice(ltp));
                }
            }
            _ => {}
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_book_event() {
        let frame = r#"{
            "event_type": "book",
            "asset_id": "0xtok",
            "market": "0xcond",
            "timestamp": "1700000000000",
            "hash": "abc",
            "bids": [{"price":"0.49","size":"10"},{"price":"0.50","size":"5"}],
            "asks": [{"price":"0.51","size":"7"},{"price":"0.52","size":"3"}]
        }"#;
        let books = DashMap::new();
        dispatch(frame, &books, None, &tokio::sync::broadcast::channel::<String>(8).0).expect("dispatch");
        let book = books.get("0xtok").unwrap();
        assert_eq!(book.bids.len(), 2);
        assert_eq!(book.asks.len(), 2);
        assert_eq!(book.best_bid().unwrap().0, 0.50);
        assert_eq!(book.best_ask().unwrap().0, 0.51);
        assert!((book.mid().unwrap() - 0.505).abs() < 1e-9);
    }

    #[test]
    fn parse_price_change_event() {
        let frame = r#"{
            "event_type": "price_change",
            "market": "0xmkt",
            "timestamp": "1700000000000",
            "price_changes": [{
                "asset_id":"0xtok","price":"0.50","size":"5","side":"BUY",
                "hash":"h","best_bid":"0.49","best_ask":"0.51"
            }]
        }"#;
        let books = DashMap::new();
        // Just verifying parse — would need a receiver to inspect the event.
        dispatch(frame, &books, None, &tokio::sync::broadcast::channel::<String>(8).0).expect("dispatch");
    }

    #[test]
    fn parse_array_of_events() {
        let frame = r#"[
            {"event_type":"tick_size_change","asset_id":"a","market":"m","old_tick_size":"0.01","new_tick_size":"0.001","timestamp":"1"},
            {"event_type":"book","asset_id":"b","market":"m","timestamp":"1","hash":"h","bids":[],"asks":[]}
        ]"#;
        let books = DashMap::new();
        dispatch(frame, &books, None, &tokio::sync::broadcast::channel::<String>(8).0).expect("dispatch array");
        assert!(books.contains_key("b"));
    }
}
