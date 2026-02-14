"""
Volatility Tracker - Rolling BTC Price Volatility

Tracks mid-price movements to estimate short-term volatility.
Used as a filter for the contrarian strategy: higher volatility
means more reversals, which increases the win rate of buying
the cheap side.

Usage:
    from lib.volatility_tracker import VolatilityTracker

    tracker = VolatilityTracker(window_seconds=1800)
    tracker.record(0.55)  # record up-side mid price
    if tracker.is_volatile_enough(min_std=0.02):
        # good conditions for contrarian trades
"""

import time
import math
from collections import deque
from dataclasses import dataclass
from typing import Optional


@dataclass
class PriceObservation:
    """A timestamped price observation."""
    timestamp: float
    price: float


class VolatilityTracker:
    """
    Tracks rolling price volatility using standard deviation of
    price changes over a configurable time window.
    """

    def __init__(self, window_seconds: int = 1800, max_observations: int = 500):
        """
        Initialize volatility tracker.

        Args:
            window_seconds: Rolling window size in seconds (default 30 min)
            max_observations: Max observations to keep in memory
        """
        self.window_seconds = window_seconds
        self._observations: deque = deque(maxlen=max_observations)

    def record(self, price: float) -> None:
        """
        Record a price observation.

        Args:
            price: Current mid-price (0-1 range for binary markets)
        """
        if price <= 0:
            return
        self._observations.append(PriceObservation(
            timestamp=time.time(),
            price=price,
        ))

    def _get_recent_prices(self) -> list[float]:
        """Get prices within the rolling window."""
        cutoff = time.time() - self.window_seconds
        return [obs.price for obs in self._observations if obs.timestamp >= cutoff]

    def get_std_dev(self) -> float:
        """
        Calculate standard deviation of prices in the window.

        Returns:
            Standard deviation, or 0.0 if insufficient data
        """
        prices = self._get_recent_prices()
        if len(prices) < 5:
            return 0.0

        mean = sum(prices) / len(prices)
        variance = sum((p - mean) ** 2 for p in prices) / len(prices)
        return math.sqrt(variance)

    def get_price_range(self) -> float:
        """
        Get high-low range within the window.

        Returns:
            Price range (max - min), or 0.0 if insufficient data
        """
        prices = self._get_recent_prices()
        if len(prices) < 2:
            return 0.0
        return max(prices) - min(prices)

    def is_volatile_enough(self, min_std: float = 0.0) -> bool:
        """
        Check if current volatility meets minimum threshold.

        Args:
            min_std: Minimum standard deviation required

        Returns:
            True if volatile enough (or if min_std is 0, always True)
        """
        if min_std <= 0:
            return True
        return self.get_std_dev() >= min_std

    def get_observation_count(self) -> int:
        """Get number of price observations in window."""
        cutoff = time.time() - self.window_seconds
        return sum(1 for obs in self._observations if obs.timestamp >= cutoff)

    def clear(self) -> None:
        """Clear all observations."""
        self._observations.clear()
