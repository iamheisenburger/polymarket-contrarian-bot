//! Theo engine v6 — multi-horizon ensemble (HAR-style).
//!
//! Direct port of `lib/theo_v6.py`. Numerical parity required: golden-file
//! tests in `tests/theo_parity.rs` compare 10,000 random inputs against
//! Python output and require |Δ| < 1e-9.
//!
//! Per Ohan: "use most common measures of recent realized volatility" — we
//! use a HAR-RV-style mixture of multiple horizons (30s, 60s, 180s, 300s,
//! 900s). For each horizon `i`:
//!   σ_i = realized_vol(spot_series, t, window_i)
//!   p_BS_i = bs_digital_up(S, K, T, σ_i)
//!
//! Ensemble in logit space (coefficients fit on settled outcomes):
//!   logit(p_theo) = α + Σ_i β_i · logit(p_BS_i)
//!                 + γ · log(max(1, T_remaining))
//!                 + δ · log(S / K)
//!
//! No PM mid input — purely (Binance/Pyth)-σ + BS + clock + spot/strike.

use serde::{Deserialize, Serialize};
use statrs::distribution::{ContinuousCDF, Normal};

/// Standard ϕ⁻¹ horizons used by the v6 ensemble.
pub const DEFAULT_HORIZONS_S: [f64; 5] = [30.0, 60.0, 180.0, 300.0, 900.0];

/// Fitted HAR ensemble coefficients. Matches `TheoV6Params` in
/// `lib/theo_v6.py` field-for-field. Loaded from
/// `data/pricing_model/theo_v6_current.json`.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct TheoV6Params {
    pub horizons_s: Vec<f64>,
    #[serde(default)]
    pub sigma_floor: f64,
    #[serde(default = "default_sigma_scale")]
    pub sigma_scale: f64,
    pub intercept: f64,
    pub coef_logit_bs: Vec<f64>,
    #[serde(default)]
    pub coef_log_t: f64,
    #[serde(default)]
    pub coef_log_dist: f64,
    /// Per-coin additive intercept (logit space). Looked up by lowercase
    /// coin string ("btc"/"eth"/"sol"/"xrp"). Absent or missing key = 0.
    /// Captures structural mispricings PM has on specific coins without
    /// requiring per-coin σ-mix coefficients.
    #[serde(default)]
    pub coin_intercepts: std::collections::HashMap<String, f64>,
}

fn default_sigma_scale() -> f64 {
    1.0
}

impl TheoV6Params {
    /// Number of σ horizons.
    pub fn n_horizons(&self) -> usize {
        self.horizons_s.len()
    }
}

// ---------- helpers (parity-critical: byte-equivalent semantics to Python) ----------

#[inline]
fn clip_p(p: f64) -> f64 {
    const LO: f64 = 1e-6;
    const HI: f64 = 1.0 - 1e-6;
    p.clamp(LO, HI)
}

#[inline]
pub fn logit(p: f64) -> f64 {
    let p = clip_p(p);
    (p / (1.0 - p)).ln()
}

#[inline]
pub fn sigmoid(x: f64) -> f64 {
    if x > 35.0 {
        1.0 - 1e-15
    } else if x < -35.0 {
        1e-15
    } else {
        1.0 / (1.0 + (-x).exp())
    }
}

/// Black-Scholes digital call (Up): P(S_T > K) under risk-neutral measure.
///
/// d2 = (ln(S/K) - 0.5σ²T) / (σ√T)
/// price = Φ(d2)
///
/// Returns None for invalid inputs (matches Python None semantics; callers
/// should propagate). r is assumed 0 (intraday horizon).
pub fn bs_digital_up(s: f64, k: f64, t: f64, sigma: f64) -> Option<f64> {
    if !(s > 0.0 && k > 0.0 && t > 0.0 && sigma > 0.0) {
        return None;
    }
    if !s.is_finite() || !k.is_finite() || !t.is_finite() || !sigma.is_finite() {
        return None;
    }
    let s_t = sigma * t.sqrt();
    if !(s_t > 0.0) {
        return None;
    }
    let d2 = ((s / k).ln() - 0.5 * sigma * sigma * t) / s_t;
    if !d2.is_finite() {
        return None;
    }
    let n = Normal::standard();
    Some(n.cdf(d2))
}

/// Realized volatility (per-√sec stdev of normalized log returns) over
/// the window `[t_end - window_s, t_end]`.
///
/// `series` is a slice of `(timestamp_seconds, price)` pairs sorted by
/// timestamp ascending. `keys` must mirror the timestamps in `series` (we
/// keep them separate to allow O(log n) bisect without re-extracting).
///
/// Returns None if fewer than 5 return samples fall in the window — matches
/// the Python guard exactly.
pub fn realized_vol(series: &[(f64, f64)], keys: &[f64], t_end: f64, window_s: f64) -> Option<f64> {
    if series.is_empty() || keys.is_empty() {
        return None;
    }
    debug_assert_eq!(series.len(), keys.len(), "series/keys length mismatch");

    let t_lo = t_end - window_s;
    // bisect_left for t_lo (first index with key >= t_lo)
    let i_lo = keys.partition_point(|&k| k < t_lo);
    // bisect_right for t_end (first index with key > t_end)
    let i_hi = keys.partition_point(|&k| k <= t_end);

    // Match Python: needs at least 5 candidate samples in [i_lo, i_hi).
    if i_hi.saturating_sub(i_lo) < 5 {
        return None;
    }

    let mut rets: Vec<f64> = Vec::with_capacity(i_hi - i_lo);
    for k in (i_lo + 1)..i_hi {
        let (t0, p0) = series[k - 1];
        let (t1, p1) = series[k];
        let dt = t1 - t0;
        if p0 > 0.0 && p1 > 0.0 && dt > 0.0 {
            let r = (p1 / p0).ln() / dt.sqrt();
            rets.push(r);
        }
    }
    if rets.len() < 5 {
        return None;
    }

    let n = rets.len() as f64;
    let mean: f64 = rets.iter().sum::<f64>() / n;
    // Sample variance (Bessel-corrected; matches Python n-1 divisor).
    let var: f64 = rets.iter().map(|r| (r - mean) * (r - mean)).sum::<f64>() / (n - 1.0);
    Some(var.max(0.0).sqrt())
}

/// Compute calibrated v6 theo P(Up) using the HAR ensemble.
///
/// Returns None if any required input is missing or any σ window is too
/// sparse to estimate. Otherwise returns a probability in (0, 1) ≈ Φ(z)
/// where z = α + Σ β_i · logit(p_BS_i) + γ·log(max(1,T)) + δ·log(S/K).
pub fn theo_p_up_at(
    t: f64,
    s: f64,
    k: f64,
    t_remaining: f64,
    series: &[(f64, f64)],
    keys: &[f64],
    params: &TheoV6Params,
) -> Option<f64> {
    theo_p_up_at_for_coin(t, s, k, t_remaining, series, keys, params, None)
}

/// Variant that applies the per-coin intercept correction. Pass coin as
/// lowercase ("btc"/"eth"/"sol"/"xrp"); None or unknown skips the correction.
pub fn theo_p_up_at_for_coin(
    t: f64,
    s: f64,
    k: f64,
    t_remaining: f64,
    series: &[(f64, f64)],
    keys: &[f64],
    params: &TheoV6Params,
    coin: Option<&str>,
) -> Option<f64> {
    if !(t_remaining > 0.0 && s > 0.0 && k > 0.0) {
        return None;
    }
    if params.horizons_s.len() != params.coef_logit_bs.len() {
        return None;
    }

    let mut z = params.intercept;
    if let Some(c) = coin {
        if let Some(extra) = params.coin_intercepts.get(&c.to_lowercase()) {
            z += *extra;
        }
    }
    for (w, beta) in params.horizons_s.iter().zip(params.coef_logit_bs.iter()) {
        let sig_raw = realized_vol(series, keys, t, *w)?;
        let sig = sig_raw.max(params.sigma_floor) * params.sigma_scale;
        let p_bs = bs_digital_up(s, k, t_remaining, sig)?;
        z += beta * logit(p_bs);
    }
    z += params.coef_log_t * t_remaining.max(1.0).ln();
    z += params.coef_log_dist * (s / k).ln();
    Some(sigmoid(z))
}

/// Convenience: brier score Σ(p - y)² / n.
pub fn brier(yps: &[(f64, f64)]) -> Option<f64> {
    if yps.is_empty() {
        return None;
    }
    let s: f64 = yps.iter().map(|(y, p)| (p - y).powi(2)).sum();
    Some(s / yps.len() as f64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use approx::assert_relative_eq;

    #[test]
    fn logit_sigmoid_inverse() {
        for p in [0.1, 0.3, 0.5, 0.7, 0.9] {
            let z = logit(p);
            let p_back = sigmoid(z);
            assert_relative_eq!(p, p_back, max_relative = 1e-12);
        }
    }

    #[test]
    fn sigmoid_clamps_extreme_inputs() {
        assert!(sigmoid(100.0) > 1.0 - 1e-14);
        assert!(sigmoid(-100.0) < 1e-14);
    }

    #[test]
    fn bs_digital_at_money_with_short_t_below_half() {
        // S == K, very short time: d2 = -0.5σ√T < 0, so p < 0.5.
        let p = bs_digital_up(100.0, 100.0, 60.0, 0.5).unwrap();
        assert!(p < 0.5);
        assert!(p > 0.0);
    }

    #[test]
    fn bs_digital_deep_itm_approaches_one() {
        // S >> K and realistic per-√sec σ: d2 large positive, Φ→1.
        // σ=0.01/√s is typical for calibrated theo over a 60s window
        // (σ√T ≈ 0.077). At S/K=2.0, ln(S/K)=0.693, d2≈8.9 → p≈1.
        let p = bs_digital_up(200.0, 100.0, 60.0, 0.01).unwrap();
        assert!(p > 0.99, "expected p>0.99, got {}", p);
    }

    #[test]
    fn bs_digital_rejects_invalid() {
        assert!(bs_digital_up(0.0, 100.0, 60.0, 0.5).is_none());
        assert!(bs_digital_up(100.0, 0.0, 60.0, 0.5).is_none());
        assert!(bs_digital_up(100.0, 100.0, 0.0, 0.5).is_none());
        assert!(bs_digital_up(100.0, 100.0, 60.0, 0.0).is_none());
        assert!(bs_digital_up(f64::NAN, 100.0, 60.0, 0.5).is_none());
    }

    #[test]
    fn realized_vol_requires_min_samples() {
        let series: Vec<(f64, f64)> = (0..4)
            .map(|i| (i as f64, 100.0 + i as f64))
            .collect();
        let keys: Vec<f64> = series.iter().map(|(t, _)| *t).collect();
        // 4 samples → 3 returns → below 5-return threshold → None.
        assert!(realized_vol(&series, &keys, 3.0, 5.0).is_none());
    }

    #[test]
    fn realized_vol_constant_price_is_zero() {
        let series: Vec<(f64, f64)> = (0..20).map(|i| (i as f64, 100.0)).collect();
        let keys: Vec<f64> = series.iter().map(|(t, _)| *t).collect();
        let sig = realized_vol(&series, &keys, 19.0, 20.0).unwrap();
        assert!(sig.abs() < 1e-12);
    }
}
