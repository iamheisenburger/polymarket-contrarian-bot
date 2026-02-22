"""
Weather Market Scanner — discovers and prices Polymarket weather markets.

Uses tag_slug=temperature on Gamma API to find all active temperature events,
parses bucket ranges from market questions, fetches orderbook prices from CLOB.
"""

import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_API = "https://clob.polymarket.com"


@dataclass
class WeatherMarket:
    """A single temperature bucket market on Polymarket."""
    slug: str
    question: str
    event_slug: str
    condition_id: str
    token_id: str       # YES token
    bucket_low: float   # e.g. 34
    bucket_high: float  # e.g. 36
    bucket_label: str   # e.g. "34-35F"
    best_ask: float     # current cheapest ask price
    end_date: str
    city: str
    date: str           # YYYY-MM-DD


# Map city names in event slugs to our city keys
SLUG_CITY_MAP = {
    "nyc": "NYC",
    "new-york": "NYC",
    "london": "London",
    "chicago": "Chicago",
    "miami": "Miami",
    "dallas": "Dallas",
    "seattle": "Seattle",
    "seoul": "Seoul",
    "paris": "Paris",
    "toronto": "Toronto",
    "atlanta": "Atlanta",
    "ankara": "Ankara",
    "wellington": "Wellington",
    "sao-paulo": "Sao Paulo",
    "buenos-aires": "Buenos Aires",
}


class WeatherMarketScanner:
    """Discovers weather temperature markets on Polymarket."""

    def find_all_temperature_events(self) -> List[Dict]:
        """
        Find ALL active temperature events via tag_slug=temperature.
        Returns raw event data from Gamma.
        """
        try:
            resp = requests.get(f"{GAMMA_API}/events", params={
                "tag_slug": "temperature",
                "active": "true",
                "closed": "false",
                "limit": 100,
            }, timeout=15)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, list):
                    logger.info(f"Found {len(data)} active temperature events")
                    return data
        except Exception as e:
            logger.error(f"Failed to fetch temperature events: {e}")
        return []

    def _extract_city_from_slug(self, event_slug: str) -> str:
        """Extract city name from event slug like 'highest-temperature-in-nyc-on-february-22-2026'."""
        # Pattern: highest-temperature-in-{city}-on-{month}-{day}-{year}
        m = re.search(r'highest-temperature-in-(.+?)-on-', event_slug)
        if m:
            city_slug = m.group(1)
            return SLUG_CITY_MAP.get(city_slug, city_slug.replace("-", " ").title())
        return ""

    def _extract_date_from_slug(self, event_slug: str) -> str:
        """Extract date from event slug. Returns YYYY-MM-DD."""
        # slug format: highest-temperature-in-nyc-on-february-22-2026
        m = re.search(r'-on-(\w+)-(\d{1,2})-(\d{4})$', event_slug)
        if m:
            month_name, day, year = m.group(1), m.group(2), m.group(3)
            try:
                dt = datetime.strptime(f"{month_name} {day} {year}", "%B %d %Y")
                return dt.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return ""

    def parse_temperature_markets(self, event: Dict) -> List[WeatherMarket]:
        """
        Parse an event's markets into WeatherMarket objects.
        Extracts bucket ranges from market questions.
        """
        markets_raw = event.get("markets", [])
        event_slug = event.get("slug", "")
        end_date = event.get("endDate", "")

        city = self._extract_city_from_slug(event_slug)
        market_date = self._extract_date_from_slug(event_slug)

        if not city or not market_date:
            logger.debug(f"Could not parse city/date from slug: {event_slug}")
            return []

        results = []
        for mkt in markets_raw:
            question = mkt.get("question", "")
            slug = mkt.get("slug", "")
            condition_id = mkt.get("conditionId", "")

            # Parse token IDs - outcomes[0] is typically YES
            tokens_raw = mkt.get("clobTokenIds", "")
            if isinstance(tokens_raw, str):
                try:
                    tokens_raw = json.loads(tokens_raw)
                except (json.JSONDecodeError, TypeError):
                    tokens_raw = []

            if not tokens_raw:
                continue

            yes_token = tokens_raw[0]

            # Parse bucket range from question
            bucket = self._parse_bucket(question)
            if not bucket:
                logger.debug(f"Could not parse bucket from: {question}")
                continue

            low, high, label = bucket

            results.append(WeatherMarket(
                slug=slug,
                question=question,
                event_slug=event_slug,
                condition_id=condition_id,
                token_id=yes_token,
                bucket_low=low,
                bucket_high=high,
                bucket_label=label,
                best_ask=0.0,
                end_date=end_date,
                city=city,
                date=market_date,
            ))

        return results

    def _parse_bucket(self, question: str) -> Optional[Tuple[float, float, str]]:
        """
        Parse temperature bucket from market question.
        Returns (low, high, label) or None.

        Real examples from Polymarket:
        "...be between 30-31°F on February 23?"     → (30, 32, "30-31F")
        "...be 45°F or below on February 22?"        → (-200, 46, "≤45F")
        "...be 57°F or higher on February 22?"       → (57, 200, "57F+")
        "...be 16°C on February 22?"                 → (16, 17, "16C")
        "...be -6°C or below on February 22?"        → (-200, -5, "≤-6C")
        "...be 4°C or below on February 22?"         → (-200, 5, "≤4C")
        "...be 12°C or higher on February 22?"       → (12, 200, "12C+")
        """
        q = question

        # Pattern: "between X-Y°F" or "between X-Y°C"
        m = re.search(r'between\s+(-?\d+)[–\-](-?\d+)\s*°?\s*([FC])', q, re.IGNORECASE)
        if m:
            low = float(m.group(1))
            high = float(m.group(2)) + 1  # "30-31" means 30.0 to 31.999
            unit = "F" if m.group(3).upper() == "F" else "C"
            return (low, high, f"{low:.0f}-{high-1:.0f}{unit}")

        # Pattern: "X°F or higher" / "X°C or higher"
        m = re.search(r'(-?\d+)\s*°?\s*([FC])\s+or\s+higher', q, re.IGNORECASE)
        if m:
            low = float(m.group(1))
            unit = "F" if m.group(2).upper() == "F" else "C"
            return (low, 200, f"{low:.0f}{unit}+")

        # Pattern: "X°F or below" / "X°C or below"
        m = re.search(r'(-?\d+)\s*°?\s*([FC])\s+or\s+(?:below|lower)', q, re.IGNORECASE)
        if m:
            high = float(m.group(1)) + 1
            unit = "F" if m.group(2).upper() == "F" else "C"
            return (-200, high, f"<={high-1:.0f}{unit}")

        # Pattern: single temperature "be X°C" or "be X°F" (1-degree buckets)
        m = re.search(r'be\s+(-?\d+)\s*°?\s*([FC])\b', q, re.IGNORECASE)
        if m:
            temp = float(m.group(1))
            unit = "F" if m.group(2).upper() == "F" else "C"
            return (temp, temp + 1, f"{temp:.0f}{unit}")

        return None

    def get_best_ask(self, token_id: str) -> Optional[float]:
        """Get current best ask price for a YES token from CLOB."""
        try:
            resp = requests.get(f"{CLOB_API}/book", params={
                "token_id": token_id
            }, timeout=10)
            if resp.status_code != 200:
                return None
            book = resp.json()
            asks = book.get("asks", [])
            if not asks:
                return None
            return min(float(a["price"]) for a in asks)
        except Exception:
            return None

    def price_markets(self, markets: List[WeatherMarket]) -> List[WeatherMarket]:
        """Fetch best ask for each market in parallel. Returns only markets with liquidity."""
        def _fetch(mkt):
            ask = self.get_best_ask(mkt.token_id)
            return (mkt, ask)

        priced = []
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [pool.submit(_fetch, mkt) for mkt in markets]
            for fut in as_completed(futures):
                mkt, ask = fut.result()
                if ask is not None and ask > 0:
                    mkt.best_ask = ask
                    priced.append(mkt)
        return priced

    def scan_all(self, city_filter: List[str] = None) -> List[WeatherMarket]:
        """
        Full scan: find all temperature events → parse markets → price them.
        Optional city_filter to limit to specific cities.
        """
        events = self.find_all_temperature_events()
        if not events:
            return []

        all_markets = []
        for evt in events:
            markets = self.parse_temperature_markets(evt)
            if city_filter:
                markets = [m for m in markets if m.city in city_filter]
            all_markets.extend(markets)

        if not all_markets:
            logger.info("No temperature markets parsed from events")
            return []

        logger.info(f"Parsed {len(all_markets)} markets from {len(events)} events, fetching prices...")
        priced = self.price_markets(all_markets)
        logger.info(f"Priced {len(priced)}/{len(all_markets)} markets")
        return priced
