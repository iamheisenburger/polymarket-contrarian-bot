//! Binance WebSocket feed (bookTicker stream, faster than aggTrade).
//!
//! Port of `lib/binance_ws.py` simplified to use only `@bookTicker` (which
//! gives mid = (best_bid + best_ask) / 2 on every book update — typically
//! 50-200 updates/sec per symbol, faster than trade-based feeds).
//!
//! Protocol:
//!   wss://stream.binance.com:9443/stream?streams=btcusdt@bookTicker/...
//!   updates wrap in {"stream": "...", "data": {"u": ..., "s": "BTCUSDT", "b": "...", "a": "..."}}

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

static BINANCE_SYMBOLS: Lazy<HashMap<Symbol, &'static str>> = Lazy::new(|| {
    let mut m = HashMap::new();
    m.insert(Symbol::BTC, "btcusdt");
    m.insert(Symbol::ETH, "ethusdt");
    m.insert(Symbol::SOL, "solusdt");
    m.insert(Symbol::XRP, "xrpusdt");
    m.insert(Symbol::DOGE, "dogeusdt");
    m.insert(Symbol::BNB, "bnbusdt");
    // HYPE not on Binance
    m
});

static SYMBOL_TO_COIN: Lazy<HashMap<&'static str, Symbol>> = Lazy::new(|| {
    BINANCE_SYMBOLS.iter().map(|(c, s)| (*s, *c)).collect()
});

pub struct BinanceFeed {
    symbols: Vec<Symbol>,
    latest: Arc<DashMap<Symbol, PriceTick>>,
    tx: Option<mpsc::UnboundedSender<PriceTick>>,
    handle: Option<JoinHandle<()>>,
}

impl BinanceFeed {
    pub fn new(symbols: &[Symbol]) -> Self {
        let symbols: Vec<Symbol> = symbols
            .iter()
            .copied()
            .filter(|s| BINANCE_SYMBOLS.contains_key(s))
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
impl PriceFeed for BinanceFeed {
    fn name(&self) -> &'static str {
        "Binance"
    }

    fn supported(&self) -> &[Symbol] {
        &self.symbols
    }

    async fn start(&mut self) -> Result<()> {
        if self.handle.is_some() {
            return Err(anyhow!("Binance feed already started"));
        }
        let symbols = self.symbols.clone();
        let latest = Arc::clone(&self.latest);
        let tx = self.tx.clone();
        let handle = tokio::spawn(async move {
            let mut backoff = Duration::from_millis(500);
            loop {
                match run_session(&symbols, &latest, tx.as_ref()).await {
                    Ok(()) => {
                        info!("Binance WS session ended; reconnecting");
                        backoff = Duration::from_millis(500);
                    }
                    Err(e) => {
                        warn!(error = %e, "Binance WS error; reconnecting in {:?}", backoff);
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

impl Drop for BinanceFeed {
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
    let streams: Vec<String> = symbols
        .iter()
        .filter_map(|s| BINANCE_SYMBOLS.get(s).copied().map(|sym| format!("{sym}@bookTicker")))
        .collect();
    if streams.is_empty() {
        return Err(anyhow!("no Binance-supported symbols requested"));
    }
    let url = format!(
        "wss://stream.binance.com:9443/stream?streams={}",
        streams.join("/")
    );
    let (ws, _resp) = tokio_tungstenite::connect_async(&url)
        .await
        .with_context(|| "connect binance ws")?;
    info!("Binance: WS connected ({} streams)", streams.len());
    let (mut sink, mut stream) = ws.split();

    while let Some(msg) = stream.next().await {
        let msg = msg.with_context(|| "binance ws recv")?;
        match msg {
            Message::Text(text) => {
                if let Err(e) = handle_text(&text, latest, tx) {
                    debug!(error = %e, "Binance: dropped malformed message");
                }
            }
            Message::Ping(p) => {
                sink.send(Message::Pong(p)).await.ok();
            }
            Message::Close(c) => {
                info!(close = ?c, "Binance: WS closed by server");
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
    // Combined-stream messages wrap data in {"stream": ..., "data": {...}}
    let data = v.get("data").unwrap_or(&v);

    // bookTicker shape: {"u": ..., "s": "BTCUSDT", "b": "...", "B": "...", "a": "...", "A": "..."}
    let symbol_upper = match data.get("s").and_then(|s| s.as_str()) {
        Some(s) => s,
        None => return Ok(()),
    };
    let symbol_lower = symbol_upper.to_ascii_lowercase();
    let symbol = match SYMBOL_TO_COIN.get(symbol_lower.as_str()) {
        Some(s) => *s,
        None => return Ok(()),
    };

    let bid_str = data.get("b").and_then(|b| b.as_str()).unwrap_or("0");
    let ask_str = data.get("a").and_then(|a| a.as_str()).unwrap_or("0");
    let bid: f64 = bid_str.parse().with_context(|| "parse bid")?;
    let ask: f64 = ask_str.parse().with_context(|| "parse ask")?;
    if !(bid > 0.0 && ask > 0.0 && ask >= bid) {
        return Ok(());
    }
    let mid = (bid + ask) / 2.0;
    if !(mid.is_finite() && mid > 0.0) {
        return Ok(());
    }
    let tick = PriceTick {
        symbol,
        source: FeedSource::Binance,
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
    fn symbol_map_covers_expected() {
        for sym in &[Symbol::BTC, Symbol::ETH, Symbol::SOL, Symbol::XRP, Symbol::DOGE, Symbol::BNB] {
            assert!(BINANCE_SYMBOLS.contains_key(sym), "missing {sym}");
        }
        assert!(!BINANCE_SYMBOLS.contains_key(&Symbol::HYPE));
    }

    #[test]
    fn parse_combined_stream_bookticker() {
        let frame = r#"{
            "stream": "btcusdt@bookTicker",
            "data": {"u":123,"s":"BTCUSDT","b":"79500.00","B":"1.0","a":"79501.00","A":"1.0"}
        }"#;
        let latest = DashMap::new();
        handle_text(frame, &latest, None).expect("parse");
        let tick = latest.get(&Symbol::BTC).unwrap();
        assert!((tick.mid - 79500.5).abs() < 1e-6, "got {}", tick.mid);
        assert_eq!(tick.source, FeedSource::Binance);
    }

    #[test]
    fn parse_raw_bookticker_no_wrapper() {
        let frame = r#"{"u":123,"s":"ETHUSDT","b":"2261.00","B":"1.0","a":"2261.50","A":"1.0"}"#;
        let latest = DashMap::new();
        handle_text(frame, &latest, None).expect("parse");
        let tick = latest.get(&Symbol::ETH).unwrap();
        assert!((tick.mid - 2261.25).abs() < 1e-6);
    }

    #[test]
    fn parse_rejects_zero_bid() {
        let frame = r#"{"data":{"s":"BTCUSDT","b":"0","a":"100"}}"#;
        let latest = DashMap::new();
        handle_text(frame, &latest, None).expect("parse");
        assert!(latest.is_empty());
    }
}
