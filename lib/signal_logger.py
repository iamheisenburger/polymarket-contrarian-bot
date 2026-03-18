"""
Signal Logger - CSV-based Signal Evaluation Logging

Logs EVERY signal the strategy evaluates (not just the ones that pass filters).
This is Layer 1 of the adaptive trading system: raw data capture for offline
optimization of filter thresholds.

Each row = one coin/side evaluation per market window. Records fair value,
best ask, edge, and which filters passed/failed. The optimizer can then
replay alternative filter configs against this data without needing to
re-run the strategy.

Usage:
    from lib.signal_logger import SignalLogger

    logger = SignalLogger(log_dir="data")
    logger.log_signal(record)
    # Later, when market resolves:
    logger.resolve_outcome(market_slug, side, won=True, pnl=0.60)
"""

import csv
import logging
import os
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Set

logger = logging.getLogger(__name__)


@dataclass
class SignalRecord:
    """Single signal evaluation record."""

    timestamp: str
    coin: str
    market_slug: str
    side: str  # "up" or "down"
    spot_price: float
    strike_price: float
    strike_source: str
    fair_value: float
    best_ask: float
    edge: float
    momentum: float
    volatility: float
    vol_source: str
    time_to_expiry: float
    ema_trend: str  # "bullish", "bearish", or "none"
    entry_price: float

    # Filter results (True = passed, False = failed)
    passed_edge_filter: bool = False
    passed_price_filter: bool = False
    passed_momentum_filter: bool = False
    passed_tte_filter: bool = False
    passed_fv_filter: bool = False
    passed_vatic_filter: bool = False
    passed_trend_filter: bool = False

    # Outcome (filled in later by resolve_outcome)
    outcome: str = "pending"  # "won", "lost", "pending"
    pnl: float = 0.0

    @property
    def signal_key(self) -> str:
        """Unique key: slug + side."""
        return f"{self.market_slug}:{self.side}"


CSV_HEADERS = [
    "timestamp", "coin", "market_slug", "side",
    "spot_price", "strike_price", "strike_source",
    "fair_value", "best_ask", "edge",
    "momentum", "volatility", "vol_source",
    "time_to_expiry", "ema_trend", "entry_price",
    "passed_edge_filter", "passed_price_filter", "passed_momentum_filter",
    "passed_tte_filter", "passed_fv_filter", "passed_vatic_filter",
    "passed_trend_filter",
    "outcome", "pnl",
]


class SignalLogger:
    """
    CSV-based signal logger — one file per coin.

    Records every signal evaluation so the optimizer can replay alternative
    filter configurations offline. Deduplicates by slug:side to avoid
    flooding the CSV with repeated evaluations of the same opportunity.
    """

    def __init__(self, log_dir: str = "data"):
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)

        # Track which slug:side combos we've already logged this session
        # to avoid duplicate rows for the same opportunity
        self._logged_keys: Set[str] = set()

    def _csv_path(self, coin: str) -> Path:
        """Get CSV path for a coin."""
        return self.log_dir / f"signals_{coin.upper()}.csv"

    def _ensure_headers(self, coin: str) -> None:
        """Write CSV headers if file doesn't exist."""
        path = self._csv_path(coin)
        if not path.exists():
            with open(path, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(CSV_HEADERS)

    def log_signal(self, record: SignalRecord) -> None:
        """
        Log a signal evaluation to the per-coin CSV.

        Deduplicates by slug:side — only logs once per market window per side.
        """
        key = record.signal_key
        if key in self._logged_keys:
            return  # Already logged this slug:side

        self._logged_keys.add(key)
        self._ensure_headers(record.coin)

        try:
            path = self._csv_path(record.coin)
            with open(path, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    record.timestamp,
                    record.coin,
                    record.market_slug,
                    record.side,
                    f"{record.spot_price:.2f}",
                    f"{record.strike_price:.2f}",
                    record.strike_source,
                    f"{record.fair_value:.6f}",
                    f"{record.best_ask:.4f}",
                    f"{record.edge:.4f}",
                    f"{record.momentum:.8f}",
                    f"{record.volatility:.6f}",
                    record.vol_source,
                    f"{record.time_to_expiry:.0f}",
                    record.ema_trend,
                    f"{record.entry_price:.4f}",
                    int(record.passed_edge_filter),
                    int(record.passed_price_filter),
                    int(record.passed_momentum_filter),
                    int(record.passed_tte_filter),
                    int(record.passed_fv_filter),
                    int(record.passed_vatic_filter),
                    int(record.passed_trend_filter),
                    record.outcome,
                    f"{record.pnl:.2f}",
                ])
        except Exception as e:
            logger.error(f"Failed to write signal to CSV: {e}")

    def resolve_outcome(
        self, market_slug: str, side: str, won: bool, pnl: float = 0.0
    ) -> None:
        """
        Update outcome for a resolved market in all coin CSVs.

        Reads the CSV, updates matching rows, rewrites. Only touches rows
        where outcome is still 'pending'.
        """
        # Find which coin file has this slug
        for csv_file in self.log_dir.glob("signals_*.csv"):
            try:
                rows = []
                modified = False
                with open(csv_file, "r", newline="") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        if (row["market_slug"] == market_slug
                                and row["side"] == side
                                and row["outcome"] == "pending"):
                            row["outcome"] = "won" if won else "lost"
                            row["pnl"] = f"{pnl:.2f}"
                            modified = True
                        rows.append(row)

                if modified and fieldnames:
                    with open(csv_file, "w", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(rows)
            except Exception as e:
                logger.error(f"Failed to resolve outcome in {csv_file}: {e}")

    def clear_slug(self, market_slug: str) -> None:
        """Remove a slug from the dedup set (call on market rotation)."""
        keys_to_remove = [k for k in self._logged_keys if k.startswith(f"{market_slug}:")]
        for k in keys_to_remove:
            self._logged_keys.discard(k)

    def cleanup(self, days: int = 14) -> None:
        """
        Remove signal rows older than `days` from all CSV files.

        Rolling window cleanup to prevent unbounded file growth.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)

        for csv_file in self.log_dir.glob("signals_*.csv"):
            try:
                rows = []
                with open(csv_file, "r", newline="") as f:
                    reader = csv.DictReader(f)
                    fieldnames = reader.fieldnames
                    for row in reader:
                        try:
                            ts = datetime.fromisoformat(row["timestamp"])
                            if ts >= cutoff:
                                rows.append(row)
                        except (ValueError, KeyError):
                            rows.append(row)  # Keep rows with unparseable timestamps

                if fieldnames:
                    with open(csv_file, "w", newline="") as f:
                        writer = csv.DictWriter(f, fieldnames=fieldnames)
                        writer.writeheader()
                        writer.writerows(rows)
            except Exception as e:
                logger.error(f"Failed to cleanup {csv_file}: {e}")
