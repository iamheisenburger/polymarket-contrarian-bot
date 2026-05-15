//! SpotPulse — unified spot-price aggregator across Pyth + Coinbase + Binance.
//!
//! Port of `lib.edge_pipeline.SpotPulse`. Tracks per-(symbol, source) first-
//! tick-win counts for telemetry — matches the Phase 0 Python addition where
//! Pyth was measured at 48% first-tick share.
//!
//! Strategy code reads via `latest()` or `freshest()` and gets the most-
//! recently-received tick across all feeds. Lock-free reads (`DashMap` +
//! `ArcSwap`) — no contention with the WS writers.

use std::collections::VecDeque;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use anyhow::Result;
use dashmap::DashMap;
use parking_lot::Mutex;
use tokio::task::JoinHandle;
use tracing::{debug, info};

use crate::feeds::{binance::BinanceFeed, coinbase::CoinbaseFeed, pyth::PythFeed, PriceFeed};
use crate::types::{now_mono_ns, FeedSource, PriceTick, Symbol};

/// Maximum (ts_seconds, mid) samples retained per symbol. ~10 min at 2 Hz
/// effective rate (Pyth Hermes bursts faster, Binance/Coinbase slower).
pub const HISTORY_CAPACITY: usize = 1200;

/// Aggregated, lock-free freshest-tick view across all enabled feeds.
pub struct SpotPulse {
    symbols: Vec<Symbol>,
    /// Per-symbol freshest tick across all feeds.
    latest: Arc<DashMap<Symbol, PriceTick>>,
    /// Per-symbol bounded history of (ts_seconds, mid) on price-change.
    /// Theo recompute reads via `history_snapshot()`.
    history: Arc<DashMap<Symbol, Mutex<VecDeque<(f64, f64)>>>>,
    /// Source attribution counters for first-tick-win telemetry.
    tick_wins: Arc<DashMap<(Symbol, FeedSource), u64>>,
    /// Broadcast channel — every price-change tick is published here.
    /// PAAM's on_spot_tick AS-cancel path subscribes for sub-50ms reaction.
    /// Capacity 1024 — enough for bursty Pyth ticks during volatile moments.
    tick_tx: tokio::sync::broadcast::Sender<PriceTick>,
    /// Feed handles (kept alive for the lifetime of SpotPulse).
    pyth: Option<PythFeed>,
    coinbase: Option<CoinbaseFeed>,
    binance: Option<BinanceFeed>,
    /// Time when all symbols received their first tick. 0 = not warm.
    warm_at_ns: Arc<AtomicU64>,
    aggregator_handle: Mutex<Option<JoinHandle<()>>>,
}

impl SpotPulse {
    pub fn new(symbols: &[Symbol]) -> Self {
        let (tick_tx, _) = tokio::sync::broadcast::channel(1024);
        Self {
            symbols: symbols.to_vec(),
            latest: Arc::new(DashMap::new()),
            history: Arc::new(DashMap::new()),
            tick_wins: Arc::new(DashMap::new()),
            tick_tx,
            pyth: Some(PythFeed::new(symbols)),
            coinbase: Some(CoinbaseFeed::new(symbols)),
            binance: Some(BinanceFeed::new(symbols)),
            warm_at_ns: Arc::new(AtomicU64::new(0)),
            aggregator_handle: Mutex::new(None),
        }
    }

    /// Start all feeds and the aggregator task.
    pub async fn start(&mut self) -> Result<()> {
        // Take broadcast receivers from each feed *before* starting them.
        let mut rx_pyth = self
            .pyth
            .as_mut()
            .expect("pyth missing")
            .take_receiver()?;
        let mut rx_cb = self
            .coinbase
            .as_mut()
            .expect("coinbase missing")
            .take_receiver()?;
        let mut rx_bn = self
            .binance
            .as_mut()
            .expect("binance missing")
            .take_receiver()?;

        // Start feeds.
        self.pyth.as_mut().unwrap().start().await?;
        self.coinbase.as_mut().unwrap().start().await?;
        self.binance.as_mut().unwrap().start().await?;

        let latest = Arc::clone(&self.latest);
        let history = Arc::clone(&self.history);
        let tick_wins = Arc::clone(&self.tick_wins);
        let warm_at = Arc::clone(&self.warm_at_ns);
        let tick_tx = self.tick_tx.clone();
        let warm_target = self.symbols.len();

        let handle = tokio::spawn(async move {
            let mut last_warm_check: u32 = 0;
            loop {
                tokio::select! {
                    Some(t) = rx_pyth.recv() => apply_tick(&latest, &history, &tick_wins, &tick_tx, t),
                    Some(t) = rx_cb.recv()   => apply_tick(&latest, &history, &tick_wins, &tick_tx, t),
                    Some(t) = rx_bn.recv()   => apply_tick(&latest, &history, &tick_wins, &tick_tx, t),
                    else => break,
                }
                // Cheap warmup check — once we have a tick for each symbol,
                // record the warm timestamp.
                if warm_at.load(Ordering::Acquire) == 0 {
                    last_warm_check = last_warm_check.wrapping_add(1);
                    if last_warm_check % 16 == 0 && latest.len() >= warm_target {
                        warm_at.store(now_mono_ns(), Ordering::Release);
                        info!(symbols_warm = latest.len(), "SpotPulse: warmup complete");
                    }
                }
            }
        });
        *self.aggregator_handle.lock() = Some(handle);
        Ok(())
    }

    /// Latest tick across all feeds for one symbol.
    pub fn latest(&self, symbol: Symbol) -> Option<PriceTick> {
        self.latest.get(&symbol).map(|e| *e.value())
    }

    /// Age in nanoseconds since last tick for one symbol. None if no tick yet.
    pub fn age_ns(&self, symbol: Symbol) -> Option<u64> {
        let t = self.latest(symbol)?;
        Some(now_mono_ns().saturating_sub(t.ts_local_ns))
    }

    /// Subscribe to the price-change tick broadcast. The receiver is fed by
    /// `apply_tick` on every tick that changes the freshest mid. Consumer
    /// must handle Lagged() errors if it falls behind 1024 ticks.
    pub fn subscribe_ticks(&self) -> tokio::sync::broadcast::Receiver<PriceTick> {
        self.tick_tx.subscribe()
    }

    /// Snapshot of per-symbol price history as (series, keys) suitable for
    /// `theo::realized_vol` / `theo::theo_p_up_at`. Returns empty Vecs if
    /// no history yet. Clones — caller may keep the buffers across awaits.
    pub fn history_snapshot(&self, symbol: Symbol) -> (Vec<(f64, f64)>, Vec<f64>) {
        let Some(entry) = self.history.get(&symbol) else {
            return (Vec::new(), Vec::new());
        };
        let guard = entry.value().lock();
        let series: Vec<(f64, f64)> = guard.iter().copied().collect();
        let keys: Vec<f64> = series.iter().map(|(t, _)| *t).collect();
        (series, keys)
    }

    /// Number of (ts, mid) samples currently retained for `symbol`.
    pub fn history_len(&self, symbol: Symbol) -> usize {
        self.history.get(&symbol).map(|e| e.value().lock().len()).unwrap_or(0)
    }

    /// Per-(symbol, source) tick counts — telemetry for first-tick-win share.
    pub fn tick_wins(&self) -> Vec<(Symbol, FeedSource, u64)> {
        self.tick_wins
            .iter()
            .map(|e| (e.key().0, e.key().1, *e.value()))
            .collect()
    }

    /// Block (asynchronously) until all symbols have received at least one
    /// tick or the deadline expires. Returns true if warm, false on timeout.
    pub async fn warmup(&self, deadline: Duration) -> bool {
        let start = Instant::now();
        while start.elapsed() < deadline {
            if self.warm_at_ns.load(Ordering::Acquire) != 0 {
                return true;
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
        false
    }
}

impl Drop for SpotPulse {
    fn drop(&mut self) {
        if let Some(h) = self.aggregator_handle.lock().take() {
            h.abort();
        }
    }
}

/// Apply a tick from any feed.
///
/// First-tick-win counting: matches the Python `SpotPulse._pulse_loop`
/// semantics — only attribute a "win" when the freshest mid VALUE changes
/// (not on every WS event). This way Binance's high-rate `bookTicker`
/// doesn't dominate the counter just by sending lots of no-op updates.
///
/// Rule: a tick "wins" if its mid differs from the prior latest by more
/// than 1e-12, OR if it's the very first tick for this symbol.
fn apply_tick(
    latest: &DashMap<Symbol, PriceTick>,
    history: &DashMap<Symbol, Mutex<VecDeque<(f64, f64)>>>,
    tick_wins: &DashMap<(Symbol, FeedSource), u64>,
    tick_tx: &tokio::sync::broadcast::Sender<PriceTick>,
    tick: PriceTick,
) {
    let symbol = tick.symbol;
    let prev = latest.insert(symbol, tick);
    let counted_as_win = match prev {
        Some(p) => (tick.mid - p.mid).abs() > 1e-12,
        None => true, // first tick for this symbol
    };
    if counted_as_win {
        *tick_wins.entry((symbol, tick.source)).or_insert(0) += 1;
        let ts_s = (tick.ts_local_ns as f64) / 1_000_000_000.0;
        let entry = history
            .entry(symbol)
            .or_insert_with(|| Mutex::new(VecDeque::with_capacity(HISTORY_CAPACITY)));
        let mut buf = entry.value().lock();
        buf.push_back((ts_s, tick.mid));
        while buf.len() > HISTORY_CAPACITY {
            buf.pop_front();
        }
        drop(buf);
        // Broadcast the tick. Ignore failure (no subscribers yet is fine).
        let _ = tick_tx.send(tick);
    }
    debug!(
        sym = %symbol,
        src = tick.source.as_str(),
        mid = tick.mid,
        ts_ns = tick.ts_local_ns,
        counted = counted_as_win,
        "SpotPulse: tick applied"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn apply_tick_counts_only_price_changes() {
        let latest = DashMap::new();
        let history = DashMap::new();
        let wins = DashMap::new();
        let (tx, _rx) = tokio::sync::broadcast::channel(8);
        apply_tick(&latest, &history, &wins, &tx, PriceTick {
            symbol: Symbol::BTC, source: FeedSource::Pyth, mid: 79500.0, ts_local_ns: 1_000_000_000,
        });
        apply_tick(&latest, &history, &wins, &tx, PriceTick {
            symbol: Symbol::BTC, source: FeedSource::Coinbase, mid: 79501.0, ts_local_ns: 1_500_000_000,
        });
        apply_tick(&latest, &history, &wins, &tx, PriceTick {
            symbol: Symbol::BTC, source: FeedSource::Binance, mid: 79501.0, ts_local_ns: 2_000_000_000,
        });
        apply_tick(&latest, &history, &wins, &tx, PriceTick {
            symbol: Symbol::BTC, source: FeedSource::Binance, mid: 79502.0, ts_local_ns: 3_000_000_000,
        });
        assert_eq!(*wins.get(&(Symbol::BTC, FeedSource::Pyth)).unwrap().value(), 1);
        assert_eq!(*wins.get(&(Symbol::BTC, FeedSource::Coinbase)).unwrap().value(), 1);
        assert_eq!(*wins.get(&(Symbol::BTC, FeedSource::Binance)).unwrap().value(), 1);
        assert_eq!(latest.get(&Symbol::BTC).unwrap().mid, 79502.0);
        assert_eq!(history.get(&Symbol::BTC).unwrap().value().lock().len(), 3);
    }

    #[test]
    fn history_ring_bounds_at_capacity() {
        let latest = DashMap::new();
        let history = DashMap::new();
        let wins = DashMap::new();
        let (tx, _rx) = tokio::sync::broadcast::channel(2048);
        for i in 0..(HISTORY_CAPACITY + 50) {
            apply_tick(&latest, &history, &wins, &tx, PriceTick {
                symbol: Symbol::ETH,
                source: FeedSource::Pyth,
                mid: 3000.0 + (i as f64),
                ts_local_ns: (i as u64 + 1) * 1_000_000,
            });
        }
        let len = history.get(&Symbol::ETH).unwrap().value().lock().len();
        assert_eq!(len, HISTORY_CAPACITY, "history must be bounded at HISTORY_CAPACITY");
    }
}
