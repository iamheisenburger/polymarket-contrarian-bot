"""
Trade Logger - CSV-based Trade Logging and Analytics

Logs every trade with full context for post-analysis and strategy improvement.
Tracks performance by entry price bucket to find optimal parameters.

Key design:
    - CSV only contains RESOLVED trades (won/lost) — no duplicate pending rows
    - Pending trades are saved to a JSON sidecar file that survives restarts
    - Trade key is slug:side to support both sides in the same market
    - Real USDC balance is logged alongside each trade for reconciliation

Usage:
    from lib.trade_logger import TradeLogger

    logger = TradeLogger("data/sniper_trades.csv")
    logger.log_trade(
        market_slug="btc-updown-5m-123456",
        coin="BTC",
        timeframe="5m",
        side="down",
        entry_price=0.21,
        bet_size_usdc=1.05,
        num_tokens=5.0,
    )
    # Later, when market resolves:
    logger.log_outcome("btc-updown-5m-123456", side="down", won=True, payout=5.0)
"""

import csv
import json
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List, Tuple


@dataclass
class TradeRecord:
    """Single trade record."""

    timestamp: str
    market_slug: str
    coin: str
    timeframe: str
    side: str  # "up" or "down"
    entry_price: float
    bet_size_usdc: float
    num_tokens: float
    outcome: str = "pending"  # "pending", "won", "lost"
    payout: float = 0.0
    pnl: float = 0.0
    bankroll: float = 0.0
    usdc_balance: float = 0.0  # Actual on-chain USDC at time of trade
    btc_price: float = 0.0
    other_side_price: float = 0.0
    volatility_std: float = 0.0

    @property
    def trade_key(self) -> str:
        """Unique key: slug + side."""
        return f"{self.market_slug}:{self.side}"


@dataclass
class SessionStats:
    """Running session statistics."""

    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    total_wagered: float = 0.0
    total_payout: float = 0.0
    total_pnl: float = 0.0

    # Per entry-price bucket stats (rounded to nearest cent)
    bucket_trades: Dict[int, int] = field(default_factory=dict)  # price_cents -> count
    bucket_wins: Dict[int, int] = field(default_factory=dict)  # price_cents -> wins

    @property
    def win_rate(self) -> float:
        """Overall win rate percentage."""
        decided = self.wins + self.losses
        return (self.wins / decided * 100) if decided > 0 else 0.0

    @property
    def roi(self) -> float:
        """Return on investment percentage."""
        return (self.total_pnl / self.total_wagered * 100) if self.total_wagered > 0 else 0.0

    def bucket_win_rate(self, price_cents: int) -> float:
        """Win rate for a specific entry price bucket."""
        trades = self.bucket_trades.get(price_cents, 0)
        wins = self.bucket_wins.get(price_cents, 0)
        return (wins / trades * 100) if trades > 0 else 0.0

    def get_summary(self) -> str:
        """Get formatted summary string."""
        decided = self.wins + self.losses
        lines = [
            f"Trades: {self.total_trades} (W:{self.wins} L:{self.losses} P:{self.pending})",
            f"Win Rate: {self.win_rate:.1f}% ({self.wins}/{decided})",
            f"Wagered: ${self.total_wagered:.2f}",
            f"Payout: ${self.total_payout:.2f}",
            f"PnL: ${self.total_pnl:+.2f} (ROI: {self.roi:+.1f}%)",
        ]

        # Per-bucket breakdown
        if self.bucket_trades:
            lines.append("--- By Entry Price ---")
            for cents in sorted(self.bucket_trades.keys()):
                trades = self.bucket_trades[cents]
                wins = self.bucket_wins.get(cents, 0)
                wr = self.bucket_win_rate(cents)
                lines.append(f"  ${cents/100:.2f}: {trades} trades, {wr:.0f}% win rate ({wins}/{trades})")

        return "\n".join(lines)


class TradeLogger:
    """
    CSV-based trade logger with analytics.

    - CSV only contains RESOLVED trades (won/lost) — no duplicates
    - Pending trades persist in a JSON sidecar file across restarts
    - Trade key is slug:side to support both sides in same market
    """

    CSV_HEADERS = [
        "timestamp", "market_slug", "coin", "timeframe", "side",
        "entry_price", "bet_size_usdc", "num_tokens",
        "outcome", "payout", "pnl", "bankroll", "usdc_balance",
        "btc_price", "other_side_price", "volatility_std",
    ]

    def __init__(self, filepath: str = "data/trades.csv"):
        self.filepath = Path(filepath)
        self.pending_filepath = self.filepath.with_suffix(".pending.json")
        self.stats = SessionStats()
        self._pending_trades: Dict[str, TradeRecord] = {}  # trade_key -> record

        # Create data directory if needed
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        # Write headers if file doesn't exist
        if not self.filepath.exists():
            with open(self.filepath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)

        # Load stats from existing resolved trades
        self._load_existing_stats()

        # Load pending trades from JSON sidecar (survives restarts)
        self._load_pending()

    def _load_existing_stats(self) -> None:
        """Load stats from existing CSV file (only resolved trades)."""
        if not self.filepath.exists():
            return

        try:
            with open(self.filepath, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    outcome = row.get("outcome", "pending")
                    if outcome == "pending":
                        continue  # Skip any legacy pending rows

                    entry_price = float(row.get("entry_price", 0))
                    bet_size = float(row.get("bet_size_usdc", 0))
                    payout = float(row.get("payout", 0))
                    pnl = float(row.get("pnl", 0))
                    price_bucket = round(entry_price * 100)

                    self.stats.total_trades += 1
                    self.stats.total_wagered += bet_size
                    self.stats.bucket_trades[price_bucket] = self.stats.bucket_trades.get(price_bucket, 0) + 1

                    if outcome == "won":
                        self.stats.wins += 1
                        self.stats.total_payout += payout
                        self.stats.total_pnl += pnl
                        self.stats.bucket_wins[price_bucket] = self.stats.bucket_wins.get(price_bucket, 0) + 1
                    elif outcome == "lost":
                        self.stats.losses += 1
                        self.stats.total_pnl += pnl
        except Exception:
            pass

    def _load_pending(self) -> None:
        """Load pending trades from JSON sidecar file."""
        if not self.pending_filepath.exists():
            return

        try:
            with open(self.pending_filepath, "r") as f:
                data = json.load(f)

            for key, record_dict in data.items():
                record = TradeRecord(**record_dict)
                self._pending_trades[key] = record
                self.stats.pending += 1
        except Exception:
            pass

    def _save_pending(self) -> None:
        """Save pending trades to JSON sidecar file."""
        try:
            data = {}
            for key, record in self._pending_trades.items():
                data[key] = asdict(record)

            with open(self.pending_filepath, "w") as f:
                json.dump(data, f, indent=2)
        except Exception:
            pass

    def log_trade(
        self,
        market_slug: str,
        coin: str,
        timeframe: str,
        side: str,
        entry_price: float,
        bet_size_usdc: float,
        num_tokens: float,
        bankroll: float = 0.0,
        usdc_balance: float = 0.0,
        btc_price: float = 0.0,
        other_side_price: float = 0.0,
        volatility_std: float = 0.0,
    ) -> TradeRecord:
        """
        Log a new trade entry. Stored in pending JSON only (not CSV yet).
        CSV entry is written when outcome is known.
        """
        record = TradeRecord(
            timestamp=datetime.now(timezone.utc).isoformat(),
            market_slug=market_slug,
            coin=coin,
            timeframe=timeframe,
            side=side,
            entry_price=entry_price,
            bet_size_usdc=bet_size_usdc,
            num_tokens=num_tokens,
            bankroll=bankroll,
            usdc_balance=usdc_balance,
            btc_price=btc_price,
            other_side_price=other_side_price,
            volatility_std=volatility_std,
        )

        trade_key = record.trade_key

        # Track as pending
        self._pending_trades[trade_key] = record
        self._save_pending()

        # Update stats
        self.stats.total_trades += 1
        self.stats.pending += 1
        self.stats.total_wagered += bet_size_usdc

        price_bucket = round(entry_price * 100)
        self.stats.bucket_trades[price_bucket] = self.stats.bucket_trades.get(price_bucket, 0) + 1

        return record

    def log_outcome(
        self,
        market_slug: str,
        side: str,
        won: bool,
        payout: float = 0.0,
        usdc_balance: float = 0.0,
    ) -> None:
        """
        Log the outcome of a trade when market resolves.
        This writes the resolved entry to CSV.
        """
        trade_key = f"{market_slug}:{side}"
        record = self._pending_trades.pop(trade_key, None)
        if not record:
            return

        record.outcome = "won" if won else "lost"
        record.payout = payout
        record.pnl = payout - record.bet_size_usdc
        if usdc_balance > 0:
            record.usdc_balance = usdc_balance

        # Update stats
        self.stats.pending -= 1
        if won:
            self.stats.wins += 1
            self.stats.total_payout += payout
            price_bucket = round(record.entry_price * 100)
            self.stats.bucket_wins[price_bucket] = self.stats.bucket_wins.get(price_bucket, 0) + 1
        else:
            self.stats.losses += 1

        self.stats.total_pnl += record.pnl

        # Write resolved entry to CSV (the ONLY time we write to CSV)
        self._write_record(record)

        # Update pending sidecar
        self._save_pending()

    def _write_record(self, record: TradeRecord) -> None:
        """Append a resolved record to CSV."""
        try:
            with open(self.filepath, "a", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    record.timestamp,
                    record.market_slug,
                    record.coin,
                    record.timeframe,
                    record.side,
                    f"{record.entry_price:.4f}",
                    f"{record.bet_size_usdc:.2f}",
                    f"{record.num_tokens:.2f}",
                    record.outcome,
                    f"{record.payout:.2f}",
                    f"{record.pnl:.2f}",
                    f"{record.bankroll:.2f}",
                    f"{record.usdc_balance:.2f}",
                    f"{record.btc_price:.2f}",
                    f"{record.other_side_price:.4f}",
                    f"{record.volatility_std:.6f}",
                ])
        except Exception:
            pass

    def get_pending_slugs(self) -> List[str]:
        """Get list of market slugs with pending outcomes."""
        return list(set(r.market_slug for r in self._pending_trades.values()))

    def get_pending_trades(self) -> Dict[str, TradeRecord]:
        """Get all pending trades (for resolving on startup)."""
        return dict(self._pending_trades)

    def get_pending_for_market(self, market_slug: str) -> List[Tuple[str, TradeRecord]]:
        """Get pending trades for a specific market slug."""
        results = []
        for key, record in self._pending_trades.items():
            if record.market_slug == market_slug:
                results.append((record.side, record))
        return results
