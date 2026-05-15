//! Polymarket CLOB REST client.
//!
//! Native replacement for `py-clob-client`'s order placement and cancellation
//! endpoints. Persistent `reqwest::Client` with HTTP/2 keepalive + rustls TLS.
//! All endpoints from `docs/developers/CLOB/orders/`:
//!
//!   POST /order              → place one order
//!   POST /orders             → place batch (≤15)
//!   DELETE /order            → cancel one
//!   DELETE /orders           → cancel many (plural, works around the v0
//!                              `cancel()` singleton bug from py-clob-client)
//!   DELETE /cancel-all       → cancel everything for this maker
//!   DELETE /cancel-market    → cancel everything for one market+side

use std::collections::HashMap;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use alloy_primitives::Address;
use anyhow::Result;
use reqwest::header::{HeaderMap, HeaderName, HeaderValue};
use reqwest::{Method, Url};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tracing::{debug, warn};

use super::error::{ClobError, ClobErrorCode};
use crate::signer::{builder_headers, l2_headers, BuilderCreds, L2Creds};
use crate::signer::eip712::{OrderWirePayload, SignedOrderResponse};
use crate::types::OrderType;

const DEFAULT_HOST: &str = "https://clob.polymarket.com";

#[derive(Debug, Clone)]
pub struct ClobConfig {
    pub host: Url,
    pub l2: L2Creds,
    pub builder: Option<BuilderCreds>,
    pub signer_address: Address,
    pub request_timeout: Duration,
}

impl ClobConfig {
    pub fn new(
        host: Option<&str>,
        l2: L2Creds,
        builder: Option<BuilderCreds>,
        signer_address: Address,
    ) -> Result<Self> {
        let host_str = host.unwrap_or(DEFAULT_HOST);
        let host = Url::parse(host_str)?;
        Ok(Self {
            host,
            l2,
            builder,
            signer_address,
            request_timeout: Duration::from_secs(8),
        })
    }
}

#[derive(Debug, Clone)]
pub struct ClobClient {
    cfg: ClobConfig,
    http: reqwest::Client,
}

impl ClobClient {
    pub fn new(cfg: ClobConfig) -> Result<Self> {
        let http = reqwest::Client::builder()
            .use_rustls_tls()
            .http2_prior_knowledge()
            .pool_idle_timeout(Duration::from_secs(90))
            .timeout(cfg.request_timeout)
            .build()?;
        Ok(Self { cfg, http })
    }

    pub fn host(&self) -> &Url {
        &self.cfg.host
    }

    /// POST /order — place one signed order. Returns the server's response
    /// or a typed error.
    pub async fn post_order(
        &self,
        signed: &SignedOrderResponse,
        order_type: OrderType,
        post_only: bool,
    ) -> Result<OrderResponse, ClobError> {
        let path = "/order";
        // PM v2 expects: {order:{…fields incl signature}, owner, orderType,
        //                  deferExec, postOnly}. Mirrors py-clob-client-v2
        //                  `order_to_json_v2`. `postOnly=true` makes PM
        //                  reject (not match) any marketable order.
        let body = json!({
            "order": signed.order,
            "owner": signed.owner,
            "orderType": order_type.as_str(),
            "deferExec": false,
            "postOnly": post_only,
        });
        let body_str = serde_json::to_string(&body).map_err(|e| ClobError::Parse(e.to_string()))?;
        tracing::warn!(target: "pmmm::clob::post_order_payload", body = %body_str, "POST /order body");
        self.send_json::<OrderResponse>(Method::POST, path, Some(body_str)).await
    }

    /// POST /orders — place batch up to 15 orders in one request.
    pub async fn post_orders_batch(
        &self,
        signed: &[SignedOrderResponse],
        order_type: OrderType,
    ) -> Result<Vec<OrderResponse>, ClobError> {
        if signed.is_empty() {
            return Ok(Vec::new());
        }
        if signed.len() > 15 {
            return Err(ClobError::Other(format!(
                "batch size {} exceeds PM cap of 15",
                signed.len()
            )));
        }
        let path = "/orders";
        let entries: Vec<_> = signed
            .iter()
            .map(|s| {
                json!({
                    "order": s.order,
                    "owner": s.owner,
                    "orderType": order_type.as_str(),
                    "deferExec": false,
                    "postOnly": false,
                })
            })
            .collect();
        let body_str = serde_json::to_string(&entries).map_err(|e| ClobError::Parse(e.to_string()))?;
        self.send_json::<Vec<OrderResponse>>(Method::POST, path, Some(body_str)).await
    }

    /// DELETE /orders — cancel one OR many order IDs in one request. Uses the
    /// plural form which works around a known bug in py-clob-client v0 where
    /// `cancel()` (singular) crashes with "'str' object has no attribute
    /// 'orderID'". See `lib/maker_bot.py.bak.v8` comments for history.
    pub async fn cancel_orders(&self, order_ids: &[String]) -> Result<CancelResponse, ClobError> {
        if order_ids.is_empty() {
            return Ok(CancelResponse::default());
        }
        let body_str = serde_json::to_string(order_ids)
            .map_err(|e| ClobError::Parse(e.to_string()))?;
        self.send_json::<CancelResponse>(Method::DELETE, "/orders", Some(body_str)).await
    }

    /// DELETE /cancel-all — cancel every resting order for this maker.
    pub async fn cancel_all(&self) -> Result<CancelResponse, ClobError> {
        self.send_json::<CancelResponse>(Method::DELETE, "/cancel-all", None).await
    }

    /// Build the full set of auth headers (L2 + Builder if configured).
    fn build_headers(
        &self,
        method: &str,
        path: &str,
        body: Option<&str>,
    ) -> Result<HeaderMap, ClobError> {
        let ts = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| ClobError::Auth(e.to_string()))?
            .as_secs();

        let mut all: HashMap<&'static str, String> = HashMap::new();
        all.extend(
            l2_headers(&self.cfg.l2, &self.cfg.signer_address, ts, method, path, body)
                .map_err(|e| ClobError::Auth(e.to_string()))?,
        );
        if let Some(b) = &self.cfg.builder {
            all.extend(
                builder_headers(b, ts, method, path, body)
                    .map_err(|e| ClobError::Auth(e.to_string()))?,
            );
        }
        let mut h = HeaderMap::new();
        for (k, v) in all {
            // PM header names are uppercase (POLY_API_KEY, POLY_BUILDER_*).
            // HeaderName::from_static panics on uppercase; from_bytes normalizes
            // to lowercase per RFC 7230 — HTTP header names are case-insensitive.
            let name = HeaderName::from_bytes(k.as_bytes())
                .map_err(|e| ClobError::Auth(format!("bad header name {k}: {e}")))?;
            let value = HeaderValue::from_str(&v).map_err(|e| ClobError::Auth(e.to_string()))?;
            h.insert(name, value);
        }
        h.insert(
            HeaderName::from_static("content-type"),
            HeaderValue::from_static("application/json"),
        );
        Ok(h)
    }

    /// Generic JSON request + response helper.
    async fn send_json<Resp>(
        &self,
        method: Method,
        path: &str,
        body: Option<String>,
    ) -> Result<Resp, ClobError>
    where
        Resp: serde::de::DeserializeOwned,
    {
        let url = self
            .cfg
            .host
            .join(path.trim_start_matches('/'))
            .map_err(|e| ClobError::Other(format!("join url: {e}")))?;
        let headers = self.build_headers(method.as_str(), path, body.as_deref())?;
        let mut req = self.http.request(method.clone(), url).headers(headers);
        if let Some(b) = body.clone() {
            req = req.body(b);
        }
        let t0 = std::time::Instant::now();
        let resp = req.send().await?;
        let status = resp.status();
        let elapsed = t0.elapsed();
        let text = resp.text().await?;

        debug!(
            method = %method,
            path = path,
            status = status.as_u16(),
            elapsed_us = elapsed.as_micros() as u64,
            body_len = text.len(),
            "CLOB response"
        );

        if !status.is_success() {
            // Try to extract an error message from the body
            let msg = serde_json::from_str::<serde_json::Value>(&text)
                .ok()
                .and_then(|v| v.get("error").and_then(|e| e.as_str()).map(|s| s.to_string()))
                .unwrap_or_else(|| text.clone());
            warn!(
                status = status.as_u16(),
                error = %msg,
                "CLOB request failed"
            );
            let code = ClobErrorCode::parse(&msg);
            return Err(ClobError::Rejected(code, msg));
        }

        serde_json::from_str::<Resp>(&text).map_err(|e| {
            ClobError::Parse(format!("decode {} response: {} [body={}]", path, e, text))
        })
    }
}

/// Response from POST /order or POST /orders.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderResponse {
    pub success: bool,
    #[serde(default, rename = "errorMsg")]
    pub error_msg: String,
    #[serde(default, rename = "orderID", alias = "orderId")]
    pub order_id: String,
    #[serde(default, rename = "orderHashes")]
    pub order_hashes: Vec<String>,
    /// "matched" | "live" | "delayed" | "unmatched"
    #[serde(default)]
    pub status: String,
}

/// Response from DELETE /orders or DELETE /cancel-all.
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct CancelResponse {
    #[serde(default)]
    pub canceled: Vec<String>,
    #[serde(default, alias = "not_canceled")]
    pub not_canceled: HashMap<String, String>,
}

#[cfg(test)]
mod tests {
    use super::*;
    use base64::engine::general_purpose::URL_SAFE;
    use base64::Engine;

    fn test_creds() -> L2Creds {
        L2Creds {
            api_key: "test-key".into(),
            secret: URL_SAFE.encode(b"hello"),
            passphrase: "test-pass".into(),
        }
    }

    fn test_addr() -> Address {
        "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266".parse().unwrap()
    }

    #[test]
    fn config_uses_default_host() {
        let cfg = ClobConfig::new(None, test_creds(), None, test_addr()).unwrap();
        assert_eq!(cfg.host.as_str(), "https://clob.polymarket.com/");
    }

    #[test]
    fn config_accepts_custom_host() {
        let cfg = ClobConfig::new(Some("https://example.local"), test_creds(), None, test_addr()).unwrap();
        assert_eq!(cfg.host.as_str(), "https://example.local/");
    }

    #[test]
    fn order_response_decodes_success() {
        let body = r#"{
            "success": true,
            "errorMsg": "",
            "orderID": "0xabc123",
            "orderHashes": ["0xabc123"],
            "status": "live"
        }"#;
        let r: OrderResponse = serde_json::from_str(body).unwrap();
        assert!(r.success);
        assert_eq!(r.order_id, "0xabc123");
        assert_eq!(r.status, "live");
    }

    #[test]
    fn order_response_decodes_alias_orderId() {
        // PM sometimes returns `orderId` (camelCase) instead of `orderID`.
        let body = r#"{"success":true,"orderId":"abc","status":"live"}"#;
        let r: OrderResponse = serde_json::from_str(body).unwrap();
        assert_eq!(r.order_id, "abc");
    }

    #[test]
    fn cancel_response_decodes_partial() {
        let body = r#"{"canceled":["a","b"],"not_canceled":{"c":"already canceled or matched"}}"#;
        let r: CancelResponse = serde_json::from_str(body).unwrap();
        assert_eq!(r.canceled.len(), 2);
        assert_eq!(r.not_canceled.len(), 1);
    }
}
