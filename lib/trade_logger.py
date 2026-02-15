"""
Trade Logger - CSV-based Trade Logging and Analytics

Logs every trade with full context for post-analysis and strategy improvement.
Tracks performance by entry price bucket to find optimal parameters.

Usage:
    from lib.trade_logger import TradeLogger

    logger = TradeLogger("trades.csv")
    logger.log_trade(
        market_slug="btc-updown-5m-123456",
        side="down",
        entry_price=0.05,
        bet_size_usdc=1.0,
        num_tokens=20.0,
    )
    # Later, when market resolves:
    logger.log_outcome(market_slug="btc-updown-5m-123456", won=True, payout=20.0)
"""

import csv
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Dict, List


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
    btc_price: float = 0.0
    other_side_price: float = 0.0
    volatility_std: float = 0.0


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

    Writes every trade to a CSV file and maintains running statistics
    in memory for live display.
    """

    CSV_HEADERS = [
        "timestamp", "market_slug", "coin", "timeframe", "side",
        "entry_price", "bet_size_usdc", "num_tokens",
        "outcome", "payout", "pnl", "bankroll",
        "btc_price", "other_side_price", "volatility_std",
    ]

    def __init__(self, filepath: str = "data/trades.csv"):
        """
        Initialize trade logger.

        Args:
            filepath: Path to CSV file for trade log
        """
        self.filepath = Path(filepath)
        self.stats = SessionStats()
        self._pending_trades: Dict[str, TradeRecord] = {}  # market_slug -> record

        # Create data directory if needed
        self.filepath.parent.mkdir(parents=True, exist_ok=True)

        # Write headers if file doesn't exist
        if not self.filepath.exists():
            with open(self.filepath, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)

        # Load existing trades to build stats
        self._load_existing_stats()

    def _load_existing_stats(self) -> None:
        """Load stats from existing CSV file."""
        if not self.filepath.exists():
            return

        try:
            with open(self.filepath, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    entry_price = float(row.get("entry_price", 0))
                    bet_size = float(row.get("bet_size_usdc", 0))
                    outcome = row.get("outcome", "pending")
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
                    else:
                        self.stats.pending += 1
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
        btc_price: float = 0.0,
        other_side_price: float = 0.0,
        volatility_std: float = 0.0,
    ) -> TradeRecord:
        """
        Log a new trade entry.

        Args:
            market_slug: Polymarket market slug
            coin: Coin symbol
            timeframe: Market timeframe
            side: "up" or "down"
            entry_price: Price paid per token
            bet_size_usdc: Total USDC wagered
            num_tokens: Number of tokens bought

        Returns:
            TradeRecord for tracking
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
            btc_price=btc_price,
            other_side_price=other_side_price,
            volatility_std=volatility_std,
        )

        # Track as pending
        self._pending_trades[market_slug] = record

        # Update stats
        self.stats.total_trades += 1
        self.stats.pending += 1
        self.stats.total_wagered += bet_size_usdc

        price_bucket = round(entry_price * 100)
        self.stats.bucket_trades[price_bucket] = self.stats.bucket_trades.get(price_bucket, 0) + 1

        # Write to CSV
        self._write_record(record)

        return record

    def log_outcome(self, market_slug: str, won: bool, payout: float = 0.0) -> None:
        """
        Log the outcome of a trade when market resolves.

        Args:
            market_slug: Market slug to update
            won: Whether the trade won
            payout: Actual payout received
        """
        record = self._pending_trades.pop(market_slug, None)
        if not record:
            return

        record.outcome = "won" if won else "lost"
        record.payout = payout
        record.pnl = payout - record.bet_size_usdc

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

        # Update CSV (append updated record)
        self._write_record(record)

    def _write_record(self, record: TradeRecord) -> None:
        """Append a record to CSV."""
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
                    f"{record.btc_price:.2f}",
                    f"{record.other_side_price:.4f}",
                    f"{record.volatility_std:.6f}",
                ])
        except Exception:
            pass

    def get_pending_slugs(self) -> List[str]:
        """Get list of market slugs with pending outcomes."""
        return list(self._pending_trades.keys())
