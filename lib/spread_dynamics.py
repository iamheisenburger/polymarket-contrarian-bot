"""
Spread dynamics tracker. Telemetry-only signal for FAK fire decisions.

Per-coin, per-side rolling history of (bid-ask) spread snapshots on
Polymarket. On fire attempts, compute whether spread is NARROWING
(makers committing to price → higher fill confidence) or WIDENING
(makers pulling → more AS risk).

Not a directional signal — purely a confidence gauge on maker stability.
"""
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Tuple


@dataclass
class _SpreadSnap:
    ts: float
    spread: float


class SpreadDynamicsTracker:
    """Per (coin, side) rolling spread snapshots. Sample via sample(), query
    via get_direction()."""

    def __init__(self, lookback_seconds: float = 10.0, min_sample_interval: float = 0.5):
        self.lookback = float(lookback_seconds)
        self.min_sample_interval = float(min_sample_interval)
        self._history: Dict[Tuple[str, str], Deque[_SpreadSnap]] = {}
        self._last_sample_ts: Dict[Tuple[str, str], float] = {}

    def sample(self, coin: str, side: str, spread: float) -> bool:
        """Rate-limited spread snapshot. Returns True if sample was taken,
        False if skipped due to throttling. Call on every price update."""
        if spread < 0 or spread > 1:
            return False  # sanity
        k = (coin.upper(), side)
        now = time.time()
        last = self._last_sample_ts.get(k, 0.0)
        if now - last < self.min_sample_interval:
            return False
        self._last_sample_ts[k] = now
        dq = self._history.get(k)
        if dq is None:
            dq = deque()
            self._history[k] = dq
        dq.append(_SpreadSnap(now, spread))
        cutoff = now - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        return True

    def get_direction(self, coin: str, side: str) -> Tuple[str, float, int]:
        """Return (direction_str, slope_per_sec, n_samples).

        direction_str is 'narrowing', 'widening', 'stable', or 'insufficient'.
        slope_per_sec is the least-squares spread derivative ($ per second).
        Negative slope = narrowing. Positive = widening.
        """
        k = (coin.upper(), side)
        dq = self._history.get(k)
        if not dq or len(dq) < 3:
            return ("insufficient", 0.0, len(dq) if dq else 0)
        # Prune stale
        cutoff = time.time() - self.lookback
        while dq and dq[0].ts < cutoff:
            dq.popleft()
        if len(dq) < 3:
            return ("insufficient", 0.0, len(dq))
        # Least-squares slope: sum((t-t̄)(s-s̄)) / sum((t-t̄)²)
        n = len(dq)
        ts = [snap.ts for snap in dq]
        sp = [snap.spread for snap in dq]
        t0 = ts[0]
        xs = [t - t0 for t in ts]
        x_mean = sum(xs) / n
        y_mean = sum(sp) / n
        num = sum((x - x_mean) * (y - y_mean) for x, y in zip(xs, sp))
        den = sum((x - x_mean) ** 2 for x in xs)
        slope = num / den if den > 0 else 0.0
        # Classify: slope threshold $0.001/sec = $0.01 per 10s window
        if abs(slope) < 0.001:
            label = "stable"
        elif slope < 0:
            label = "narrowing"
        else:
            label = "widening"
        return (label, slope, n)

    def get_current_spread(self, coin: str, side: str) -> float:
        """Return most recent spread sample, or 0.0 if none."""
        k = (coin.upper(), side)
        dq = self._history.get(k)
        if not dq:
            return 0.0
        return dq[-1].spread
