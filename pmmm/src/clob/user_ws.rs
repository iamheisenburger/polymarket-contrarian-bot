//! Polymarket user-channel WebSocket — authenticated stream of fills,
//! order placements, updates, and cancellations for the wallet.
//!
//! Per `docs/developers/CLOB/websocket/user-channel.md`:
//!   wss://ws-subscriptions-clob.polymarket.com/ws/user
//!   subscribe: {"type":"USER","auth":{"apiKey":...,"secret":...,"passphrase":...},"markets":["..."]}
//!
//! This is the ~100ms push channel that replaces the Python bot's 4-second
//! wallet-activity polling. Trade events progress through statuses:
//!   MATCHED → MINED → CONFIRMED  (success path)
//!   MATCHED → RETRYING → FAILED  (failure path)
//!
//! We rely on CONFIRMED for ground-truth fill detection. CLAUDE.md rule:
//! wallet-confirmed fill is truth.

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

use crate::signer::L2Creds;
use crate::types::now_mono_ns;

pub const PM_USER_WS_URL: &str = "wss://ws-subscriptions-clob.polymarket.com/ws/user";

/// One leg of a trade (the maker order that got hit).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct MakerOrderRef {
    #[serde(default)]
    pub order_id: String,
    #[serde(default)]
    pub maker: String,
    #[serde(default)]
    pub asset_id: String,
    #[serde(default)]
    pub matched_amount: String,
    #[serde(default)]
    pub price: String,
    #[serde(default)]
    pub fee_rate_bps: String,
    #[serde(default)]
    pub side: String,
    #[serde(default)]
    pub outcome: String,
}

/// Trade event — fired on MATCHED/MINED/CONFIRMED/RETRYING/FAILED transitions.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TradeEvent {
    #[serde(default)]
    pub status: String, // MATCHED | MINED | CONFIRMED | RETRYING | FAILED
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub market: String,
    #[serde(default)]
    pub asset_id: String,
    #[serde(default)]
    pub side: String, // BUY | SELL
    #[serde(default)]
    pub outcome: String, // Up | Down
    #[serde(default)]
    pub price: String,
    #[serde(default)]
    pub size: String,
    #[serde(default, alias = "transaction_hash")]
    pub transaction_hash: String,
    #[serde(default)]
    pub maker_orders: Vec<MakerOrderRef>,
    #[serde(default)]
    pub timestamp: String,
    #[serde(skip_serializing, default = "now_mono_ns")]
    pub ts_local_ns: u64,
}

/// Order lifecycle event — fired on PLACEMENT, UPDATE (partial fill),
/// CANCELLATION.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderEvent {
    #[serde(default, rename = "type")]
    pub kind: String, // PLACEMENT | UPDATE | CANCELLATION
    #[serde(default)]
    pub id: String,
    #[serde(default)]
    pub market: String,
    #[serde(default)]
    pub asset_id: String,
    #[serde(default)]
    pub side: String,
    #[serde(default)]
    pub price: String,
    #[serde(default)]
    pub size: String,
    #[serde(default)]
    pub original_size: String,
    #[serde(default)]
    pub size_matched: String,
    #[serde(default)]
    pub timestamp: String,
    #[serde(skip_serializing, default = "now_mono_ns")]
    pub ts_local_ns: u64,
}

/// Unified user-channel event.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event_type")]
pub enum UserEvent {
    #[serde(rename = "trade")]
    Trade(TradeEvent),
    #[serde(rename = "order")]
    Order(OrderEvent),
}

/// Authenticated PM user-channel WebSocket client.
pub struct UserWs {
    creds: L2Creds,
    markets: Vec<String>,
    /// Latest known status per order_id (CONFIRMED fills are sticky).
    latest_orders: Arc<DashMap<String, OrderEvent>>,
    tx: Option<mpsc::UnboundedSender<UserEvent>>,
    handle: Option<JoinHandle<()>>,
    url: String,
}

impl UserWs {
    pub fn new(creds: L2Creds, markets: Vec<String>) -> Self {
        Self {
            creds,
            markets,
            latest_orders: Arc::new(DashMap::new()),
            tx: None,
            handle: None,
            url: PM_USER_WS_URL.to_string(),
        }
    }

    pub fn with_url(mut self, url: impl Into<String>) -> Self {
        self.url = url.into();
        self
    }

    pub fn take_receiver(&mut self) -> Result<mpsc::UnboundedReceiver<UserEvent>> {
        if self.tx.is_some() {
            return Err(anyhow!("receiver already taken"));
        }
        let (tx, rx) = mpsc::unbounded_channel();
        self.tx = Some(tx);
        Ok(rx)
    }

    pub fn latest_order(&self, order_id: &str) -> Option<OrderEvent> {
        self.latest_orders.get(order_id).map(|e| e.value().clone())
    }

    pub async fn start(&mut self) -> Result<()> {
        if self.handle.is_some() {
            return Err(anyhow!("UserWs already started"));
        }
        let creds = self.creds.clone();
        let markets = self.markets.clone();
        let url = self.url.clone();
        let latest_orders = Arc::clone(&self.latest_orders);
        let tx = self.tx.clone();

        let handle = tokio::spawn(async move {
            let mut backoff = Duration::from_millis(500);
            loop {
                match run_session(&url, &creds, &markets, &latest_orders, tx.as_ref()).await {
                    Ok(()) => {
                        info!("PM user WS session ended; reconnecting");
                        backoff = Duration::from_millis(500);
                    }
                    Err(e) => {
                        warn!(error = %e, "PM user WS error; reconnecting in {:?}", backoff);
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

impl Drop for UserWs {
    fn drop(&mut self) {
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }
}

async fn run_session(
    url: &str,
    creds: &L2Creds,
    markets: &[String],
    latest_orders: &DashMap<String, OrderEvent>,
    tx: Option<&mpsc::UnboundedSender<UserEvent>>,
) -> Result<()> {
    let (ws, _resp) = tokio_tungstenite::connect_async(url)
        .await
        .with_context(|| "connect PM user ws")?;
    info!("PM user WS connected ({} markets)", markets.len());
    let (mut sink, mut stream) = ws.split();
    let sub = serde_json::json!({
        "type": "USER",
        "auth": {
            "apiKey": creds.api_key,
            "secret": creds.secret,
            "passphrase": creds.passphrase,
        },
        "markets": markets,
    });
    sink.send(Message::Text(sub.to_string())).await?;

    while let Some(msg) = stream.next().await {
        let msg = msg.with_context(|| "pm user ws recv")?;
        match msg {
            Message::Text(text) => {
                if let Err(e) = dispatch(&text, latest_orders, tx) {
                    debug!(error = %e, "PM user WS: dropped malformed message");
                }
            }
            Message::Ping(p) => {
                sink.send(Message::Pong(p)).await.ok();
            }
            Message::Close(c) => {
                info!(close = ?c, "PM user WS closed by server");
                return Ok(());
            }
            _ => {}
        }
    }
    Ok(())
}

fn dispatch(
    text: &str,
    latest_orders: &DashMap<String, OrderEvent>,
    tx: Option<&mpsc::UnboundedSender<UserEvent>>,
) -> Result<()> {
    let trimmed = text.trim();
    let to_process: Vec<Value> = if trimmed.starts_with('[') {
        serde_json::from_str(trimmed)?
    } else {
        vec![serde_json::from_str(trimmed)?]
    };
    for v in to_process {
        let event_type = v.get("event_type").and_then(|t| t.as_str()).unwrap_or("");
        match event_type {
            "trade" => {
                let mut t: TradeEvent = serde_json::from_value(v).context("parse trade")?;
                t.ts_local_ns = now_mono_ns();
                if let Some(tx) = tx {
                    let _ = tx.send(UserEvent::Trade(t));
                }
            }
            "order" => {
                let mut o: OrderEvent = serde_json::from_value(v).context("parse order")?;
                o.ts_local_ns = now_mono_ns();
                if !o.id.is_empty() {
                    latest_orders.insert(o.id.clone(), o.clone());
                }
                if let Some(tx) = tx {
                    let _ = tx.send(UserEvent::Order(o));
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
    fn parse_trade_matched() {
        let frame = r#"{
            "event_type": "trade",
            "status": "MATCHED",
            "id": "tr_1",
            "market": "0xmkt",
            "asset_id": "0xtok",
            "side": "BUY",
            "outcome": "Up",
            "price": "0.50",
            "size": "5",
            "transaction_hash": "0xtxhash",
            "maker_orders": [{
                "order_id": "ord_1",
                "maker": "0xmaker",
                "asset_id": "0xtok",
                "matched_amount": "5",
                "price": "0.50",
                "fee_rate_bps": "0",
                "side": "SELL",
                "outcome": "Up"
            }],
            "timestamp": "1700000000000"
        }"#;
        let latest = DashMap::new();
        dispatch(frame, &latest, None).expect("dispatch");
    }

    #[test]
    fn parse_order_placement_tracks_id() {
        let frame = r#"{
            "event_type": "order",
            "type": "PLACEMENT",
            "id": "ord_99",
            "market": "0xmkt",
            "asset_id": "0xtok",
            "side": "BUY",
            "price": "0.65",
            "size": "5",
            "original_size": "5",
            "size_matched": "0",
            "timestamp": "1700000000000"
        }"#;
        let latest = DashMap::new();
        dispatch(frame, &latest, None).expect("dispatch");
        let o = latest.get("ord_99").unwrap();
        assert_eq!(o.kind, "PLACEMENT");
        assert_eq!(o.side, "BUY");
    }
}
