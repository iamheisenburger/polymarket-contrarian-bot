//! Theo v6 numerical parity test.
//!
//! Loads `reference/parity_fixtures/theo_v6_cases.json` (10,000 cases generated
//! by `pmmm/reference/gen_theo_fixtures.py` against the Python reference
//! `lib/theo_v6.py`) and asserts the Rust port produces matching output:
//!
//! - When Python returns a value, Rust must return Some(v) with |v - python| < 1e-9
//! - When Python returns None, Rust must also return None
//!
//! Goal: bit-precise-enough parity to drop the Rust theo into production
//! without changing decisions vs the Python baseline.

use std::fs;
use std::path::PathBuf;

use serde::Deserialize;

use pmmm::strategy::{theo_p_up_at, TheoV6Params};

/// Match the Python `to_dict()` shape exactly. Note Python uses
/// `coef_log_T` and `coef_log_dist` (capital T).
#[derive(Debug, Deserialize)]
struct ParamsRaw {
    horizons_s: Vec<f64>,
    #[serde(default)]
    sigma_floor: f64,
    #[serde(default = "default_one")]
    sigma_scale: f64,
    intercept: f64,
    coef_logit_bs: Vec<f64>,
    #[serde(default, rename = "coef_log_T")]
    coef_log_t: f64,
    #[serde(default)]
    coef_log_dist: f64,
}

fn default_one() -> f64 {
    1.0
}

/// One fixture row. The JSON has `"T"` (remaining time) and `"t"` (wall
/// clock); we rename to avoid the field-name collision.
#[derive(Debug, Deserialize)]
struct Case {
    #[serde(rename = "S")]
    s: f64,
    #[serde(rename = "K")]
    k: f64,
    #[serde(rename = "T")]
    t_remaining: f64,
    #[serde(rename = "t")]
    t_wall: f64,
    series: Vec<(f64, f64)>,
    params: ParamsRaw,
    expected: Option<f64>,
}

fn fixtures_path() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .join("reference")
        .join("parity_fixtures")
        .join("theo_v6_cases.json")
}

#[test]
fn theo_v6_parity_10k_cases() {
    let path = fixtures_path();
    if !path.exists() {
        eprintln!(
            "skipping theo_v6_parity_10k_cases: fixtures not present at {}",
            path.display()
        );
        eprintln!("generate them with: python3 pmmm/reference/gen_theo_fixtures.py");
        return;
    }

    let raw = fs::read_to_string(&path).expect("read fixtures");
    let cases: Vec<Case> = serde_json::from_str(&raw).expect("parse fixtures");
    eprintln!("loaded {} cases", cases.len());

    // Production-relevant tolerance: bid prices round to 1c (1e-2), so any
    // theo difference << 1e-3 is invisible to placement decisions. We use
    // 1e-6 as a hard floor — anything above that suggests an algorithmic
    // divergence rather than ULP-level floating-point noise.
    const TOLERANCE: f64 = 1e-6;

    let mut n_null_ok = 0usize;
    let mut n_none_mismatch = 0usize;
    let mut diffs: Vec<f64> = Vec::new();
    let mut over_tol: Vec<(usize, f64, f64, f64)> = Vec::new();

    for (i, case) in cases.iter().enumerate() {
        let keys: Vec<f64> = case.series.iter().map(|(t, _)| *t).collect();
        let params: TheoV6Params = clone_params(&case.params);
        let got = theo_p_up_at(
            case.t_wall,
            case.s,
            case.k,
            case.t_remaining,
            &case.series,
            &keys,
            &params,
        );

        match (got, case.expected) {
            (None, None) => {
                n_null_ok += 1;
            }
            (Some(got_v), Some(exp_v)) => {
                let diff = (got_v - exp_v).abs();
                diffs.push(diff);
                if diff > TOLERANCE && over_tol.len() < 20 {
                    over_tol.push((i, got_v, exp_v, diff));
                }
            }
            (got_some, exp) => {
                n_none_mismatch += 1;
                if n_none_mismatch <= 5 {
                    eprintln!(
                        "  case {}: got {:?}, expected {:?} (None/Some mismatch)",
                        i, got_some, exp
                    );
                }
            }
        }
    }

    // Distribution stats.
    diffs.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
    let n = diffs.len();
    let p50 = if n > 0 { diffs[n / 2] } else { 0.0 };
    let p90 = if n > 0 { diffs[(n * 90 / 100).min(n - 1)] } else { 0.0 };
    let p99 = if n > 0 { diffs[(n * 99 / 100).min(n - 1)] } else { 0.0 };
    let p999 = if n > 0 { diffs[(n * 999 / 1000).min(n - 1)] } else { 0.0 };
    let max = diffs.last().copied().unwrap_or(0.0);
    let n_within_1e9 = diffs.iter().filter(|&&d| d <= 1e-9).count();
    let n_within_1e6 = diffs.iter().filter(|&&d| d <= 1e-6).count();
    let n_within_1e4 = diffs.iter().filter(|&&d| d <= 1e-4).count();

    eprintln!();
    eprintln!("=== PARITY DISTRIBUTION (non-null cases) ===");
    eprintln!("non-null cases: {}", n);
    eprintln!("null matches:   {}", n_null_ok);
    eprintln!("None/Some mismatches: {}", n_none_mismatch);
    eprintln!();
    eprintln!("diffs |rust - python|:");
    eprintln!("  P50:  {:.3e}", p50);
    eprintln!("  P90:  {:.3e}", p90);
    eprintln!("  P99:  {:.3e}", p99);
    eprintln!("  P999: {:.3e}", p999);
    eprintln!("  MAX:  {:.3e}", max);
    eprintln!();
    eprintln!("within tolerances:");
    eprintln!(
        "  ≤1e-9: {}/{} ({:.1}%)",
        n_within_1e9,
        n,
        100.0 * n_within_1e9 as f64 / n.max(1) as f64
    );
    eprintln!(
        "  ≤1e-6: {}/{} ({:.1}%)",
        n_within_1e6,
        n,
        100.0 * n_within_1e6 as f64 / n.max(1) as f64
    );
    eprintln!(
        "  ≤1e-4: {}/{} ({:.1}%)",
        n_within_1e4,
        n,
        100.0 * n_within_1e4 as f64 / n.max(1) as f64
    );

    if !over_tol.is_empty() {
        eprintln!();
        eprintln!("first cases over tolerance ({:.0e}):", TOLERANCE);
        for (i, got, exp, diff) in &over_tol {
            eprintln!(
                "  case {}: got {:.15}, expected {:.15}, diff {:.3e}",
                i, got, exp, diff
            );
        }
    }

    if n_none_mismatch > 0 {
        panic!(
            "None/Some mismatch: {} cases — algorithmic divergence (expected None vs Some or vice versa)",
            n_none_mismatch
        );
    }

    let pct_within = 100.0 * n_within_1e6 as f64 / n.max(1) as f64;
    assert!(
        pct_within >= 99.0,
        "<99% of cases within {:.0e}: only {:.1}% (max diff: {:.3e})",
        TOLERANCE,
        pct_within,
        max
    );
}

fn clone_params(r: &ParamsRaw) -> TheoV6Params {
    TheoV6Params {
        horizons_s: r.horizons_s.clone(),
        sigma_floor: r.sigma_floor,
        sigma_scale: r.sigma_scale,
        intercept: r.intercept,
        coef_logit_bs: r.coef_logit_bs.clone(),
        coef_log_t: r.coef_log_t,
        coef_log_dist: r.coef_log_dist,
    }
}
