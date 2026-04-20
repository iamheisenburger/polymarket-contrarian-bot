"""
Bybit liquidation stream tracker. Real-time liquidation events on Bybit's
public WS — these CAUSE price moves, so detecting a big liq gives a
50-200ms lead before the price impact propagates to spot feeds.

Subscribes to `allLiquidation.<symbol>` channel on linear USDT perps.
Each event:
  {"symbol": "BTCUSDT", "side": "Buy"|"Sell", "size": "x.xx", "price": "..."}

Side convention (Bybit):
  "Buy"  liquidation  → a SHORT position got liquidated (taker buy to close)
  "Sell" liquidation  → a LONG  position got liquidated (taker sell to close)

Heavy long liqs cascading → downward price pressure. Heavy short liqs
cascading → upward price pressure.
"""
import asyncio
import json
import orjson
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)

BYBIT_LIQ_WS_URL = "wss://stream.bybit.com/v5/public/linear"

BYBIT_LIQ_SYMBOLS = {
    "BTC": "BTCUSDT",
    "ETH": "ETHUSDT",
    "SOL": "SOLUSDT",
    "XRP": "XRPUSDT",
    "DOGE": "DOGEUSDT",
    "BNB": "BNBUSDT",
}
SYMBOL_TO_COIN = {v: k for k, v in BYBIT_LIQ_SYMBOLS.items()}


@dataclass
class _LiqEvent:
    ts: float
    side: str   # "Buy" = short liquidated (up pressure), "Sell" = long liquidated (down pressure)
    notional: float  # USD


class BybitLiquidationTracker:
    """Rolling per-coin liquidation event log from Bybit public WS."""

    def __init__(self, coins: list[str] | None = None, lookback_seconds: float = 60.0):
        requested = [c.upper() for c in (coins or ["BTC"])]
        self.coins = [c for c in requested if c in BYBIT_LIQ_SYMBOLS]
        self.lookback = float(lookback_seconds)
        self._events: Dict[str, Deque[_LiqEvent]] = {c: deque() for c in self.coins}
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._ping_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self) -> bool:
        if self._running or not self.coins:
            return self._running or False
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        # Give the subscription a moment to land; return even if no events yet.
        await asyncio.sleep(1.0)
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
                logger.info(f"BybitLiquidationTracker connecting: {self.coins}")
                async with ws_connect(BYBIT_LIQ_WS_URL) as ws:
                    self._ws = ws
                    args = [f"allLiquidation.{BYBIT_LIQ_SYMBOLS[c]}" for c in self.coins]
                    await ws.send(json.dumps({"op": "subscribe", "args": args}))
                    logger.info("BybitLiquidationTracker connected + subscribed")
                    self._ping_task = asyncio.create_task(self._ping_loop(ws))
                    async for msg in ws:
                        if not self._running:
                            break
                        self._handle_message(msg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"BybitLiquidationTracker error: {e}, reconnecting in 2s")
                self._ws = None
                if self._ping_task:
                    self._ping_task.cancel()
                    self._ping_task = None
                await asyncio.sleep(2)
        self._ws = None

    def _handle_message(self, raw):
        try:
            data = orjson.loads(raw)
            topic = data.get("topic", "")
            if not topic.startswith("allLiquidation."):
                return
            payload = data.get("data") or []
            if not payload:
                return
            entries = payload if isinstance(payload, list) else [payload]
            now = time.time()
            for entry in entries:
                sym = entry.get("symbol", "")
                coin = SYMBOL_TO_COIN.get(sym)
                if not coin or coin not in self._events:
                    continue
                try:
                    size = float(entry.get("size", 0))
                    price = float(entry.get("price", 0))
                except (TypeError, ValueError):
                    continue
                notional = size * price
                if notional <= 0:
                    continue
                side = entry.get("side", "")
                dq = self._events[coin]
                dq.append(_LiqEvent(now, side, notional))
                cutoff = now - self.lookback
                while dq and dq[0].ts < cutoff:
                    dq.popleft()
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass

    def get_recent_notional(self, coin: str) -> float:
        """Total USD notional liquidated in the lookback window for this coin."""
        dq = self._events.get(coin.upper())
        if not dq:
            return 0.0
        cutoff = time.time() - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        return sum(e.notional for e in dq)

    def get_side_imbalance(self, coin: str) -> float:
        """(buy_side_notional - sell_side_notional) / total.
        Positive = more shorts getting liquidated (up pressure).
        Negative = more longs getting liquidated (down pressure).
        Range [-1, +1]. Zero if no events."""
        dq = self._events.get(coin.upper())
        if not dq:
            return 0.0
        cutoff = time.time() - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        buy_n = 0.0
        sell_n = 0.0
        for e in dq:
            if e.side == "Buy":
                buy_n += e.notional
            elif e.side == "Sell":
                sell_n += e.notional
        total = buy_n + sell_n
        if total <= 0:
            return 0.0
        return (buy_n - sell_n) / total

    def get_event_count(self, coin: str) -> int:
        """Number of liquidation events in lookback window."""
        dq = self._events.get(coin.upper())
        if not dq:
            return 0
        cutoff = time.time() - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        return len(dq)
