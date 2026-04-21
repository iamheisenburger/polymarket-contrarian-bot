"""
Empirical Edge Model — calibrated on IC data with adverse selection adjustment.

Uses integrated collector (IC) data to build a lookup table of actual win rates
by (entry_price, elapsed, momentum) bucket. Compares to the market's implied
probability (= entry price) to compute edge.

This is NOT Black-Scholes. NOT a theoretical model. It's pure empirical:
"what actually happens when signals look like this?"

Usage:
    model = EdgeModel.from_csv("data/vps_ic_latest.csv")
    edge = model.get_edge(entry_price=0.83, elapsed=50, momentum=0.001)
    # edge = -0.15 → NO TRADE (our WR is 68% but we're paying 83%)

    edge = model.get_edge(entry_price=0.58, elapsed=180, momentum=0.001)
    # edge = +0.29 → TRADE (our WR is 87% but only paying 58%)
"""

import csv
import logging
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# Dynamic adverse selection model.
# AS is a function of all 3 variables: (entry_price, elapsed, momentum).
#
# Key findings from 524 live vs 37K IC comparison:
#   - Late entry (>=120s) reduces AS dramatically at mid prices
#   - High momentum (>=0.08%) reduces AS when controlling for price
#   - Cheap tokens (<$0.55) have high AS regardless of timing/momentum
#   - $0.75+ has high AS regardless of timing/momentum
#   - The sweet spot $0.55-0.75 + late + high mom has near-zero AS
#
# Model: 3D lookup table (entry_price_zone × elapsed_zone × momentum_zone)
# Calibrated from live vs IC comparison. Falls back to 2D/1D when
# 3D bucket has insufficient data.
#
# AS values are floored at 0 (we never assume we BEAT IC as a baseline).

# 3D AS table: {(ep_zone, el_zone, mom_zone): haircut}
# ep_zone: 0=<0.55, 1=0.55-0.65, 2=0.65-0.75, 3=0.75+
# el_zone: 0=early (<120s), 1=late (>=120s)
# mom_zone: 0=low (<0.0008), 1=high (>=0.0008)
AS_TABLE_3D = {
    # early + low momentum
    (0, 0, 0): 0.20,   # <$0.55, early, low mom: heavy AS
    (1, 0, 0): 0.07,   # $0.55-0.65, early, low mom: measured +7.2%
    (2, 0, 0): 0.07,   # $0.65-0.75, early, low mom: measured +6.6%
    (3, 0, 0): 0.11,   # $0.75+, early, low mom: measured +11.1%

    # early + high momentum
    (0, 0, 1): 0.08,   # <$0.55, early, high mom: measured +7.7% (n=9)
    (1, 0, 1): 0.00,   # $0.55-0.65, early, high mom: measured -2.2% → floor to 0
    (2, 0, 1): 0.16,   # $0.65-0.75, early, high mom: measured +15.8% (n=120)
    (3, 0, 1): 0.11,   # $0.75+, early, high mom: measured +11.1%

    # late + low momentum
    (0, 1, 0): 0.20,   # <$0.55, late, low mom: measured +19.8% (n=12)
    (1, 1, 0): 0.00,   # $0.55-0.65, late, low mom: insufficient data → conservative 0
    (2, 1, 0): 0.15,   # $0.65-0.75, late, low mom: measured +14.9% (n=6)
    (3, 1, 0): 0.09,   # $0.75+, late, low mom: interpolated

    # late + high momentum — YOUR TRADING ZONE
    (0, 1, 1): 0.20,   # <$0.55, late, high mom: too few trades, stay conservative
    (1, 1, 1): 0.06,   # $0.55-0.65, late, high mom: measured +5.8% (n=11)
    (2, 1, 1): 0.00,   # $0.65-0.75, late, high mom: measured -2.2% (n=38) → 0
    (3, 1, 1): 0.10,   # $0.75+, late, high mom: measured +10.1% (n=33)
}


def _get_as_haircut(entry_price: float, elapsed: float = 0.0, momentum: float = 0.0) -> float:
    """
    Return the adverse selection haircut as a function of all 3 variables.

    Uses a 3D lookup table calibrated from live vs IC comparison.
    AS is the expected gap between IC win rate and our actual live win rate,
    driven by faster bots, thin books, and fill quality degradation.
    """
    # Entry price zone
    if entry_price < 0.55:
        ep_z = 0
    elif entry_price < 0.65:
        ep_z = 1
    elif entry_price < 0.75:
        ep_z = 2
    else:
        ep_z = 3

    # Elapsed zone
    el_z = 1 if elapsed >= 120.0 else 0

    # Momentum zone
    mom_z = 1 if momentum >= 0.0008 else 0

    return AS_TABLE_3D.get((ep_z, el_z, mom_z), 0.10)  # default 10% if missing


def _bucket_key(entry_price: float, elapsed: float, momentum: float) -> Tuple[int, int, int]:
    """
    Map continuous values to discrete bucket indices.

    Entry price: 5-cent bins (0.00-0.05 = 0, 0.05-0.10 = 1, etc.)
    Elapsed: 30-second bins (0-30 = 0, 30-60 = 1, etc.)
    Momentum: log-scale bins matching IC collector thresholds
    """
    ep_idx = min(int(entry_price * 20), 19)  # 5-cent bins, 0-19
    el_idx = min(int(elapsed / 30), 9)       # 30s bins, 0-9

    # Momentum bins aligned with IC thresholds
    if momentum < 0.0003:
        mom_idx = 0
    elif momentum < 0.0005:
        mom_idx = 1
    elif momentum < 0.0008:
        mom_idx = 2
    elif momentum < 0.001:
        mom_idx = 3
    elif momentum < 0.002:
        mom_idx = 4
    elif momentum < 0.005:
        mom_idx = 5
    else:
        mom_idx = 6

    return (ep_idx, el_idx, mom_idx)


class EdgeModel:
    """
    Empirical edge calculator calibrated on IC data.

    Stores a 3D lookup table: (entry_price_bin, elapsed_bin, momentum_bin) → win_rate.
    Edge = estimated_live_WR - entry_price.
    """

    def __init__(self):
        # {(ep_idx, el_idx, mom_idx): (wins, total)}
        self._buckets = {}
        # Fallback: 2D tables when 3D bucket is sparse
        self._ep_el_buckets = {}   # {(ep_idx, el_idx): (wins, total)}
        self._el_mom_buckets = {}  # {(el_idx, mom_idx): (wins, total)}
        self._loaded = False
        self._total_signals = 0
        self._min_bucket_size = 10  # minimum trades in a bucket to trust it

    @classmethod
    def from_csv(cls, csv_path: str, min_bucket_size: int = 10) -> "EdgeModel":
        """Build the model from an IC CSV file."""
        model = cls()
        model._min_bucket_size = min_bucket_size

        wins_3d = {}
        total_3d = {}
        wins_ep_el = {}
        total_ep_el = {}
        wins_el_mom = {}
        total_el_mom = {}

        count = 0
        skipped = 0

        try:
            with open(csv_path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Only use momentum-side signals (is_momentum_side=True)
                    if row.get("is_momentum_side", "").lower() not in ("true", "1"):
                        continue
                    # Only use resolved outcomes
                    outcome = row.get("outcome", "")
                    if outcome not in ("won", "lost"):
                        continue

                    try:
                        entry_price = float(row["entry_price"])
                        elapsed = float(row["elapsed"])
                        momentum = float(row["momentum"])
                    except (ValueError, KeyError):
                        skipped += 1
                        continue

                    # Skip invalid entries
                    if entry_price <= 0 or entry_price >= 1.0:
                        continue

                    won = 1 if outcome == "won" else 0
                    count += 1

                    # 3D bucket
                    key_3d = _bucket_key(entry_price, elapsed, momentum)
                    wins_3d[key_3d] = wins_3d.get(key_3d, 0) + won
                    total_3d[key_3d] = total_3d.get(key_3d, 0) + 1

                    # 2D fallbacks
                    key_ep_el = (key_3d[0], key_3d[1])
                    wins_ep_el[key_ep_el] = wins_ep_el.get(key_ep_el, 0) + won
                    total_ep_el[key_ep_el] = total_ep_el.get(key_ep_el, 0) + 1

                    key_el_mom = (key_3d[1], key_3d[2])
                    wins_el_mom[key_el_mom] = wins_el_mom.get(key_el_mom, 0) + won
                    total_el_mom[key_el_mom] = total_el_mom.get(key_el_mom, 0) + 1

        except FileNotFoundError:
            logger.error(f"EdgeModel: IC data file not found: {csv_path}")
            return model
        except Exception as e:
            logger.error(f"EdgeModel: Error loading {csv_path}: {e}")
            return model

        # Store as tuples
        model._buckets = {k: (wins_3d[k], total_3d[k]) for k in total_3d}
        model._ep_el_buckets = {k: (wins_ep_el[k], total_ep_el[k]) for k in total_ep_el}
        model._el_mom_buckets = {k: (wins_el_mom[k], total_el_mom[k]) for k in total_el_mom}
        model._total_signals = count
        model._loaded = True

        n_buckets_3d = sum(1 for v in model._buckets.values() if v[1] >= min_bucket_size)
        logger.info(
            f"EdgeModel loaded: {count} IC signals, "
            f"{n_buckets_3d} usable 3D buckets (>= {min_bucket_size} samples)"
        )

        return model

    @property
    def loaded(self) -> bool:
        return self._loaded

    def get_ic_win_rate(self, entry_price: float, elapsed: float, momentum: float) -> Optional[float]:
        """
        Look up the empirical IC win rate for a signal.

        Uses 3D bucket first, falls back to 2D if sparse.
        Returns None if no bucket has enough data.
        """
        key_3d = _bucket_key(entry_price, elapsed, momentum)

        # Try 3D bucket first
        bucket = self._buckets.get(key_3d)
        if bucket and bucket[1] >= self._min_bucket_size:
            return bucket[0] / bucket[1]

        # Fallback to entry_price x elapsed (most predictive pair)
        key_ep_el = (key_3d[0], key_3d[1])
        bucket = self._ep_el_buckets.get(key_ep_el)
        if bucket and bucket[1] >= self._min_bucket_size:
            return bucket[0] / bucket[1]

        # Fallback to elapsed x momentum
        key_el_mom = (key_3d[1], key_3d[2])
        bucket = self._el_mom_buckets.get(key_el_mom)
        if bucket and bucket[1] >= self._min_bucket_size:
            return bucket[0] / bucket[1]

        return None

    def get_edge(self, entry_price: float, elapsed: float, momentum: float) -> Optional[float]:
        """
        Compute estimated live edge for a signal.

        edge = estimated_live_WR - entry_price
        estimated_live_WR = IC_WR - AS_haircut

        Returns None if insufficient IC data for this bucket.
        Positive edge = trade is +EV. Negative = skip.
        """
        ic_wr = self.get_ic_win_rate(entry_price, elapsed, momentum)
        if ic_wr is None:
            return None

        as_haircut = _get_as_haircut(entry_price, elapsed, momentum)
        est_live_wr = ic_wr - as_haircut
        edge = est_live_wr - entry_price

        return edge

    def get_detail(self, entry_price: float, elapsed: float, momentum: float) -> dict:
        """
        Get full edge breakdown for logging/debugging.

        Returns dict with: ic_wr, as_haircut, est_live_wr, market_prob, edge, bucket_n
        """
        key_3d = _bucket_key(entry_price, elapsed, momentum)

        ic_wr = None
        bucket_n = 0
        bucket_type = "none"

        # Try 3D
        bucket = self._buckets.get(key_3d)
        if bucket and bucket[1] >= self._min_bucket_size:
            ic_wr = bucket[0] / bucket[1]
            bucket_n = bucket[1]
            bucket_type = "3d"
        else:
            # Try ep x el
            key_ep_el = (key_3d[0], key_3d[1])
            bucket = self._ep_el_buckets.get(key_ep_el)
            if bucket and bucket[1] >= self._min_bucket_size:
                ic_wr = bucket[0] / bucket[1]
                bucket_n = bucket[1]
                bucket_type = "ep_el"
            else:
                # Try el x mom
                key_el_mom = (key_3d[1], key_3d[2])
                bucket = self._el_mom_buckets.get(key_el_mom)
                if bucket and bucket[1] >= self._min_bucket_size:
                    ic_wr = bucket[0] / bucket[1]
                    bucket_n = bucket[1]
                    bucket_type = "el_mom"

        if ic_wr is None:
            return {
                "ic_wr": None,
                "as_haircut": _get_as_haircut(entry_price, elapsed, momentum),
                "est_live_wr": None,
                "market_prob": entry_price,
                "edge": None,
                "bucket_n": 0,
                "bucket_type": "none",
            }

        as_haircut = _get_as_haircut(entry_price, elapsed, momentum)
        est_live_wr = ic_wr - as_haircut
        edge = est_live_wr - entry_price

        return {
            "ic_wr": round(ic_wr, 4),
            "as_haircut": as_haircut,
            "est_live_wr": round(est_live_wr, 4),
            "market_prob": entry_price,
            "edge": round(edge, 4),
            "bucket_n": bucket_n,
            "bucket_type": bucket_type,
        }

    def summary(self) -> str:
        """Human-readable model summary."""
        if not self._loaded:
            return "EdgeModel: NOT LOADED"

        n3d = sum(1 for v in self._buckets.values() if v[1] >= self._min_bucket_size)
        n2d = sum(1 for v in self._ep_el_buckets.values() if v[1] >= self._min_bucket_size)
        return (
            f"EdgeModel: {self._total_signals} IC signals, "
            f"{n3d} 3D buckets, {n2d} 2D fallbacks "
            f"(min_n={self._min_bucket_size})"
        )
