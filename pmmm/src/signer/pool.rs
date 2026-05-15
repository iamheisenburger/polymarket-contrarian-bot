//! Pre-signed order pool.
//!
//! Maintains a queue of pre-signed `SignedOrderResponse`s for each (token_id,
//! side, price_bucket) tuple. When the strategy decides to place at a price
//! we already have a pre-signed order for, we skip the ~300us EIP-712 sign
//! and just POST the cached payload.
//!
//! A background refill task keeps each bucket at `target_depth`, signing
//! new orders ahead of time. On book moves, stale buckets (outside the
//! current ±N tick window) are evicted to bound memory.
//!
//! Empirical key: PM accepts `salt=0` + same-nonce for distinct orders as
//! long as the order *hashes* differ — they do, because each order has a
//! unique (token_id, price, size, nonce) tuple. The refill task uses
//! `nonce = unix_seconds + sequence_counter` to guarantee uniqueness.

use std::collections::VecDeque;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use alloy_primitives::{Address, U256};
use anyhow::Result;
use dashmap::DashMap;
use parking_lot::Mutex;
use tracing::{debug, warn};

use crate::signer::eip712::{OrderSigner, SignedOrderResponse};
use crate::state::Wei6;
use crate::types::{Order, OrderSide};

/// One pre-signed bucket key.
#[derive(Debug, Clone, PartialEq, Eq, Hash)]
pub struct PoolKey {
    pub token_id: String,
    pub price_cents: u32, // price * 100 (e.g. 0.65 → 65)
    pub size_tokens: u32, // size in whole tokens (e.g. 5)
    pub order_side: OrderSide,
}

#[derive(Debug, Clone)]
pub struct PoolEntry {
    pub signed: SignedOrderResponse,
    pub signed_ts_ns: u64,
}

pub struct PreSignedPool {
    /// Per-bucket queue of pre-signed orders.
    buckets: Arc<DashMap<PoolKey, Mutex<VecDeque<PoolEntry>>>>,
    /// Target depth per bucket — background refill keeps it ≥ this.
    target_depth: usize,
    /// Stats.
    pub n_signed: Arc<AtomicU64>,
    pub n_consumed: Arc<AtomicU64>,
    pub n_evicted: Arc<AtomicU64>,
    /// Used to ensure nonce uniqueness across same-second signs.
    nonce_seq: Arc<AtomicU64>,
}

impl PreSignedPool {
    pub fn new(target_depth: usize) -> Self {
        Self {
            buckets: Arc::new(DashMap::new()),
            target_depth,
            n_signed: Arc::new(AtomicU64::new(0)),
            n_consumed: Arc::new(AtomicU64::new(0)),
            n_evicted: Arc::new(AtomicU64::new(0)),
            nonce_seq: Arc::new(AtomicU64::new(0)),
        }
    }

    /// Try to consume one pre-signed order from a bucket. Returns None if
    /// the bucket is empty (caller falls back to sign-on-demand).
    pub fn take(&self, key: &PoolKey) -> Option<PoolEntry> {
        let entry = self.buckets.get(key)?;
        let mut queue = entry.value().lock();
        let popped = queue.pop_front()?;
        self.n_consumed.fetch_add(1, Ordering::Relaxed);
        Some(popped)
    }

    /// Refill one bucket up to `target_depth` by signing new orders.
    pub fn refill_bucket(
        &self,
        key: &PoolKey,
        signer: &OrderSigner,
        funder: Address,
        api_key: &str,
    ) -> Result<usize> {
        let entry = self
            .buckets
            .entry(key.clone())
            .or_insert_with(|| Mutex::new(VecDeque::with_capacity(self.target_depth)));
        let mut queue = entry.value().lock();
        let needed = self.target_depth.saturating_sub(queue.len());
        if needed == 0 {
            return Ok(0);
        }

        let token_id_u256 = U256::from_str_radix(&key.token_id, 10)
            .map_err(|e| anyhow::anyhow!("parse token_id: {e}"))?;
        let price = key.price_cents as f64 / 100.0;
        let size = key.size_tokens as f64;
        let maker_amount = Wei6::from_usd(price * size).0;
        let taker_amount = Wei6::from_token(size).0;

        let base_unix = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)?
            .as_secs();

        for _ in 0..needed {
            let seq = self.nonce_seq.fetch_add(1, Ordering::Relaxed);
            // v2: salt+timestamp give uniqueness; nonce is gone from the struct.
            let timestamp_ms = base_unix.saturating_mul(1000) + seq;
            let order = Order {
                salt: U256::from(seq),
                maker: funder,
                signer: signer.address(),
                token_id: token_id_u256,
                maker_amount,
                taker_amount,
                side: key.order_side.as_u8(),
                signature_type: 2,
                timestamp: U256::from(timestamp_ms),
                metadata: alloy_primitives::B256::ZERO,
                builder: alloy_primitives::B256::ZERO,
                expiration: U256::ZERO,
            };
            let signed = signer.sign_order(&order, api_key)?;
            queue.push_back(PoolEntry {
                signed,
                signed_ts_ns: crate::types::now_mono_ns(),
            });
            self.n_signed.fetch_add(1, Ordering::Relaxed);
        }
        Ok(needed)
    }

    /// Evict buckets whose prices are outside the [center - window, center + window]
    /// cent range for this token. Returns the number of buckets pruned.
    pub fn evict_outside_window(
        &self,
        token_id: &str,
        center_cents: u32,
        window_cents: u32,
    ) -> usize {
        let lo = center_cents.saturating_sub(window_cents);
        let hi = center_cents.saturating_add(window_cents);
        let mut to_remove: Vec<PoolKey> = Vec::new();
        for entry in self.buckets.iter() {
            let k = entry.key();
            if k.token_id == token_id && (k.price_cents < lo || k.price_cents > hi) {
                to_remove.push(k.clone());
            }
        }
        let n = to_remove.len();
        for k in to_remove {
            if let Some(removed) = self.buckets.remove(&k) {
                let q = removed.1.lock();
                self.n_evicted
                    .fetch_add(q.len() as u64, Ordering::Relaxed);
            }
        }
        n
    }

    /// Current depth of one bucket (for monitoring).
    pub fn depth(&self, key: &PoolKey) -> usize {
        self.buckets
            .get(key)
            .map(|e| e.value().lock().len())
            .unwrap_or(0)
    }

    pub fn stats(&self) -> PoolStats {
        PoolStats {
            n_buckets: self.buckets.len(),
            n_signed: self.n_signed.load(Ordering::Relaxed),
            n_consumed: self.n_consumed.load(Ordering::Relaxed),
            n_evicted: self.n_evicted.load(Ordering::Relaxed),
        }
    }
}

#[derive(Debug, Clone)]
pub struct PoolStats {
    pub n_buckets: usize,
    pub n_signed: u64,
    pub n_consumed: u64,
    pub n_evicted: u64,
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::signer::OrderSigner;
    use alloy_primitives::address;

    const TEST_KEY: &str = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";

    fn signer() -> OrderSigner {
        OrderSigner::from_hex(TEST_KEY).unwrap()
    }

    fn funder() -> Address {
        address!("f39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
    }

    fn key(price_cents: u32) -> PoolKey {
        PoolKey {
            token_id: "12345678901234567890".to_string(),
            price_cents,
            size_tokens: 5,
            order_side: OrderSide::Buy,
        }
    }

    #[test]
    fn refill_and_take() {
        let pool = PreSignedPool::new(3);
        let s = signer();
        let n = pool.refill_bucket(&key(75), &s, funder(), "K").unwrap();
        assert_eq!(n, 3);
        assert_eq!(pool.depth(&key(75)), 3);
        let e1 = pool.take(&key(75));
        assert!(e1.is_some());
        assert_eq!(pool.depth(&key(75)), 2);
        let e2 = pool.take(&key(75));
        let e3 = pool.take(&key(75));
        let e4 = pool.take(&key(75));
        assert!(e2.is_some());
        assert!(e3.is_some());
        assert!(e4.is_none());
        let stats = pool.stats();
        assert_eq!(stats.n_signed, 3);
        assert_eq!(stats.n_consumed, 3);
    }

    #[test]
    fn nonces_are_unique_within_bucket() {
        let pool = PreSignedPool::new(5);
        let s = signer();
        pool.refill_bucket(&key(75), &s, funder(), "K").unwrap();
        // v2 schema: nonce is gone; uniqueness comes from salt + timestamp.
        let mut keys = std::collections::HashSet::new();
        for _ in 0..5 {
            let e = pool.take(&key(75)).unwrap();
            keys.insert((e.signed.order.salt, e.signed.order.timestamp.clone()));
        }
        assert_eq!(keys.len(), 5, "(salt,timestamp) must be unique");
    }

    #[test]
    fn evict_outside_window() {
        let pool = PreSignedPool::new(1);
        let s = signer();
        for price in [65u32, 70, 75, 80, 85] {
            pool.refill_bucket(&key(price), &s, funder(), "K").unwrap();
        }
        assert_eq!(pool.stats().n_buckets, 5);
        // Center 75, window ±5 keeps [70, 75, 80]; evicts 65 and 85.
        let n = pool.evict_outside_window("12345678901234567890", 75, 5);
        assert_eq!(n, 2);
        assert_eq!(pool.stats().n_buckets, 3);
    }

    #[test]
    fn refill_is_idempotent_at_target_depth() {
        let pool = PreSignedPool::new(3);
        let s = signer();
        let n1 = pool.refill_bucket(&key(75), &s, funder(), "K").unwrap();
        let n2 = pool.refill_bucket(&key(75), &s, funder(), "K").unwrap();
        assert_eq!(n1, 3);
        assert_eq!(n2, 0);
        assert_eq!(pool.depth(&key(75)), 3);
    }
}
