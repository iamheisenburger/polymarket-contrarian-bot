#!/usr/bin/env python3
"""Generate parity fixtures for the Rust theo_v6 port.

Reads `lib/theo_v6.py` (Python reference), produces 10,000 random test cases,
and writes them as JSON to `pmmm/reference/parity_fixtures/theo_v6_cases.json`.

Each case has the form:
  {
    "S": 79000.0,
    "K": 79100.0,
    "T": 60.0,
    "t": 1700000000.0,
    "series": [[t0, p0], [t1, p1], ...],
    "params": {
      "horizons_s": [30, 60, 180, 300, 900],
      "sigma_floor": 0.0,
      "sigma_scale": 1.0,
      "intercept": 0.1,
      "coef_logit_bs": [0.2, 0.2, 0.2, 0.2, 0.2],
      "coef_log_T": 0.0,
      "coef_log_dist": 0.0
    },
    "expected": 0.4837...   // or null
  }

The Rust test loads this file and asserts |rust_p - expected| < 1e-9
(or both are null).

Run:
  python3 pmmm/reference/gen_theo_fixtures.py
"""

import json
import math
import random
import sys
from pathlib import Path

# Make sure we can import the Python reference module.
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from lib.theo_v6 import TheoV6Params, theo_p_up_at  # noqa: E402

# Reproducible fixtures.
random.seed(42)

N_CASES = 10_000
OUT_PATH = REPO_ROOT / "pmmm" / "reference" / "parity_fixtures" / "theo_v6_cases.json"


def random_series(t_end: float, lookback: float = 1200.0,
                  base_price: float = 79000.0,
                  vol_per_sqrt_s: float = 0.001) -> list:
    """Synthesize a realistic spot-tick series ending at t_end."""
    n = random.randint(50, 400)
    ts = sorted(t_end - random.uniform(0.0, lookback) for _ in range(n))
    series = []
    p = base_price * random.uniform(0.95, 1.05)
    for i, t in enumerate(ts):
        dt = (ts[i] - ts[i - 1]) if i > 0 else 0.5
        shock = random.gauss(0.0, vol_per_sqrt_s * math.sqrt(max(dt, 0.01)))
        p = max(0.01, p * math.exp(shock))
        series.append([t, p])
    return series


def random_params() -> TheoV6Params:
    """Random (but reasonable) HAR ensemble parameters."""
    horizons = [30, 60, 180, 300, 900]
    return TheoV6Params(
        horizons_s=horizons,
        sigma_floor=random.choice([0.0, 1e-5, 1e-4]),
        sigma_scale=random.uniform(0.5, 2.0),
        intercept=random.uniform(-2.0, 2.0),
        coef_logit_bs=[random.uniform(-1.0, 1.0) for _ in horizons],
        coef_log_T=random.uniform(-0.5, 0.5),
        coef_log_dist=random.uniform(-5.0, 5.0),
    )


def main() -> int:
    cases = []
    nulls = 0
    for i in range(N_CASES):
        t = 1_700_000_000.0 + random.uniform(0, 1_000_000.0)
        S = random.uniform(50_000, 100_000) if random.random() < 0.5 else random.uniform(1.0, 5_000)
        K = S * random.uniform(0.95, 1.05)
        T_rem = random.uniform(1.0, 900.0)
        series = random_series(t, base_price=S)
        params = random_params()
        keys = [pt[0] for pt in series]
        try:
            expected = theo_p_up_at(t, S, K, T_rem, series, keys, params)
        except Exception:
            expected = None
        if expected is None:
            nulls += 1
        cases.append({
            "S": S,
            "K": K,
            "T": T_rem,
            "t": t,
            "series": series,
            "params": params.to_dict(),
            "expected": expected,
        })
        if (i + 1) % 1000 == 0:
            print(f"  {i + 1}/{N_CASES} (nulls so far: {nulls})", file=sys.stderr)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_PATH, "w") as f:
        json.dump(cases, f)
    print(f"\nwrote {len(cases)} cases to {OUT_PATH}", file=sys.stderr)
    print(f"  non-null expected: {len(cases) - nulls}", file=sys.stderr)
    print(f"  null expected:     {nulls}", file=sys.stderr)
    print(f"  file size: {OUT_PATH.stat().st_size / 1024 / 1024:.1f} MB", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
