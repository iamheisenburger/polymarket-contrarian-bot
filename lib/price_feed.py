"""
BTC Spot Price Feed - Cached price fetcher for trade logging.

Fetches BTC/USDT spot price from Binance public API.
Caches for 60 seconds to avoid excessive API calls.
Never blocks or crashes the trading loop — returns 0.0 on failure.
"""

import time
import requests


class PriceFeed:
    """Cached BTC spot price fetcher."""

    BINANCE_URL = "https://api.binance.com/api/v3/ticker/price"

    def __init__(self, symbol: str = "BTCUSDT", cache_seconds: float = 60.0):
        self.symbol = symbol
        self.cache_seconds = cache_seconds
        self._cached_price: float = 0.0
        self._cached_at: float = 0.0

    def get_price(self) -> float:
        """Get current BTC spot price (cached)."""
        now = time.time()
        if now - self._cached_at < self.cache_seconds and self._cached_price > 0:
            return self._cached_price

        try:
            resp = requests.get(
                self.BINANCE_URL,
                params={"symbol": self.symbol},
                timeout=5,
            )
            resp.raise_for_status()
            self._cached_price = float(resp.json()["price"])
            self._cached_at = now
        except Exception:
            pass  # Return stale cache or 0.0

        return self._cached_price
