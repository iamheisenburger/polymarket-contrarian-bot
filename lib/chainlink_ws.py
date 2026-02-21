"""
Chainlink Price Feed via Polymarket RTDS WebSocket.

Provides real-time Chainlink oracle prices — the SAME price feed Polymarket
uses for settlement. Using this instead of Binance eliminates the
Binance-vs-Chainlink divergence that caused 35% settlement misclassification.

Usage:
    from lib.chainlink_ws import ChainlinkPriceFeed

    feed = ChainlinkPriceFeed(coins=["BTC", "ETH", "SOL", "XRP"])
    await feed.start()

    price = feed.get_price("BTC")       # Latest Chainlink price
    vol = feed.get_volatility("BTC")     # Annualized vol from Chainlink data

    await feed.stop()
"""

import asyncio
import json
import math
import time
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, Optional, Deque

logger = logging.getLogger(__name__)

# Polymarket RTDS WebSocket URL
RTDS_WS_URL = "wss://ws-live-data.polymarket.com"

# Coin to Chainlink symbol mapping (slash-separated)
CHAINLINK_SYMBOLS = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "SOL": "sol/usd",
    "XRP": "xrp/usd",
}

# Reverse mapping for fast lookup
SYMBOL_TO_COIN = {v: k for k, v in CHAINLINK_SYMBOLS.items()}

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


class ChainlinkPriceFeed:
    """
    Real-time price feed from Chainlink via Polymarket RTDS.

    Connects to Polymarket's WebSocket and subscribes to the
    crypto_prices_chainlink topic. This is the SAME price source
    Polymarket uses for settlement — no more Binance divergence.
    """

    def __init__(
        self,
        coins: list[str] | None = None,
        vol_window_seconds: int = 300,
        vol_sample_interval: float = 5.0,
        vol_recalc_interval: float = 10.0,
    ):
        self.coins = [c.upper() for c in (coins or ["BTC"])]
        self.vol_window_seconds = vol_window_seconds
        self.vol_sample_interval = vol_sample_interval
        self.vol_recalc_interval = vol_recalc_interval

        self._state: Dict[str, CoinState] = {}
        for coin in self.coins:
            sym = CHAINLINK_SYMBOLS.get(coin)
            if not sym:
                raise ValueError(f"Unsupported coin: {coin}. Use: {list(CHAINLINK_SYMBOLS.keys())}")
            self._state[coin] = CoinState(symbol=sym)

        self._last_sample: Dict[str, float] = {c: 0.0 for c in self.coins}
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._running = False

    @property
    def connected(self) -> bool:
        return self._running and self._ws is not None

    def get_price(self, coin: str = "BTC") -> float:
        """Get latest Chainlink price for a coin. Returns 0.0 if unavailable."""
        state = self._state.get(coin.upper())
        return state.price if state else 0.0

    def get_age(self, coin: str = "BTC") -> float:
        """Get seconds since last price update."""
        state = self._state.get(coin.upper())
        if not state or state.last_update == 0:
            return float("inf")
        return time.time() - state.last_update

    def get_volatility(self, coin: str = "BTC") -> float:
        """Get annualized realized volatility from Chainlink price history."""
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
        """Return price change % over last N seconds. Positive = price going up."""
        coin = coin.upper()
        state = self._state.get(coin)
        if not state or not state.history:
            return 0.0

        current_price = state.price
        if current_price <= 0:
            return 0.0

        cutoff = time.time() - lookback_seconds
        past_price = None
        for point in state.history:
            if point.timestamp <= cutoff:
                past_price = point.price
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
            return 0.50

        returns = []
        for i in range(1, len(points)):
            if points[i - 1].price > 0 and points[i].price > 0:
                dt = points[i].timestamp - points[i - 1].timestamp
                if dt > 0:
                    log_ret = math.log(points[i].price / points[i - 1].price)
                    returns.append((log_ret, dt))

        if len(returns) < 5:
            return 0.50

        sum_sq = sum(r * r for r, _ in returns)
        sum_dt = sum(dt for _, dt in returns)

        if sum_dt <= 0:
            return 0.50

        var_per_sec = sum_sq / sum_dt
        annualized_vol = math.sqrt(var_per_sec * SECONDS_PER_YEAR)
        return max(0.10, min(2.0, annualized_vol))

    async def start(self) -> bool:
        """Start the WebSocket connection."""
        if self._running:
            return True

        self._running = True
        self._task = asyncio.create_task(self._run_loop())

        # Wait for first price (Chainlink updates slower than Binance)
        for _ in range(100):  # 10 seconds max
            await asyncio.sleep(0.1)
            if all(self._state[c].price > 0 for c in self.coins):
                return True

        logger.warning("ChainlinkPriceFeed: timeout waiting for initial prices")
        return self._running

    async def stop(self):
        """Stop the WebSocket connection."""
        self._running = False
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _ping_loop(self, ws):
        """Send PING every 5 seconds to keep connection alive (per RTDS docs)."""
        try:
            while self._running:
                await asyncio.sleep(5)
                try:
                    await ws.ping()
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

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
                logger.info(f"ChainlinkPriceFeed connecting to {RTDS_WS_URL}")

                async with ws_connect(RTDS_WS_URL) as ws:
                    self._ws = ws

                    # Subscribe to Chainlink crypto prices
                    subscribe_msg = json.dumps({
                        "action": "subscribe",
                        "subscriptions": [{
                            "topic": "crypto_prices_chainlink",
                            "type": "*",
                            "filters": "",
                        }]
                    })
                    await ws.send(subscribe_msg)
                    logger.info(f"ChainlinkPriceFeed subscribed: {self.coins}")

                    # Start ping loop
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))

                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(msg)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"ChainlinkPriceFeed error: {e}, reconnecting in 3s")
                self._ws = None
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None
                await asyncio.sleep(3)

        self._ws = None

    def _handle_message(self, raw: str):
        """Process incoming RTDS message."""
        try:
            data = json.loads(raw)

            if data.get("topic") != "crypto_prices_chainlink":
                return

            payload = data.get("payload", {})
            symbol = payload.get("symbol", "")  # e.g., "btc/usd"
            value = payload.get("value")
            ts_ms = payload.get("timestamp", 0)

            if not symbol or value is None:
                return

            price = float(value)
            ts = ts_ms / 1000.0 if ts_ms > 1e12 else float(ts_ms)

            coin = SYMBOL_TO_COIN.get(symbol)
            if coin and coin in self._state:
                state = self._state[coin]
                state.price = price
                state.last_update = ts if ts > 0 else time.time()

                # Sample for volatility calculation
                sample_ts = state.last_update
                if sample_ts - self._last_sample.get(coin, 0) >= self.vol_sample_interval:
                    state.history.append(PricePoint(price=price, timestamp=sample_ts))
                    self._last_sample[coin] = sample_ts

        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
