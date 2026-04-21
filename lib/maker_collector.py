"""
Maker IC — simulates resting GTD maker orders at MULTIPLE price levels.

Records every tick where a resting BUY would fill, with full momentum
context. Records at multiple rest prices (like taker IC records at
multiple momentum thresholds) so we can sweep for optimal price offline.

Detection: if the best_ask on a side drops to our rest_price or below,
that means there ARE sellers at our price level and our resting BUY
would be filled.
"""

import csv
import os
import threading
import logging
from datetime import datetime, timezone
from typing import Dict, List, Set

logger = logging.getLogger(__name__)

# Multiple rest prices to record — sweep offline to find optimal
REST_PRICES = [0.50, 0.52, 0.55, 0.58, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

HEADER = [
    'timestamp', 'market_slug', 'coin', 'side',
    'rest_price', 'elapsed', 'momentum', 'momentum_direction',
    'is_momentum_side',
    'best_ask', 'best_bid', 'other_side_ask',
    'outcome',
]


class MakerCollector:
    """Records simulated maker order fills at multiple price levels."""

    def __init__(self, output_path: str, place_elapsed: float = 10.0):
        self._output = output_path
        self._place_elapsed = place_elapsed
        self._lock = threading.Lock()

        # Per-window per-side per-price tracking
        # Key: "slug:coin:side:price"
        self._filled: Set[str] = set()

        # Pending records awaiting outcome resolution
        self._pending: List[dict] = []

        # Stats
        self._total_fills = 0
        self._total_resolved = 0

        # Write header if new file
        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            with open(output_path, 'w', newline='') as f:
                csv.writer(f).writerow(HEADER)

    def tick(self, coin: str, slug: str, elapsed: float,
             spot: float, strike: float,
             up_ask: float, up_bid: float,
             down_ask: float, down_bid: float):
        """
        Called every tick. Checks if a resting BUY at each rest price
        would fill on either side. Records with full momentum context.
        """
        if elapsed < self._place_elapsed:
            return
        if strike <= 0 or spot <= 0:
            return
        if elapsed > 280:
            return

        disp = (spot - strike) / strike
        mom_dir = "up" if disp > 0 else "down"

        for rest_price in REST_PRICES:
            for side in ['up', 'down']:
                fill_key = f"{slug}:{coin}:{side}:{rest_price}"
                if fill_key in self._filled:
                    continue

                side_ask = up_ask if side == 'up' else down_ask
                side_bid = up_bid if side == 'up' else down_bid
                other_ask = down_ask if side == 'up' else up_ask

                if side_ask <= rest_price and side_ask > 0:
                    self._filled.add(fill_key)
                    self._total_fills += 1

                    with self._lock:
                        self._pending.append({
                            'timestamp': datetime.now(timezone.utc).isoformat(),
                            'slug': slug,
                            'coin': coin,
                            'side': side,
                            'rest_price': rest_price,
                            'elapsed': round(elapsed, 1),
                            'momentum': round(disp, 8),
                            'mom_dir': mom_dir,
                            'is_momentum_side': (side == 'up' and disp > 0) or (side == 'down' and disp < 0),
                            'best_ask': round(side_ask, 4),
                            'best_bid': round(side_bid, 4),
                            'other_side_ask': round(other_ask, 4),
                            'outcome': 'pending',
                        })

    def resolve(self, slug: str, winning_side: str):
        """Resolve outcome for all pending records matching this slug."""
        with self._lock:
            for record in self._pending:
                if record['slug'] == slug and record['outcome'] == 'pending':
                    record['outcome'] = 'won' if record['side'] == winning_side else 'lost'

    def flush(self):
        """Write resolved records to CSV."""
        with self._lock:
            resolved = [r for r in self._pending if r['outcome'] in ('won', 'lost')]
            if not resolved:
                return 0

            try:
                with open(self._output, 'a', newline='') as f:
                    writer = csv.writer(f)
                    for r in resolved:
                        writer.writerow([
                            r['timestamp'], r['slug'], r['coin'], r['side'],
                            r['rest_price'], r['elapsed'],
                            r['momentum'], r['mom_dir'],
                            r['is_momentum_side'],
                            r['best_ask'], r['best_bid'], r['other_side_ask'],
                            r['outcome'],
                        ])
                self._total_resolved += len(resolved)
                self._pending = [r for r in self._pending if r['outcome'] == 'pending']
                return len(resolved)
            except Exception as e:
                logger.error(f"MakerCollector flush error: {e}")
                return 0

    def on_market_change(self, old_slug: str):
        """Clean up tracking for old market window."""
        keys_to_remove = [k for k in self._filled if old_slug in k]
        for k in keys_to_remove:
            self._filled.discard(k)

    def stats(self) -> str:
        """Human-readable stats."""
        with self._lock:
            resolved = [r for r in self._pending if r['outcome'] in ('won', 'lost')]
            pending = len(self._pending) - len(resolved)
            wins = sum(1 for r in resolved if r['outcome'] == 'won')
            losses = len(resolved) - wins
        return (
            f"MakerIC: total_fills={self._total_fills} "
            f"resolved={self._total_resolved} pending={pending} "
            f"buffer={wins}W/{losses}L"
        )
