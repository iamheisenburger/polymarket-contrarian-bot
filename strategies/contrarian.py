"""
Contrarian Cheap-Side Strategy - Asymmetric Binary Market Trading

Buys the cheap (unpopular) side of short-duration crypto binary markets
when crowd panic pushes one side to extreme probabilities. Holds to
settlement for full $1.00 payout on wins.

Strategy thesis:
    In 5-min and 15-min BTC/ETH Up/Down markets, the crowd frequently
    overreacts, pushing one side to 90%+ and the other to <10%.
    Data shows ~8.8% of markets reverse from the 5-cent level.
    At $0.05 entry with $1.00 payout (20x), you only need 5% win rate
    to break even. The 3.8% excess win rate is your edge.

Core rules:
    - Buy tokens priced at $0.03-$0.07 (configurable)
    - Fixed USDC bet size per trade (default $1.00)
    - Hold to settlement - NO early exit, NO TP/SL
    - The math only works over many trades with consistency

Usage:
    from strategies.contrarian import ContrarianStrategy, ContrarianConfig

    config = ContrarianConfig(coin="BTC", timeframe="5m", bet_size=1.0)
    strategy = ContrarianStrategy(bot, config)
    await strategy.run()
"""

import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List

from lib.console import Colors, format_countdown, StatusDisplay
from lib.trade_logger import TradeLogger
from lib.volatility_tracker import VolatilityTracker
from strategies.base import BaseStrategy, StrategyConfig
from src.bot import TradingBot
from src.websocket_client import OrderbookSnapshot


@dataclass
class ContrarianConfig(StrategyConfig):
    """Contrarian strategy configuration."""

    # Entry thresholds (buy when price is in this range)
    min_entry_price: float = 0.03  # Don't buy below this (too unlikely)
    max_entry_price: float = 0.07  # Don't buy above this (edge decreases)

    # Bet sizing (used as max when Kelly is enabled)
    bet_size: float = 1.0  # Max USDC per trade

    # Kelly Criterion position sizing
    kelly_fraction: float = 0.25  # Fractional Kelly (1/4 = conservative)
    estimated_win_rate: float = 0.088  # 8.8% from historical data
    starting_bankroll: float = 10.0  # Starting capital in USDC
    min_bet_size: float = 1.00  # Polymarket minimum for marketable orders is $1
    max_bet_fraction: float = 1.0  # Kelly is the safety - let it trade until balance runs out

    # Rate limiting
    max_trades_per_hour: int = 10
    min_seconds_between_trades: float = 30.0  # Cooldown between trades

    # Volatility filter (0 = disabled)
    min_volatility: float = 0.0

    # Observe-only mode (detect but don't execute)
    observe_only: bool = False

    # Daily loss limit
    daily_loss_limit: float = 10.0  # Stop trading after losing this much

    # Trade log file
    log_file: str = "data/trades.csv"

    def __post_init__(self):
        # Override base class TP/SL - we don't use them
        self.take_profit = 99.0  # Effectively disabled
        self.stop_loss = 99.0   # Effectively disabled
        self.max_positions = 5   # Allow multiple concurrent (different markets)


class ContrarianStrategy(BaseStrategy):
    """
    Contrarian Cheap-Side Trading Strategy.

    Monitors short-duration crypto binary markets and buys the cheap
    side when crowd panic creates mispriced tokens. Holds to settlement.
    """

    def __init__(self, bot: TradingBot, config: ContrarianConfig):
        """Initialize contrarian strategy."""
        super().__init__(bot, config)
        self.cc = config  # Shorthand for contrarian config

        # Trade logger
        self.logger = TradeLogger(config.log_file)

        # Volatility tracker
        self.vol_tracker = VolatilityTracker(window_seconds=1800)

        # Rate limiting state
        self._trades_this_hour: List[float] = []
        self._last_trade_time: float = 0
        self._daily_pnl: float = 0.0
        self._session_start: float = time.time()
        self._markets_scanned: int = 0
        self._opportunities_found: int = 0
        self._trades_skipped_rate_limit: int = 0
        self._trades_skipped_volatility: int = 0

        # Track which markets we already traded in (don't double-enter)
        self._traded_slugs: set = set()

        # Display
        self._display = StatusDisplay()

    async def on_book_update(self, snapshot: OrderbookSnapshot) -> None:
        """Record price data for volatility tracking."""
        # Track the "up" side price for volatility calculation
        up_token = self.token_ids.get("up", "")
        if snapshot.asset_id == up_token and snapshot.mid_price > 0:
            self.vol_tracker.record(snapshot.mid_price)

    async def on_tick(self, prices: Dict[str, float]) -> None:
        """
        Main strategy tick - check for cheap-side opportunities.

        This runs every 100ms. We check if either side is priced
        within our target range and place a trade if conditions are met.
        """
        self._markets_scanned += 1

        if not prices:
            return

        # Check daily loss limit
        if self._daily_pnl <= -self.cc.daily_loss_limit:
            return

        # Check both sides for cheap tokens
        for side in ["up", "down"]:
            price = prices.get(side, 0)
            if price <= 0:
                continue

            # Is this side cheap enough?
            if not (self.cc.min_entry_price <= price <= self.cc.max_entry_price):
                continue

            self._opportunities_found += 1

            # Don't trade same market twice
            market = self.current_market
            if not market:
                continue
            if market.slug in self._traded_slugs:
                continue

            # Rate limit check
            if not self._check_rate_limit():
                self._trades_skipped_rate_limit += 1
                continue

            # Volatility filter
            if not self.vol_tracker.is_volatile_enough(self.cc.min_volatility):
                self._trades_skipped_volatility += 1
                continue

            # Check orderbook depth - make sure there's actually liquidity
            ob = self.market.get_orderbook(side)
            if not ob or not ob.asks:
                continue

            # Use best ask price for execution (what we'd actually pay)
            execution_price = ob.best_ask
            if execution_price <= 0 or execution_price > self.cc.max_entry_price:
                continue

            # All conditions met - execute (or observe)
            if self.cc.observe_only:
                self.log(
                    f"[OBSERVE] Would buy {side.upper()} @ ${execution_price:.4f} "
                    f"(mid: ${price:.4f})",
                    "trade"
                )
                self._traded_slugs.add(market.slug)
            else:
                await self._execute_contrarian_buy(side, execution_price)

    def _kelly_bet_size(self, market_price: float) -> float:
        """
        Calculate bet size using fractional Kelly Criterion.

        Formula: F = kelly_fraction * (p - P) / (1 - P)
        Where:
            p = estimated probability of winning
            P = market price (what we pay)
            F = fraction of bankroll to bet

        Returns:
            Bet size in USDC, or 0 if no edge.
        """
        p = self.cc.estimated_win_rate
        P = market_price

        # No edge = no bet
        if p <= P:
            return 0.0

        # Full Kelly fraction
        full_kelly = (p - P) / (1 - P)

        # Apply fractional Kelly (e.g., 1/4 Kelly)
        kelly_f = full_kelly * self.cc.kelly_fraction

        # Current bankroll = real USDC balance from Polymarket (or fallback to estimate)
        live_balance = self.bot.get_usdc_balance()
        if live_balance is not None:
            bankroll = live_balance
        else:
            bankroll = self.cc.starting_bankroll + self._daily_pnl

        if bankroll <= 0:
            return 0.0

        # Bet size = Kelly fraction * bankroll
        bet = kelly_f * bankroll

        # Kelly confirms edge exists — enforce platform minimum ($1)
        # This bets more than pure Kelly suggests, but it's the Polymarket floor
        bet = max(bet, self.cc.min_bet_size)

        # Cap at max_bet_fraction of bankroll (hard safety limit)
        max_bet = self.cc.max_bet_fraction * bankroll
        bet = min(bet, max_bet)

        # Cap at configured max bet_size
        bet = min(bet, self.cc.bet_size)

        # Final sanity: if caps pushed bet below minimum, no trade
        if bet < self.cc.min_bet_size:
            return 0.0

        return round(bet, 2)

    async def _execute_contrarian_buy(self, side: str, price: float) -> None:
        """
        Execute a contrarian buy order with Kelly Criterion sizing.

        Args:
            side: "up" or "down"
            price: Execution price
        """
        market = self.current_market
        if not market:
            return

        token_id = self.token_ids.get(side)
        if not token_id:
            return

        # Calculate Kelly-optimal bet size
        bet_size = self._kelly_bet_size(price)
        if bet_size <= 0:
            self.log(
                f"Kelly says no bet at ${price:.4f} "
                f"(edge too small or bankroll depleted)",
                "info"
            )
            return

        # Calculate token quantity
        num_tokens = bet_size / price
        # Buy price slightly above ask to ensure fill
        buy_price = min(price + 0.01, 0.99)

        # Kelly sizing info
        bankroll = self.cc.starting_bankroll + self._daily_pnl
        kelly_pct = (bet_size / bankroll * 100) if bankroll > 0 else 0

        self.log(
            f"BUY {side.upper()} @ ${price:.4f} | "
            f"${bet_size:.2f} ({kelly_pct:.1f}% of ${bankroll:.2f}) -> "
            f"{num_tokens:.1f} tokens | Payout: ${num_tokens:.2f}",
            "trade"
        )

        result = await self.bot.place_order(
            token_id=token_id,
            price=buy_price,
            size=num_tokens,
            side="BUY"
        )

        if result.success:
            self.log(f"Order filled: {result.order_id}", "success")

            # Log trade
            self.logger.log_trade(
                market_slug=market.slug,
                coin=self.cc.coin,
                timeframe=self.cc.timeframe,
                side=side,
                entry_price=price,
                bet_size_usdc=bet_size,
                num_tokens=num_tokens,
            )

            # Track position (for display purposes)
            self.positions.open_position(
                side=side,
                token_id=token_id,
                entry_price=price,
                size=num_tokens,
                order_id=result.order_id,
            )

            # Update rate limit tracking
            self._trades_this_hour.append(time.time())
            self._last_trade_time = time.time()
            self._traded_slugs.add(market.slug)
        else:
            self.log(f"Order failed: {result.message}", "error")

    def _check_rate_limit(self) -> bool:
        """Check if we can place another trade."""
        now = time.time()

        # Cooldown between trades
        if now - self._last_trade_time < self.cc.min_seconds_between_trades:
            return False

        # Hourly limit
        one_hour_ago = now - 3600
        self._trades_this_hour = [t for t in self._trades_this_hour if t > one_hour_ago]
        if len(self._trades_this_hour) >= self.cc.max_trades_per_hour:
            return False

        return True

    def on_market_change(self, old_slug: str, new_slug: str) -> None:
        """
        Handle market expiry.

        When a market changes, the old one has settled. We check if
        we had a position and log the outcome.
        """
        self.prices.clear()
        self.vol_tracker.clear()
        # Clear positions from expired market (they settled)
        self.positions.clear()

        # Check if we had a trade in the old market
        if old_slug in self._traded_slugs:
            self.log(f"Market settled: {old_slug}", "info")

        # Auto-redeem any winning positions back to USDC
        try:
            results = self.bot.redeem_all()
            if results:
                self.log(f"Auto-redeemed {len(results)} position(s) to USDC", "success")
        except Exception as e:
            self.log(f"Redeem check failed: {e}", "info")

        self.log(f"New market: {new_slug}", "success")

    def render_status(self, prices: Dict[str, float]) -> None:
        """Render TUI status display."""
        d = self._display
        d.clear()

        # Header
        ws_status = f"{Colors.GREEN}LIVE{Colors.RESET}" if self.is_connected else f"{Colors.RED}DISC{Colors.RESET}"
        countdown = self._get_countdown_str()
        mode = f"{Colors.YELLOW}OBSERVE{Colors.RESET}" if self.cc.observe_only else f"{Colors.GREEN}LIVE{Colors.RESET}"

        d.add_bold_separator()
        d.add_line(
            f"{Colors.CYAN}CONTRARIAN{Colors.RESET} [{self.cc.coin}] [{self.cc.timeframe}] "
            f"[{ws_status}] [{mode}] Ends: {countdown}"
        )
        d.add_bold_separator()

        # Current market prices
        up_price = prices.get("up", 0)
        down_price = prices.get("down", 0)
        up_color = Colors.GREEN if self.cc.min_entry_price <= up_price <= self.cc.max_entry_price else Colors.DIM
        down_color = Colors.GREEN if self.cc.min_entry_price <= down_price <= self.cc.max_entry_price else Colors.DIM

        d.add_line(
            f"  UP:   {up_color}${up_price:.4f}{Colors.RESET}  |  "
            f"DOWN: {down_color}${down_price:.4f}{Colors.RESET}  |  "
            f"Target: ${self.cc.min_entry_price:.2f}-${self.cc.max_entry_price:.2f}"
        )

        # Orderbook depth at target prices
        for side in ["up", "down"]:
            ob = self.market.get_orderbook(side)
            if ob and ob.asks:
                best_ask = ob.best_ask
                depth = sum(level.size for level in ob.asks[:3])
                d.add_line(
                    f"  {side.upper():4} ask: ${best_ask:.4f}  depth(3): {depth:.1f} tokens"
                )

        d.add_separator()

        # Volatility info
        vol_std = self.vol_tracker.get_std_dev()
        vol_range = self.vol_tracker.get_price_range()
        vol_obs = self.vol_tracker.get_observation_count()
        vol_status = f"{Colors.GREEN}OK{Colors.RESET}" if self.vol_tracker.is_volatile_enough(self.cc.min_volatility) else f"{Colors.RED}LOW{Colors.RESET}"

        d.add_line(
            f"  Volatility: std={vol_std:.4f} range={vol_range:.4f} "
            f"obs={vol_obs} [{vol_status}]"
        )

        # Session stats
        stats = self.logger.stats
        runtime = time.time() - self._session_start
        runtime_min = runtime / 60

        d.add_separator()
        d.add_header("Session Stats")
        d.add_line(
            f"  Runtime: {runtime_min:.0f}m | "
            f"Markets scanned: {self._markets_scanned} | "
            f"Opportunities: {self._opportunities_found}"
        )
        d.add_line(
            f"  Trades: {stats.total_trades} (W:{stats.wins} L:{stats.losses} P:{stats.pending}) | "
            f"Win rate: {stats.win_rate:.1f}%"
        )
        d.add_line(
            f"  Wagered: ${stats.total_wagered:.2f} | "
            f"Payout: ${stats.total_payout:.2f} | "
            f"PnL: ${stats.total_pnl:+.2f}"
        )

        if self._trades_skipped_rate_limit or self._trades_skipped_volatility:
            d.add_line(
                f"  Skipped: rate_limit={self._trades_skipped_rate_limit} "
                f"volatility={self._trades_skipped_volatility}"
            )

        # Per-bucket performance (if any trades)
        if stats.bucket_trades:
            d.add_separator()
            d.add_header("Performance by Entry Price")
            for cents in sorted(stats.bucket_trades.keys()):
                trades = stats.bucket_trades[cents]
                wins = stats.bucket_wins.get(cents, 0)
                wr = stats.bucket_win_rate(cents)
                payout_mult = 1.0 / (cents / 100) if cents > 0 else 0
                d.add_line(
                    f"  ${cents/100:.2f} ({payout_mult:.0f}x): "
                    f"{trades} trades, {wr:.0f}% win rate ({wins}/{trades})"
                )

        # Risk status with Kelly info
        live_balance = self.bot.get_usdc_balance()
        bankroll = live_balance if live_balance is not None else (self.cc.starting_bankroll + self._daily_pnl)
        avg_price = (self.cc.min_entry_price + self.cc.max_entry_price) / 2
        kelly_bet = self._kelly_bet_size(avg_price)
        d.add_separator()
        d.add_line(
            f"  Kelly: {self.cc.kelly_fraction:.0%} frac | "
            f"Bankroll: ${bankroll:.2f} | "
            f"Next bet ~${kelly_bet:.2f} @ ${avg_price:.2f}"
        )
        d.add_line(
            f"  Daily PnL: ${self._daily_pnl:+.2f} / -${self.cc.daily_loss_limit:.2f} limit | "
            f"Trades/hr: {len(self._trades_this_hour)}/{self.cc.max_trades_per_hour}"
        )

        # Recent logs
        if self._log_buffer.messages:
            d.add_separator()
            d.add_header("Recent Events")
            for msg in self._log_buffer.get_messages():
                d.add_line(f"  {msg}")

        d.render()

    def _get_countdown_str(self) -> str:
        """Get formatted countdown string."""
        market = self.current_market
        if not market:
            return "--:--"
        mins, secs = market.get_countdown()
        return format_countdown(mins, secs)
