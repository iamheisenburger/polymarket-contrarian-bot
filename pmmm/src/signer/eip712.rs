//! EIP-712 Polymarket Order signing.
//!
//! Direct port of `src/signer.py:OrderSigner.sign_order`. Uses alloy's
//! `sol!` macro to generate the canonical EIP-712 hash. Produces a 65-byte
//! signature (r || s || v) hex-encoded with `0x` prefix — byte-identical
//! to what `eth_account.sign_message(encode_typed_data(...))` produces in
//! the Python reference.
//!
//! Domain (matches Python `OrderSigner.DOMAIN`):
//!   name="ClobAuthDomain", version="1", chainId=137 (Polygon mainnet)
//!
//! Order struct (matches Python `OrderSigner.ORDER_TYPES`):
//!   salt, maker, signer, taker, tokenId, makerAmount, takerAmount,
//!   expiration, nonce, feeRateBps, side (uint8), signatureType (uint8)

use alloy_primitives::Address;
use alloy_signer::SignerSync;
use alloy_signer_local::PrivateKeySigner;
use alloy_sol_types::{eip712_domain, sol, SolStruct};
use anyhow::{Context, Result};
use serde::{Deserialize, Serialize};

use crate::types::Order as DomainOrder;

// EIP-712 typed-data definition — matches Polymarket CTF Exchange v2.
// Type string (per py-clob-client-v2 `exchange_order_builder_v2.py`):
// "Order(uint256 salt,address maker,address signer,uint256 tokenId,
//  uint256 makerAmount,uint256 takerAmount,uint8 side,uint8 signatureType,
//  uint256 timestamp,bytes32 metadata,bytes32 builder)"
//
// `expiration` is in the wire body but NOT in the EIP-712 hash (it's a
// server-side TTL hint only).
sol! {
    #[derive(Debug)]
    struct Order {
        uint256 salt;
        address maker;
        address signer;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint8 side;
        uint8 signatureType;
        uint256 timestamp;
        bytes32 metadata;
        bytes32 builder;
    }
}

/// Polymarket Exchange contract used for pUSD-based trading.
/// This is the **new** Exchange (deployed when PM migrated to pUSD wallet
/// abstraction); the legacy USDC.e-based exchange at `0x4bFb41…8982E` is
/// no longer used for current order placement and signing against it
/// returns `order_version_mismatch`. Confirmed by inspecting on-chain
/// settlement tx for a recent fill from this wallet.
pub const POLYGON_CTF_EXCHANGE: &str = "0xe111180000d2663c0091e4f400237545b87b996b";

/// EIP-712 domain for **order signing** — read live from the on-chain
/// contract's `eip712Domain()` (EIP-5267) on 2026-05-14:
///   name              = "Polymarket CTF Exchange"
///   version           = "2"   (Polymarket bumped from v1 in mid-May 2026)
///   chainId           = 137
///   verifyingContract = 0xe111180000d2663c0091e4f400237545b87b996b
/// The old v1 domain at 0x4bFb41…8982E now returns `order_version_mismatch`
/// (both py-clob-client v0.34.6 and TS clob-client are stale on this).
fn clob_domain() -> alloy_sol_types::Eip712Domain {
    let contract: Address = POLYGON_CTF_EXCHANGE.parse().expect("hardcoded ctf address");
    eip712_domain! {
        name: "Polymarket CTF Exchange",
        version: "2",
        chain_id: 137,
        verifying_contract: contract,
    }
}

/// Wraps a local EOA private key for signing CLOB orders.
#[derive(Clone)]
pub struct OrderSigner {
    signer: PrivateKeySigner,
}

impl std::fmt::Debug for OrderSigner {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("OrderSigner")
            .field("address", &self.address())
            .finish()
    }
}

impl OrderSigner {
    /// Build from a hex private key (with or without `0x` prefix). Same
    /// semantics as the Python `OrderSigner.__init__`.
    pub fn from_hex(private_key: &str) -> Result<Self> {
        let trimmed = private_key.trim_start_matches("0x");
        let signer: PrivateKeySigner = trimmed
            .parse()
            .with_context(|| "parse private key as PrivateKeySigner")?;
        Ok(Self { signer })
    }

    /// The EOA's address (this is what goes in the `signer` field of an Order
    /// when using signature_type=2 / Gnosis Safe).
    pub fn address(&self) -> Address {
        self.signer.address()
    }

    /// Sign one Order. Returns the wire-ready order body with `signature`
    /// embedded inside, matching Python `order_to_json` byte-for-byte. The
    /// caller wraps this with `owner` (L2 API key UUID), `orderType`, and
    /// `postOnly` to form the full POST body.
    pub fn sign_order(&self, order: &DomainOrder, api_key: &str) -> Result<SignedOrderResponse> {
        let typed = Order {
            salt: order.salt,
            maker: order.maker,
            signer: self.signer.address(),
            tokenId: order.token_id,
            makerAmount: order.maker_amount,
            takerAmount: order.taker_amount,
            side: order.side,
            signatureType: order.signature_type,
            timestamp: order.timestamp,
            metadata: order.metadata,
            builder: order.builder,
        };
        let domain = clob_domain();
        let digest = typed.eip712_signing_hash(&domain);
        let signature = self
            .signer
            .sign_hash_sync(&digest)
            .with_context(|| "sign EIP-712 digest")?;
        let sig_hex = format!("0x{}", hex::encode(signature.as_bytes()));

        Ok(SignedOrderResponse {
            order: OrderWirePayload::from_order(order, self.signer.address(), sig_hex),
            owner: api_key.to_string(),
        })
    }

    /// Compute the EIP-712 digest for an Order WITHOUT signing it.
    /// Used by the pre-signed order pool to validate cached payloads.
    pub fn order_digest(&self, order: &DomainOrder) -> [u8; 32] {
        let typed = Order {
            salt: order.salt,
            maker: order.maker,
            signer: self.signer.address(),
            tokenId: order.token_id,
            makerAmount: order.maker_amount,
            takerAmount: order.taker_amount,
            side: order.side,
            signatureType: order.signature_type,
            timestamp: order.timestamp,
            metadata: order.metadata,
            builder: order.builder,
        };
        let domain = clob_domain();
        typed.eip712_signing_hash(&domain).into()
    }
}

/// JSON-serializable Order body for the CLOB v2 `POST /order` request.
/// Mirrors the wire shape from py-clob-client-v2 `order_to_json_v2`:
/// `{salt:int, maker, signer, tokenId, makerAmount, takerAmount, side,
///   expiration, signatureType, timestamp, metadata, builder, signature}`.
/// `expiration` is wire-only (not in the EIP-712 hash). Addresses are
/// EIP-55 checksum-encoded.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderWirePayload {
    pub salt: u64,
    pub maker: String,
    pub signer: String,
    #[serde(rename = "tokenId")]
    pub token_id: String,
    #[serde(rename = "makerAmount")]
    pub maker_amount: String,
    #[serde(rename = "takerAmount")]
    pub taker_amount: String,
    pub side: String,
    pub expiration: String,
    #[serde(rename = "signatureType")]
    pub signature_type: u8,
    /// Order creation timestamp in milliseconds (wire as string).
    pub timestamp: String,
    /// bytes32 hex string (e.g. "0x000…0").
    pub metadata: String,
    /// bytes32 hex string (e.g. "0x000…0").
    pub builder: String,
    /// Hex-encoded EIP-712 signature with `0x` prefix.
    pub signature: String,
}

impl OrderWirePayload {
    pub fn from_order(o: &DomainOrder, signer_addr: Address, signature: String) -> Self {
        let salt: u64 = o.salt.try_into().unwrap_or(u64::MAX);
        Self {
            salt,
            // EIP-55 checksum case — PM validates this server-side.
            maker: o.maker.to_checksum(None),
            signer: signer_addr.to_checksum(None),
            token_id: o.token_id.to_string(),
            maker_amount: o.maker_amount.to_string(),
            taker_amount: o.taker_amount.to_string(),
            side: match o.side {
                0 => "BUY".to_string(),
                1 => "SELL".to_string(),
                n => format!("UNKNOWN_{n}"),
            },
            expiration: o.expiration.to_string(),
            signature_type: o.signature_type,
            timestamp: o.timestamp.to_string(),
            metadata: format!("{:#x}", o.metadata),
            builder: format!("{:#x}", o.builder),
            signature,
        }
    }
}

/// Response from sign_order — `owner` is the L2 API key UUID (NOT a wallet
/// address). The outer post body is `{order: OrderWirePayload, owner: String,
/// orderType: "GTC", postOnly: false}`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SignedOrderResponse {
    pub order: OrderWirePayload,
    pub owner: String,
}

#[cfg(test)]
mod tests {
    use super::*;
    use alloy_primitives::{address, U256};

    /// Deterministic test private key (NOT used for real funds).
    const TEST_KEY: &str = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80";

    #[test]
    fn signer_from_hex_works_with_or_without_0x() {
        let with = OrderSigner::from_hex(TEST_KEY).unwrap();
        let without = OrderSigner::from_hex(&TEST_KEY[2..]).unwrap();
        assert_eq!(with.address(), without.address());
        // anvil account 0:
        assert_eq!(
            with.address(),
            address!("f39Fd6e51aad88F6F4ce6aB8827279cffFb92266")
        );
    }

    fn make_v2_order(salt: u64, side: u8) -> DomainOrder {
        DomainOrder {
            salt: U256::from(salt),
            maker: Address::ZERO,
            signer: Address::ZERO,
            token_id: U256::from(123456789u64),
            maker_amount: U256::from(3_000_000u64),
            taker_amount: U256::from(5_000_000u64),
            side,
            signature_type: 2,
            timestamp: U256::from(1_700_000_000_000u64),
            metadata: alloy_primitives::B256::ZERO,
            builder: alloy_primitives::B256::ZERO,
            expiration: U256::ZERO,
        }
    }

    #[test]
    fn sign_order_produces_65_byte_signature() {
        let signer = OrderSigner::from_hex(TEST_KEY).unwrap();
        let order = make_v2_order(0, 0);
        let signed = signer.sign_order(&order, "TEST_API_KEY").unwrap();
        assert!(signed.order.signature.starts_with("0x"));
        assert_eq!(signed.order.signature.len(), 2 + 65 * 2);
        assert_eq!(signed.order.signature_type, 2);
        assert_eq!(signed.order.side, "BUY");
        assert_eq!(signed.order.salt, 0);
        assert_eq!(signed.order.timestamp, "1700000000000");
        assert_eq!(signed.owner, "TEST_API_KEY");
    }

    #[test]
    fn sign_order_is_deterministic_for_same_inputs() {
        let signer = OrderSigner::from_hex(TEST_KEY).unwrap();
        let order = make_v2_order(42, 1);
        let s1 = signer.sign_order(&order, "K").unwrap();
        let s2 = signer.sign_order(&order, "K").unwrap();
        assert_eq!(s1.order.signature, s2.order.signature);
    }

    #[test]
    fn order_digest_changes_with_salt() {
        let signer = OrderSigner::from_hex(TEST_KEY).unwrap();
        let mut order = DomainOrder {
            salt: U256::ZERO,
            maker: signer.address(),
            signer: signer.address(),
            token_id: U256::from(1u64),
            maker_amount: U256::from(1_000_000u64),
            taker_amount: U256::from(2_000_000u64),
            side: 0,
            signature_type: 2,
            timestamp: U256::from(1_700_000_000_000u64),
            metadata: alloy_primitives::B256::ZERO,
            builder: alloy_primitives::B256::ZERO,
            expiration: U256::ZERO,
        };
        let d0 = signer.order_digest(&order);
        order.salt = U256::from(1u64);
        let d1 = signer.order_digest(&order);
        assert_ne!(d0, d1);
    }
}
