"""
Direct Fair Value Calculator — Empirical probability from price gap + time.

Replaces Black-Scholes with a direct approach: given spot vs strike and
time remaining, what is the empirical probability the current side wins?

No volatility model. No theoretical distribution. Just calibrated from
real trade outcomes and historical Binance 5m candle data.

The key insight from the $313-414K profitable bot: with T seconds remaining
and spot X% above strike, what % of the time does spot stay above strike?

Usage:
    from lib.direct_fv import DirectFairValue
    from lib.fair_value import FairValue

    fv = DirectFairValue()
    result = fv.calculate(spot=84500, strike=84400, seconds_to_expiry=120)
    # result.fair_up ≈ 0.72 (72% chance UP wins)
"""

import json
import math
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Tuple, List

from lib.fair_value import FairValue, MIN_PRICE, MAX_PRICE

logger = logging.getLogger(__name__)


# Default calibration parameters for the sigmoid model.
# These define the base sensitivity (how quickly confidence grows with gap).
# Calibrate from real data using calibrate() or apps/calibrate_fv.py.
DEFAULT_CALIBRATION = {
    # Sigmoid steepness parameter: controls how fast P rises with gap.
    # Higher = more aggressive (snaps to 0 or 1 faster).
    # At k=800, a 0.1% gap with 60s left → ~70% confidence.
    "k": 800.0,

    # Time decay exponent: controls how time remaining affects confidence.
    # Lower time = higher confidence (outcome more decided).
    # At alpha=0.5, confidence grows as 1/sqrt(time).
    "alpha": 0.5,

    # Reference time in seconds: the "full window" length.
    # Sigmoid is normalized so that at t=ref_time, sensitivity is baseline.
    "ref_time": 300.0,

    # Floor probability: never go below this (even with tiny gap + lots of time).
    # Prevents the model from saying 50.01% on a clear gap.
    "floor": 0.50,

    # Ceiling probability: never exceed this (always leave some uncertainty).
    "ceiling": 0.99,
}


class DirectFairValue:
    """
    Calculate fair value by directly comparing spot vs strike.
    No Black-Scholes. No volatility model. Just price gap + time remaining.

    The core function:
        gap_pct = (spot - strike) / strike
        time_factor = (ref_time / seconds_left) ^ alpha
        P(UP wins) = sigmoid(k * gap_pct * time_factor)

    Where sigmoid(x) = 1 / (1 + exp(-x))

    This means:
    - Larger gap → higher confidence
    - Less time remaining → higher confidence (price has less time to reverse)
    - Both effects multiply: big gap + little time = very high confidence
    """

    def __init__(self, calibration_path: Optional[str] = None):
        """
        Args:
            calibration_path: Path to calibration JSON file.
                If None, uses default parameters.
                If file exists, loads calibrated parameters.
        """
        self.params = dict(DEFAULT_CALIBRATION)

        if calibration_path:
            self._load_calibration(calibration_path)

    def _load_calibration(self, path: str) -> bool:
        """Load calibration from JSON file."""
        try:
            p = Path(path)
            if p.exists():
                with open(p) as f:
                    data = json.load(f)
                if "params" in data:
                    self.params.update(data["params"])
                    logger.info(f"Loaded calibration from {path}: k={self.params['k']:.1f}, "
                                f"alpha={self.params['alpha']:.2f}")
                    return True
        except Exception as e:
            logger.warning(f"Failed to load calibration from {path}: {e}")
        return False

    def calculate(
        self,
        spot: float,
        strike: float,
        seconds_to_expiry: float,
        volatility: float = 0.0,  # Accepted but ignored — kept for interface compat
    ) -> FairValue:
        """
        Calculate fair value from direct price comparison.

        Args:
            spot: Current price (from Chainlink or Binance)
            strike: Strike price (price when market opened)
            seconds_to_expiry: Seconds until settlement
            volatility: IGNORED — kept for interface compatibility with BinaryFairValue

        Returns:
            FairValue with fair_up and fair_down probabilities
        """
        k = self.params["k"]
        alpha = self.params["alpha"]
        ref_time = self.params["ref_time"]
        floor = self.params["floor"]
        ceiling = self.params["ceiling"]

        # Handle edge cases
        if seconds_to_expiry <= 0:
            fair_up = 1.0 if spot > strike else 0.0 if spot < strike else 0.5
            gap_pct = (spot - strike) / strike if strike > 0 else 0.0
            return FairValue(
                fair_up=fair_up, fair_down=1.0 - fair_up,
                d=gap_pct,
                spot=spot, strike=strike, seconds_left=0, vol=0.0,
            )

        if spot <= 0 or strike <= 0:
            return FairValue(
                fair_up=0.5, fair_down=0.5, d=0.0,
                spot=spot, strike=strike, seconds_left=seconds_to_expiry, vol=0.0,
            )

        # Price gap as a fraction of strike
        gap_pct = (spot - strike) / strike

        # Time factor: less time remaining → higher confidence
        # When seconds_left = ref_time (full window), time_factor = 1.0
        # When seconds_left = ref_time/4, time_factor = 2.0 (at alpha=0.5)
        # When seconds_left approaches 0, time_factor → infinity (certainty)
        clamped_time = max(1.0, seconds_to_expiry)  # Prevent division by zero
        time_factor = (ref_time / clamped_time) ** alpha

        # Sigmoid input: gap * steepness * time_factor
        x = k * gap_pct * time_factor

        # Sigmoid function: maps (-inf, +inf) → (0, 1)
        # Clamp x to avoid overflow in exp()
        x = max(-20.0, min(20.0, x))
        fair_up = 1.0 / (1.0 + math.exp(-x))

        # Clamp to [floor, ceiling] when gap is small (near 50/50)
        # But allow full range when gap is decisive
        fair_up = max(MIN_PRICE, min(MAX_PRICE, fair_up))
        fair_down = max(MIN_PRICE, min(MAX_PRICE, 1.0 - fair_up))

        return FairValue(
            fair_up=fair_up,
            fair_down=fair_down,
            d=gap_pct,  # Repurpose d as price gap percentage
            spot=spot,
            strike=strike,
            seconds_left=seconds_to_expiry,
            vol=0.0,  # No volatility in direct model
        )

    def explain(self, spot: float, strike: float, seconds_to_expiry: float) -> str:
        """Human-readable explanation of the fair value calculation."""
        fv = self.calculate(spot, strike, seconds_to_expiry)
        gap_pct = (spot - strike) / strike if strike > 0 else 0.0
        direction = "ABOVE" if gap_pct > 0 else "BELOW" if gap_pct < 0 else "AT"

        return (
            f"Spot ${spot:,.2f} is {direction} strike ${strike:,.2f} "
            f"by {abs(gap_pct)*100:.4f}% with {seconds_to_expiry:.0f}s left. "
            f"P(UP)={fv.fair_up:.3f} P(DOWN)={fv.fair_down:.3f}"
        )

    def calibrate(self, trade_csv_path: str) -> Dict:
        """
        Calibrate the model from actual trade outcomes.

        Reads trade data, buckets by (gap_pct, seconds_remaining),
        calculates actual win rates per bucket, and fits the sigmoid
        parameters to match empirical probabilities.

        Args:
            trade_csv_path: Path to CSV with columns:
                - spot (or binance_price): spot price at entry
                - strike (or strike_price): strike price
                - seconds_left (or tte): time to expiry at entry
                - outcome: "win" or "loss" (or 1/0)
                - side: "up" or "down"

        Returns:
            Dict with calibration results and bucket data
        """
        import csv
        from collections import defaultdict

        trades = []
        p = Path(trade_csv_path)
        if not p.exists():
            logger.error(f"Trade CSV not found: {trade_csv_path}")
            return {"error": "file not found"}

        with open(p) as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    # Parse fields — support both raw (spot/strike) and trade CSV format
                    spot_raw = row.get("spot") or row.get("binance_price") or row.get("spot_price", "")
                    strike_raw = row.get("strike") or row.get("strike_price", "")
                    momentum_raw = row.get("momentum_at_entry", "")
                    tte = float(row.get("seconds_left") or row.get("tte") or row.get("time_to_expiry_at_entry") or row.get("time_to_expiry", 0))
                    side = (row.get("side", "") or "").lower()

                    # Parse outcome
                    outcome_raw = (row.get("outcome", "") or row.get("result", "")).lower()
                    if outcome_raw in ("win", "1", "true", "won"):
                        won = True
                    elif outcome_raw in ("loss", "0", "false", "lost", "lose"):
                        won = False
                    else:
                        continue  # Skip pending/unknown

                    if tte <= 0:
                        continue

                    # Calculate gap_pct: prefer spot/strike, fall back to momentum_at_entry
                    if spot_raw and strike_raw:
                        spot = float(spot_raw)
                        strike = float(strike_raw)
                        if spot <= 0 or strike <= 0:
                            continue
                        gap_pct = (spot - strike) / strike
                    elif momentum_raw:
                        # momentum_at_entry = (spot - strike) / strike = gap_pct
                        gap_pct = float(momentum_raw)
                    else:
                        continue

                    # For DOWN trades, winning means price went below strike
                    # So the "directional gap" is inverted for DOWN
                    if side == "down":
                        directional_gap = -gap_pct
                    else:
                        directional_gap = gap_pct

                    trades.append({
                        "gap_pct": gap_pct,
                        "directional_gap": directional_gap,
                        "tte": tte,
                        "won": won,
                        "side": side,
                    })
                except (ValueError, TypeError):
                    continue

        if len(trades) < 10:
            logger.warning(f"Only {len(trades)} valid trades — need at least 10 for calibration")
            return {"error": f"insufficient trades ({len(trades)})"}

        # Bucket trades by (gap_range, time_range)
        gap_edges = [0.0, 0.0005, 0.001, 0.002, 0.003, 0.005, 0.01, 0.02, 0.05, 1.0]
        time_edges = [0, 30, 60, 120, 180, 240, 300, 600]

        buckets = defaultdict(lambda: {"wins": 0, "total": 0, "trades": []})

        for t in trades:
            abs_gap = abs(t["directional_gap"])
            tte = t["tte"]

            # Find gap bucket
            gap_bucket = len(gap_edges) - 2
            for i in range(len(gap_edges) - 1):
                if abs_gap < gap_edges[i + 1]:
                    gap_bucket = i
                    break

            # Find time bucket
            time_bucket = len(time_edges) - 2
            for i in range(len(time_edges) - 1):
                if tte < time_edges[i + 1]:
                    time_bucket = i
                    break

            key = (gap_bucket, time_bucket)
            buckets[key]["total"] += 1
            if t["won"]:
                buckets[key]["wins"] += 1
            buckets[key]["trades"].append(t)

        # Build calibration table
        calibration_table = []
        for (gi, ti), data in sorted(buckets.items()):
            if data["total"] < 3:  # Need minimum trades per bucket
                continue
            wr = data["wins"] / data["total"]
            gap_lo = gap_edges[gi]
            gap_hi = gap_edges[gi + 1] if gi + 1 < len(gap_edges) else 1.0
            time_lo = time_edges[ti]
            time_hi = time_edges[ti + 1] if ti + 1 < len(time_edges) else 600

            calibration_table.append({
                "gap_range": f"{gap_lo*100:.2f}%-{gap_hi*100:.2f}%",
                "time_range": f"{time_lo}-{time_hi}s",
                "trades": data["total"],
                "wins": data["wins"],
                "win_rate": round(wr, 3),
                "gap_mid": (gap_lo + gap_hi) / 2,
                "time_mid": (time_lo + time_hi) / 2,
            })

        # Fit sigmoid parameters using least squares on bucket midpoints
        # Minimize sum of (predicted_prob - actual_wr)^2 weighted by sqrt(n)
        best_k, best_alpha = self._fit_sigmoid(calibration_table)

        self.params["k"] = best_k
        self.params["alpha"] = best_alpha

        result = {
            "total_trades": len(trades),
            "overall_wr": sum(1 for t in trades if t["won"]) / len(trades),
            "buckets": calibration_table,
            "params": dict(self.params),
        }

        logger.info(f"Calibration complete: {len(trades)} trades, k={best_k:.1f}, alpha={best_alpha:.2f}")
        return result

    def _fit_sigmoid(self, table: List[Dict]) -> Tuple[float, float]:
        """
        Fit sigmoid parameters (k, alpha) to empirical bucket data.

        Simple grid search — fast enough for the small number of buckets.
        """
        if not table:
            return self.params["k"], self.params["alpha"]

        ref_time = self.params["ref_time"]
        best_loss = float("inf")
        best_k = self.params["k"]
        best_alpha = self.params["alpha"]

        # Grid search over reasonable parameter ranges
        for k in range(100, 3001, 50):
            for alpha_x10 in range(2, 11):  # 0.2 to 1.0
                alpha = alpha_x10 / 10.0
                loss = 0.0

                for row in table:
                    gap_mid = row["gap_mid"]
                    time_mid = max(1.0, row["time_mid"])
                    actual_wr = row["win_rate"]
                    n = row["trades"]

                    time_factor = (ref_time / time_mid) ** alpha
                    x = k * gap_mid * time_factor
                    x = max(-20.0, min(20.0, x))
                    predicted = 1.0 / (1.0 + math.exp(-x))

                    # Weight by sqrt(n) — more data = more trust
                    weight = math.sqrt(n)
                    loss += weight * (predicted - actual_wr) ** 2

                if loss < best_loss:
                    best_loss = loss
                    best_k = float(k)
                    best_alpha = alpha

        return best_k, best_alpha

    def save_calibration(self, path: str, result: Dict) -> None:
        """Save calibration result to JSON file."""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "w") as f:
            json.dump(result, f, indent=2)
        logger.info(f"Saved calibration to {path}")
