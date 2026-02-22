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
    search_name: str    # e.g. "new york city", "london"
    # Bias correction: Open-Meteo systematically under/over-reads vs WU station.
    # This value is ADDED to ensemble members before computing probabilities.
    # Positive = OM reads lower than WU, so we shift up.
    bias_correction: float = 0.0
    # Extra std added in quadrature to account for station-model mismatch noise.
    extra_std: float = 0.0
    # ICAO station code for the WU resolution source
    station_code: str = ""


# Airport coordinates matching Polymarket's Weather Underground resolution sources.
# Bias corrections calibrated from Feb 18-21 2026 (OM observed vs WU actual).
# Resolution: https://www.wunderground.com/history/daily/.../{station_code}
CITIES = {
    # bias=+0.0 but high variance (±3.6F) — extra_std compensates
    "NYC": CityConfig("NYC", 40.7772, -73.8726, "fahrenheit", 2, "new york city",
                       bias_correction=0.0, extra_std=3.0, station_code="KLGA"),
    # bias=-0.6C, very consistent
    "London": CityConfig("London", 51.5053, 0.0553, "celsius", 1, "london",
                          bias_correction=0.6, extra_std=0.5, station_code="EGLC"),
    # bias=+0.2F, moderate variance
    "Chicago": CityConfig("Chicago", 41.9742, -87.9073, "fahrenheit", 2, "chicago",
                           bias_correction=-0.2, extra_std=1.5, station_code="KORD"),
    # bias=-0.8F, extremely consistent
    "Miami": CityConfig("Miami", 25.7959, -80.2870, "fahrenheit", 2, "miami",
                         bias_correction=0.8, extra_std=0.3, station_code="KMIA"),
    # bias=-1.5F, moderate variance — Love Field
    "Dallas": CityConfig("Dallas", 32.8471, -96.8518, "fahrenheit", 2, "dallas",
                          bias_correction=1.5, extra_std=1.5, station_code="KDAL"),
    # bias=-0.7C, decent consistency
    "Paris": CityConfig("Paris", 49.0097, 2.5478, "celsius", 1, "paris",
                         bias_correction=0.7, extra_std=0.8, station_code="LFPG"),
    # bias=-1.1C, moderate variance
    "Toronto": CityConfig("Toronto", 43.6772, -79.6306, "celsius", 1, "toronto",
                           bias_correction=1.1, extra_std=1.0, station_code="CYYZ"),
    # bias=-1.4F, moderate variance
    "Atlanta": CityConfig("Atlanta", 33.6407, -84.4277, "fahrenheit", 2, "atlanta",
                           bias_correction=1.4, extra_std=1.0, station_code="KATL"),
    # bias=-1.4C, very consistent
    "Ankara": CityConfig("Ankara", 40.1281, 32.9951, "celsius", 1, "ankara",
                          bias_correction=1.4, extra_std=0.5, station_code="LTAC"),
    # bias=-2.0C, moderately consistent
    "Wellington": CityConfig("Wellington", -41.3272, 174.8051, "celsius", 1, "wellington",
                              bias_correction=2.0, extra_std=0.8, station_code="NZWN"),
    # bias=-1.1C — using Guarulhos airport coords
    "Sao Paulo": CityConfig("Sao Paulo", -23.4356, -46.4731, "celsius", 1, "sao paulo",
                              bias_correction=1.1, extra_std=1.2, station_code="SBGR"),
    # bias=-1.5C — using Ezeiza airport coords
    "Buenos Aires": CityConfig("Buenos Aires", -34.8222, -58.5358, "celsius", 1, "buenos aires",
                                bias_correction=1.5, extra_std=0.8, station_code="SAEZ"),
}

# Cities excluded from defaults due to unreliable OM-WU mapping:
# Seoul (RKSI): -3.8C bias, Incheon grid cell doesn't match station
# Seattle (KSEA): -2.6F avg bias with ±3.5F variance, too noisy
EXCLUDED_CITIES = {
    "Seoul": CityConfig("Seoul", 37.4692, 126.4505, "celsius", 1, "seoul",
                         bias_correction=3.8, extra_std=2.0, station_code="RKSI"),
    "Seattle": CityConfig("Seattle", 47.4502, -122.3088, "fahrenheit", 2, "seattle",
                           bias_correction=2.6, extra_std=3.0, station_code="KSEA"),
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
        bias_correction: float = 0.0,
        extra_std: float = 0.0,
    ) -> List[Dict]:
        """
        Compute probability for each temperature bucket using fitted normal.

        buckets: list of (low, high) tuples, e.g. [(34, 36), (36, 38), ...]
        bias_correction: added to each member to correct OM-WU systematic bias
        extra_std: added in quadrature to ensemble std for station-model noise

        Returns list of {low, high, label, prob_count, prob_normal, prob}
        """
        if not members or not buckets:
            return []

        # Apply bias correction to each member
        corrected = [m + bias_correction for m in members]

        n = len(corrected)
        mean = sum(corrected) / n
        variance = sum((m - mean) ** 2 for m in corrected) / n
        # Add extra uncertainty in quadrature
        std = math.sqrt(variance + extra_std ** 2) if (variance + extra_std ** 2) > 0 else 1.0

        results = []
        for low, high in buckets:
            # Method 1: counting (on bias-corrected members)
            count = sum(1 for m in corrected if low <= m < high)
            prob_count = count / n

            # Method 2: normal CDF integration (with inflated std)
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
