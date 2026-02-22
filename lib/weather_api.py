"""
Weather Forecast API — Open-Meteo ensemble forecasts.

Pulls 51 ECMWF ensemble members for daily max temperature,
fits a normal distribution, and computes bucket probabilities
for Polymarket weather markets.
"""

import logging
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"


@dataclass
class CityConfig:
    name: str
    lat: float
    lon: float
    unit: str           # "fahrenheit" or "celsius"
    bucket_size: float  # 2 for NYC (2F buckets), 1 for London (1C buckets)
    # Polymarket search terms
    search_name: str    # e.g. "new york city", "london"


# Weather station coordinates matching Polymarket resolution sources
CITIES = {
    "NYC": CityConfig("NYC", 40.7728, -73.87, "fahrenheit", 2, "new york city"),
    "London": CityConfig("London", 51.5053, 0.0553, "celsius", 1, "london"),
    "Chicago": CityConfig("Chicago", 41.9742, -87.9073, "fahrenheit", 2, "chicago"),
    "Miami": CityConfig("Miami", 25.7959, -80.2870, "fahrenheit", 2, "miami"),
    "Dallas": CityConfig("Dallas", 32.8998, -97.0403, "fahrenheit", 2, "dallas"),
    "Seattle": CityConfig("Seattle", 47.4502, -122.3088, "fahrenheit", 2, "seattle"),
    "Seoul": CityConfig("Seoul", 37.4602, 126.4407, "celsius", 1, "seoul"),
    "Paris": CityConfig("Paris", 49.0097, 2.5478, "celsius", 1, "paris"),
    "Toronto": CityConfig("Toronto", 43.6777, -79.6248, "celsius", 1, "toronto"),
    "Atlanta": CityConfig("Atlanta", 33.6407, -84.4277, "fahrenheit", 2, "atlanta"),
    "Ankara": CityConfig("Ankara", 40.1281, 32.9951, "celsius", 1, "ankara"),
    "Wellington": CityConfig("Wellington", -41.3272, 174.8051, "celsius", 1, "wellington"),
    "Sao Paulo": CityConfig("Sao Paulo", -23.6273, -46.6566, "celsius", 1, "sao paulo"),
    "Buenos Aires": CityConfig("Buenos Aires", -34.5592, -58.4156, "celsius", 1, "buenos aires"),
}


class WeatherForecaster:
    """Fetches ensemble forecasts and computes bucket probabilities."""

    def get_ensemble_forecast(self, city: str, days: int = 3) -> Dict[str, List[float]]:
        """
        Get ECMWF ensemble forecast for daily max temperature.
        Returns {date_str: [member_1_temp, member_2_temp, ..., member_51_temp]}
        """
        cfg = CITIES.get(city)
        if not cfg:
            logger.error(f"Unknown city: {city}")
            return {}

        try:
            resp = requests.get(ENSEMBLE_URL, params={
                "latitude": cfg.lat,
                "longitude": cfg.lon,
                "daily": "temperature_2m_max",
                "temperature_unit": cfg.unit,
                "timezone": "auto",
                "models": "ecmwf_ifs025",
                "forecast_days": days,
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Ensemble API error for {city}: {e}")
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])

        # Collect all ensemble member columns
        result = {}
        for i, date in enumerate(dates):
            members = []
            for key, values in daily.items():
                if key.startswith("temperature_2m_max_member") and i < len(values):
                    val = values[i]
                    if val is not None:
                        members.append(float(val))
            if members:
                result[date] = members

        if result:
            first_date = list(result.keys())[0]
            logger.info(f"{city}: {len(result[first_date])} ensemble members, {len(result)} days")

        return result

    @staticmethod
    def compute_bucket_probabilities(
        members: List[float],
        buckets: List[Tuple[float, float]],
    ) -> List[Dict]:
        """
        Compute probability for each temperature bucket using fitted normal.

        buckets: list of (low, high) tuples, e.g. [(34, 36), (36, 38), ...]
        Returns list of {low, high, label, prob_count, prob_normal, prob}
        """
        if not members or not buckets:
            return []

        n = len(members)
        mean = sum(members) / n
        variance = sum((m - mean) ** 2 for m in members) / n
        std = math.sqrt(variance) if variance > 0 else 1.0

        results = []
        for low, high in buckets:
            # Method 1: counting
            count = sum(1 for m in members if low <= m < high)
            prob_count = count / n

            # Method 2: normal CDF integration
            z_low = (low - mean) / std
            z_high = (high - mean) / std
            prob_normal = _normal_cdf(z_high) - _normal_cdf(z_low)

            # Blend: 60% normal (smoother), 40% counting (empirical)
            prob = 0.6 * prob_normal + 0.4 * prob_count

            label = f"{low:.0f}-{high:.0f}"
            results.append({
                "low": low,
                "high": high,
                "label": label,
                "prob_count": round(prob_count, 4),
                "prob_normal": round(prob_normal, 4),
                "prob": round(prob, 4),
            })

        return results


def _normal_cdf(x: float) -> float:
    """Approximation of the standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
