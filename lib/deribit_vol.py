"""
Deribit Implied Volatility Feed

Fetches forward-looking implied volatility from Deribit's DVOL index
for BTC and ETH. Used as the primary volatility input to Black-Scholes,
replacing backward-looking realized volatility from Binance tick data.

DVOL is Deribit's 30-day implied volatility index — the market's consensus
on expected future volatility. It's more accurate than realized vol because
it's forward-looking and incorporates all market participants' information.

Usage:
    feed = DeribitVolFeed(coins=["BTC", "ETH"])
    vol = feed.get_implied_vol("BTC")  # Returns 0.52 (52% annualized) or None
"""

import logging
import time
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Deribit DVOL index names per coin
DVOL_INDEX_NAMES: Dict[str, str] = {
    "BTC": "btcdvol_usdc",
    "ETH": "ethdvol_usdc",
}

DERIBIT_API_BASE = "https://www.deribit.com/api/v2/public"


class DeribitVolFeed:
    """
    Fetches implied volatility from Deribit DVOL index.

    - Caches results for cache_ttl seconds (default: 60)
    - Returns None on any error (caller falls back to Binance realized vol)
    - Only supports BTC and ETH (the coins Viper v2 trades)
    """

    def __init__(self, coins: list, cache_ttl: float = 60.0):
        self.coins = [c.upper() for c in coins]
        self.cache_ttl = cache_ttl

        # Cache: coin -> (vol, timestamp)
        self._cache: Dict[str, tuple] = {}

    def get_implied_vol(self, coin: str) -> Optional[float]:
        """
        Get Deribit implied vol for a coin.

        Returns annualized vol as decimal (e.g., 0.52 = 52%), or None
        if Deribit is unavailable or coin is not supported.
        """
        coin = coin.upper()

        if coin not in DVOL_INDEX_NAMES:
            return None

        # Check cache
        cached = self._cache.get(coin)
        if cached:
            vol, ts = cached
            if time.time() - ts < self.cache_ttl:
                return vol

        # Fetch fresh
        vol = self._fetch_dvol(coin)
        if vol is not None:
            self._cache[coin] = (vol, time.time())

        return vol

    def _fetch_dvol(self, coin: str) -> Optional[float]:
        """
        HTTP GET to Deribit DVOL endpoint.

        Returns annualized vol as decimal, or None on any error.
        """
        index_name = DVOL_INDEX_NAMES.get(coin)
        if not index_name:
            return None

        try:
            url = f"{DERIBIT_API_BASE}/get_index_price?index_name={index_name}"
            response = requests.get(url, timeout=5)
            response.raise_for_status()

            data = response.json()
            dvol_pct = data["result"]["index_price"]

            # DVOL is in percentage points (e.g., 51.92 = 51.92%)
            # Convert to decimal for Black-Scholes (0.5192)
            vol = dvol_pct / 100.0

            # Sanity check: vol should be between 10% and 300%
            if vol < 0.10 or vol > 3.0:
                logger.warning(f"Deribit DVOL {coin} out of range: {dvol_pct:.1f}%")
                return None

            return vol

        except requests.Timeout:
            logger.debug(f"Deribit DVOL timeout for {coin}")
            return None
        except requests.RequestException as e:
            logger.debug(f"Deribit DVOL request failed for {coin}: {e}")
            return None
        except (KeyError, TypeError, ValueError) as e:
            logger.debug(f"Deribit DVOL parse error for {coin}: {e}")
            return None
        except Exception as e:
            logger.debug(f"Deribit DVOL unexpected error for {coin}: {e}")
            return None
