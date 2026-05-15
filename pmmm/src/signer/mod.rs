//! Signing module — EIP-712 order signer, HMAC L2/Builder auth, and the
//! pre-signed order pool (Phase 4).

pub mod eip712;
pub mod hmac;
pub mod pool;

pub use eip712::{OrderSigner, OrderWirePayload, SignedOrderResponse};
pub use hmac::{builder_headers, hmac_sign, l2_headers, BuilderCreds, L2Creds};
pub use pool::{PoolEntry, PoolKey, PoolStats, PreSignedPool};
