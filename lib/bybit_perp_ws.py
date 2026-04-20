"""
Bybit Perpetual Futures WebSocket v5 price feed.

Provides a separate feed class tracking Bybit linear (USDT-margined) perpetual
futures prices. Research shows perp prices typically lead spot by 50-200ms on
directional moves. Used as a confirmatory lead indicator on top of spot.

Same interface as spot feeds (CoinbasePriceFeed, BybitSpotPriceFeed, etc.) so
it plugs into the same `get_price` / `get_momentum` / `on_price` calls, but
maintained as its OWN feed (not aggregated into spot) because perp price is
not identical to spot — there's a basis.

Supports: BTC, ETH, SOL (the major USDT perps). Coins without USDT perps
(HYPE, XRP on some exchanges) are silently skipped.
"""
import asyncio
import json
import orjson  # fast JSON parse on WS hot path
import logging
import math
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)

BYBIT_PERP_WS_URL = "wss://stream.bybit.com/v5/public/linear"

# USDT perp symbols. Same naming as spot.
BYBIT_PERP_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "BNB": "BNBUSDT",
}
SYMBOL_TO_COIN = {v: k for k, v in BYBIT_PERP_SYMBOLS.items()}

SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass
class PricePoint:
    price: float
    timestamp: float


@dataclass
class CoinState:
    symbol: str
    price: float = 0.0
    last_update: float = 0.0
    history: Deque[PricePoint] = field(default_factory=lambda: deque(maxlen=600))
    _cached_vol: float = 0.50
    _vol_calc_time: float = 0.0
    # Funding rate from Bybit ticker (per 8h). Positive = longs paying shorts =
    # crowded long = potential mean-reversion / top risk. Negative = inverse.
    funding_rate: float = 0.0
    funding_update_ts: float = 0.0


class BybitPerpsPriceFeed:
    """Bybit linear USDT perp feed. Sister class to BybitSpotPriceFeed.
    Interface mirrors CoinbasePriceFeed so it plugs into the same utilities."""

    def __init__(
        self,
        coins: list[str] | None = None,
        vol_window_seconds: int = 300,
        vol_sample_interval: float = 5.0,
        vol_recalc_interval: float = 10.0,
    ):
        requested = [c.upper() for c in (coins or ["BTC"])]
        self.coins = [c for c in requested if c in BYBIT_PERP_SYMBOLS]
        skipped = [c for c in requested if c not in BYBIT_PERP_SYMBOLS]
        if skipped:
            logger.info(f"BybitPerpsPriceFeed: skipping unsupported coins: {skipped}")
        self.vol_window_seconds = vol_window_seconds
        self.vol_sample_interval = vol_sample_interval
        self.vol_recalc_interval = vol_recalc_interval

        self._state: Dict[str, CoinState] = {
            c: CoinState(symbol=BYBIT_PERP_SYMBOLS[c]) for c in self.coins
        }
        self._last_sample: Dict[str, float] = {c: 0.0 for c in self.coins}
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._running = False
        self._price_callbacks: list = []

    def on_price(self, callback) -> None:
        self._price_callbacks.append(callback)

    @property
    def connected(self) -> bool:
        return self._running and self._ws is not None

    def get_price(self, coin: str = "BTC") -> float:
        state = self._state.get(coin.upper())
        return state.price if state else 0.0

    def get_age(self, coin: str = "BTC") -> float:
        state = self._state.get(coin.upper())
        if not state or state.last_update == 0:
            return float("inf")
        return time.time() - state.last_update

    def get_volatility(self, coin: str = "BTC") -> float:
        coin = coin.upper()
        state = self._state.get(coin)
        if not state:
            return 0.50
        now = time.time()
        if now - state._vol_calc_time > self.vol_recalc_interval:
            state._cached_vol = self._calculate_volatility(coin)
            state._vol_calc_time = now
        return state._cached_vol

    def get_funding_rate(self, coin: str) -> float:
        """Return most recent funding rate for a coin (fraction per 8h interval).
        0.0 if unknown. Positive = longs paying shorts, negative = inverse."""
        state = self._state.get(coin.upper())
        return state.funding_rate if state else 0.0

    def get_momentum(self, coin: str, lookback_seconds: float = 30.0) -> float:
        coin = coin.upper()
        state = self._state.get(coin)
        if not state or not state.history:
            return 0.0
        current = state.price
        if current <= 0:
            return 0.0
        cutoff = time.time() - lookback_seconds
        past = None
        for p in state.history:
            if p.timestamp <= cutoff:
                past = p.price
        if past is None and state.history:
            past = state.history[0].price
        if not past or past <= 0:
            return 0.0
        return (current - past) / past

    def _calculate_volatility(self, coin: str) -> float:
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
        return math.sqrt(max(var_per_sec, 0.0) * SECONDS_PER_YEAR)

    async def start(self) -> bool:
        if self._running or not self.coins:
            return self._running or False
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        for _ in range(50):
            await asyncio.sleep(0.1)
            if all(self._state[c].price > 0 for c in self.coins):
                return True
        logger.warning("BybitPerpsPriceFeed: timeout waiting for initial prices")
        return self._running

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ping_task:
            self._ping_task.cancel()
            try:
                await self._ping_task
            except asyncio.CancelledError:
                pass
            self._ping_task = None

    async def _ping_loop(self, ws):
        try:
            while self._running:
                await asyncio.sleep(20)
                if not self._running:
                    break
                try:
                    await ws.send(json.dumps({"op": "ping"}))
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    async def _run_loop(self):
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            import websockets
            ws_connect = websockets.connect
        while self._running:
            try:
                logger.info(f"BybitPerpsPriceFeed connecting: {self.coins}")
                async with ws_connect(BYBIT_PERP_WS_URL) as ws:
                    self._ws = ws
                    args = [f"tickers.{BYBIT_PERP_SYMBOLS[c]}" for c in self.coins]
                    sub_msg = json.dumps({"op": "subscribe", "args": args})
                    await ws.send(sub_msg)
                    logger.info("BybitPerpsPriceFeed connected + subscribed")
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    _n = 0
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(msg)
                        _n += 1
                        if _n % 50 == 0:
                            await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"BybitPerpsPriceFeed error: {e}, reconnecting in 2s")
                self._ws = None
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None
                await asyncio.sleep(2)
        self._ws = None

    def _handle_message(self, raw: str):
        try:
            data = orjson.loads(raw)
            topic = data.get("topic", "")
            if not topic.startswith("tickers."):
                return
            payload = data.get("data")
            if not payload:
                return
            entries = payload if isinstance(payload, list) else [payload]
            now = time.time()
            for entry in entries:
                sym = entry.get("symbol", "")
                coin = SYMBOL_TO_COIN.get(sym)
                if not coin or coin not in self._state:
                    continue
                # For perps, prefer lastPrice (actual trade price). markPrice is
                # smoother but lags. For directional signal we want trade-level.
                last_str = entry.get("lastPrice") or entry.get("last") or entry.get("markPrice")
                if last_str is None:
                    continue
                price = float(last_str)
                if price <= 0:
                    continue
                state = self._state[coin]
                state.price = price
                state.last_update = now
                if now - self._last_sample.get(coin, 0) >= self.vol_sample_interval:
                    state.history.append(PricePoint(price=price, timestamp=now))
                    self._last_sample[coin] = now
                # Funding rate: Bybit publishes current funding in linear perp ticker
                fr_str = entry.get("fundingRate")
                if fr_str is not None:
                    try:
                        fr = float(fr_str)
                        state.funding_rate = fr
                        state.funding_update_ts = now
                    except (TypeError, ValueError):
                        pass
                for cb in self._price_callbacks:
                    try:
                        cb(coin, price)
                    except Exception:
                        pass
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
