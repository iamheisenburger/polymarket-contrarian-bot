"""
Market Maker Strategy — Two-sided quoting on crypto binary markets.

Provides liquidity to Polymarket binary crypto markets (BTC/ETH/SOL Up/Down)
by posting limit orders on BOTH sides of our fair value estimate. Profits from
the bid-ask spread without needing to predict direction.

How it works:
    1. Binance WebSocket provides real-time crypto price
    2. Fair value calculator computes P(Up) using Black-Scholes binary pricing
    3. We post BUY orders below fair value on both Up and Down sides
    4. When a taker fills one side, we profit (bought below fair value)
    5. If BOTH sides fill: guaranteed profit (total cost < $1.00, payout = $1.00)

Key insight: Polymarket binary crypto orderbooks are thin ($0.01/$0.99).
    We provide the liquidity that takers need, earning the spread.

Risk management:
    - Max inventory per side (don't accumulate too much directional risk)
    - Stop quoting in last 2 minutes before expiry (gamma risk too high)
    - Widen spread when inventory is tilted
    - Cancel all orders on market change
    - Balance-aware: won't quote if USDC balance too low
    - Auto-redemption of winning tokens on market settlement

Usage:
    from strategies.market_maker import MarketMakerStrategy, MarketMakerConfig

    config = MarketMakerConfig(coin="BTC", timeframe="15m", quote_size=2.0)
    strategy = MarketMakerStrategy(bot, config)
    await strategy.run()
"""

import asyncio
import time
import math
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone

from lib.binance_ws import BinancePriceFeed
from lib.fair_value import BinaryFairValue, FairValue
from lib.console import Colors, StatusDisplay, format_countdown
from lib.trade_logger import TradeLogger
from strategies.base import BaseStrategy, StrategyConfig
from src.bot import TradingBot, OrderResult
from src.websocket_client import OrderbookSnapshot


@dataclass
class MarketMakerConfig(StrategyConfig):
    """Market maker configuration."""

    # Quoting parameters
    half_spread: float = 0.04       # Half-spread around fair value (4 cents each side)
    min_edge: float = 0.02          # Minimum edge to quote (2 cents)

    # Kelly Criterion position sizing
    kelly_fraction: float = 0.50    # Fractional Kelly (0.5 = half Kelly)
    starting_bankroll: float = 10.0 # Starting capital in USDC
    min_quote_usdc: float = 1.0     # Minimum quote size (Polymarket floor)
    max_quote_usdc: float = 10.0    # Max USDC per quote (Kelly ceiling)

    # Inventory limits
    max_inventory_per_side: int = 5  # Max fills per side before stopping quotes
    inventory_skew: float = 0.02     # Extra spread on heavy side per unit of inventory

    # Timing
    requote_threshold: float = 0.02  # Re-quote if fair value moved by this much
    min_requote_interval: float = 5.0  # Minimum seconds between requotes
    stop_quoting_seconds: int = 120   # Stop quoting this many seconds before expiry

    # Risk — Kelly is the only protection, no artificial loss limit
    min_balance: float = 1.0           # Stop if balance below Polymarket minimum

    # Trade logging
    log_file: str = "data/mm_trades.csv"

    # Observe mode (log but don't trade)
    observe_only: bool = False

    def __post_init__(self):
        self.take_profit = 99.0  # Disabled — we manage exits ourselves
        self.stop_loss = 99.0
        self.max_positions = 20   # We'll manage inventory ourselves


class QuoteState:
    """Tracks our outstanding quotes and inventory."""

    def __init__(self):
        # Active order IDs
        self.up_order_id: Optional[str] = None
        self.down_order_id: Optional[str] = None

        # Prices and sizes we quoted at
        self.up_quote_price: float = 0.0
        self.down_quote_price: float = 0.0
        self.up_quote_size: float = 0.0
        self.down_quote_size: float = 0.0

        # Fair value when we last quoted
        self.last_fair_up: float = 0.5
        self.last_quote_time: float = 0.0

        # Inventory tracking (tokens we hold from fills)
        self.up_inventory: float = 0.0     # Up tokens held
        self.down_inventory: float = 0.0   # Down tokens held
        self.up_cost_basis: float = 0.0    # Total USDC spent on Up
        self.down_cost_basis: float = 0.0

        # Session stats
        self.quotes_posted: int = 0
        self.fills_up: int = 0
        self.fills_down: int = 0
        self.markets_settled: int = 0
        self.realized_pnl: float = 0.0
        self.total_volume: float = 0.0

    @property
    def net_inventory(self) -> float:
        """Net directional exposure (positive = long Up)."""
        return self.up_inventory - self.down_inventory

    @property
    def total_cost(self) -> float:
        """Total USDC tied up in inventory."""
        return self.up_cost_basis + self.down_cost_basis

    @property
    def has_active_quotes(self) -> bool:
        return self.up_order_id is not None or self.down_order_id is not None

    def record_fill(self, side: str, price: float, size: float, usdc: float):
        """Record a fill on one side."""
        if side == "up":
            self.up_inventory += size
            self.up_cost_basis += usdc
            self.fills_up += 1
        else:
            self.down_inventory += size
            self.down_cost_basis += usdc
            self.fills_down += 1
        self.total_volume += usdc

    def settle_market(self, winning_side: str) -> float:
        """
        Settle inventory when market resolves. Returns PnL.

        Winning tokens pay $1.00 each, losing tokens pay $0.00.
        """
        if winning_side == "up":
            pnl = self.up_inventory * 1.0 - self.up_cost_basis
            pnl -= self.down_cost_basis  # Down tokens worth $0
        else:
            pnl = self.down_inventory * 1.0 - self.down_cost_basis
            pnl -= self.up_cost_basis
        self.realized_pnl += pnl
        self.markets_settled += 1
        # Reset inventory
        self.up_inventory = 0.0
        self.down_inventory = 0.0
        self.up_cost_basis = 0.0
        self.down_cost_basis = 0.0
        return pnl

    def reset_for_new_market(self):
        """Reset quote state for a new market (keep stats)."""
        self.up_order_id = None
        self.down_order_id = None
        self.up_quote_price = 0.0
        self.down_quote_price = 0.0
        self.up_quote_size = 0.0
        self.down_quote_size = 0.0
        self.last_fair_up = 0.5
        self.last_quote_time = 0.0


class MarketMakerStrategy(BaseStrategy):
    """
    Two-sided market making strategy for binary crypto markets.

    Posts limit BUY orders on both Up and Down sides below our fair value.
    Profits from the spread when takers fill against us.
    """

    def __init__(self, bot: TradingBot, config: MarketMakerConfig):
        super().__init__(bot, config)
        self.mc = config  # Market maker config shorthand

        # Fair value calculator
        self.fv_calc = BinaryFairValue()

        # Binance real-time price feed
        self.binance = BinancePriceFeed(coins=[config.coin])

        # Quote state
        self.quotes = QuoteState()

        # Trade logger
        self.trade_logger = TradeLogger(config.log_file)

        # Strike price for current market (crypto price at market open)
        self._strike_price: float = 0.0
        self._market_end_ts: float = 0.0

        # Current fair value and last known prices (for settlement)
        self._current_fv: Optional[FairValue] = None
        self._last_prices: Dict[str, float] = {}

        # USDC balance
        self._balance: float = 0.0
        self._last_balance_check: float = 0.0

        # Session tracking
        self._session_start = time.time()
        self._current_slug: str = ""

    async def start(self) -> bool:
        """Start strategy with Binance price feed."""
        # Start Binance WebSocket first
        self.log("Starting Binance price feed...")
        if not await self.binance.start():
            self.log("Failed to start Binance price feed", "error")
            return False

        spot = self.binance.get_price(self.mc.coin)
        self.log(f"Binance {self.mc.coin} price: ${spot:,.2f}", "success")

        # Check USDC balance
        self._refresh_balance()
        if self._balance > 0:
            self.log(f"USDC balance: ${self._balance:.2f}", "success")
        else:
            self.log("Could not query balance (will continue)", "warning")

        # Start base strategy (market manager + Polymarket WebSocket)
        if not await super().start():
            return False

        # Set initial strike price
        self._set_strike_from_current_market()

        return True

    async def stop(self):
        """Stop strategy and cancel all quotes."""
        await self._cancel_all_quotes()
        await self.binance.stop()
        await super().stop()

    def _refresh_balance(self):
        """Query USDC balance from Polymarket (rate-limited to every 60s)."""
        now = time.time()
        if now - self._last_balance_check < 60:
            return
        self._last_balance_check = now
        bal = self.bot.get_usdc_balance()
        if bal is not None:
            self._balance = bal

    def _set_strike_from_current_market(self):
        """
        Determine the strike price for the current market.

        The strike is the crypto price at the time the market was created.
        We estimate from the market price + current Binance price.
        """
        market = self.current_market
        if not market:
            return

        slug = market.slug
        self._current_slug = slug

        # Parse market end time from slug
        try:
            ts_str = slug.rsplit("-", 1)[-1]
            market_start_ts = int(ts_str)
            duration = 900 if self.mc.timeframe == "15m" else 300
            self._market_end_ts = market_start_ts + duration
        except (ValueError, IndexError):
            self._market_end_ts = 0

        spot = self.binance.get_price(self.mc.coin)
        if spot <= 0:
            return

        # Use current market mid price to back-solve for strike
        up_price = self.market.get_mid_price("up")
        if 0.1 < up_price < 0.9:
            vol = self.binance.get_volatility(self.mc.coin)
            secs_left = self._seconds_to_expiry()
            if secs_left > 30 and vol > 0:
                from lib.fair_value import SECONDS_PER_YEAR
                T = secs_left / SECONDS_PER_YEAR
                sigma_sqrt_t = vol * math.sqrt(T)
                d = self._approx_inv_normal(up_price)
                self._strike_price = spot * math.exp(-d * sigma_sqrt_t)
            else:
                self._strike_price = spot
        else:
            self._strike_price = spot

        self.log(
            f"Market: {slug} | Strike: ${self._strike_price:,.0f} | "
            f"Expiry: {self._seconds_to_expiry():.0f}s",
            "info"
        )

    @staticmethod
    def _approx_inv_normal(p: float) -> float:
        """Approximate inverse normal CDF (Abramowitz & Stegun 26.2.23)."""
        if p <= 0.0 or p >= 1.0:
            return 0.0
        if p < 0.5:
            return -MarketMakerStrategy._approx_inv_normal(1.0 - p)
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)

    def _seconds_to_expiry(self) -> float:
        """Get seconds until current market expires."""
        if self._market_end_ts <= 0:
            return 600
        return max(0, self._market_end_ts - time.time())

    def _calculate_fair_value(self) -> Optional[FairValue]:
        """Calculate fair value using current Binance price."""
        spot = self.binance.get_price(self.mc.coin)
        if spot <= 0 or self._strike_price <= 0:
            return None

        secs = self._seconds_to_expiry()
        vol = self.binance.get_volatility(self.mc.coin)

        return self.fv_calc.calculate(
            spot=spot,
            strike=self._strike_price,
            seconds_to_expiry=secs,
            volatility=vol,
        )

    def _kelly_quote_usdc(self, fair_prob: float, quote_price: float) -> float:
        """
        Calculate Kelly-optimal USDC size for a quote.

        Each fill is a binary bet: buy at `quote_price`, win probability
        is `fair_prob`, payout is $1.00 per token on win, $0.00 on loss.

        Kelly fraction: f = (p * b - q) / b
            where p = win probability, q = 1-p, b = net payout ratio

        Args:
            fair_prob: Our estimated probability this side wins (from fair value)
            quote_price: Price we're quoting at (what we pay per token)

        Returns:
            USDC to risk on this quote (after half-Kelly adjustment)
        """
        if quote_price <= 0.01 or quote_price >= 0.99 or fair_prob <= 0:
            return 0.0

        p = fair_prob          # Win probability
        q = 1.0 - p            # Loss probability
        b = (1.0 / quote_price) - 1.0  # Net payout ratio (win $1, paid $price → net = 1/price - 1)

        if b <= 0:
            return 0.0

        # Kelly fraction
        kelly_f = (p * b - q) / b
        if kelly_f <= 0:
            return 0.0  # No edge — don't quote

        # Apply fractional Kelly
        fraction = kelly_f * self.mc.kelly_fraction

        # Get current bankroll (use live balance if available, else starting)
        bankroll = self._balance if self._balance > 0 else self.mc.starting_bankroll

        # Account for USDC already tied up in inventory
        available = bankroll - self.quotes.total_cost
        if available < self.mc.min_quote_usdc:
            return 0.0

        usdc = fraction * available

        # Clamp to min/max
        usdc = max(self.mc.min_quote_usdc, min(self.mc.max_quote_usdc, usdc))

        # Don't exceed available balance
        usdc = min(usdc, available)

        return usdc

    def _calculate_quotes(self, fv: FairValue) -> Tuple[float, float]:
        """
        Calculate quote prices for Up and Down sides.

        Returns:
            (up_quote, down_quote) — prices to post BUY orders at
        """
        half = self.mc.half_spread

        # Inventory skew: widen spread on the side where we're heavy
        up_skew = max(0, self.quotes.up_inventory) * self.mc.inventory_skew
        down_skew = max(0, self.quotes.down_inventory) * self.mc.inventory_skew

        up_quote = fv.fair_up - half - up_skew
        down_quote = fv.fair_down - half - down_skew

        # Clamp to valid range
        up_quote = max(0.01, min(0.99, up_quote))
        down_quote = max(0.01, min(0.99, down_quote))

        return up_quote, down_quote

    def _should_requote(self, fv: FairValue) -> bool:
        """Check if we need to re-quote (fair value moved enough)."""
        if not self.quotes.has_active_quotes:
            return True

        now = time.time()
        if now - self.quotes.last_quote_time < self.mc.min_requote_interval:
            return False

        fv_diff = abs(fv.fair_up - self.quotes.last_fair_up)
        return fv_diff >= self.mc.requote_threshold

    async def _post_quotes(self, fv: FairValue):
        """Post or update two-sided quotes with Kelly-sized positions."""
        if self.mc.observe_only:
            return

        # Balance check (rate-limited)
        self._refresh_balance()
        if self._balance > 0 and self._balance < self.mc.min_balance:
            self.log(f"Balance ${self._balance:.2f} below minimum — done", "warning")
            return

        up_quote, down_quote = self._calculate_quotes(fv)

        # Check minimum edge
        up_edge = fv.fair_up - up_quote
        down_edge = fv.fair_down - down_quote

        # Check inventory limits
        post_up = self.quotes.fills_up < self.mc.max_inventory_per_side
        post_down = self.quotes.fills_down < self.mc.max_inventory_per_side

        # Cancel existing quotes first
        await self._cancel_all_quotes()

        # Kelly-sized USDC per quote
        up_usdc = self._kelly_quote_usdc(fv.fair_up, up_quote) if up_edge >= self.mc.min_edge else 0
        down_usdc = self._kelly_quote_usdc(fv.fair_down, down_quote) if down_edge >= self.mc.min_edge else 0

        # Convert USDC to token count
        up_tokens = round(up_usdc / up_quote, 2) if up_quote > 0.01 and up_usdc > 0 else 0
        down_tokens = round(down_usdc / down_quote, 2) if down_quote > 0.01 and down_usdc > 0 else 0

        # Post new quotes
        tasks = []
        if post_up and up_tokens >= 1.0:
            tasks.append(self._post_one_quote("up", up_quote, up_tokens))
        if post_down and down_tokens >= 1.0:
            tasks.append(self._post_one_quote("down", down_quote, down_tokens))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for r in results:
                if isinstance(r, Exception):
                    self.log(f"Quote error: {r}", "error")

        # Update quote state
        self.quotes.last_fair_up = fv.fair_up
        self.quotes.last_quote_time = time.time()
        self.quotes.quotes_posted += len(tasks)

    async def _post_one_quote(self, side: str, price: float, size: float) -> Optional[str]:
        """Post a single BUY quote on one side."""
        token_id = self.token_ids.get(side)
        if not token_id:
            return None

        price = round(price, 2)
        size = round(size, 2)
        if size < 1.0:
            return None

        result = await self.bot.place_order(
            token_id=token_id,
            price=price,
            size=size,
            side="BUY",
        )

        if result.success:
            usdc_cost = price * size
            if side == "up":
                self.quotes.up_order_id = result.order_id
                self.quotes.up_quote_price = price
                self.quotes.up_quote_size = size
            else:
                self.quotes.down_order_id = result.order_id
                self.quotes.down_quote_price = price
                self.quotes.down_quote_size = size

            fv_val = self._current_fv
            fair = fv_val.fair_up if side == "up" else fv_val.fair_down if fv_val else 0
            self.log(
                f"QUOTE {side.upper()} @ {price:.2f} x{size:.0f} "
                f"(${usdc_cost:.2f}) fair={fair:.2f}",
                "trade"
            )
            return result.order_id
        else:
            self.log(f"Quote failed ({side}): {result.message}", "error")
            return None

    async def _cancel_all_quotes(self):
        """Cancel all outstanding quotes."""
        tasks = []
        if self.quotes.up_order_id:
            tasks.append(self.bot.cancel_order(self.quotes.up_order_id))
            self.quotes.up_order_id = None
        if self.quotes.down_order_id:
            tasks.append(self.bot.cancel_order(self.quotes.down_order_id))
            self.quotes.down_order_id = None

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _check_fills(self):
        """
        Check if any of our quotes got filled.

        Uses the open orders cache (refreshed in background by BaseStrategy).
        When an order disappears, confirms it was filled by checking order status.
        """
        orders = self.open_orders  # Cached from base class

        for side, order_id_attr, price_attr, size_attr in [
            ("up", "up_order_id", "up_quote_price", "up_quote_size"),
            ("down", "down_order_id", "down_quote_price", "down_quote_size"),
        ]:
            order_id = getattr(self.quotes, order_id_attr)
            if not order_id:
                continue

            still_open = any(o.get("id") == order_id for o in orders)
            if still_open:
                continue

            # Order disappeared — verify it was filled (not cancelled)
            order_detail = await self.bot.get_order(order_id)
            if order_detail:
                status = order_detail.get("status", "").lower()
                # Only count as fill if actually matched/filled
                if status not in ("matched", "filled"):
                    self.log(f"Order {order_id[:8]} was {status} (not a fill)", "info")
                    setattr(self.quotes, order_id_attr, None)
                    continue

            # Confirmed fill
            price = getattr(self.quotes, price_attr)
            size = getattr(self.quotes, size_attr)
            usdc = price * size

            self.quotes.record_fill(side, price, size, usdc)
            setattr(self.quotes, order_id_attr, None)

            # Log fill to CSV
            spot = self.binance.get_price(self.mc.coin)
            vol = self.binance.get_volatility(self.mc.coin)
            fv = self._current_fv
            other_price = self._last_prices.get("down" if side == "up" else "up", 0)

            self.trade_logger.log_trade(
                market_slug=self._current_slug,
                coin=self.mc.coin,
                timeframe=self.mc.timeframe,
                side=side,
                entry_price=price,
                bet_size_usdc=usdc,
                num_tokens=size,
                bankroll=self._balance,
                btc_price=spot,
                other_side_price=other_price,
                volatility_std=vol,
            )

            self.log(
                f"FILL {side.upper()} @ {price:.2f} x{size:.0f} (${usdc:.2f}) | "
                f"Inv: Up={self.quotes.up_inventory:.0f} Down={self.quotes.down_inventory:.0f}",
                "success"
            )

    def _resolve_trades(self, slug: str):
        """
        Determine trade outcomes when market settles.

        Uses last known prices: winning side trends to $1.00, losing to $0.00.
        Logs outcomes to CSV and settles inventory.
        """
        if self.quotes.up_inventory <= 0 and self.quotes.down_inventory <= 0:
            return

        # Determine winner from last known prices
        up_price = self._last_prices.get("up", 0)
        down_price = self._last_prices.get("down", 0)

        if up_price <= 0 and down_price <= 0:
            self.log(f"Settlement: {slug} (no price data)", "warning")
            return

        winning_side = "up" if up_price > 0.50 else "down"
        pnl = self.quotes.settle_market(winning_side)

        # Log outcomes for all pending trades in this market
        pending = self.trade_logger.get_pending_slugs()
        for pending_slug in pending:
            if pending_slug == slug:
                # Determine if each logged trade won
                # We logged individual fills, the outcome depends on which side
                # For simplicity, log the aggregate outcome
                won = pnl >= 0
                self.trade_logger.log_outcome(
                    market_slug=slug,
                    won=won,
                    payout=max(0, pnl + self.quotes.total_cost),
                )

        outcome_str = f"{'WIN' if pnl >= 0 else 'LOSS'}"
        self.log(
            f"SETTLED {slug}: {outcome_str} ${pnl:+.2f} "
            f"({winning_side.upper()} won) | "
            f"Session PnL: ${self.quotes.realized_pnl:+.2f}",
            "success" if pnl >= 0 else "warning"
        )

    # === BaseStrategy hooks ===

    async def on_book_update(self, snapshot: OrderbookSnapshot):
        """Handle orderbook update."""
        pass  # We use on_tick for timing control

    async def on_tick(self, prices: Dict[str, float]):
        """Main strategy tick — calculate fair value and manage quotes."""
        # Save prices for settlement detection
        if prices:
            self._last_prices = dict(prices)

        # Calculate fair value
        fv = self._calculate_fair_value()
        if not fv:
            return

        self._current_fv = fv
        secs_left = self._seconds_to_expiry()

        # Check for fills
        await self._check_fills()

        # Stop quoting near expiry (gamma risk too high)
        if secs_left < self.mc.stop_quoting_seconds:
            if self.quotes.has_active_quotes:
                self.log(
                    f"Expiry in {secs_left:.0f}s — pulling quotes",
                    "warning"
                )
                await self._cancel_all_quotes()
            return

        # Check if market is too close to certainty
        if fv.dominant_prob > 0.92:
            if self.quotes.has_active_quotes:
                await self._cancel_all_quotes()
            return

        # No artificial loss limit — Kelly Criterion manages risk.
        # If bankroll goes to $0, balance check in _post_quotes stops us.

        # Post or update quotes if needed
        if self._should_requote(fv):
            await self._post_quotes(fv)

    def on_market_change(self, old_slug: str, new_slug: str):
        """
        Handle market transition.

        When a market changes, the old one has settled. We determine the
        winner from last known prices, settle inventory, auto-redeem, and
        prepare for the next market.
        """
        # Cancel any lingering quotes
        # (can't await here — use fire-and-forget)
        if self.quotes.has_active_quotes:
            self.quotes.up_order_id = None
            self.quotes.down_order_id = None

        # Resolve trades and settle inventory
        self._resolve_trades(old_slug)

        # Auto-redeem winning tokens back to USDC
        try:
            results = self.bot.redeem_all()
            if results:
                self.log(f"Auto-redeemed {len(results)} position(s) to USDC", "success")
        except Exception as e:
            self.log(f"Redeem check: {e}", "info")

        # Refresh balance after settlement
        self._last_balance_check = 0  # Force refresh
        self._refresh_balance()

        # Reset for new market
        self.quotes.reset_for_new_market()
        self._last_prices.clear()

        # Set new strike (with small delay for data to arrive)
        try:
            loop = asyncio.get_running_loop()
            loop.call_later(2.0, self._set_strike_from_current_market)
        except RuntimeError:
            self._set_strike_from_current_market()

    def _print_summary(self):
        """Print session summary on exit."""
        self._status_mode = False
        print()
        elapsed = time.time() - self._session_start
        mins = elapsed / 60

        print(f"{'=' * 60}")
        print(f"  SESSION SUMMARY")
        print(f"{'=' * 60}")
        print(f"  Duration:       {mins:.0f} minutes")
        print(f"  Markets traded: {self.quotes.markets_settled}")
        print(f"  Quotes posted:  {self.quotes.quotes_posted}")
        print(f"  Fills:          {self.quotes.fills_up} Up / {self.quotes.fills_down} Down")
        print(f"  Volume:         ${self.quotes.total_volume:.2f}")
        print(f"  Realized PnL:   ${self.quotes.realized_pnl:+.2f}")
        if self._balance > 0:
            print(f"  Final balance:  ${self._balance:.2f}")
        print(f"{'=' * 60}")

    def render_status(self, prices: Dict[str, float]):
        """Render the market maker status display."""
        fv = self._current_fv
        spot = self.binance.get_price(self.mc.coin)
        vol = self.binance.get_volatility(self.mc.coin)
        secs = self._seconds_to_expiry()
        market = self.current_market

        lines = []
        lines.append("")
        lines.append(f"{'=' * 60}")

        mode_tag = "OBSERVE" if self.mc.observe_only else "LIVE"
        ws_tag = "LIVE" if self.is_connected else "DISC"
        lines.append(f"  MARKET MAKER [{mode_tag}] [{ws_tag}] — {self.mc.coin} {self.mc.timeframe}")
        lines.append(f"{'=' * 60}")

        # Market info
        if market:
            mins, s = divmod(int(secs), 60)
            lines.append(f"  Market: {market.slug}")
            lines.append(f"  Expiry: {mins}m {s}s")
        else:
            lines.append("  Market: (waiting...)")

        lines.append("")

        # Pricing
        if fv and spot > 0:
            lines.append(f"  {self.mc.coin} Spot:  ${spot:>10,.2f}  (Binance, {self.binance.get_age(self.mc.coin):.1f}s ago)")
            lines.append(f"  Strike:     ${self._strike_price:>10,.2f}")
            lines.append(f"  Vol:        {vol * 100:>9.1f}%  (annualized)")
            lines.append("")
            lines.append(f"  Fair Up:    {fv.fair_up:>9.2f}   |  Market Up:   {prices.get('up', 0):>5.2f}")
            lines.append(f"  Fair Down:  {fv.fair_down:>9.2f}   |  Market Down: {prices.get('down', 0):>5.2f}")

            mkt_up = prices.get("up", 0.5)
            edge = fv.fair_up - mkt_up
            if abs(edge) > 0.01:
                direction = "UP underpriced" if edge > 0 else "DOWN underpriced"
                lines.append(f"  Edge:       {abs(edge):>9.2f}   ({direction})")

        lines.append("")

        # Quotes
        if self.quotes.has_active_quotes:
            parts = []
            if self.quotes.up_order_id:
                parts.append(f"UP @ {self.quotes.up_quote_price:.2f}")
            if self.quotes.down_order_id:
                parts.append(f"DOWN @ {self.quotes.down_quote_price:.2f}")
            lines.append(f"  Quotes:  {' | '.join(parts)}")
        elif secs < self.mc.stop_quoting_seconds:
            lines.append("  Quotes:  PAUSED (near expiry)")
        elif fv and fv.dominant_prob > 0.92:
            lines.append("  Quotes:  PAUSED (extreme fair value)")
        else:
            lines.append("  Quotes:  (none active)")

        # Inventory
        lines.append(
            f"  Inventory: Up={self.quotes.up_inventory:.0f} "
            f"Down={self.quotes.down_inventory:.0f} "
            f"Net={self.quotes.net_inventory:+.0f}"
        )
        lines.append(f"  Cost basis: ${self.quotes.total_cost:.2f}")

        lines.append("")

        # Session stats
        elapsed = time.time() - self._session_start
        mins_elapsed = elapsed / 60
        lines.append(
            f"  Session: {mins_elapsed:.0f}m | "
            f"Quotes: {self.quotes.quotes_posted} | "
            f"Fills: {self.quotes.fills_up}U/{self.quotes.fills_down}D | "
            f"Settled: {self.quotes.markets_settled}"
        )
        lines.append(
            f"  Volume: ${self.quotes.total_volume:.2f} | "
            f"PnL: ${self.quotes.realized_pnl:+.2f}"
        )
        if self._balance > 0:
            lines.append(f"  Balance: ${self._balance:.2f}")

        if self.mc.observe_only:
            lines.append("")
            lines.append("  *** OBSERVE MODE — not posting quotes ***")

        lines.append(f"{'=' * 60}")

        # Recent log messages
        for msg in self._log_buffer.get_messages()[-3:]:
            lines.append(f"  {msg}")

        output = "\n".join(lines)
        print(f"\033[H\033[J{output}", flush=True)
