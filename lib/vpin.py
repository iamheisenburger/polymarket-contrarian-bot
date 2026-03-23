"""
VPIN (Volume-synchronized Probability of Informed Trading) calculator.

Measures the toxicity of order flow on Polymarket binary markets.
High VPIN = informed traders are active = strong directional signal.

Usage:
    tracker = VPINTracker()
    tracker.register_token("token_123", "btc", "up")
    tracker.on_trade(trade_event)  # feed LastTradePrice events
    vpin = tracker.get_vpin("token_123")  # 0.0 to 1.0
"""

import csv
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class VPINConfig:
    bucket_volume: float = 50.0  # Volume per bucket (in tokens)
    n_buckets: int = 20          # Rolling window size
    min_buckets: int = 5         # Minimum buckets before valid VPIN


class TokenVPIN:
    """VPIN tracker for a single token (UP or DOWN)."""

    def __init__(self, config: VPINConfig):
        self.config = config
        self._current_buy: float = 0.0
        self._current_sell: float = 0.0
        self._current_total: float = 0.0
        self._buckets: deque = deque(maxlen=config.n_buckets)
        self._total_trades: int = 0

    def ingest_trade(self, size: float, side: str) -> None:
        """Add a trade. Side is 'BUY' or 'SELL' (taker/aggressor side)."""
        self._total_trades += 1
        remaining = size

        while remaining > 0:
            space = self.config.bucket_volume - self._current_total
            fill = min(remaining, space)

            if side.upper() == "BUY":
                self._current_buy += fill
            else:
                self._current_sell += fill
            self._current_total += fill
            remaining -= fill

            if self._current_total >= self.config.bucket_volume:
                self._buckets.append((self._current_buy, self._current_sell))
                self._current_buy = 0.0
                self._current_sell = 0.0
                self._current_total = 0.0

    def get_vpin(self) -> Optional[float]:
        """Return VPIN (0.0-1.0). None if insufficient data."""
        if len(self._buckets) < self.config.min_buckets:
            return None

        total_imbalance = sum(abs(b - s) for b, s in self._buckets)
        total_volume = sum(b + s for b, s in self._buckets)

        if total_volume == 0:
            return None
        return total_imbalance / total_volume

    def get_flow_direction(self) -> Optional[float]:
        """Return signed flow: positive = net buying, negative = net selling.
        Normalized to [-1, 1]."""
        if len(self._buckets) < self.config.min_buckets:
            return None

        total_buy = sum(b for b, s in self._buckets)
        total_sell = sum(s for b, s in self._buckets)
        total = total_buy + total_sell

        if total == 0:
            return None
        return (total_buy - total_sell) / total

    def reset(self) -> None:
        """Reset state (on market transition)."""
        self._current_buy = 0.0
        self._current_sell = 0.0
        self._current_total = 0.0
        self._buckets.clear()
        self._total_trades = 0

    @property
    def n_buckets_filled(self) -> int:
        return len(self._buckets)

    @property
    def total_trades(self) -> int:
        return self._total_trades


class VPINTracker:
    """Top-level tracker: manages per-token VPIN instances."""

    def __init__(self, config: VPINConfig = None):
        self.config = config or VPINConfig()
        self._trackers: Dict[str, TokenVPIN] = {}
        self._token_to_coin_side: Dict[str, Tuple[str, str]] = {}
        self._coin_side_to_token: Dict[Tuple[str, str], str] = {}

    def register_token(self, token_id: str, coin: str, side: str) -> None:
        """Register a token for VPIN tracking."""
        if token_id not in self._trackers:
            self._trackers[token_id] = TokenVPIN(self.config)
        self._token_to_coin_side[token_id] = (coin.lower(), side.lower())
        self._coin_side_to_token[(coin.lower(), side.lower())] = token_id

    def on_trade(self, token_id: str, size: float, side: str) -> None:
        """Receive a trade event. Route to correct TokenVPIN.

        Args:
            token_id: The token that was traded
            size: Trade size in tokens
            side: 'BUY' or 'SELL' (aggressor side)
        """
        tracker = self._trackers.get(token_id)
        if tracker:
            tracker.ingest_trade(size, side)

    def get_vpin(self, token_id: str) -> Optional[float]:
        """Get VPIN for a token."""
        tracker = self._trackers.get(token_id)
        return tracker.get_vpin() if tracker else None

    def get_flow_direction(self, token_id: str) -> Optional[float]:
        """Get signed flow direction for a token."""
        tracker = self._trackers.get(token_id)
        return tracker.get_flow_direction() if tracker else None

    def get_vpin_by_coin_side(self, coin: str, side: str) -> Optional[float]:
        """Get VPIN by coin name and side."""
        token_id = self._coin_side_to_token.get((coin.lower(), side.lower()))
        return self.get_vpin(token_id) if token_id else None

    def get_flow_by_coin_side(self, coin: str, side: str) -> Optional[float]:
        """Get flow direction by coin name and side."""
        token_id = self._coin_side_to_token.get((coin.lower(), side.lower()))
        return self.get_flow_direction(token_id) if token_id else None

    def reset_coin(self, coin: str) -> None:
        """Reset all tokens for a coin (on market transition)."""
        for token_id, (c, s) in list(self._token_to_coin_side.items()):
            if c == coin.lower():
                tracker = self._trackers.get(token_id)
                if tracker:
                    tracker.reset()

    def get_status(self, coin: str) -> str:
        """Status string for display."""
        parts = []
        for side in ["up", "down"]:
            vpin = self.get_vpin_by_coin_side(coin, side)
            flow = self.get_flow_by_coin_side(coin, side)
            if vpin is not None:
                flow_str = "buy" if flow and flow > 0 else "sell" if flow and flow < 0 else "?"
                parts.append(f"{side.capitalize()}={vpin:.2f}({flow_str})")
        return " ".join(parts) if parts else "no data"
