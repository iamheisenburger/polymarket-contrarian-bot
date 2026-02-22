"""
Weather Edge Strategy — exploit ensemble forecast edge on Polymarket weather markets.

Pulls 51 ECMWF ensemble members from Open-Meteo, computes bucket probabilities
via fitted normal distribution, compares to Polymarket prices, and trades
where model_prob > market_price + min_edge. Half-Kelly sizing.

Settlement: daily via Gamma API (markets resolve same day).
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

from lib.weather_api import CITIES, WeatherForecaster
from lib.weather_markets import WeatherMarket, WeatherMarketScanner

logger = logging.getLogger(__name__)

GAMMA_API = "https://gamma-api.polymarket.com"


@dataclass
class WeatherConfig:
    """Configuration for the weather edge bot."""
    bankroll: float = 12.95
    observe_only: bool = True
    min_edge: float = 0.05          # only trade when model_prob - price > 5c
    kelly_fraction: float = 0.5     # half-Kelly
    max_position_pct: float = 0.20  # max 20% bankroll per trade
    min_entry_price: float = 0.01   # floor (weather buckets can be 1-2c)
    max_entry_price: float = 0.50   # cap — don't buy expensive buckets
    scan_interval: int = 300        # rescan every 5 min
    settle_interval: int = 300      # check settlements every 5 min
    cities: list = field(default_factory=lambda: list(CITIES.keys()))  # Seoul/Seattle excluded from CITIES
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

        # Paper portfolio
        self.balance = config.bankroll
        self.starting_balance = config.bankroll
        self.positions: Dict[str, PaperPosition] = {}  # slug -> position

        # Stats
        self.total_trades = 0
        self.wins = 0
        self.losses = 0
        self.total_wagered = 0.0
        self.total_payout = 0.0

        # CSV logger
        self.csv_path = Path("data/weather_trades.csv")
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_csv()

        # Pending JSON sidecar (survives restarts)
        self.pending_path = self.csv_path.with_suffix(".pending.json")
        self._load_pending()

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
            logger.info(f"Loaded {len(self.positions)} pending weather positions")
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
    # Opportunity scanning
    # ------------------------------------------------------------------

    def scan_opportunities(self) -> List[Dict[str, Any]]:
        """
        Scan all cities: pull ensemble forecasts + market prices, compute edge.
        Returns list of opportunities with positive edge.
        """
        opportunities = []

        # 1. Get all priced markets from Polymarket (one API call for discovery)
        markets = self.scanner.scan_all(city_filter=self.config.cities)
        if not markets:
            logger.info("No priced weather markets found")
            return []

        # 2. Get ensemble forecasts for each unique city in parallel
        cities_needed = set()
        for mkt in markets:
            if CITIES.get(mkt.city):
                cities_needed.add(mkt.city)

        city_forecasts: Dict[str, Dict] = {}
        def _fetch_forecast(city_key):
            return city_key, self.forecaster.get_ensemble_forecast(
                city_key, days=self.config.forecast_days
            )

        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = [pool.submit(_fetch_forecast, c) for c in cities_needed]
            for fut in as_completed(futs):
                city_key, data = fut.result()
                city_forecasts[city_key] = data

        # 3. Match markets to forecasts and compute edge
        for mkt in markets:
            forecasts = city_forecasts.get(mkt.city, {})
            if mkt.date not in forecasts:
                continue
            members = forecasts[mkt.date]

            # Get city-specific bias correction
            city_cfg = CITIES.get(mkt.city)
            bias = city_cfg.bias_correction if city_cfg else 0.0
            extra = city_cfg.extra_std if city_cfg else 0.0

            # Compute model probability for this bucket (with bias correction)
            bucket_probs = self.forecaster.compute_bucket_probabilities(
                members, [(mkt.bucket_low, mkt.bucket_high)],
                bias_correction=bias,
                extra_std=extra,
            )
            if not bucket_probs:
                continue

            model_prob = bucket_probs[0]["prob"]
            edge = model_prob - mkt.best_ask

            if edge >= self.config.min_edge:
                # Report corrected mean/std for diagnostics
                corrected = [m + bias for m in members]
                mean = sum(corrected) / len(corrected)
                variance = sum((m - mean)**2 for m in corrected) / len(corrected)
                std = math.sqrt(variance + extra**2)
                opportunities.append({
                    "market": mkt,
                    "model_prob": model_prob,
                    "prob_count": bucket_probs[0]["prob_count"],
                    "prob_normal": bucket_probs[0]["prob_normal"],
                    "edge": edge,
                    "members_mean": mean,
                    "members_std": std,
                })

        # Sort by edge descending
        opportunities.sort(key=lambda x: x["edge"], reverse=True)
        return opportunities

    def evaluate_opportunity(self, opp: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        Apply filters and Kelly sizing to an opportunity.
        Returns trade dict or None.
        """
        mkt: WeatherMarket = opp["market"]
        model_prob = opp["model_prob"]
        price = mkt.best_ask

        # Price range filter
        if price < self.config.min_entry_price or price > self.config.max_entry_price:
            return None

        # Already have position
        if mkt.slug in self.positions:
            return None

        # Kelly sizing
        if price <= 0 or price >= 1:
            return None
        b = (1.0 - price) / price  # net odds (buy at price, win 1.0)
        kelly_f = (model_prob * b - (1 - model_prob)) / b if b > 0 else 0
        kelly_f = max(0, min(kelly_f, self.config.max_position_pct))
        kelly_f *= self.config.kelly_fraction  # half-Kelly

        bet_amount = self.balance * kelly_f
        if bet_amount < 1.0:
            # Try minimum viable bet
            bet_amount = min(1.0, self.balance * 0.15)
        if bet_amount > self.balance:
            return None

        num_tokens = bet_amount / price
        num_tokens = max(5.0, round(num_tokens, 2))
        cost = num_tokens * price

        if cost > self.balance:
            num_tokens = int(self.balance / price)
            cost = num_tokens * price
        if cost < 1.0 or num_tokens < 5:
            return None

        return {
            "market": mkt,
            "model_prob": model_prob,
            "edge": opp["edge"],
            "prob_count": opp["prob_count"],
            "prob_normal": opp["prob_normal"],
            "num_tokens": num_tokens,
            "cost": cost,
            "kelly_f": kelly_f,
            "members_mean": opp["members_mean"],
            "members_std": opp["members_std"],
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
        print(
            f"[{mode}] WEATHER {mkt.city} {mkt.date} {mkt.bucket_label} "
            f"@ ${price:.3f} (model: {trade['model_prob']:.1%}, edge: {trade['edge']:.1%}) "
            f"| {tokens:.0f} tokens, ${cost:.2f} "
            f"| mean={trade['members_mean']:.1f}±{trade['members_std']:.1f} "
            f"| Bal: ${self.balance:.2f}"
        )

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

        resolved_any = False
        for pos in pending:
            winner = self._determine_winner(pos.slug)
            time.sleep(0.1)

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

        if resolved_any:
            self.positions = {k: v for k, v in self.positions.items() if not v.resolved}
            self._save_pending()

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
        print(f"  WEATHER EDGE — {'OBSERVE' if self.config.observe_only else 'LIVE'}")
        print(f"  Balance: ${self.balance:.2f} (started: ${self.starting_balance:.2f})")
        print(f"  Trades: {self.total_trades} | W:{self.wins} L:{self.losses} P:{pending}")
        print(f"  Win Rate: {wr:.1f}% | PnL: ${pnl:+.2f}")
        print(f"  Cities: {', '.join(self.config.cities)}")
        print(f"  Min edge: {self.config.min_edge:.0%} | Kelly: {self.config.kelly_fraction:.0%}")
        print(f"{'='*60}\n")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self):
        """Main event loop."""
        print("\n" + "=" * 60)
        print("  WEATHER EDGE BOT — Ensemble Forecast vs. Polymarket")
        print(f"  Mode: {'OBSERVE (paper)' if self.config.observe_only else 'LIVE'}")
        print(f"  Bankroll: ${self.config.bankroll:.2f}")
        print(f"  Cities: {', '.join(self.config.cities)}")
        print(f"  Min edge: {self.config.min_edge:.0%} | Kelly: {self.config.kelly_fraction:.0%}")
        print(f"  Scan interval: {self.config.scan_interval}s")
        print("=" * 60 + "\n")

        last_settle = time.time()
        last_status = time.time()
        cycle = 0

        while True:
            try:
                cycle += 1
                now = time.time()

                # Scan for opportunities
                print(f"[Cycle {cycle}] Scanning {len(self.config.cities)} cities...")
                opps = self.scan_opportunities()

                if opps:
                    print(f"[Cycle {cycle}] Found {len(opps)} opportunities with edge >= {self.config.min_edge:.0%}")
                    for opp in opps:
                        mkt = opp["market"]
                        print(
                            f"  → {mkt.city} {mkt.date} {mkt.bucket_label}: "
                            f"ask=${mkt.best_ask:.3f} model={opp['model_prob']:.1%} "
                            f"edge={opp['edge']:+.1%}"
                        )

                # Evaluate and trade
                traded = 0
                for opp in opps:
                    trade = self.evaluate_opportunity(opp)
                    if trade is not None:
                        self.execute_paper_trade(trade)
                        traded += 1

                if traded:
                    print(f"[Cycle {cycle}] Executed {traded} paper trades")

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
