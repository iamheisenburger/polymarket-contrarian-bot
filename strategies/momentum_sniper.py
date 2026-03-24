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
from datetime import datetime, timezone, timedelta

from lib.binance_ws import BinancePriceFeed
from lib.coinbase_ws import CoinbasePriceFeed, COINBASE_SYMBOLS
from lib.fair_value import BinaryFairValue, FairValue
from lib.direct_fv import DirectFairValue
from lib.console import Colors, LogBuffer, log
from lib.trade_logger import TradeLogger
from lib.signal_logger import SignalLogger, SignalRecord
from lib.shadow_logger import ShadowLogger
from lib.market_manager import MarketManager, MarketInfo
from src.bot import TradingBot, OrderResult
from src.websocket_client import MarketWebSocket, OrderbookSnapshot
from src.gamma_client import GammaClient

# Coins that use Coinbase instead of Binance for price data
COINBASE_COINS = set(COINBASE_SYMBOLS.keys())  # {"HYPE"}


class MultiPriceFeed:
    """
    Routes price queries to the correct source per coin.

    HYPE -> CoinbasePriceFeed (not on Binance)
    Everything else -> BinancePriceFeed (or ChainlinkPriceFeed)

    Exposes the same interface as BinancePriceFeed so the strategy
    doesn't need any per-call routing logic.
    """

    def __init__(self, primary_feed, coinbase_feed=None):
        """
        Args:
            primary_feed: BinancePriceFeed or ChainlinkPriceFeed for most coins
            coinbase_feed: CoinbasePriceFeed for HYPE (None if HYPE not in coin list)
        """
        self._primary = primary_feed
        self._coinbase = coinbase_feed

    def _feed_for(self, coin: str):
        """Return the correct feed for a given coin."""
        if self._coinbase and coin.upper() in COINBASE_COINS:
            return self._coinbase
        return self._primary

    @property
    def connected(self) -> bool:
        ok = self._primary.connected
        if self._coinbase:
            ok = ok and self._coinbase.connected
        return ok

    def get_price(self, coin: str = "BTC") -> float:
        return self._feed_for(coin).get_price(coin)

    def get_age(self, coin: str = "BTC") -> float:
        return self._feed_for(coin).get_age(coin)

    def get_volatility(self, coin: str = "BTC") -> float:
        return self._feed_for(coin).get_volatility(coin)

    def get_momentum(self, coin: str, lookback_seconds: float = 30.0) -> float:
        return self._feed_for(coin).get_momentum(coin, lookback_seconds)

    async def start(self) -> bool:
        ok = await self._primary.start()
        if self._coinbase:
            ok2 = await self._coinbase.start()
            ok = ok and ok2
        return ok

    async def stop(self):
        await self._primary.stop()
        if self._coinbase:
            await self._coinbase.stop()


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

    # Pre-signal speculative GTC: place maker orders before signal fully confirms.
    # When momentum crosses speculative_momentum (lower than min_momentum), a GTC
    # rests on the book. If signal confirms, we're already in (zero fees, no race).
    # If momentum reverses, the GTC is cancelled.
    speculative_enabled: bool = False
    speculative_momentum: float = 0.0003  # Pre-signal momentum threshold

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

    # Position limits: prevent deploying entire bankroll simultaneously.
    # max_concurrent_positions=1 means only 1 open trade at a time across ALL coins.
    # This prevents the V6 failure mode where 9 trades fired in 6 minutes.
    max_concurrent_positions: int = 1

    # Market settings
    market_check_interval: float = 30.0

    # Price source for fair value calculation.
    # "binance" = Binance WebSocket (fast but NOT settlement source)
    # "chainlink" = Polymarket RTDS Chainlink feed (settlement source)
    price_source: str = "binance"

    # Direct fair value: use empirical sigmoid model instead of Black-Scholes.
    # No volatility assumption — just price gap + time remaining.
    # Calibrate first with: python apps/calibrate_fv.py --trade-csv <csv> --output data/calibration.json
    use_direct_fv: bool = False
    # Path to calibration JSON for DirectFairValue (empty = use defaults)
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

    # Number of FOK retry steps after initial rejection. Each step adds +1c.
    # With tolerance=0.04 and retries=3: tries at +4c, +5c, +6c, +7c.
    fok_retry_steps: int = 3

    # Enhanced circuit breaker: rolling window of last 10 trade outcomes.
    # - 3 consecutive losses -> pause trading for 1 hour
    # - 5 losses out of last 10 -> stop live trading entirely
    # Cannot be disabled by any automated system — only by explicit CLI flag.
    enable_circuit_breaker: bool = True



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
    """A pre-signal GTC resting on the book as maker."""
    coin: str
    side: str
    order_id: str
    token_id: str
    price: float
    size: float
    market_slug: str
    placed_at: float
    fair_value: float
    displacement: float


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
        if config.use_direct_fv:
            cal_path = config.direct_fv_calibration or None
            self.fv_calc = DirectFairValue(calibration_path=cal_path)
            self._direct_fv_msg = f"Using DirectFairValue (empirical sigmoid){' with calibration: ' + cal_path if cal_path else ' with defaults'}"
        else:
            self.fv_calc = BinaryFairValue()
            self._direct_fv_msg = None

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

        # If only Coinbase coins (e.g. only HYPE), coinbase IS the primary
        if primary_feed is None and coinbase_feed is not None:
            self.binance = coinbase_feed
        elif coinbase_feed is not None:
            self.binance = MultiPriceFeed(primary_feed, coinbase_feed)
        else:
            self.binance = primary_feed

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

        # Deferred log messages from before buffer was ready
        if self._direct_fv_msg:
            self.log(self._direct_fv_msg, "info")

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

        # Pre-signal speculative GTC order (only one at a time)
        self._speculative_order: Optional[SpeculativeOrder] = None
        self._spec_cooldown: Dict[str, float] = {}  # coin:side -> time of last cancel

        # Circuit breaker: consecutive losing WINDOWS (not individual trades)
        # 3 coins losing in the same 5m window = 1 losing window, not 3 losses
        self._consecutive_loss_windows: int = 0
        self._last_loss_window: str = ""  # slug window timestamp of last counted loss
        self._circuit_breaker_tripped: bool = False

        # Enhanced circuit breaker: rolling window of recent trade outcomes
        self._cb_recent_outcomes: List[bool] = []  # last 10 trade outcomes (True=win)
        self._cb_paused_until: Optional[datetime] = None  # 1-hour pause after 3 consecutive losses
        self._cb_stopped: bool = False  # 5/10 losses -> hard stop

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
            """Called on EVERY Binance/Coinbase price tick — instant signal detection.
            Bypasses the tick loop entirely for maximum speed."""
            if not self.running or self.config.observe_only:
                return
            state = self.coin_states.get(coin)
            if not state or not state.strike_price or state.strike_price <= 0:
                return
            disp = (price - state.strike_price) / state.strike_price
            if abs(disp) < self.config.min_momentum:
                return

            # Check if we already have a position (max concurrent)
            active = sum(1 for s in self.coin_states.values() if s.has_up_position or s.has_down_position)
            if active >= self.config.max_concurrent_positions:
                return

            # Quick filter: side, ask, edge
            side = "up" if disp > 0 else "down"
            best_ask = state.manager.get_best_ask(side)
            if best_ask <= 0 or best_ask > self.config.max_entry_price:
                return

            fv = self._calculate_fair_value(state)
            if not fv:
                return
            fair_prob = fv.fair_up if side == "up" else fv.fair_down
            if fair_prob < self.config.min_fair_value:
                return

            worst_fill = min(round(best_ask + self.config.fok_tolerance, 2), self.config.max_entry_price)
            fee = 0.005
            edge = fair_prob - worst_fill - fee
            if edge < self.config.min_edge:
                return

            # TTE check
            tte = state.seconds_to_expiry()
            duration = 300
            min_tte = duration - self.config.max_window_elapsed
            max_tte = duration - self.config.min_window_elapsed
            if tte < min_tte or tte > max_tte:
                return

            # Flag for tick loop (fallback)
            if not hasattr(self, '_urgent_coins'):
                self._urgent_coins = set()
            self._urgent_coins.add(coin)

            # FAST FIRE: submit order directly in FastOrder's thread — zero asyncio
            if self._fast_order:
                token_id = state.manager.token_ids.get(side)
                if not token_id:
                    return

                # Balance check
                if self._balance < 1.0:
                    return

                # Check position + de-dup
                if side == "up" and state.has_up_position:
                    return
                if side == "down" and state.has_down_position:
                    return
                snipe_key = f"{state.current_slug}:{side}"
                if not hasattr(self, '_snipe_in_flight'):
                    self._snipe_in_flight = set()
                if snipe_key in self._snipe_in_flight:
                    return
                self._snipe_in_flight.add(snipe_key)

                buy_price = min(round(best_ask + self.config.fok_tolerance, 2), self.config.max_entry_price)
                signal_time = time.time()

                def _fast_fire():
                    """Runs entirely in FastOrder's dedicated thread."""
                    try:
                        result = self._fast_order._place_order_sync(
                            token_id, buy_price, 5.0, "BUY", "FAK", 1000,
                        )
                        # Queue bookkeeping back to asyncio
                        try:
                            loop = asyncio.get_event_loop()
                            loop.call_soon_threadsafe(
                                lambda: asyncio.ensure_future(
                                    self._handle_fast_fire_result(
                                        state, side, buy_price, edge, fv,
                                        result, signal_time,
                                    )
                                )
                            )
                        except Exception:
                            pass
                    except Exception as e:
                        self._event_logger.warning(f"[FAST-FIRE] {coin} {side} error: {e}")
                    finally:
                        self._snipe_in_flight.discard(snipe_key)

                self._fast_order._executor.submit(_fast_fire)
                return  # Don't also fire via tick loop

            # Fallback: fire via asyncio (slower path)
            try:
                asyncio.get_event_loop().create_task(
                    self._execute_snipe(state, side, worst_fill, edge, fv, signal_time=time.time())
                )
            except Exception:
                pass

        if hasattr(self.binance, 'on_price'):
            self.binance.on_price(_on_price_update)
        # For MultiPriceFeed, try to register on sub-feeds
        if hasattr(self.binance, '_primary') and hasattr(self.binance._primary, 'on_price'):
            self.binance._primary.on_price(_on_price_update)
        if hasattr(self.binance, '_secondary') and hasattr(self.binance._secondary, 'on_price'):
            self.binance._secondary.on_price(_on_price_update)

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

        # If require_vatic is set and Vatic failed, skip this market entirely
        # (Binance/backsolve strikes can be wrong, especially near the money)
        if strike_source == "unknown" and self.config.require_vatic:
            self.log(f"{state.coin} Vatic strike unavailable — skipping market (require_vatic=True)", "warning")
            state.strike_price = 0
            state._strike_source = "skipped"
            return

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

            # Retry Vatic if strike was not set (Vatic may not have had the
            # target ready at market discovery time — retry each scan cycle)
            _require_vatic = self.config.require_vatic
            if state.strike_price <= 0 and _require_vatic and self._vatic:
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

                fair_prob = fv.fair_up if side == "up" else fv.fair_down

                # Get best ask (cheapest price we can buy at)
                best_ask = state.manager.get_best_ask(side)
                if best_ask <= 0 or best_ask >= 1.0:
                    continue

                # Calculate edge at WORST-CASE fill price (ask + tolerance + GTC bump).
                # GTC fallback adds +$0.03 beyond tolerance. The HYPE -$3.07 loss
                # filled at ask+$0.07 when edge was only checked at ask+$0.04.
                buy_price = round(best_ask, 2)
                worst_fill = round(buy_price + self.config.fok_tolerance + 0.03, 2)
                if worst_fill > self.config.max_entry_price:
                    worst_fill = self.config.max_entry_price

                # Net edge = fair value - worst fill price - taker fee
                fee = taker_fee_per_token(worst_fill, self.config.timeframe)
                edge = fair_prob - worst_fill - fee

                # --- Signal logging: evaluate all filters and log before filtering ---
                if self.signal_logger:
                    spot_for_signal = self.binance.get_price(state.coin)
                    vol_for_signal, vol_src_for_signal = self._get_volatility(state.coin)
                    strike_src_for_signal = getattr(state, '_strike_source', 'unknown')

                    # Evaluate each filter independently (per-coin aware)
                    _c_max_entry = self.config.max_entry_price
                    _c_min_entry = self.config.min_entry_price
                    _c_min_edge = self.config.min_edge
                    _c_min_fv = self.config.min_fair_value
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
                        fair_value=fair_prob,
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
                        passed_fv_filter=_passed_fv,
                        passed_vatic_filter=_passed_vatic,
                        passed_trend_filter=_passed_trend,
                    )
                    self.signal_logger.log_signal(signal)

                # --- Filter chain (global config, no per-coin overrides) ---

                coin_max_entry = self.config.max_entry_price
                coin_min_entry = self.config.min_entry_price
                coin_min_edge = self.config.min_edge
                coin_min_fv = self.config.min_fair_value
                coin_min_mom = self.config.min_momentum
                coin_require_vatic = self.config.require_vatic

                # Structural price filters only (not risk limits)
                if best_ask > coin_max_entry:
                    continue
                if best_ask < coin_min_entry:
                    continue

                # Never buy the opposite side of an existing position.
                # Kelly sizes each side independently, so token counts differ
                # and buying both sides is NOT a guaranteed arb — it's a coin
                # flip on which side has more tokens. Just pick a direction.
                if side == "up" and state.has_down_position:
                    continue
                if side == "down" and state.has_up_position:
                    continue

                if edge >= coin_min_edge:
                    # Fair value confidence filter: skip coin-flip trades
                    if fair_prob < coin_min_fv:
                        continue  # Model not confident enough

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
                            fair_value=fair_prob,
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
        fair_prob = fv.fair_up if side == "up" else fv.fair_down
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
        fv: FairValue,
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
        fv: FairValue,
        signal_time: float = 0.0,
    ) -> bool:
        """Inner snipe execution (guarded by _execute_snipe)."""
        if self.config.observe_only:
            # Paper trading with realistic simulation
            fair_prob = fv.fair_up if side == "up" else fv.fair_down
            num_tokens = 5.0
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
            # Balance floor: preserve capital
            if self.config.balance_floor > 0 and self._available_balance() - bet_usdc < self.config.balance_floor:
                self.log(f"BALANCE FLOOR: ${self._available_balance():.2f} - ${bet_usdc:.2f} would drop below ${self.config.balance_floor:.2f} floor. Skipping.", "warning")
                return False

        if num_tokens < 5.0:
            # Not enough for Polymarket minimum order size
            return False

        # Get token ID
        token_id = state.manager.token_ids.get(side)
        if not token_id:
            return False

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

        # DUAL EXECUTION: FastOrderClient (fast) + SDK (reliable fallback)
        # FastOrder fires first for speed. SDK always fires as backup.
        # Both results logged for side-by-side comparison to diagnose FastOrder issues.
        fast_success = False
        if self._fast_order:
            try:
                fast_result = await self._fast_order.place_order(
                    token_id=token_id, price=buy_price,
                    size=num_tokens, side="BUY", order_type="FAK",
                )
                fast_success = fast_result.get("success", False)
                fast_order_id = fast_result.get("orderID", "")
                self.log(
                    f"[FAST] {state.coin} {side.upper()} success={fast_success} "
                    f"id={fast_order_id[:16] if fast_order_id else 'none'} "
                    f"raw={str(fast_result)[:150]}",
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

        # SDK fallback — always runs if FastOrder didn't succeed
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
                momentum_at_entry=(spot - state.strike_price) / state.strike_price if state.strike_price > 0 else 0,
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

        pending = self.trade_logger.get_pending_trades()
        if not pending:
            return

        from src.gamma_client import GammaClient
        gamma = GammaClient()
        real_balance = await asyncio.to_thread(self.bot.get_usdc_balance) or self._balance

        for trade_key, record in list(pending.items()):
            try:
                market_data = await asyncio.to_thread(gamma.get_market_by_slug, record.market_slug)
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

                if up_price > 0.9:
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

        # Clear signal logger dedup for old market
        if self.signal_logger and old_slug:
            self.signal_logger.clear_slug(old_slug)

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

            _coin_min_edge = self.config.min_edge
            if edge >= _coin_min_edge:
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
            fair_value_at_entry=spec.fair_value,
            time_to_expiry_at_entry=state.seconds_to_expiry(),
            momentum_at_entry=mom, volatility_at_entry=vol,
            signal_to_order_ms=0, order_latency_ms=0, total_latency_ms=0,
            vol_source=vol_src, strike_source=strike_src,
        )
        self.log(
            f"SNIPE {spec.coin} {spec.side.upper()} @ {spec.price:.2f} "
            f"x{filled_tokens:.0f} (${actual_cost:.2f}) MAKER (zero fees) "
            f"FV={spec.fair_value:.2f}",
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
            if not fv:
                continue

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

                fair_prob = fv.fair_up if side == "up" else fv.fair_down
                best_ask = state.manager.get_best_ask(side)
                if best_ask <= 0 or best_ask >= 1.0:
                    continue
                if best_ask > self.config.max_entry_price:
                    continue
                if best_ask < self.config.min_entry_price:
                    continue

                # Edge at ask (no tolerance — maker fills at ask, zero fees)
                fee = taker_fee_per_token(best_ask, self.config.timeframe)
                edge = fair_prob - best_ask - fee

                # Lower thresholds for speculative orders: we're placing EARLY
                # when momentum is building. If it fills and momentum continues,
                # the full signal would have confirmed anyway. If momentum
                # reverses, we cancel before fill. The cancel-on-reversal is
                # the risk control, not the entry threshold.
                spec_min_edge = 0.08
                spec_min_fv = 0.58
                if edge < spec_min_edge:
                    continue
                if fair_prob < spec_min_fv:
                    continue

                if edge > best_edge:
                    best_edge = edge
                    best_candidate = (state, side, best_ask, edge, fv, fair_prob, disp)

        if not best_candidate:
            return

        state, side, ask, edge, fv, fair_prob, disp = best_candidate
        token_id = state.manager.token_ids.get(side)
        if not token_id:
            return

        num_tokens = 5.0  # min-size
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
        # Periodic settlement + redemption — run in background so they don't block trading
        asyncio.create_task(self._periodic_settle())
        asyncio.create_task(self._periodic_redeem())

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

        # Pre-signal speculative GTC management (maker orders)
        if self.config.speculative_enabled:
            await self._manage_speculative_order()

        # Update last known prices for all coins
        for coin, state in self.coin_states.items():
            up = state.manager.get_mid_price("up")
            down = state.manager.get_mid_price("down")
            if up > 0:
                state.last_up_price = up
            if down > 0:
                state.last_down_price = down

        # Balance check (non-blocking)
        await self._async_refresh_balance()
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

        for state, side, price, edge, fv in opportunities:
            # Skip if speculative order already covers this coin/side
            if (self._speculative_order
                    and self._speculative_order.coin == state.coin
                    and self._speculative_order.side == side
                    and self._speculative_order.market_slug == state.current_slug):
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
