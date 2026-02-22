"""
Copy Sniper Strategy — follow proven Polymarket winners.

Polls top traders' activity every 60 seconds. When an alpha wallet makes
a new BUY trade, evaluates whether to copy it based on safety filters
(slippage, liquidity, freshness, etc.). In observe mode, logs paper trades.
In live mode, executes via TradingBot.

Settlement: periodic Gamma API check (same pattern as the arena).
"""

import asyncio
import csv
import json
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import requests

from lib.leaderboard_api import PolymarketDataAPI
from lib.wallet_tracker import AlphaWallet, CopySignal, WalletTracker

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class CopySniperConfig:
    """Configuration for the copy sniper."""
    bankroll: float = 50.0
    observe_only: bool = True
    categories: list = field(default_factory=lambda: [
        "SPORTS", "POLITICS", "ECONOMICS", "CULTURE", "TECH", "FINANCE",
    ])
    # Timing
    poll_interval: int = 60         # seconds between polls
    settle_interval: int = 60       # seconds between settlement checks
    alpha_refresh_hours: int = 12   # refresh alpha wallets every N hours
    # Filters
    max_slippage: float = 0.05      # max price move since alpha trade (5 cents)
    min_liquidity: float = 100      # minimum market liquidity ($)
    max_trade_age: int = 300        # max seconds since alpha trade (5 min)
    min_entry_price: float = 0.10   # don't buy below $0.10
    max_entry_price: float = 0.90   # don't buy above $0.90
    max_position_pct: float = 0.30  # max 30% of bankroll per trade
    max_daily_loss_pct: float = 0.40  # stop if daily loss > 40%
    # Kelly
    default_wr: float = 0.60       # assumed WR when we don't have alpha-specific data
    kelly_fraction: float = 0.5    # half-Kelly for safety
    # Alpha selection
    min_weekly_pnl: float = 1000.0
    wallets_per_category: int = 10
    # Ignore crypto binary markets (we proved they're unbeatable)
    skip_crypto_binaries: bool = True
    # Resolution speed: only copy trades on markets resolving within N hours
    # With $12.95 balance, can't lock capital in week-long events
    max_hours_to_resolution: float = 24.0  # 0 = no filter


@dataclass
class PaperPosition:
    """Track a paper trade."""
    slug: str
    market_question: str
    event_slug: str
    token_id: str
    outcome: str            # "Yes" or "No"
    condition_id: str
    entry_price: float
    num_tokens: float
    cost: float
    alpha_wallet: str
    alpha_username: str
    alpha_category: str
    timestamp: str
    resolved: bool = False
    won: Optional[bool] = None
    payout: float = 0.0


class CopySniper:
    """
    Copy-trade strategy that follows top Polymarket traders.
    """

    def __init__(self, config: CopySniperConfig):
        self.config = config
        self.api = PolymarketDataAPI()
        self.tracker = WalletTracker(
            api=self.api,
            categories=config.categories,
            min_weekly_pnl=config.min_weekly_pnl,
            wallets_per_category=config.wallets_per_category,
        )

        # Paper portfolio
        self.balance = config.bankroll
        self.starting_balance = config.bankroll
        self.positions: Dict[str, PaperPosition] = {}  # slug -> position
        self.daily_pnl = 0.0

        # Stats
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_wagered = 0.0
        self.total_payout = 0.0

        # CSV logger
        self.csv_path = Path("data/copy_sniper.csv")
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_csv()

        # Pending JSON sidecar (survives restarts)
        self.pending_path = self.csv_path.with_suffix(".pending.json")
        self._load_pending()

        # Track last alpha refresh
        self.last_alpha_refresh = 0.0

    # ------------------------------------------------------------------
    # CSV I/O
    # ------------------------------------------------------------------

    CSV_HEADERS = [
        "timestamp", "market_slug", "market_question", "event_slug",
        "outcome_side", "entry_price", "cost", "num_tokens",
        "alpha_wallet", "alpha_username", "alpha_category",
        "result", "payout", "pnl", "balance",
    ]

    def _init_csv(self):
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="") as f:
                csv.writer(f).writerow(self.CSV_HEADERS)
        else:
            self._load_stats_from_csv()

    def _load_stats_from_csv(self):
        """Reload stats from existing CSV."""
        try:
            with open(self.csv_path, "r") as f:
                for row in csv.DictReader(f):
                    result = row.get("result", "pending")
                    cost = float(row.get("cost", 0))
                    payout = float(row.get("payout", 0))
                    pnl = float(row.get("pnl", 0))
                    self.total_trades += 1
                    self.total_wagered += cost
                    if result == "won":
                        self.wins += 1
                        self.total_payout += payout
                    elif result == "lost":
                        self.losses += 1
        except Exception:
            pass

    def _write_resolved(self, pos: PaperPosition, result: str, pnl: float):
        """Write a resolved trade to CSV."""
        with open(self.csv_path, "a", newline="") as f:
            csv.writer(f).writerow([
                pos.timestamp,
                pos.slug,
                pos.market_question[:80],
                pos.event_slug,
                pos.outcome,
                f"{pos.entry_price:.4f}",
                f"{pos.cost:.4f}",
                f"{pos.num_tokens:.2f}",
                pos.alpha_wallet[:10] + "...",
                pos.alpha_username,
                pos.alpha_category,
                result,
                f"{pos.payout:.4f}",
                f"{pnl:.4f}",
                f"{self.balance:.4f}",
            ])

    def _load_pending(self):
        """Load pending positions from JSON sidecar."""
        if not self.pending_path.exists():
            return
        try:
            with open(self.pending_path, "r") as f:
                data = json.load(f)
            for key, d in data.items():
                self.positions[key] = PaperPosition(**d)
            logger.info(f"Loaded {len(self.positions)} pending positions from sidecar")
        except Exception as e:
            logger.warning(f"Failed to load pending: {e}")

    def _save_pending(self):
        """Save pending positions to JSON sidecar."""
        try:
            data = {}
            for key, pos in self.positions.items():
                if not pos.resolved:
                    data[key] = asdict(pos)
            with open(self.pending_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save pending: {e}")

    # ------------------------------------------------------------------
    # Signal evaluation
    # ------------------------------------------------------------------

    def _is_crypto_binary(self, slug: str) -> bool:
        """Check if a market is a crypto binary (5m/15m) we should skip."""
        crypto_patterns = ["updown-5m", "updown-15m", "updown-4h", "updown-1h"]
        slug_lower = slug.lower()
        return any(p in slug_lower for p in crypto_patterns)

    def _get_market_price(self, token_id: str) -> Optional[float]:
        """Get current best ask price for a token from the CLOB."""
        try:
            url = f"https://clob.polymarket.com/book"
            resp = requests.get(url, params={"token_id": token_id}, timeout=10)
            if resp.status_code != 200:
                return None
            book = resp.json()
            asks = book.get("asks", [])
            if not asks:
                return None
            # Best ask = lowest price
            best_ask = min(float(a["price"]) for a in asks)
            return best_ask
        except Exception as e:
            logger.debug(f"Failed to get price for {token_id[:20]}...: {e}")
            return None

    def _get_market_info(self, slug: str) -> Optional[Dict[str, Any]]:
        """Get market info from Gamma API."""
        try:
            url = f"{GAMMA_API}/markets/slug/{slug}"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            return resp.json()
        except Exception:
            return None

    def evaluate_signal(self, signal: CopySignal) -> Optional[Dict[str, Any]]:
        """
        Evaluate whether to copy an alpha trade.
        Returns trade details dict if we should copy, None otherwise.
        """
        now = int(time.time())

        # Filter: skip crypto binaries
        if self.config.skip_crypto_binaries and self._is_crypto_binary(signal.market_slug):
            return None

        # --- Fast checks first (no API calls) ---

        # Filter: trade age
        age = now - signal.alpha_timestamp
        if age > self.config.max_trade_age:
            logger.info(f"SKIP [age {age}s] {signal.market_question[:50]}")
            return None

        # Filter: already have position in this market
        if signal.market_slug in self.positions:
            return None

        # Filter: entry price range from alpha's trade
        if signal.alpha_price < self.config.min_entry_price:
            logger.info(f"SKIP [price ${signal.alpha_price:.2f} < min] {signal.market_question[:50]}")
            return None
        if signal.alpha_price > self.config.max_entry_price:
            logger.info(f"SKIP [price ${signal.alpha_price:.2f} > max] {signal.market_question[:50]}")
            return None

        # --- Expensive checks (API calls) ---

        # Filter: resolution time — skip markets that won't resolve soon
        if self.config.max_hours_to_resolution > 0:
            market_info = self._get_market_info(signal.market_slug)
            if market_info:
                end_date_str = market_info.get("endDate") or market_info.get("end_date_iso")
                if end_date_str:
                    try:
                        end_str = end_date_str.replace("Z", "+00:00")
                        end_dt = datetime.fromisoformat(end_str)
                        now_dt = datetime.now(timezone.utc)
                        hours_left = (end_dt - now_dt).total_seconds() / 3600
                        if hours_left > self.config.max_hours_to_resolution:
                            logger.info(
                                f"SKIP [resolves {hours_left:.0f}h] {signal.market_question[:50]}"
                            )
                            return None
                        if hours_left < 0.05:  # < 3 minutes left, too late
                            logger.info(f"SKIP [too late {hours_left*60:.0f}m] {signal.market_question[:50]}")
                            return None
                        logger.info(f"PASS [resolves {hours_left:.1f}h] {signal.market_question[:50]}")
                    except (ValueError, TypeError):
                        pass  # Can't parse date, let it through
                else:
                    logger.info(f"SKIP [no end date] {signal.market_question[:50]}")
                    return None
            else:
                logger.info(f"SKIP [no market info] {signal.market_question[:50]}")
                return None

        # Get current price
        current_price = self._get_market_price(signal.token_id)
        if current_price is None:
            logger.info(f"SKIP [no orderbook] {signal.market_question[:50]}")
            return None

        # Filter: slippage
        slippage = current_price - signal.alpha_price
        if slippage > self.config.max_slippage:
            logger.info(f"SKIP [slippage {slippage:.3f}] {signal.market_question[:50]}")
            return None

        # Filter: current price range
        if current_price < self.config.min_entry_price:
            return None
        if current_price > self.config.max_entry_price:
            return None

        # Filter: daily loss limit
        if self.daily_pnl < -(self.config.max_daily_loss_pct * self.starting_balance):
            logger.warning("Daily loss limit reached — skipping all trades")
            return None

        # Position sizing — Kelly
        wr = signal.alpha_wr if signal.alpha_wr > 0 else self.config.default_wr
        if current_price <= 0 or current_price >= 1:
            return None
        b = (1.0 - current_price) / current_price  # net odds
        kelly_f = (wr * b - (1 - wr)) / b if b > 0 else 0
        kelly_f = max(0, min(kelly_f, self.config.max_position_pct))
        kelly_f *= self.config.kelly_fraction  # half-Kelly

        bet_amount = self.balance * kelly_f
        if bet_amount < 1.0:  # minimum $1 on Polymarket
            bet_amount = min(1.0, self.balance * 0.10)  # at least try 10% of bankroll
        if bet_amount > self.balance:
            return None

        num_tokens = bet_amount / current_price
        num_tokens = max(5.0, round(num_tokens, 2))  # minimum 5 tokens
        cost = num_tokens * current_price

        if cost > self.balance:
            num_tokens = int(self.balance / current_price)
            cost = num_tokens * current_price
        if cost < 1.0 or num_tokens < 5:
            return None

        return {
            "signal": signal,
            "current_price": current_price,
            "slippage": slippage,
            "num_tokens": num_tokens,
            "cost": cost,
            "kelly_f": kelly_f,
            "assumed_wr": wr,
            "age_seconds": age,
        }

    # ------------------------------------------------------------------
    # Trade execution (paper)
    # ------------------------------------------------------------------

    def execute_paper_trade(self, trade: Dict[str, Any]):
        """Execute a paper trade."""
        signal: CopySignal = trade["signal"]
        price = trade["current_price"]
        tokens = trade["num_tokens"]
        cost = trade["cost"]

        pos = PaperPosition(
            slug=signal.market_slug,
            market_question=signal.market_question,
            event_slug=signal.event_slug,
            token_id=signal.token_id,
            outcome=signal.outcome,
            condition_id=signal.condition_id,
            entry_price=price,
            num_tokens=tokens,
            cost=cost,
            alpha_wallet=signal.alpha_address,
            alpha_username=signal.alpha_username,
            alpha_category=signal.alpha_category,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        self.positions[signal.market_slug] = pos
        self.balance -= cost
        self.total_trades += 1
        self.total_wagered += cost
        self._save_pending()

        mode = "OBSERVE" if self.config.observe_only else "LIVE"
        print(f"[{mode}] COPY #{signal.alpha_rank} {signal.alpha_username} "
              f"({signal.alpha_category}) → {signal.outcome} @ ${price:.3f} "
              f"| {tokens:.0f} tokens, ${cost:.2f} "
              f"| {signal.market_question[:50]}...")

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def _determine_winner(self, slug: str) -> Optional[str]:
        """Check if market has resolved via Gamma API."""
        try:
            url = f"{GAMMA_API}/markets/slug/{slug}"
            resp = requests.get(url, timeout=10)
            if resp.status_code != 200:
                return None
            market = resp.json()
            if not market:
                return None

            # Check outcomePrices
            prices_raw = market.get("outcomePrices", "")
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except (json.JSONDecodeError, TypeError):
                    return None
            else:
                prices = prices_raw

            if not prices or len(prices) < 2:
                return None

            yes_p = float(prices[0])
            no_p = float(prices[1])

            if yes_p > 0.95:
                return "Yes"
            elif no_p > 0.95:
                return "No"

            # Also check if market is closed
            closed = market.get("closed", False)
            if closed:
                if yes_p > 0.5:
                    return "Yes"
                elif no_p > 0.5:
                    return "No"

            return None
        except Exception as e:
            logger.debug(f"Settle check error for {slug}: {e}")
            return None

    def settle_positions(self):
        """Check all pending positions for resolution."""
        if not self.positions:
            return

        # Collect unique slugs
        pending_slugs = set()
        for pos in self.positions.values():
            if not pos.resolved:
                pending_slugs.add(pos.slug)

        if not pending_slugs:
            return

        # Check each slug
        winners: Dict[str, Optional[str]] = {}
        for slug in pending_slugs:
            winners[slug] = self._determine_winner(slug)
            time.sleep(0.1)

        resolved_count = sum(1 for v in winners.values() if v is not None)
        if resolved_count > 0:
            logger.info(f"Settle: {len(pending_slugs)} pending, {resolved_count} resolved")

        # Resolve positions
        for slug, pos in list(self.positions.items()):
            if pos.resolved:
                continue
            winner = winners.get(slug)
            if winner is None:
                continue

            won = (pos.outcome == winner)
            if won:
                payout = pos.num_tokens * 1.0  # Binary pays $1 per token
                pnl = payout - pos.cost
                pos.won = True
                pos.payout = payout
                self.balance += payout
                self.wins += 1
                self.total_payout += payout
                self.daily_pnl += pnl
                result = "won"
            else:
                pnl = -pos.cost
                pos.won = False
                pos.payout = 0
                self.losses += 1
                self.daily_pnl += pnl
                result = "lost"

            pos.resolved = True
            self._write_resolved(pos, result, pnl)

            wr = (self.wins / (self.wins + self.losses) * 100) if (self.wins + self.losses) > 0 else 0
            print(f"  [{result.upper()}] {pos.outcome} @ ${pos.entry_price:.3f} "
                  f"→ PnL ${pnl:+.2f} | Balance: ${self.balance:.2f} "
                  f"| WR: {wr:.1f}% ({self.wins}W/{self.losses}L) "
                  f"| {pos.market_question[:40]}...")

        # Clean up resolved positions
        self.positions = {k: v for k, v in self.positions.items() if not v.resolved}
        self._save_pending()

    # ------------------------------------------------------------------
    # Status display
    # ------------------------------------------------------------------

    def print_status(self):
        """Print current status."""
        decided = self.wins + self.losses
        wr = (self.wins / decided * 100) if decided > 0 else 0
        pnl = self.total_payout - self.total_wagered
        pending = sum(1 for p in self.positions.values() if not p.resolved)

        print(f"\n{'='*60}")
        print(f"  COPY SNIPER — {'OBSERVE' if self.config.observe_only else 'LIVE'}")
        print(f"  Balance: ${self.balance:.2f} (started: ${self.starting_balance:.2f})")
        print(f"  Trades: {self.total_trades} | W:{self.wins} L:{self.losses} P:{pending}")
        print(f"  Win Rate: {wr:.1f}% | PnL: ${pnl:+.2f}")
        print(f"  Daily PnL: ${self.daily_pnl:+.2f}")
        print(f"  Alphas: {self.tracker.get_alpha_count()} wallets tracked")
        print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        """Main event loop."""
        print("\n" + "=" * 60)
        print("  COPY SNIPER — Alpha Wallet Copy-Trade System")
        print(f"  Mode: {'OBSERVE (paper)' if self.config.observe_only else 'LIVE'}")
        print(f"  Bankroll: ${self.config.bankroll:.2f}")
        print(f"  Categories: {', '.join(self.config.categories)}")
        print(f"  Poll interval: {self.config.poll_interval}s")
        if self.config.max_hours_to_resolution > 0:
            print(f"  Max resolve time: {self.config.max_hours_to_resolution:.0f}h (skip long-dated)")
        print("=" * 60 + "\n")

        # Initial alpha discovery
        print("[CopySniper] Discovering alpha wallets...")
        self.tracker.discover_alphas()
        self.last_alpha_refresh = time.time()
        print(f"[CopySniper] Tracking {self.tracker.get_alpha_count()} alpha wallets\n")

        if self.tracker.get_alpha_count() == 0:
            print("[CopySniper] ERROR: No alpha wallets found. Check API or filters.")
            return

        last_settle = time.time()
        last_status = time.time()
        cycle = 0

        while True:
            try:
                cycle += 1
                now = time.time()

                # Refresh alphas periodically
                if now - self.last_alpha_refresh > self.config.alpha_refresh_hours * 3600:
                    print("[CopySniper] Refreshing alpha wallets...")
                    self.tracker.discover_alphas()
                    self.last_alpha_refresh = now

                # Poll for new trades
                signals = self.tracker.poll_new_trades()

                for signal in signals:
                    trade = self.evaluate_signal(signal)
                    if trade is not None:
                        self.execute_paper_trade(trade)

                # Settlement check
                if now - last_settle >= self.config.settle_interval:
                    self.settle_positions()
                    last_settle = now

                # Status display every 5 minutes
                if now - last_status >= 300:
                    self.print_status()
                    last_status = now

                # Housekeeping
                if cycle % 100 == 0:
                    self.tracker.prune_seen_trades()

                await asyncio.sleep(self.config.poll_interval)

            except KeyboardInterrupt:
                print("\n[CopySniper] Shutting down...")
                self._save_pending()
                self.print_status()
                break
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                await asyncio.sleep(10)
