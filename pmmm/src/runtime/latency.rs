//! Phase 5: TSC-based latency profiling.
//!
//! Uses `quanta::Clock` for ~10ns-resolution timing, accumulates per-span
//! histograms in atomics, and dumps summary stats on demand.
//!
//! Span IDs are static enum variants so we can index a fixed-size array
//! without locks. Each histogram is a power-of-two bucketed counter.

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

use once_cell::sync::Lazy;
use quanta::Clock;

/// Named spans we track. Add new variants as we instrument more code.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Span {
    /// SpotPulse: apply_tick (lock-free DashMap insert + tick-win attribution)
    SpotApplyTick = 0,
    /// strategy::theo::theo_p_up_at — full ensemble eval
    TheoCompute = 1,
    /// quoter::decide — V7Quoter::decide() call
    QuoterDecide = 2,
    /// signer::sign_order — EIP-712 hash + ECDSA
    OrderSign = 3,
    /// clob::post_order — full HTTP POST round-trip
    ClobPost = 4,
    /// clob::cancel_orders round-trip
    ClobCancel = 5,
    /// runtime tick (decide + execute_intents for all markets)
    RuntimeTick = 6,
}

impl Span {
    pub fn as_str(&self) -> &'static str {
        match self {
            Span::SpotApplyTick => "spot_apply_tick",
            Span::TheoCompute => "theo_compute",
            Span::QuoterDecide => "quoter_decide",
            Span::OrderSign => "order_sign",
            Span::ClobPost => "clob_post",
            Span::ClobCancel => "clob_cancel",
            Span::RuntimeTick => "runtime_tick",
        }
    }

    pub fn all() -> &'static [Span] {
        &[
            Span::SpotApplyTick,
            Span::TheoCompute,
            Span::QuoterDecide,
            Span::OrderSign,
            Span::ClobPost,
            Span::ClobCancel,
            Span::RuntimeTick,
        ]
    }
}

const N_SPANS: usize = 7;
/// Histogram buckets: 0..=12 → [0, 1us, 4us, 16us, 64us, 256us, 1ms, 4ms,
/// 16ms, 64ms, 256ms, 1s, >1s]. Bucket i contains samples in [4^(i-1), 4^i) us.
const N_BUCKETS: usize = 13;

static CLOCK: Lazy<Clock> = Lazy::new(Clock::new);

/// Atomic histogram per span.
struct SpanHist {
    buckets: [AtomicU64; N_BUCKETS],
    count: AtomicU64,
    sum_ns: AtomicU64,
    max_ns: AtomicU64,
}

impl SpanHist {
    const fn new() -> Self {
        const Z: AtomicU64 = AtomicU64::new(0);
        Self {
            buckets: [Z, Z, Z, Z, Z, Z, Z, Z, Z, Z, Z, Z, Z],
            count: AtomicU64::new(0),
            sum_ns: AtomicU64::new(0),
            max_ns: AtomicU64::new(0),
        }
    }

    fn record(&self, ns: u64) {
        let us = ns / 1000;
        // bucket = ceil(log4(us+1))
        let bucket = if us == 0 {
            0
        } else {
            let mut b = 0usize;
            let mut x = us;
            while x > 0 && b < N_BUCKETS - 1 {
                x >>= 2;
                b += 1;
            }
            b
        };
        self.buckets[bucket].fetch_add(1, Ordering::Relaxed);
        self.count.fetch_add(1, Ordering::Relaxed);
        self.sum_ns.fetch_add(ns, Ordering::Relaxed);
        let mut cur_max = self.max_ns.load(Ordering::Relaxed);
        while ns > cur_max {
            match self.max_ns.compare_exchange_weak(
                cur_max,
                ns,
                Ordering::Relaxed,
                Ordering::Relaxed,
            ) {
                Ok(_) => break,
                Err(prev) => cur_max = prev,
            }
        }
    }

    fn snapshot(&self) -> SpanSnapshot {
        let count = self.count.load(Ordering::Relaxed);
        let sum_ns = self.sum_ns.load(Ordering::Relaxed);
        let max_ns = self.max_ns.load(Ordering::Relaxed);
        let buckets: Vec<u64> = self.buckets.iter().map(|b| b.load(Ordering::Relaxed)).collect();
        SpanSnapshot {
            count,
            sum_ns,
            max_ns,
            buckets,
        }
    }
}

static SPAN_HISTOGRAMS: [SpanHist; N_SPANS] = [
    SpanHist::new(),
    SpanHist::new(),
    SpanHist::new(),
    SpanHist::new(),
    SpanHist::new(),
    SpanHist::new(),
    SpanHist::new(),
];

/// RAII guard: starts a TSC timer on construction, records on Drop.
pub struct LatencyGuard {
    span: Span,
    start_ns: u64,
}

impl LatencyGuard {
    pub fn start(span: Span) -> Self {
        Self {
            span,
            start_ns: CLOCK.raw(),
        }
    }
}

impl Drop for LatencyGuard {
    fn drop(&mut self) {
        let end = CLOCK.raw();
        let elapsed = end.saturating_sub(self.start_ns);
        SPAN_HISTOGRAMS[self.span as usize].record(elapsed);
    }
}

/// Convenience macro for ergonomic span instrumentation:
/// `let _g = latency_span!(Span::ClobPost);`
#[macro_export]
macro_rules! latency_span {
    ($span:expr) => {
        $crate::runtime::latency::LatencyGuard::start($span)
    };
}

#[derive(Debug, Clone)]
pub struct SpanSnapshot {
    pub count: u64,
    pub sum_ns: u64,
    pub max_ns: u64,
    pub buckets: Vec<u64>,
}

impl SpanSnapshot {
    pub fn mean_us(&self) -> f64 {
        if self.count == 0 {
            0.0
        } else {
            (self.sum_ns as f64) / (self.count as f64) / 1000.0
        }
    }
    pub fn max_us(&self) -> f64 {
        self.max_ns as f64 / 1000.0
    }
}

/// Snapshot all spans as a JSON-serializable map. Used by the runtime
/// heartbeat to emit periodic latency stats.
pub fn snapshot_all() -> Vec<(Span, SpanSnapshot)> {
    Span::all()
        .iter()
        .map(|s| (*s, SPAN_HISTOGRAMS[*s as usize].snapshot()))
        .collect()
}

/// Reset all histograms (for testing or interval-based reporting).
pub fn reset_all() {
    for h in SPAN_HISTOGRAMS.iter() {
        h.count.store(0, Ordering::Relaxed);
        h.sum_ns.store(0, Ordering::Relaxed);
        h.max_ns.store(0, Ordering::Relaxed);
        for b in h.buckets.iter() {
            b.store(0, Ordering::Relaxed);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread::sleep;

    #[test]
    fn span_records_and_summarizes() {
        reset_all();
        {
            let _g = LatencyGuard::start(Span::TheoCompute);
            sleep(Duration::from_millis(2));
        }
        let snaps = snapshot_all();
        let (_, theo) = snaps.iter().find(|(s, _)| *s == Span::TheoCompute).unwrap();
        assert_eq!(theo.count, 1);
        assert!(theo.mean_us() >= 1500.0, "expected ~2ms, got {}us", theo.mean_us());
    }

    #[test]
    fn multiple_spans_independent() {
        reset_all();
        for _ in 0..10 {
            let _g = LatencyGuard::start(Span::QuoterDecide);
        }
        let snaps = snapshot_all();
        let (_, decide) = snaps.iter().find(|(s, _)| *s == Span::QuoterDecide).unwrap();
        let (_, sign) = snaps.iter().find(|(s, _)| *s == Span::OrderSign).unwrap();
        assert_eq!(decide.count, 10);
        assert_eq!(sign.count, 0);
    }
}
