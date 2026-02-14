"""
Gamma API Client - Market Discovery for Polymarket

Provides access to the Gamma API for discovering active markets,
including 15-minute Up/Down markets for crypto assets.

Example:
    from src.gamma_client import GammaClient

    client = GammaClient()
    market = client.get_current_15m_market("ETH")
    print(market["slug"], market["clobTokenIds"])
"""

import json
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone

from .http import ThreadLocalSessionMixin


class GammaClient(ThreadLocalSessionMixin):
    """
    Client for Polymarket's Gamma API.

    Used to discover markets and get market metadata.
    """

    DEFAULT_HOST = "https://gamma-api.polymarket.com"

    # Supported coins and their slug prefixes by timeframe
    COIN_SLUGS_15M = {
        "BTC": "btc-updown-15m",
        "ETH": "eth-updown-15m",
        "SOL": "sol-updown-15m",
        "XRP": "xrp-updown-15m",
    }

    COIN_SLUGS_5M = {
        "BTC": "btc-updown-5m",
        "ETH": "eth-updown-5m",
        "SOL": "sol-updown-5m",
        "XRP": "xrp-updown-5m",
    }

    # Keep backward compat
    COIN_SLUGS = COIN_SLUGS_15M

    # Timeframe durations in seconds
    TIMEFRAME_SECONDS = {
        "5m": 300,
        "15m": 900,
    }

    def __init__(self, host: str = DEFAULT_HOST, timeout: int = 10):
        """
        Initialize Gamma client.

        Args:
            host: Gamma API host URL
            timeout: Request timeout in seconds
        """
        super().__init__()
        self.host = host.rstrip("/")
        self.timeout = timeout

    def get_market_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        """
        Get market data by slug.

        Args:
            slug: Market slug (e.g., "eth-updown-15m-1766671200")

        Returns:
            Market data dictionary or None if not found
        """
        url = f"{self.host}/markets/slug/{slug}"

        try:
            response = self.session.get(url, timeout=self.timeout)
            if response.status_code == 200:
                return response.json()
            return None
        except Exception:
            return None

    def get_current_market(self, coin: str, timeframe: str = "15m") -> Optional[Dict[str, Any]]:
        """
        Get the current active market for a coin and timeframe.

        Args:
            coin: Coin symbol (BTC, ETH, SOL, XRP)
            timeframe: Market timeframe ("5m" or "15m")

        Returns:
            Market data for the current window, or None
        """
        coin = coin.upper()
        slug_map = self.COIN_SLUGS_5M if timeframe == "5m" else self.COIN_SLUGS_15M
        duration = self.TIMEFRAME_SECONDS.get(timeframe, 900)

        if coin not in slug_map:
            raise ValueError(f"Unsupported coin: {coin}. Use: {list(slug_map.keys())}")

        prefix = slug_map[coin]
        now = datetime.now(timezone.utc)

        # Calculate window size in minutes
        window_minutes = duration // 60
        minute = (now.minute // window_minutes) * window_minutes
        current_window = now.replace(minute=minute, second=0, microsecond=0)
        current_ts = int(current_window.timestamp())

        # Try current, next, and previous windows
        for offset in [0, duration, -duration]:
            ts = current_ts + offset
            slug = f"{prefix}-{ts}"
            market = self.get_market_by_slug(slug)
            if market and market.get("acceptingOrders"):
                return market

        return None

    def get_current_15m_market(self, coin: str) -> Optional[Dict[str, Any]]:
        """Get the current active 15-minute market for a coin."""
        return self.get_current_market(coin, "15m")

    def get_current_5m_market(self, coin: str) -> Optional[Dict[str, Any]]:
        """Get the current active 5-minute market for a coin."""
        return self.get_current_market(coin, "5m")

    def get_next_15m_market(self, coin: str) -> Optional[Dict[str, Any]]:
        """
        Get the next upcoming 15-minute market for a coin.

        Args:
            coin: Coin symbol (BTC, ETH, SOL, XRP)

        Returns:
            Market data for the next 15-minute window, or None
        """
        coin = coin.upper()
        if coin not in self.COIN_SLUGS:
            raise ValueError(f"Unsupported coin: {coin}")

        prefix = self.COIN_SLUGS[coin]
        now = datetime.now(timezone.utc)

        # Calculate next 15-minute window
        minute = ((now.minute // 15) + 1) * 15
        if minute >= 60:
            next_window = now.replace(hour=now.hour + 1, minute=0, second=0, microsecond=0)
        else:
            next_window = now.replace(minute=minute, second=0, microsecond=0)

        next_ts = int(next_window.timestamp())
        slug = f"{prefix}-{next_ts}"

        return self.get_market_by_slug(slug)

    def parse_token_ids(self, market: Dict[str, Any]) -> Dict[str, str]:
        """
        Parse token IDs from market data.

        Args:
            market: Market data dictionary

        Returns:
            Dictionary with "up" and "down" token IDs
        """
        clob_token_ids = market.get("clobTokenIds", "[]")
        token_ids = self._parse_json_field(clob_token_ids)

        outcomes = market.get("outcomes", '["Up", "Down"]')
        outcomes = self._parse_json_field(outcomes)

        return self._map_outcomes(outcomes, token_ids)

    def parse_prices(self, market: Dict[str, Any]) -> Dict[str, float]:
        """
        Parse current prices from market data.

        Args:
            market: Market data dictionary

        Returns:
            Dictionary with "up" and "down" prices
        """
        outcome_prices = market.get("outcomePrices", '["0.5", "0.5"]')
        prices = self._parse_json_field(outcome_prices)

        outcomes = market.get("outcomes", '["Up", "Down"]')
        outcomes = self._parse_json_field(outcomes)

        return self._map_outcomes(outcomes, prices, cast=float)

    @staticmethod
    def _parse_json_field(value: Any) -> List[Any]:
        """Parse a field that may be a JSON string or a list."""
        if isinstance(value, str):
            return json.loads(value)
        return value

    @staticmethod
    def _map_outcomes(
        outcomes: List[Any],
        values: List[Any],
        cast=lambda v: v
    ) -> Dict[str, Any]:
        """Map outcome labels to values with optional casting."""
        result: Dict[str, Any] = {}
        for i, outcome in enumerate(outcomes):
            if i < len(values):
                result[str(outcome).lower()] = cast(values[i])
        return result

    def get_market_info(self, coin: str, timeframe: str = "15m") -> Optional[Dict[str, Any]]:
        """
        Get comprehensive market info for current market.

        Args:
            coin: Coin symbol
            timeframe: Market timeframe ("5m" or "15m")

        Returns:
            Dictionary with market info including token IDs and prices
        """
        market = self.get_current_market(coin, timeframe)
        if not market:
            return None

        token_ids = self.parse_token_ids(market)
        prices = self.parse_prices(market)

        return {
            "slug": market.get("slug"),
            "question": market.get("question"),
            "end_date": market.get("endDate"),
            "token_ids": token_ids,
            "prices": prices,
            "accepting_orders": market.get("acceptingOrders", False),
            "best_bid": market.get("bestBid"),
            "best_ask": market.get("bestAsk"),
            "spread": market.get("spread"),
            "raw": market,
        }
