"""
Vatic API Client — Fetches exact Chainlink strike prices for Polymarket crypto markets.

Vatic reverse-engineered Polymarket's settlement sources:
- 5m/15m/4h: Chainlink Data Streams
- 1h: Binance aggTrade
- Daily: Binance Klines 1m close

This client fetches the EXACT opening strike price that Polymarket will use for
settlement, eliminating the ~0.1-0.3% error from back-solving or using Binance spot.

API: https://api.vatic.trading (free, no auth required)
"""

import logging
import time
from typing import Dict, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# Cache: asset -> (expire_ts, targets_list)
_cache: Dict[str, Tuple[float, list]] = {}
CACHE_TTL = 30  # 30 seconds — must be short so new 5m markets get fresh data
NEGATIVE_CACHE_TTL = 10  # Cache failures for 10s to avoid hammering API


class VaticClient:
    """Fetches exact strike prices from Vatic's public API."""

    BASE_URL = "https://api.vatic.trading"

    # Vatic asset names (lowercase)
    ASSET_MAP = {
        "BTC": "btc",
        "ETH": "eth",
        "SOL": "sol",
        "XRP": "xrp",
    }

    # Vatic marketType field values
    TIMEFRAME_MAP = {
        "5m": "5min",
        "15m": "15min",
        "1h": "1hour",
        "4h": "4hour",
        "daily": "daily",
    }

    def get_strike(
        self,
        coin: str,
        timeframe: str,
        window_start_ts: Optional[float] = None,
    ) -> Optional[float]:
        """Get the exact strike price for a coin/timeframe.

        Args:
            coin: Coin symbol (BTC, ETH, SOL, XRP)
            timeframe: Market timeframe (5m, 15m, 1h, 4h, daily)
            window_start_ts: Expected window start unix timestamp (for matching)

        Returns:
            Strike price as float, or None if unavailable
        """
        asset = self.ASSET_MAP.get(coin.upper())
        if not asset:
            return None

        vatic_tf = self.TIMEFRAME_MAP.get(timeframe)
        if not vatic_tf:
            return None

        # If we have a window_start_ts and cached data doesn't match,
        # force a fresh fetch (cache might have stale previous-window data)
        if window_start_ts and window_start_ts > 0:
            cached = self._get_cached(asset)
            if cached is not None:
                # Check if any 5min target matches our window
                has_match = any(
                    t.get("marketType") == vatic_tf and
                    abs(t.get("windowStart", 0) - window_start_ts) <= 5
                    for t in cached
                )
                if not has_match:
                    # Stale cache — invalidate and re-fetch
                    _cache.pop(asset, None)

        targets = self._fetch_targets(asset)
        if not targets:
            return None

        # Find matching target by timeframe
        for target in targets:
            if target.get("marketType") != vatic_tf:
                continue

            price = target.get("price")
            if price is None:
                continue

            # If we have a window_start_ts, verify it matches
            if window_start_ts and window_start_ts > 0:
                target_start = target.get("windowStart", 0)
                # Allow 5 second tolerance for timestamp matching
                if abs(target_start - window_start_ts) > 5:
                    continue

            return float(price)

        return None

    def _get_cached(self, asset: str) -> Optional[list]:
        """Return cached targets if still valid, else None."""
        now = time.time()
        if asset in _cache:
            expire_ts, cached_targets = _cache[asset]
            if now < expire_ts:
                return cached_targets
        return None

    def _fetch_targets(self, asset: str) -> Optional[list]:
        """Fetch active targets for an asset, with caching."""
        now = time.time()

        # Check cache
        if asset in _cache:
            expire_ts, cached_targets = _cache[asset]
            if now < expire_ts:
                return cached_targets

        # Fetch from API
        try:
            resp = requests.get(
                f"{self.BASE_URL}/api/v1/targets/active",
                params={"asset": asset},
                timeout=10,
            )
            if resp.status_code == 429:
                logger.warning(f"Vatic API rate limited for {asset}")
                _cache[asset] = (now + NEGATIVE_CACHE_TTL, [])
                return None
            if resp.status_code != 200:
                logger.warning(f"Vatic API returned {resp.status_code} for {asset}")
                _cache[asset] = (now + NEGATIVE_CACHE_TTL, [])
                return None

            data = resp.json()
            targets = data.get("results", [])

            # Filter out targets where ok=false (e.g. "Too Many Requests")
            ok_targets = [t for t in targets if t.get("ok", True) is not False]

            if not ok_targets:
                # All targets errored — negative cache
                _cache[asset] = (now + NEGATIVE_CACHE_TTL, [])
                return None

            # Cache successful response
            _cache[asset] = (now + CACHE_TTL, ok_targets)
            return ok_targets

        except Exception as e:
            logger.warning(f"Vatic API error for {asset}: {e}")
            _cache[asset] = (now + NEGATIVE_CACHE_TTL, [])
            return None
