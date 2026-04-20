"""
Momentum Sniper Strategy — Pure momentum trading on crypto binary markets.

Exploits price displacement between spot (Coinbase/Binance) and Polymarket
binary market pricing. When spot moves relative to the market strike,
we snipe the side matching the displacement direction.

How it works:
    1. Coinbase/Binance WebSocket provides sub-second crypto price updates
    2. Compute displacement = (spot - strike) / strike
    3. If |displacement| >= min_momentum AND TTE in configured window
       AND entry price in range → fire on the displacement direction
    4. Hold to settlement: binary pays $1 on win, $0 on loss
    5. Auto-redeem winnings and compound into next market

    NO fair value model. NO Black-Scholes. NO edge calculation.
    Three filters only: momentum + TTE + entry-price.

Execution modes:
    - FAK taker: instant fill-or-kill at best ask (primary)
    - GTD maker: resting limit orders below ask with auto-expiry (optional)

Usage:
    from strategies.momentum_sniper import MomentumSniperStrategy, SniperConfig

    config = SniperConfig(coins=["BTC", "ETH"], timeframe="5m", bankroll=20.0)
    strategy = MomentumSniperStrategy(bot, config)
    await strategy.run()
"""

import asyncio
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, Optional, List, Tuple
from datetime import datetime, timezone, timedelta

from lib.binance_ws import BinancePriceFeed
from lib.coinbase_ws import CoinbasePriceFeed, COINBASE_SYMBOLS
from lib.edge_model import EdgeModel
# fair_value / direct_fv REMOVED — bot is pure momentum + TTE + entry-price
from lib.console import Colors, LogBuffer, log
from lib.trade_logger import TradeLogger
from lib.signal_logger import SignalLogger, SignalRecord
from lib.shadow_logger import ShadowLogger
from lib.market_manager import MarketManager, MarketInfo
from src.bot import TradingBot, OrderResult
from src.websocket_client import MarketWebSocket, OrderbookSnapshot, OrderbookLevel, UserWebSocket
from src.gamma_client import GammaClient

# Coins that use Coinbase as primary price feed (~0.8ms latency).
# BNB is the only coin that stays on Binance (not on Coinbase).
COINBASE_COINS = set(COINBASE_SYMBOLS.keys())  # {BTC, ETH, SOL, XRP, DOGE, HYPE}


class MultiPriceFeed:
    """
    Aggregates multiple per-exchange price feeds into one interface.

    First-tick-wins semantics: get_price / get_age return the freshest
    observation across all feeds that support a given coin. Lets us benefit
    from whichever exchange ticks fastest on each individual move.

    Backward compatible with the (primary_feed, coinbase_feed) legacy init.
    Extra feeds (Kraken, Bybit spot, etc.) can be attached via `extra_feeds`.
    """

    def __init__(self, primary_feed, coinbase_feed=None, extra_feeds=None):
        """
        Args:
            primary_feed: BinancePriceFeed or ChainlinkPriceFeed for most coins
            coinbase_feed: CoinbasePriceFeed for HYPE (and others if used)
            extra_feeds: list of additional feeds (KrakenPriceFeed, BybitSpotPriceFeed, ...)
        """
        self._primary = primary_feed
        self._coinbase = coinbase_feed
        self._feeds: list = [primary_feed]
        if coinbase_feed is not None:
            self._feeds.append(coinbase_feed)
        if extra_feeds:
            self._feeds.extend([f for f in extra_feeds if f is not None])

    def _feeds_for(self, coin: str) -> list:
        """Return feeds that advertise data for this coin (has it in _state)."""
        c = coin.upper()
        out = []
        for f in self._feeds:
            state = getattr(f, "_state", None)
            if state and c in state:
                out.append(f)
        return out

    def _freshest(self, coin: str):
        """Return (feed, last_update_ts) with newest observation for coin, or (None, 0)."""
        best_feed = None
        best_ts = 0.0
        for f in self._feeds_for(coin):
            state = f._state[coin.upper()]
            if state.price > 0 and state.last_update > best_ts:
                best_feed = f
                best_ts = state.last_update
        return best_feed, best_ts

    @property
    def connected(self) -> bool:
        # Primary must be up. Aux feeds can fail gracefully (we just lose their tick).
        return bool(self._primary.connected)

    def get_price(self, coin: str = "BTC") -> float:
        feed, _ = self._freshest(coin)
        if feed is not None:
            return feed.get_price(coin)
        # Fallback to legacy routing if no feed has data yet (startup race).
        if self._coinbase and coin.upper() in COINBASE_COINS:
            return self._coinbase.get_price(coin)
        return self._primary.get_price(coin)

    def get_age(self, coin: str = "BTC") -> float:
        # Return the SMALLEST age across feeds (newest data).
        best_age = float("inf")
        for f in self._feeds_for(coin):
            age = f.get_age(coin)
            if age < best_age:
                best_age = age
        return best_age

    def get_volatility(self, coin: str = "BTC") -> float:
        # Prefer the freshest feed's vol estimate.
        feed, _ = self._freshest(coin)
        if feed is not None:
            return feed.get_volatility(coin)
        if self._coinbase and coin.upper() in COINBASE_COINS:
            return self._coinbase.get_volatility(coin)
        return self._primary.get_volatility(coin)

    def get_momentum(self, coin: str, lookback_seconds: float = 30.0) -> float:
        # Use the freshest feed's price history for the momentum calc.
        # Each feed maintains its own history via its own sampling; we pick
        # whichever has the most recent tick.
        feed, _ = self._freshest(coin)
        if feed is not None:
            return feed.get_momentum(coin, lookback_seconds)
        if self._coinbase and coin.upper() in COINBASE_COINS:
            return self._coinbase.get_momentum(coin, lookback_seconds)
        return self._primary.get_momentum(coin, lookback_seconds)

    async def start(self) -> bool:
        # Start the primary first (must succeed) then others in parallel.
        ok = await self._primary.start()
        aux = [f for f in self._feeds if f is not self._primary]
        if aux:
            import asyncio as _aio
            results = await _aio.gather(*[f.start() for f in aux], return_exceptions=True)
            for f, r in zip(aux, results):
                if isinstance(r, Exception):
                    logger.warning(f"aux feed {type(f).__name__} failed to start: {r}")
        return ok

    async def stop(self):
        import asyncio as _aio
        await _aio.gather(
            *[f.stop() for f in self._feeds], return_exceptions=True
        )


# Taker fee rates per timeframe.
# As of 2026-04: CLOB docs show 0 bps maker and taker for all volume levels.
# Live trade data confirms: bet_size = price * tokens exactly, no fee deducted.
TAKER_FEE_RATES = {
    "5m": 0.0,
    "15m": 0.0,
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

    # Conservative mode: always bet fixed number of tokens per trade.
    # Use this when bankroll is small and you need statistical significance
    # before committing to full Kelly sizing.
    min_size_mode: bool = False

    # Number of tokens per trade in min_size_mode. Default 5.
    # At low bankroll, use 2-3 for survivability (2 tokens at $0.70 = $1.40/trade).
    min_size_tokens: float = 5.0

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

    # Multi-exchange first-tick aggregation (added Apr-2026).
    # When True, subscribes to Kraken + Bybit Spot in addition to the primary
    # Binance/Coinbase feeds. MultiPriceFeed returns the freshest tick across
    # all feeds, letting us react on whichever exchange ticks first.
    multi_exchange: bool = False

    # Book-imbalance confirmation gate (added Apr-2026).
    # When firing FAK, check Polymarket bid-depth on our side vs opposite.
    # our_ratio = our_side_bid_depth / (our_side_bid_depth + opposite_side_bid_depth)
    # If our_ratio < imbalance_min_ratio → skip fire (book strongly disagrees).
    # 0.0 = disabled (default OFF for safety; use --imbalance-min-ratio to enable).
    # 0.40 = skip only when our side has < 40% of combined bid depth (permissive).
    # 0.50 = skip unless book is at least neutral toward our side.
    # 0.55 = require mild book agreement.
    imbalance_min_ratio: float = 0.0

    # Pre-signal speculative GTC: place maker orders before signal fully confirms.
    # When momentum crosses speculative_momentum (lower than min_momentum), a GTC
    # rests on the book. If signal confirms, we're already in (zero fees, no race).
    # If momentum reverses, the GTC is cancelled.
    speculative_enabled: bool = False
    speculative_momentum: float = 0.0003  # Pre-signal momentum threshold

    # GTD maker order system.
    # Master switch (off by default). When enabled, ALL FAK firing paths are disabled.
    maker_enabled: bool = False
    max_maker_orders: int = 6            # Max simultaneous resting orders (3 coins × 2 sides)
    maker_poll_interval: float = 5.0     # CLOB poll frequency (seconds)
    maker_pre_momentum: float = 0.0005   # Lower threshold to trigger maker placement (single-side mode)
    maker_sustain_seconds: float = 5.0   # Momentum sustain before placing (single-side mode)

    # Dual-GTD mode: rest on BOTH UP and DOWN sides at a fixed price.
    # The fill IS the signal — whichever side fills first is the trade.
    # No direction prediction, no momentum confirmation for placement.
    maker_dual_mode: bool = False
    maker_rest_price: float = 0.55       # Fixed price to rest GTD orders at
    maker_place_elapsed: float = 120.0   # Seconds into window before placing orders
    maker_dynamic_pricing: bool = False  # Use best_ask - 0.01 instead of fixed rest price
    maker_max_entry_price: float = 0.65  # Cap for dynamic pricing (EV goes negative above this)
    maker_cancel_momentum: float = 0.0008  # Cancel threshold (higher than placement to give cancel latency buffer)
    # Queue-aware placement gate. Skip placement if bid depth at >= rest_price
    # exceeds this threshold. Heavy queue = our order would only fill on deep
    # dips (= adverse selection). None disables the gate.
    maker_max_queue_ahead: float = 0.0   # 0 = disabled; positive value = skip if total bid size at ≥ rest is greater than N

    # Momentum lookback period in seconds
    momentum_lookback: float = 30.0

    # Fair value REMOVED — field kept for config compat but never used for filtering.
    min_fair_value: float = 0.0      # DISABLED — pure momentum, no FV model

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

    # Position limits: prevent deploying entire bankroll simultaneously.
    # max_concurrent_positions=1 means only 1 open trade at a time across ALL coins.
    # This prevents the V6 failure mode where 9 trades fired in 6 minutes.
    max_concurrent_positions: int = 99  # uncapped — 0% bust at all levels on recorder data

    # Market settings
    market_check_interval: float = 30.0

    # Price source for fair value calculation.
    # "binance" = Binance WebSocket (fast but NOT settlement source)
    # "chainlink" = Polymarket RTDS Chainlink feed (settlement source)
    price_source: str = "binance"

    # Direct FV REMOVED — fields kept for config compat but never used.
    use_direct_fv: bool = False
    direct_fv_calibration: str = ""

    # Vatic API: use exact Chainlink strike prices instead of Binance approximation
    use_vatic: bool = True
    # Require Vatic strike — skip market entirely if Vatic fails (no Binance/backsolve fallback)
    require_vatic: bool = False

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

    # Side filter: "both" (default), "up" (UP-ONLY), "down" (DOWN-ONLY),
    # or "trend" (EMA-directed — trade UP when bullish, DOWN when bearish).
    # "trend" requires ema_fast and ema_slow to be set.
    side_filter: str = "both"

    # EMA trend detection: fast/slow EMA crossover on 5-minute prices.
    # EMA fast > slow = bullish (trade UP), fast < slow = bearish (trade DOWN).
    # Periods are in 5-minute candles: 6 = 30min, 24 = 2hr.
    ema_fast: int = 6
    ema_slow: int = 24

    # Block weekends: skip trading on Saturday and Sunday (UTC).
    # Backtest shows weekend WR is 5-10% below weekday across all configs.
    block_weekends: bool = False

    # Circuit breaker: pause trading after N consecutive losses.
    # At 69% WR, 5 in a row = 0.29% chance. Protects bankroll from zeroing.
    # 0 = disabled. Bot logs CIRCUIT BREAKER and stops placing new trades.
    max_consecutive_losses: int = 0

    # Balance floor: stop trading if USDC balance drops below this amount.
    # Preserves capital instead of bleeding to zero. 0 = disabled.
    balance_floor: float = 0.0

    # Signal logging: directory to write per-coin signal CSVs.
    # Empty string = disabled. When set, logs every signal evaluation
    # (not just trades) for offline filter optimization.
    signal_log_dir: str = ""

    # Shadow logging: CSV path for matched paper-vs-live tracking.
    # When set, every qualifying signal gets a paper record alongside
    # the live trade, enabling real-time degradation measurement.
    # Empty string = disabled.
    shadow_log: str = ""

    # FOK price tolerance: add this many cents to the ask price on the initial
    # FOK submission to absorb orderbook movement and reduce rejections.
    # Edge is still calculated against the original ask — tolerance just means
    # we're willing to pay slightly more to ensure the fill.
    # 0.0 = disabled (submit at exact ask). 0.01 = 1 cent tolerance (default).
    fok_tolerance: float = 0.04

    # Maximum spread (ask - bid) to allow trading. Tight spreads mean
    # makers are confident in the price. Wide spreads = uncertainty.
    # Data: spread <= $0.02 has 80% WR vs 67% for spread > $0.02.
    max_spread: float = 0.05

    # BTC negative filter: block alt trades that disagree with BTC's
    # direction when BTC's ask is >= this value. 0 = disabled.
    # Data: +54% daily EV improvement, removes 59.8% WR trades, keeps 65.7% WR.
    btc_block_entry: float = 0.0
    # BTC displacement threshold for the block filter. BTC must have moved
    # at least this much from strike before the filter activates.
    btc_block_momentum: float = 0.0003

    # Number of FOK retry steps after initial rejection. Each step adds +1c.
    # With tolerance=0.04 and retries=3: tries at +4c, +5c, +6c, +7c.
    fok_retry_steps: int = 3

    # Enhanced circuit breaker: rolling window of last 10 trade outcomes.
    # - 3 consecutive losses -> pause trading for 1 hour
    # - 5 losses out of last 10 -> stop live trading entirely
    # Cannot be disabled by any automated system — only by explicit CLI flag.
    enable_circuit_breaker: bool = True

    # Edge model: empirical probability model calibrated on IC data.
    # Compares our estimated live WR to the market price to compute edge.
    # Only trades when edge > min_edge_pct.
    # Path to IC CSV file for calibration. Empty = disabled.
    edge_model_path: str = ""
    # Minimum edge (as fraction) to trade. 0.0 = any positive edge.
    # edge = estimated_live_WR - entry_price
    min_edge_pct: float = 0.0

    # Backtest-calibrated guardrails. Thresholds set just OUTSIDE worst
    # case seen in simulation. If ANY triggers, bot pauses (touches .bot_paused).
    # These must be recalibrated for each new config via config_sweep.py.
    guardrail_max_consec_losses: int = 16        # backtest max was 14
    guardrail_max_window_loss: float = 26.0      # backtest max was $21.90
    guardrail_max_drawdown_pct: float = 40.0     # backtest max was 39.1%
    guardrail_balance_floor: float = 5.0         # absolute minimum



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


class EMATracker:
    """
    EMA crossover trend detector per coin.

    On startup, fetches historical 5m klines from Binance to warm up EMAs
    immediately (no cold-start delay). Then updates on each new market
    window with the Vatic strike price (= exact Chainlink open).

    EMA fast > slow = bullish → trade UP.
    EMA fast < slow = bearish → trade DOWN.
    """

    def __init__(self, fast_period: int = 6, slow_period: int = 24):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self._alpha_fast = 2.0 / (fast_period + 1)
        self._alpha_slow = 2.0 / (slow_period + 1)
        self._ema_fast: Dict[str, float] = {}
        self._ema_slow: Dict[str, float] = {}
        self._valid: Dict[str, bool] = {}
        self._last_update_slug: Dict[str, str] = {}  # prevent double-updates
        self._logger = logging.getLogger("sniper.ema")

    def initialize(self, coin: str) -> bool:
        """Fetch historical 5m klines and compute initial EMAs.

        Uses Coinbase for HYPE, Binance for everything else.
        """
        import requests as _req
        limit = self.slow_period * 3

        try:
            if coin.upper() in COINBASE_COINS:
                closes = self._fetch_coinbase_candles(_req, coin, limit)
            else:
                closes = self._fetch_binance_candles(_req, coin, limit)

            if closes is None or len(closes) < self.slow_period:
                self._valid[coin] = False
                return False

            # Compute EMAs from historical data
            ema_fast = closes[0]
            ema_slow = closes[0]
            for price in closes[1:]:
                ema_fast = self._alpha_fast * price + (1 - self._alpha_fast) * ema_fast
                ema_slow = self._alpha_slow * price + (1 - self._alpha_slow) * ema_slow

            self._ema_fast[coin] = ema_fast
            self._ema_slow[coin] = ema_slow
            self._valid[coin] = True
            return True

        except Exception as e:
            self._logger.warning(f"EMA init failed for {coin}: {e}")
            self._valid[coin] = False
            return False

    def _fetch_binance_candles(self, _req, coin: str, limit: int):
        """Fetch historical 5m klines from Binance."""
        symbol = f"{coin.upper()}USDT"
        resp = _req.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "limit": limit},
            timeout=10,
        )
        if resp.status_code != 200:
            self._logger.warning(f"Binance klines {resp.status_code} for {coin}")
            return None
        klines = resp.json()
        return [float(k[4]) for k in klines]  # k[4] = close price

    def _fetch_coinbase_candles(self, _req, coin: str, limit: int):
        """Fetch historical 5m candles from Coinbase for coins not on Binance."""
        from lib.coinbase_ws import COINBASE_SYMBOLS
        product_id = COINBASE_SYMBOLS.get(coin.upper())
        if not product_id:
            self._logger.warning(f"No Coinbase symbol for {coin}")
            return None
        # Coinbase candles endpoint: granularity 300 = 5 min
        resp = _req.get(
            f"https://api.exchange.coinbase.com/products/{product_id}/candles",
            params={"granularity": 300},
            timeout=10,
        )
        if resp.status_code != 200:
            self._logger.warning(f"Coinbase candles {resp.status_code} for {coin}")
            return None
        candles = resp.json()
        # Coinbase returns [time, low, high, open, close, volume] in DESCENDING order
        # Reverse to chronological and extract close prices
        candles.reverse()
        closes = [float(c[4]) for c in candles[-limit:]]
        return closes

    def update(self, coin: str, price: float, slug: str = ""):
        """Update EMAs with a new price observation (once per market window)."""
        if slug and slug == self._last_update_slug.get(coin):
            return  # Already updated for this market window
        if slug:
            self._last_update_slug[coin] = slug

        if coin not in self._ema_fast:
            self._ema_fast[coin] = price
            self._ema_slow[coin] = price
            return

        self._ema_fast[coin] = (
            self._alpha_fast * price + (1 - self._alpha_fast) * self._ema_fast[coin]
        )
        self._ema_slow[coin] = (
            self._alpha_slow * price + (1 - self._alpha_slow) * self._ema_slow[coin]
        )

    def is_bullish(self, coin: str) -> bool:
        """True if EMA fast > EMA slow (uptrend)."""
        if coin not in self._ema_fast:
            return True  # Default bullish if no data
        return self._ema_fast[coin] > self._ema_slow[coin]

    def is_valid(self, coin: str) -> bool:
        """True if enough data to trust EMA signals."""
        return self._valid.get(coin, False)

    def trend_str(self, coin: str) -> str:
        """One-line status for display."""
        if not self.is_valid(coin):
            return "EMA: warmup"
        fast = self._ema_fast.get(coin, 0)
        slow = self._ema_slow.get(coin, 0)
        direction = "BULL" if fast > slow else "BEAR"
        spread_pct = ((fast - slow) / slow * 100) if slow > 0 else 0
        return f"EMA({self.fast_period},{self.slow_period}): {direction} ({spread_pct:+.3f}%)"


@dataclass
class SpeculativeOrder:
    """A pre-signal GTC resting on the book as maker. (Legacy — kept for reference.)"""
    coin: str
    side: str
    order_id: str
    token_id: str
    price: float
    size: float
    market_slug: str
    placed_at: float
    fair_value: float = 0.0  # DEPRECATED — always 0.0, no FV model
    displacement: float = 0.0


@dataclass
class MakerOrder:
    """A GTD maker order resting on the book, waiting for fill."""
    coin: str
    side: str              # "up" / "down"
    order_id: str
    token_id: str
    price: float           # our limit price
    size: float            # tokens requested
    market_slug: str
    placed_at: float       # time.time()
    expiry_ts: int         # unix timestamp for GTD expiry
    status: str = "LIVE"   # "LIVE", "FILLED", "CANCELLED", "EXPIRED"
    size_matched: float = 0.0
    last_polled: float = 0.0
    reserved_usdc: float = 0.0
    condition_id: str = "" # market condition ID for trades API fill detection


class OrderLedger:
    """Thread-safe ledger tracking all resting maker orders."""

    def __init__(self):
        self._orders: Dict[str, MakerOrder] = {}
        self._lock = threading.Lock()

    def add_order(self, order: MakerOrder):
        """Add a new maker order to the ledger."""
        with self._lock:
            self._orders[order.order_id] = order

    def mark_filled(self, order_id: str, size_matched: float) -> Optional[MakerOrder]:
        """Transition order to FILLED status. Returns the order or None."""
        with self._lock:
            order = self._orders.get(order_id)
            if order and order.status == "LIVE":
                order.status = "FILLED"
                order.size_matched = size_matched
                return order
            return None

    def mark_cancelled(self, order_id: str) -> Optional[MakerOrder]:
        """Transition order to CANCELLED status. Returns the order or None."""
        with self._lock:
            order = self._orders.get(order_id)
            if order and order.status == "LIVE":
                order.status = "CANCELLED"
                return order
            return None

    def get_live_orders(self) -> List[MakerOrder]:
        """Return all LIVE orders."""
        with self._lock:
            return [o for o in self._orders.values() if o.status == "LIVE"]

    def get_orders_for_market(self, slug: str) -> List[MakerOrder]:
        """Return all orders (any status) for a given market slug."""
        with self._lock:
            return [o for o in self._orders.values() if o.market_slug == slug]

    def get_orders_for_coin(self, coin: str) -> List[MakerOrder]:
        """Return all orders (any status) for a given coin."""
        with self._lock:
            return [o for o in self._orders.values() if o.coin == coin]

    def get_live_orders_for_coin(self, coin: str) -> List[MakerOrder]:
        """Return LIVE orders for a given coin."""
        with self._lock:
            return [o for o in self._orders.values()
                    if o.coin == coin and o.status == "LIVE"]

    def get_live_orders_for_coin_side(self, coin: str, side: str) -> List[MakerOrder]:
        """Return LIVE orders for a given coin and side."""
        with self._lock:
            return [o for o in self._orders.values()
                    if o.coin == coin and o.side == side and o.status == "LIVE"]

    def remove_order(self, order_id: str):
        """Remove an order from the ledger entirely."""
        with self._lock:
            self._orders.pop(order_id, None)

    def count_live(self) -> int:
        """Count of LIVE orders."""
        with self._lock:
            return sum(1 for o in self._orders.values() if o.status == "LIVE")


class DualGTDState:
    """
    State machine for dual-GTD per coin per window.

    States:
        IDLE       → waiting for placement conditions
        RESTING    → orders are live on the CLOB
        FILLED     → one side filled, holding to settlement
        CANCELLED  → reversal cancelled, done for this window
        DONE       → window ended, no more action

    Transitions:
        IDLE → RESTING       (placement conditions met, orders placed)
        RESTING → FILLED     (fill detected)
        RESTING → CANCELLED  (momentum reversed past threshold)
        RESTING → DONE       (window ending, orders expired)
        FILLED → DONE        (market settled)
        CANCELLED → DONE     (window ended)

    NO re-entry: CANCELLED never goes back to IDLE or RESTING.
    """

    IDLE = "IDLE"
    RESTING = "RESTING"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    DONE = "DONE"

    def __init__(self):
        # {slug:coin → state}
        self._states: Dict[str, str] = {}
        self._lock = threading.Lock()

    def get(self, slug: str, coin: str) -> str:
        with self._lock:
            return self._states.get(f"{slug}:{coin}", self.IDLE)

    def set(self, slug: str, coin: str, state: str):
        with self._lock:
            self._states[f"{slug}:{coin}"] = state

    def can_place(self, slug: str, coin: str) -> bool:
        """Only IDLE coins can have orders placed."""
        return self.get(slug, coin) == self.IDLE

    def can_cancel(self, slug: str, coin: str) -> bool:
        """Only RESTING coins can be cancelled."""
        return self.get(slug, coin) == self.RESTING

    def mark_resting(self, slug: str, coin: str):
        with self._lock:
            key = f"{slug}:{coin}"
            if self._states.get(key, self.IDLE) == self.IDLE:
                self._states[key] = self.RESTING

    def mark_filled(self, slug: str, coin: str):
        with self._lock:
            key = f"{slug}:{coin}"
            # Can fill from RESTING or CANCELLED (fill-during-cancel)
            if self._states.get(key) in (self.RESTING, self.CANCELLED):
                self._states[key] = self.FILLED

    def mark_cancelled(self, slug: str, coin: str):
        with self._lock:
            key = f"{slug}:{coin}"
            if self._states.get(key) == self.RESTING:
                self._states[key] = self.CANCELLED

    def mark_done(self, slug: str, coin: str):
        with self._lock:
            self._states[f"{slug}:{coin}"] = self.DONE

    def cleanup(self, old_slug: str):
        """Remove all entries for an old window."""
        with self._lock:
            keys = [k for k in self._states if k.startswith(f"{old_slug}:")]
            for k in keys:
                del self._states[k]


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

        # Fair value REMOVED — pure momentum + TTE + entry-price only.
        # No BinaryFairValue, no DirectFairValue, no Black-Scholes.
        self.fv_calc = None

        # Real-time price feed (all coins)
        # Chainlink = Polymarket's settlement source (most accurate for outcomes)
        # Binance = faster updates, but diverges from settlement on close markets
        # HYPE uses Coinbase (not on Binance)
        binance_coins = [c for c in config.coins if c not in COINBASE_COINS]
        coinbase_coins = [c for c in config.coins if c in COINBASE_COINS]

        if config.price_source == "chainlink":
            from lib.chainlink_ws import ChainlinkPriceFeed
            primary_feed = ChainlinkPriceFeed(coins=binance_coins) if binance_coins else None
        else:
            primary_feed = BinancePriceFeed(coins=binance_coins) if binance_coins else None

        coinbase_feed = CoinbasePriceFeed(coins=coinbase_coins) if coinbase_coins else None

        # Extra exchange feeds for first-tick aggregation (Apr-2026).
        # Enabled via config.multi_exchange. MultiPriceFeed picks whichever
        # exchange delivers the freshest tick per get_price call.
        #
        # Kraken DROPPED — prior measurement showed 0.5% first-tick share
        # (dead weight, see measure_feed_leaders results).
        extra_feeds: list = []
        if getattr(config, "multi_exchange", False):
            try:
                from lib.bybit_spot_ws import BybitSpotPriceFeed, BYBIT_SPOT_SYMBOLS
                b_coins = [c for c in config.coins if c in BYBIT_SPOT_SYMBOLS]
                if b_coins:
                    extra_feeds.append(BybitSpotPriceFeed(coins=b_coins))
            except Exception as e:
                logger.warning(f"BybitSpotPriceFeed unavailable: {e}")
            try:
                from lib.okx_spot_ws import OKXSpotPriceFeed, OKX_SPOT_SYMBOLS
                o_coins = [c for c in config.coins if c in OKX_SPOT_SYMBOLS]
                if o_coins:
                    extra_feeds.append(OKXSpotPriceFeed(coins=o_coins))
            except Exception as e:
                logger.warning(f"OKXSpotPriceFeed unavailable: {e}")

        # If only Coinbase coins (e.g. only HYPE), coinbase IS the primary
        if primary_feed is None and coinbase_feed is not None:
            if extra_feeds:
                self.binance = MultiPriceFeed(coinbase_feed, None, extra_feeds=extra_feeds)
            else:
                self.binance = coinbase_feed
        elif coinbase_feed is not None:
            self.binance = MultiPriceFeed(primary_feed, coinbase_feed, extra_feeds=extra_feeds)
        elif extra_feeds:
            self.binance = MultiPriceFeed(primary_feed, None, extra_feeds=extra_feeds)
        else:
            self.binance = primary_feed

        # Deribit implied volatility feed (forward-looking, market consensus)
        self._deribit_feed = None
        try:
            from lib.deribit_vol import DeribitVolFeed
            self._deribit_feed = DeribitVolFeed(coins=config.coins)
        except Exception:
            pass

        # Vatic API REMOVED — lib/vatic_client.py deleted.
        # Strike is determined by Binance/Coinbase spot or back-solve only.
        self._vatic = None

        # EMA trend tracker (initialized in start() with historical data)
        self._ema_tracker: Optional[EMATracker] = None
        if config.side_filter == "trend":
            self._ema_tracker = EMATracker(
                fast_period=config.ema_fast,
                slow_period=config.ema_slow,
            )

        # Per-coin market managers
        self.coin_states: Dict[str, CoinMarketState] = {}

        # Trade logger
        self.trade_logger = TradeLogger(config.log_file)

        # Signal logger (Layer 1: captures all signal evaluations)
        self.signal_logger: Optional[SignalLogger] = None
        if config.signal_log_dir:
            self.signal_logger = SignalLogger(log_dir=config.signal_log_dir)

        # Shadow logger: matched paper-vs-live tracking on same markets
        self.shadow_logger: Optional[ShadowLogger] = None
        if config.shadow_log:
            self.shadow_logger = ShadowLogger(config.shadow_log)

        # VPIN tracker — data collection mode (logs VPIN alongside signals, no filtering)
        self._vpin_tracker = None
        try:
            from lib.vpin import VPINTracker, VPINConfig
            self._vpin_tracker = VPINTracker(VPINConfig(
                bucket_volume=30.0,  # Small buckets for 5m market volume
                n_buckets=15,
                min_buckets=3,
            ))
        except Exception:
            pass

        # Fast order client — bypasses SDK for ~100-200ms savings per order
        self._fast_order = None
        try:
            from lib.fast_order import FastOrderClient
            import os
            # Try multiple env var naming conventions
            pk = os.environ.get("POLY_PRIVATE_KEY", "") or os.environ.get("PRIVATE_KEY", "")
            safe = os.environ.get("POLY_SAFE_ADDRESS", "") or os.environ.get("POLYMARKET_SAFE_ADDRESS", "")
            ak = os.environ.get("POLY_BUILDER_API_KEY", "") or os.environ.get("CLOB_API_KEY", "")
            ask_key = os.environ.get("POLY_BUILDER_API_SECRET", "") or os.environ.get("CLOB_SECRET", "")
            ap = os.environ.get("POLY_BUILDER_API_PASSPHRASE", "") or os.environ.get("CLOB_API_PASSPHRASE", "")
            if all([pk, safe, ak, ask_key, ap]):
                self._fast_order = FastOrderClient(pk, safe, ak, ask_key, ap)
            else:
                import logging as _log
                _log.getLogger(__name__).warning(f"FastOrderClient: missing env vars")
        except Exception as e:
            import logging as _log
            _log.getLogger(__name__).warning(f"FastOrderClient not available: {e}")

        # Session stats
        self.stats = SniperStats()

        # USDC balance tracking
        self._balance: float = config.bankroll
        self._last_balance_check: float = 0.0

        # Log buffer for display
        self._log_buffer = LogBuffer(max_size=8)

        # (direct_fv_msg removed — no fair value model)

        # Redemption tracking
        self._last_redeem_time: float = 0.0
        self._redeem_interval: float = 60.0  # Check every 60 seconds

        # Background settlement tracking
        self._last_settle_time: float = 0.0

        # Pause flag — checks data/.bot_paused file
        self._pause_flag_dir = "data"
        self._pause_cache_val = False
        self._pause_cache_time: float = 0.0
        self._pause_orders_cancelled = False  # Track if we already cancelled on this pause

        # CUSUM edge monitor
        self._edge_monitor = EdgeMonitor(
            target_wr=config.cusum_target_wr,
            threshold=config.cusum_threshold,
        ) if config.enable_cusum else None

        # Edge model: empirical probability from IC data
        self._edge_model = None
        if config.edge_model_path:
            self._edge_model = EdgeModel.from_csv(config.edge_model_path)
            if self._edge_model.loaded:
                self._event_logger.info(f"EdgeModel loaded: {self._edge_model.summary()}")
            else:
                self._event_logger.warning(f"EdgeModel FAILED to load from {config.edge_model_path}")

        # Maker IC v1 REMOVED — replaced by Shadow Maker IC v2
        # v1 was misleading (showed 90% WR for configs that ran at 35% live)
        # v1 code preserved in lib/maker_collector.py but no longer called
        self._maker_collector = None

        # Shadow Maker IC v2: simulates full dual-GTD lifecycle for trustworthy backtest
        self._shadow_maker = None
        if config.maker_enabled or config.maker_dual_mode:
            from lib.shadow_maker import ShadowMaker, ShadowConfig
            # Broad sweep: 12 prices × 8 momentums × 5 placements = 480 configs
            # max_concurrent=99 (uncapped) — simulate concurrent offline from fill timestamps
            shadow_configs = []
            for rp in [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
                for cm in [0.0003, 0.0005, 0.0006, 0.0008, 0.0010, 0.0012, 0.0015, 0.0020]:
                    for pe in [60, 90, 120, 150, 180]:
                        cid = f"rp{int(rp*100)}_cm{round(cm*10000)}_pe{pe}"
                        shadow_configs.append(ShadowConfig(
                            config_id=cid, rest_price=rp,
                            cancel_momentum=cm, place_elapsed=float(pe),
                            max_concurrent=99,
                            # Bot parity — enforce same placement gates
                            min_momentum=config.min_momentum,
                            max_queue_ahead=config.maker_max_queue_ahead,
                            max_entry_price=config.max_entry_price,
                        ))
            self._shadow_maker = ShadowMaker(
                output_path="data/shadow_maker.csv",
                configs=shadow_configs,
            )
            self._event_logger.info(
                f"ShadowMaker started: {len(shadow_configs)} configs → data/shadow_maker.csv"
            )

        # Pending signals awaiting confirmation
        self._pending_signals: Dict[str, PendingSignal] = {}

        # Pre-signal speculative GTC order (only one at a time) — legacy
        self._speculative_order: Optional[SpeculativeOrder] = None
        self._spec_cooldown: Dict[str, float] = {}  # coin:side -> time of last cancel

        # GTD maker order ledger (Phase 1 maker system)
        self._order_ledger = OrderLedger()
        self._gtd_state = DualGTDState()
        self._user_ws = None  # Set in start() if credentials available
        self._pending_ws_subs = []
        self._maker_lock = threading.Lock()  # protects balance changes for maker orders
        self._maker_momentum_first_seen: Dict[str, float] = {}  # "coin:side" -> time first crossed threshold

        # Circuit breaker: consecutive losing WINDOWS (not individual trades)
        # 3 coins losing in the same 5m window = 1 losing window, not 3 losses
        self._consecutive_loss_windows: int = 0
        self._last_loss_window: str = ""  # slug window timestamp of last counted loss
        self._circuit_breaker_tripped: bool = False

        # Enhanced circuit breaker: rolling window of recent trade outcomes
        self._cb_recent_outcomes: List[bool] = []  # last 10 trade outcomes (True=win)
        self._cb_paused_until: Optional[datetime] = None  # 1-hour pause after 3 consecutive losses
        self._cb_stopped: bool = False  # 5/10 losses -> hard stop

        # Price ring buffer for strike lookback — matches collector exactly.
        # Records (timestamp, price) for each coin. When a new window opens,
        # _set_strike looks back to find the price at window-open time (t+0).
        from collections import deque, defaultdict
        self._price_ring: Dict[str, deque] = defaultdict(lambda: deque(maxlen=600))

        # Integrated collector: records threshold crossings in the same CSV
        # format as run_collector.py, using the EXACT same prices the bot
        # evaluates for trading. This guarantees live = collector by definition.
        self._collector_crossed: Dict[tuple, set] = {}  # (slug, side) -> set of crossed thresholds
        self._collector_pending: list = []  # pending signals awaiting resolution
        self._collector_thresholds = [0.0003, 0.0005, 0.0007, 0.001, 0.0012, 0.0015, 0.002, 0.003, 0.005]
        self._coll_debug_logged: set = set()  # dedup for COLL-DBG logging
        self._collector_csv = config.log_file.replace(".csv", ".collector.csv") if config.log_file else "data/live_collector.csv"
        self._collector_resolved = 0
        # Write header if file doesn't exist
        import os
        if not os.path.exists(self._collector_csv):
            with open(self._collector_csv, "w", newline="") as f:
                import csv as _csv
                _csv.writer(f).writerow([
                    "timestamp", "market_slug", "coin", "side",
                    "momentum_direction", "is_momentum_side",
                    "threshold", "entry_price", "best_bid",
                    "momentum", "elapsed", "outcome",
                ])

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
                if up_price > 0.9 and down_price > 0.9:
                    winning_side = "up" if up_price > down_price else "down"
                elif up_price > 0.9:
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
                if self.signal_logger:
                    self.signal_logger.resolve_outcome(
                        record.market_slug, record.side,
                        won=side_won, pnl=payout - record.bet_size_usdc,
                    )

                outcome = "WON" if side_won else "LOST"
                self.log(f"  Resolved orphan: {record.coin} {record.side.upper()} {record.market_slug} → {outcome}", "info")
                resolved += 1
            except Exception as e:
                self.log(f"  Failed to resolve {trade_key}: {e}", "warning")

        self.log(f"Resolved {resolved}/{len(pending)} orphaned trades", "success")

    def _refresh_balance(self):
        """Query USDC balance (rate-limited to every 10s). Sync version for non-async callers."""
        now = time.time()
        if now - self._last_balance_check < 10:
            return
        self._last_balance_check = now
        bal = self.bot.get_usdc_balance()
        if bal is not None:
            self._balance = bal

    async def _async_refresh_balance(self):
        """Query USDC balance without blocking event loop."""
        now = time.time()
        if now - self._last_balance_check < 10:
            return
        self._last_balance_check = now
        bal = await asyncio.to_thread(self.bot.get_usdc_balance)
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
        self._start_time = time.time()  # Skip trades within 60s of boot
        self._loop = None  # Set after event loop is running

        # Start price feeds (Binance + Coinbase for HYPE if needed)
        coinbase_coins = [c for c in self.config.coins if c in COINBASE_COINS]
        feed_label = "price feeds" if coinbase_coins else "Binance price feed"
        self.log(f"Starting {feed_label}...")
        if not await self.binance.start():
            self.log("Failed to start price feed", "error")
            return False

        for coin in self.config.coins:
            price = self.binance.get_price(coin)
            source = "Coinbase" if coin in COINBASE_COINS else "Binance"
            self.log(f"{source} {coin}: ${price:,.2f}", "success")

        # Register instant signal detection on price updates
        # Instead of waiting for next tick (100ms), evaluate signal immediately when price moves
        def _on_price_update(coin: str, price: float):
            """Called on EVERY Binance/Coinbase price tick.
            Records ticks in ring buffer AND evaluates integrated collector
            threshold crossings. NO trading from this path — trading is
            tick-loop only (prevents firing on momentary spikes)."""
            # Record tick in ring buffer — needed for strike lookback
            self._price_ring[coin].append((time.time(), price))

            # Integrated collector: record threshold crossings on every tick
            # (same as standalone collector). This ensures the integrated
            # collector captures the same signals as the standalone.
            try:
                state = self.coin_states.get(coin)
                if not state or not state.strike_price or state.strike_price <= 0:
                    return
                if not state.current_slug:
                    return
                # NOTE: do NOT check startup_slug here. The collector must
                # record ALL threshold crossings regardless of whether the
                # bot is in startup mode. Only trading code needs startup check.

                disp = (price - state.strike_price) / state.strike_price
                abs_disp = abs(disp)

                # Maker IC: runs at ALL momentum levels (needs early window data)
                if self._shadow_maker:
                    tte_mic = state.seconds_to_expiry()
                    duration_mic = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                    elapsed_mic = max(0, duration_mic - tte_mic)
                    _up_ask = self._get_best_ask_with_rest_fallback(state, 'up')
                    _up_bid = self._get_best_bid_with_rest_fallback(state, 'up')
                    _dn_ask = self._get_best_ask_with_rest_fallback(state, 'down')
                    _dn_bid = self._get_best_bid_with_rest_fallback(state, 'down')

                    # Book depth for queue model: sum bid sizes by price.
                    # Only populate if we have a book snapshot for that side.
                    _up_bid_depth: Dict[float, float] = {}
                    _dn_bid_depth: Dict[float, float] = {}
                    try:
                        _ob_up = state.manager.get_orderbook('up') if state.manager else None
                        if _ob_up and _ob_up.bids:
                            for lvl in _ob_up.bids:
                                _up_bid_depth[lvl.price] = _up_bid_depth.get(lvl.price, 0.0) + lvl.size
                        _ob_dn = state.manager.get_orderbook('down') if state.manager else None
                        if _ob_dn and _ob_dn.bids:
                            for lvl in _ob_dn.bids:
                                _dn_bid_depth[lvl.price] = _dn_bid_depth.get(lvl.price, 0.0) + lvl.size
                    except Exception:
                        pass

                    # Shadow Maker IC v2: tick at ALL elapsed (shadow handles its own timing)
                    if self._shadow_maker:
                        self._shadow_maker.tick(
                            coin=state.coin, slug=state.current_slug,
                            elapsed=elapsed_mic, spot=price, strike=state.strike_price,
                            up_ask=_up_ask, up_bid=_up_bid,
                            down_ask=_dn_ask, down_bid=_dn_bid,
                            up_bid_depth_at=_up_bid_depth if _up_bid_depth else None,
                            down_bid_depth_at=_dn_bid_depth if _dn_bid_depth else None,
                        )

                if abs_disp < 0.0003:  # below lowest threshold
                    return

                # DEBUG: log first threshold crossing per window (once per coin per slug)
                _dbg_key = (state.current_slug, coin)
                if _dbg_key not in self._coll_debug_logged:
                    self._coll_debug_logged.add(_dbg_key)
                    self._event_logger.info(
                        f"[COLL-DBG] {coin} disp={disp:+.6f} abs={abs_disp:.6f} "
                        f"slug={state.current_slug[-15:]} startup={state.startup_slug[-15:]} "
                        f"pending={len(self._collector_pending)}"
                    )

                mom_dir = "up" if disp > 0 else "down"
                opp_dir = "down" if mom_dir == "up" else "up"
                tte = state.seconds_to_expiry()
                duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                elapsed = max(0, duration - tte)

                # Cache ask/bid once per tick (avoid repeated lock acquisition
                # in _get_best_ask_with_rest_fallback which starves the event loop)
                _cached_asks = {}
                _cached_bids = {}
                for cs in [mom_dir, opp_dir]:
                    ck = (state.current_slug, cs)
                    if ck not in self._collector_crossed:
                        self._collector_crossed[ck] = set()
                    # Check if any new thresholds crossed before fetching prices
                    new_thresholds = [ct for ct in self._collector_thresholds
                                      if abs_disp >= ct and ct not in self._collector_crossed[ck]]
                    if not new_thresholds:
                        continue
                    # Fetch ask/bid once per side per tick
                    if cs not in _cached_asks:
                        _cached_asks[cs] = self._get_best_ask_with_rest_fallback(state, cs)
                        _cached_bids[cs] = self._get_best_bid_with_rest_fallback(state, cs)
                    ca = _cached_asks[cs]
                    cb = _cached_bids[cs]
                    for ct in new_thresholds:
                        self._collector_crossed[ck].add(ct)
                        self._collector_pending.append({
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                            "slug": state.current_slug,
                            "coin": state.coin,
                            "side": cs,
                            "momentum_direction": mom_dir,
                            "is_momentum_side": cs == mom_dir,
                            "threshold": ct,
                            "entry_price": round(ca, 4) if ca < 1.0 else 0.0,
                            "best_bid": round(cb, 4),
                            "momentum": round(abs_disp, 8),
                            "elapsed": round(elapsed, 1),
                            "_created": time.time(),
                        })
                if abs_disp < self.config.min_momentum:
                    return
                # GTD-only mode: skip FAK firing entirely, let _manage_maker_orders handle placement
                if self.config.maker_enabled:
                    return
                if not state.startup_slug or state.current_slug == state.startup_slug:
                    return
                if time.time() - self._start_time < 60:
                    return
                if self._is_paused():
                    return
                # Don't fire in the last 10s of window — market is settling,
                # ask/bid are unreliable, orders get rejected as invalid
                if tte < 10:
                    return

                side = mom_dir  # trade in momentum direction

                # BTC negative filter: block alt trades that disagree with
                # BTC's direction when BTC has a confident signal (entry >= $0.50).
                # Data: alts agreeing with BTC = 65.7% WR, disagreeing = 59.8% WR.
                # Simulation: +54% daily EV improvement, no change to min balance.
                if coin not in ("BTC", "ETH") and self.config.btc_block_entry > 0:
                    btc_state = self.coin_states.get("BTC")
                    if btc_state and btc_state.strike_price > 0:
                        btc_spot = self.binance.get_price("BTC")
                        btc_disp = (btc_spot - btc_state.strike_price) / btc_state.strike_price
                        btc_dir = "up" if btc_disp > 0 else "down"
                        if abs(btc_disp) >= self.config.btc_block_momentum:
                            btc_ask = self._get_best_ask_with_rest_fallback(btc_state, btc_dir)
                            if btc_ask >= self.config.btc_block_entry and side != btc_dir:
                                return  # alt disagrees with confident BTC — block

                # Block if we already have a position on EITHER side
                # (prevents hedging — trading UP then DOWN in same window)
                if state.has_up_position or state.has_down_position:
                    return

                best_ask = self._get_best_ask_with_rest_fallback(state, side)
                if best_ask <= 0 or best_ask > self.config.max_entry_price or best_ask < self.config.min_entry_price:
                    return

                # Spread filter — tight spread = makers committed to price
                best_bid = self._get_best_bid_with_rest_fallback(state, side)
                if best_bid > 0 and (best_ask - best_bid) > self.config.max_spread:
                    return

                # TTE check
                _ff_duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                _ff_tte = state.seconds_to_expiry()
                if _ff_tte < (_ff_duration - self.config.max_window_elapsed) or _ff_tte > (_ff_duration - self.config.min_window_elapsed):
                    return

                # Edge model: check empirical edge before firing
                if self._edge_model and self._edge_model.loaded:
                    _edge = self._edge_model.get_edge(best_ask, elapsed, abs_disp)
                    if _edge is None:
                        # No IC data for this bucket — skip (unknown territory)
                        self._event_logger.info(
                            f"[EDGE-SKIP] {coin} {side} ep={best_ask:.2f} el={elapsed:.0f}s "
                            f"mom={abs_disp:.5f} — no IC data for bucket"
                        )
                        return
                    if _edge < self.config.min_edge_pct:
                        _detail = self._edge_model.get_detail(best_ask, elapsed, abs_disp)
                        self._event_logger.info(
                            f"[EDGE-BLOCK] {coin} {side} ep={best_ask:.2f} el={elapsed:.0f}s "
                            f"mom={abs_disp:.5f} ic_wr={_detail['ic_wr']:.1%} "
                            f"live_wr={_detail['est_live_wr']:.1%} "
                            f"edge={_edge:+.1%} < min={self.config.min_edge_pct:.1%}"
                        )
                        return

                # Dedup: one signal per market/side
                snipe_key = f"{state.current_slug}:{side}"
                if not hasattr(self, '_snipe_in_flight'):
                    self._snipe_in_flight = set()
                if snipe_key in self._snipe_in_flight:
                    return
                self._snipe_in_flight.add(snipe_key)

                # Position limit
                active = sum(1 for s in self.coin_states.values()
                             if s.has_up_position or s.has_down_position)
                if active >= self.config.max_concurrent_positions:
                    self._snipe_in_flight.discard(snipe_key)
                    return

                # Book-imbalance confirmation gate (Apr-2026).
                _imb_ok, _imb_ratio, _imb_reason = self._check_book_imbalance(state, side)
                if not _imb_ok:
                    self._event_logger.info(
                        f"[IMBAL-SKIP] {coin} {side} our_ratio={_imb_ratio:.2%} "
                        f"< min={self.config.imbalance_min_ratio:.0%} — book disagrees"
                    )
                    self._snipe_in_flight.discard(snipe_key)
                    return
                if _imb_reason == "agree":
                    self._event_logger.info(
                        f"[IMBAL-OK] {coin} {side} our_ratio={_imb_ratio:.2%}"
                    )

                # Set position sentinel BEFORE queuing — prevents race where
                # a second tick fires the opposite side before the signal
                # thread processes the first. This is in the callback thread.
                if side == "up":
                    state.up_tokens = 0.01
                else:
                    state.down_tokens = 0.01

                signal_time = time.time()
                # Capture edge for logging (None if model not loaded)
                _signal_edge = _edge if (self._edge_model and self._edge_model.loaded) else None

                if self._fast_order:
                    _t_queued = time.time()
                    def _signal_task(state=state, side=side, best_ask=best_ask,
                                     disp=disp, signal_time=signal_time, snipe_key=snipe_key,
                                     sig_edge=_signal_edge):
                        fast_success = False
                        try:
                            buy_price = min(round(best_ask + self.config.fok_tolerance, 2),
                                            self.config.max_entry_price)
                            if buy_price < self.config.min_entry_price or self._balance < 1.0:
                                if side == "up": state.up_tokens = 0
                                else: state.down_tokens = 0
                                return

                            token_id = state.manager.token_ids.get(side)
                            if not token_id:
                                if side == "up": state.up_tokens = 0
                                else: state.down_tokens = 0
                                return

                            # Sentinel already set in callback thread above

                            # Try pre-signed order first (~23ms) then fall back to full sign (~280ms)
                            result = None
                            _path = "full-sign"
                            _ladder_size = 0
                            presigned_cache = getattr(self, '_presigned_cache', {})
                            ladder = presigned_cache.get((state.coin, side))
                            if ladder:
                                _ladder_size = len(ladder)
                                price_key = int(buy_price * 100)
                                body_str = ladder.get(price_key)
                                if body_str:
                                    result = self._fast_order.post_presigned_order(body_str)
                                    if result is not None:
                                        _path = "presigned"

                            # Fall back to full sign if no pre-signed match
                            if result is None:
                                result = self._fast_order._place_order_sync(
                                    token_id, buy_price, self.config.min_size_tokens, "BUY", "FAK", 1000,
                                )
                            has_error = bool(result.get("error", ""))
                            has_order_id = bool(result.get("orderID", ""))
                            taking = float(result.get("takingAmount", 0) or 0)
                            fast_success = has_order_id and not has_error and taking > 0
                            total_lat = (time.time() - signal_time) * 1000

                            self._event_logger.info(
                                f"[FAST-FIRE] {state.coin} {side} success={fast_success} "
                                f"path={_path} ladder={_ladder_size} "
                                f"id={result.get('orderID','')[:16] or 'none'} "
                                f"filled={taking:.1f} lat={total_lat:.0f}ms"
                                f"{' ERROR: ' + result.get('error','')[:60] if has_error else ''}"
                            )

                            if not fast_success:
                                # Log rejection details for adverse selection analysis
                                current_ask = state.manager.get_best_ask(side)
                                current_bid = state.manager.get_best_bid(side)
                                spread = current_ask - current_bid if current_bid > 0 else 0
                                self._event_logger.info(
                                    f"[FAK-REJECT] {state.coin} {side} bid=${buy_price:.2f} "
                                    f"ask_now=${current_ask:.2f} spread=${spread:.2f} "
                                    f"mom={abs_disp:.5f} el={elapsed:.0f}s "
                                    f"error={result.get('error','')[:80]} lat={total_lat:.0f}ms"
                                )
                                if side == "up":
                                    state.up_tokens = 0
                                else:
                                    state.down_tokens = 0
                                return

                            filled_tokens = taking
                            actual_cost = buy_price * filled_tokens
                            self._balance -= actual_cost

                            if side == "up":
                                state.up_tokens += filled_tokens
                                state.up_cost += actual_cost
                            else:
                                state.down_tokens += filled_tokens
                                state.down_cost += actual_cost

                            state.last_trade_time = time.time()
                            self.stats.trades += 1
                            self.stats.pending += 1
                            self.stats.total_wagered += actual_cost

                            edge_str = f"{sig_edge:+.1%}" if sig_edge is not None else "N/A"
                            self._event_logger.info(
                                f"SNIPE {state.coin} {side.upper()} @ {buy_price:.2f} "
                                f"x{filled_tokens:.0f} (${actual_cost:.2f}) "
                                f"edge={edge_str} [FAST-FIRE] strike={getattr(state, '_strike_source', '?')} "
                                f"lat={total_lat:.0f}ms"
                            )

                            spot = self.binance.get_price(state.coin)
                            vol, vol_src = self._get_volatility(state.coin)
                            self.trade_logger.log_trade(
                                market_slug=state.current_slug,
                                coin=state.coin,
                                timeframe=self.config.timeframe,
                                side=side,
                                entry_price=buy_price,
                                bet_size_usdc=actual_cost,
                                num_tokens=filled_tokens,
                                bankroll=self._balance,
                                usdc_balance=self._balance,
                                btc_price=spot,
                                other_side_price=state.manager.get_mid_price("down" if side == "up" else "up"),
                                volatility_std=vol,
                                fair_value_at_entry=0.0,
                                time_to_expiry_at_entry=state.seconds_to_expiry(),
                                momentum_at_entry=disp,
                                volatility_at_entry=vol,
                                signal_to_order_ms=0,
                                order_latency_ms=total_lat,
                                total_latency_ms=total_lat,
                                vol_source=vol_src,
                                strike_source=getattr(state, '_strike_source', 'unknown'),
                            )
                        except Exception as e:
                            self._event_logger.warning(f"[FAST-FIRE] {state.coin} {side} error: {e}")
                            if side == "up" and state.up_tokens == 0.01:
                                state.up_tokens = 0
                            elif side == "down" and state.down_tokens == 0.01:
                                state.down_tokens = 0
                        finally:
                            # Only discard snipe key on SUCCESS so failed signals
                            # don't retry on every subsequent tick (was causing 96%
                            # of rejections to be retry spam on the same failed order)
                            if fast_success:
                                self._snipe_in_flight.discard(snipe_key)

                    self._fast_order._signal_pool.submit(_signal_task)
                else:
                    self._snipe_in_flight.discard(snipe_key)

            except Exception as e:
                self._event_logger.warning(f"[COLL] {coin} error: {e}")

            if abs(disp) < self.config.min_momentum:
                return

            # GTD-only mode: skip tick-loop FAK firing
            if self.config.maker_enabled:
                return

            side = "up" if disp > 0 else "down"

            # BTC negative filter (tick-loop path) — same as fast-fire path
            if coin not in ("BTC", "ETH") and self.config.btc_block_entry > 0:
                btc_state = self.coin_states.get("BTC")
                if btc_state and btc_state.strike_price > 0:
                    btc_spot = self.binance.get_price("BTC")
                    btc_disp = (btc_spot - btc_state.strike_price) / btc_state.strike_price
                    btc_dir = "up" if btc_disp > 0 else "down"
                    if abs(btc_disp) >= 0.0003:  # BTC has a signal (IC-validated)
                        btc_ask = self._get_best_ask_with_rest_fallback(btc_state, btc_dir)
                        if btc_ask >= self.config.btc_block_entry and side != btc_dir:
                            return

            if side == "up" and state.has_up_position:
                return
            if side == "down" and state.has_down_position:
                return

            best_ask = self._get_best_ask_with_rest_fallback(state, side)
            if best_ask <= 0 or best_ask > self.config.max_entry_price or best_ask < self.config.min_entry_price:
                return

            # Spread filter — tight spread = makers committed to price
            best_bid = self._get_best_bid_with_rest_fallback(state, side)
            if best_bid > 0 and (best_ask - best_bid) > self.config.max_spread:
                return

            # TTE check (cheap arithmetic)
            _duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
            tte = state.seconds_to_expiry()
            if tte < (_duration - self.config.max_window_elapsed) or tte > (_duration - self.config.min_window_elapsed):
                return
            if tte < 10:
                return  # Market settling, orders rejected as invalid

            # Edge model check (tick-loop path)
            if self._edge_model and self._edge_model.loaded:
                _elapsed_tl = _duration - tte
                _abs_disp_tl = abs(disp)
                _edge_tl = self._edge_model.get_edge(best_ask, _elapsed_tl, _abs_disp_tl)
                if _edge_tl is None:
                    return
                if _edge_tl < self.config.min_edge_pct:
                    return

            # Book-imbalance confirmation gate (Apr-2026 — tick-loop path).
            _imb_ok, _imb_ratio, _imb_reason = self._check_book_imbalance(state, side)
            if not _imb_ok:
                self._event_logger.info(
                    f"[IMBAL-SKIP] {coin} {side} our_ratio={_imb_ratio:.2%} "
                    f"< min={self.config.imbalance_min_ratio:.0%} — book disagrees (tick-loop)"
                )
                return
            if _imb_reason == "agree":
                self._event_logger.info(
                    f"[IMBAL-OK] {coin} {side} our_ratio={_imb_ratio:.2%} (tick-loop)"
                )

            # Dedup: one signal per market/side in flight
            snipe_key = f"{state.current_slug}:{side}"
            if not hasattr(self, '_snipe_in_flight'):
                self._snipe_in_flight = set()
            if snipe_key in self._snipe_in_flight:
                return
            self._snipe_in_flight.add(snipe_key)

            # Flag for tick loop (fallback if no fast_order)
            if not hasattr(self, '_urgent_coins'):
                self._urgent_coins = set()
            self._urgent_coins.add(coin)

            signal_time = time.time()

            if self._fast_order:
                # Queue to signal thread — position check happens IN the thread
                # (serial execution prevents race conditions)
                _t_queued = time.time()
                def _signal_task():
                    fast_success = False
                    try:
                        _t_start = time.time()
                        _queue_wait = (_t_start - _t_queued) * 1000

                        # Position check INSIDE signal thread (serial, no race)
                        active = sum(1 for s in self.coin_states.values()
                                     if s.has_up_position or s.has_down_position)
                        if active >= self.config.max_concurrent_positions:
                            return

                        # Pure momentum — no fair value model. Entry filters are
                        # momentum + TTE + entry-price (checked by caller).
                        fair_prob = 0.0  # Not used — logged as 0.0

                        buy_price = min(round(best_ask + self.config.fok_tolerance, 2),
                                        self.config.max_entry_price)
                        edge = 0.0  # No edge calc — pure momentum
                        if buy_price < self.config.min_entry_price or self._balance < 1.0:
                            return

                        token_id = state.manager.token_ids.get(side)
                        if not token_id:
                            return

                        # Mark position BEFORE ordering so tick loop won't double-fire
                        if side == "up":
                            state.up_tokens = 0.01  # sentinel
                        else:
                            state.down_tokens = 0.01  # sentinel

                        # FAK execution — try pre-signed first (~23ms) then fall back
                        # to full sign (~270ms). Pre-signed cache is built at market
                        # transition with every $0.01 from min to max entry price.
                        _t_pre_order = time.time()
                        result = None
                        _path = "full-sign"
                        _ladder_size = 0
                        presigned_cache = getattr(self, '_presigned_cache', {})
                        ladder = presigned_cache.get((state.coin, side))
                        if ladder:
                            _ladder_size = len(ladder)
                            price_key = int(buy_price * 100)
                            body_str = ladder.get(price_key)
                            if body_str:
                                result = self._fast_order.post_presigned_order(body_str)
                                if result is not None:
                                    _path = "presigned"

                        # Fall back to full sign if no pre-signed match
                        if result is None:
                            result = self._fast_order._place_order_sync(
                                token_id, buy_price, self.config.min_size_tokens, "BUY", "FAK", 1000,
                            )
                        _t_post_order = time.time()
                        _order_ms = (_t_post_order - _t_pre_order) * 1000
                        _pre_order_ms = (_t_pre_order - _t_start) * 1000
                        # Check BOTH orderID AND error field. The CLOB returns an orderID
                        # even when FAK is killed with "no orders found to match". The error
                        # field is the ground truth — if present, the order did NOT fill.
                        has_error = bool(result.get("error", ""))
                        has_order_id = bool(result.get("orderID", ""))
                        taking = float(result.get("takingAmount", 0) or 0)
                        fast_success = has_order_id and not has_error and taking > 0
                        total_lat = (time.time() - signal_time) * 1000

                        self._event_logger.info(
                            f"[FAST-FIRE] {state.coin} {side} success={fast_success} "
                            f"path={_path} ladder={_ladder_size} "
                            f"id={result.get('orderID','')[:16] or 'none'} "
                            f"filled={taking:.1f} lat={total_lat:.0f}ms "
                            f"qwait={_queue_wait:.0f}ms pre={_pre_order_ms:.0f}ms order={_order_ms:.0f}ms"
                            f"{' ERROR: ' + result.get('error','')[:60] if has_error else ''}"
                        )

                        if not fast_success:
                            # Log rejection details for adverse selection analysis
                            current_ask = state.manager.get_best_ask(side)
                            current_bid = state.manager.get_best_bid(side)
                            spread = current_ask - current_bid if current_bid > 0 else 0
                            self._event_logger.info(
                                f"[FAK-REJECT] {state.coin} {side} bid=${buy_price:.2f} "
                                f"ask_now=${current_ask:.2f} spread=${spread:.2f} "
                                f"mom={abs(disp):.5f} el={300 - tte:.0f}s "
                                f"error={result.get('error','')[:80]} lat={total_lat:.0f}ms"
                            )
                            if side == "up":
                                state.up_tokens = 0
                            else:
                                state.down_tokens = 0
                            return

                        # === BOOKKEEPING: directly in signal thread (no asyncio) ===
                        filled_tokens = taking  # Never default to 5.0 — use actual fill amount
                        actual_cost = buy_price * filled_tokens  # 0 bps taker fee on 5m
                        self._balance -= actual_cost

                        if side == "up":
                            state.up_tokens += filled_tokens
                            state.up_cost += actual_cost
                        else:
                            state.down_tokens += filled_tokens
                            state.down_cost += actual_cost

                        state.last_trade_time = time.time()
                        self.stats.trades += 1
                        self.stats.pending += 1
                        self.stats.total_wagered += actual_cost

                        partial_tag = f" PARTIAL({filled_tokens:.0f}/5)" if filled_tokens < 5 else ""
                        self._event_logger.info(
                            f"SNIPE {state.coin} {side.upper()} @ {buy_price:.2f} "
                            f"x{filled_tokens:.0f} (${actual_cost:.2f}){partial_tag} "
                            f"edge={edge:.2f} [FAST-FIRE] "
                            f"strike={getattr(state, '_strike_source', '?')} lat={total_lat:.0f}ms"
                        )

                        # Write CSV — trade_logger is thread-safe (just file append)
                        # Use signal-time displacement (disp) for momentum_at_entry,
                        # NOT a fresh spot price. The spot can move 300ms between
                        # signal and fill, making the logged momentum misleading.
                        spot = self.binance.get_price(state.coin)
                        vol, vol_src = self._get_volatility(state.coin)
                        self.trade_logger.log_trade(
                            market_slug=state.current_slug,
                            coin=state.coin,
                            timeframe=self.config.timeframe,
                            side=side,
                            entry_price=buy_price,
                            bet_size_usdc=actual_cost,
                            num_tokens=filled_tokens,
                            bankroll=self._balance,
                            usdc_balance=self._balance,
                            btc_price=spot,
                            other_side_price=state.manager.get_mid_price("down" if side == "up" else "up"),
                            volatility_std=vol,
                            fair_value_at_entry=fair_prob,
                            time_to_expiry_at_entry=state.seconds_to_expiry(),
                            momentum_at_entry=disp,
                            volatility_at_entry=vol,
                            signal_to_order_ms=0,
                            order_latency_ms=total_lat,
                            total_latency_ms=total_lat,
                            vol_source=vol_src,
                            strike_source=getattr(state, '_strike_source', 'unknown'),
                        )

                    except Exception as e:
                        self._event_logger.warning(f"[FAST-FIRE] {state.coin} {side} error: {e}")
                        # Reset sentinel if we set it but never filled
                        if side == "up" and state.up_tokens == 0.01:
                            state.up_tokens = 0
                        elif side == "down" and state.down_tokens == 0.01:
                            state.down_tokens = 0
                    finally:
                        # Only discard on success — prevents retry spam
                        if fast_success:
                            self._snipe_in_flight.discard(snipe_key)

                self._fast_order._signal_pool.submit(_signal_task)
            else:
                # No FastOrder — fallback to tick loop
                self._snipe_in_flight.discard(snipe_key)

        if hasattr(self.binance, 'on_price'):
            self.binance.on_price(_on_price_update)
        # For MultiPriceFeed, register on BOTH sub-feeds so ring buffer
        # captures all ticks (primary=Binance for BNB, coinbase=everything else)
        if hasattr(self.binance, '_primary') and hasattr(self.binance._primary, 'on_price'):
            self.binance._primary.on_price(_on_price_update)
        if hasattr(self.binance, '_coinbase') and hasattr(self.binance._coinbase, 'on_price'):
            self.binance._coinbase.on_price(_on_price_update)

        # Initialize EMA trend tracker from historical Binance klines
        if self._ema_tracker:
            for coin in self.config.coins:
                ok = self._ema_tracker.initialize(coin)
                trend = self._ema_tracker.trend_str(coin)
                if ok:
                    self.log(f"{coin} {trend}", "success")
                else:
                    self.log(f"{coin} EMA init failed — will warm up from live strikes", "warning")

        # Check USDC balance
        self._refresh_balance()
        self.log(f"USDC balance: ${self._balance:.2f}", "success")

        # Cancel stale orders from previous session (maker orders that survived a crash)
        try:
            open_orders = await asyncio.to_thread(
                lambda: self.bot._official_client.get_orders() or []
            )
            cancelled_count = 0
            for order in open_orders:
                if order.get('status') in ('LIVE', 'OPEN'):
                    try:
                        await self.bot.cancel_order(order['id'])
                        cancelled_count += 1
                    except Exception:
                        pass
            if cancelled_count > 0:
                self.log(f"Cancelled {cancelled_count} stale orders from previous session", "warning")
        except Exception as e:
            self._event_logger.warning(f"Stale order cleanup failed: {e}")

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

        # Background REST orderbook refresher — keeps prices fresh when WS goes stale.
        # The WS often stops receiving data after market transitions (resubscription failure).
        # This provides a reliable fallback: the signal path checks _rest_book_cache when
        # get_best_ask() returns 1.0 (no WS data).
        self._rest_book_cache: Dict[str, tuple] = {}  # {token_id: (timestamp, best_ask, best_bid)}
        self._rest_cache_lock = threading.Lock()

        def _rest_book_refresh():
            while True:
                try:
                    # Collect all active token IDs
                    all_tids = []
                    for st in list(self.coin_states.values()):
                        if not st.manager or not st.manager.token_ids:
                            continue
                        for side_name in ("up", "down"):
                            tid = st.manager.token_ids.get(side_name, "")
                            if tid:
                                all_tids.append(tid)

                    if not all_tids:
                        time.sleep(3)
                        continue

                    # Batch fetch — 1 API call instead of 14
                    try:
                        books = self.bot.clob_client.get_order_books_batch(all_tids)
                        now = time.time()
                        if isinstance(books, list):
                            for book in books:
                                tid = book.get("asset_id", "") or book.get("token_id", "")
                                asks = book.get("asks", [])
                                bids = book.get("bids", [])
                                ba = min(float(a.get("price", 1)) for a in asks) if asks else 0.0
                                bb = max(float(b.get("price", 0)) for b in bids) if bids else 0.0
                                if tid:
                                    with self._rest_cache_lock:
                                        self._rest_book_cache[tid] = (now, ba, bb)
                    except Exception:
                        # Fallback to individual calls if batch fails
                        for tid in all_tids:
                            try:
                                book = self.bot.clob_client.get_order_book(tid)
                                asks = book.get("asks", [])
                                bids = book.get("bids", [])
                                ba = min(float(a.get("price", 1)) for a in asks) if asks else 0.0
                                bb = max(float(b.get("price", 0)) for b in bids) if bids else 0.0
                                with self._rest_cache_lock:
                                    self._rest_book_cache[tid] = (time.time(), ba, bb)
                            except Exception:
                                pass
                except Exception:
                    pass
                time.sleep(3)

        rest_thread = threading.Thread(target=_rest_book_refresh, daemon=True)
        rest_thread.start()
        self.log("Background REST orderbook refresher started (3s interval)", "info")

        # User WebSocket for real-time fill detection on maker orders.
        # Instant fill awareness vs 5-second CLOB polling.
        if self.config.maker_enabled:
            try:
                # Get API credentials from the bot's derived L2 creds
                creds = getattr(self.bot, '_api_creds', None)
                api_key = creds.api_key if creds else ""
                api_secret = creds.secret if creds else ""
                api_passphrase = creds.passphrase if creds else ""
                if api_key and api_secret and api_passphrase:
                    self._user_ws = UserWebSocket(
                        api_key=api_key,
                        api_secret=api_secret,
                        api_passphrase=api_passphrase,
                    )

                    @self._user_ws.on_trade
                    async def _on_user_trade(event):
                        """Handle real-time fill notifications from User WS."""
                        status = event.get("status", "")
                        if status != "MATCHED":
                            return  # Only act on initial match, not MINED/CONFIRMED

                        # Check if any of our maker orders were filled
                        maker_orders = event.get("maker_orders", [])
                        for mo in maker_orders:
                            oid = mo.get("order_id", "")
                            matched = float(mo.get("matched_amount", 0) or 0)
                            if oid and matched > 0:
                                # Check if this is one of our orders
                                filled_order = self._order_ledger.mark_filled(oid, matched)
                                if filled_order:
                                    self.log(
                                        f"[USER-WS] INSTANT FILL: {filled_order.coin} "
                                        f"{filled_order.side.upper()} {matched} tokens "
                                        f"@ ${filled_order.price:.2f}",
                                        "success"
                                    )
                                    await self._record_maker_fill(filled_order, matched)
                                    if self.config.maker_dual_mode:
                                        await self._cancel_opposite_side(filled_order)

                        # Also check taker_order_id (if our GTD crossed as taker)
                        taker_oid = event.get("taker_order_id", "")
                        if taker_oid:
                            size = float(event.get("size", 0) or 0)
                            if size > 0:
                                filled_order = self._order_ledger.mark_filled(taker_oid, size)
                                if filled_order:
                                    self.log(
                                        f"[USER-WS] INSTANT FILL (taker): {filled_order.coin} "
                                        f"{filled_order.side.upper()} {size} tokens "
                                        f"@ ${filled_order.price:.2f}",
                                        "success"
                                    )
                                    await self._record_maker_fill(filled_order, size)
                                    if self.config.maker_dual_mode:
                                        await self._cancel_opposite_side(filled_order)

                    # Subscribe to all current markets
                    condition_ids = []
                    for state in self.coin_states.values():
                        if state.manager and state.manager.current_market:
                            cid = getattr(state.manager.current_market, 'condition_id', '')
                            if cid:
                                condition_ids.append(cid)

                    # Run User WS in background
                    async def _user_ws_loop():
                        if condition_ids:
                            self._user_ws._subscribed_markets.update(condition_ids)
                        await self._user_ws.run(auto_reconnect=True)

                    asyncio.create_task(_user_ws_loop())
                    self.log(f"User WebSocket started ({len(condition_ids)} markets)", "info")
                else:
                    self.log("User WS skipped — missing CLOB API credentials in env", "warning")
            except Exception as e:
                self.log(f"User WS failed to start: {e}", "warning")

        return True

    def _is_paused(self) -> bool:
        """Check if bot is paused via data/.bot_paused flag file.
        Cached for 2 seconds to avoid hammering the filesystem."""
        now = time.time()
        if now - self._pause_cache_time < 2.0:
            return self._pause_cache_val
        self._pause_cache_time = now
        import os
        flag_path = os.path.join(self._pause_flag_dir, ".bot_paused")
        self._pause_cache_val = os.path.exists(flag_path)
        return self._pause_cache_val

    async def _cancel_all_maker_orders(self, reason: str = "paused"):
        """Cancel ALL open orders on the CLOB with a single API call, then clean up ledger."""
        # Single API call cancels everything — fastest and most reliable
        try:
            result = await self.bot.cancel_all_orders()
            self.log(f"[MAKER] cancel_all_orders: {result.success} ({reason})", "warning")
        except Exception as e:
            self.log(f"[MAKER] cancel_all_orders failed: {e}", "error")

        # Return reserved capital for all live orders in ledger
        live_orders = self._order_ledger.get_live_orders()
        for order in live_orders:
            cancelled = self._order_ledger.mark_cancelled(order.order_id)
            if cancelled:
                with self._maker_lock:
                    self._balance += cancelled.reserved_usdc
                self.log(
                    f"[MAKER] Returned ${cancelled.reserved_usdc:.2f} for {order.coin} {order.side.upper()}",
                    "info"
                )
            self._order_ledger.remove_order(order.order_id)

    def _get_best_ask_with_rest_fallback(self, state: 'CoinMarketState', side: str) -> float:
        """Get best ask from WS orderbook, falling back to REST cache if WS is stale."""
        best_ask = state.manager.get_best_ask(side)
        if best_ask < 1.0 and best_ask > 0:
            return best_ask
        # WS returned 1.0 (no data) — try REST cache
        token_id = state.manager.token_ids.get(side, "")
        if token_id and hasattr(self, '_rest_cache_lock'):
            with self._rest_cache_lock:
                cached = self._rest_book_cache.get(token_id)
            if cached and (time.time() - cached[0]) < 10:
                return cached[1]  # best_ask from REST
        return best_ask  # return 1.0 if no fallback

    def _get_best_bid_with_rest_fallback(self, state: 'CoinMarketState', side: str) -> float:
        """Get best bid from WS orderbook, falling back to REST cache if WS is stale."""
        best_bid = state.manager.get_best_bid(side)
        if best_bid > 0:
            return best_bid
        token_id = state.manager.token_ids.get(side, "")
        if token_id and hasattr(self, '_rest_cache_lock'):
            with self._rest_cache_lock:
                cached = self._rest_book_cache.get(token_id)
            if cached and (time.time() - cached[0]) < 10:
                return cached[2]  # best_bid from REST
        return best_bid

    def _check_book_imbalance(self, state: 'CoinMarketState', side: str):
        """Check Polymarket bid-depth imbalance as a confirmatory gate before fire.

        Returns (ok: bool, our_ratio: float, reason: str).
        When imbalance_min_ratio is 0 or book data is missing, always returns ok=True.
        When set, fail iff our_side's bid depth is less than min_ratio of total bid depth
        across both sides — i.e. the book disagrees with our momentum direction.

        Rationale: informed participants accumulate bid depth on the side they expect
        to win. Our momentum signal + book agreement = stronger confirmation.
        """
        min_ratio = self.config.imbalance_min_ratio
        if min_ratio <= 0:
            return True, 0.0, "disabled"
        up_ob = state.manager.get_orderbook('up') if state.manager else None
        down_ob = state.manager.get_orderbook('down') if state.manager else None
        if not up_ob or not down_ob or not up_ob.bids or not down_ob.bids:
            return True, 0.0, "no_book"
        up_depth = sum(lvl.size for lvl in up_ob.bids)
        down_depth = sum(lvl.size for lvl in down_ob.bids)
        total = up_depth + down_depth
        if total < 10:
            return True, 0.0, "thin_book"  # Don't block on under-populated books
        our_depth = up_depth if side == "up" else down_depth
        our_ratio = our_depth / total
        if our_ratio >= min_ratio:
            return True, our_ratio, "agree"
        return False, our_ratio, "disagree"

    def _register_callbacks(self, coin: str, manager: MarketManager):
        """Register market change callbacks for a coin."""
        @manager.on_market_change
        def on_change(old_slug: str, new_slug: str, _coin=coin):
            self._handle_market_change(_coin, old_slug, new_slug)

        # VPIN: feed trade events to the tracker
        if self._vpin_tracker:
            @manager.on_trade_event
            def on_trade(trade, _coin=coin):
                try:
                    self._vpin_tracker.on_trade(
                        token_id=trade.asset_id,
                        size=float(trade.size),
                        side=trade.side,
                    )
                except Exception:
                    pass

        # Shadow queue model: feed SELL trade events (= counterparty hitting our
        # side of the book) so shadow can decrement queue_ahead for resting orders.
        if self._shadow_maker:
            @manager.on_trade_event
            def on_trade_shadow(trade, _coin=coin, _manager=manager):
                try:
                    # Only SELL trades affect our bid-side queue
                    if getattr(trade, 'side', '') != 'SELL':
                        return
                    current_mkt = _manager.current_market
                    if not current_mkt:
                        return
                    # Map asset_id → outcome side (up/down) via token_ids
                    trade_side = None
                    for s in ('up', 'down'):
                        if current_mkt.token_ids.get(s) == trade.asset_id:
                            trade_side = s
                            break
                    if not trade_side:
                        return
                    self._shadow_maker.on_trade(
                        slug=_manager.current_slug or '',
                        coin=_coin,
                        traded_outcome_side=trade_side,
                        trade_price=float(trade.price),
                        trade_size=float(trade.size),
                    )
                except Exception:
                    pass

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

        # Subscribe UserWS to new market for real-time fill detection
        # Always queue — UserWS might not exist yet during startup but will be
        # created shortly after. The tick loop processes the queue when WS is ready.
        cid = getattr(market, 'condition_id', '')
        if cid:
            self._pending_ws_subs.append(cid)
            self._event_logger.info(f"[USER-WS] Queued {state.coin} {cid[:16]}... (pending={len(self._pending_ws_subs)})")

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

        # Strike source: EXACT MATCH to collector logic (apps/run_collector.py).
        # Collector uses: 1) ring buffer <3s, 2) spot if <5s, 3) spot always.
        # Bot MUST match this exactly or strikes diverge.

        # 1. PRICE RING BUFFER — find the tick closest to window open (t+0)
        #    No delta threshold — always use nearest tick. Both bot and collector
        #    receive the same Coinbase ticks, so nearest-to-t+0 gives identical
        #    strikes regardless of when _set_strike fires. This eliminates the
        #    DOGE/HYPE divergence caused by falling through to time-dependent spot.
        if market_start_ts > 0 and state.coin in self._price_ring:
            ring = self._price_ring[state.coin]
            if ring:
                best_price = None
                best_delta = float('inf')
                for ts, p in ring:
                    delta = abs(ts - market_start_ts)
                    if delta < best_delta:
                        best_delta = delta
                        best_price = p
                if best_price is not None:
                    state.strike_price = best_price
                    strike_source = "coinbase" if state.coin in COINBASE_COINS else "binance"
                    self.log(f"{state.coin} strike from ring buffer (delta={best_delta:.1f}s)")

        # 2. Current spot — fallback only if ring buffer is completely empty
        if strike_source == "unknown":
            state.strike_price = spot
            _feed = "coinbase" if state.coin in COINBASE_COINS else "binance"
            strike_source = _feed
            self.log(f"{state.coin} strike from spot fallback (window {secs_since_window_open:.0f}s old)")

        # Store strike source for trade logging
        state._strike_source = strike_source

        # Update EMA tracker with new strike (once per market window)
        if self._ema_tracker and state.strike_price > 0 and strike_source != "skipped":
            self._ema_tracker.update(state.coin, state.strike_price, state.current_slug)

        # Format strike with appropriate precision
        if state.strike_price < 10:
            strike_str = f"${state.strike_price:,.4f}"
        elif state.strike_price < 1000:
            strike_str = f"${state.strike_price:,.2f}"
        else:
            strike_str = f"${state.strike_price:,.0f}"
        ema_str = f" | {self._ema_tracker.trend_str(state.coin)}" if self._ema_tracker else ""
        self.log(
            f"{state.coin} strike: {strike_str} [{strike_source}] | "
            f"expiry: {state.seconds_to_expiry():.0f}s{ema_str}"
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

    def _calculate_fair_value(self, state: CoinMarketState):
        """Fair value REMOVED — returns passthrough so trade flow is not blocked.

        Returns a simple namespace with fair_up/fair_down = 1.0 so that edge
        calculations degenerate to (1.0 - ask - fee), which is always positive.
        Actual trade filtering is done by momentum + TTE + entry-price only.
        """
        class _Passthrough:
            fair_up = 1.0
            fair_down = 1.0
        return _Passthrough()

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

    def _find_opportunities(self) -> List[Tuple[CoinMarketState, str, float, float, object]]:
        """
        Scan all coins for trading opportunities.

        No artificial limits — if there's edge, it shows up here.
        Kelly handles position sizing. Volume is how we make money.

        Returns list of (state, side, entry_price, edge, fair_value)
        sorted by edge descending (best opportunity first).
        """
        opportunities = []

        # GTD-only mode: skip find_opportunities entirely (maker handles placement)
        if self.config.maker_enabled:
            return []

        # Circuit breaker: stop trading after N consecutive losses
        if self._circuit_breaker_tripped:
            return []

        # Enhanced circuit breaker checks
        if self.config.enable_circuit_breaker:
            if self._cb_stopped:
                return []
            if self._cb_paused_until:
                now = datetime.now(timezone.utc)
                if now < self._cb_paused_until:
                    return []
                else:
                    self.log("Circuit breaker pause expired — resuming trading", "warning")
                    self._cb_paused_until = None

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

            # Retry strike if not set (market may have been discovered late)
            if state.strike_price <= 0:
                self._set_strike(state)
                if state.strike_price <= 0:
                    continue  # Still no strike — skip this cycle

            # Late/max entry filter using REAL market time (not discovery time).
            # Bug fix: previously used time.time() - market_start_time which drifts
            # when market is discovered late, causing entries before min TTE.
            _min_window = self.config.min_window_elapsed
            _max_window = self.config.max_window_elapsed
            if _min_window > 0 or _max_window > 0:
                tte = state.seconds_to_expiry()
                duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                if _min_window > 0:
                    max_tte = duration - _min_window  # 300-120=180
                    if tte > max_tte:
                        continue
                if _max_window > 0:
                    min_tte = duration - _max_window  # 300-180=120
                    if tte < min_tte:
                        continue

            # Volatility filter: only trade in low-vol regimes.
            # Data: vol < 0.50 -> 35.8% WR vs 22.4% when vol > 0.50.
            if self.config.max_volatility > 0:
                current_vol, _ = self._get_volatility(state.coin)
                if current_vol > self.config.max_volatility:
                    continue

            # Integrated collector runs in _on_price_update callback (every tick).
            # Trading decisions run here in the tick-loop (every 10s).

            # No fair value model — pure momentum. Passthrough so downstream
            # code that references fv.fair_up/fv.fair_down still works.
            fv = self._calculate_fair_value(state)

            # Check both sides — one entry per side per market.
            # Buying BTC UP 5 times at $0.57 in the same market is the
            # same trade repeated, not 5 different opportunities.
            # Determine which sides to check based on side_filter
            sides_to_check = ["up", "down"]
            if self.config.side_filter == "up":
                sides_to_check = ["up"]
            elif self.config.side_filter == "down":
                sides_to_check = ["down"]
            elif self.config.side_filter == "trend" and self._ema_tracker:
                if not self._ema_tracker.is_valid(coin):
                    continue  # EMA not warmed up yet — skip this coin
                if self._ema_tracker.is_bullish(coin):
                    sides_to_check = ["up"]
                else:
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

                # No fair value model — pure momentum strategy
                fair_prob = 0.0  # Logged as 0 — not used for filtering

                # Get best ask (cheapest price we can buy at)
                best_ask = self._get_best_ask_with_rest_fallback(state, side)
                if best_ask <= 0 or best_ask >= 1.0:
                    continue

                buy_price = round(best_ask, 2)
                edge = 0.0  # No edge calc — pure momentum

                # --- Signal logging: evaluate all filters and log before filtering ---
                if self.signal_logger:
                    spot_for_signal = self.binance.get_price(state.coin)
                    vol_for_signal, vol_src_for_signal = self._get_volatility(state.coin)
                    strike_src_for_signal = getattr(state, '_strike_source', 'unknown')

                    # Evaluate each filter independently (per-coin aware)
                    _c_max_entry = self.config.max_entry_price
                    _c_min_entry = self.config.min_entry_price
                    _c_min_edge = self.config.min_edge
                    _c_min_fv = 0.0  # FV filter disabled — pure momentum
                    _c_require_vatic = self.config.require_vatic
                    _c_min_mom = self.config.min_momentum
                    _passed_price = (
                        best_ask <= _c_max_entry
                        and best_ask >= _c_min_entry
                    )
                    _passed_edge = edge >= _c_min_edge
                    _passed_fv = fair_prob >= _c_min_fv
                    _passed_vatic = (
                        state.strike_price > 0 if _c_require_vatic else True
                    )

                    # Momentum filter evaluation
                    _passed_momentum = True
                    _displacement = 0.0
                    if _c_min_mom > 0 and state.strike_price > 0:
                        _displacement = (spot_for_signal - state.strike_price) / state.strike_price
                        if side == "up" and _displacement < _c_min_mom:
                            _passed_momentum = False
                        if side == "down" and _displacement > -_c_min_mom:
                            _passed_momentum = False
                    elif state.strike_price > 0:
                        _displacement = (spot_for_signal - state.strike_price) / state.strike_price

                    # TTE filter evaluation
                    _passed_tte = True
                    if _min_window > 0 or _max_window > 0:
                        _tte = state.seconds_to_expiry()
                        _duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                        if _min_window > 0:
                            if _tte > (_duration - _min_window):
                                _passed_tte = False
                        if _max_window > 0:
                            if _tte < (_duration - _max_window):
                                _passed_tte = False

                    # Trend filter evaluation
                    _passed_trend = True
                    _ema_trend = "none"
                    if self.config.side_filter == "trend" and self._ema_tracker:
                        if self._ema_tracker.is_valid(coin):
                            if self._ema_tracker.is_bullish(coin):
                                _ema_trend = "bullish"
                                if side == "down":
                                    _passed_trend = False
                            else:
                                _ema_trend = "bearish"
                                if side == "up":
                                    _passed_trend = False
                        else:
                            _passed_trend = False

                    signal = SignalRecord(
                        timestamp=datetime.now(timezone.utc).isoformat(),
                        coin=coin,
                        market_slug=state.current_slug,
                        side=side,
                        spot_price=spot_for_signal,
                        strike_price=state.strike_price,
                        strike_source=strike_src_for_signal,
                        best_ask=best_ask,
                        edge=edge,
                        momentum=_displacement,
                        volatility=vol_for_signal,
                        vol_source=vol_src_for_signal,
                        time_to_expiry=state.seconds_to_expiry(),
                        ema_trend=_ema_trend,
                        entry_price=buy_price,
                        passed_edge_filter=_passed_edge,
                        passed_price_filter=_passed_price,
                        passed_momentum_filter=_passed_momentum,
                        passed_tte_filter=_passed_tte,
                        passed_trend_filter=_passed_trend,
                    )
                    self.signal_logger.log_signal(signal)

                # --- Filter chain (global config, no per-coin overrides) ---

                coin_max_entry = self.config.max_entry_price
                coin_min_entry = self.config.min_entry_price
                coin_min_mom = self.config.min_momentum
                coin_require_vatic = self.config.require_vatic

                # Structural price filters only (not risk limits)
                if best_ask > coin_max_entry:
                    continue
                if best_ask < coin_min_entry:
                    continue

                # Spread filter — tight spread = makers committed to price
                _tick_bid = self._get_best_bid_with_rest_fallback(state, side)
                if _tick_bid > 0 and (best_ask - _tick_bid) > self.config.max_spread:
                    continue

                # Never buy the opposite side of an existing position.
                if side == "up" and state.has_down_position:
                    continue
                if side == "down" and state.has_up_position:
                    continue

                # Pure momentum entry — no edge or FV gating
                if True:
                    # Vatic requirement (per-coin)
                    if coin_require_vatic and state.strike_price <= 0:
                        continue

                    # Momentum filter: price must have displaced from strike
                    # in the direction we're betting. This matches the backtest
                    # logic exactly: mom = (spot - strike) / strike.
                    # DO NOT use binance.get_momentum() — that measures 30s
                    # price change, which is a different (weaker) signal.
                    if coin_min_mom > 0 and state.strike_price > 0:
                        spot = self.binance.get_price(state.coin)
                        displacement = (spot - state.strike_price) / state.strike_price
                        if side == "up" and displacement < coin_min_mom:
                            continue  # Price below strike — don't buy Up
                        if side == "down" and displacement > -coin_min_mom:
                            continue  # Price above strike — don't buy Down

                        # INTEGRATED COLLECTOR — record at the EXACT trading
                        # decision point. Uses a SEPARATE dedup key ("trade:")
                        # so callback recordings can't block this. Guarantees:
                        # if the bot trades it, the collector records it.
                        _abs_disp = abs(displacement)
                        _mom_dir = "up" if displacement > 0 else "down"
                        _opp_dir = "down" if _mom_dir == "up" else "up"
                        _tte = state.seconds_to_expiry()
                        _duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                        _elapsed = max(0, _duration - _tte)
                        for _cs in [_mom_dir, _opp_dir]:
                            # Use "trade:" prefix to avoid collision with callback dedup
                            _ck = ("trade:" + state.current_slug, _cs)
                            if _ck not in self._collector_crossed:
                                self._collector_crossed[_ck] = set()
                            for _ct in self._collector_thresholds:
                                if _abs_disp >= _ct and _ct not in self._collector_crossed[_ck]:
                                    self._collector_crossed[_ck].add(_ct)
                                    _ca = self._get_best_ask_with_rest_fallback(state, _cs)
                                    _cb = self._get_best_bid_with_rest_fallback(state, _cs)
                                    self._collector_pending.append({
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                        "slug": state.current_slug,
                                        "coin": state.coin,
                                        "side": _cs,
                                        "momentum_direction": _mom_dir,
                                        "is_momentum_side": _cs == _mom_dir,
                                        "threshold": _ct,
                                        "entry_price": round(_ca, 4) if _ca < 1.0 else 0.0,
                                        "best_bid": round(_cb, 4),
                                        "momentum": round(_abs_disp, 8),
                                        "elapsed": round(_elapsed, 1),
                                        "_created": time.time(),
                                    })

                    # Edge model check (tick-loop evaluate path)
                    if self._edge_model and self._edge_model.loaded:
                        _edge_ev = self._edge_model.get_edge(best_ask, _elapsed, _abs_disp)
                        if _edge_ev is None:
                            self._event_logger.info(
                                f"[EDGE-SKIP] {state.coin} {side} ep={best_ask:.2f} el={_elapsed:.0f}s "
                                f"mom={_abs_disp:.5f} — no IC data for bucket"
                            )
                            continue
                        if _edge_ev < self.config.min_edge_pct:
                            _detail_ev = self._edge_model.get_detail(best_ask, _elapsed, _abs_disp)
                            self._event_logger.info(
                                f"[EDGE-BLOCK] {state.coin} {side} ep={best_ask:.2f} el={_elapsed:.0f}s "
                                f"mom={_abs_disp:.5f} ic_wr={_detail_ev['ic_wr']:.1%} "
                                f"live_wr={_detail_ev['est_live_wr']:.1%} "
                                f"edge={_edge_ev:+.1%} < min={self.config.min_edge_pct:.1%}"
                            )
                            continue

                    # VPIN data collection (log alongside signal, no filtering)
                    _vpin_val = None
                    _vpin_flow = None
                    if self._vpin_tracker:
                        _vpin_val = self._vpin_tracker.get_vpin_by_coin_side(state.coin, side)
                        _vpin_flow = self._vpin_tracker.get_flow_by_coin_side(state.coin, side)

                    # Shadow log: record paper entry for every qualifying signal
                    _vpin_str = f" vpin={_vpin_val:.2f}({'+' if _vpin_flow and _vpin_flow>0 else '-'}{abs(_vpin_flow or 0):.2f})" if _vpin_val is not None else ""
                    self.log(f"[SHADOW] Signal passed all filters: {state.coin} {side} @ ${best_ask:.4f} edge={edge:.4f}{_vpin_str} shadow={'ON' if self.shadow_logger else 'OFF'}", "info")
                    if self.shadow_logger:
                        _spot = self.binance.get_price(state.coin)
                        _mom = (_spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0.0
                        _strike_src = getattr(state, '_strike_source', 'unknown')
                        self.shadow_logger.log_signal(
                            market_slug=state.current_slug,
                            coin=state.coin,
                            side=side,
                            ask_price=best_ask,
                            edge=edge,
                            momentum=_mom,
                            tte=state.seconds_to_expiry(),
                            strike_source=_strike_src,
                        )

                    # DEPTH FILTER: only trade thin books where HFT doesn't compete
                    # Thick books (100+ tokens) get scooped by pre-placed HFT orders.
                    # Thin books (<50 tokens) are ignored by HFT = less adverse selection.
                    ob = state.manager.get_orderbook(side)
                    book_depth = ob.asks[0].size if ob and ob.asks else 0
                    # Depth filter DISABLED — collecting data on all book depths
                    # Log depth for analysis but don't filter
                    if book_depth > 0:
                        pass  # no filter

                    opportunities.append((state, side, best_ask, edge, fv))

        # Sort by edge, best first
        opportunities.sort(key=lambda x: x[3], reverse=True)
        return opportunities

    async def _handle_fast_fire_result(
        self, state, side, buy_price, edge, fv, result, signal_time,
    ):
        """Handle bookkeeping after a fast-fire order (runs in asyncio)."""
        fast_success = result.get("success", False) or result.get("orderID", "")
        fast_order_id = result.get("orderID", "")
        taking = float(result.get("takingAmount", 0) or 0)

        self.log(
            f"[FAST-FIRE] {state.coin} {side.upper()} success={bool(fast_success)} "
            f"id={fast_order_id[:16] if fast_order_id else 'none'} "
            f"filled={taking:.1f} lat={((time.time() - signal_time) * 1000):.0f}ms",
            "info"
        )

        if not fast_success:
            return

        # Track position
        fair_prob = 0.0  # No FV model — pure momentum
        filled_tokens = taking if taking > 0 else 5.0
        fee_per_tok = buy_price * 0.01  # ~1% taker fee estimate
        actual_cost = (buy_price + fee_per_tok) * filled_tokens
        self._balance -= actual_cost

        if side == "up":
            state.up_tokens += filled_tokens
            state.up_cost += actual_cost
        else:
            state.down_tokens += filled_tokens
            state.down_cost += actual_cost

        state.last_trade_time = time.time()
        self.stats.trades += 1
        self.stats.pending += 1
        self.stats.total_wagered += actual_cost
        self.stats.opportunities_found += 1

        total_lat = (time.time() - signal_time) * 1000
        partial_tag = f" PARTIAL({filled_tokens:.0f}/5)" if filled_tokens < 5 else ""
        self.log(
            f"SNIPE {state.coin} {side.upper()} @ {buy_price:.2f} x{filled_tokens:.0f} "
            f"(${actual_cost:.2f}){partial_tag} edge={edge:.2f} [FAST-FIRE] "
            f"strike={getattr(state, '_strike_source', '?')} lat={total_lat:.0f}ms",
            "info"
        )

        # Log trade
        spot = self.binance.get_price(state.coin)
        vol, vol_src = self._get_volatility(state.coin)
        strike_src = getattr(state, '_strike_source', 'unknown')
        self.trade_logger.log_trade(
            market_slug=state.current_slug,
            coin=state.coin,
            timeframe=self.config.timeframe,
            side=side,
            entry_price=buy_price,
            bet_size_usdc=actual_cost,
            num_tokens=filled_tokens,
            bankroll=self._balance,
            usdc_balance=self._balance,
            btc_price=spot,
            other_side_price=state.manager.get_mid_price("down" if side == "up" else "up"),
            volatility_std=vol,
            fair_value_at_entry=fair_prob,
            time_to_expiry_at_entry=state.seconds_to_expiry(),
            momentum_at_entry=(spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0,
            volatility_at_entry=vol,
            signal_to_order_ms=0,
            order_latency_ms=total_lat,
            total_latency_ms=total_lat,
            vol_source=vol_src,
            strike_source=strike_src,
        )

    async def _execute_snipe(
        self,
        state: CoinMarketState,
        side: str,
        entry_price: float,
        edge: float,
        fv: object,
        signal_time: float = 0.0,
    ) -> bool:
        """Execute a snipe trade."""
        # Prevent double execution from callback + tick loop
        snipe_key = f"{state.current_slug}:{side}"
        if not hasattr(self, '_snipe_in_flight'):
            self._snipe_in_flight = set()
        if snipe_key in self._snipe_in_flight:
            return False
        self._snipe_in_flight.add(snipe_key)
        try:
            return await self._execute_snipe_inner(state, side, entry_price, edge, fv, signal_time)
        finally:
            self._snipe_in_flight.discard(snipe_key)

    async def _execute_snipe_inner(
        self,
        state: CoinMarketState,
        side: str,
        entry_price: float,
        edge: float,
        fv: object,
        signal_time: float = 0.0,
    ) -> bool:
        """Inner snipe execution (guarded by _execute_snipe)."""
        if self.config.observe_only:
            # Paper trading with realistic simulation
            fair_prob = 0.0  # No FV model — pure momentum
            num_tokens = self.config.min_size_tokens
            buy_price = round(entry_price, 2)

            # FOK rejection simulation: log depth but don't skip.
            # Paper should capture all signals regardless of book state
            # so we get an unbiased view of signal quality.
            ob = state.manager.get_orderbook(side)
            if ob and ob.asks:
                best_ask_size = ob.asks[0].size
                if best_ask_size < num_tokens:
                    self.log(
                        f"[PAPER] {state.coin} {side.upper()} @ {buy_price:.2f} "
                        f"thin book (depth={best_ask_size:.0f} < {num_tokens:.0f}), logging anyway",
                        "info"
                    )

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
                momentum_at_entry=_pre_order_momentum,
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
        fair_prob = 0.0  # No FV model — pure momentum
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
            num_tokens = self.config.min_size_tokens
            bet_usdc = num_tokens * entry_price
            # Polymarket rejects orders below $1.00 USDC
            if bet_usdc < 1.0:
                return False
            if bet_usdc > self._available_balance():
                return False
            # Balance floor: preserve capital
            if self.config.balance_floor > 0 and self._available_balance() - bet_usdc < self.config.balance_floor:
                self.log(f"BALANCE FLOOR: ${self._available_balance():.2f} - ${bet_usdc:.2f} would drop below ${self.config.balance_floor:.2f} floor. Skipping.", "warning")
                return False

        if num_tokens < self.config.min_size_tokens:
            # Not enough for Polymarket minimum order size
            return False

        # Get token ID
        token_id = state.manager.token_ids.get(side)
        if not token_id:
            return False

        # Capture momentum NOW (before order placement delay)
        _pre_order_spot = self.binance.get_price(state.coin)
        _pre_order_momentum = (_pre_order_spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0

        # Place FOK (Fill Or Kill) order with price tolerance to reduce rejections.
        # The tolerance is applied to the submission price (not the edge calc).
        # Shadow logger captures original ask as paper entry price.
        original_ask = round(entry_price, 2)
        tolerance = self.config.fok_tolerance
        buy_price = round(original_ask + tolerance, 2) if tolerance > 0 else original_ask

        # Don't exceed max entry price with tolerance
        if buy_price > self.config.max_entry_price:
            buy_price = self.config.max_entry_price

        # Depth filter: only trade thin books where HFT doesn't pre-place orders.
        # Data: <10 tokens at best ask = 70% WR, 10+ tokens = 44% WR.
        ob = state.manager.get_orderbook(side)
        best_ask_depth = 0
        if ob and ob.asks:
            best_ask_depth = ob.asks[0].size
            available_tokens = 0.0
            for level in ob.asks:
                if level.price <= buy_price:
                    available_tokens += level.size
                else:
                    break
            if available_tokens > 0 and available_tokens < num_tokens:
                depth_capped = float(int(min(num_tokens, available_tokens)))
                if depth_capped >= 1.0:
                    self.log(
                        f"[DEPTH] {state.coin} {side.upper()} depth={available_tokens:.0f} "
                        f"<= ${buy_price:.2f}, sizing {num_tokens:.0f} -> {depth_capped:.0f}",
                        "info"
                    )
                    num_tokens = depth_capped

        # Log depth for data collection (no filter — fight head-on with speed)
        self.log(
            f"[BOOK] {state.coin} {side.upper()} depth={best_ask_depth:.0f} @ ${original_ask:.2f}",
            "info"
        )

        order_start = time.time()
        signal_to_order_ms = (order_start - signal_time) * 1000 if signal_time else 0

        # Log orderbook state before attempting fill (rejection diagnostics)
        ob_snapshot = ""
        if ob and ob.asks:
            top_levels = [(a.price, a.size) for a in ob.asks[:5]]
            ob_snapshot = " | ".join(f"${p:.2f}x{s:.0f}" for p, s in top_levels)
            self.log(
                f"[FILL] {state.coin} {side.upper()} submitting FAK @ ${buy_price:.2f} "
                f"(ask=${original_ask:.2f} +{tolerance:.2f}tol) book=[{ob_snapshot}]",
                "info"
            )

        # FAK TAKER EXECUTION — direct, fast, no resting orders.
        # Uses FastOrderClient to bypass SDK overhead (~100-300ms → ~10-20ms signing).
        # FAK fills or dies instantly. No ambiguity, no ghosts.
        actual_fill = num_tokens  # default, updated on partial fills

        self.log(
            f"[FILL] {state.coin} {side.upper()} FAK @ ${buy_price:.2f} "
            f"(ask=${original_ask:.2f} +{tolerance:.2f}tol)",
            "info"
        )

        # ORDER EXECUTION: route through signal queue (non-blocking for event loop)
        fast_success = False
        fast_result = {}
        if self._fast_order:
            try:
                fast_result = await self._fast_order.place_order(
                    token_id=token_id, price=buy_price,
                    size=num_tokens, side="BUY", order_type="FAK",
                )
                fast_order_id = fast_result.get("orderID", "")
                fast_error = fast_result.get("error", "")
                fast_taking = float(fast_result.get("takingAmount", 0) or 0)
                fast_success = bool(fast_order_id) and not bool(fast_error) and fast_taking > 0
                self.log(
                    f"[FAST] {state.coin} {side.upper()} success={fast_success} "
                    f"id={fast_order_id[:16] if fast_order_id else 'none'} "
                    f"filled={fast_taking:.1f} "
                    f"{'ERROR: ' + fast_error[:60] if fast_error else ''}",
                    "info"
                )
                if fast_success and fast_order_id:
                    from src.bot import OrderResult
                    result = OrderResult(
                        success=True, order_id=fast_order_id,
                        status=fast_result.get("status", ""),
                        message="Fast order filled",
                        data=fast_result,
                    )
            except Exception as e:
                self.log(f"[FAST] {state.coin} {side.upper()} EXCEPTION: {e}", "warning")

        # SDK fallback — only if FastOrder not available or failed
        if not fast_success:
            result = await self.bot.place_order(
                token_id=token_id, price=buy_price,
                size=num_tokens, side="BUY", order_type="FAK",
            )
            sdk_success = result.success
            self.log(
                f"[SDK] {state.coin} {side.upper()} success={sdk_success} "
                f"id={result.order_id[:16] if result.order_id else 'none'}",
                "info"
            )

        # For SDK orders: wait for background FAK verify to populate fill_amount
        # For FastOrder: fill amount is in the response, no need to wait
        if result.success and not result.fill_amount and not fast_success:
            await asyncio.sleep(0.4)
        # Extract actual fill from FastOrder response (takingAmount field)
        if fast_success and fast_result:
            try:
                taking = float(fast_result.get("takingAmount", 0))
                if taking > 0:
                    result.fill_amount = taking
            except (ValueError, TypeError):
                pass
        actual_fill = result.fill_amount if result.fill_amount else num_tokens
        self.log(
            f"[ORDER] {state.coin} {side.upper()} FAK @ ${buy_price:.2f}: "
            f"success={result.success} filled={actual_fill:.1f}/{num_tokens:.0f}",
            "info"
        )

        # GTC fallback REMOVED — caused ghost fills on every session.
        # GTC orders fill after balance/CLOB checks, creating untracked positions.
        # GTD maker + FAK taker is the complete execution chain. No resting GTC.

        order_end = time.time()
        order_latency_ms = (order_end - order_start) * 1000
        total_latency_ms = (order_end - signal_time) * 1000 if signal_time else 0

        # Log rejection diagnostics — understand WHY fills fail
        if not result.success:
            max_price_tried = round(original_ask + tolerance, 2)  # FAK max (no GTC)
            ob_now = state.manager.get_orderbook(side)
            if ob_now and ob_now.asks:
                current_best = ob_now.asks[0].price
                current_depth = sum(a.size for a in ob_now.asks[:3])
                if current_best > max_price_tried:
                    reason = f"PRICE_MOVED (best now ${current_best:.2f}, max tried ${max_price_tried:.2f})"
                elif current_depth < 2:
                    reason = f"NO_DEPTH (only {current_depth:.0f} tokens in top 3 levels)"
                else:
                    reason = f"SCOOPED (book has ${current_best:.2f}x{current_depth:.0f} but fill failed — someone faster)"
            else:
                reason = "NO_BOOK (orderbook empty)"
            self.log(
                f"[REJECT] {state.coin} {side.upper()} ALL attempts failed: {reason} "
                f"(tried ${original_ask:.2f} to ${min(max_price_tried, self.config.max_entry_price):.2f}, "
                f"{order_latency_ms:.0f}ms total)",
                "warning"
            )

        # Track position if order was accepted AND filled.
        # Some fills return status "LIVE"/"OPEN" even when matched — the status
        # field is unreliable. Use balance change as ground truth for ambiguous cases.
        order_status = (result.status or "").upper()
        is_confirmed_fill = False
        if result.success:
            if order_status not in ("LIVE", "OPEN"):
                is_confirmed_fill = True
            elif hasattr(result, '_gtc_confirmed') and result._gtc_confirmed:
                is_confirmed_fill = True
            else:
                # Ambiguous status — check balance to confirm
                await asyncio.sleep(2)
                bal_check = await asyncio.to_thread(self.bot.get_usdc_balance)
                expected_cost = buy_price * num_tokens
                if bal_check is not None and bal_check < (self._balance - expected_cost * 0.3):
                    self.log(
                        f"[FILL RESCUE] {state.coin} {side.upper()} status={order_status} "
                        f"but balance dropped ${self._balance:.2f} -> ${bal_check:.2f} — FILLED",
                        "warning"
                    )
                    is_confirmed_fill = True
                else:
                    self.log(
                        f"[FILL CHECK] {state.coin} {side.upper()} status={order_status} "
                        f"balance unchanged (${bal_check:.2f}) — NOT filled",
                        "info"
                    )
        if is_confirmed_fill:
            # FAK partial fills: use actual fill amount from bg verify
            filled_tokens = actual_fill
            # Guard: FAK can "match" with 0 tokens on empty books
            if filled_tokens <= 0:
                self.log(
                    f"[SKIP] {state.coin} {side.upper()} FAK matched but 0 tokens filled — ignoring",
                    "warning"
                )
                return False
            fee_per_token = taker_fee_per_token(buy_price, self.config.timeframe)
            actual_cost = (buy_price + fee_per_token) * filled_tokens

            # Immediately deduct from balance so next Kelly calculation
            # sees the correct available capital (no stale balance)
            self._balance -= actual_cost

            # Record position
            if side == "up":
                state.up_tokens += filled_tokens
                state.up_cost += actual_cost
            else:
                state.down_tokens += filled_tokens
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
            real_balance = self._balance  # Use cached balance, don't block event loop

            strike_src = getattr(state, '_strike_source', 'unknown')
            self.trade_logger.log_trade(
                market_slug=state.current_slug,
                coin=state.coin,
                timeframe=self.config.timeframe,
                side=side,
                entry_price=buy_price,
                bet_size_usdc=actual_cost,
                num_tokens=filled_tokens,
                bankroll=self._balance,
                usdc_balance=real_balance,
                btc_price=spot,
                other_side_price=other_price,
                volatility_std=vol,
                fair_value_at_entry=fair_prob,
                time_to_expiry_at_entry=state.seconds_to_expiry(),
                momentum_at_entry=_pre_order_momentum,
                volatility_at_entry=vol,
                signal_to_order_ms=signal_to_order_ms,
                order_latency_ms=order_latency_ms,
                total_latency_ms=total_latency_ms,
                vol_source=vol_src,
                strike_source=strike_src,
            )

            # Shadow log: record live fill
            if self.shadow_logger:
                self.shadow_logger.log_live_fill(
                    market_slug=state.current_slug,
                    coin=state.coin,
                    side=side,
                    fill_price=buy_price,
                    latency_ms=total_latency_ms,
                )

            kelly_pct = (actual_cost / (self._available_balance() + actual_cost) * 100) if (self._available_balance() + actual_cost) > 0 else 0
            strength = "STRONG" if is_strong else "NORMAL"

            partial_tag = f" PARTIAL({filled_tokens:.0f}/{num_tokens:.0f})" if filled_tokens < num_tokens else ""
            self.log(
                f"SNIPE {state.coin} {side.upper()} @ {buy_price:.2f} "
                f"x{filled_tokens:.0f} (${actual_cost:.2f}){partial_tag} "
                f"edge={edge:.2f} [{strength}] kelly={kelly_pct:.0f}% "
                f"strike={strike_src} lat={total_latency_ms:.0f}ms",
                "success"
            )
            return True
        elif result.success and not is_confirmed_fill:
            # Order was accepted but balance check confirms NOT filled
            if self.shadow_logger:
                self.shadow_logger.log_fok_reject(state.current_slug, state.coin, side)
            self.log(
                f"Order NOT FILLED ({state.coin} {side} @ {buy_price:.2f}) "
                f"status={result.status} — balance confirmed no fill",
                "warning"
            )
            state.last_fail_time[side] = time.time()
            return False
        else:
            # Shadow log: order failed (FOK reject or other error)
            if self.shadow_logger:
                self.shadow_logger.log_fok_reject(state.current_slug, state.coin, side)
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

                if up_price > 0.9 and down_price > 0.9:
                    winner = "up" if up_price > down_price else "down"
                    self.log(f"{state.coin} Gamma API: {winner.upper()} won (up={up_price:.2f} down={down_price:.2f}) [both>0.9]")
                    return winner
                elif up_price > 0.9:
                    self.log(f"{state.coin} Gamma API: UP won (up={up_price:.2f})")
                    return "up"
                elif down_price > 0.9:
                    self.log(f"{state.coin} Gamma API: DOWN won (down={down_price:.2f})")
                    return "down"
        except Exception as e:
            self.log(f"{state.coin} Gamma API check failed: {e}", "warning")

        self.log(f"{state.coin} {old_slug} not yet resolved — will retry periodically")
        return None

    async def _periodic_settle(self):
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

        from src.gamma_client import GammaClient
        gamma = GammaClient()

        # Resolve integrated collector signals (runs even with no trades pending)
        if self._collector_pending:
            self._resolve_collector_pending(gamma)
        # Flush maker IC resolved records to CSV
        # Maker IC v1 removed — shadow v2 handles resolution below
        if self._shadow_maker:
            # Self-resolve: the shadow has fills for windows the live bot didn't trade.
            # Resolve them independently via Gamma API.
            unresolved_slugs = set()
            for order in self._shadow_maker._pending:
                if order.state == "FILLED" and not order.outcome:
                    unresolved_slugs.add(order.slug)
            # Also check _orders for FILLED but not yet moved to pending
            for order in self._shadow_maker._orders.values():
                if order.state == "FILLED" and not order.outcome:
                    unresolved_slugs.add(order.slug)

            if unresolved_slugs:
                from src.gamma_client import GammaClient
                gamma = GammaClient()
                for slug in unresolved_slugs:
                    try:
                        market_data = gamma.get_market_by_slug(slug)
                        if not market_data:
                            continue
                        prices = gamma.parse_prices(market_data)
                        up_price = prices.get("up", 0)
                        down_price = prices.get("down", 0)
                        if up_price > 0.9:
                            self._shadow_maker.resolve(slug, "up")
                        elif down_price > 0.9:
                            self._shadow_maker.resolve(slug, "down")
                    except Exception:
                        pass

            self._shadow_maker.flush()
            self._event_logger.info(f"[SHADOW-IC] {self._shadow_maker.get_stats()}")
            # Memory safety: drop signals older than 10 min that failed to resolve
            # (market expired but Gamma API never returned result)
            if len(self._collector_pending) > 500:
                cutoff = time.time() - 600  # 10 minutes
                before = len(self._collector_pending)
                self._collector_pending = [
                    s for s in self._collector_pending
                    if s.get("_created", time.time()) > cutoff
                ]
                dropped = before - len(self._collector_pending)
                if dropped > 0:
                    self._event_logger.warning(
                        f"[COLL] Dropped {dropped} stale pending signals (memory safety)"
                    )

        pending = self.trade_logger.get_pending_trades()
        if not pending:
            return
        real_balance = await asyncio.to_thread(self.bot.get_usdc_balance) or self._balance

        for trade_key, record in list(pending.items()):
            try:
                market_data = await asyncio.to_thread(gamma.get_market_by_slug, record.market_slug)
                if not market_data:
                    continue

                prices = gamma.parse_prices(market_data)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)

                if up_price > 0.9 and down_price > 0.9:
                    # Both sides high — pick the higher one
                    winning_side = "up" if up_price > down_price else "down"
                elif up_price > 0.9:
                    winning_side = "up"
                elif down_price > 0.9:
                    winning_side = "down"
                else:
                    continue  # Not resolved yet

                side_won = (record.side == winning_side)
                payout = record.num_tokens * 1.0 if side_won else 0.0

                # Maker IC: resolve simulated fills for this market
                # Maker IC v1 removed
                if self._shadow_maker:
                    self._shadow_maker.resolve(record.market_slug, winning_side)
                    self._shadow_maker.flush()

                # Diagnostic: log raw prices for settlement verification
                self._event_logger.info(
                    f"[SETTLE-DEBUG] {record.coin} {record.side} {record.market_slug}: "
                    f"up={up_price:.4f} down={down_price:.4f} winner={winning_side} "
                    f"side_won={side_won}"
                )

                self.trade_logger.log_outcome(
                    market_slug=record.market_slug,
                    side=record.side,
                    won=side_won,
                    payout=payout,
                    usdc_balance=real_balance,
                )
                if self.signal_logger:
                    self.signal_logger.resolve_outcome(
                        record.market_slug, record.side,
                        won=side_won, pnl=payout - record.bet_size_usdc,
                    )
                # Shadow log: resolve both paper and live outcomes
                if self.shadow_logger:
                    self.shadow_logger.resolve(record.market_slug, record.side, side_won)

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
                # Enhanced circuit breaker
                self._cb_record_outcome(side_won)

                outcome = "WON" if side_won else "LOST"
                self.log(
                    f"SETTLED {record.coin} {record.side.upper()} {record.market_slug}: "
                    f"{outcome} ${payout - record.bet_size_usdc:+.2f} (Gamma API verified)",
                    "success" if side_won else "warning"
                )
            except Exception as e:
                self.log(f"Settle check failed for {trade_key}: {e}", "warning")

        # Shadow log: resolve paper-only shadow records that have no trade_logger entry
        if self.shadow_logger and self.shadow_logger.pending_count() > 0:
            self._settle_shadow_pending(gamma)

        # Integrated collector resolution already handled above

    def _resolve_collector_pending(self, gamma):
        """Resolve integrated collector signals via Gamma API.
        Uses >= 0.99 threshold (same as standalone collector) to avoid
        mid-resolution false settlements."""
        resolved_indices = []
        slug_results = {}

        for i, sig in enumerate(self._collector_pending):
            slug = sig["slug"]
            if slug not in slug_results:
                try:
                    data = gamma.get_market_by_slug(slug)
                    if data:
                        outcome_prices = data.get("outcomePrices", [])
                        if isinstance(outcome_prices, str):
                            import json as _json
                            try:
                                outcome_prices = _json.loads(outcome_prices)
                            except (ValueError, TypeError):
                                outcome_prices = []
                        if len(outcome_prices) >= 2:
                            up = float(outcome_prices[0])
                            down = float(outcome_prices[1])
                        else:
                            prices = gamma.parse_prices(data)
                            up = prices.get("up", 0)
                            down = prices.get("down", 0)

                        if up >= 0.99:
                            slug_results[slug] = "up"
                        elif down >= 0.99:
                            slug_results[slug] = "down"
                        else:
                            slug_results[slug] = None
                    else:
                        slug_results[slug] = None
                except Exception:
                    slug_results[slug] = None

            winner = slug_results.get(slug)
            if winner is not None:
                # Maker IC: resolve simulated fills for this settled market
                # Maker IC v1 removed
                if self._shadow_maker:
                    self._shadow_maker.resolve(slug, winner)

                outcome = "won" if winner == sig["side"] else "lost"
                row = [
                    sig["timestamp"], sig["slug"], sig["coin"], sig["side"],
                    sig["momentum_direction"], sig["is_momentum_side"],
                    sig["threshold"], sig["entry_price"], sig["best_bid"],
                    sig["momentum"], sig["elapsed"], outcome,
                ]
                with open(self._collector_csv, "a", newline="") as f:
                    import csv as _csv
                    _csv.writer(f).writerow(row)
                self._collector_resolved += 1
                resolved_indices.append(i)

        for i in reversed(resolved_indices):
            self._collector_pending.pop(i)

    def _cb_record_outcome(self, won: bool):
        """Record a trade outcome in the enhanced circuit breaker rolling window."""
        if not self.config.enable_circuit_breaker:
            return

        self._cb_recent_outcomes.append(won)
        if len(self._cb_recent_outcomes) > 10:
            self._cb_recent_outcomes = self._cb_recent_outcomes[-10:]

        # Check 1: 3 consecutive losses -> pause for 1 hour
        if len(self._cb_recent_outcomes) >= 3:
            if all(not o for o in self._cb_recent_outcomes[-3:]):
                if not self._cb_paused_until and not self._cb_stopped:
                    self._cb_paused_until = datetime.now(timezone.utc) + timedelta(hours=1)
                    self.log(
                        f"CIRCUIT BREAKER: 3 consecutive losses — pausing for 1 hour "
                        f"(until {self._cb_paused_until.strftime('%H:%M UTC')})",
                        level="warning",
                    )

        # Check 2: 5 losses out of last 10 -> hard stop
        if len(self._cb_recent_outcomes) >= 10:
            losses = sum(1 for o in self._cb_recent_outcomes if not o)
            if losses >= 5:
                self._cb_stopped = True
                self.log(
                    f"CIRCUIT BREAKER STOPPED: {losses}/10 recent trades lost — "
                    f"live trading halted. Manual restart with --disable-circuit-breaker required.",
                    level="error",
                )

    def _check_guardrails(self):
        """Check backtest-calibrated guardrails. Pause if anything exceeds
        what was seen in simulation — indicates an unseen regime."""
        import os

        # 1. Consecutive losses
        if not hasattr(self, '_guardrail_consec_losses'):
            self._guardrail_consec_losses = 0
            self._guardrail_peak_balance = self._balance
            self._guardrail_window_pnl = {}  # slug -> cumulative pnl

        # Track consecutive losses from _cb_recent_outcomes
        consec = 0
        for o in reversed(self._cb_recent_outcomes):
            if not o:
                consec += 1
            else:
                break
        self._guardrail_consec_losses = consec

        # Track peak balance
        if self._balance > self._guardrail_peak_balance:
            self._guardrail_peak_balance = self._balance

        # 1. Max consecutive losses
        if (self.config.guardrail_max_consec_losses > 0 and
                self._guardrail_consec_losses >= self.config.guardrail_max_consec_losses):
            self._event_logger.error(
                f"[GUARDRAIL] {self._guardrail_consec_losses} consecutive losses "
                f"(limit: {self.config.guardrail_max_consec_losses}). PAUSING BOT."
            )
            self._pause_bot("guardrail: consecutive losses")
            return

        # 2. Balance floor
        if (self.config.guardrail_balance_floor > 0 and
                self._balance <= self.config.guardrail_balance_floor):
            self._event_logger.error(
                f"[GUARDRAIL] Balance ${self._balance:.2f} below floor "
                f"${self.config.guardrail_balance_floor:.2f}. PAUSING BOT."
            )
            self._pause_bot("guardrail: balance floor")
            return

        # 3. Drawdown from peak
        if self._guardrail_peak_balance > 0 and self.config.guardrail_max_drawdown_pct > 0:
            drawdown_pct = (self._guardrail_peak_balance - self._balance) / self._guardrail_peak_balance * 100
            if drawdown_pct >= self.config.guardrail_max_drawdown_pct:
                self._event_logger.error(
                    f"[GUARDRAIL] Drawdown {drawdown_pct:.1f}% from peak ${self._guardrail_peak_balance:.2f} "
                    f"(limit: {self.config.guardrail_max_drawdown_pct}%). PAUSING BOT."
                )
                self._pause_bot("guardrail: max drawdown")
                return

    def _pause_bot(self, reason: str):
        """Touch .bot_paused to stop trading. Process stays alive for IC."""
        import os
        pause_path = os.path.join(os.path.dirname(self.config.log_file or "data/x"), ".bot_paused")
        try:
            with open(pause_path, "w") as f:
                f.write(reason)
            self._event_logger.error(f"[GUARDRAIL] Bot paused: {reason}")
        except Exception as e:
            self._event_logger.error(f"[GUARDRAIL] Failed to pause: {e}")

    def _settle_shadow_pending(self, gamma):
        """Resolve shadow records that are paper-only (no live trade).

        These records exist because a signal passed filters but was in observe mode
        or the live order was FOK rejected. They still need settlement.
        """
        # Get all unique market slugs from shadow pending
        shadow_slugs = set()
        for key in list(self.shadow_logger._pending.keys()):
            slug = key.rsplit(":", 1)[0]
            shadow_slugs.add(slug)

        for slug in shadow_slugs:
            try:
                market_data = gamma.get_market_by_slug(slug)
                if not market_data:
                    continue
                prices = gamma.parse_prices(market_data)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)

                if up_price > 0.9 and down_price > 0.9:
                    winner = "up" if up_price > down_price else "down"
                    self.shadow_logger.resolve_all_for_market(slug, winner)
                elif up_price > 0.9:
                    self.shadow_logger.resolve_all_for_market(slug, "up")
                elif down_price > 0.9:
                    self.shadow_logger.resolve_all_for_market(slug, "down")
            except Exception as e:
                logger_mod = logging.getLogger(__name__)
                logger_mod.debug(f"Shadow settle check for {slug}: {e}")

    def _handle_market_change(self, coin: str, old_slug: str, new_slug: str):
        """Handle market settlement and transition."""
        state = self.coin_states.get(coin)
        if not state:
            return

        # Subscribe UserWS to new market for fill detection
        has_mgr = bool(state.manager)
        has_mkt = bool(state.manager and state.manager.current_market) if has_mgr else False
        cid = getattr(state.manager.current_market, 'condition_id', '') if has_mkt else ''
        self._event_logger.info(f"[USER-WS-DBG] {coin} market_change: mgr={has_mgr} mkt={has_mkt} cid={cid[:16] if cid else 'NONE'}")
        if cid:
            self._pending_ws_subs.append(cid)
            self._event_logger.info(f"[USER-WS] Queued {coin} {cid[:16]}...")

        # Clear integrated collector dedup for old market (both callback and trade paths)
        for side in ("up", "down"):
            self._collector_crossed.pop((old_slug, side), None)
            self._collector_crossed.pop(("trade:" + old_slug, side), None)

        # Reset maker sustain timers for this coin on market change
        self._maker_momentum_first_seen.pop(f"{coin}:up", None)
        self._maker_momentum_first_seen.pop(f"{coin}:down", None)

        # Shadow Maker IC: clean up state for old market
        if self._shadow_maker:
            self._shadow_maker.on_market_change(old_slug)

        # Handle maker orders on market change (cancel unfilled, record filled)
        if self.config.maker_enabled:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._handle_maker_orders_on_market_change(old_slug))
            except RuntimeError:
                # No running loop — handle synchronously via ledger cleanup
                for order in self._order_ledger.get_orders_for_market(old_slug):
                    if order.status == "LIVE":
                        cancelled = self._order_ledger.mark_cancelled(order.order_id)
                        if cancelled:
                            with self._maker_lock:
                                self._balance += cancelled.reserved_usdc
                        self._order_ledger.remove_order(order.order_id)

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
                    if self.signal_logger:
                        self.signal_logger.resolve_outcome(
                            old_slug, side_name,
                            won=side_won, pnl=side_payout - record.bet_size_usdc,
                        )
                    # Shadow log: resolve both paper and live for this side
                    if self.shadow_logger:
                        self.shadow_logger.resolve(old_slug, side_name, side_won)
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
                    # Enhanced circuit breaker
                    self._cb_record_outcome(side_won)

                    outcome = "WON" if side_won else "LOST"
                    cusum_str = f" | {self._edge_monitor.status_str()}" if self._edge_monitor else ""
                    self.log(
                        f"SETTLED {coin} {side_name.upper()} {old_slug}: {outcome} "
                        f"${pnl:+.2f} | Session: ${self.stats.realized_pnl:+.2f}{cusum_str}",
                        "success" if side_won else "warning"
                    )

                    # Backtest-calibrated guardrails — pause if anything
                    # exceeds what was seen in simulation
                    self._check_guardrails()
                # Shadow log: also resolve any paper-only shadow records for this market
                if self.shadow_logger:
                    self.shadow_logger.resolve_all_for_market(old_slug, winning_side)
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

        # Register new token IDs with VPIN tracker
        if self._vpin_tracker and state.manager.current_market:
            self._vpin_tracker.reset_coin(coin)
            token_ids = state.manager.token_ids
            for side_name, tid in token_ids.items():
                if tid:
                    self._vpin_tracker.register_token(tid, coin, side_name)

        # Pre-warm SDK caches for new tokens (eliminates hidden HTTP calls during order creation)
        if state.manager.current_market:
            token_ids = state.manager.token_ids
            for side_name, tid in token_ids.items():
                if tid:
                    try:
                        self.bot._official_client.get_tick_size(tid)
                    except Exception:
                        pass

        # Immediate REST book fetch for new market — pre-warm BOTH the REST
        # cache AND the WS orderbook cache. This eliminates the 0.5-3s gap
        # where WS has no data for new token IDs after market transition.
        # Without this, signals hitting get_best_ask() return 1.0 (empty book).
        if state.manager.current_market and hasattr(self, '_rest_cache_lock'):
            token_ids = state.manager.token_ids
            for side_name in ("up", "down"):
                tid = token_ids.get(side_name, "")
                if tid:
                    try:
                        book = self.bot.clob_client.get_order_book(tid)
                        asks = book.get("asks", [])
                        bids = book.get("bids", [])
                        ba = min(float(a.get("price", 1)) for a in asks) if asks else 0.0
                        bb = max(float(b.get("price", 0)) for b in bids) if bids else 0.0
                        # Populate REST cache
                        with self._rest_cache_lock:
                            self._rest_book_cache[tid] = (time.time(), ba, bb)
                        # Inject directly into WS orderbook cache so
                        # get_best_ask() returns real data immediately
                        if state.manager.ws and asks:
                            snapshot = OrderbookSnapshot(
                                asset_id=tid,
                                market="",
                                timestamp=int(time.time()),
                                bids=[OrderbookLevel(price=float(b["price"]), size=float(b["size"])) for b in bids],
                                asks=[OrderbookLevel(price=float(a["price"]), size=float(a["size"])) for a in asks],
                            )
                            snapshot.bids.sort(key=lambda x: x.price, reverse=True)
                            snapshot.asks.sort(key=lambda x: x.price)
                            state.manager.ws._orderbooks[tid] = snapshot
                    except Exception:
                        pass

        # Pre-sign order ladder for instant execution (~23ms instead of ~280ms).
        # Signs FAK orders at every $0.01 from min to max entry price.
        # On signal, we just POST the matching pre-signed body — no signing delay.
        if self._fast_order and state.manager.current_market:
            if not hasattr(self, '_presigned_cache'):
                self._presigned_cache = {}
            token_ids = state.manager.token_ids
            for side_name in ("up", "down"):
                tid = token_ids.get(side_name)
                if tid:
                    try:
                        ladder = self._fast_order.presign_price_ladder(
                            tid, 5.0, "BUY",
                            self.config.min_entry_price,
                            self.config.max_entry_price,
                            step=0.01,
                            fee_rate_bps=1000,
                        )
                        self._presigned_cache[(coin, side_name)] = ladder
                    except Exception as e:
                        self._event_logger.warning(f"[PRESIGN] {coin} {side_name} failed: {e}")

        # Clear signal logger dedup for old market
        if self.signal_logger and old_slug:
            self.signal_logger.clear_slug(old_slug)

        # If startup_slug was never set (coin wasn't discovered at boot),
        # treat this first-discovered market as the startup market — skip it.
        if not state.startup_slug:
            state.startup_slug = new_slug
            self.log(f"{coin} first discovery: {new_slug} (skipping — treat as startup)", "warning")

        # Set new strike immediately — no delay. Collector does the same.
        # Ring buffer has historical ticks, so t+0 lookback works even if
        # we detect the transition a few seconds late.
        state.current_slug = new_slug
        state.market_start_time = time.time()
        self._set_strike(state)

    async def _periodic_redeem(self):
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
            results = await asyncio.to_thread(self.bot.redeem_all)
            if results:
                self.log(f"Redeemed {len(results)} position(s) to USDC", "success")
                # Refresh balance after successful redemption
                self._last_balance_check = 0
                await asyncio.to_thread(self._refresh_balance)
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

            fair_prob = 0.0  # No FV model
            best_ask = self._get_best_ask_with_rest_fallback(state, sig.side)
            if best_ask <= 0 or best_ask >= 1.0:
                self.log(f"EXPIRED: {sig.coin} {sig.side.upper()} — no ask", "warning")
                continue

            buy_price = round(best_ask, 2)
            edge = 0.0  # No edge calc — pure momentum

            # Always proceed (momentum already confirmed at detection time)
            if True:
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
                    f"({edge:+.2f} < {_coin_min_edge:.2f})",
                    "warning"
                )

        for key in expired_keys:
            self._pending_signals.pop(key, None)

    # ========== GTD MAKER ORDER SYSTEM (Phase 1) ==========

    async def _manage_maker_orders(self):
        """Place/monitor/cancel GTD maker orders for all coins.

        Two modes:
        - Single-side (maker_dual_mode=False): momentum-confirmed, one side only
        - Dual-GTD (maker_dual_mode=True): rest on BOTH sides at fixed price,
          fill IS the signal, cancel opposite on fill
        """
        if not self.config.maker_enabled:
            return
        if self._is_paused():
            return

        live_count = self._order_ledger.count_live()

        # --- Cancel orders that should no longer be live ---
        for order in self._order_ledger.get_live_orders():
            state = self.coin_states.get(order.coin)
            should_cancel = False
            cancel_reason = ""

            # Market changed — cancel in all modes
            if not state or state.current_slug != order.market_slug:
                should_cancel = True
                cancel_reason = "market changed"

            # Window ending — not enough time for fill
            elif state:
                tte = state.seconds_to_expiry()
                if tte < 20:
                    should_cancel = True
                    cancel_reason = "window ending"

            # Momentum reversed — cancel in ALL modes
            # Uses maker_cancel_momentum (higher than placement threshold) to give
            # the 3-6s CLOB cancel latency a buffer before momentum fully reverses.
            if not should_cancel:
                if state and state.strike_price > 0:
                    spot = self.binance.get_price(order.coin)
                    disp = (spot - state.strike_price) / state.strike_price
                    cancel_mom = self.config.maker_cancel_momentum
                    if order.side == "up" and disp < cancel_mom:
                        should_cancel = True
                        cancel_reason = "momentum reversed (up→flat/down)"
                    elif order.side == "down" and disp > -cancel_mom:
                        should_cancel = True
                        cancel_reason = "momentum reversed (down→flat/up)"

            # Position already held on this side — cancel in all modes
            if not should_cancel and state:
                if order.side == "up" and state.has_up_position:
                    should_cancel = True
                    cancel_reason = "position already held"
                elif order.side == "down" and state.has_down_position:
                    should_cancel = True
                    cancel_reason = "position already held"

            if should_cancel:
                await self._cancel_maker_order(order, cancel_reason)
                live_count = self._order_ledger.count_live()
                # State machine: mark cancelled (prevents re-placement)
                if "reversed" in cancel_reason and state:
                    self._gtd_state.mark_cancelled(order.market_slug, order.coin)

        # --- GTD Placement ---
        if self.config.maker_dual_mode:
            # DUAL-GTD: Rest on BOTH UP and DOWN at fixed price.
            # No momentum check. No direction prediction.
            # The fill IS the signal — whichever side fills is the trade.
            rest_price = self.config.maker_rest_price
            num_tokens = self.config.min_size_tokens
            cost_per_side = rest_price * num_tokens

            # Count EVERYTHING: filled positions + coins with resting orders
            active_positions = sum(1 for s in self.coin_states.values()
                                   if s.has_up_position or s.has_down_position)
            coins_with_orders = len(set(o.coin for o in self._order_ledger.get_live_orders()))
            total_active = active_positions + coins_with_orders

            # Hard stop: only 1 coin active at a time (positions + resting combined)
            if total_active >= self.config.max_concurrent_positions:
                return  # don't even loop through coins

            for coin, state in self.coin_states.items():
                if not state.current_slug or state.current_slug == state.startup_slug:
                    continue

                # STATE MACHINE: only IDLE coins can have orders placed
                if not self._gtd_state.can_place(state.current_slug, coin):
                    continue

                if state.has_up_position or state.has_down_position:
                    continue
                if self._order_ledger.get_live_orders_for_coin(coin):
                    continue

                # Elapsed check
                duration = GammaClient.TIMEFRAME_SECONDS.get(self.config.timeframe, 300)
                tte = state.seconds_to_expiry()
                elapsed = duration - tte
                if elapsed < self.config.maker_place_elapsed:
                    continue
                if tte < 30:
                    continue

                # Momentum check BEFORE placing — don't place if momentum
                # is below cancel threshold (would be immediately cancelled)
                spot = self.binance.get_price(coin)
                if spot > 0 and state.strike_price > 0:
                    disp = (spot - state.strike_price) / state.strike_price
                    abs_disp = abs(disp)
                    # At least one side needs momentum above cancel threshold
                    if abs_disp < self.config.maker_cancel_momentum:
                        continue

                # Capital: need to afford BOTH sides (skip in observe mode)
                total_cost = cost_per_side * 2
                if not self.config.observe_only and (total_cost > self._balance or cost_per_side < 1.0):
                    continue

                # Respect max_maker_orders (each coin uses 2 slots)
                if live_count + 2 > self.config.max_maker_orders:
                    continue

                # Get token IDs for both sides
                up_token = state.manager.token_ids.get("up")
                down_token = state.manager.token_ids.get("down")
                if not up_token or not down_token:
                    continue

                expiry_ts = int(time.time() + tte + 60)

                # Determine momentum direction for side filtering
                mom_direction = "up" if disp > 0 else "down"

                # Place BOTH sides (skip sides that would cross the book)
                placed_count = 0
                for side, token_id in [("up", up_token), ("down", down_token)]:
                    if self.config.observe_only:
                        placed_count += 1
                        continue

                    # Dynamic pricing: ONLY place on the momentum-aligned side.
                    # The anti-momentum side has a low ask that would fill instantly
                    # against momentum — that's adverse selection, not edge.
                    if self.config.maker_dynamic_pricing and side != mom_direction:
                        self._event_logger.info(
                            f"[GTD-SKIP] {coin} {side} anti-momentum (mom={mom_direction}) — skipped"
                        )
                        continue

                    # Get current ask for this side
                    side_ask = self._get_best_ask_with_rest_fallback(state, side)

                    # Dynamic pricing: place at best_ask - 0.01 (one tick below ask)
                    if self.config.maker_dynamic_pricing:
                        if side_ask <= 0 or side_ask >= 1.0:
                            self._event_logger.info(
                                f"[GTD-SKIP] {coin} {side} ask=${side_ask:.2f} invalid (no book data)"
                            )
                            continue
                        side_rest_price = round(side_ask - 0.01, 2)
                        # Cap at max entry price
                        side_rest_price = min(side_rest_price, self.config.maker_max_entry_price)
                        # Floor: never place below $0.05
                        if side_rest_price < 0.05:
                            self._event_logger.info(
                                f"[GTD-SKIP] {coin} {side} dynamic_price=${side_rest_price:.2f} too low (ask=${side_ask:.2f})"
                            )
                            continue
                        side_cost = side_rest_price * num_tokens
                        if not self.config.observe_only and side_cost > self._balance:
                            continue
                        self._event_logger.info(
                            f"[GTD-DYN] {coin} {side} ask=${side_ask:.2f} → rest=${side_rest_price:.2f} "
                            f"(gap=${side_ask - side_rest_price:.2f})"
                        )
                    else:
                        side_rest_price = rest_price
                        side_cost = cost_per_side
                        # Pre-check: don't place if our rest price >= ask (would cross book)
                        if side_ask > 0 and side_ask <= side_rest_price:
                            self._event_logger.info(
                                f"[GTD-SKIP] {coin} {side} ask=${side_ask:.2f} <= rest=${side_rest_price:.2f} (would cross)"
                            )
                            continue

                    # Queue-aware placement gate + telemetry. Compute our
                    # queue_ahead at rest_price on this side. Skip if exceeding
                    # configured max (direct AS defense — heavy queue means
                    # we only fill when dip is deep = adverse selection).
                    _queue_ahead = 0.0
                    _bid_at_place = 0.0
                    _ob = state.manager.get_orderbook(side) if state.manager else None
                    if _ob and _ob.bids:
                        for _lvl in _ob.bids:
                            if _lvl.price >= side_rest_price:
                                _queue_ahead += _lvl.size
                        _bid_at_place = _ob.best_bid
                    if self.config.maker_max_queue_ahead > 0 and _queue_ahead > self.config.maker_max_queue_ahead:
                        self._event_logger.info(
                            f"[GTD-SKIP-QUEUE] {coin} {side} queue_ahead={_queue_ahead:.1f} > "
                            f"max={self.config.maker_max_queue_ahead:.1f} (AS risk)"
                        )
                        continue
                    # Per-placement telemetry for post-hoc calibration.
                    self._event_logger.info(
                        f"[GTD-PLACE-STATS] {coin} {side} rest=${side_rest_price:.4f} "
                        f"ask=${side_ask:.4f} bid=${_bid_at_place:.4f} "
                        f"queue_ahead={_queue_ahead:.2f} momentum={disp:+.6f} elapsed={elapsed:.1f}"
                    )

                    try:
                        result = await asyncio.to_thread(
                            self._fast_order._place_order_sync,
                            token_id, side_rest_price, num_tokens, "BUY", "GTD", 1000,
                            expiration=expiry_ts,
                        )
                        order_id = result.get("orderID", "")
                        has_error = bool(result.get("error", "") or result.get("errorMsg", ""))

                        if order_id and not has_error:
                            self._balance -= side_cost
                            cid = getattr(state.manager.current_market, 'condition_id', '') if state.manager and state.manager.current_market else ''
                            order = MakerOrder(
                                coin=coin, side=side, order_id=order_id,
                                token_id=token_id, price=side_rest_price,
                                size=num_tokens, market_slug=state.current_slug,
                                placed_at=time.time(), expiry_ts=expiry_ts,
                                reserved_usdc=side_cost,
                                condition_id=cid,
                            )
                            self._order_ledger.add_order(order)
                            live_count += 1
                            placed_count += 1
                        else:
                            error_msg = (result.get("errorMsg", "") or result.get("error", "unknown"))[:80]
                            self._event_logger.info(
                                f"[GTD-FAIL] {coin} {side} @ ${side_rest_price:.2f}: {error_msg}"
                            )
                            # "crosses book" may have filled as taker despite postOnly rejection
                            # Check if CLOB returned takingAmount (partial/full fill on rejection)
                            taking = float(result.get("takingAmount", 0) or 0)
                            if taking > 0:
                                self._event_logger.warning(
                                    f"[GTD-CROSSFILL] {coin} {side.upper()} filled {taking} tokens "
                                    f"on 'crosses book' rejection! Recording position."
                                )
                                self._balance -= side_rest_price * taking
                                if side == "up":
                                    state.up_tokens += taking
                                    state.up_cost += side_rest_price * taking
                                else:
                                    state.down_tokens += taking
                                    state.down_cost += side_rest_price * taking
                                state.last_trade_time = time.time()
                                self.stats.trades += 1
                                self.stats.pending += 1
                                self._gtd_state.mark_filled(state.current_slug, coin)
                                self.log(
                                    f"SNIPE {coin} {side.upper()} @ {side_rest_price:.2f} "
                                    f"x{taking:.0f} (${side_rest_price*taking:.2f}) MAKER-CROSS",
                                    "success"
                                )
                    except Exception as e:
                        self._event_logger.warning(
                            f"[GTD-ERROR] {coin} {side} placement failed: {e}"
                        )

                if placed_count > 0:
                    # State machine: IDLE → RESTING
                    self._gtd_state.mark_resting(state.current_slug, coin)
                    # Summarize actual placed prices from ledger
                    placed_orders = self._order_ledger.get_live_orders_for_coin(coin)
                    placed_prices = [f"${o.price:.2f}" for o in placed_orders]
                    total_reserved = sum(o.reserved_usdc for o in placed_orders)
                    price_str = "/".join(placed_prices) if placed_prices else f"${rest_price:.2f}"
                    mode = "DYN" if self.config.maker_dynamic_pricing else "FIX"
                    self._event_logger.info(
                        f"[DUAL-GTD] {coin} placed {placed_count} orders @ {price_str} [{mode}] "
                        f"x{num_tokens:.0f} el={elapsed:.0f}s expiry={int(tte)}s "
                        f"reserved=${total_reserved:.2f}"
                    )
                    self.log(
                        f"[DUAL-GTD] {coin} resting @ {price_str} [{mode}] "
                        f"(${total_reserved:.2f} reserved)",
                        "info"
                    )

    async def _check_trades_for_fill(self, order: MakerOrder) -> bool:
        """Authoritative fill detection for a single maker order.

        The CLOB /data/trades endpoint only returns trades where the wallet
        is the TAKER — it silently misses maker fills. Apr-2026 reconciliation
        against Polymarket on-chain activity showed 22/37 maker fills were
        missed by /data/trades alone. This function now checks BOTH sources:

          1. CLOB /data/trades by order_id — catches any taker hits.
          2. data-api /activity?type=TRADE by asset+price+time — catches
             maker fills (our order rests, someone else takes it).

        Returns True if a fill was detected and recorded.
        """
        # --- Check 1: CLOB /data/trades (order_id match) ---
        condition_id = order.condition_id
        if not condition_id:
            state = self.coin_states.get(order.coin)
            if state and state.manager and state.manager.current_market:
                condition_id = getattr(state.manager.current_market, 'condition_id', '')

        if condition_id:
            try:
                trades = await self.bot.get_trades_for_market(condition_id)
                for trade in trades:
                    maker_orders = trade.get("maker_orders", [])
                    for mo in maker_orders:
                        if mo.get("order_id") == order.order_id:
                            matched = float(mo.get("matched_amount", 0) or 0)
                            if matched > 0:
                                filled_order = self._order_ledger.mark_filled(order.order_id, matched)
                                if filled_order:
                                    self.log(
                                        f"[MAKER] {order.coin} {order.side.upper()} FILLED "
                                        f"{matched} tokens (detected via CLOB trades)",
                                        "warning"
                                    )
                                    await self._record_maker_fill(filled_order, matched)
                                    if self.config.maker_dual_mode:
                                        await self._cancel_opposite_side(filled_order)
                                return True
                    if trade.get("taker_order_id") == order.order_id:
                        size = float(trade.get("size", 0) or 0)
                        if size > 0:
                            filled_order = self._order_ledger.mark_filled(order.order_id, size)
                            if filled_order:
                                self.log(
                                    f"[MAKER] {order.coin} {order.side.upper()} FILLED "
                                    f"{size} tokens as taker (detected via CLOB trades)",
                                    "warning"
                                )
                                await self._record_maker_fill(filled_order, size)
                                if self.config.maker_dual_mode:
                                    await self._cancel_opposite_side(filled_order)
                            return True
            except Exception as e:
                self._event_logger.warning(
                    f"[MAKER] CLOB trades check failed for {order.coin}: {e}"
                )

        # --- Check 2: data-api /activity (asset + price + time match) ---
        # Activity records don't include our order_id; match by fingerprint.
        try:
            acts = await self.bot.get_recent_activity_trades(limit=100)
            # Aggregate partial fills for this asset+price within the order's lifetime
            matched_total = 0.0
            # Look at activity records from (placed_at - 2s) to now
            now = time.time()
            window_lo = (order.placed_at or (now - 600)) - 2
            for t in acts:
                if t.get("type") != "TRADE": continue
                if t.get("side") != "BUY": continue
                if str(t.get("asset", "")) != str(order.token_id): continue
                tp = float(t.get("price", 0) or 0)
                if abs(tp - order.price) > 0.015: continue
                ts = float(t.get("timestamp", 0) or 0)
                if ts < window_lo: continue
                if ts > now + 5: continue
                matched_total += float(t.get("size", 0) or 0)
            if matched_total > 0:
                filled_order = self._order_ledger.mark_filled(order.order_id, matched_total)
                if filled_order:
                    self.log(
                        f"[MAKER] {order.coin} {order.side.upper()} FILLED "
                        f"{matched_total:.3f} tokens (detected via data-api activity)",
                        "warning"
                    )
                    await self._record_maker_fill(filled_order, matched_total)
                    if self.config.maker_dual_mode:
                        await self._cancel_opposite_side(filled_order)
                return True
        except Exception as e:
            self._event_logger.warning(
                f"[MAKER] Activity check failed for {order.coin}: {e}"
            )
        return False

    async def _poll_maker_orders(self):
        """Poll CLOB for fill status on all LIVE maker orders.

        Uses two methods:
        1. get_order() — fast but can return stale size_matched=0
        2. get_trades(market=condition_id) — authoritative, catches ghost fills
        """
        if not self.config.maker_enabled:
            return

        now = time.time()
        for order in self._order_ledger.get_live_orders():
            # Rate limit polling per order
            if now - order.last_polled < self.config.maker_poll_interval:
                continue
            order.last_polled = now

            try:
                order_data = await asyncio.to_thread(
                    self.bot._official_client.get_order, order.order_id
                )
                if not order_data:
                    continue

                clob_status = order_data.get("status", "")
                size_matched = float(order_data.get("size_matched", 0) or 0)

                if size_matched > 0:
                    # Filled (fully or partially)
                    filled_order = self._order_ledger.mark_filled(order.order_id, size_matched)
                    if filled_order:
                        await self._record_maker_fill(filled_order, size_matched)
                        if self.config.maker_dual_mode:
                            await self._cancel_opposite_side(filled_order)
                    continue

                if clob_status in ("CANCELLED", "DEAD", "EXPIRED"):
                    # SAFETY NET: before returning capital, verify no late fill.
                    # CLOB can report EXPIRED while a fill is still propagating; skipping
                    # this check is how historical ghosts were missed in _poll_maker_orders.
                    fill_found = await self._check_trades_for_fill(order)
                    if fill_found:
                        # _check_trades_for_fill already recorded the fill and cleaned up.
                        continue
                    cancelled_order = self._order_ledger.mark_cancelled(order.order_id)
                    if cancelled_order:
                        with self._maker_lock:
                            self._balance += cancelled_order.reserved_usdc
                        self.log(
                            f"[MAKER] {order.coin} {order.side.upper()} "
                            f"CLOB status={clob_status} — returned ${cancelled_order.reserved_usdc:.2f}",
                            "info"
                        )
                    self._order_ledger.remove_order(order.order_id)
                    continue

                # get_order shows LIVE with size_matched=0 — but order may
                # have filled and CLOB hasn't propagated yet.
                # Check trades API (CLOB + /activity) as authoritative fallback.
                if now - order.placed_at > 10:
                    await self._check_trades_for_fill(order)

            except Exception as e:
                self._event_logger.warning(
                    f"[MAKER] Poll failed for {order.coin} {order.side} "
                    f"oid={order.order_id[:16]}: {e}"
                )

    async def _cancel_maker_order(self, order: MakerOrder, reason: str = ""):
        """Cancel a maker order with robust fill detection.

        For reversal cancels: FAST PATH — send cancel immediately, then verify.
        Every second of pre-check is a second the order is live and exposed to
        adverse fills from sellers dumping during momentum reversal.

        For other cancels (window ending, market changed): full pre-check path.

        Post-cancel: retry loop (3 attempts, 3s apart) checking
        get_order + trades API for delayed CLOB propagation.
        """
        # Don't cancel orders less than 15 seconds old
        age = time.time() - order.placed_at
        if age < 15 and "window ending" not in reason and "market changed" not in reason:
            return

        is_reversal = "reversed" in reason

        self._event_logger.info(
            f"[CANCEL-DBG] {order.coin} {order.side} START reason={reason} "
            f"{'FAST' if is_reversal else 'NORMAL'} oid={order.order_id[:16]}"
        )

        # For reversal cancels: skip pre-checks, send cancel IMMEDIATELY.
        # Pre-checks cost 3-6 seconds during which the order is live and exposed.
        # If the order was already filled, the cancel will fail harmlessly and
        # Step 3 will detect the fill.
        if not is_reversal:
            # Non-reversal cancels: safe to pre-check (no urgency)
            # Step 1: Check if already filled BEFORE sending cancel (get_order)
            try:
                pre_check = await asyncio.to_thread(
                    self.bot._official_client.get_order, order.order_id
                )
                matched = float((pre_check or {}).get("size_matched", 0) or 0)
                status = (pre_check or {}).get("status", "")
                self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step1-order: status={status} matched={matched}")
                if matched > 0 or status == "MATCHED":
                    filled_order = self._order_ledger.mark_filled(order.order_id, matched or order.size)
                    if filled_order:
                        self.log(
                            f"[MAKER] {order.coin} {order.side.upper()} FILLED {matched or order.size} "
                            f"tokens (caught PRE-cancel via get_order).",
                            "warning"
                        )
                        await self._record_maker_fill(filled_order, matched or order.size)
                        if self.config.maker_dual_mode:
                            await self._cancel_opposite_side(filled_order)
                    return
            except Exception as e:
                self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step1-order FAILED: {e}")

            # Step 1b: Check trades API pre-cancel (authoritative)
            if await self._check_trades_for_fill(order):
                self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step1-trades: FILL FOUND")
                return

        # Step 2: Send cancel (FIRST action for reversal cancels).
        # Record timestamps for latency telemetry — enables post-hoc measurement
        # of real cancel latency (send → CLOB-confirmed CANCELLED) to replace
        # shadow's fixed 3s assumption.
        _cancel_sent_ts = time.time()
        try:
            cancel_result = await self.bot.cancel_order(order.order_id)
            _cancel_resp_ts = time.time()
            _resp_ms = int((_cancel_resp_ts - _cancel_sent_ts) * 1000)
            self._event_logger.info(
                f"[CANCEL-LAT] {order.coin} {order.side} oid={order.order_id[:16]} "
                f"resp_ms={_resp_ms} success={getattr(cancel_result, 'success', '?')}"
            )
            self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step2: cancel sent")
        except Exception as e:
            self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step2 FAILED: {e}")

        # Step 3: Post-cancel retry loop — CLOB propagation can take 5-15s
        # Check 3 times with 3s delays (total ~9s window)
        fill_detected = False
        for attempt in range(3):
            await asyncio.sleep(3.0)

            # 3a: Check get_order for updated status
            try:
                order_data = await asyncio.to_thread(
                    self.bot._official_client.get_order, order.order_id
                )
                matched = float((order_data or {}).get("size_matched", 0) or 0)
                status = (order_data or {}).get("status", "")
                _elapsed_since_send = int((time.time() - _cancel_sent_ts) * 1000)
                self._event_logger.info(
                    f"[CANCEL-DBG] {order.coin} Step3.{attempt+1}-order: "
                    f"status={status} matched={matched} since_send_ms={_elapsed_since_send}"
                )
                # Telemetry: first time we observe CANCELLED/DEAD status, record latency
                if status in ("CANCELLED", "DEAD"):
                    self._event_logger.info(
                        f"[CANCEL-LAT] {order.coin} {order.side} oid={order.order_id[:16]} "
                        f"confirmed_ms={_elapsed_since_send} status={status}"
                    )
                if matched > 0 or status == "MATCHED":
                    fill_detected = True
                    break
            except Exception as e:
                self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step3.{attempt+1}-order FAILED: {e}")

            # 3b: Check trades API (authoritative)
            if await self._check_trades_for_fill(order):
                self._event_logger.info(f"[CANCEL-DBG] {order.coin} Step3.{attempt+1}-trades: FILL FOUND")
                return  # _check_trades_for_fill already recorded it

            # If order is confirmed cancelled/dead, no need to keep checking
            try:
                if status in ("CANCELLED", "DEAD", "EXPIRED"):
                    self._event_logger.info(
                        f"[CANCEL-DBG] {order.coin} Step3.{attempt+1}: confirmed {status}, stopping"
                    )
                    break
            except Exception:
                pass

        if fill_detected:
            filled_order = self._order_ledger.mark_filled(order.order_id, order.size)
            if filled_order:
                self.log(
                    f"[MAKER] {order.coin} {order.side.upper()} FILLED {order.size} "
                    f"tokens (caught post-cancel retry)!",
                    "warning"
                )
                await self._record_maker_fill(filled_order, order.size)
                if self.config.maker_dual_mode:
                    await self._cancel_opposite_side(filled_order)
            return

        # Order was not filled — cancel and return capital
        cancelled_order = self._order_ledger.mark_cancelled(order.order_id)
        if cancelled_order:
            with self._maker_lock:
                self._balance += cancelled_order.reserved_usdc
            self.log(
                f"[MAKER] Cancelled {order.coin} {order.side.upper()} @ ${order.price:.2f}"
                f"{' — ' + reason if reason else ''}"
                f" (returned ${cancelled_order.reserved_usdc:.2f})",
                "info"
            )

        # Schedule a delayed ghost-fill re-check for fast-cancelled orders.
        # The CLOB API is eventually consistent — fills can take 15-30s to
        # propagate. The fast cancel path checks too quickly. This background
        # task re-checks 30s later via the trades API (authoritative).
        if is_reversal:
            async def _delayed_ghost_check(oid, o_coin, o_side, o_size, o_price, o_reserved, cid, o_token_id, o_placed_at, o_slug):
                await asyncio.sleep(30)
                try:
                    # Check 1: CLOB /data/trades by order_id (catches taker fills).
                    trades_data = await self.bot.get_trades_for_market(cid) if cid else []
                    if not trades_data:
                        from py_clob_client.clob_types import TradeParams
                        trades_data = await asyncio.to_thread(
                            self.bot._official_client.get_trades, TradeParams()
                        )
                        if isinstance(trades_data, dict):
                            trades_data = trades_data.get("data", [])
                    for trade in (trades_data or []):
                        for mo in trade.get("maker_orders", []):
                            if mo.get("order_id") == oid:
                                matched = float(mo.get("matched_amount", 0) or 0)
                                if matched > 0:
                                    self._event_logger.info(
                                        f"[GHOST-FIX] {o_coin} {o_side} DELAYED FILL (CLOB) matched={matched}"
                                    )
                                    with self._maker_lock:
                                        self._balance -= o_reserved
                                    ghost_order = MakerOrder(
                                        coin=o_coin, side=o_side, order_id=oid,
                                        token_id=o_token_id, price=o_price,
                                        size=o_size, reserved_usdc=o_reserved,
                                        market_slug=o_slug, placed_at=o_placed_at,
                                        expiry_ts=0, condition_id=cid,
                                    )
                                    await self._record_maker_fill(ghost_order, matched)
                                    return

                    # Check 2: data-api /activity (catches maker fills — the real ghost gap).
                    acts = await self.bot.get_recent_activity_trades(limit=100)
                    matched_total = 0.0
                    import time as _t
                    now = _t.time()
                    window_lo = (o_placed_at or (now - 600)) - 2
                    for t in acts or []:
                        if t.get("type") != "TRADE": continue
                        if t.get("side") != "BUY": continue
                        if str(t.get("asset", "")) != str(o_token_id): continue
                        tp = float(t.get("price", 0) or 0)
                        if abs(tp - o_price) > 0.015: continue
                        ts = float(t.get("timestamp", 0) or 0)
                        if ts < window_lo: continue
                        if ts > now + 5: continue
                        matched_total += float(t.get("size", 0) or 0)
                    if matched_total > 0:
                        self._event_logger.info(
                            f"[GHOST-FIX] {o_coin} {o_side} DELAYED FILL (activity) matched={matched_total:.3f}"
                        )
                        with self._maker_lock:
                            self._balance -= o_reserved
                        ghost_order = MakerOrder(
                            coin=o_coin, side=o_side, order_id=oid,
                            token_id=o_token_id, price=o_price,
                            size=o_size, reserved_usdc=o_reserved,
                            market_slug=o_slug, placed_at=o_placed_at,
                            expiry_ts=0, condition_id=cid,
                        )
                        await self._record_maker_fill(ghost_order, matched_total)
                        return

                    self._event_logger.info(
                        f"[GHOST-FIX] {o_coin} {o_side} delayed check: no fill "
                        f"(CLOB={len(trades_data or [])} activity={len(acts or [])})"
                    )
                except Exception as e:
                    self._event_logger.info(f"[GHOST-FIX] {o_coin} {o_side} delayed check FAILED: {e}")

            condition_id = ""
            if hasattr(order, 'condition_id') and order.condition_id:
                condition_id = order.condition_id
            elif state and state.manager and state.manager.current_market:
                condition_id = getattr(state.manager.current_market, 'condition_id', '')

            asyncio.create_task(_delayed_ghost_check(
                order.order_id, order.coin, order.side,
                order.size, order.price, order.reserved_usdc,
                condition_id,
                order.token_id, order.placed_at, order.market_slug,
            ))
            self._event_logger.info(
                f"[GHOST-FIX] {order.coin} {order.side} scheduled delayed re-check in 30s"
            )

        self._order_ledger.remove_order(order.order_id)

        # Maker V2 FAK fallback: maker didn't fill, try FAK if signal still valid.
        # DISABLED in dual mode — no FAK should ever fire
        if self.config.maker_dual_mode:
            return
        if reason and ("reversed" in reason or "reversal" in reason
                       or "market changed" in reason
                       or "paused" in reason or "opposite side filled" in reason
                       or "window ending" in reason):
            return
        state = self.coin_states.get(order.coin)
        if not state or state.current_slug != order.market_slug:
            return
        if state.has_up_position or state.has_down_position:
            return
        # Check momentum still confirmed
        spot = self.binance.get_price(order.coin)
        if spot > 0 and state.strike_price > 0:
            disp = (spot - state.strike_price) / state.strike_price
            mom_ok = (order.side == "up" and disp >= self.config.min_momentum) or \
                     (order.side == "down" and disp <= -self.config.min_momentum)
            if mom_ok:
                best_ask = self._get_best_ask_with_rest_fallback(state, order.side)
                if 0 < best_ask <= self.config.max_entry_price:
                    self.log(
                        f"[MAKER→FAK] {order.coin} {order.side.upper()} maker expired, "
                        f"momentum still valid — firing FAK @ ${best_ask:.2f}",
                        "info"
                    )
                    await self._execute_snipe(state, order.side, best_ask)

    async def _record_maker_fill(self, order: MakerOrder, filled_tokens: float):
        """Record a maker order fill as a position."""
        state = self.coin_states.get(order.coin)
        if not state:
            return

        # State machine: mark FILLED (prevents any re-placement or re-cancellation)
        self._gtd_state.mark_filled(order.market_slug, order.coin)

        # Check if momentum still supports this direction (reversal detection)
        # In dual mode, fills on either side are expected — skip reversal warning
        spot = self.binance.get_price(order.coin)
        if not self.config.maker_dual_mode and spot > 0 and state.strike_price > 0:
            fill_disp = (spot - state.strike_price) / state.strike_price
            reversal = (order.side == "up" and fill_disp < 0) or (order.side == "down" and fill_disp > 0)
            if reversal:
                self.log(
                    f"[MAKER] REVERSAL FILL: {order.coin} {order.side.upper()} filled "
                    f"but momentum now {fill_disp:+.4f} (wrong direction!)",
                    "warning"
                )

        # Maker = zero taker fees
        actual_cost = order.price * filled_tokens
        # Adjust reserved capital: refund any excess reservation
        refund = order.reserved_usdc - actual_cost
        if refund > 0:
            with self._maker_lock:
                self._balance += refund

        if order.side == "up":
            state.up_tokens += filled_tokens
            state.up_cost += actual_cost
        else:
            state.down_tokens += filled_tokens
            state.down_cost += actual_cost

        state.last_trade_time = time.time()
        self.stats.trades += 1
        self.stats.pending += 1
        self.stats.total_wagered += actual_cost
        self.stats.opportunities_found += 1

        # Log trade
        spot = self.binance.get_price(order.coin)
        vol, vol_src = self._get_volatility(order.coin)
        other_side = "down" if order.side == "up" else "up"
        other_price = state.manager.get_mid_price(other_side)
        strike_src = getattr(state, '_strike_source', 'unknown')
        mom = (spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0
        self.trade_logger.log_trade(
            market_slug=state.current_slug, coin=order.coin,
            timeframe=self.config.timeframe, side=order.side,
            entry_price=order.price, bet_size_usdc=actual_cost,
            num_tokens=filled_tokens, bankroll=self._balance,
            usdc_balance=self._balance,
            btc_price=spot,
            other_side_price=other_price, volatility_std=vol,
            time_to_expiry_at_entry=state.seconds_to_expiry(),
            momentum_at_entry=mom, volatility_at_entry=vol,
            signal_to_order_ms=0, order_latency_ms=0, total_latency_ms=0,
            vol_source=vol_src, strike_source=strike_src,
        )
        self.log(
            f"SNIPE {order.coin} {order.side.upper()} @ {order.price:.2f} "
            f"x{filled_tokens:.0f} (${actual_cost:.2f}) MAKER (zero fees) "
            f"mom={mom:+.4f}",
            "success"
        )

        # Clean up from ledger
        self._order_ledger.remove_order(order.order_id)

    async def _cancel_opposite_side(self, filled_order: MakerOrder):
        """After one side fills, cancel LIVE orders on the opposite side for the same coin."""
        opposite = "down" if filled_order.side == "up" else "up"
        for order in self._order_ledger.get_live_orders_for_coin_side(filled_order.coin, opposite):
            await self._cancel_maker_order(order, f"opposite side filled ({filled_order.side})")

        # Detect dual-fill: both sides now have positions
        state = self.coin_states.get(filled_order.coin)
        if state and state.has_up_position and state.has_down_position:
            self._event_logger.warning(
                f"[DUAL-FILL] {filled_order.coin} — BOTH sides filled! "
                f"UP cost=${state.up_cost:.2f} DOWN cost=${state.down_cost:.2f} "
                f"Total=${state.up_cost + state.down_cost:.2f} (guaranteed $1 payout, "
                f"loss=${state.up_cost + state.down_cost - filled_order.size:.2f})"
            )

    async def _handle_maker_orders_on_market_change(self, old_slug: str):
        """Handle maker orders when a market transitions.

        For each LIVE order on the old market:
        - Final CLOB status check (get_order + trades API)
        - If filled: record position and log trade
        - If not filled: cancel, return reserved capital
        """
        # Clean up state machine for old window
        self._gtd_state.cleanup(old_slug)
        if hasattr(self, '_cancelled_this_window'):
            self._cancelled_this_window.pop(old_slug, None)
        live_orders = [o for o in self._order_ledger.get_orders_for_market(old_slug)
                       if o.status == "LIVE"]
        if not live_orders:
            return

        for order in live_orders:
            fill_found = False

            # Check 1: get_order (may be stale but check anyway)
            try:
                order_data = await asyncio.to_thread(
                    self.bot._official_client.get_order, order.order_id
                )
                matched = float((order_data or {}).get("size_matched", 0) or 0)
                if matched > 0:
                    filled_order = self._order_ledger.mark_filled(order.order_id, matched)
                    if filled_order:
                        self.log(
                            f"[MAKER] {order.coin} {order.side.upper()} FILLED {matched} "
                            f"tokens on market change (get_order).",
                            "warning"
                        )
                        await self._record_maker_fill(filled_order, matched)
                    fill_found = True
            except Exception:
                pass

            # Check 2: trades API (authoritative — catches ghost fills)
            if not fill_found:
                fill_found = await self._check_trades_for_fill(order)

            if fill_found:
                continue

            # Not filled — cancel and return capital
            try:
                await self.bot.cancel_order(order.order_id)
            except Exception:
                pass

            cancelled_order = self._order_ledger.mark_cancelled(order.order_id)
            if cancelled_order:
                with self._maker_lock:
                    self._balance += cancelled_order.reserved_usdc
                self.log(
                    f"[MAKER] Cancelled {order.coin} {order.side.upper()} on market change "
                    f"(returned ${cancelled_order.reserved_usdc:.2f})",
                    "info"
                )
            self._order_ledger.remove_order(order.order_id)

    # ========== END GTD MAKER ORDER SYSTEM ==========

    async def _cancel_speculative(self, reason: str = ""):
        """Cancel the current speculative order."""
        if not self._speculative_order:
            return
        spec = self._speculative_order
        try:
            await self.bot.cancel_order(spec.order_id)
        except Exception:
            pass
        # Post-cancel fill check (same pattern as GTC fallback)
        import asyncio as _aio
        await _aio.sleep(1)
        try:
            order_data = await _aio.to_thread(
                self.bot._official_client.get_order, spec.order_id
            )
            matched = float((order_data or {}).get("size_matched", 0))
            if matched > 0:
                self.log(
                    f"[SPEC] {spec.coin} {spec.side.upper()} FILLED {matched} "
                    f"tokens during cancel! Recording position.",
                    "warning"
                )
                await self._record_speculative_fill(spec, matched)
                self._speculative_order = None
                return
        except Exception:
            pass
        self.log(
            f"[SPEC] Cancelled {spec.coin} {spec.side.upper()} @ ${spec.price:.2f}"
            f"{' — ' + reason if reason else ''}",
            "info"
        )
        key = f"{spec.coin}:{spec.side}"
        self._spec_cooldown[key] = time.time()
        self._speculative_order = None

    async def _record_speculative_fill(self, spec: SpeculativeOrder, filled_tokens: float):
        """Record a speculative order fill as a position."""
        state = self.coin_states.get(spec.coin)
        if not state:
            return
        # Maker = zero taker fees
        actual_cost = spec.price * filled_tokens
        if spec.side == "up":
            state.up_tokens += filled_tokens
            state.up_cost += actual_cost
        else:
            state.down_tokens += filled_tokens
            state.down_cost += actual_cost
        self._balance -= actual_cost
        state.last_trade_time = time.time()
        self.stats.trades += 1
        self.stats.pending += 1
        self.stats.total_wagered += actual_cost
        self.stats.opportunities_found += 1
        # Log trade
        spot = self.binance.get_price(spec.coin)
        vol, vol_src = self._get_volatility(spec.coin)
        other_price = state.manager.get_mid_price("down" if spec.side == "up" else "up")
        real_balance = self.bot.get_usdc_balance() or self._balance
        strike_src = getattr(state, '_strike_source', 'unknown')
        mom = (spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0
        self.trade_logger.log_trade(
            market_slug=state.current_slug, coin=spec.coin,
            timeframe=self.config.timeframe, side=spec.side,
            entry_price=spec.price, bet_size_usdc=actual_cost,
            num_tokens=filled_tokens, bankroll=self._balance,
            usdc_balance=real_balance, btc_price=spot,
            other_side_price=other_price, volatility_std=vol,
            time_to_expiry_at_entry=state.seconds_to_expiry(),
            momentum_at_entry=mom, volatility_at_entry=vol,
            signal_to_order_ms=0, order_latency_ms=0, total_latency_ms=0,
            vol_source=vol_src, strike_source=strike_src,
        )
        self.log(
            f"SNIPE {spec.coin} {spec.side.upper()} @ {spec.price:.2f} "
            f"x{filled_tokens:.0f} (${actual_cost:.2f}) MAKER (zero fees) "
            f"mom={mom:+.4f}",
            "success"
        )

    async def _manage_speculative_order(self):
        """Place/monitor/cancel pre-signal GTC maker orders."""
        if self.config.observe_only or not self.config.speculative_enabled:
            return
        import asyncio as _aio

        # --- Check existing speculative order ---
        if self._speculative_order:
            spec = self._speculative_order
            state = self.coin_states.get(spec.coin)

            # Market changed — cancel
            if not state or state.current_slug != spec.market_slug:
                await self._cancel_speculative("market changed")
                return

            # Check CLOB for fill
            try:
                order_data = await _aio.to_thread(
                    self.bot._official_client.get_order, spec.order_id
                )
                matched = float((order_data or {}).get("size_matched", 0))
                clob_status = (order_data or {}).get("status", "")
                if matched > 0:
                    self.log(
                        f"[SPEC FILL] {spec.coin} {spec.side.upper()} "
                        f"{matched} tokens @ ${spec.price:.2f} (maker, zero fees)",
                        "success"
                    )
                    await self._record_speculative_fill(spec, matched)
                    self._speculative_order = None
                    return
                if clob_status in ("CANCELLED", "DEAD", "EXPIRED"):
                    self._speculative_order = None
                    return
            except Exception as e:
                self.log(f"[SPEC] CLOB check failed: {e}", "warning")

            # Momentum reversal — cancel
            spot = self.binance.get_price(spec.coin)
            if state.strike_price > 0:
                disp = (spot - state.strike_price) / state.strike_price
                if spec.side == "up" and disp < 0.0001:
                    await self._cancel_speculative("momentum reversed")
                    return
                if spec.side == "down" and disp > -0.0001:
                    await self._cancel_speculative("momentum reversed")
                    return

            # Timeout — cancel after 20s
            if time.time() - spec.placed_at > 20:
                await self._cancel_speculative("timeout")
                return

            return  # Speculative order still active, don't place another

        # --- Place new speculative order ---
        # Don't place if we have an active position
        active = sum(
            1 for s in self.coin_states.values()
            if s.has_up_position or s.has_down_position
        )
        if active >= self.config.max_concurrent_positions:
            return

        best_edge = 0.0
        best_candidate = None

        for coin, state in self.coin_states.items():
            if not state.current_slug or state.current_slug == state.startup_slug:
                continue
            if not state.strike_price or state.strike_price <= 0:
                continue

            # TTE check
            tte = state.seconds_to_expiry()
            duration = 300 if self.config.timeframe == "5m" else 900
            if self.config.min_window_elapsed > 0:
                if tte > (duration - self.config.min_window_elapsed):
                    continue
            if self.config.max_window_elapsed > 0:
                if tte < (duration - self.config.max_window_elapsed):
                    continue

            fv = self._calculate_fair_value(state)

            spot = self.binance.get_price(coin)
            disp = (spot - state.strike_price) / state.strike_price

            for side in ["up", "down"]:
                # Existing position check
                if side == "up" and (state.has_up_position or state.has_down_position):
                    continue
                if side == "down" and (state.has_down_position or state.has_up_position):
                    continue

                # Pre-signal momentum (lower threshold)
                spec_mom = self.config.speculative_momentum
                if side == "up" and disp < spec_mom:
                    continue
                if side == "down" and disp > -spec_mom:
                    continue

                # Cooldown check (10s after cancel)
                key = f"{coin}:{side}"
                cooldown_until = self._spec_cooldown.get(key, 0)
                if time.time() - cooldown_until < 10:
                    continue

                fair_prob = 0.0  # No FV model
                best_ask = self._get_best_ask_with_rest_fallback(state, side)
                if best_ask <= 0 or best_ask >= 1.0:
                    continue
                if best_ask > self.config.max_entry_price:
                    continue
                if best_ask < self.config.min_entry_price:
                    continue

                edge = 0.0  # No edge calc — pure momentum

                if abs(disp) > abs(best_edge):
                    best_edge = abs(disp)
                    best_candidate = (state, side, best_ask, edge, fv, fair_prob, disp)

        if not best_candidate:
            return

        state, side, ask, edge, fv, fair_prob, disp = best_candidate
        token_id = state.manager.token_ids.get(side)
        if not token_id:
            return

        num_tokens = self.config.min_size_tokens  # min-size
        buy_price = round(ask, 2)

        result = await self.bot.place_order(
            token_id=token_id,
            price=buy_price,
            size=num_tokens,
            side="BUY",
            order_type="GTC",
        )
        if result.success and result.order_id:
            self._speculative_order = SpeculativeOrder(
                coin=state.coin, side=side, order_id=result.order_id,
                token_id=token_id, price=buy_price, size=num_tokens,
                market_slug=state.current_slug, placed_at=time.time(),
                fair_value=fair_prob, displacement=disp,
            )
            self.log(
                f"[SPEC] Placed GTC {state.coin} {side.upper()} @ ${buy_price:.2f} "
                f"x{num_tokens:.0f} edge={edge:.2f} mom={disp:.4f} (maker)",
                "info"
            )

    async def _tick(self):
        """Main strategy tick — scan for opportunities and execute."""
        # Settlement + collector resolution runs ALWAYS (even when paused).
        # The collector must keep resolving pending signals regardless of
        # trading state, and trades need settlement even after pausing.
        asyncio.create_task(self._periodic_settle())
        asyncio.create_task(self._periodic_redeem())

        # PAUSE CHECK: if paused, cancel all maker orders and skip trading
        if self._is_paused():
            if not self._pause_orders_cancelled:
                await self._cancel_all_maker_orders("bot paused")
                self._pause_orders_cancelled = True
            return
        else:
            self._pause_orders_cancelled = False

        # Flush stale shadow records that were never resolved
        if self.shadow_logger:
            self.shadow_logger.flush_stale(max_age_seconds=600)

        # HTTP keepalive + pre-sign orders every 10s
        if not hasattr(self, '_last_keepalive'):
            self._last_keepalive = 0
        if not hasattr(self, '_last_presign'):
            self._last_presign = 0
        now = time.time()

        if now - self._last_keepalive > 3:
            self._last_keepalive = now
            try:
                if self._fast_order:
                    # Keepalive client only — order client warmed by signal loop idle ping
                    asyncio.create_task(self._fast_order.keepalive())
                else:
                    self.bot._official_client.get_server_time()
            except Exception:
                pass

        # Pre-sign orders for all active tokens every 10s (moves signing off critical path)
        if self._fast_order and now - self._last_presign > 10:
            self._last_presign = now
            for coin, state in self.coin_states.items():
                if state.manager.current_market:
                    token_ids = state.manager.token_ids
                    try:
                        asyncio.create_task(self._fast_order.pre_sign_orders(token_ids))
                    except Exception:
                        pass

        # Process pending confirmation signals
        if self.config.confirm_gap > 0:
            await self._process_pending_signals()

        # Pre-signal speculative GTC management (legacy maker orders)
        if self.config.speculative_enabled:
            await self._manage_speculative_order()

        # Process pending UserWS subscriptions from callback thread
        if hasattr(self, '_pending_ws_subs') and self._pending_ws_subs and hasattr(self, '_user_ws') and self._user_ws:
            subs = self._pending_ws_subs[:]
            self._pending_ws_subs.clear()
            try:
                await self._user_ws.subscribe_more(subs)
                self._event_logger.info(f"[USER-WS] Queued subscribe sent for {len(subs)} markets")
            except Exception:
                pass

        # GTD maker order management (Phase 1)
        if self.config.maker_enabled:
            await self._manage_maker_orders()
            await self._poll_maker_orders()

        # Update last known prices for all coins
        for coin, state in self.coin_states.items():
            up = state.manager.get_mid_price("up")
            down = state.manager.get_mid_price("down")
            if up > 0:
                state.last_up_price = up
            if down > 0:
                state.last_down_price = down

        # Balance check — background, don't block signal scanning
        asyncio.create_task(self._async_refresh_balance())
        if self._balance < self.config.min_bet_usdc and not self.config.observe_only:
            return

        # Log VPIN snapshot every 60s for ALL coins (data collection for backtesting)
        if self._vpin_tracker and not hasattr(self, '_last_vpin_log'):
            self._last_vpin_log = 0
        if self._vpin_tracker and time.time() - getattr(self, '_last_vpin_log', 0) > 60:
            self._last_vpin_log = time.time()
            for coin, state in self.coin_states.items():
                if not state.current_slug:
                    continue
                for side in ["up", "down"]:
                    vpin = self._vpin_tracker.get_vpin_by_coin_side(coin, side)
                    flow = self._vpin_tracker.get_flow_by_coin_side(coin, side)
                    if vpin is not None:
                        spot = self.binance.get_price(coin)
                        disp = (spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0
                        self._event_logger.info(
                            f"[VPIN] {coin} {side} vpin={vpin:.2f} flow={flow:+.2f} "
                            f"disp={disp:+.5f} tte={state.seconds_to_expiry():.0f}s "
                            f"slug={state.current_slug}"
                        )

        # Find opportunities across all coins
        opportunities = self._find_opportunities()
        signal_time = time.time()

        # Position limit: count how many coins have open positions right now.
        active_positions = sum(
            1 for s in self.coin_states.values()
            if s.has_up_position or s.has_down_position
        )
        # Count speculative order as a pending position
        if self._speculative_order:
            spec_state = self.coin_states.get(self._speculative_order.coin)
            if spec_state and not (spec_state.has_up_position or spec_state.has_down_position):
                active_positions += 1

        # Count LIVE maker orders as pending positions
        if self.config.maker_enabled:
            for maker_order in self._order_ledger.get_live_orders():
                maker_state = self.coin_states.get(maker_order.coin)
                if maker_state and not (maker_state.has_up_position or maker_state.has_down_position):
                    active_positions += 1

        for state, side, price, edge, fv in opportunities:
            # Skip if speculative order already covers this coin/side
            if (self._speculative_order
                    and self._speculative_order.coin == state.coin
                    and self._speculative_order.side == side
                    and self._speculative_order.market_slug == state.current_slug):
                continue

            # Skip if maker order already covers this coin/side
            if self.config.maker_enabled:
                live_makers = self._order_ledger.get_live_orders_for_coin_side(state.coin, side)
                if live_makers:
                    continue
            # Re-check position — previous execution in this loop may have filled
            if side == "up" and state.has_up_position:
                continue
            if side == "down" and state.has_down_position:
                continue

            # Enforce max concurrent positions across ALL coins.
            if active_positions >= self.config.max_concurrent_positions:
                if not (state.has_up_position or state.has_down_position):
                    # This coin doesn't have an open position — skip it.
                    continue

            if not self.config.observe_only and self._available_balance() < self.config.min_bet_usdc:
                break  # Out of capital

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
                # Update active position count after execution
                if state.has_up_position or state.has_down_position:
                    active_positions += 1

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

            up_mid = state.manager.get_mid_price("up")
            down_mid = state.manager.get_mid_price("down")
            up_ask = self._get_best_ask_with_rest_fallback(state, "up")
            down_ask = self._get_best_ask_with_rest_fallback(state, "down")

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

            # Pure momentum — show ask/mid prices, displacement from strike
            disp = 0.0
            if state.strike_price > 0 and spot > 0:
                disp = (spot - state.strike_price) / state.strike_price
            disp_dir = "UP" if disp > 0 else "DOWN" if disp < 0 else "FLAT"
            lines.append(
                f"       Ask: Up={up_ask:.2f} Down={down_ask:.2f}  |  "
                f"Mid: Up={up_mid:.2f} Down={down_mid:.2f}  |  "
                f"mom={disp:+.4f} ({disp_dir})"
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
        # Build filters string — pure momentum (no edge/FV)
        filters = f"mom>={self.config.min_momentum:.2%}" if self.config.min_momentum > 0 else "mom=any"
        if self.config.fixed_volatility > 0:
            filters += f" | vol=FIXED {self.config.fixed_volatility:.0%}"
        elif self.config.max_volatility > 0:
            filters += f" | vol<{self.config.max_volatility:.2f}"
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
        # Enhanced circuit breaker status
        if self.config.enable_circuit_breaker:
            recent = self._cb_recent_outcomes
            n_recent = len(recent)
            losses_recent = sum(1 for o in recent if not o)
            if self._cb_stopped:
                lines.append(f"  {Colors.RED}CIRCUIT BREAKER STOPPED: {losses_recent}/{n_recent} losses in rolling window — TRADING HALTED{Colors.RESET}")
            elif self._cb_paused_until:
                lines.append(f"  {Colors.YELLOW}CIRCUIT BREAKER PAUSED until {self._cb_paused_until.strftime('%H:%M UTC')}{Colors.RESET}")
            else:
                lines.append(f"  Circuit Breaker: {losses_recent}/{n_recent} losses in rolling window (10)")

        # EMA trend status
        if self._ema_tracker:
            ema_parts = []
            for coin in self.config.coins:
                ema_parts.append(f"{coin}: {self._ema_tracker.trend_str(coin)}")
            lines.append(f"  Trend: {' | '.join(ema_parts)}")

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
            self._loop = asyncio.get_running_loop()

            while self.running:
                await self._tick()
                self._render_status()
                # If a price update flagged an urgent coin, skip the sleep — evaluate NOW
                if hasattr(self, '_urgent_coins') and self._urgent_coins:
                    self._urgent_coins.clear()
                    await asyncio.sleep(0.01)  # 10ms yield (let event loop process)
                else:
                    await asyncio.sleep(0.1)  # 100ms normal scan

        except KeyboardInterrupt:
            self.log("Stopped by user")
        finally:
            await self.stop()
            self._print_summary()

    async def stop(self):
        """Stop the strategy."""
        self.running = False

        # CRITICAL: Cancel ALL open orders on the CLOB before shutting down.
        # Without this, GTD orders remain live on the CLOB after the bot dies,
        # can fill undetected (ghost fills), and drain the balance.
        if self.config.maker_enabled and hasattr(self, 'bot') and self.bot and self.bot._official_client:
            try:
                self.log("[SHUTDOWN] Cancelling all open orders on CLOB...", "warning")
                result = self.bot._official_client.cancel_all()
                cancelled = result.get("canceled", []) if isinstance(result, dict) else []
                self.log(f"[SHUTDOWN] Cancelled {len(cancelled)} orders", "warning")
            except Exception as e:
                self.log(f"[SHUTDOWN] cancel_all failed: {e}", "error")

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
