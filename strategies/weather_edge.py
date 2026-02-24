"""
Weather Edge Strategy v3 — Multi-model consensus forecasting.

Pulls ensemble members from 4 independent models (ECMWF, GFS, ICON, GEM)
via Open-Meteo and computes consensus bucket probabilities. Only trades when
multiple models agree on a bucket, eliminating single-model overconfidence.

Also uses HRRR (3km deterministic) for US same-day temperature predictions
and WU intraday cross-reference to kill impossible bets.

Key v3 changes vs v2:
- 4 models (143 members) instead of 1 model (51 members)
- Consensus: only trade when min_models_agree models see >=5% probability
- Max 1 bet per city-date (best edge only, no bucket scatter)
- HRRR boost for US same-day markets
- WU cross-reference: for same-day markets, check actual station readings before betting
"""

import asyncio
import csv
import json
import logging
import math
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from lib.weather_api import CITIES, US_CITIES, ENSEMBLE_MODELS, WeatherForecaster
from lib.weather_markets import WeatherMarket, WeatherMarketScanner
from lib.wu_monitor import get_current_observations, validate_forecast
from src.bot import TradingBot

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class WeatherConfig:
    """Configuration for the weather edge bot v3 — Temperature Laddering."""
    bankroll: float = 50.0
    observe_only: bool = True
    min_edge: float = 0.05          # 5% min edge — only bet on clear mispricings
    kelly_fraction: float = 0.5     # half-Kelly
    max_position_pct: float = 0.15  # max 15% bankroll per trade
    min_entry_price: float = 0.05   # no tail chasing — min $0.05 entry
    max_entry_price: float = 0.50   # raised for prob-sorted ladder (peak buckets cost more)
    max_model_prob: float = 0.50    # consensus overconfidence cap
    min_models_agree: int = 3       # require >=3 of 4 models to show >=5% for bucket
    max_bets_per_city_date: int = 2 # ladder: bet on top 2 buckets per city-date (tight)
    max_cost_per_city_date: float = 1.50  # budget cap per city-date (ladder total)
    use_hrrr: bool = True           # use HRRR for US same-day predictions
    scan_interval: int = 600        # rescan every 10 min (avoid Open-Meteo 429 rate limits)
    settle_interval: int = 60       # check settlements every 60s
    cities: list = field(default_factory=lambda: list(CITIES.keys()))
    forecast_days: int = 2          # today + tomorrow


@dataclass
class PaperPosition:
    """Track a paper weather trade."""
    slug: str
    market_question: str
    event_slug: str
    token_id: str
    condition_id: str
    bucket_label: str
    city: str
    market_date: str
    entry_price: float
    model_prob: float
    edge: float
    num_tokens: float
    cost: float
    timestamp: str
    resolved: bool = False
    won: Optional[bool] = None
    payout: float = 0.0


class WeatherEdgeBot:
    """
    Weather temperature edge strategy.
    Uses ECMWF ensemble forecasts vs. Polymarket bucket prices.
    """

    def __init__(self, config: WeatherConfig):
        self.config = config
        self.forecaster = WeatherForecaster()
        self.scanner = WeatherMarketScanner()

        # Live trading bot (initialized only when not observe-only)
        self.trading_bot: Optional[TradingBot] = None
        if not config.observe_only:
            import os
            pk = os.getenv("POLY_PRIVATE_KEY")
            safe = os.getenv("POLY_SAFE_ADDRESS")
            if pk and safe:
                self.trading_bot = TradingBot(private_key=pk, safe_address=safe)
                logger.info(f"Live trading enabled: safe={safe[:10]}...")
            else:
                logger.error("LIVE mode but missing POLY_PRIVATE_KEY or POLY_SAFE_ADDRESS!")
                config.observe_only = True

        # Portfolio tracking
        self.balance = config.bankroll
        self.starting_balance = config.bankroll
        self.positions: Dict[str, PaperPosition] = {}  # slug -> position

        # Stats
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_wagered = 0.0
        self.total_payout = 0.0

        # CSV logger — v5 gets a fresh file
        self.csv_path = Path("data/weather_trades_v5.csv")
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_csv()

        # Pending JSON sidecar (survives restarts)
        self.pending_path = self.csv_path.with_suffix(".pending.json")
        self._load_pending()

        # In live mode: reconcile pending against on-chain, then sync balance
        if self.trading_bot:
            self._reconcile_positions()

        # In live mode, override balance with actual USDC from chain
        # (must come AFTER _load_pending and reconcile so cost deductions are correct)
        if self.trading_bot:
            real_bal = self.trading_bot.get_usdc_balance()
            if real_bal is not None:
                self.balance = real_bal
                self.starting_balance = real_bal
                logger.info(f"Live USDC balance: ${real_bal:.2f}")
            else:
                # CRITICAL: Do NOT trade with a fake balance. Set to 0 so no trades fire
                # until we can successfully query the exchange balance.
                self.balance = 0.0
                self.starting_balance = 0.0
                logger.error(
                    f"CANNOT fetch USDC balance — setting balance to $0 to prevent untracked trades. "
                    f"Will retry on next scan cycle."
                )

    # ------------------------------------------------------------------
    # CSV I/O
    # ------------------------------------------------------------------

    CSV_HEADERS = [
        "timestamp", "city", "market_date", "bucket_label", "market_slug",
        "market_question", "entry_price", "model_prob", "edge",
        "cost", "num_tokens", "result", "payout", "pnl", "balance",
    ]

    def _init_csv(self):
        if not self.csv_path.exists():
            with open(self.csv_path, "w", newline="") as f:
                csv.writer(f).writerow(self.CSV_HEADERS)
        else:
            self._load_stats_from_csv()

    def _load_stats_from_csv(self):
        """Reload stats from existing CSV."""
        try:
            with open(self.csv_path, "r") as f:
                for row in csv.DictReader(f):
                    result = row.get("result", "pending")
                    cost = float(row.get("cost", 0))
                    payout = float(row.get("payout", 0))
                    self.total_trades += 1
                    self.total_wagered += cost
                    if result == "won":
                        self.wins += 1
                        self.total_payout += payout
                    elif result == "lost":
                        self.losses += 1
        except Exception:
            pass

    def _write_resolved(self, pos: PaperPosition, result: str, pnl: float):
        """Write a resolved trade to CSV."""
        with open(self.csv_path, "a", newline="") as f:
            csv.writer(f).writerow([
                pos.timestamp,
                pos.city,
                pos.market_date,
                pos.bucket_label,
                pos.slug,
                pos.market_question[:80],
                f"{pos.entry_price:.4f}",
                f"{pos.model_prob:.4f}",
                f"{pos.edge:.4f}",
                f"{pos.cost:.4f}",
                f"{pos.num_tokens:.2f}",
                result,
                f"{pos.payout:.4f}",
                f"{pnl:.4f}",
                f"{self.balance:.4f}",
            ])

    def _load_pending(self):
        """Load pending positions from JSON sidecar."""
        if not self.pending_path.exists():
            return
        try:
            with open(self.pending_path, "r") as f:
                data = json.load(f)
            for key, d in data.items():
                self.positions[key] = PaperPosition(**d)
            # Deduct existing position costs from balance
            existing_cost = sum(pos.cost for pos in self.positions.values() if not pos.resolved)
            self.balance -= existing_cost
            logger.info(f"Loaded {len(self.positions)} pending weather positions (${existing_cost:.2f} deployed, balance ${self.balance:.2f})")
        except Exception as e:
            logger.warning(f"Failed to load pending: {e}")

    def _save_pending(self):
        """Save pending positions to JSON sidecar."""
        try:
            data = {}
            for key, pos in self.positions.items():
                if not pos.resolved:
                    data[key] = asdict(pos)
            with open(self.pending_path, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.error(f"Failed to save pending: {e}")

    # ------------------------------------------------------------------
    # On-chain reconciliation
    # ------------------------------------------------------------------

    def _reconcile_positions(self):
        """Sync pending file against actual Polymarket positions API.

        - Adds on-chain positions missing from pending (crash recovery)
        - Removes phantom positions not found on-chain
        - Logs every discrepancy
        """
        import re
        safe = self.trading_bot.config.safe_address if self.trading_bot else None
        if not safe:
            logger.warning("No safe address for reconciliation")
            return

        try:
            # Fetch ALL positions from Polymarket (paginate if needed)
            all_positions = []
            offset = 0
            while True:
                r = requests.get(
                    f"https://data-api.polymarket.com/positions",
                    params={"user": safe, "sizeThreshold": 0, "limit": 500, "offset": offset},
                    timeout=15,
                )
                if r.status_code != 200:
                    logger.error(f"Positions API returned {r.status_code}")
                    return
                batch = r.json()
                if not batch:
                    break
                all_positions.extend(batch)
                if len(batch) < 500:
                    break
                offset += len(batch)

            # Filter to weather positions with size > 0
            on_chain = {}
            for p in all_positions:
                title = p.get("title", "")
                size = float(p.get("size", 0))
                slug = p.get("slug", "")
                if "temperature" in title.lower() and size > 0 and slug:
                    on_chain[slug] = p

            logger.info(f"Reconciliation: {len(on_chain)} weather positions on-chain")

            # Find phantoms: in pending but NOT on-chain
            phantoms = []
            for slug, pos in list(self.positions.items()):
                if pos.resolved:
                    continue
                if slug not in on_chain:
                    phantoms.append(slug)
                    logger.warning(
                        f"PHANTOM removed: {pos.city} {pos.market_date} {pos.bucket_label} "
                        f"${pos.cost:.2f} (not on-chain)"
                    )
            for slug in phantoms:
                del self.positions[slug]

            # Update existing positions: sync token counts from on-chain
            updated = 0
            for slug, p in on_chain.items():
                if slug in self.positions and not self.positions[slug].resolved:
                    pos = self.positions[slug]
                    chain_size = float(p.get("size", 0))
                    chain_avg = float(p.get("avgPrice", 0))
                    chain_cost = round(chain_size * chain_avg, 2)
                    if abs(pos.num_tokens - chain_size) > 0.1 or abs(pos.cost - chain_cost) > 0.05:
                        logger.info(
                            f"SYNC {pos.city} {pos.bucket_label}: "
                            f"tokens {pos.num_tokens:.1f}→{chain_size:.1f}, "
                            f"cost ${pos.cost:.2f}→${chain_cost:.2f}"
                        )
                        pos.num_tokens = chain_size
                        pos.entry_price = chain_avg
                        pos.cost = chain_cost
                        updated += 1

            # Find missing: on-chain but NOT in pending
            added = 0
            for slug, p in on_chain.items():
                if slug in self.positions:
                    continue
                # Reconstruct position from on-chain data
                title = p.get("title", "")
                size = float(p.get("size", 0))
                avg_price = float(p.get("avgPrice", 0))
                cost = round(size * avg_price, 2)

                # Extract city
                city = "Unknown"
                m = re.search(r"temperature in (.+?) be", title)
                if m:
                    city = m.group(1)

                # Extract date
                market_date = "Unknown"
                dm = re.search(r"on (February \d+)", title)
                if dm:
                    day = re.search(r"\d+", dm.group(1)).group()
                    market_date = f"2026-02-{int(day):02d}"

                # Extract bucket
                bucket = title.split("be ")[-1].split(" on ")[0] if "be " in title else "?"

                pos = PaperPosition(
                    slug=slug,
                    market_question=title,
                    event_slug=p.get("eventSlug", ""),
                    token_id=p.get("tokenId", p.get("asset", "")),
                    condition_id=p.get("conditionId", ""),
                    bucket_label=bucket,
                    city=city,
                    market_date=market_date,
                    entry_price=avg_price,
                    model_prob=0.0,  # unknown for recovered positions
                    edge=0.0,
                    num_tokens=size,
                    cost=cost,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                self.positions[slug] = pos
                added += 1
                logger.warning(
                    f"RECOVERED from chain: {city} {market_date} {bucket} "
                    f"{size:.1f} tok @ ${avg_price:.3f} = ${cost:.2f}"
                )

            if phantoms or added or updated:
                self._save_pending()
                logger.info(
                    f"Reconciliation complete: removed {len(phantoms)} phantoms, "
                    f"recovered {added} missing, updated {updated} stale. "
                    f"Total tracked: {sum(1 for p in self.positions.values() if not p.resolved)}"
                )
            else:
                logger.info("Reconciliation: pending file matches on-chain perfectly")

        except Exception as e:
            logger.error(f"Reconciliation failed: {e}", exc_info=True)

    # ------------------------------------------------------------------
    # Opportunity scanning
    # ------------------------------------------------------------------

    def scan_opportunities(self) -> List[Dict[str, Any]]:
        """
        Scan all cities: pull multi-model forecasts + market prices, compute
        consensus edge. Only returns opportunities where multiple models agree.
        Limits to max_bets_per_city_date per city-date combination.
        """
        opportunities = []

        # 1. Get all priced markets from Polymarket
        markets = self.scanner.scan_all(city_filter=self.config.cities)
        if not markets:
            logger.info("No priced weather markets found")
            return []

        # 2. Get multi-model forecasts + HRRR for each city in parallel
        cities_needed = set()
        for mkt in markets:
            if CITIES.get(mkt.city):
                cities_needed.add(mkt.city)

        city_forecasts: Dict[str, Dict] = {}     # city -> {date: {model: [members]}}
        city_hrrr: Dict[str, Dict[str, float]] = {}  # city -> {date: temp}

        def _fetch_multi(city_key):
            return city_key, self.forecaster.get_multi_model_forecast(
                city_key, days=self.config.forecast_days
            )

        def _fetch_hrrr(city_key):
            return city_key, self.forecaster.get_hrrr_forecast(city_key, days=2)

        # Serialize ensemble calls with delay to avoid 429 rate limiting
        for city_key in cities_needed:
            _, data = _fetch_multi(city_key)
            city_forecasts[city_key] = data
            time.sleep(5)  # 5s gap between ensemble API calls

        # HRRR uses different endpoint (api.open-meteo.com), can be parallel
        if self.config.use_hrrr:
            with ThreadPoolExecutor(max_workers=3) as pool:
                hrrr_futs = [pool.submit(_fetch_hrrr, c) for c in cities_needed if c in US_CITIES]
                for fut in as_completed(hrrr_futs):
                    city_key, data = fut.result()
                    city_hrrr[city_key] = data

        # 3. Match markets to forecasts and compute consensus edge
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        current_utc_hour = datetime.now(timezone.utc).hour

        for mkt in markets:
            forecasts = city_forecasts.get(mkt.city, {})
            if mkt.date not in forecasts:
                continue
            model_members = forecasts[mkt.date]  # {model_key: [members]}

            if not model_members:
                continue

            city_cfg = CITIES.get(mkt.city)
            bias = city_cfg.bias_correction if city_cfg else 0.0
            extra = city_cfg.extra_std if city_cfg else 0.0

            # Get HRRR temp if available for this city-date
            hrrr_temp = city_hrrr.get(mkt.city, {}).get(mkt.date)

            # Late entry gate: ONLY allow same-day bets AFTER peak temp hour
            # Before peak, the max temp hasn't been reached — predictions unreliable
            # After peak, WU station data shows the actual max
            if mkt.date == today_str and city_cfg and city_cfg.peak_hour_utc is not None:
                before_peak = current_utc_hour < city_cfg.peak_hour_utc
                if before_peak:
                    logger.info(
                        f"LATE ENTRY GATE {mkt.city} {mkt.bucket_label} {mkt.date}: "
                        f"UTC {current_utc_hour}h < peak {city_cfg.peak_hour_utc}h — too early"
                    )
                    continue

            # HRRR Hard Gate: block US city entries where HRRR contradicts bucket
            if hrrr_temp is not None and mkt.city in US_CITIES:
                threshold = 2.0 if city_cfg.unit == "fahrenheit" else 1.0
                if hrrr_temp < mkt.bucket_low - threshold or hrrr_temp > mkt.bucket_high + threshold:
                    logger.info(
                        f"HRRR BLOCK {mkt.city} {mkt.bucket_label} {mkt.date}: "
                        f"HRRR={hrrr_temp:.1f}, bucket={mkt.bucket_low:.0f}-{mkt.bucket_high:.0f}, "
                        f"threshold={threshold}"
                    )
                    continue

            # WU Hard Gate: for same-day markets, check station observations
            # If current max already exceeds bucket ceiling, this bucket is DEAD
            wu_size_mult = 1.0
            if mkt.date == today_str and city_cfg and city_cfg.station_code:
                wu_date = datetime.now(timezone.utc).strftime("%Y%m%d")
                try:
                    wu_obs = get_current_observations(
                        city_cfg.station_code, wu_date, city_cfg.unit
                    )
                except Exception:
                    wu_obs = None

                if wu_obs:
                    wu_max = wu_obs["max_temp"]
                    if wu_max >= mkt.bucket_high:
                        # Current max already above this bucket — impossible
                        logger.info(
                            f"WU GATE REJECT {mkt.city} {mkt.bucket_label} {mkt.date}: "
                            f"WU max={wu_max:.0f} >= bucket ceiling {mkt.bucket_high:.0f}"
                        )
                        continue
                    elif mkt.bucket_low <= wu_max < mkt.bucket_high:
                        # Currently IN this bucket — boost confidence
                        wu_size_mult = 1.5
                        logger.info(
                            f"WU GATE CONFIRM {mkt.city} {mkt.bucket_label} {mkt.date}: "
                            f"WU max={wu_max:.0f} IN bucket [{mkt.bucket_low:.0f}-{mkt.bucket_high:.0f}]"
                        )
                    else:
                        # Bucket still reachable
                        wu_size_mult = 1.0
                else:
                    # Same-day but no WU data — reduce confidence
                    wu_size_mult = 0.5

            # For next-day markets with no WU data, full confidence in models
            # (wu_size_mult stays 1.0)

            # Compute consensus probability
            bucket_probs = self.forecaster.compute_consensus_probability(
                model_members,
                [(mkt.bucket_low, mkt.bucket_high)],
                bias_correction=bias,
                extra_std=extra,
                hrrr_temp=hrrr_temp,
                min_models_agree=self.config.min_models_agree,
            )
            if not bucket_probs:
                continue

            bp = bucket_probs[0]
            model_prob = bp["prob"]
            edge = model_prob - mkt.best_ask

            if edge >= self.config.min_edge:
                # Compute consensus mean for diagnostics
                all_members = []
                for members in model_members.values():
                    all_members.extend(members)
                mean = sum(all_members) / len(all_members) if all_members else 0
                variance = sum((m - mean) ** 2 for m in all_members) / len(all_members) if all_members else 0
                std = math.sqrt(variance + extra ** 2)

                opportunities.append({
                    "market": mkt,
                    "model_prob": model_prob,
                    "prob_count": bp["prob_count"],
                    "prob_normal": bp["prob_normal"],
                    "edge": edge,
                    "members_mean": mean,
                    "members_std": std,
                    "models_agreeing": bp.get("models_agreeing", 0),
                    "per_model": bp.get("per_model", {}),
                    "hrrr_boost": bp.get("hrrr_boost", False),
                    "wu_size_mult": wu_size_mult,
                })

        # Sort by model probability descending (center ladder on peak, not tails)
        # Tiebreak by edge so we still prefer underpriced within the same prob tier
        opportunities.sort(key=lambda x: (x["model_prob"], x["edge"]), reverse=True)

        # Limit to max_bets_per_city_date per city-date
        if self.config.max_bets_per_city_date > 0:
            seen: Dict[str, int] = {}  # "city|date" -> count
            filtered = []
            for opp in opportunities:
                mkt = opp["market"]
                key = f"{mkt.city}|{mkt.date}"
                count = seen.get(key, 0)
                if count < self.config.max_bets_per_city_date:
                    filtered.append(opp)
                    seen[key] = count + 1
            if len(filtered) < len(opportunities):
                logger.info(f"City-date limit: {len(opportunities)} → {len(filtered)} opportunities")
            opportunities = filtered

        return opportunities

    def evaluate_opportunity(self, opp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Apply filters, WU cross-reference, and Kelly sizing to an opportunity.
        Returns trade dict or None.
        """
        mkt: WeatherMarket = opp["market"]
        model_prob = opp["model_prob"]
        price = mkt.best_ask

        # Price range filter
        if price < self.config.min_entry_price or price > self.config.max_entry_price:
            logger.debug(f"SKIP {mkt.city} {mkt.bucket_label}: price ${price:.3f} outside [{self.config.min_entry_price}, {self.config.max_entry_price}]")
            return None

        # Model overconfidence filter — relaxed when HRRR confirms
        prob_cap = 0.85 if opp.get("hrrr_boost", False) else self.config.max_model_prob
        if model_prob > prob_cap:
            logger.debug(f"SKIP {mkt.city} {mkt.bucket_label}: model_prob {model_prob:.1%} > cap {prob_cap:.0%}")
            return None

        # Already have position for this exact bucket
        if mkt.slug in self.positions:
            return None

        # Laddering: allow multiple buckets per city-date, but cap count and budget
        city_date_key = f"{mkt.city}|{mkt.date}"
        existing_for_cd = [
            pos for pos in self.positions.values()
            if not pos.resolved and f"{pos.city}|{pos.market_date}" == city_date_key
        ]
        if len(existing_for_cd) >= self.config.max_bets_per_city_date:
            return None
        existing_cost_cd = sum(pos.cost for pos in existing_for_cd)
        if existing_cost_cd >= self.config.max_cost_per_city_date:
            return None

        # WU cross-reference for same-day markets
        wu_adj = 1.0
        wu_info = None
        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        city_cfg = CITIES.get(mkt.city)
        if city_cfg and city_cfg.station_code and mkt.date == today_str:
            wu_date = datetime.now(timezone.utc).strftime("%Y%m%d")
            try:
                wu_info = validate_forecast(
                    station_code=city_cfg.station_code,
                    date_str=wu_date,
                    unit=city_cfg.unit,
                    model_mean=opp["members_mean"],
                    bucket_low=mkt.bucket_low,
                    bucket_high=mkt.bucket_high,
                )
            except Exception as e:
                logger.debug(f"WU check failed for {mkt.city}: {e}")

            if wu_info:
                wu_adj = wu_info["confidence_adjustment"]
                if not wu_info["bucket_possible"]:
                    logger.info(
                        f"WU KILL {mkt.city} {mkt.bucket_label}: current max "
                        f"{wu_info['current_max']:.0f} already above bucket "
                        f"({wu_info['num_observations']} obs)"
                    )
                    return None
                if wu_adj < 1.0:
                    logger.info(
                        f"WU ADJ {mkt.city} {mkt.bucket_label}: adj={wu_adj:.1f} "
                        f"(current max={wu_info['current_max']:.0f}, "
                        f"{wu_info['num_observations']} obs)"
                    )
                elif wu_adj > 1.0:
                    logger.info(
                        f"WU BOOST {mkt.city} {mkt.bucket_label}: adj={wu_adj:.1f} "
                        f"(current max IN bucket, {wu_info['num_observations']} obs)"
                    )

                # Apply WU adjustment to model probability
                model_prob = min(0.95, model_prob * wu_adj)
                # Recheck edge after WU adjustment
                if model_prob - price < self.config.min_edge:
                    logger.debug(f"SKIP {mkt.city} {mkt.bucket_label}: edge gone after WU adj")
                    return None

        # Kelly sizing
        if price <= 0 or price >= 1:
            return None
        b = (1.0 - price) / price  # net odds (buy at price, win 1.0)
        kelly_f = (model_prob * b - (1 - model_prob)) / b if b > 0 else 0
        kelly_f = max(0, min(kelly_f, self.config.max_position_pct))
        kelly_f *= self.config.kelly_fraction  # half-Kelly

        # Apply WU size multiplier (from scan_opportunities WU hard gate)
        wu_size_mult = opp.get("wu_size_mult", 1.0)

        bet_amount = self.balance * kelly_f * wu_size_mult

        # Laddering: cap each leg to fit within city-date budget
        remaining_budget = self.config.max_cost_per_city_date - existing_cost_cd
        if remaining_budget < 1.0:
            return None
        # HRRR-priority: confirmed legs get 70% of remaining budget, tails get 30%
        # Only apply split for US cities where HRRR data exists
        if mkt.city in US_CITIES:
            if opp.get("hrrr_boost", False):
                leg_budget = remaining_budget * 0.70
            else:
                leg_budget = remaining_budget * 0.30
            bet_amount = min(bet_amount, leg_budget)
        else:
            # International cities: no HRRR, equal budget access
            bet_amount = min(bet_amount, remaining_budget)

        if bet_amount < 1.0:
            # Force $1.00 minimum (Polymarket floor) if balance allows
            bet_amount = 1.0
        if bet_amount > self.balance:
            return None

        num_tokens = math.ceil(bet_amount / price)  # round UP to meet $1.00 min cost
        num_tokens = max(5, num_tokens)
        cost = round(num_tokens * price, 2)

        if cost > self.balance:
            num_tokens = int(self.balance / price)
            cost = round(num_tokens * price, 2)
        if cost < 1.0 or num_tokens < 5:
            return None

        return {
            "market": mkt,
            "model_prob": model_prob,
            "edge": model_prob - price,  # recalc with WU-adjusted prob
            "prob_count": opp["prob_count"],
            "prob_normal": opp["prob_normal"],
            "num_tokens": num_tokens,
            "cost": cost,
            "kelly_f": kelly_f,
            "members_mean": opp["members_mean"],
            "members_std": opp["members_std"],
            "models_agreeing": opp.get("models_agreeing", 0),
            "per_model": opp.get("per_model", {}),
            "hrrr_boost": opp.get("hrrr_boost", False),
            "wu_adjustment": wu_adj,
            "wu_info": wu_info,
        }

    # ------------------------------------------------------------------
    # Trade execution (paper)
    # ------------------------------------------------------------------

    def execute_paper_trade(self, trade: Dict[str, Any]):
        """Execute a paper trade."""
        mkt: WeatherMarket = trade["market"]
        price = mkt.best_ask
        tokens = trade["num_tokens"]
        cost = trade["cost"]

        pos = PaperPosition(
            slug=mkt.slug,
            market_question=mkt.question,
            event_slug=mkt.event_slug,
            token_id=mkt.token_id,
            condition_id=mkt.condition_id,
            bucket_label=mkt.bucket_label,
            city=mkt.city,
            market_date=mkt.date,
            entry_price=price,
            model_prob=trade["model_prob"],
            edge=trade["edge"],
            num_tokens=tokens,
            cost=cost,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        self.positions[mkt.slug] = pos
        self.balance -= cost
        self.total_trades += 1
        self.total_wagered += cost
        self._save_pending()

        mode = "OBSERVE" if self.config.observe_only else "LIVE"
        models_info = trade.get("models_agreeing", "?")
        hrrr = " +HRRR" if trade.get("hrrr_boost") else ""
        wu = ""
        wu_adj = trade.get("wu_adjustment", 1.0)
        wu_info = trade.get("wu_info")
        if wu_info:
            wu = f" WU={wu_info['current_max']:.0f}({wu_adj:.1f}x)"
        per_model = trade.get("per_model", {})
        model_str = " ".join(f"{k}={v:.0%}" for k, v in per_model.items()) if per_model else ""
        print(
            f"[{mode}] WEATHER {mkt.city} {mkt.date} {mkt.bucket_label} "
            f"@ ${price:.3f} (consensus: {trade['model_prob']:.1%}, edge: {trade['edge']:.1%}) "
            f"| {models_info}/4 models agree{hrrr}{wu} "
            f"| {tokens:.0f} tokens, ${cost:.2f} "
            f"| [{model_str}] "
            f"| Bal: ${self.balance:.2f}"
        )

    async def execute_live_trade(self, trade: Dict[str, Any]) -> bool:
        """Execute a REAL trade on Polymarket via FOK order.

        Records position BEFORE sending order to prevent crash-induced tracking loss.
        If order fails, the pre-recorded position is removed.
        """
        if not self.trading_bot:
            logger.error("Live trade called but no trading bot!")
            return False

        mkt: WeatherMarket = trade["market"]
        price = mkt.best_ask
        tokens = trade["num_tokens"]
        cost = trade["cost"]

        # PRE-RECORD: Save position BEFORE sending order so crashes can't lose it
        pos = PaperPosition(
            slug=mkt.slug,
            market_question=mkt.question,
            event_slug=mkt.event_slug,
            token_id=mkt.token_id,
            condition_id=mkt.condition_id,
            bucket_label=mkt.bucket_label,
            city=mkt.city,
            market_date=mkt.date,
            entry_price=price,
            model_prob=trade["model_prob"],
            edge=trade["edge"],
            num_tokens=tokens,
            cost=cost,
            timestamp=datetime.now(timezone.utc).isoformat(),
        )
        self.positions[mkt.slug] = pos
        self.balance -= cost
        self._save_pending()
        logger.info(f"Pre-recorded position: {mkt.city} {mkt.bucket_label} ${cost:.2f}")

        try:
            result = await self.trading_bot.place_order(
                token_id=mkt.token_id,
                price=price,
                size=tokens,
                side="BUY",
                order_type="FOK",
            )
        except Exception as e:
            logger.error(f"Live order FAILED for {mkt.city} {mkt.bucket_label}: {e}")
            # ROLLBACK: remove pre-recorded position
            self.positions.pop(mkt.slug, None)
            self.balance += cost
            self._save_pending()
            logger.info(f"Rolled back pre-recorded position: {mkt.city} {mkt.bucket_label}")
            return False

        if not result.success:
            logger.warning(
                f"Live order REJECTED {mkt.city} {mkt.bucket_label}: {result.message}"
            )
            # ROLLBACK: remove pre-recorded position
            self.positions.pop(mkt.slug, None)
            self.balance += cost
            self._save_pending()
            logger.info(f"Rolled back pre-recorded position: {mkt.city} {mkt.bucket_label}")
            return False

        # FOK filled and verified — position was already recorded
        self.total_trades += 1
        self.total_wagered += cost

        mode = "LIVE"
        models_info = trade.get("models_agreeing", "?")
        hrrr = " +HRRR" if trade.get("hrrr_boost") else ""
        per_model = trade.get("per_model", {})
        model_str = " ".join(f"{k}={v:.0%}" for k, v in per_model.items()) if per_model else ""
        print(
            f"[{mode}] WEATHER {mkt.city} {mkt.date} {mkt.bucket_label} "
            f"@ ${price:.3f} (consensus: {trade['model_prob']:.1%}, edge: {trade['edge']:.1%}) "
            f"| {models_info}/4 models agree{hrrr} "
            f"| {tokens:.0f} tokens, ${cost:.2f} "
            f"| [{model_str}] "
            f"| Bal: ${self.balance:.2f}"
        )

        logger.info(
            f"LIVE FILLED {mkt.city} {mkt.date} {mkt.bucket_label} "
            f"@ ${price:.3f} | {tokens} tokens, ${cost:.2f} "
            f"| order_id={result.order_id}"
        )
        return True

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def _determine_winner(self, slug: str) -> Optional[str]:
        """Check if a weather market has resolved via Gamma API.
        ONLY settles when market is officially closed — never on price movement."""
        try:
            resp = requests.get(f"{GAMMA_API}/markets/slug/{slug}", timeout=10)
            if resp.status_code != 200:
                return None
            market = resp.json()
            if not market:
                return None

            # MUST be closed before we settle — price drops are NOT settlements
            closed = market.get("closed", False)
            if not closed:
                return None

            prices_raw = market.get("outcomePrices", "")
            if isinstance(prices_raw, str):
                try:
                    prices = json.loads(prices_raw)
                except (json.JSONDecodeError, TypeError):
                    return None
            else:
                prices = prices_raw

            if not prices or len(prices) < 2:
                return None

            yes_p = float(prices[0])
            return "Yes" if yes_p > 0.5 else "No"
        except Exception as e:
            logger.debug(f"Settle check error for {slug}: {e}")
            return None

    def settle_positions(self):
        """Check all pending positions for resolution."""
        if not self.positions:
            return

        pending = [pos for pos in self.positions.values() if not pos.resolved]
        if not pending:
            return

        logger.info(f"Settlement check: {len(pending)} pending positions")
        resolved_any = False
        closed_count = 0
        for pos in pending:
            winner = self._determine_winner(pos.slug)
            time.sleep(0.1)
            if winner is not None:
                closed_count += 1

            if winner is None:
                continue

            resolved_any = True
            # Weather markets: we always buy YES token
            won = (winner == "Yes")
            if won:
                payout = pos.num_tokens * 1.0
                pnl = payout - pos.cost
                pos.won = True
                pos.payout = payout
                self.balance += payout
                self.wins += 1
                self.total_payout += payout
                result = "won"
            else:
                pnl = -pos.cost
                pos.won = False
                pos.payout = 0
                self.losses += 1
                result = "lost"

            pos.resolved = True
            self._write_resolved(pos, result, pnl)

            wr = (self.wins / (self.wins + self.losses) * 100) if (self.wins + self.losses) > 0 else 0
            print(
                f"  [{result.upper()}] {pos.city} {pos.market_date} {pos.bucket_label} "
                f"@ ${pos.entry_price:.3f} (model: {pos.model_prob:.1%}) "
                f"→ PnL ${pnl:+.2f} | Bal: ${self.balance:.2f} "
                f"| WR: {wr:.1f}% ({self.wins}W/{self.losses}L)"
            )

        if not resolved_any:
            logger.info(f"Settlement: 0/{len(pending)} resolved (none closed yet)")
        else:
            remaining = sum(1 for v in self.positions.values() if not v.resolved)
            logger.info(f"Settlement: {closed_count} resolved, {remaining} still pending")
            self.positions = {k: v for k, v in self.positions.items() if not v.resolved}
            self._save_pending()

            # Live mode: redeem winnings and refresh balance from chain
            if self.trading_bot:
                try:
                    self.trading_bot.redeem_all()
                except Exception as e:
                    logger.debug(f"Redeem attempt: {e}")
                real_bal = self.trading_bot.get_usdc_balance()
                if real_bal is not None:
                    self.balance = real_bal
                    logger.info(f"Refreshed live USDC balance: ${real_bal:.2f}")

    # ------------------------------------------------------------------
    # Status display
    # ------------------------------------------------------------------

    def print_status(self):
        """Print current status."""
        decided = self.wins + self.losses
        wr = (self.wins / decided * 100) if decided > 0 else 0
        pnl = self.total_payout - self.total_wagered
        pending = sum(1 for p in self.positions.values() if not p.resolved)

        print(f"\n{'='*60}")
        print(f"  STORMCHASER v5 — Temperature Laddering — {'OBSERVE' if self.config.observe_only else 'LIVE'}")
        print(f"  Models: ECMWF + GFS + ICON + GEM (4-model consensus)")
        print(f"  Balance: ${self.balance:.2f} (started: ${self.starting_balance:.2f})")
        print(f"  Trades: {self.total_trades} | W:{self.wins} L:{self.losses} P:{pending}")
        print(f"  Win Rate: {wr:.1f}% | PnL: ${pnl:+.2f}")
        print(f"  Cities: {', '.join(self.config.cities)}")
        print(f"  Min edge: {self.config.min_edge:.0%} | Kelly: {self.config.kelly_fraction:.0%}")
        print(f"  Min models agree: {self.config.min_models_agree}/4 | LADDER {self.config.max_bets_per_city_date} buckets, ${self.config.max_cost_per_city_date:.2f}/city-date")
        print(f"  HRRR: {'ON' if self.config.use_hrrr else 'OFF'} (US cities: {', '.join(c for c in self.config.cities if c in US_CITIES)})")
        print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        """Main event loop."""
        print("\n" + "=" * 60)
        print("  STORMCHASER v5 — Temperature Laddering")
        print(f"  Models: ECMWF (51) + GFS (31) + ICON (40) + GEM (21) = 143 members")
        print(f"  Mode: {'OBSERVE (paper)' if self.config.observe_only else 'LIVE'}")
        print(f"  Bankroll: ${self.config.bankroll:.2f}")
        print(f"  Cities: {', '.join(self.config.cities)}")
        print(f"  Min edge: {self.config.min_edge:.0%} | Kelly: {self.config.kelly_fraction:.0%}")
        print(f"  Min models agree: {self.config.min_models_agree}/4 | LADDER {self.config.max_bets_per_city_date} buckets, ${self.config.max_cost_per_city_date:.2f}/city-date")
        print(f"  HRRR: {'ON' if self.config.use_hrrr else 'OFF'}")
        print(f"  Scan interval: {self.config.scan_interval}s")
        print("=" * 60 + "\n")

        last_settle = time.time()
        last_status = time.time()
        cycle = 0

        while True:
            try:
                cycle += 1
                now = time.time()

                # Live mode: refresh balance from exchange every cycle
                if self.trading_bot:
                    real_bal = self.trading_bot.get_usdc_balance()
                    if real_bal is not None:
                        old_bal = self.balance
                        self.balance = real_bal
                        if abs(old_bal - real_bal) > 0.10:
                            logger.info(f"Balance sync: ${old_bal:.2f} → ${real_bal:.2f}")
                    else:
                        logger.warning(f"Balance sync failed — keeping ${self.balance:.2f}")

                # Skip scanning if balance too low to place any trade
                if self.balance < 1.0:
                    print(f"[Cycle {cycle}] Balance too low (${self.balance:.2f}) — skipping scan, checking settlements only")
                    opps = []
                else:
                    print(f"[Cycle {cycle}] Scanning {len(self.config.cities)} cities...")
                    opps = self.scan_opportunities()

                if opps:
                    print(f"[Cycle {cycle}] Found {len(opps)} consensus opportunities (edge >= {self.config.min_edge:.0%}, >={self.config.min_models_agree} models)")
                    for opp in opps:
                        mkt = opp["market"]
                        pm = opp.get("per_model", {})
                        pm_str = " ".join(f"{k}={v:.0%}" for k, v in pm.items())
                        hrrr = " +HRRR" if opp.get("hrrr_boost") else ""
                        print(
                            f"  → {mkt.city} {mkt.date} {mkt.bucket_label}: "
                            f"ask=${mkt.best_ask:.3f} consensus={opp['model_prob']:.1%} "
                            f"edge={opp['edge']:+.1%} ({opp.get('models_agreeing', '?')}/4 agree{hrrr}) "
                            f"[{pm_str}]"
                        )

                # Evaluate and trade
                traded = 0
                skipped = 0
                failed = 0
                for opp in opps:
                    trade = self.evaluate_opportunity(opp)
                    if trade is not None:
                        if self.config.observe_only:
                            self.execute_paper_trade(trade)
                            traded += 1
                        else:
                            # LIVE: place real order on Polymarket
                            success = await self.execute_live_trade(trade)
                            if success:
                                traded += 1
                            else:
                                failed += 1
                    else:
                        skipped += 1

                mode_str = "paper" if self.config.observe_only else "LIVE"
                if traded:
                    fail_str = f" ({failed} failed)" if failed else ""
                    print(f"[Cycle {cycle}] Executed {traded} {mode_str} trades ({skipped} filtered{fail_str})")
                elif opps:
                    print(f"[Cycle {cycle}] All {skipped} opportunities filtered (price/model_prob/existing/size)")

                # Settlement check
                if now - last_settle >= self.config.settle_interval:
                    self.settle_positions()
                    last_settle = now

                # Status display every 10 minutes
                if now - last_status >= 600:
                    self.print_status()
                    last_status = now

                await asyncio.sleep(self.config.scan_interval)

            except KeyboardInterrupt:
                print("\n[WeatherEdge] Shutting down...")
                self._save_pending()
                self.print_status()
                break
            except Exception as e:
                logger.error(f"Loop error: {e}", exc_info=True)
                await asyncio.sleep(30)
