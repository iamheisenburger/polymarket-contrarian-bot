"""
Pyth Network Hermes WebSocket price feed.

Pyth aggregates prices from tier-1 market makers (Jane Street, Jump, DRW,
Wintermute, Virtu, GTS) who publish directly from their own systems. Published
prices are available via Hermes (https://hermes.pyth.network) with median
update cadence ~400ms — but for directional moves the FIRST publisher hits
Hermes within tens of ms, which can beat CEX WS in some scenarios.

Same interface as CoinbasePriceFeed / BybitSpotPriceFeed — drops into
MultiPriceFeed aggregation.

Coins: BTC, ETH, SOL, XRP, DOGE, BNB. HYPE is on Pyth as well.
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

PYTH_WS_URL = "wss://hermes.pyth.network/ws"

# Pyth "price feed IDs" (32-byte hex, no 0x prefix).
# Source: https://pyth.network/developers/price-feed-ids
PYTH_IDS = {
    "BTC":  "e62df6c8b4a85fe1a67db44dc12de5db330f7ac66b72dc658afedf0f4a415b43",
    "ETH":  "ff61491a931112ddf1bd8147cd1b641375f79f5825126d665480874634fd0ace",
    "SOL":  "ef0d8b6fda2ceba41da15d4095d1da392a0d2f8ed0c6c7bc0f4cfac8c280b56d",
    "XRP":  "ec5d399846a9209f3fe5881d70aae9268c94339ff9817e8d18ff19fa05eea1c8",
    "DOGE": "dcef50dd0a4cd2dcc17e45df1676dcb336a11a61c69df7a0299b0150c672d25c",
    "BNB":  "2f95862b045670cd22bee3114c39763a4a08beeb663b145d283c31d7d1101c4f",
    "HYPE": "4279e31cc369bbcc2faf022b382b080e32a8e689ff20fbc530d2a603eb6cd98b",
}
ID_TO_COIN = {v: k for k, v in PYTH_IDS.items()}

SECONDS_PER_YEAR = 365.25 * 24 * 3600


@dataclass
class PricePoint:
    price: float
    timestamp: float


@dataclass
class CoinState:
    pyth_id: str
    price: float = 0.0
    last_update: float = 0.0
    history: Deque[PricePoint] = field(default_factory=lambda: deque(maxlen=600))
    _cached_vol: float = 0.50
    _vol_calc_time: float = 0.0


class PythPriceFeed:
    """Real-time price feed from Pyth Network Hermes WS."""

    def __init__(
        self,
        coins: list[str] | None = None,
        vol_window_seconds: int = 300,
        vol_sample_interval: float = 5.0,
        vol_recalc_interval: float = 10.0,
    ):
        requested = [c.upper() for c in (coins or ["BTC"])]
        self.coins = [c for c in requested if c in PYTH_IDS]
        skipped = [c for c in requested if c not in PYTH_IDS]
        if skipped:
            logger.info(f"PythPriceFeed: skipping unsupported coins: {skipped}")
        self.vol_window_seconds = vol_window_seconds
        self.vol_sample_interval = vol_sample_interval
        self.vol_recalc_interval = vol_recalc_interval

        self._state: Dict[str, CoinState] = {
            c: CoinState(pyth_id=PYTH_IDS[c]) for c in self.coins
        }
        self._last_sample: Dict[str, float] = {c: 0.0 for c in self.coins}
        self._ws = None
        self._task: Optional[asyncio.Task] = None
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
        logger.warning("PythPriceFeed: timeout waiting for initial prices")
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

    async def _run_loop(self):
        try:
            from websockets.asyncio.client import connect as ws_connect
        except ImportError:
            import websockets
            ws_connect = websockets.connect
        while self._running:
            try:
                logger.info(f"PythPriceFeed connecting: {self.coins}")
                async with ws_connect(PYTH_WS_URL) as ws:
                    self._ws = ws
                    sub_msg = json.dumps({
                        "type": "subscribe",
                        "ids": [PYTH_IDS[c] for c in self.coins],
                        "verbose": False,
                        "binary": False,
                    })
                    await ws.send(sub_msg)
                    logger.info("PythPriceFeed connected + subscribed")
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
                logger.warning(f"PythPriceFeed error: {e}, reconnecting in 2s")
                self._ws = None
                await asyncio.sleep(2)
        self._ws = None

    def _handle_message(self, raw):
        try:
            data = orjson.loads(raw)
            # Hermes emits {"type": "price_update", "price_feed": {...}} messages.
            if data.get("type") != "price_update":
                return
            pf = data.get("price_feed") or {}
            fid = pf.get("id", "")
            if not fid:
                return
            # IDs from Hermes may or may not have 0x prefix
            if fid.startswith("0x"):
                fid = fid[2:]
            coin = ID_TO_COIN.get(fid.lower())
            if not coin or coin not in self._state:
                return
            p = pf.get("price") or {}
            price_raw = p.get("price")
            expo = p.get("expo")
            publish_time = p.get("publish_time", 0)
            if price_raw is None or expo is None:
                return
            try:
                price = float(price_raw) * (10 ** float(expo))
            except (TypeError, ValueError):
                return
            if price <= 0:
                return
            state = self._state[coin]
            state.price = price
            # Use LOCAL reception time for first-tick-win measurement; publish_time
            # from Pyth is when MM published, not when we received.
            now = time.time()
            state.last_update = now
            if now - self._last_sample.get(coin, 0) >= self.vol_sample_interval:
                state.history.append(PricePoint(price=price, timestamp=now))
                self._last_sample[coin] = now
            for cb in self._price_callbacks:
                try:
                    cb(coin, price)
                except Exception:
                    pass
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass
