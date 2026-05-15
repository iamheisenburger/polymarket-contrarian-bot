//! Process-level configuration: env-var loading + CLI overrides.
//!
//! Mirrors the env vars the Python bot already uses (POLY_PRIVATE_KEY,
//! POLY_SAFE_ADDRESS, POLY_BUILDER_*), so we can drop into the same wallet
//! without re-deriving credentials.

use std::env;

use alloy_primitives::Address;
use anyhow::{Context, Result};

use crate::signer::{BuilderCreds, L2Creds, OrderSigner};

/// All bot credentials and addresses loaded from the environment.
pub struct EnvCreds {
    pub signer: OrderSigner,
    pub signer_address: Address,
    /// Safe (funder) address — what holds USDC and where orders are
    /// attributed for signature_type=2.
    pub funder_address: Address,
    /// Builder Program creds (optional; if absent we still trade but lose
    /// rebate attribution).
    pub builder: Option<BuilderCreds>,
}

impl EnvCreds {
    /// Load from env. Required:
    ///   POLY_PRIVATE_KEY        — EOA signing key (with/without 0x)
    ///   POLY_SAFE_ADDRESS       — Gnosis Safe / funder address
    /// Optional:
    ///   POLY_BUILDER_API_KEY / _SECRET / _PASSPHRASE
    pub fn from_env() -> Result<Self> {
        let pk = env::var("POLY_PRIVATE_KEY")
            .with_context(|| "POLY_PRIVATE_KEY env var")?;
        let signer = OrderSigner::from_hex(&pk)?;
        let signer_address = signer.address();

        let safe = env::var("POLY_SAFE_ADDRESS")
            .with_context(|| "POLY_SAFE_ADDRESS env var")?;
        let funder_address: Address = safe
            .parse()
            .with_context(|| "parse POLY_SAFE_ADDRESS as Address")?;

        let builder = match (
            env::var("POLY_BUILDER_API_KEY").ok(),
            env::var("POLY_BUILDER_API_SECRET").ok(),
            env::var("POLY_BUILDER_API_PASSPHRASE").ok(),
        ) {
            (Some(api_key), Some(secret), Some(passphrase)) => Some(BuilderCreds {
                api_key,
                secret,
                passphrase,
            }),
            _ => None,
        };

        Ok(Self {
            signer,
            signer_address,
            funder_address,
            builder,
        })
    }
}

/// L2 credentials — derived once at startup, then cached on disk so the
/// Python bot and Rust bot can share them without re-deriving.
///
/// For Phase 3/4 we read these from a JSON file at
/// `data/.l2_creds.json` (write-only by the Python bot). Later phases will
/// implement native EIP-712 ClobAuth signing to derive them ourselves.
pub fn load_l2_creds(path: &str) -> Result<L2Creds> {
    let raw = std::fs::read_to_string(path).with_context(|| format!("read {}", path))?;
    let v: serde_json::Value = serde_json::from_str(&raw).with_context(|| "parse l2 creds json")?;
    Ok(L2Creds {
        api_key: v.get("api_key").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        secret: v.get("secret").and_then(|x| x.as_str()).unwrap_or("").to_string(),
        passphrase: v.get("passphrase").and_then(|x| x.as_str()).unwrap_or("").to_string(),
    })
}
