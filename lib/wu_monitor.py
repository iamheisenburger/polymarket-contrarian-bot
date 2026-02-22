"""
Weather Underground intraday monitor.

Fetches current-day observations from WU stations (the EXACT source
Polymarket uses for settlement) to validate model predictions in real-time.

Use cases:
- Before betting: check if current obs is consistent with model prediction
- Intraday: if max temp already exceeds a bucket, skip that bucket
- Post-settlement: verify actual high temp vs model prediction
"""

import logging
from datetime import datetime, timezone
from typing import Dict, Optional

import requests

logger = logging.getLogger(__name__)

# Public WU API key (same one used by wunderground.com frontend)
WU_API_KEY = "e1f10a1e78da46f5b10a1e78da96f525"

# Station code to country mapping for API URL format
# US stations use {STATION}:9:US, others use country-specific codes
STATION_COUNTRIES = {
    "KLGA": "US", "KORD": "US", "KMIA": "US", "KDAL": "US", "KATL": "US",
    "EGLC": "GB", "LFPG": "FR", "CYYZ": "CA", "LTAC": "TR",
    "NZWN": "NZ", "SBGR": "BR", "SAEZ": "AR",
}


def _station_location(station_code: str) -> str:
    """Format station code for WU API location parameter."""
    country = STATION_COUNTRIES.get(station_code, "US")
    return f"{station_code}:9:{country}"


def get_current_observations(
    station_code: str,
    date_str: str,
    unit: str = "fahrenheit",
) -> Optional[Dict]:
    """
    Fetch current-day observations from a WU station.

    Args:
        station_code: ICAO code (e.g. "KLGA")
        date_str: Date in YYYYMMDD format (e.g. "20260222")
        unit: "fahrenheit" or "celsius"

    Returns dict with:
        - observations: list of hourly temp readings
        - max_temp: highest temp observed so far today
        - num_observations: count of readings
        - latest_temp: most recent reading
    """
    location = _station_location(station_code)
    units = "e" if unit == "fahrenheit" else "m"

    try:
        resp = requests.get(
            f"https://api.weather.com/v1/location/{location}/observations/historical.json",
            params={
                "apiKey": WU_API_KEY,
                "units": units,
                "startDate": date_str,
                "endDate": date_str,
            },
            timeout=10,
        )
        if resp.status_code != 200:
            logger.debug(f"WU {station_code}: HTTP {resp.status_code}")
            return None

        data = resp.json()
        obs_list = data.get("observations", [])
        if not obs_list:
            return None

        temps = [o.get("temp") for o in obs_list if o.get("temp") is not None]
        if not temps:
            return None

        return {
            "observations": temps,
            "max_temp": max(temps),
            "min_temp": min(temps),
            "latest_temp": temps[-1],
            "num_observations": len(temps),
            "station": station_code,
        }
    except Exception as e:
        logger.debug(f"WU {station_code} error: {e}")
        return None


def validate_forecast(
    station_code: str,
    date_str: str,
    unit: str,
    model_mean: float,
    bucket_low: float,
    bucket_high: float,
) -> Optional[Dict]:
    """
    Validate a model prediction against current WU observations.

    Returns dict with:
        - current_max: highest temp so far
        - bucket_possible: whether the bucket is still achievable
        - bucket_already_hit: whether current max is already in bucket
        - confidence_adjustment: multiplier for model probability
          - 1.0 = no change
          - 1.3 = current obs supports prediction
          - 0.5 = current obs contradicts prediction
          - 0.0 = bucket is impossible (current max already above bucket)
    """
    obs = get_current_observations(station_code, date_str, unit)
    if not obs:
        return None

    current_max = obs["max_temp"]

    # Check if bucket is still possible
    # Temperature can only go UP during the day (max temp hasn't been reached yet,
    # or has been reached and won't go higher)
    bucket_already_hit = bucket_low <= current_max < bucket_high
    bucket_below_current = bucket_high <= current_max  # bucket ceiling below current max
    bucket_above_current = bucket_low > current_max    # bucket floor above current max

    if bucket_below_current:
        # Current max already exceeds this bucket. If it's a "will the HIGH be X"
        # market, this bucket is impossible (the high is already above it).
        confidence_adjustment = 0.0
        bucket_possible = False
    elif bucket_already_hit:
        # Current max is in this bucket. If temp doesn't rise further, we win.
        # But temp could still rise above the bucket.
        confidence_adjustment = 1.3
        bucket_possible = True
    elif bucket_above_current:
        # Bucket is above current max. Still possible if temp rises.
        # How reasonable is the rise? Compare to model mean.
        gap = bucket_low - current_max
        expected_rise = model_mean - current_max
        if expected_rise > 0 and gap <= expected_rise * 1.5:
            confidence_adjustment = 1.0  # rise is within expected range
        else:
            confidence_adjustment = 0.7  # needs more rise than model expects
        bucket_possible = True
    else:
        confidence_adjustment = 1.0
        bucket_possible = True

    return {
        "current_max": current_max,
        "num_observations": obs["num_observations"],
        "bucket_possible": bucket_possible,
        "bucket_already_hit": bucket_already_hit,
        "confidence_adjustment": confidence_adjustment,
    }
