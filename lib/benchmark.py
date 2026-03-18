"""
Rolling Benchmark System — Tracks strategy performance over time.

Benchmark becomes 'official' at 100 trades, 'provisional' before that.
Used by discovery layer to evaluate whether candidate filters beat current strategy.

Usage:
    from lib.benchmark import StrategyBenchmark

    bench = StrategyBenchmark("v4_ema_4_16", "data/benchmark.json")
    bench.update_from_trades("data/live_trades.csv", CoinManager.FIXED_FILTERS)
    print(bench.summary())
"""

import csv
import json
import math
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

PROVISIONAL_THRESHOLD = 100  # trades needed for official benchmark


class StrategyBenchmark:
    """Tracks rolling performance of the active strategy."""

    def __init__(self, strategy_name: str, csv_path: str):
        self.strategy_name = strategy_name
        self.csv_path = csv_path  # path to save/load benchmark state
        self.total_trades: int = 0
        self.wins: int = 0
        self.pnl: float = 0.0
        self.avg_entry: float = 0.0
        self._entry_sum: float = 0.0
        self._updated_at: Optional[str] = None

    def update_from_trades(self, trade_csv: str, fixed_filters: dict):
        """Read trade CSV, filter to fixed filters, update stats."""
        path = Path(trade_csv)
        if not path.exists():
            logger.warning(f"Trade CSV not found: {trade_csv}")
            return

        self.total_trades = 0
        self.wins = 0
        self.pnl = 0.0
        self._entry_sum = 0.0

        with open(path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                outcome = row.get("outcome", "").strip()
                if outcome not in ("won", "lost"):
                    continue

                try:
                    entry_price = float(row.get("entry_price", 0))
                    fair_value = float(row.get("fair_value_at_entry", 0))
                    edge = fair_value - entry_price
                    momentum = abs(float(row.get("momentum_at_entry", 0)))
                    tte = float(row.get("time_to_expiry_at_entry", 0))
                    pnl_val = float(row.get("pnl", 0))
                except (ValueError, TypeError):
                    continue

                # Apply fixed filters
                if edge < fixed_filters.get('min_edge', 0):
                    continue
                if entry_price < fixed_filters.get('min_entry_price', 0):
                    continue
                if entry_price > fixed_filters.get('max_entry_price', 1):
                    continue
                if momentum < fixed_filters.get('min_momentum', 0):
                    continue
                if fair_value < fixed_filters.get('min_fair_value', 0):
                    continue

                min_tte = 300 - fixed_filters.get('max_window_elapsed', 300)
                max_tte = 300 - fixed_filters.get('min_window_elapsed', 0)
                if tte < min_tte or tte > max_tte:
                    continue

                self.total_trades += 1
                self._entry_sum += entry_price
                self.pnl += pnl_val
                if outcome == "won":
                    self.wins += 1

        self.avg_entry = self._entry_sum / self.total_trades if self.total_trades > 0 else 0.0
        self._updated_at = datetime.now(timezone.utc).isoformat()
        logger.info(
            f"Benchmark updated: {self.total_trades} trades, "
            f"WR={self.get_wr():.1%}, Wilson={self.get_wilson_lower():.1%}"
        )

    def is_official(self) -> bool:
        """Has this benchmark accumulated 100+ trades?"""
        return self.total_trades >= PROVISIONAL_THRESHOLD

    def get_wr(self) -> float:
        """Current win rate."""
        return self.wins / self.total_trades if self.total_trades > 0 else 0.0

    def get_wilson_lower(self) -> float:
        """95% confidence lower bound on WR."""
        return _wilson_lower(self.wins, self.total_trades)

    def beats_benchmark(self, candidate_wr: float, candidate_n: int, margin: float = 0.05) -> bool:
        """Does the candidate beat this benchmark by `margin` pp?

        Both candidate and benchmark must have 100+ trades.
        Compares Wilson lower bounds (conservative).
        """
        if candidate_n < PROVISIONAL_THRESHOLD:
            return False
        if not self.is_official():
            return False

        candidate_wins = round(candidate_wr * candidate_n)
        candidate_wilson = _wilson_lower(candidate_wins, candidate_n)
        bench_wilson = self.get_wilson_lower()

        return candidate_wilson > bench_wilson + margin

    def summary(self) -> str:
        """Human readable status."""
        status = "OFFICIAL" if self.is_official() else f"PROVISIONAL ({self.total_trades}/{PROVISIONAL_THRESHOLD})"
        lines = [
            "=" * 50,
            f"  BENCHMARK: {self.strategy_name}",
            f"  Status: {status}",
            "=" * 50,
            f"  Trades:       {self.total_trades}",
            f"  Wins:         {self.wins}",
            f"  Win Rate:     {self.get_wr():.1%}",
            f"  Wilson Lower: {self.get_wilson_lower():.1%}",
            f"  PnL:          ${self.pnl:+.2f}",
            f"  Avg Entry:    ${self.avg_entry:.3f}",
        ]
        if not self.is_official():
            lines.append(f"  WARNING: Provisional — need {PROVISIONAL_THRESHOLD - self.total_trades} more trades")
        if self._updated_at:
            lines.append(f"  Updated:      {self._updated_at}")
        lines.append("=" * 50)
        return "\n".join(lines)

    def save(self):
        """Save benchmark state to JSON."""
        path = Path(self.csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "strategy_name": self.strategy_name,
            "total_trades": self.total_trades,
            "wins": self.wins,
            "pnl": round(self.pnl, 4),
            "avg_entry": round(self.avg_entry, 4),
            "updated_at": self._updated_at,
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load(self):
        """Load benchmark state from JSON."""
        path = Path(self.csv_path)
        if not path.exists():
            logger.info(f"No benchmark file found at {self.csv_path}")
            return
        with open(path, "r") as f:
            data = json.load(f)
        self.strategy_name = data.get("strategy_name", self.strategy_name)
        self.total_trades = data.get("total_trades", 0)
        self.wins = data.get("wins", 0)
        self.pnl = data.get("pnl", 0.0)
        self.avg_entry = data.get("avg_entry", 0.0)
        self._updated_at = data.get("updated_at")


def _wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
    """Wilson score lower bound for win rate (95% CI)."""
    if total == 0:
        return 0.0
    p = wins / total
    denominator = 1 + z ** 2 / total
    centre = (p + z ** 2 / (2 * total)) / denominator
    spread = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * total)) / total) / denominator
    return centre - spread
