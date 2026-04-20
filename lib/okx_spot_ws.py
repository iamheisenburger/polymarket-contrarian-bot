"""
OKX Spot WebSocket v5 price feed. Same interface as CoinbasePriceFeed for
drop-in use in MultiPriceFeed aggregation.

OKX public WS v5 endpoint. Ticker channel emits last-trade ticks with a
default throttle of ~100ms. Adds a fourth independent spot feed (alongside
Coinbase, Binance, Bybit Spot) so the first-tick-wins aggregator has another
shot at the fastest tick on a given move.

Supports: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE (all as USDT pairs on OKX).
Coins without a USDT pair are silently skipped.
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

OKX_SPOT_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"

# OKX spot instIds per coin.
OKX_SPOT_SYMBOLS = {
    "BTC": "BTC-USDT",
    "ETH": "ETH-USDT",
    "SOL": "SOL-USDT",
    "XRP": "XRP-USDT",
    "DOGE": "DOGE-USDT",
    "BNB": "BNB-USDT",
    "HYPE": "HYPE-USDT",
}
SYMBOL_TO_COIN = {v: k for k, v in OKX_SPOT_SYMBOLS.items()}

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


class OKXSpotPriceFeed:
    """Real-time spot price feed from OKX WS v5. Mirrors the BybitSpotPriceFeed
    interface so MultiPriceFeed can aggregate first-tick-wins across feeds."""

    def __init__(
        self,
        coins: list[str] | None = None,
        vol_window_seconds: int = 300,
        vol_sample_interval: float = 5.0,
        vol_recalc_interval: float = 10.0,
    ):
        requested = [c.upper() for c in (coins or ["BTC"])]
        self.coins = [c for c in requested if c in OKX_SPOT_SYMBOLS]
        skipped = [c for c in requested if c not in OKX_SPOT_SYMBOLS]
        if skipped:
            logger.info(f"OKXSpotPriceFeed: skipping unsupported coins: {skipped}")
        self.vol_window_seconds = vol_window_seconds
        self.vol_sample_interval = vol_sample_interval
        self.vol_recalc_interval = vol_recalc_interval

        self._state: Dict[str, CoinState] = {
            c: CoinState(symbol=OKX_SPOT_SYMBOLS[c]) for c in self.coins
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
        logger.warning("OKXSpotPriceFeed: timeout waiting for initial prices")
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
        """OKX WS v5 closes idle connections after 30s — send a plain 'ping'
        string (not JSON) every 25s to keep it alive."""
        try:
            while self._running:
                await asyncio.sleep(25)
                if not self._running:
                    break
                try:
                    await ws.send("ping")
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
                logger.info(f"OKXSpotPriceFeed connecting: {self.coins}")
                async with ws_connect(OKX_SPOT_WS_URL) as ws:
                    self._ws = ws
                    args = [{"channel": "tickers", "instId": OKX_SPOT_SYMBOLS[c]}
                            for c in self.coins]
                    sub_msg = json.dumps({"op": "subscribe", "args": args})
                    await ws.send(sub_msg)
                    logger.info("OKXSpotPriceFeed connected + subscribed")
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    _n = 0
                    async for msg in ws:
                        if not self._running:
                            break
                        # OKX can send plain 'pong' string back to our ping
                        if msg == "pong":
                            continue
                        self._handle_message(msg)
                        _n += 1
                        if _n % 50 == 0:
                            await asyncio.sleep(0)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"OKXSpotPriceFeed error: {e}, reconnecting in 2s")
                self._ws = None
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None
                await asyncio.sleep(2)
        self._ws = None

    def _handle_message(self, raw):
        try:
            data = orjson.loads(raw)
            # Ignore subscription acks and error frames.
            if "event" in data:
                return
            arg = data.get("arg") or {}
            if arg.get("channel") != "tickers":
                return
            payload = data.get("data") or []
            if not payload:
                return
            now = time.time()
            for entry in payload:
                sym = entry.get("instId", "")
                coin = SYMBOL_TO_COIN.get(sym)
                if not coin or coin not in self._state:
                    continue
                last_str = entry.get("last") or entry.get("lastPrice")
                if last_str is None:
                    continue
                try:
                    price = float(last_str)
                except (TypeError, ValueError):
                    continue
                if price <= 0:
                    continue
                state = self._state[coin]
                state.price = price
                state.last_update = now
                if now - self._last_sample.get(coin, 0) >= self.vol_sample_interval:
                    state.history.append(PricePoint(price=price, timestamp=now))
                    self._last_sample[coin] = now
                for cb in self._price_callbacks:
                    try:
                        cb(coin, price)
                    except Exception:
                        pass
        except (json.JSONDecodeError, KeyError, ValueError):
            pass
