//! Coinbase WebSocket feed.
//!
//! Direct port of `lib/coinbase_ws.py`. Subscribes to the `ticker` channel
//! for sub-second price updates. Coinbase publishes for all major coins
//! including HYPE (which Binance doesn't have).
//!
//! Protocol:
//!   wss://ws-feed.exchange.coinbase.com
//!   subscribe: {"type":"subscribe","product_ids":["BTC-USD",...],"channels":["ticker"]}
//!   updates:   {"type":"ticker","product_id":"BTC-USD","price":"79500.00",...}

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

const COINBASE_WS_URL: &str = "wss://ws-feed.exchange.coinbase.com";

static COINBASE_PRODUCTS: Lazy<HashMap<Symbol, &'static str>> = Lazy::new(|| {
    let mut m = HashMap::new();
    m.insert(Symbol::BTC, "BTC-USD");
    m.insert(Symbol::ETH, "ETH-USD");
    m.insert(Symbol::SOL, "SOL-USD");
    m.insert(Symbol::XRP, "XRP-USD");
    m.insert(Symbol::DOGE, "DOGE-USD");
    m.insert(Symbol::HYPE, "HYPE-USD");
    // BNB not listed on Coinbase
    m
});

static PRODUCT_TO_SYMBOL: Lazy<HashMap<&'static str, Symbol>> = Lazy::new(|| {
    COINBASE_PRODUCTS.iter().map(|(s, p)| (*p, *s)).collect()
});

pub struct CoinbaseFeed {
    symbols: Vec<Symbol>,
    latest: Arc<DashMap<Symbol, PriceTick>>,
    tx: Option<mpsc::UnboundedSender<PriceTick>>,
    handle: Option<JoinHandle<()>>,
}

impl CoinbaseFeed {
    pub fn new(symbols: &[Symbol]) -> Self {
        let symbols: Vec<Symbol> = symbols
            .iter()
            .copied()
            .filter(|s| COINBASE_PRODUCTS.contains_key(s))
            .collect();
        Self {
            symbols,
            latest: Arc::new(DashMap::new()),
            tx: None,
            handle: None,
        }
    }

    pub fn take_receiver(&mut self) -> Result<mpsc::UnboundedReceiver<PriceTick>> {
        if self.tx.is_some() {
            return Err(anyhow!("receiver already taken"));
        }
        let (tx, rx) = mpsc::unbounded_channel();
        self.tx = Some(tx);
        Ok(rx)
    }
}

#[async_trait]
impl PriceFeed for CoinbaseFeed {
    fn name(&self) -> &'static str {
        "Coinbase"
    }

    fn supported(&self) -> &[Symbol] {
        &self.symbols
    }

    async fn start(&mut self) -> Result<()> {
        if self.handle.is_some() {
            return Err(anyhow!("Coinbase feed already started"));
        }
        let symbols = self.symbols.clone();
        let latest = Arc::clone(&self.latest);
        let tx = self.tx.clone();
        let handle = tokio::spawn(async move {
            let mut backoff = Duration::from_millis(500);
            loop {
                match run_session(&symbols, &latest, tx.as_ref()).await {
                    Ok(()) => {
                        info!("Coinbase WS session ended; reconnecting");
                        backoff = Duration::from_millis(500);
                    }
                    Err(e) => {
                        warn!(error = %e, "Coinbase WS error; reconnecting in {:?}", backoff);
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
        self.latest.get(&symbol).map(|e| *e.value())
    }
}

impl Drop for CoinbaseFeed {
    fn drop(&mut self) {
        if let Some(h) = self.handle.take() {
            h.abort();
        }
    }
}

async fn run_session(
    symbols: &[Symbol],
    latest: &DashMap<Symbol, PriceTick>,
    tx: Option<&mpsc::UnboundedSender<PriceTick>>,
) -> Result<()> {
    let (ws, _resp) = tokio_tungstenite::connect_async(COINBASE_WS_URL)
        .await
        .with_context(|| "connect coinbase ws")?;
    info!("Coinbase: WS connected ({} symbols)", symbols.len());
    let (mut sink, mut stream) = ws.split();

    let product_ids: Vec<&str> = symbols
        .iter()
        .filter_map(|s| COINBASE_PRODUCTS.get(s).copied())
        .collect();
    let sub = serde_json::json!({
        "type": "subscribe",
        "product_ids": product_ids,
        "channels": ["ticker"],
    });
    sink.send(Message::Text(sub.to_string())).await?;
    debug!(?product_ids, "Coinbase: subscribed");

    while let Some(msg) = stream.next().await {
        let msg = msg.with_context(|| "coinbase ws recv")?;
        match msg {
            Message::Text(text) => {
                if let Err(e) = handle_text(&text, latest, tx) {
                    debug!(error = %e, "Coinbase: dropped malformed message");
                }
            }
            Message::Ping(p) => {
                sink.send(Message::Pong(p)).await.ok();
            }
            Message::Close(c) => {
                info!(close = ?c, "Coinbase: WS closed by server");
                return Ok(());
            }
            _ => {}
        }
    }
    Ok(())
}

fn handle_text(
    text: &str,
    latest: &DashMap<Symbol, PriceTick>,
    tx: Option<&mpsc::UnboundedSender<PriceTick>>,
) -> Result<()> {
    let v: Value = serde_json::from_str(text)?;
    if v.get("type").and_then(|t| t.as_str()) != Some("ticker") {
        return Ok(());
    }
    let product_id = v
        .get("product_id")
        .and_then(|p| p.as_str())
        .ok_or_else(|| anyhow!("missing product_id"))?;
    let symbol = match PRODUCT_TO_SYMBOL.get(product_id) {
        Some(s) => *s,
        None => return Ok(()),
    };
    let price_str = v
        .get("price")
        .and_then(|p| p.as_str())
        .ok_or_else(|| anyhow!("missing price"))?;
    let mid: f64 = price_str.parse().with_context(|| "parse price f64")?;
    if !(mid.is_finite() && mid > 0.0) {
        return Ok(());
    }
    let tick = PriceTick {
        symbol,
        source: FeedSource::Coinbase,
        mid,
        ts_local_ns: now_mono_ns(),
    };
    latest.insert(symbol, tick);
    if let Some(tx) = tx {
        let _ = tx.send(tick);
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn product_map_covers_expected_symbols() {
        // BTC/ETH/SOL/XRP/DOGE/HYPE are on Coinbase; BNB is not.
        for sym in &[Symbol::BTC, Symbol::ETH, Symbol::SOL, Symbol::XRP, Symbol::DOGE, Symbol::HYPE] {
            assert!(COINBASE_PRODUCTS.contains_key(sym), "missing {sym}");
        }
        assert!(!COINBASE_PRODUCTS.contains_key(&Symbol::BNB));
    }

    #[test]
    fn parse_ticker_message() {
        let frame = r#"{
            "type": "ticker",
            "product_id": "BTC-USD",
            "price": "79500.50",
            "best_bid": "79500.00",
            "best_ask": "79501.00"
        }"#;
        let latest = DashMap::new();
        handle_text(frame, &latest, None).expect("parse");
        let tick = latest.get(&Symbol::BTC).unwrap();
        assert!((tick.mid - 79500.50).abs() < 1e-6);
        assert_eq!(tick.source, FeedSource::Coinbase);
    }

    #[test]
    fn parse_ignores_subscribe_ack() {
        let frame = r#"{"type":"subscriptions","channels":[]}"#;
        let latest = DashMap::new();
        handle_text(frame, &latest, None).expect("parse");
        assert!(latest.is_empty());
    }
}
