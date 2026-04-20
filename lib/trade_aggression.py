"""
Trade aggression tracker. Telemetry-only signal for FAK fire decisions.

Per-coin rolling ratio of ask-hit vs bid-hit volume over the last N seconds.
A positive ratio means aggressive BUYING (takers lifting asks). Negative
means aggressive SELLING (takers hitting bids). Magnitude in [-1, +1].

Fed by WS aggTrade callbacks from spot feeds (Binance, optionally others).

Usage (from sniper):

    tracker = TradeAggressionTracker(lookback_seconds=10.0)
    binance_feed.on_trade(tracker.on_trade)
    # ... in fire path:
    ratio = tracker.get_ratio(coin)
    self._event_logger.info(f"[AGGR] {coin} ratio={ratio:+.3f}")

Do NOT use as a gate until 2-4h of live data has been correlated with fill
outcomes and shows >=5pp WR lift on agreeing fires. See upgrade queue rules.
"""
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict


@dataclass
class _TradeEvent:
    ts: float
    side_hit: str  # "ASK" (buy pressure) or "BID" (sell pressure)
    qty: float


class TradeAggressionTracker:
    """Rolling buy-vs-sell aggression per coin."""

    def __init__(self, lookback_seconds: float = 10.0):
        self.lookback = float(lookback_seconds)
        self._events: Dict[str, Deque[_TradeEvent]] = {}

    def on_trade(self, coin: str, side_hit: str, qty: float) -> None:
        """Ingest a trade. side_hit is 'ASK' (taker buy) or 'BID' (taker sell).
        Safe to call from any thread — single-threaded feed callbacks only."""
        if qty <= 0 or side_hit not in ("ASK", "BID"):
            return
        c = coin.upper()
        dq = self._events.get(c)
        if dq is None:
            dq = deque()
            self._events[c] = dq
        now = time.time()
        dq.append(_TradeEvent(now, side_hit, qty))
        # Prune any events older than lookback
        cutoff = now - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()

    def get_ratio(self, coin: str) -> float:
        """Return (ask_hit_vol - bid_hit_vol) / (ask_hit_vol + bid_hit_vol).
        Positive = buy pressure. Negative = sell pressure. Zero if no trades
        in lookback window. Range [-1.0, +1.0]."""
        c = coin.upper()
        dq = self._events.get(c)
        if not dq:
            return 0.0
        cutoff = time.time() - self.lookback
        ask_hit = 0.0  # aggressive buying
        bid_hit = 0.0  # aggressive selling
        # Prune stale while summing
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        for ev in dq:
            if ev.side_hit == "ASK":
                ask_hit += ev.qty
            else:
                bid_hit += ev.qty
        total = ask_hit + bid_hit
        if total <= 0:
            return 0.0
        return (ask_hit - bid_hit) / total

    def get_volume(self, coin: str) -> float:
        """Return total volume (ask_hit + bid_hit) in lookback window. Used to
        gate aggression signal — ratio on thin volume is noisy."""
        c = coin.upper()
        dq = self._events.get(c)
        if not dq:
            return 0.0
        cutoff = time.time() - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        return sum(ev.qty for ev in dq)
