"""
Momentum Sniper Strategy — Directional edge trading on crypto binary markets.

Exploits the latency between Binance spot price movements and Polymarket
binary market repricing. When Binance moves but Polymarket hasn't caught up,
we snipe the mispriced side before the market adjusts.

How it works:
    1. Binance WebSocket provides sub-second crypto price updates
    2. Fair value calculator computes P(Up) from Black-Scholes binary pricing
    3. Compare fair value to Polymarket best ask prices
    4. When the ask is significantly below fair value → BUY (the market is slow)
    5. Hold to settlement: binary pays $1 on win, $0 on loss
    6. Auto-redeem winnings and compound into next market

Key differences from Market Maker:
    - Market Maker: passive, posts limit orders, profits from spread
    - Momentum Sniper: active, takes mispriced asks, profits from direction

Risk management:
    - Kelly Criterion position sizing (fractional Kelly)
    - Minimum edge threshold before entering
    - One position per side per market (no pyramiding)
    - Cooldown between trades to avoid overtrading
    - Stop trading near expiry (last 60s — outcome is too certain or too noisy)
    - Multi-coin scanning for maximum opportunity

Usage:
    from strategies.momentum_sniper import MomentumSniperStrategy, SniperConfig

    config = SniperConfig(coins=["BTC", "ETH"], timeframe="15m", bankroll=20.0)
    strategy = MomentumSniperStrategy(bot, config)
    await strategy.run()
"""

import asyncio
import logging
import math
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone

from lib.binance_ws import BinancePriceFeed
from lib.fair_value import BinaryFairValue, FairValue
from lib.console import Colors, LogBuffer, log
from lib.trade_logger import TradeLogger
from lib.market_manager import MarketManager, MarketInfo
from src.bot import TradingBot, OrderResult
from src.websocket_client import MarketWebSocket, OrderbookSnapshot
from src.gamma_client import GammaClient

# Taker fee rates per timeframe.
# Fee per token = price * (1 - price) * rate.
# Max fee at p=0.50: 5m=0.44%, 15m=1.56%, others=0%.
TAKER_FEE_RATES = {
    "5m": 0.0176,
    "15m": 0.0624,
    "4h": 0.0,
    "1h": 0.0,
    "daily": 0.0,
}


def taker_fee_per_token(price: float, timeframe: str) -> float:
    """Calculate taker fee per token at a given price and timeframe."""
    rate = TAKER_FEE_RATES.get(timeframe, 0.0)
    if rate <= 0:
        return 0.0
    return price * (1.0 - price) * rate


@dataclass
class SniperConfig:
    """Momentum sniper configuration."""

    # Coins to scan (more coins = more opportunities)
    coins: List[str] = field(default_factory=lambda: ["BTC"])
    timeframe: str = "15m"

    # Edge thresholds
    min_edge: float = 0.05        # Minimum edge to enter (5 cents)
    strong_edge: float = 0.10     # Strong edge — use larger Kelly (10 cents)

    # Position sizing
    kelly_fraction: float = 0.50    # Normal: half Kelly
    kelly_strong: float = 0.75      # Strong edge: 3/4 Kelly
    bankroll: float = 20.0          # Starting capital in USDC
    min_bet_usdc: float = 1.0       # Polymarket minimum ($1 floor)
    max_bet_usdc: float = 100.0     # Cap per trade (absolute)
    max_bet_fraction: float = 0.15  # Max fraction of bankroll per trade (15%)

    # Conservative mode: always bet minimum (5 tokens) to gather data.
    # Use this when bankroll is small and you need statistical significance
    # before committing to full Kelly sizing.
    min_size_mode: bool = False

    # Per-coin Kelly override: when min_size_mode is True, coins listed here
    # use Kelly sizing instead of min-size. Coins NOT listed stay at min-size.
    # Empty list = all coins follow min_size_mode setting.
    kelly_coins: List[str] = field(default_factory=list)

    # Hour blocking: UTC hours to skip trading entirely.
    # Empty list = trade all hours. E.g. [0, 2, 9, 16, 17] to block bad hours.
    blocked_hours: List[int] = field(default_factory=list)

    # Volatility filter: skip trading when realized vol exceeds this.
    # Data shows vol < 0.50 -> 35.8% WR vs 22.4% when vol > 0.50.
    # Set to 0.0 to disable.
    max_volatility: float = 0.50

    # Fixed volatility: override dynamic IV/RV with a constant value for FV calc.
    # Backtest shows vol=30% gives best trade selection (67% WR on 395 trades).
    # Set to 0.0 to use dynamic Deribit IV / Binance RV (default).
    fixed_volatility: float = 0.0

    # Momentum filter: minimum Binance price change (fraction) in trade direction
    # over the lookback period. 0.0005 = 0.05%. Set to 0.0 to disable.
    min_momentum: float = 0.0005

    # Momentum lookback period in seconds
    momentum_lookback: float = 30.0

    # Fair value confidence: minimum model confidence to trade.
    # FV 0.50 = coin flip (no edge). FV 0.60+ = model is confident.
    # Data: FV >= 0.60 → 80% WR. FV 0.50-0.52 → 26% WR.
    min_fair_value: float = 0.50     # 0.50 = disabled (default). 0.58+ recommended.

    # Late entry: minimum seconds elapsed in the window before considering trades.
    # 0 = disabled (enter any time). 600 = wait 10 min (5 min left in 15m window).
    # Forces the bot to wait for the trend to establish before entering.
    min_window_elapsed: float = 0

    # Max entry: maximum seconds elapsed in the window to consider trades.
    # 0 = disabled (enter until expiry). 180 = stop entering after 180s (120s left in 5m).
    # Prevents late entries where latency arbitrage has disappeared.
    max_window_elapsed: float = 0

    # Price thresholds (structural, not risk limits)
    max_entry_price: float = 0.85    # Above this the payout ratio is too low for edge to matter
    min_entry_price: float = 0.02    # Below Polymarket minimum tick

    # Market settings
    market_check_interval: float = 30.0

    # Price source for fair value calculation.
    # "binance" = Binance WebSocket (fast but NOT settlement source)
    # "chainlink" = Polymarket RTDS Chainlink feed (settlement source)
    price_source: str = "binance"

    # Vatic API: use exact Chainlink strike prices instead of Binance approximation
    use_vatic: bool = True

    # Logging
    log_file: str = "data/longshot_trades.csv"
    observe_only: bool = False

    # --- Edge Amplifier features ---

    # CUSUM decay detection: monitors cumulative evidence of WR drop.
    # When CUSUM alarm triggers, auto-reduces Kelly fraction to 25%.
    enable_cusum: bool = False
    cusum_threshold: float = 5.0       # Alarm threshold (higher = fewer false alarms)
    cusum_target_wr: float = 0.63      # Expected WR from backtest (conservative)

    # Adaptive Kelly: scale Kelly fraction based on Wilson lower bound of observed WR.
    # Auto-shrinks bets when WR confidence is low or dropping.
    adaptive_kelly: bool = False

    # Signal confirmation: wait N seconds and re-check edge before trading.
    # 0 = disabled (trade immediately). 30 = wait 30s and verify edge persists.
    confirm_gap: float = 0.0

    # Side filter: "both" (default), "up" (UP-ONLY), or "down" (DOWN-ONLY).
    # Backtest + live paper data shows DOWN side is -EV. UP-ONLY is structural.
    side_filter: str = "both"

    # Block weekends: skip trading on Saturday and Sunday (UTC).
    # Backtest shows weekend WR is 5-10% below weekday across all configs.
    block_weekends: bool = False

    # Circuit breaker: pause trading after N consecutive losses.
    # At 69% WR, 5 in a row = 0.29% chance. Protects bankroll from zeroing.
    # 0 = disabled. Bot logs CIRCUIT BREAKER and stops placing new trades.
    max_consecutive_losses: int = 0


class EdgeMonitor:
    """
    CUSUM-based edge decay detector.

    Accumulates evidence that the true win rate has dropped below target.
    When cumulative evidence exceeds threshold, triggers alarm → reduce bets.

    Math: After each trade, add (target_wr - outcome) to cumulative sum.
    Wins subtract (1 - target_wr) ≈ 0.36, losses add target_wr ≈ 0.64.
    Under target WR, CUSUM stays near 0. If WR drops, CUSUM drifts up.
    Alarm at threshold ≈ 5.0 detects a 4% WR drop within ~40-60 trades.
    """

    def __init__(self, target_wr: float = 0.636, threshold: float = 5.0):
        self.target_wr = target_wr
        self.threshold = threshold
        self.cusum: float = 0.0
        self.alarm: bool = False
        self.trades: int = 0
        self.wins: int = 0

    def record(self, won: bool):
        """Record a trade outcome and update CUSUM."""
        self.trades += 1
        if won:
            self.wins += 1

        # CUSUM: accumulate deviation from target
        outcome = 1.0 if won else 0.0
        self.cusum = max(0.0, self.cusum + (self.target_wr - outcome))

        # Alarm triggers when cumulative evidence exceeds threshold
        self.alarm = self.cusum >= self.threshold

    @property
    def observed_wr(self) -> float:
        return (self.wins / self.trades) if self.trades > 0 else 0.0

    @property
    def should_reduce(self) -> bool:
        """True when evidence suggests edge has decayed."""
        return self.alarm

    def reset(self):
        """Reset after strategy adjustment."""
        self.cusum = 0.0
        self.alarm = False

    def status_str(self) -> str:
        """One-line status for display."""
        wr = f"{self.observed_wr*100:.0f}%" if self.trades > 0 else "n/a"
        state = "ALARM" if self.alarm else "ok"
        return f"CUSUM={self.cusum:.1f}/{self.threshold:.0f} [{state}] WR={wr} ({self.wins}/{self.trades})"


@dataclass
class PendingSignal:
    """A signal waiting for confirmation before execution."""
    coin: str
    side: str
    detected_at: float        # time.time() when first detected
    confirm_at: float         # time.time() when we should re-check
    entry_price: float        # price at detection
    edge: float               # edge at detection
    market_slug: str          # market this signal belongs to


@dataclass
class CoinMarketState:
    """Tracks state for a single coin's market."""
    coin: str
    manager: MarketManager
    strike_price: float = 0.0
    market_end_ts: float = 0.0
    current_slug: str = ""

    # Strike source tracking (vatic, binance, backsolve)
    _strike_source: str = "unknown"

    # Positions held in this market
    up_tokens: float = 0.0
    down_tokens: float = 0.0
    up_cost: float = 0.0
    down_cost: float = 0.0

    # Timing
    last_trade_time: float = 0.0
    market_start_time: float = 0.0
    last_fail_time: Dict[str, float] = field(default_factory=dict)  # side -> timestamp

    # Startup safety: skip the market that was active when bot started.
    # Only trade on markets the bot saw freshly transition to.
    startup_slug: str = ""  # The slug active at boot — never trade on this

    # Last known prices (for settlement)
    last_up_price: float = 0.0
    last_down_price: float = 0.0

    @property
    def has_up_position(self) -> bool:
        return self.up_tokens > 0

    @property
    def has_down_position(self) -> bool:
        return self.down_tokens > 0

    @property
    def total_cost(self) -> float:
        return self.up_cost + self.down_cost

    def seconds_to_expiry(self) -> float:
        if self.market_end_ts <= 0:
            return 600
        return max(0, self.market_end_ts - time.time())

    def seconds_since_start(self) -> float:
        if self.market_start_time <= 0:
            return 999
        return time.time() - self.market_start_time

    def reset_positions(self):
        self.up_tokens = 0.0
        self.down_tokens = 0.0
        self.up_cost = 0.0
        self.down_cost = 0.0
        self.last_fail_time.clear()


@dataclass
class SniperStats:
    """Session statistics."""
    trades: int = 0
    wins: int = 0
    losses: int = 0
    pending: int = 0
    total_wagered: float = 0.0
    total_payout: float = 0.0
    realized_pnl: float = 0.0
    markets_seen: int = 0
    opportunities_found: int = 0
    start_time: float = field(default_factory=time.time)

    @property
    def win_rate(self) -> float:
        decided = self.wins + self.losses
        return (self.wins / decided * 100) if decided > 0 else 0.0

    @property
    def elapsed_minutes(self) -> float:
        return (time.time() - self.start_time) / 60


class MomentumSniperStrategy:
    """
    Momentum sniper: buys mispriced sides of binary crypto markets.

    Scans multiple coins simultaneously, looking for moments when
    Binance price has moved but Polymarket hasn't repriced yet.
    """

    def __init__(self, bot: TradingBot, config: SniperConfig):
        self.bot = bot
        self.config = config

        # Event logger — clean, deduplicated log (no terminal escape codes)
        self._event_logger = logging.getLogger("sniper.events")
        self._event_logger.setLevel(logging.INFO)
        if not self._event_logger.handlers:
            log_path = config.log_file.replace(".csv", ".events.log") if config.log_file else "data/sniper.events.log"
            fh = logging.FileHandler(log_path)
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
            self._event_logger.addHandler(fh)
            self._event_logger.propagate = False

        # Fair value calculator
        self.fv_calc = BinaryFairValue()

        # Real-time price feed (all coins)
        # Chainlink = Polymarket's settlement source (most accurate for outcomes)
        # Binance = faster updates, but diverges from settlement on close markets
        if config.price_source == "chainlink":
            from lib.chainlink_ws import ChainlinkPriceFeed
            self.binance = ChainlinkPriceFeed(coins=config.coins)
        else:
            self.binance = BinancePriceFeed(coins=config.coins)

        # Deribit implied volatility feed (forward-looking, market consensus)
        self._deribit_feed = None
        try:
            from lib.deribit_vol import DeribitVolFeed
            self._deribit_feed = DeribitVolFeed(coins=config.coins)
        except Exception:
            pass

        # Vatic API client for exact Chainlink strikes
        self._vatic = None
        if config.use_vatic:
            try:
                from lib.vatic_client import VaticClient
                self._vatic = VaticClient()
            except Exception:
                pass

        # Per-coin market managers
        self.coin_states: Dict[str, CoinMarketState] = {}

        # Trade logger
        self.trade_logger = TradeLogger(config.log_file)

        # Session stats
        self.stats = SniperStats()

        # USDC balance tracking
        self._balance: float = config.bankroll
        self._last_balance_check: float = 0.0

        # Log buffer for display
        self._log_buffer = LogBuffer(max_size=8)

        # Redemption tracking
        self._last_redeem_time: float = 0.0
        self._redeem_interval: float = 60.0  # Check every 60 seconds

        # Background settlement tracking
        self._last_settle_time: float = 0.0

        # CUSUM edge monitor
        self._edge_monitor = EdgeMonitor(
            target_wr=config.cusum_target_wr,
            threshold=config.cusum_threshold,
        ) if config.enable_cusum else None

        # Pending signals awaiting confirmation
        self._pending_signals: Dict[str, PendingSignal] = {}

        # Circuit breaker: consecutive losing WINDOWS (not individual trades)
        # 3 coins losing in the same 5m window = 1 losing window, not 3 losses
        self._consecutive_loss_windows: int = 0
        self._last_loss_window: str = ""  # slug window timestamp of last counted loss
        self._circuit_breaker_tripped: bool = False

        # Running state
        self.running = False

    def log(self, msg: str, level: str = "info"):
        self._log_buffer.add(msg, level)
        # Write to clean event log (no duplicates from status redraws)
        log_fn = {"error": self._event_logger.error, "warning": self._event_logger.warning,
                   "success": self._event_logger.info}.get(level, self._event_logger.info)
        log_fn(msg)

    def _resolve_orphaned_trades(self):
        """Resolve pending trades from previous sessions via Gamma API."""
        pending = self.trade_logger.get_pending_trades()
        if not pending:
            return

        self.log(f"Found {len(pending)} orphaned pending trades — resolving...", "warning")

        from src.gamma_client import GammaClient
        gamma = GammaClient()
        real_balance = self.bot.get_usdc_balance() or self._balance

        resolved = 0
        for trade_key, record in list(pending.items()):
            try:
                market_data = gamma.get_market_by_slug(record.market_slug)
                if not market_data:
                    continue

                prices = gamma.parse_prices(market_data)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)

                # Resolved markets have one side at ~1.0 and other at ~0.0
                if up_price > 0.9:
                    winning_side = "up"
                elif down_price > 0.9:
                    winning_side = "down"
                else:
                    continue  # Market not yet resolved

                side_won = (record.side == winning_side)
                payout = record.num_tokens * 1.0 if side_won else 0.0

                self.trade_logger.log_outcome(
                    market_slug=record.market_slug,
                    side=record.side,
                    won=side_won,
                    payout=payout,
                    usdc_balance=real_balance,
                )

                outcome = "WON" if side_won else "LOST"
                self.log(f"  Resolved orphan: {record.coin} {record.side.upper()} {record.market_slug} → {outcome}", "info")
                resolved += 1
            except Exception as e:
                self.log(f"  Failed to resolve {trade_key}: {e}", "warning")

        self.log(f"Resolved {resolved}/{len(pending)} orphaned trades", "success")

    def _refresh_balance(self):
        """Query USDC balance (rate-limited to every 10s)."""
        now = time.time()
        if now - self._last_balance_check < 10:
            return
        self._last_balance_check = now
        bal = self.bot.get_usdc_balance()
        if bal is not None:
            self._balance = bal

    def _available_balance(self) -> float:
        """USDC available for new trades.

        After each trade, we deduct cost from self._balance immediately,
        so no need to subtract tied_up (that would double-count).
        On-chain refresh also reflects spent USDC, keeping things in sync.
        """
        return max(0, self._balance)

    async def start(self) -> bool:
        """Start the strategy: Binance feed + market managers for each coin."""
        self.running = True

        # Start Binance price feed
        self.log("Starting Binance price feed...")
        if not await self.binance.start():
            self.log("Failed to start Binance feed", "error")
            return False

        for coin in self.config.coins:
            price = self.binance.get_price(coin)
            self.log(f"Binance {coin}: ${price:,.2f}", "success")

        # Check USDC balance
        self._refresh_balance()
        self.log(f"USDC balance: ${self._balance:.2f}", "success")

        # Resolve orphaned pending trades from previous sessions
        self._resolve_orphaned_trades()

        # Create market managers for each coin
        for coin in self.config.coins:
            manager = MarketManager(
                coin=coin,
                market_check_interval=self.config.market_check_interval,
                auto_switch_market=True,
                timeframe=self.config.timeframe,
            )

            state = CoinMarketState(coin=coin, manager=manager)
            self.coin_states[coin] = state

            # Register callbacks
            self._register_callbacks(coin, manager)

            # Start manager
            if not await manager.start():
                self.log(f"Failed to start {coin} market manager", "error")
                continue

            # Wait for initial data
            await manager.wait_for_data(timeout=10.0)

            # Set initial strike
            self._set_strike(state)

            # Mark this as the startup market — we will NOT trade on it.
            # We might have positions from a previous run on this market.
            # Only trade on markets we see freshly transition to.
            state.startup_slug = state.current_slug
            self.log(f"{coin} market ready: {state.current_slug} (skipping until next cycle)", "success")

        if not any(s.current_slug for s in self.coin_states.values()):
            self.log("No markets found for any coin", "error")
            return False

        return True

    def _register_callbacks(self, coin: str, manager: MarketManager):
        """Register market change callbacks for a coin."""
        @manager.on_market_change
        def on_change(old_slug: str, new_slug: str, _coin=coin):
            self._handle_market_change(_coin, old_slug, new_slug)

    def _set_strike(self, state: CoinMarketState):
        """Determine strike price for the current market.

        Priority chain:
        1. Vatic API — exact Chainlink opening price (settlement source)
        2. Binance spot at window open (first 60s) — close approximation
        3. Back-solve from Polymarket mid price (least accurate)
        """
        market = state.manager.current_market
        if not market:
            return

        slug = market.slug
        state.current_slug = slug
        state.market_start_time = time.time()
        strike_source = "unknown"

        # Parse market start/end time from slug or end_date
        market_start_ts = 0
        duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 900)
        try:
            ts_str = slug.rsplit("-", 1)[-1]
            market_start_ts = int(ts_str)
            state.market_end_ts = market_start_ts + duration
        except (ValueError, IndexError):
            # Non-timestamp slug (1h, daily) — use end_date from market info
            end_ts = market.end_timestamp()
            if end_ts:
                state.market_end_ts = end_ts
                market_start_ts = end_ts - duration
            else:
                state.market_end_ts = 0

        spot = self.binance.get_price(state.coin)
        if spot <= 0:
            return

        secs_since_window_open = time.time() - market_start_ts if market_start_ts > 0 else 999

        # 1. Try Vatic API first — exact Chainlink strike
        if self._vatic:
            vatic_strike = self._vatic.get_strike(
                state.coin, self.config.timeframe, market_start_ts
            )
            if vatic_strike and vatic_strike > 0:
                state.strike_price = vatic_strike
                strike_source = "vatic"
                self.log(f"{state.coin} strike from Vatic (exact Chainlink)")

        # 2. Binance spot early in window
        if strike_source == "unknown" and secs_since_window_open < 60:
            state.strike_price = spot
            strike_source = "binance"
            self.log(f"{state.coin} strike from Binance spot (window {secs_since_window_open:.0f}s old)")

        # 3. Back-solve from Polymarket mid price
        if strike_source == "unknown":
            up_price = state.manager.get_mid_price("up")
            if 0.1 < up_price < 0.9:
                vol, _ = self._get_volatility(state.coin)
                secs_left = state.seconds_to_expiry()
                if secs_left > 30 and vol > 0:
                    from lib.fair_value import SECONDS_PER_YEAR
                    T = secs_left / SECONDS_PER_YEAR
                    sigma_sqrt_t = vol * math.sqrt(T)
                    d = self._approx_inv_normal(up_price)
                    state.strike_price = spot * math.exp(-d * sigma_sqrt_t)
                    strike_source = "backsolve"
                    self.log(f"{state.coin} strike back-solved from mid={up_price:.2f} (window {secs_since_window_open:.0f}s old)")
                else:
                    state.strike_price = spot
                    strike_source = "binance"
            else:
                state.strike_price = spot
                strike_source = "binance"

        # Store strike source for trade logging
        state._strike_source = strike_source

        # Format strike with appropriate precision
        if state.strike_price < 10:
            strike_str = f"${state.strike_price:,.4f}"
        elif state.strike_price < 1000:
            strike_str = f"${state.strike_price:,.2f}"
        else:
            strike_str = f"${state.strike_price:,.0f}"
        self.log(
            f"{state.coin} strike: {strike_str} [{strike_source}] | "
            f"expiry: {state.seconds_to_expiry():.0f}s"
        )

    @staticmethod
    def _approx_inv_normal(p: float) -> float:
        """Approximate inverse normal CDF (Abramowitz & Stegun)."""
        if p <= 0.0 or p >= 1.0:
            return 0.0
        if p < 0.5:
            return -MomentumSniperStrategy._approx_inv_normal(1.0 - p)
        t = math.sqrt(-2.0 * math.log(1.0 - p))
        c0, c1, c2 = 2.515517, 0.802853, 0.010328
        d1, d2, d3 = 1.432788, 0.189269, 0.001308
        return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)

    def _get_volatility(self, coin: str) -> tuple:
        """
        Get best available volatility estimate for a coin.

        Priority:
        0. Fixed volatility override (for backtest-matched operation)
        1. Deribit implied vol (forward-looking, market consensus)
        2. Binance realized vol (backward-looking, from tick data)

        Uses max(deribit_iv, realized_vol) for conservative estimate.
        Returns (vol, source) where source is "FIXED", "IV", or "RV".
        """
        if self.config.fixed_volatility > 0:
            return (self.config.fixed_volatility, "FIXED")

        deribit_vol = None
        if self._deribit_feed:
            deribit_vol = self._deribit_feed.get_implied_vol(coin)

        realized_vol = self.binance.get_volatility(coin)

        if deribit_vol is not None:
            return (max(deribit_vol, realized_vol), "IV")

        return (realized_vol, "RV")

    def _calculate_fair_value(self, state: CoinMarketState) -> Optional[FairValue]:
        """Calculate fair value for a coin's current market."""
        spot = self.binance.get_price(state.coin)
        if spot <= 0 or state.strike_price <= 0:
            return None

        secs = state.seconds_to_expiry()
        vol, _ = self._get_volatility(state.coin)

        return self.fv_calc.calculate(
            spot=spot,
            strike=state.strike_price,
            seconds_to_expiry=secs,
            volatility=vol,
        )

    def _wilson_lower(self, wins: int, total: int, z: float = 1.28) -> float:
        """Wilson score lower bound (80% confidence by default)."""
        if total == 0:
            return 0.0
        p_hat = wins / total
        denom = 1 + z * z / total
        centre = p_hat + z * z / (2 * total)
        spread = z * math.sqrt((p_hat * (1 - p_hat) + z * z / (4 * total)) / total)
        return (centre - spread) / denom

    def _adaptive_kelly_fraction(self, entry_price: float, strong: bool = False) -> float:
        """
        Scale Kelly fraction based on Wilson lower bound of observed WR.

        - <10 trades: 25% Kelly (conservative, minimal data)
        - 10+ trades: scale linearly between 25% and target Kelly
          based on how far Wilson floor is above breakeven
        - CUSUM alarm: clamp to 25% Kelly regardless
        """
        # Base Kelly (what we'd use without adaptation)
        price_kelly = self.config.kelly_strong if entry_price >= 0.60 else self.config.kelly_fraction
        edge_kelly = self.config.kelly_strong if strong else self.config.kelly_fraction
        base_fraction = max(price_kelly, edge_kelly)

        # If CUSUM alarm is active, reduce to 25% Kelly
        if self._edge_monitor and self._edge_monitor.should_reduce:
            return 0.25

        # Need at least 10 trades for any confidence
        decided = self.stats.wins + self.stats.losses
        if decided < 10:
            return 0.25

        # Wilson 80% lower bound
        wr_floor = self._wilson_lower(self.stats.wins, decided, z=1.28)

        # Breakeven WR ≈ average entry price (for binary payoffs)
        # Use target WR from config as reference
        target_wr = self.config.cusum_target_wr if self.config.enable_cusum else 0.636

        if wr_floor <= target_wr - 0.10:
            # WR floor is well below target — minimal Kelly
            return 0.25
        elif wr_floor >= target_wr:
            # WR floor at or above target — full Kelly
            return base_fraction
        else:
            # Linear interpolation between 25% and base
            progress = (wr_floor - (target_wr - 0.10)) / 0.10
            return 0.25 + progress * (base_fraction - 0.25)

    def _kelly_bet_usdc(self, fair_prob: float, entry_price: float, strong: bool = False) -> float:
        """
        Calculate Kelly-optimal bet size.

        Binary payoff: pay entry_price, receive $1 on win, $0 on loss.
        Kelly: f = (p*b - q) / b where b = 1/price - 1
        """
        if entry_price <= 0.01 or entry_price >= 0.99 or fair_prob <= 0:
            return 0.0

        p = fair_prob
        q = 1.0 - p
        b = (1.0 / entry_price) - 1.0

        if b <= 0:
            return 0.0

        kelly_f = (p * b - q) / b
        if kelly_f <= 0:
            return 0.0

        # Adaptive Kelly: scale fraction based on observed performance
        if self.config.adaptive_kelly:
            fraction = self._adaptive_kelly_fraction(entry_price, strong)
        else:
            # Two-factor Kelly: entry price confidence + edge strength
            price_kelly = self.config.kelly_strong if entry_price >= 0.60 else self.config.kelly_fraction
            edge_kelly = self.config.kelly_strong if strong else self.config.kelly_fraction
            fraction = max(price_kelly, edge_kelly)
        bet_fraction = kelly_f * fraction

        # Hard cap: never risk more than max_bet_fraction of bankroll on one trade.
        # Kelly can compute huge fractions near expiry (fair_prob ~0.99 → 65%+ of
        # bankroll). This cap prevents any single trade from blowing up the account.
        if self.config.max_bet_fraction > 0:
            bet_fraction = min(bet_fraction, self.config.max_bet_fraction)

        available = self._available_balance()
        if available < self.config.min_bet_usdc:
            return 0.0

        usdc = bet_fraction * available
        usdc = max(self.config.min_bet_usdc, min(self.config.max_bet_usdc, usdc))
        usdc = min(usdc, available)

        return usdc

    def _find_opportunities(self) -> List[Tuple[CoinMarketState, str, float, float, FairValue]]:
        """
        Scan all coins for trading opportunities.

        No artificial limits — if there's edge, it shows up here.
        Kelly handles position sizing. Volume is how we make money.

        Returns list of (state, side, entry_price, edge, fair_value)
        sorted by edge descending (best opportunity first).
        """
        opportunities = []

        # Circuit breaker: stop trading after N consecutive losses
        if self._circuit_breaker_tripped:
            return []

        # Hour blocking: skip trading entirely during blocked UTC hours
        if self.config.blocked_hours:
            import datetime as _dt
            current_hour = _dt.datetime.utcnow().hour
            if current_hour in self.config.blocked_hours:
                return []

        # Weekend blocking: skip Saturday (5) and Sunday (6) UTC
        if self.config.block_weekends:
            import datetime as _dt
            if _dt.datetime.utcnow().weekday() >= 5:
                return []

        for coin, state in self.coin_states.items():
            if not state.current_slug:
                continue

            # Never trade on the market that was active at startup.
            # We might have unknown positions from a previous run.
            # Also skip if startup_slug was never set (coin discovered late).
            if not state.startup_slug or state.current_slug == state.startup_slug:
                continue

            # Only skip if market is literally expired (0 seconds left)
            if state.seconds_to_expiry() <= 0:
                continue

            # Late entry filter: wait for trend to establish before entering.
            if self.config.min_window_elapsed > 0 and state.market_start_time > 0:
                elapsed = time.time() - state.market_start_time
                if elapsed < self.config.min_window_elapsed:
                    continue

            # Max entry filter: stop entering after this many seconds.
            # Prevents late entries where latency arbitrage has disappeared.
            if self.config.max_window_elapsed > 0 and state.market_start_time > 0:
                elapsed = time.time() - state.market_start_time
                if elapsed > self.config.max_window_elapsed:
                    continue

            # Volatility filter: only trade in low-vol regimes.
            # Data: vol < 0.50 -> 35.8% WR vs 22.4% when vol > 0.50.
            if self.config.max_volatility > 0:
                current_vol, _ = self._get_volatility(state.coin)
                if current_vol > self.config.max_volatility:
                    continue

            # Calculate fair value
            fv = self._calculate_fair_value(state)
            if not fv:
                continue

            # Check both sides — one entry per side per market.
            # Buying BTC UP 5 times at $0.57 in the same market is the
            # same trade repeated, not 5 different opportunities.
            # Determine which sides to check based on side_filter
            sides_to_check = ["up", "down"]
            if self.config.side_filter == "up":
                sides_to_check = ["up"]
            elif self.config.side_filter == "down":
                sides_to_check = ["down"]

            for side in sides_to_check:
                if side == "up" and state.has_up_position:
                    continue
                if side == "down" and state.has_down_position:
                    continue

                # Skip if recently failed (60s cooldown to prevent retry spam)
                fail_time = state.last_fail_time.get(side, 0)
                if fail_time > 0 and (time.time() - fail_time) < 60:
                    continue

                fair_prob = fv.fair_up if side == "up" else fv.fair_down

                # Get best ask (cheapest price we can buy at)
                best_ask = state.manager.get_best_ask(side)
                if best_ask <= 0 or best_ask >= 1.0:
                    continue

                # Structural price filters only (not risk limits)
                if best_ask > self.config.max_entry_price:
                    continue
                if best_ask < self.config.min_entry_price:
                    continue

                # Never buy the opposite side of an existing position.
                # Kelly sizes each side independently, so token counts differ
                # and buying both sides is NOT a guaranteed arb — it's a coin
                # flip on which side has more tokens. Just pick a direction.
                if side == "up" and state.has_down_position:
                    continue
                if side == "down" and state.has_up_position:
                    continue

                # Calculate edge at exact ask price (FOK handles stale prices)
                buy_price = round(best_ask, 2)

                # Net edge = fair value - buy price - taker fee
                fee = taker_fee_per_token(buy_price, self.config.timeframe)
                edge = fair_prob - buy_price - fee

                if edge >= self.config.min_edge:
                    # Fair value confidence filter: skip coin-flip trades
                    if fair_prob < self.config.min_fair_value:
                        continue  # Model not confident enough

                    # Momentum filter: price must have displaced from strike
                    # in the direction we're betting. This matches the backtest
                    # logic exactly: mom = (spot - strike) / strike.
                    # DO NOT use binance.get_momentum() — that measures 30s
                    # price change, which is a different (weaker) signal.
                    if self.config.min_momentum > 0 and state.strike_price > 0:
                        spot = self.binance.get_price(state.coin)
                        displacement = (spot - state.strike_price) / state.strike_price
                        if side == "up" and displacement < self.config.min_momentum:
                            continue  # Price below strike — don't buy Up
                        if side == "down" and displacement > -self.config.min_momentum:
                            continue  # Price above strike — don't buy Down

                    opportunities.append((state, side, best_ask, edge, fv))

        # Sort by edge, best first
        opportunities.sort(key=lambda x: x[3], reverse=True)
        return opportunities

    async def _execute_snipe(
        self,
        state: CoinMarketState,
        side: str,
        entry_price: float,
        edge: float,
        fv: FairValue,
        signal_time: float = 0.0,
    ) -> bool:
        """Execute a snipe trade."""
        if self.config.observe_only:
            # Paper trading with realistic simulation
            fair_prob = fv.fair_up if side == "up" else fv.fair_down
            num_tokens = 5.0
            buy_price = round(entry_price, 2)

            # FOK rejection simulation: check orderbook depth
            ob = state.manager.get_orderbook(side)
            if ob and ob.asks:
                best_ask_size = ob.asks[0].size
                if best_ask_size < num_tokens:
                    self.log(
                        f"[PAPER SKIP] {state.coin} {side.upper()} @ {buy_price:.2f} "
                        f"FOK killed (depth={best_ask_size:.0f} < {num_tokens:.0f})",
                        "warning"
                    )
                    return False

            # Fee-inclusive cost basis
            fee_per_token = taker_fee_per_token(buy_price, self.config.timeframe)
            actual_cost = (buy_price + fee_per_token) * num_tokens

            if side == "up":
                state.up_tokens += num_tokens
                state.up_cost += actual_cost
            else:
                state.down_tokens += num_tokens
                state.down_cost += actual_cost

            state.last_trade_time = time.time()
            self.stats.trades += 1
            self.stats.pending += 1
            self.stats.total_wagered += actual_cost
            self.stats.opportunities_found += 1

            spot = self.binance.get_price(state.coin)
            vol, vol_src = self._get_volatility(state.coin)
            other_price = state.manager.get_mid_price("down" if side == "up" else "up")
            strike_src = getattr(state, '_strike_source', 'unknown')

            self.trade_logger.log_trade(
                market_slug=state.current_slug,
                coin=state.coin,
                timeframe=self.config.timeframe,
                side=side,
                entry_price=buy_price,
                bet_size_usdc=actual_cost,
                num_tokens=num_tokens,
                bankroll=self._balance,
                usdc_balance=self._balance,
                btc_price=spot,
                other_side_price=other_price,
                volatility_std=vol,
                fair_value_at_entry=fair_prob,
                time_to_expiry_at_entry=state.seconds_to_expiry(),
                momentum_at_entry=(spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0,
                volatility_at_entry=vol,
                signal_to_order_ms=0,
                order_latency_ms=0,
                total_latency_ms=0,
                vol_source=vol_src,
                strike_source=strike_src,
            )

            self.log(
                f"[PAPER] {state.coin} {side.upper()} @ {buy_price:.2f} "
                f"x{num_tokens:.0f} (${actual_cost:.2f} inc fees) edge={edge:.2f} "
                f"FV={fair_prob:.2f} strike={strike_src}",
                "trade"
            )
            return True

        # Calculate bet size
        fair_prob = fv.fair_up if side == "up" else fv.fair_down
        is_strong = edge >= self.config.strong_edge

        # Per-coin sizing: kelly_coins use Kelly even when min_size_mode is on
        use_kelly = (not self.config.min_size_mode) or (state.coin in self.config.kelly_coins)

        if use_kelly:
            bet_usdc = self._kelly_bet_usdc(fair_prob, entry_price, strong=is_strong)
            if bet_usdc < self.config.min_bet_usdc:
                return False
            num_tokens = int(bet_usdc / entry_price)  # Whole tokens: price(2dp) × int = max 2dp USDC
        else:
            # Conservative: always bet exactly 5 tokens (Polymarket minimum).
            # Cheapest way to get data on whether the edge is real.
            num_tokens = 5.0
            bet_usdc = num_tokens * entry_price
            # Polymarket rejects orders below $1.00 USDC
            if bet_usdc < 1.0:
                return False
            if bet_usdc > self._available_balance():
                return False

        if num_tokens < 5.0:
            # Not enough for Polymarket minimum order size
            return False

        # Get token ID
        token_id = state.manager.token_ids.get(side)
        if not token_id:
            return False

        # Place FOK (Fill Or Kill) order — fills immediately or is cancelled.
        # Enter at exact ask price; FOK handles stale prices naturally.
        buy_price = round(entry_price, 2)

        order_start = time.time()
        signal_to_order_ms = (order_start - signal_time) * 1000 if signal_time else 0

        result = await self.bot.place_order(
            token_id=token_id,
            price=buy_price,
            size=num_tokens,
            side="BUY",
            order_type="FOK",
        )

        # FOK fallback: if Kelly-sized order fails on thin orderbook,
        # retry with minimum 5 tokens (paper-validated fill size).
        if not result.success and num_tokens > 5:
            self.log(
                f"FOK failed at {num_tokens:.0f} tokens, retrying min-size (5)",
                "warning"
            )
            num_tokens = 5.0
            result = await self.bot.place_order(
                token_id=token_id,
                price=buy_price,
                size=num_tokens,
                side="BUY",
                order_type="FOK",
            )

        order_end = time.time()
        order_latency_ms = (order_end - order_start) * 1000
        total_latency_ms = (order_end - signal_time) * 1000 if signal_time else 0

        # Only track position if order was accepted AND filled
        order_status = (result.status or "").upper()
        if result.success and order_status not in ("LIVE", "OPEN"):
            actual_cost = buy_price * num_tokens

            # Immediately deduct from balance so next Kelly calculation
            # sees the correct available capital (no stale balance)
            self._balance -= actual_cost

            # Record position
            if side == "up":
                state.up_tokens += num_tokens
                state.up_cost += actual_cost
            else:
                state.down_tokens += num_tokens
                state.down_cost += actual_cost

            state.last_trade_time = time.time()

            # Update stats
            self.stats.trades += 1
            self.stats.pending += 1
            self.stats.total_wagered += actual_cost
            self.stats.opportunities_found += 1

            # Log trade
            spot = self.binance.get_price(state.coin)
            vol, vol_src = self._get_volatility(state.coin)
            other_price = state.manager.get_mid_price("down" if side == "up" else "up")

            # Get real USDC balance for accurate logging
            real_balance = self.bot.get_usdc_balance() or self._balance

            strike_src = getattr(state, '_strike_source', 'unknown')
            self.trade_logger.log_trade(
                market_slug=state.current_slug,
                coin=state.coin,
                timeframe=self.config.timeframe,
                side=side,
                entry_price=buy_price,
                bet_size_usdc=actual_cost,
                num_tokens=num_tokens,
                bankroll=self._balance,
                usdc_balance=real_balance,
                btc_price=spot,
                other_side_price=other_price,
                volatility_std=vol,
                fair_value_at_entry=fair_prob,
                time_to_expiry_at_entry=state.seconds_to_expiry(),
                momentum_at_entry=(spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0,
                volatility_at_entry=vol,
                signal_to_order_ms=signal_to_order_ms,
                order_latency_ms=order_latency_ms,
                total_latency_ms=total_latency_ms,
                vol_source=vol_src,
                strike_source=strike_src,
            )

            kelly_pct = (actual_cost / (self._available_balance() + actual_cost) * 100) if (self._available_balance() + actual_cost) > 0 else 0
            strength = "STRONG" if is_strong else "NORMAL"

            self.log(
                f"SNIPE {state.coin} {side.upper()} @ {buy_price:.2f} "
                f"x{num_tokens:.0f} (${actual_cost:.2f}) "
                f"edge={edge:.2f} [{strength}] kelly={kelly_pct:.0f}% "
                f"strike={strike_src} lat={total_latency_ms:.0f}ms",
                "success"
            )
            return True
        elif result.success and order_status in ("LIVE", "OPEN"):
            # Order was accepted but NOT filled (sat on book) — do NOT track position
            self.log(
                f"Order NOT FILLED ({state.coin} {side} @ {buy_price:.2f}) "
                f"status={result.status} — skipping phantom position",
                "warning"
            )
            state.last_fail_time[side] = time.time()
            return False
        else:
            self.log(f"Order failed ({state.coin} {side}): {result.message}", "error")
            # Cooldown: don't retry this coin/side for 60 seconds
            state.last_fail_time[side] = time.time()
            return False

    def _determine_winner(self, state: CoinMarketState, old_slug: str) -> Optional[str]:
        """
        Try to determine the winning side of a settled market (non-blocking).

        Uses ONLY the Gamma API (authoritative source). Makes a SINGLE attempt.
        If not yet resolved, returns None — the periodic settler will retry later.
        """
        if not old_slug:
            return None

        try:
            market_data = state.manager.gamma.get_market_by_slug(old_slug)
            if market_data:
                prices = state.manager.gamma.parse_prices(market_data)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)

                if up_price > 0.9:
                    self.log(f"{state.coin} Gamma API: UP won (up={up_price:.2f})")
                    return "up"
                elif down_price > 0.9:
                    self.log(f"{state.coin} Gamma API: DOWN won (down={down_price:.2f})")
                    return "down"
        except Exception as e:
            self.log(f"{state.coin} Gamma API check failed: {e}", "warning")

        self.log(f"{state.coin} {old_slug} not yet resolved — will retry periodically")
        return None

    def _periodic_settle(self):
        """
        Periodically resolve pending trades via Gamma API.

        The Gamma API takes 2-5 minutes to update outcomePrices after a 5m market
        closes. Instead of blocking at market change, we check all pending trades
        every 30 seconds until they resolve.
        """
        now = time.time()
        if now - self._last_settle_time < 30:
            return
        self._last_settle_time = now

        pending = self.trade_logger.get_pending_trades()
        if not pending:
            return

        from src.gamma_client import GammaClient
        gamma = GammaClient()
        real_balance = self.bot.get_usdc_balance() or self._balance

        for trade_key, record in list(pending.items()):
            try:
                market_data = gamma.get_market_by_slug(record.market_slug)
                if not market_data:
                    continue

                prices = gamma.parse_prices(market_data)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)

                if up_price > 0.9:
                    winning_side = "up"
                elif down_price > 0.9:
                    winning_side = "down"
                else:
                    continue  # Not resolved yet

                side_won = (record.side == winning_side)
                payout = record.num_tokens * 1.0 if side_won else 0.0

                self.trade_logger.log_outcome(
                    market_slug=record.market_slug,
                    side=record.side,
                    won=side_won,
                    payout=payout,
                    usdc_balance=real_balance,
                )

                # Update session stats
                if side_won:
                    self.stats.wins += 1
                    self.stats.total_payout += payout
                    self._consecutive_loss_windows = 0
                    self._last_loss_window = ""
                else:
                    self.stats.losses += 1
                    # Count per-window: same window = 1 event
                    window_id = record.market_slug.rsplit("-", 1)[-1]
                    if window_id != self._last_loss_window:
                        self._consecutive_loss_windows += 1
                        self._last_loss_window = window_id
                    if (self.config.max_consecutive_losses > 0 and
                            self._consecutive_loss_windows >= self.config.max_consecutive_losses):
                        self._circuit_breaker_tripped = True
                        self.log(
                            f"CIRCUIT BREAKER: {self._consecutive_loss_windows} consecutive losing windows! "
                            f"Trading paused. Investigate before resuming.",
                            level="error",
                        )
                self.stats.pending -= 1
                self.stats.realized_pnl += (payout - record.bet_size_usdc)

                # Record in CUSUM monitor
                if self._edge_monitor:
                    self._edge_monitor.record(side_won)

                outcome = "WON" if side_won else "LOST"
                self.log(
                    f"SETTLED {record.coin} {record.side.upper()} {record.market_slug}: "
                    f"{outcome} ${payout - record.bet_size_usdc:+.2f} (Gamma API verified)",
                    "success" if side_won else "warning"
                )
            except Exception as e:
                self.log(f"Settle check failed for {trade_key}: {e}", "warning")

    def _handle_market_change(self, coin: str, old_slug: str, new_slug: str):
        """Handle market settlement and transition."""
        state = self.coin_states.get(coin)
        if not state:
            return

        # Try immediate settlement (non-blocking, single attempt)
        if state.up_tokens > 0 or state.down_tokens > 0:
            winning_side = self._determine_winner(state, old_slug)
            if winning_side:
                # Immediate resolution succeeded
                real_balance = self.bot.get_usdc_balance() or self._balance
                for side_name, record in self.trade_logger.get_pending_for_market(old_slug):
                    side_won = (side_name == winning_side)
                    side_payout = record.num_tokens * 1.0 if side_won else 0.0
                    self.trade_logger.log_outcome(
                        market_slug=old_slug,
                        side=side_name,
                        won=side_won,
                        payout=side_payout,
                        usdc_balance=real_balance,
                    )
                    pnl = side_payout - record.bet_size_usdc
                    self.stats.realized_pnl += pnl
                    self.stats.pending -= 1
                    if side_won:
                        self.stats.wins += 1
                        self.stats.total_payout += side_payout
                        self._consecutive_loss_windows = 0
                        self._last_loss_window = ""
                    else:
                        self.stats.losses += 1
                        window_id = old_slug.rsplit("-", 1)[-1]
                        if window_id != self._last_loss_window:
                            self._consecutive_loss_windows += 1
                            self._last_loss_window = window_id
                        if (self.config.max_consecutive_losses > 0 and
                                self._consecutive_loss_windows >= self.config.max_consecutive_losses):
                            self._circuit_breaker_tripped = True
                            self.log(
                                f"CIRCUIT BREAKER: {self._consecutive_loss_windows} consecutive losing windows! "
                                f"Trading paused. Investigate before resuming.",
                                level="error",
                            )
                    # Record in CUSUM monitor
                    if self._edge_monitor:
                        self._edge_monitor.record(side_won)

                    outcome = "WON" if side_won else "LOST"
                    cusum_str = f" | {self._edge_monitor.status_str()}" if self._edge_monitor else ""
                    self.log(
                        f"SETTLED {coin} {side_name.upper()} {old_slug}: {outcome} "
                        f"${pnl:+.2f} | Session: ${self.stats.realized_pnl:+.2f}{cusum_str}",
                        "success" if side_won else "warning"
                    )
            # If not resolved, _periodic_settle will handle it later

        # Refresh balance
        self._last_balance_check = 0
        self._refresh_balance()

        # Reset for new market
        state.reset_positions()
        state.last_up_price = 0.0
        state.last_down_price = 0.0
        self.stats.markets_seen += 1

        # Clear pending signals for this coin (market changed, signals invalid)
        stale_keys = [k for k in self._pending_signals if k.startswith(f"{coin}:")]
        for k in stale_keys:
            self._pending_signals.pop(k, None)

        # If startup_slug was never set (coin wasn't discovered at boot),
        # treat this first-discovered market as the startup market — skip it.
        if not state.startup_slug:
            state.startup_slug = new_slug
            self.log(f"{coin} first discovery: {new_slug} (skipping — treat as startup)", "warning")

        # Set new strike (delay to let data arrive)
        state.current_slug = new_slug
        state.market_start_time = time.time()

        try:
            loop = asyncio.get_running_loop()
            loop.call_later(3.0, lambda: self._set_strike(state))
        except RuntimeError:
            self._set_strike(state)

    def _periodic_redeem(self):
        """Periodically attempt to redeem settled winning positions.

        The UMA oracle takes minutes to resolve markets after they expire.
        Calling redeem_all() only on market change is too early — the market
        isn't resolved yet. This periodic check catches them once the oracle
        has reported payouts.
        """
        now = time.time()
        if now - self._last_redeem_time < self._redeem_interval:
            return
        self._last_redeem_time = now

        if self.config.observe_only:
            return

        try:
            results = self.bot.redeem_all()
            if results:
                self.log(f"Redeemed {len(results)} position(s) to USDC", "success")
                # Refresh balance after successful redemption
                self._last_balance_check = 0
                self._refresh_balance()
        except Exception as e:
            self.log(f"Periodic redeem failed: {e}", "warning")

    async def _process_pending_signals(self):
        """Check pending signals that have reached their confirmation time."""
        if not self._pending_signals:
            return

        now = time.time()
        expired_keys = []

        for key, sig in list(self._pending_signals.items()):
            # Not ready yet
            if now < sig.confirm_at:
                continue

            expired_keys.append(key)

            # Re-check: does edge still exist on the same side?
            state = self.coin_states.get(sig.coin)
            if not state or state.current_slug != sig.market_slug:
                self.log(f"EXPIRED: {sig.coin} {sig.side.upper()} — market changed", "warning")
                continue

            fv = self._calculate_fair_value(state)
            if not fv:
                self.log(f"EXPIRED: {sig.coin} {sig.side.upper()} — no fair value", "warning")
                continue

            fair_prob = fv.fair_up if sig.side == "up" else fv.fair_down
            best_ask = state.manager.get_best_ask(sig.side)
            if best_ask <= 0 or best_ask >= 1.0:
                self.log(f"EXPIRED: {sig.coin} {sig.side.upper()} — no ask", "warning")
                continue

            buy_price = round(best_ask, 2)
            fee = taker_fee_per_token(buy_price, self.config.timeframe)
            edge = fair_prob - buy_price - fee

            if edge >= self.config.min_edge:
                # Edge confirmed — execute
                self.log(
                    f"CONFIRMED: {sig.coin} {sig.side.upper()} @ {buy_price:.2f} "
                    f"edge={edge:.2f} (was {sig.edge:.2f} at detection)",
                    "success"
                )
                await self._execute_snipe(state, sig.side, buy_price, edge, fv, signal_time=sig.detected_at)
            else:
                self.log(
                    f"EXPIRED: {sig.coin} {sig.side.upper()} — edge gone "
                    f"({edge:+.2f} < {self.config.min_edge:.2f})",
                    "warning"
                )

        for key in expired_keys:
            self._pending_signals.pop(key, None)

    async def _tick(self):
        """Main strategy tick — scan for opportunities and execute."""
        # Periodic settlement of pending trades via Gamma API
        self._periodic_settle()

        # Periodic redemption of settled winning positions
        self._periodic_redeem()

        # Process pending confirmation signals
        if self.config.confirm_gap > 0:
            await self._process_pending_signals()

        # Update last known prices for all coins
        for coin, state in self.coin_states.items():
            up = state.manager.get_mid_price("up")
            down = state.manager.get_mid_price("down")
            if up > 0:
                state.last_up_price = up
            if down > 0:
                state.last_down_price = down

        # Balance check
        self._refresh_balance()
        if self._balance < self.config.min_bet_usdc and not self.config.observe_only:
            return

        # Find opportunities across all coins
        opportunities = self._find_opportunities()
        signal_time = time.time()

        # Execute ALL opportunities — no artificial limit on trades per tick.
        # Kelly sizes each bet against available balance, so later bets
        # naturally get smaller as capital is allocated. Let volume work.
        for state, side, price, edge, fv in opportunities:
            if not self.config.observe_only and self._available_balance() < self.config.min_bet_usdc:
                break  # Out of capital — Kelly's job is done

            # Signal confirmation: queue instead of immediate execution
            if self.config.confirm_gap > 0:
                sig_key = f"{state.coin}:{side}:{state.current_slug}"
                if sig_key not in self._pending_signals:
                    self._pending_signals[sig_key] = PendingSignal(
                        coin=state.coin,
                        side=side,
                        detected_at=signal_time,
                        confirm_at=signal_time + self.config.confirm_gap,
                        entry_price=price,
                        edge=edge,
                        market_slug=state.current_slug,
                    )
                    self.log(
                        f"PENDING: {state.coin} {side.upper()} @ {price:.2f} "
                        f"edge={edge:.2f}, confirming in {self.config.confirm_gap:.0f}s...",
                        "info"
                    )
            else:
                await self._execute_snipe(state, side, price, edge, fv, signal_time=signal_time)

    def _render_status(self):
        """Render the live status display."""
        lines = []
        lines.append("")
        lines.append(f"{'=' * 70}")

        mode = "OBSERVE" if self.config.observe_only else "LIVE"
        coins_str = "/".join(self.config.coins)
        lines.append(f"  MOMENTUM SNIPER [{mode}] — {coins_str} {self.config.timeframe}")
        lines.append(f"{'=' * 70}")

        # Per-coin status
        for coin, state in self.coin_states.items():
            if not state.current_slug:
                lines.append(f"  {coin}: (no market)")
                continue

            spot = self.binance.get_price(coin)
            vol, vol_src = self._get_volatility(coin)
            secs = state.seconds_to_expiry()
            mins, s = divmod(int(secs), 60)

            fv = self._calculate_fair_value(state)

            up_mid = state.manager.get_mid_price("up")
            down_mid = state.manager.get_mid_price("down")
            up_ask = state.manager.get_best_ask("up")
            down_ask = state.manager.get_best_ask("down")

            waiting = " [WAITING]" if (not state.startup_slug or state.current_slug == state.startup_slug) else ""
            if state.strike_price < 10:
                strike_fmt = f"${state.strike_price:>,.4f}"
            elif state.strike_price < 1000:
                strike_fmt = f"${state.strike_price:>,.2f}"
            else:
                strike_fmt = f"${state.strike_price:>,.0f}"
            lines.append(f"  {coin}  ${spot:>10,.2f}  vol={vol*100:.0f}%({vol_src})  "
                         f"strike={strike_fmt}  "
                         f"expiry={mins}m{s:02d}s{waiting}")

            if fv:
                up_fee = taker_fee_per_token(up_ask, self.config.timeframe) if up_ask > 0 else 0
                down_fee = taker_fee_per_token(down_ask, self.config.timeframe) if down_ask > 0 else 0
                up_edge = fv.fair_up - up_ask - up_fee if up_ask > 0 else 0
                down_edge = fv.fair_down - down_ask - down_fee if down_ask > 0 else 0

                # Show edges with color indicators
                up_signal = "*" if up_edge >= self.config.min_edge else " "
                down_signal = "*" if down_edge >= self.config.min_edge else " "

                lines.append(
                    f"       Fair: Up={fv.fair_up:.2f} Down={fv.fair_down:.2f}  |  "
                    f"Ask: Up={up_ask:.2f} Down={down_ask:.2f}"
                )
                lines.append(
                    f"       Edge:{up_signal}Up={up_edge:+.2f} {down_signal}Down={down_edge:+.2f}  |  "
                    f"Mid: Up={up_mid:.2f} Down={down_mid:.2f}"
                )

            # Show positions
            pos_parts = []
            if state.has_up_position:
                pos_parts.append(f"UP x{state.up_tokens:.0f} (${state.up_cost:.2f})")
            if state.has_down_position:
                pos_parts.append(f"DOWN x{state.down_tokens:.0f} (${state.down_cost:.2f})")
            if pos_parts:
                lines.append(f"       Pos: {' | '.join(pos_parts)}")

            lines.append("")

        # Session stats
        lines.append(f"{'─' * 70}")
        lines.append(
            f"  Balance: ${self._balance:.2f}  |  "
            f"Available: ${self._available_balance():.2f}  |  "
            f"PnL: ${self.stats.realized_pnl:+.2f}"
        )
        lines.append(
            f"  Trades: {self.stats.trades}  |  "
            f"W/L: {self.stats.wins}/{self.stats.losses}  |  "
            f"Win Rate: {self.stats.win_rate:.0f}%  |  "
            f"Wagered: ${self.stats.total_wagered:.2f}"
        )
        lines.append(
            f"  Markets: {self.stats.markets_seen}  |  "
            f"Opportunities: {self.stats.opportunities_found}  |  "
            f"Runtime: {self.stats.elapsed_minutes:.0f}m"
        )

        # Edge settings
        if self.config.kelly_coins:
            kelly_str = "/".join(self.config.kelly_coins)
            sizing = f"Kelly: {kelly_str} | MIN-SIZE: others"
        elif self.config.min_size_mode:
            sizing = "MIN-SIZE (5 tok)"
        else:
            sizing = f"kelly={self.config.kelly_fraction:.0%}/{self.config.kelly_strong:.0%}"
        # Build filters string
        filters = f"edge>={self.config.min_edge:.0%}"
        if self.config.fixed_volatility > 0:
            filters += f" | vol=FIXED {self.config.fixed_volatility:.0%}"
        elif self.config.max_volatility > 0:
            filters += f" | vol<{self.config.max_volatility:.2f}"
        if self.config.min_momentum > 0:
            filters += f" | mom>{self.config.min_momentum:.2%}"
        if self.config.min_fair_value > 0.50:
            filters += f" | FV>={self.config.min_fair_value:.2f}"
        if self.config.min_window_elapsed > 0 or self.config.max_window_elapsed > 0:
            lo = f"{self.config.min_window_elapsed:.0f}" if self.config.min_window_elapsed > 0 else "0"
            hi = f"{self.config.max_window_elapsed:.0f}" if self.config.max_window_elapsed > 0 else "end"
            filters += f" | entry=[{lo}-{hi}]s"
        fee_rate = TAKER_FEE_RATES.get(self.config.timeframe, 0.0)
        fee_str = "none" if fee_rate == 0 else f"{fee_rate*25:.2f}%@50c"
        lines.append(
            f"  Settings: {filters} | "
            f"{sizing} | "
            f"price=[{self.config.min_entry_price:.2f}-{self.config.max_entry_price:.2f}] | "
            f"fee={fee_str}"
        )

        # Edge Amplifier status line
        amp_parts = []
        if self._edge_monitor:
            amp_parts.append(self._edge_monitor.status_str())
        if self.config.adaptive_kelly:
            decided = self.stats.wins + self.stats.losses
            if decided >= 10:
                wr_floor = self._wilson_lower(self.stats.wins, decided, z=1.28)
                frac = self._adaptive_kelly_fraction(0.50)  # representative price
                amp_parts.append(f"AdKelly={frac:.0%} (WR_floor={wr_floor:.1%})")
            else:
                amp_parts.append(f"AdKelly=25% ({decided}/10 trades)")
        if self.config.confirm_gap > 0:
            n_pending = len(self._pending_signals)
            amp_parts.append(f"Confirm={self.config.confirm_gap:.0f}s ({n_pending} pending)")
        if amp_parts:
            lines.append(f"  Edge Amp: {' | '.join(amp_parts)}")
        if self._circuit_breaker_tripped:
            lines.append(f"  {Colors.RED}CIRCUIT BREAKER TRIPPED: {self._consecutive_loss_windows} consecutive losing windows — TRADING PAUSED{Colors.RESET}")
        elif self.config.max_consecutive_losses > 0:
            lines.append(f"  Circuit Breaker: {self._consecutive_loss_windows}/{self.config.max_consecutive_losses} losing windows")

        lines.append(f"{'─' * 70}")

        # Recent log messages
        for msg in self._log_buffer.get_messages()[-5:]:
            lines.append(f"  {msg}")

        output = "\n".join(lines)
        print(f"\033[H\033[J{output}", flush=True)

    async def run(self):
        """Main strategy loop."""
        try:
            if not await self.start():
                self.log("Failed to start strategy", "error")
                return

            self.log("Sniper active — scanning for opportunities...", "success")

            while self.running:
                await self._tick()
                self._render_status()
                await asyncio.sleep(0.5)

        except KeyboardInterrupt:
            self.log("Stopped by user")
        finally:
            await self.stop()
            self._print_summary()

    async def stop(self):
        """Stop the strategy."""
        self.running = False

        # Stop all market managers
        for state in self.coin_states.values():
            await state.manager.stop()

        # Stop Binance feed
        await self.binance.stop()

    def _print_summary(self):
        """Print session summary."""
        print()
        print(f"{'=' * 60}")
        print(f"  SNIPER SESSION SUMMARY")
        print(f"{'=' * 60}")
        print(f"  Duration:         {self.stats.elapsed_minutes:.0f} minutes")
        print(f"  Markets seen:     {self.stats.markets_seen}")
        print(f"  Opportunities:    {self.stats.opportunities_found}")
        print(f"  Trades executed:  {self.stats.trades}")
        print(f"  Win/Loss:         {self.stats.wins}/{self.stats.losses}")
        print(f"  Win rate:         {self.stats.win_rate:.1f}%")
        print(f"  Total wagered:    ${self.stats.total_wagered:.2f}")
        print(f"  Realized PnL:     ${self.stats.realized_pnl:+.2f}")
        print(f"  Final balance:    ${self._balance:.2f}")
        print()
        print(f"  Trade log:        {self.config.log_file}")
        print(f"{'=' * 60}")
