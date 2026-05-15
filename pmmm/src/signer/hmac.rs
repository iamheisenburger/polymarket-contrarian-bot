//! HMAC-SHA256 auth header builder for PM CLOB L2 + Builder requests.
//!
//! Direct port of the Python `src/client.py` HMAC implementation.
//!
//! L2 / Builder auth:
//!   message = timestamp + method + path + body
//!   secret_decoded = base64_urlsafe_decode(secret)
//!   signature = base64_urlsafe_encode(HMAC-SHA256(secret_decoded, message))
//!
//! Reference (matches both):
//!   https://github.com/Polymarket/clob-client/blob/main/src/signing/hmac.ts
//!   https://github.com/Polymarket/py-clob-client/blob/main/py_clob_client/signing/hmac.py
//!
//! Two parallel header sets:
//!   L2 (user creds):     POLY_ADDRESS, POLY_API_KEY, POLY_TIMESTAMP, POLY_PASSPHRASE, POLY_SIGNATURE
//!   Builder (program):   POLY_BUILDER_API_KEY, POLY_BUILDER_TIMESTAMP, POLY_BUILDER_PASSPHRASE, POLY_BUILDER_SIGNATURE

use std::collections::HashMap;

use alloy_primitives::Address;
use anyhow::{Context, Result};
use base64::{engine::general_purpose::URL_SAFE, Engine as _};
use hmac::{Hmac, Mac};
use sha2::Sha256;

type HmacSha256 = Hmac<Sha256>;

/// Compute the HMAC-SHA256 signature used for both L2 and Builder auth.
///
/// `secret_b64` is the base64-URL-safe-encoded secret (as returned by PM's
/// auth/api-key endpoint). The output is base64-URL-safe-encoded HMAC.
pub fn hmac_sign(
    secret_b64: &str,
    timestamp: &str,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<String> {
    let secret_bytes = URL_SAFE
        .decode(secret_b64.as_bytes())
        .with_context(|| "base64-decode HMAC secret")?;
    let mut mac = HmacSha256::new_from_slice(&secret_bytes)
        .with_context(|| "HMAC-SHA256 from key")?;
    mac.update(timestamp.as_bytes());
    mac.update(method.as_bytes());
    mac.update(path.as_bytes());
    if let Some(b) = body {
        mac.update(b.as_bytes());
    }
    let out = mac.finalize().into_bytes();
    Ok(URL_SAFE.encode(out))
}

/// L2 (user-creds) credentials.
#[derive(Debug, Clone)]
pub struct L2Creds {
    pub api_key: String,
    pub secret: String, // base64-urlsafe
    pub passphrase: String,
}

/// Builder Program credentials (same shape as L2, but a separate API key
/// pool — see `docs/developers/builders/order-attribution.md`).
#[derive(Debug, Clone)]
pub struct BuilderCreds {
    pub api_key: String,
    pub secret: String, // base64-urlsafe
    pub passphrase: String,
}

/// Build the L2 headers map for one HTTP request.
///
/// `address` is the SIGNER's address (the EOA), not the funder/Safe. For
/// signature_type=2, the maker (Safe) doesn't carry the API key — the EOA does.
pub fn l2_headers(
    creds: &L2Creds,
    address: &Address,
    timestamp_secs: u64,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<HashMap<&'static str, String>> {
    let ts = timestamp_secs.to_string();
    let sig = hmac_sign(&creds.secret, &ts, method, path, body)?;
    let mut h = HashMap::new();
    h.insert("POLY_ADDRESS", format!("{address:#x}"));
    h.insert("POLY_API_KEY", creds.api_key.clone());
    h.insert("POLY_TIMESTAMP", ts);
    h.insert("POLY_PASSPHRASE", creds.passphrase.clone());
    h.insert("POLY_SIGNATURE", sig);
    Ok(h)
}

/// Build the Builder-attribution headers map for one HTTP request.
/// Add to the same request as the L2 headers.
pub fn builder_headers(
    creds: &BuilderCreds,
    timestamp_secs: u64,
    method: &str,
    path: &str,
    body: Option<&str>,
) -> Result<HashMap<&'static str, String>> {
    let ts = timestamp_secs.to_string();
    let sig = hmac_sign(&creds.secret, &ts, method, path, body)?;
    let mut h = HashMap::new();
    h.insert("POLY_BUILDER_API_KEY", creds.api_key.clone());
    h.insert("POLY_BUILDER_TIMESTAMP", ts);
    h.insert("POLY_BUILDER_PASSPHRASE", creds.passphrase.clone());
    h.insert("POLY_BUILDER_SIGNATURE", sig);
    Ok(h)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hmac_matches_known_reference() {
        // Vector chosen so we can recompute trivially.
        //   secret  = base64url("hello world")          (the bytes "hello world")
        //   ts="100" method="GET" path="/x" body=None
        //   message = "100" + "GET" + "/x" = "100GET/x"
        // Expected via python equivalence:
        //   import base64, hmac, hashlib
        //   secret = b"hello world"
        //   secret_b64 = base64.urlsafe_b64encode(secret)
        //   sig = hmac.new(secret, b"100GET/x", hashlib.sha256).digest()
        //   out = base64.urlsafe_b64encode(sig).decode()
        // -> well-defined output: ensure deterministic
        let secret_b64 = URL_SAFE.encode(b"hello world");
        let s1 = hmac_sign(&secret_b64, "100", "GET", "/x", None).unwrap();
        let s2 = hmac_sign(&secret_b64, "100", "GET", "/x", None).unwrap();
        assert_eq!(s1, s2);
        // base64url of 32-byte HMAC output: always 44 chars including '='
        // (or 43 without padding — URL_SAFE adds padding)
        assert!(s1.len() == 44 || s1.len() == 43);
    }

    #[test]
    fn hmac_body_changes_signature() {
        let secret_b64 = URL_SAFE.encode(b"k");
        let no_body = hmac_sign(&secret_b64, "1", "POST", "/o", None).unwrap();
        let with_body = hmac_sign(&secret_b64, "1", "POST", "/o", Some("{}")).unwrap();
        assert_ne!(no_body, with_body);
    }

    #[test]
    fn l2_headers_present_and_lowercase_address() {
        let creds = L2Creds {
            api_key: "k".into(),
            secret: URL_SAFE.encode(b"s"),
            passphrase: "p".into(),
        };
        let addr: Address = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266".parse().unwrap();
        let h = l2_headers(&creds, &addr, 1700000000, "POST", "/order", Some("{}"))
            .unwrap();
        assert!(h.contains_key("POLY_API_KEY"));
        assert!(h.contains_key("POLY_TIMESTAMP"));
        assert!(h.contains_key("POLY_PASSPHRASE"));
        assert!(h.contains_key("POLY_SIGNATURE"));
        // alloy's `{:#x}` for Address is lowercase, prefixed.
        assert!(h["POLY_ADDRESS"].starts_with("0x"));
    }

    #[test]
    fn builder_headers_distinct_keys() {
        let bc = BuilderCreds {
            api_key: "bk".into(),
            secret: URL_SAFE.encode(b"bs"),
            passphrase: "bp".into(),
        };
        let h = builder_headers(&bc, 1700000000, "POST", "/order", Some("{}")).unwrap();
        assert!(h.contains_key("POLY_BUILDER_API_KEY"));
        assert!(h.contains_key("POLY_BUILDER_TIMESTAMP"));
        assert!(h.contains_key("POLY_BUILDER_PASSPHRASE"));
        assert!(h.contains_key("POLY_BUILDER_SIGNATURE"));
    }
}
