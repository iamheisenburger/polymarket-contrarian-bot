"""
Weather Forecast API — Multi-model ensemble forecasts via Open-Meteo.

v3: Pulls ensemble members from ECMWF, GFS, ICON, and GEM simultaneously.
Computes per-model bucket probabilities, then consensus probability requiring
multiple models to agree before signaling an edge.

Also supports HRRR (3km deterministic) for US same-day forecasts.
"""

import logging
import math
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

ENSEMBLE_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# --------------------------------------------------------------------------
# Model configuration
# --------------------------------------------------------------------------

@dataclass
class ModelConfig:
    """Configuration for a single ensemble weather model."""
    name: str
    request_param: str          # Value for Open-Meteo `models` parameter
    response_suffix: str        # Suffix on response keys when multi-model
    num_members: int            # Expected member count (excluding aggregate)
    weight: float = 1.0         # Relative weight in consensus (adjustable)

# Models available on Open-Meteo ensemble API (single call, free tier)
ENSEMBLE_MODELS = {
    "ecmwf": ModelConfig("ECMWF IFS", "ecmwf_ifs025", "_ecmwf_ifs025_ensemble", 50, weight=1.0),
    "gfs": ModelConfig("GFS", "gfs_seamless", "_ncep_gefs_seamless", 30, weight=1.0),
    "icon": ModelConfig("ICON", "icon_seamless_eps", "_icon_seamless_eps", 39, weight=1.0),
    "gem": ModelConfig("GEM", "gem_global", "_gem_global_ensemble", 20, weight=1.0),
}

# US cities that can use HRRR (3km deterministic, hourly updates)
US_CITIES = {"NYC", "Chicago", "Miami", "Dallas", "Atlanta"}


# --------------------------------------------------------------------------
# City configuration
# --------------------------------------------------------------------------

@dataclass
class CityConfig:
    name: str
    lat: float
    lon: float
    unit: str           # "fahrenheit" or "celsius"
    bucket_size: float  # 2 for NYC (2F buckets), 1 for London (1C buckets)
    search_name: str    # e.g. "new york city", "london"
    bias_correction: float = 0.0
    extra_std: float = 0.0
    station_code: str = ""
    peak_hour_utc: int = 20  # typical peak temp hour in UTC (default ~2PM EST)


# Airport coordinates matching Polymarket's Weather Underground resolution sources.
# Bias corrections calibrated from Feb 18-21 2026 (OM observed vs WU actual).
CITIES = {
    "NYC": CityConfig("NYC", 40.7772, -73.8726, "fahrenheit", 2, "new york city",
                       bias_correction=0.0, extra_std=0.0, station_code="KLGA", peak_hour_utc=20),
    "London": CityConfig("London", 51.5053, 0.0553, "celsius", 1, "london",
                          bias_correction=0.0, extra_std=0.0, station_code="EGLC", peak_hour_utc=15),
    "Chicago": CityConfig("Chicago", 41.9742, -87.9073, "fahrenheit", 2, "chicago",
                           bias_correction=0.0, extra_std=0.0, station_code="KORD", peak_hour_utc=21),
    "Miami": CityConfig("Miami", 25.7959, -80.2870, "fahrenheit", 2, "miami",
                         bias_correction=0.0, extra_std=0.0, station_code="KMIA", peak_hour_utc=20),
    "Dallas": CityConfig("Dallas", 32.8471, -96.8518, "fahrenheit", 2, "dallas",
                          bias_correction=0.0, extra_std=0.0, station_code="KDAL", peak_hour_utc=21),
    "Paris": CityConfig("Paris", 49.0097, 2.5478, "celsius", 1, "paris",
                         bias_correction=0.0, extra_std=0.0, station_code="LFPG", peak_hour_utc=14),
    "Toronto": CityConfig("Toronto", 43.6772, -79.6306, "celsius", 1, "toronto",
                           bias_correction=0.0, extra_std=0.0, station_code="CYYZ", peak_hour_utc=20),
    "Atlanta": CityConfig("Atlanta", 33.6407, -84.4277, "fahrenheit", 2, "atlanta",
                           bias_correction=0.0, extra_std=0.0, station_code="KATL", peak_hour_utc=20),
    "Ankara": CityConfig("Ankara", 40.1281, 32.9951, "celsius", 1, "ankara",
                          bias_correction=0.0, extra_std=0.0, station_code="LTAC", peak_hour_utc=12),
    "Wellington": CityConfig("Wellington", -41.3272, 174.8051, "celsius", 1, "wellington",
                              bias_correction=0.0, extra_std=0.0, station_code="NZWN", peak_hour_utc=2),
    "Sao Paulo": CityConfig("Sao Paulo", -23.4356, -46.4731, "celsius", 1, "sao paulo",
                              bias_correction=0.0, extra_std=0.0, station_code="SBGR", peak_hour_utc=18),
    "Buenos Aires": CityConfig("Buenos Aires", -34.8222, -58.5358, "celsius", 1, "buenos aires",
                                bias_correction=0.0, extra_std=0.0, station_code="SAEZ", peak_hour_utc=18),
}

EXCLUDED_CITIES = {
    "Seoul": CityConfig("Seoul", 37.4692, 126.4505, "celsius", 1, "seoul",
                         bias_correction=0.0, extra_std=0.0, station_code="RKSI", peak_hour_utc=6),
    "Seattle": CityConfig("Seattle", 47.4502, -122.3088, "fahrenheit", 2, "seattle",
                           bias_correction=0.0, extra_std=0.0, station_code="KSEA", peak_hour_utc=22),
}


# --------------------------------------------------------------------------
# Core forecaster
# --------------------------------------------------------------------------

class WeatherForecaster:
    """Multi-model ensemble forecaster with consensus probability."""

    def get_ensemble_forecast(self, city: str, days: int = 3) -> Dict[str, List[float]]:
        """
        Legacy single-model ECMWF forecast. Kept for backward compatibility.
        Returns {date_str: [member_temps...]}
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

    # Deterministic model params for fallback (regular forecast API)
    DETERMINISTIC_MODELS = {
        "ecmwf": "ecmwf_ifs025",
        "gfs": "gfs_seamless",
        "icon": "icon_seamless",
        "gem": "gem_seamless",
    }

    def get_multi_model_forecast(
        self, city: str, days: int = 3, models: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, List[float]]]:
        """
        Fetch ensemble forecasts from multiple models in a SINGLE API call.
        Falls back to deterministic multi-model if ensemble API is rate limited.

        Returns {date_str: {model_key: [member_temps...]}}
        e.g. {"2026-02-23": {"ecmwf": [5.1, 5.3, ...], "gfs": [5.0, 5.4, ...], ...}}
        """
        cfg = CITIES.get(city)
        if not cfg:
            logger.error(f"Unknown city: {city}")
            return {}

        if models is None:
            models = list(ENSEMBLE_MODELS.keys())

        model_params = ",".join(ENSEMBLE_MODELS[m].request_param for m in models if m in ENSEMBLE_MODELS)

        try:
            resp = requests.get(ENSEMBLE_URL, params={
                "latitude": cfg.lat,
                "longitude": cfg.lon,
                "daily": "temperature_2m_max",
                "temperature_unit": cfg.unit,
                "timezone": "auto",
                "models": model_params,
                "forecast_days": days,
            }, timeout=30)
            # Check for rate limiting before raise_for_status
            if resp.status_code == 429 or "limit exceeded" in resp.text.lower():
                logger.warning(f"{city}: Ensemble API rate limited, using deterministic fallback")
                return self._get_deterministic_fallback(city, cfg, days, models)
            resp.raise_for_status()
            data = resp.json()
        except requests.exceptions.HTTPError as e:
            if "429" in str(e) or "limit" in str(e).lower():
                logger.warning(f"{city}: Ensemble API rate limited, using deterministic fallback")
                return self._get_deterministic_fallback(city, cfg, days, models)
            logger.error(f"Multi-model API error for {city}: {e}")
            return {}
        except Exception as e:
            logger.error(f"Multi-model API error for {city}: {e}")
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])

        result: Dict[str, Dict[str, List[float]]] = {}
        for i, date in enumerate(dates):
            result[date] = {}
            for model_key in models:
                mcfg = ENSEMBLE_MODELS.get(model_key)
                if not mcfg:
                    continue
                suffix = mcfg.response_suffix
                members = []
                for key, values in daily.items():
                    if (key.startswith("temperature_2m_max_member") and
                            key.endswith(suffix) and i < len(values)):
                        val = values[i]
                        if val is not None:
                            members.append(float(val))
                if members:
                    result[date][model_key] = members

        total_models = sum(1 for d in result.values() for _ in d)
        if result:
            first_date = list(result.keys())[0]
            model_summary = ", ".join(
                f"{k}={len(v)}" for k, v in result[first_date].items()
            )
            logger.info(f"{city}: multi-model [{model_summary}], {len(result)} days")

        return result

    def _get_deterministic_fallback(
        self, city: str, cfg, days: int, models: List[str]
    ) -> Dict[str, Dict[str, List[float]]]:
        """
        Fallback: use regular forecast API with multiple deterministic models.
        Generates synthetic ensemble members around each model's point estimate
        to produce data compatible with the ensemble probability computation.
        """
        import random

        det_params = ",".join(
            self.DETERMINISTIC_MODELS.get(m, m)
            for m in models if m in self.DETERMINISTIC_MODELS
        )
        try:
            resp = requests.get(FORECAST_URL, params={
                "latitude": cfg.lat,
                "longitude": cfg.lon,
                "daily": "temperature_2m_max",
                "temperature_unit": cfg.unit,
                "timezone": "auto",
                "models": det_params,
                "forecast_days": days,
            }, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"Deterministic fallback error for {city}: {e}")
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])

        # Map response keys back to our model keys
        model_key_map = {
            f"temperature_2m_max_{v}": k
            for k, v in self.DETERMINISTIC_MODELS.items()
        }

        result: Dict[str, Dict[str, List[float]]] = {}
        for i, date in enumerate(dates):
            result[date] = {}
            for resp_key, model_key in model_key_map.items():
                if model_key not in models:
                    continue
                vals = daily.get(resp_key, [])
                if i < len(vals) and vals[i] is not None:
                    point = float(vals[i])
                    # Generate 15 synthetic members around the point estimate
                    # Use std=1.5 (typical model uncertainty for daily max temp)
                    std = 1.5
                    random.seed(hash((city, date, model_key)))
                    members = [round(random.gauss(point, std), 1) for _ in range(15)]
                    result[date][model_key] = members

        if result:
            first_date = list(result.keys())[0]
            model_summary = ", ".join(
                f"{k}={len(v)}" for k, v in result[first_date].items()
            )
            logger.info(f"{city}: FALLBACK deterministic [{model_summary}], {len(result)} days")

        return result

    def get_hrrr_forecast(self, city: str, days: int = 2) -> Dict[str, float]:
        """
        Fetch HRRR deterministic forecast for US cities.
        3km resolution, updated hourly, best for same-day predictions.

        Returns {date_str: max_temp_value}
        """
        if city not in US_CITIES:
            return {}

        cfg = CITIES.get(city)
        if not cfg:
            return {}

        try:
            resp = requests.get(FORECAST_URL, params={
                "latitude": cfg.lat,
                "longitude": cfg.lon,
                "daily": "temperature_2m_max",
                "temperature_unit": cfg.unit,
                "timezone": "auto",
                "models": "ncep_hrrr_conus",
                "forecast_days": min(days, 2),  # HRRR only goes 2 days
            }, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"HRRR API error for {city}: {e}")
            return {}

        daily = data.get("daily", {})
        dates = daily.get("time", [])
        temps = daily.get("temperature_2m_max", [])

        result = {}
        for date, temp in zip(dates, temps):
            if temp is not None:
                result[date] = float(temp)

        if result:
            logger.info(f"{city} HRRR: {result}")

        return result

    # ------------------------------------------------------------------
    # Probability computation
    # ------------------------------------------------------------------

    @staticmethod
    def compute_bucket_probabilities(
        members: List[float],
        buckets: List[Tuple[float, float]],
        bias_correction: float = 0.0,
        extra_std: float = 0.0,
    ) -> List[Dict]:
        """
        Legacy single-model probability computation.
        Kept for backward compatibility.
        """
        if not members or not buckets:
            return []

        corrected = [m + bias_correction for m in members]
        n = len(corrected)
        mean = sum(corrected) / n
        variance = sum((m - mean) ** 2 for m in corrected) / n
        std = math.sqrt(variance + extra_std ** 2) if (variance + extra_std ** 2) > 0 else 1.0

        results = []
        for low, high in buckets:
            count = sum(1 for m in corrected if low <= m < high)
            prob_count = count / n

            z_low = (low - mean) / std
            z_high = (high - mean) / std
            prob_normal = _normal_cdf(z_high) - _normal_cdf(z_low)

            prob = 0.6 * prob_normal + 0.4 * prob_count

            label = f"{low:.0f}-{high:.0f}"
            results.append({
                "low": low, "high": high, "label": label,
                "prob_count": round(prob_count, 4),
                "prob_normal": round(prob_normal, 4),
                "prob": round(prob, 4),
            })

        return results

    @staticmethod
    def compute_consensus_probability(
        model_members: Dict[str, List[float]],
        buckets: List[Tuple[float, float]],
        bias_correction: float = 0.0,
        extra_std: float = 0.0,
        hrrr_temp: Optional[float] = None,
        min_models_agree: int = 2,
    ) -> List[Dict]:
        """
        Multi-model consensus probability for each bucket.

        Each model computes its own probability independently. The consensus
        is a weighted average. Buckets where fewer than `min_models_agree`
        models show >5% probability are penalized (halved).

        If HRRR temp is provided, it's used as an additional signal:
        buckets containing the HRRR temp get a confidence boost.

        Returns list of {low, high, label, prob, per_model, models_agreeing, hrrr_boost}
        """
        if not model_members or not buckets:
            return []

        per_model_probs: Dict[str, List[float]] = {}  # model -> [prob_per_bucket]
        per_model_means: Dict[str, float] = {}

        for model_key, members in model_members.items():
            if not members:
                continue
            mcfg = ENSEMBLE_MODELS.get(model_key)
            weight = mcfg.weight if mcfg else 1.0

            corrected = [m + bias_correction for m in members]
            n = len(corrected)
            mean = sum(corrected) / n
            variance = sum((m - mean) ** 2 for m in corrected) / n
            std = math.sqrt(variance + extra_std ** 2) if (variance + extra_std ** 2) > 0 else 1.0

            per_model_means[model_key] = mean
            bucket_probs = []
            for low, high in buckets:
                count = sum(1 for m in corrected if low <= m < high)
                prob_count = count / n

                z_low = (low - mean) / std
                z_high = (high - mean) / std
                prob_normal = _normal_cdf(z_high) - _normal_cdf(z_low)

                prob = 0.6 * prob_normal + 0.4 * prob_count
                bucket_probs.append(prob)

            per_model_probs[model_key] = bucket_probs

        if not per_model_probs:
            return []

        # Compute weighted consensus for each bucket
        results = []
        for i, (low, high) in enumerate(buckets):
            total_weight = 0.0
            weighted_prob = 0.0
            models_above_threshold = 0
            per_model_detail = {}

            for model_key, probs in per_model_probs.items():
                mcfg = ENSEMBLE_MODELS.get(model_key)
                weight = mcfg.weight if mcfg else 1.0
                prob = probs[i]

                weighted_prob += prob * weight
                total_weight += weight
                per_model_detail[model_key] = round(prob, 4)

                if prob >= 0.05:  # Model thinks >=5% chance
                    models_above_threshold += 1

            consensus_prob = weighted_prob / total_weight if total_weight > 0 else 0.0

            # Reject low-agreement buckets: if fewer than min_models_agree
            # see meaningful probability, zero out the consensus
            if models_above_threshold < min_models_agree:
                consensus_prob = 0.0

            # HRRR boost: if HRRR deterministic temp falls in this bucket,
            # boost consensus by 20% (capped at 0.95)
            hrrr_boost = False
            if hrrr_temp is not None and low <= hrrr_temp < high:
                consensus_prob = min(0.95, consensus_prob * 1.2)
                hrrr_boost = True

            label = f"{low:.0f}-{high:.0f}"
            results.append({
                "low": low,
                "high": high,
                "label": label,
                "prob": round(consensus_prob, 4),
                "per_model": per_model_detail,
                "models_agreeing": models_above_threshold,
                "hrrr_boost": hrrr_boost,
                # Legacy compat fields
                "prob_count": round(consensus_prob, 4),
                "prob_normal": round(consensus_prob, 4),
            })

        return results


def _normal_cdf(x: float) -> float:
    """Approximation of the standard normal CDF."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))
