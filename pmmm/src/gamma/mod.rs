//! Polymarket Gamma API client — market discovery.
//!
//! Direct port of `src/gamma_client.py`. Polls the gamma REST API to find
//! the currently-active 5m / 15m / 1h crypto Up-or-Down markets for each
//! supported coin. Returns slugs + token IDs which the strategy then
//! subscribes to via the market WS.

use std::time::Duration;

use anyhow::{Context, Result};
use reqwest::Url;
use serde::{Deserialize, Serialize};
use tracing::debug;

use crate::types::Symbol;

const DEFAULT_HOST: &str = "https://gamma-api.polymarket.com";

#[derive(Debug, Clone)]
pub struct GammaClient {
    base: Url,
    http: reqwest::Client,
}

/// Subset of the Gamma market object fields we actually use. The endpoint
/// returns many more (volume, liquidity, etc.) that we don't need here.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct GammaMarket {
    pub slug: String,
    #[serde(default)]
    pub closed: bool,
    #[serde(default, rename = "acceptingOrders")]
    pub accepting_orders: bool,
    #[serde(default, rename = "clobTokenIds")]
    pub clob_token_ids: String, // JSON-stringified ["up_id", "down_id"]
    #[serde(default)]
    pub outcomes: String, // JSON-stringified ["Up", "Down"]
    #[serde(default, rename = "endDate")]
    pub end_date: String,
}

impl GammaMarket {
    /// Parse the embedded JSON-string `clobTokenIds` into a (up, down) pair.
    pub fn token_ids(&self) -> Result<(String, String)> {
        let v: Vec<String> = serde_json::from_str(&self.clob_token_ids)
            .with_context(|| "parse clobTokenIds")?;
        if v.len() < 2 {
            return Err(anyhow::anyhow!("expected ≥2 token IDs, got {}", v.len()));
        }
        Ok((v[0].clone(), v[1].clone()))
    }
}

impl GammaClient {
    pub fn new(host: Option<&str>) -> Result<Self> {
        let base = Url::parse(host.unwrap_or(DEFAULT_HOST))?;
        let http = reqwest::Client::builder()
            .timeout(Duration::from_secs(8))
            .build()?;
        Ok(Self { base, http })
    }

    /// Fetch one market by slug. Returns None if not found / parse fails.
    pub async fn get_by_slug(&self, slug: &str) -> Result<Option<GammaMarket>> {
        let url = self.base.join(&format!("markets/slug/{slug}"))?;
        let resp = self.http.get(url).send().await?;
        if !resp.status().is_success() {
            return Ok(None);
        }
        let m: GammaMarket = resp.json().await.with_context(|| "decode gamma market")?;
        Ok(Some(m))
    }

    /// Discover the currently-active market for one (coin, timeframe).
    /// Slug pattern: `{coin}-updown-{tf}-{unix_ts}` where unix_ts is the
    /// market start unix seconds (300s / 900s / 3600s aligned).
    pub async fn current_market(&self, coin: Symbol, tf: TimeFrame) -> Result<Option<GammaMarket>> {
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .with_context(|| "system time")?
            .as_secs();
        let window = tf.seconds();
        let start = (now / window) * window;
        let slug = format!("{}-updown-{}-{}", coin.as_str().to_lowercase(), tf.as_str(), start);
        debug!(slug, "gamma: probing current market");
        self.get_by_slug(&slug).await
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum TimeFrame {
    M5,
    M15,
    H1,
}

impl TimeFrame {
    pub fn as_str(&self) -> &'static str {
        match self {
            TimeFrame::M5 => "5m",
            TimeFrame::M15 => "15m",
            TimeFrame::H1 => "1h",
        }
    }
    pub fn seconds(&self) -> u64 {
        match self {
            TimeFrame::M5 => 300,
            TimeFrame::M15 => 900,
            TimeFrame::H1 => 3600,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_clob_token_ids() {
        let m = GammaMarket {
            slug: "btc-updown-5m-1700000000".into(),
            closed: false,
            accepting_orders: true,
            clob_token_ids: r#"["12345","67890"]"#.into(),
            outcomes: r#"["Up","Down"]"#.into(),
            end_date: "2026-05-14T00:00:00Z".into(),
        };
        let (u, d) = m.token_ids().unwrap();
        assert_eq!(u, "12345");
        assert_eq!(d, "67890");
    }

    #[test]
    fn timeframe_seconds() {
        assert_eq!(TimeFrame::M5.seconds(), 300);
        assert_eq!(TimeFrame::M15.seconds(), 900);
        assert_eq!(TimeFrame::H1.seconds(), 3600);
    }
}
