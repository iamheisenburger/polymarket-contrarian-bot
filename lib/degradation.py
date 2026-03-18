"""
Degradation Estimator — Layer 2 of the Adaptive Trading System

Compares paper trade outcomes with live trade outcomes to estimate
per-coin paper-to-live win rate degradation.

Key design:
    - Buckets defined by (coin, edge_bucket, strike_source, momentum_bucket)
    - Min sample sizes: 10 paper + 5 live per bucket before trusting
    - Falls back: bucket -> coin-level aggregate -> conservative default (5pp)
    - Rolling window via timestamp filtering
    - Serializable to JSON for persistence
    - Standalone: only reads CSVs and computes statistics

Usage:
    from lib.degradation import DegradationEstimator

    est = DegradationEstimator(window_days=14)
    est.update_from_csvs("data/paper_tmp.csv", "data/btc_live_tmp.csv")
    print(est.summary())

    # Get adjusted win rate for a coin under specific conditions
    live_wr = est.adjusted_win_rate(0.80, "BTC", edge=0.25, strike_source="vatic")
"""

import csv
import json
import logging
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Conservative default degradation when no data is available (5 percentage points)
DEFAULT_DEGRADATION_PP = 0.05

# Minimum sample sizes before trusting a bucket
MIN_PAPER_TRADES = 10
MIN_LIVE_TRADES = 5

# Edge threshold separating thin vs fat (in probability units)
EDGE_THRESHOLD = 0.20

# Momentum threshold separating low vs high (absolute price change fraction)
MOMENTUM_THRESHOLD = 0.0005


def _classify_edge(edge: float) -> str:
    """Classify edge into thin or fat bucket."""
    return "fat" if edge >= EDGE_THRESHOLD else "thin"


def _classify_momentum(momentum: float) -> str:
    """Classify absolute momentum into low or high bucket."""
    return "high" if abs(momentum) >= MOMENTUM_THRESHOLD else "low"


def _bucket_label(edge_bucket: str, strike_source: str, momentum_bucket: str) -> str:
    """Build a human-readable condition label."""
    return f"{edge_bucket}_edge_{strike_source}_{momentum_bucket}_mom"


@dataclass
class DegradationBucket:
    """Win rate comparison for a specific (coin, condition) pair."""

    coin: str
    condition_label: str  # e.g. "fat_edge_vatic_high_mom"
    paper_trades: int = 0
    paper_wins: int = 0
    live_trades: int = 0
    live_wins: int = 0

    @property
    def paper_wr(self) -> float:
        """Paper win rate as a fraction (0-1)."""
        return self.paper_wins / self.paper_trades if self.paper_trades > 0 else 0.0

    @property
    def live_wr(self) -> float:
        """Live win rate as a fraction (0-1)."""
        return self.live_wins / self.live_trades if self.live_trades > 0 else 0.0

    @property
    def degradation_pp(self) -> float:
        """Paper WR minus live WR in fractional units (e.g. 0.05 = 5pp).

        Positive means paper outperforms live (the typical case).
        Returns 0.0 if either side has no data.
        """
        if self.paper_trades == 0 or self.live_trades == 0:
            return 0.0
        return self.paper_wr - self.live_wr

    @property
    def has_sufficient_data(self) -> bool:
        """True if we have enough trades to trust this bucket."""
        return (self.paper_trades >= MIN_PAPER_TRADES and
                self.live_trades >= MIN_LIVE_TRADES)

    def to_dict(self) -> dict:
        return {
            "coin": self.coin,
            "condition_label": self.condition_label,
            "paper_trades": self.paper_trades,
            "paper_wins": self.paper_wins,
            "live_trades": self.live_trades,
            "live_wins": self.live_wins,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "DegradationBucket":
        return cls(**d)


@dataclass
class _TradeRow:
    """Minimal parsed trade row for bucketing."""

    coin: str
    outcome: str
    edge: float  # fair_value_at_entry - entry_price
    momentum: float
    strike_source: str
    timestamp: datetime


def _parse_csv(filepath: str, cutoff: Optional[datetime] = None) -> List[_TradeRow]:
    """Parse a trade CSV into minimal row objects, filtering by cutoff if given."""
    rows: List[_TradeRow] = []
    path = Path(filepath)
    if not path.exists():
        logger.warning(f"CSV not found: {filepath}")
        return rows

    try:
        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                outcome = row.get("outcome", "").strip()
                if outcome not in ("won", "lost"):
                    continue

                # Parse timestamp
                ts_str = row.get("timestamp", "")
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                except (ValueError, TypeError):
                    continue

                # Apply rolling window cutoff
                if cutoff and ts < cutoff:
                    continue

                coin = row.get("coin", "").upper()
                if not coin:
                    continue

                entry_price = float(row.get("entry_price", 0))
                fair_value = float(row.get("fair_value_at_entry", 0))
                edge = fair_value - entry_price

                momentum = float(row.get("momentum_at_entry", 0))
                strike_source = row.get("strike_source", "").strip().lower()

                rows.append(_TradeRow(
                    coin=coin,
                    outcome=outcome,
                    edge=edge,
                    momentum=momentum,
                    strike_source=strike_source,
                    timestamp=ts,
                ))
    except Exception as e:
        logger.error(f"Failed to parse CSV {filepath}: {e}")

    return rows


class DegradationEstimator:
    """Estimates paper-to-live win rate degradation per coin and condition."""

    def __init__(self, window_days: int = 14):
        self.window_days = window_days
        self._buckets: Dict[str, List[DegradationBucket]] = {}  # coin -> buckets
        self._coin_aggregates: Dict[str, DegradationBucket] = {}  # coin -> aggregate
        self._last_updated: Optional[datetime] = None

    def update_from_csvs(self, paper_csv: str, live_csv: str) -> None:
        """Rebuild the degradation model from paper and live CSV files.

        Args:
            paper_csv: Path to paper trade CSV (e.g. data/paper_tmp.csv)
            live_csv: Path to live trade CSV (e.g. data/btc_live_tmp.csv)
        """
        cutoff = None
        if self.window_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=self.window_days)

        paper_rows = _parse_csv(paper_csv, cutoff)
        live_rows = _parse_csv(live_csv, cutoff)

        # Build bucket keys and accumulate counts
        # key = (coin, edge_bucket, strike_source, momentum_bucket)
        paper_counts: Dict[Tuple[str, str, str, str], Tuple[int, int]] = {}
        live_counts: Dict[Tuple[str, str, str, str], Tuple[int, int]] = {}

        for row in paper_rows:
            key = (row.coin, _classify_edge(row.edge),
                   row.strike_source, _classify_momentum(row.momentum))
            trades, wins = paper_counts.get(key, (0, 0))
            paper_counts[key] = (trades + 1, wins + (1 if row.outcome == "won" else 0))

        for row in live_rows:
            key = (row.coin, _classify_edge(row.edge),
                   row.strike_source, _classify_momentum(row.momentum))
            trades, wins = live_counts.get(key, (0, 0))
            live_counts[key] = (trades + 1, wins + (1 if row.outcome == "won" else 0))

        # Merge into DegradationBucket objects
        all_keys = set(paper_counts.keys()) | set(live_counts.keys())
        buckets_by_coin: Dict[str, List[DegradationBucket]] = {}

        for key in all_keys:
            coin, edge_b, strike_src, mom_b = key
            label = _bucket_label(edge_b, strike_src, mom_b)
            pt, pw = paper_counts.get(key, (0, 0))
            lt, lw = live_counts.get(key, (0, 0))

            bucket = DegradationBucket(
                coin=coin,
                condition_label=label,
                paper_trades=pt,
                paper_wins=pw,
                live_trades=lt,
                live_wins=lw,
            )

            if coin not in buckets_by_coin:
                buckets_by_coin[coin] = []
            buckets_by_coin[coin].append(bucket)

        self._buckets = buckets_by_coin

        # Build coin-level aggregates
        self._coin_aggregates = {}
        for coin, bucket_list in buckets_by_coin.items():
            agg = DegradationBucket(coin=coin, condition_label="aggregate")
            for b in bucket_list:
                agg.paper_trades += b.paper_trades
                agg.paper_wins += b.paper_wins
                agg.live_trades += b.live_trades
                agg.live_wins += b.live_wins
            self._coin_aggregates[coin] = agg

        self._last_updated = datetime.now(timezone.utc)
        logger.info(f"Degradation model updated: {len(all_keys)} buckets across "
                    f"{len(buckets_by_coin)} coins "
                    f"(paper={len(paper_rows)}, live={len(live_rows)} trades)")

    def get_degradation(self, coin: str, edge: float = 0.20,
                        momentum: float = 0.001,
                        strike_source: str = "vatic") -> float:
        """Get expected WR degradation in fractional units for given conditions.

        Returns a positive value meaning paper WR is higher than live WR.
        E.g., returns 0.05 means paper 75% -> live 70%.

        Fallback chain:
            1. Matching bucket with sufficient data
            2. Coin-level aggregate with sufficient data
            3. Conservative default (DEFAULT_DEGRADATION_PP)
        """
        coin = coin.upper()
        edge_b = _classify_edge(edge)
        mom_b = _classify_momentum(momentum)
        strike_src = strike_source.lower()
        target_label = _bucket_label(edge_b, strike_src, mom_b)

        # Try exact bucket match
        for bucket in self._buckets.get(coin, []):
            if bucket.condition_label == target_label and bucket.has_sufficient_data:
                return max(bucket.degradation_pp, 0.0)

        # Fall back to coin-level aggregate
        agg = self._coin_aggregates.get(coin)
        if agg and agg.has_sufficient_data:
            return max(agg.degradation_pp, 0.0)

        # No data -- use conservative default
        return DEFAULT_DEGRADATION_PP

    def adjusted_win_rate(self, paper_wr: float, coin: str, **conditions) -> float:
        """Apply degradation to paper win rate to estimate live WR.

        Args:
            paper_wr: Paper win rate as a fraction (e.g. 0.80 for 80%)
            coin: Coin symbol (e.g. "BTC")
            **conditions: Passed to get_degradation (edge, momentum, strike_source)

        Returns:
            Estimated live win rate as a fraction, floored at 0.0.
        """
        deg = self.get_degradation(coin, **conditions)
        return max(paper_wr - deg, 0.0)

    def save(self, path: str = "data/degradation_model.json") -> None:
        """Persist model to JSON."""
        filepath = Path(path)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "window_days": self.window_days,
            "last_updated": self._last_updated.isoformat() if self._last_updated else None,
            "buckets": {},
            "coin_aggregates": {},
        }

        for coin, bucket_list in self._buckets.items():
            data["buckets"][coin] = [b.to_dict() for b in bucket_list]

        for coin, agg in self._coin_aggregates.items():
            data["coin_aggregates"][coin] = agg.to_dict()

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Degradation model saved to {filepath}")

    def load(self, path: str = "data/degradation_model.json") -> None:
        """Load model from JSON."""
        filepath = Path(path)
        if not filepath.exists():
            logger.warning(f"No degradation model at {filepath}")
            return

        with open(filepath, "r") as f:
            data = json.load(f)

        self.window_days = data.get("window_days", 14)

        ts_str = data.get("last_updated")
        if ts_str:
            self._last_updated = datetime.fromisoformat(ts_str)
        else:
            self._last_updated = None

        self._buckets = {}
        for coin, bucket_dicts in data.get("buckets", {}).items():
            self._buckets[coin] = [DegradationBucket.from_dict(d) for d in bucket_dicts]

        self._coin_aggregates = {}
        for coin, agg_dict in data.get("coin_aggregates", {}).items():
            self._coin_aggregates[coin] = DegradationBucket.from_dict(agg_dict)

        logger.info(f"Degradation model loaded from {filepath} "
                    f"({sum(len(v) for v in self._buckets.values())} buckets)")

    def summary(self) -> str:
        """Human-readable summary of degradation per coin."""
        if not self._buckets:
            return "Degradation model: no data loaded."

        lines = [
            "=== Degradation Estimator ===",
            f"Window: {self.window_days} days | "
            f"Updated: {self._last_updated.strftime('%Y-%m-%d %H:%M UTC') if self._last_updated else 'never'}",
            "",
        ]

        for coin in sorted(self._buckets.keys()):
            agg = self._coin_aggregates.get(coin)
            if not agg:
                continue

            lines.append(f"--- {coin} (aggregate) ---")
            lines.append(
                f"  Paper: {agg.paper_wr*100:.1f}% ({agg.paper_wins}/{agg.paper_trades}) | "
                f"Live: {agg.live_wr*100:.1f}% ({agg.live_wins}/{agg.live_trades}) | "
                f"Degradation: {agg.degradation_pp*100:+.1f}pp"
                f"{'' if agg.has_sufficient_data else ' [INSUFFICIENT DATA]'}"
            )

            # Show per-bucket detail
            sufficient = [b for b in self._buckets[coin] if b.has_sufficient_data]
            insufficient = [b for b in self._buckets[coin] if not b.has_sufficient_data]

            if sufficient:
                for b in sorted(sufficient, key=lambda x: x.condition_label):
                    lines.append(
                        f"  [{b.condition_label}] "
                        f"paper {b.paper_wr*100:.0f}%({b.paper_trades}) "
                        f"live {b.live_wr*100:.0f}%({b.live_trades}) "
                        f"deg {b.degradation_pp*100:+.1f}pp"
                    )

            if insufficient:
                labels = ", ".join(b.condition_label for b in insufficient)
                lines.append(f"  (insufficient data: {labels})")

            lines.append("")

        return "\n".join(lines)
