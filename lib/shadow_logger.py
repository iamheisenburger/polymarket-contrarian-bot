"""
Shadow Logger — Paper tracking alongside live trading on the SAME markets.

Logs what paper WOULD have done on every market the live bot evaluates.
For every qualifying signal, records the paper entry at the current ask price.
When a live trade fills, records the actual fill price and latency.
When a market settles, updates both paper and live outcomes.

This provides matched-pair degradation: for every live trade, what would
paper have done on the exact same market with the exact same signal?

Usage:
    from lib.shadow_logger import ShadowLogger

    shadow = ShadowLogger("data/shadow.csv")
    shadow.log_signal("btc-up-5m-123", "BTC", "up", ask=0.62, fv=0.85, ...)
    shadow.log_live_fill("btc-up-5m-123", "BTC", "up", fill_price=0.62, latency_ms=340)
    shadow.resolve("btc-up-5m-123", "up", won=True)
"""

import csv
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# CSV columns for the shadow log
SHADOW_COLUMNS = [
    "timestamp",
    "market_slug",
    "coin",
    "side",
    "paper_entry_price",
    "paper_outcome",
    "paper_pnl",
    "live_entry_price",
    "live_outcome",
    "live_pnl",
    "was_live_trade",
    "fok_rejected",
    "signal_edge",
    "signal_momentum",
    "signal_fv",
    "signal_tte",
    "strike_source",
    "latency_ms",
]


class ShadowLogger:
    """Logs what paper WOULD have done on every market the live bot evaluates."""

    def __init__(self, csv_path: str):
        self.csv_path = csv_path
        # In-memory index: (market_slug, side) -> row index in the CSV
        # Used to update rows when live fills or settlements come in.
        self._pending: Dict[str, dict] = {}  # key = "slug:side"
        self._ensure_csv()

    def _ensure_csv(self):
        """Create CSV with headers if it doesn't exist."""
        path = Path(self.csv_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists() or path.stat().st_size == 0:
            with open(path, "w", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=SHADOW_COLUMNS)
                writer.writeheader()

    def _key(self, market_slug: str, side: str) -> str:
        return f"{market_slug}:{side}"

    def log_signal(
        self,
        market_slug: str,
        coin: str,
        side: str,
        ask_price: float,
        fair_value: float,
        edge: float,
        momentum: float,
        tte: float,
        strike_source: str,
    ):
        """Called for EVERY signal that passes filters, whether live trades it or not.

        Records paper entry at current ask price. This is the shadow record.
        """
        key = self._key(market_slug, side)

        # Don't double-log the same signal in the same market
        if key in self._pending:
            return

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "market_slug": market_slug,
            "coin": coin.upper(),
            "side": side,
            "paper_entry_price": round(ask_price, 4),
            "paper_outcome": "pending",
            "paper_pnl": 0.0,
            "live_entry_price": 0.0,
            "live_outcome": "",
            "live_pnl": 0.0,
            "was_live_trade": False,
            "fok_rejected": False,
            "signal_edge": round(edge, 4),
            "signal_momentum": round(momentum, 6),
            "signal_fv": round(fair_value, 4),
            "signal_tte": round(tte, 1),
            "strike_source": strike_source,
            "latency_ms": 0.0,
        }
        self._pending[key] = record

    def log_live_fill(
        self,
        market_slug: str,
        coin: str,
        side: str,
        fill_price: float,
        latency_ms: float,
    ):
        """Called when a live trade actually fills. Updates the shadow record."""
        key = self._key(market_slug, side)
        record = self._pending.get(key)
        if not record:
            # Signal wasn't logged (e.g., shadow logger added mid-session)
            # Create a minimal record
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "market_slug": market_slug,
                "coin": coin.upper(),
                "side": side,
                "paper_entry_price": round(fill_price, 4),
                "paper_outcome": "pending",
                "paper_pnl": 0.0,
                "live_entry_price": 0.0,
                "live_outcome": "pending",
                "live_pnl": 0.0,
                "was_live_trade": True,
                "fok_rejected": False,
                "signal_edge": 0.0,
                "signal_momentum": 0.0,
                "signal_fv": 0.0,
                "signal_tte": 0.0,
                "strike_source": "",
                "latency_ms": 0.0,
            }
            self._pending[key] = record

        record["live_entry_price"] = round(fill_price, 4)
        record["live_outcome"] = "pending"
        record["was_live_trade"] = True
        record["latency_ms"] = round(latency_ms, 1)

    def log_fok_reject(
        self,
        market_slug: str,
        coin: str,
        side: str,
    ):
        """Called when a live order gets FOK rejected. Signal passed but no fill."""
        key = self._key(market_slug, side)
        record = self._pending.get(key)
        if record:
            record["fok_rejected"] = True
            record["was_live_trade"] = False
            record["live_outcome"] = "fok_rejected"

    def resolve(self, market_slug: str, side: str, won: bool):
        """Called when market settles. Updates both paper and live outcomes.

        Writes the completed record to CSV and removes from pending.
        """
        key = self._key(market_slug, side)
        record = self._pending.pop(key, None)
        if not record:
            return

        # Paper outcome: paper always "fills" at the ask, so outcome is deterministic
        paper_entry = record["paper_entry_price"]
        record["paper_outcome"] = "won" if won else "lost"
        record["paper_pnl"] = round(1.0 - paper_entry if won else -paper_entry, 4)

        # Live outcome (if it was a live trade)
        if record["was_live_trade"] and record["live_outcome"] == "pending":
            live_entry = record["live_entry_price"]
            record["live_outcome"] = "won" if won else "lost"
            record["live_pnl"] = round(1.0 - live_entry if won else -live_entry, 4)

        # Append to CSV
        self._write_row(record)

    def resolve_all_for_market(self, market_slug: str, winning_side: str):
        """Resolve all pending records for a given market slug.

        Called during settlement when we know the winning side.
        """
        keys_to_resolve = [
            k for k in self._pending
            if k.startswith(f"{market_slug}:")
        ]
        for key in keys_to_resolve:
            _, side = key.rsplit(":", 1)
            won = (side == winning_side)
            self.resolve(market_slug, side, won)

    def _write_row(self, record: dict):
        """Append a single row to the CSV."""
        try:
            with open(self.csv_path, "a", newline="") as f:
                writer = csv.DictWriter(f, fieldnames=SHADOW_COLUMNS)
                writer.writerow(record)
        except Exception as e:
            logger.error(f"Shadow logger write failed: {e}")

    def get_degradation_summary(self, min_trades: int = 10) -> Dict[str, dict]:
        """Returns per-coin degradation from matched pairs.

        Only considers records where BOTH paper and live traded on the same market.

        Returns:
            {coin: {
                'paper_wr': float, 'live_wr': float, 'gap': float,
                'n_matched': int, 'paper_total': int, 'fill_rate': float,
            }}
        """
        rows = self._read_resolved()

        # Per-coin stats
        coin_data: Dict[str, dict] = {}
        for row in rows:
            coin = row.get("coin", "").upper()
            if not coin:
                continue

            if coin not in coin_data:
                coin_data[coin] = {
                    "paper_trades": 0, "paper_wins": 0,
                    "live_trades": 0, "live_wins": 0,
                    "matched_trades": 0, "matched_paper_wins": 0,
                    "matched_live_wins": 0,
                    "signals": 0, "fills": 0, "fok_rejects": 0,
                }

            d = coin_data[coin]
            d["signals"] += 1

            paper_outcome = row.get("paper_outcome", "")
            live_outcome = row.get("live_outcome", "")
            was_live = row.get("was_live_trade", "").lower() in ("true", "1", "yes")
            fok = row.get("fok_rejected", "").lower() in ("true", "1", "yes")

            # Paper stats (every signal is a paper trade)
            if paper_outcome in ("won", "lost"):
                d["paper_trades"] += 1
                if paper_outcome == "won":
                    d["paper_wins"] += 1

            # Live stats
            if was_live and live_outcome in ("won", "lost"):
                d["live_trades"] += 1
                d["fills"] += 1
                if live_outcome == "won":
                    d["live_wins"] += 1

            if fok:
                d["fok_rejects"] += 1

            # Matched pairs: both paper and live resolved on the same market
            if was_live and paper_outcome in ("won", "lost") and live_outcome in ("won", "lost"):
                d["matched_trades"] += 1
                if paper_outcome == "won":
                    d["matched_paper_wins"] += 1
                if live_outcome == "won":
                    d["matched_live_wins"] += 1

        # Compute summary
        result: Dict[str, dict] = {}
        for coin, d in coin_data.items():
            n_matched = d["matched_trades"]
            paper_wr = d["paper_wins"] / d["paper_trades"] if d["paper_trades"] > 0 else 0.0
            live_wr = d["live_wins"] / d["live_trades"] if d["live_trades"] > 0 else 0.0
            matched_paper_wr = d["matched_paper_wins"] / n_matched if n_matched > 0 else 0.0
            matched_live_wr = d["matched_live_wins"] / n_matched if n_matched > 0 else 0.0
            fill_rate = d["fills"] / d["signals"] if d["signals"] > 0 else 0.0

            result[coin] = {
                "paper_wr": round(paper_wr, 4),
                "live_wr": round(live_wr, 4),
                "gap": round(matched_paper_wr - matched_live_wr, 4),
                "n_matched": n_matched,
                "paper_total": d["paper_trades"],
                "live_total": d["live_trades"],
                "fill_rate": round(fill_rate, 4),
                "fok_rejects": d["fok_rejects"],
                "sufficient": n_matched >= min_trades,
            }

        return result

    def get_fill_rate(self) -> Dict[str, float]:
        """What % of signals that pass filters actually get filled live? Per-coin."""
        rows = self._read_resolved()
        coin_signals: Dict[str, int] = {}
        coin_fills: Dict[str, int] = {}

        for row in rows:
            coin = row.get("coin", "").upper()
            if not coin:
                continue
            coin_signals[coin] = coin_signals.get(coin, 0) + 1
            was_live = row.get("was_live_trade", "").lower() in ("true", "1", "yes")
            if was_live:
                coin_fills[coin] = coin_fills.get(coin, 0) + 1

        result = {}
        for coin in coin_signals:
            result[coin] = coin_fills.get(coin, 0) / coin_signals[coin] if coin_signals[coin] > 0 else 0.0
        return result

    def _read_resolved(self) -> List[dict]:
        """Read all resolved rows from the shadow CSV."""
        path = Path(self.csv_path)
        if not path.exists():
            return []

        rows = []
        try:
            with open(path, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Only include resolved rows (paper_outcome is won or lost)
                    if row.get("paper_outcome") in ("won", "lost"):
                        rows.append(row)
        except Exception as e:
            logger.error(f"Shadow logger read failed: {e}")
        return rows

    def pending_count(self) -> int:
        """Number of signals awaiting resolution."""
        return len(self._pending)

    def summary(self) -> str:
        """Human-readable summary of shadow tracking stats."""
        stats = self.get_degradation_summary()
        if not stats:
            return "Shadow Logger: no resolved data yet."

        lines = [
            "=== Shadow Logger — Paper vs Live (Matched Pairs) ===",
            "",
        ]
        for coin in sorted(stats.keys()):
            s = stats[coin]
            suf = " [SUFFICIENT]" if s["sufficient"] else f" [need {10 - s['n_matched']} more]"
            lines.append(f"--- {coin}{suf} ---")
            lines.append(
                f"  Paper WR: {s['paper_wr']*100:.1f}% ({s['paper_total']} trades) | "
                f"Live WR: {s['live_wr']*100:.1f}% ({s['live_total']} trades)"
            )
            if s["n_matched"] > 0:
                lines.append(
                    f"  Matched Pairs: {s['n_matched']} | "
                    f"Gap: {s['gap']*100:+.1f}pp"
                )
            lines.append(
                f"  Fill Rate: {s['fill_rate']*100:.1f}% | "
                f"FOK Rejects: {s['fok_rejects']}"
            )
            lines.append("")

        lines.append(f"  Pending: {self.pending_count()} signals awaiting settlement")
        return "\n".join(lines)
