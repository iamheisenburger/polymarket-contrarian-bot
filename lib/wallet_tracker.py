"""
Alpha Wallet Tracker — polls top trader activity and detects new trades.

Maintains a set of "alpha wallets" (proven profitable traders) and watches
for their new BUY trades. When a new trade is detected, it's emitted as a
CopySignal for the CopySniper strategy to evaluate.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from lib.leaderboard_api import (
    COPY_CATEGORIES,
    LeaderboardEntry,
    PolymarketDataAPI,
    TradeActivity,
)

logger = logging.getLogger(__name__)


@dataclass
class AlphaWallet:
    """A tracked top-trader wallet."""
    address: str
    username: str
    category: str
    weekly_pnl: float
    rank: int
    # Computed from recent activity
    recent_buys: int = 0
    recent_wins: int = 0
    recent_losses: int = 0
    win_rate: float = 0.0
    last_seen_ts: int = 0


@dataclass
class CopySignal:
    """Signal emitted when an alpha wallet makes a new trade we could copy."""
    alpha_address: str
    alpha_username: str
    alpha_category: str
    alpha_wr: float
    alpha_rank: int
    # Trade details
    market_slug: str
    market_question: str
    event_slug: str
    token_id: str               # The asset (clobTokenId) to buy
    outcome: str                # "Yes" or "No"
    outcome_index: int
    condition_id: str
    alpha_price: float          # Price the alpha paid
    alpha_size_usdc: float      # How much they bet in dollars
    alpha_timestamp: int        # When they traded


class WalletTracker:
    """Discovers alpha wallets and polls for new trades."""

    def __init__(
        self,
        api: PolymarketDataAPI,
        categories: Optional[List[str]] = None,
        min_weekly_pnl: float = 1000.0,
        min_rank: int = 20,             # Top N per category
        wallets_per_category: int = 10,
        min_wr: float = 0.0,            # Minimum win rate (0 = no filter initially)
    ):
        self.api = api
        self.categories = categories or COPY_CATEGORIES
        self.min_weekly_pnl = min_weekly_pnl
        self.min_rank = min_rank
        self.wallets_per_category = wallets_per_category
        self.min_wr = min_wr

        # Alpha wallets: address -> AlphaWallet
        self.alphas: Dict[str, AlphaWallet] = {}

        # Track which trades we've already seen (set of tx_hashes)
        self.seen_trades: Set[str] = set()

        # Track last poll timestamp per wallet
        self.last_poll_ts: Dict[str, int] = {}

    # ------------------------------------------------------------------
    # Alpha wallet discovery
    # ------------------------------------------------------------------

    def discover_alphas(self) -> List[AlphaWallet]:
        """
        Pull leaderboard across all categories and build alpha wallet list.
        Should be called once at startup and then periodically (e.g. daily).
        """
        logger.info("Discovering alpha wallets across categories...")
        all_leaders = self.api.get_top_traders_all_categories(
            time_period="WEEK",
            limit_per_category=self.wallets_per_category,
        )

        new_alphas: Dict[str, AlphaWallet] = {}

        for category, entries in all_leaders.items():
            for entry in entries:
                if entry.pnl < self.min_weekly_pnl:
                    continue
                if entry.rank > self.min_rank:
                    continue
                addr = entry.address.lower()

                # If wallet appears in multiple categories, keep the one with higher PnL
                if addr in new_alphas and new_alphas[addr].weekly_pnl >= entry.pnl:
                    continue

                new_alphas[addr] = AlphaWallet(
                    address=addr,
                    username=entry.username,
                    category=category,
                    weekly_pnl=entry.pnl,
                    rank=entry.rank,
                )

        self.alphas = new_alphas
        logger.info(f"Found {len(self.alphas)} alpha wallets across {len(self.categories)} categories")

        # Print summary
        for addr, aw in sorted(self.alphas.items(), key=lambda x: -x[1].weekly_pnl):
            logger.info(f"  #{aw.rank} {aw.username:<20} {aw.category:<12} PnL=${aw.weekly_pnl:>12,.2f}")

        return list(self.alphas.values())

    # ------------------------------------------------------------------
    # Poll for new trades
    # ------------------------------------------------------------------

    def poll_new_trades(self) -> List[CopySignal]:
        """
        Poll all alpha wallets for new BUY trades since last check.
        Returns list of CopySignals for new trades.
        """
        signals: List[CopySignal] = []
        now = int(time.time())

        for addr, alpha in self.alphas.items():
            # Get timestamp of last poll (default: 5 minutes ago)
            since_ts = self.last_poll_ts.get(addr, now - 300)

            try:
                buys = self.api.get_buys(
                    user=addr,
                    since_timestamp=since_ts,
                    limit=20,
                )
            except Exception as e:
                logger.warning(f"Failed to poll {alpha.username}: {e}")
                continue

            for trade in buys:
                # Skip if already seen
                if trade.tx_hash in self.seen_trades:
                    continue
                # Skip if older than our last poll
                if trade.timestamp <= since_ts:
                    continue

                self.seen_trades.add(trade.tx_hash)

                signal = CopySignal(
                    alpha_address=addr,
                    alpha_username=alpha.username,
                    alpha_category=alpha.category,
                    alpha_wr=alpha.win_rate,
                    alpha_rank=alpha.rank,
                    market_slug=trade.slug,
                    market_question=trade.title,
                    event_slug=trade.event_slug,
                    token_id=trade.asset,
                    outcome=trade.outcome,
                    outcome_index=trade.outcome_index,
                    condition_id=trade.condition_id,
                    alpha_price=trade.price,
                    alpha_size_usdc=trade.usdc_size,
                    alpha_timestamp=trade.timestamp,
                )
                signals.append(signal)

            # Update last poll timestamp
            self.last_poll_ts[addr] = now

            # Small delay between wallet polls to avoid rate limiting
            time.sleep(0.1)

        if signals:
            logger.info(f"Detected {len(signals)} new alpha trades")

        return signals

    # ------------------------------------------------------------------
    # Housekeeping
    # ------------------------------------------------------------------

    def prune_seen_trades(self, max_age_seconds: int = 86400):
        """Keep seen_trades set from growing unbounded."""
        # Simple approach: just cap the size
        if len(self.seen_trades) > 10000:
            # Keep last 5000
            self.seen_trades = set(list(self.seen_trades)[-5000:])

    def get_alpha_count(self) -> int:
        return len(self.alphas)

    def get_summary(self) -> str:
        """Return human-readable summary of tracked wallets."""
        lines = [f"Tracking {len(self.alphas)} alpha wallets:"]
        by_cat: Dict[str, int] = {}
        for alpha in self.alphas.values():
            by_cat[alpha.category] = by_cat.get(alpha.category, 0) + 1
        for cat, count in sorted(by_cat.items()):
            lines.append(f"  {cat}: {count} wallets")
        return "\n".join(lines)
