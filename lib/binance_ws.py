"""
Binance WebSocket Price Feed — Real-time crypto prices for market making.

Provides sub-second BTC/ETH/SOL price updates via Binance WebSocket streams.
Also calculates rolling realized volatility for fair value pricing.

Usage:
    from lib.binance_ws import BinancePriceFeed

    feed = BinancePriceFeed(coins=["BTC", "ETH", "SOL"])
    await feed.start()

    price = feed.get_price("BTC")       # Latest price
    vol = feed.get_volatility("BTC")     # Annualized vol (e.g., 0.50 = 50%)

    await feed.stop()
"""

import asyncio
import json
import orjson  # fast JSON parse on WS hot path
import math
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Deque

logger = logging.getLogger(__name__)

# Binance WebSocket stream URL
BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"

# Coin to Binance symbol mapping
COIN_SYMBOLS = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "SOL": "solusdt",
    "XRP": "xrpusdt",
    "DOGE": "dogeusdt",
    "BNB": "bnbusdt",
}

# Seconds in a year for annualization
SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass
class PricePoint:
    """A single price observation."""
    price: float
    timestamp: float  # Unix timestamp


@dataclass
class CoinState:
    """Tracks price and volatility state for a single coin."""
    symbol: str
    price: float = 0.0
    last_update: float = 0.0
    # Rolling price history for volatility calculation
    history: Deque[PricePoint] = field(default_factory=lambda: deque(maxlen=600))
    # Cached volatility (recalculated periodically)
    _cached_vol: float = 0.50  # Default 50% annualized
    _vol_calc_time: float = 0.0


class BinancePriceFeed:
    """
    Real-time price feed from Binance WebSocket.

    Connects to Binance's trade stream for sub-second price updates.
    Calculates rolling realized volatility from trade prices.
    """

    def __init__(
        self,
        coins: list[str] | None = None,
        vol_window_seconds: int = 300,
        vol_sample_interval: float = 5.0,
        vol_recalc_interval: float = 10.0,
    ):
        """
        Args:
            coins: List of coin symbols (default: ["BTC"])
            vol_window_seconds: Window for volatility calculation (default: 5 min)
            vol_sample_interval: Seconds between price samples for vol calc
            vol_recalc_interval: How often to recalculate volatility
        """
        self.coins = [c.upper() for c in (coins or ["BTC"])]
        self.vol_window_seconds = vol_window_seconds
        self.vol_sample_interval = vol_sample_interval
        self.vol_recalc_interval = vol_recalc_interval

        # State per coin
        self._state: Dict[str, CoinState] = {}
        for coin in self.coins:
            sym = COIN_SYMBOLS.get(coin)
            if not sym:
                raise ValueError(f"Unsupported coin: {coin}. Use: {list(COIN_SYMBOLS.keys())}")
            self._state[coin] = CoinState(symbol=sym)

        # Last sample time per coin (for vol sampling)
        self._last_sample: Dict[str, float] = {c: 0.0 for c in self.coins}

        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._price_callbacks: list = []  # Called on every price update for instant signal detection

    def on_price(self, callback) -> None:
        """Register a callback for every price update. callback(coin: str, price: float)."""
        self._price_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        return self._running and self._ws is not None

    def get_price(self, coin: str = "BTC") -> float:
        """Get latest price for a coin. Returns 0.0 if unavailable."""
        state = self._state.get(coin.upper())
        return state.price if state else 0.0

    def get_age(self, coin: str = "BTC") -> float:
        """Get seconds since last price update."""
        state = self._state.get(coin.upper())
        if not state or state.last_update == 0:
            return float("inf")
        return time.time() - state.last_update

    def get_volatility(self, coin: str = "BTC") -> float:
        """
        Get annualized realized volatility for a coin.

        Calculated from rolling price samples. Returns default (0.50)
        if insufficient data.
        """
        coin = coin.upper()
        state = self._state.get(coin)
        if not state:
            return 0.50

        now = time.time()
        if now - state._vol_calc_time > self.vol_recalc_interval:
            state._cached_vol = self._calculate_volatility(coin)
            state._vol_calc_time = now

        return state._cached_vol

    def get_momentum(self, coin: str, lookback_seconds: float = 30.0) -> float:
        """
        Return price change % over last N seconds. Positive = price going up.

        Used as a momentum filter: only enter trades when Binance price
        is moving in the direction of our bet.
        """
        coin = coin.upper()
        state = self._state.get(coin)
        if not state or not state.history:
            return 0.0

        current_price = state.price
        if current_price <= 0:
            return 0.0

        # Find the most recent price at or before the cutoff time
        cutoff = time.time() - lookback_seconds
        past_price = None
        for point in state.history:
            if point.timestamp <= cutoff:
                past_price = point.price
        # If no point old enough, use the oldest available
        if past_price is None and state.history:
            past_price = state.history[0].price
        if not past_price or past_price <= 0:
            return 0.0

        return (current_price - past_price) / past_price

    def _calculate_volatility(self, coin: str) -> float:
        """Calculate annualized realized volatility from price history."""
        state = self._state[coin]
        points = list(state.history)

        if len(points) < 10:
            return 0.50  # Default: 50% annualized

        # Calculate log returns between consecutive samples
        returns = []
        for i in range(1, len(points)):
            if points[i - 1].price > 0 and points[i].price > 0:
                dt = points[i].timestamp - points[i - 1].timestamp
                if dt > 0:
                    log_ret = math.log(points[i].price / points[i - 1].price)
                    returns.append((log_ret, dt))

        if len(returns) < 5:
            return 0.50

        # Variance of returns, annualized
        # Use sum of squared returns / sum of dt, then annualize
        sum_sq = sum(r * r for r, _ in returns)
        sum_dt = sum(dt for _, dt in returns)

        if sum_dt <= 0:
            return 0.50

        # Variance per second, then annualize
        var_per_sec = sum_sq / sum_dt
        annualized_vol = math.sqrt(var_per_sec * SECONDS_PER_YEAR)

        # Clamp to reasonable range
        return max(0.10, min(2.0, annualized_vol))

    async def start(self) -> bool:
        """Start the WebSocket connection."""
        if self._running:
            return True

        self._running = True
        self._task = asyncio.create_task(self._run_loop())

        # Wait for first price
        for _ in range(50):  # 5 seconds max
            await asyncio.sleep(0.1)
            if all(self._state[c].price > 0 for c in self.coins):
                return True

        logger.warning("BinancePriceFeed: timeout waiting for initial prices")
        return self._running

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run_loop(self):
        """Main WebSocket loop with auto-reconnect."""
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            try:
                import websockets
                ws_connect = websockets.connect
            except ImportError:
                logger.error("websockets package required: pip install websockets")
                self._running = False
                return

        while self._running:
            try:
                # Build combined stream URL — aggTrade + bookTicker for faster detection
                agg_streams = [f"{COIN_SYMBOLS[c]}@aggTrade" for c in self.coins]
                book_streams = [f"{COIN_SYMBOLS[c]}@bookTicker" for c in self.coins]
                streams = agg_streams + book_streams
                url = f"wss://stream.binance.com:9443/stream?streams={'/'.join(streams)}"

                logger.info(f"BinancePriceFeed connecting: {self.coins}")

                async with ws_connect(url) as ws:
                    self._ws = ws
                    logger.info("BinancePriceFeed connected")

                    _msg_count = 0
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(msg)
                        # Yield to event loop every 50 messages to prevent
                        # starvation of Polymarket WS tasks.  Without this,
                        # the high-frequency aggTrade stream keeps the
                        # websockets recv buffer non-empty and `await recv()`
                        # resolves synchronously, so this task never yields.
                        _msg_count += 1
                        if _msg_count % 50 == 0:
                            await asyncio.sleep(0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"BinancePriceFeed error: {e}, reconnecting in 2s")
                self._ws = None
                await asyncio.sleep(2)

        self._ws = None

    def _handle_message(self, raw: str):
        """Process incoming WebSocket message (aggTrade + bookTicker)."""
        try:
            data = orjson.loads(raw)

            # Combined stream format wraps data
            if "stream" in data:
                data = data["data"]

            event_type = data.get("e", "")

            if event_type == "aggTrade":
                # Trade executed — update price and volatility sampling
                symbol = data["s"].lower()
                price = float(data["p"])
                ts = data["T"] / 1000.0

                for coin, sym in COIN_SYMBOLS.items():
                    if sym == symbol and coin in self._state:
                        state = self._state[coin]
                        state.price = price
                        state.last_update = ts

                        # Volatility sampling (only on trades, not book changes)
                        if ts - self._last_sample.get(coin, 0) >= self.vol_sample_interval:
                            state.history.append(PricePoint(price=price, timestamp=ts))
                            self._last_sample[coin] = ts

                        # Fire callbacks
                        for cb in self._price_callbacks:
                            try:
                                cb(coin, price)
                            except Exception:
                                pass
                        break

            elif "b" in data and "a" in data and "s" in data:
                # bookTicker — faster price updates from order book changes
                symbol = data["s"].lower()
                bid = float(data["b"])
                ask = float(data["a"])
                if bid <= 0 or ask <= 0:
                    return
                mid = (bid + ask) / 2

                for coin, sym in COIN_SYMBOLS.items():
                    if sym == symbol and coin in self._state:
                        state = self._state[coin]
                        # Only update price if bookTicker mid differs meaningfully
                        # This avoids noisy micro-updates that don't cross momentum threshold
                        if state.price > 0 and abs(mid - state.price) / state.price < 0.00001:
                            break  # less than 0.001% change, skip
                        state.price = mid
                        state.last_update = time.time()

                        # Fire callbacks (same as aggTrade — triggers momentum check)
                        for cb in self._price_callbacks:
                            try:
                                cb(coin, mid)
                            except Exception:
                                pass
                        break

        except (json.JSONDecodeError, KeyError, ValueError):
            pass
