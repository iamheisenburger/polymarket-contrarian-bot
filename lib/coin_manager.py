"""
Coin Manager — Decides which coins trade live vs paper.

Philosophy: Strategy filters are FIXED and PROVEN. They never change.
The only thing that adapts is WHICH coins are traded live vs paper.

Promotion criteria (paper -> live):
    - 20+ paper trades at the fixed filters
    - Paper WR > 75%
    - Historical paper-to-live degradation < 5pp

Demotion criteria (live -> paper):
    - Paper WR drops below 65%

Usage:
    from lib.coin_manager import CoinManager

    cm = CoinManager()
    decisions = cm.evaluate_coins(
        paper_csv="data/paper_collector.csv",
        live_csv="data/live_trades.csv",
        degradation_model_path="data/degradation_model.json",
    )
    print(decisions)  # {'live': ['ETH', 'XRP'], 'paper': ['BTC', 'SOL', 'DOGE', 'BNB']}
"""

import csv
import json
import logging
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class CoinManager:
    """Decides which coins are live vs paper. Does NOT change strategy filters."""

    # Fixed filters — these are the proven, hardcoded strategy parameters.
    # Paper trades are evaluated AT these filters to decide promotion.
    FIXED_FILTERS = {
        'min_edge': 0.20,
        'min_momentum': 0.0005,
        'min_entry_price': 0.60,
        'max_entry_price': 0.70,
        'min_fair_value': 0.75,
        'min_window_elapsed': 120,
        'max_window_elapsed': 180,
    }

    PROMOTE_THRESHOLD = 0.75    # Paper WR must be >75% to go live
    DEMOTE_THRESHOLD = 0.65     # Paper WR drops below 65% -> back to paper
    MIN_TRADES_TO_EVALUATE = 20 # Need 20+ paper trades before considering promotion
    MAX_DEGRADATION = 0.05      # If paper-to-live gap >5pp historically, don't promote

    ALL_COINS = ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB']

    def __init__(self):
        self._coin_stats: Dict[str, dict] = {}
        self._decisions: Dict[str, str] = {}  # coin -> 'live' or 'paper'
        self._reasons: Dict[str, str] = {}    # coin -> human-readable reason

    def evaluate_coins(
        self,
        paper_csv: str,
        live_csv: str = "",
        degradation_model_path: str = "",
    ) -> Dict[str, List[str]]:
        """Evaluate all coins and decide live vs paper.

        Args:
            paper_csv: Path to paper collector CSV (wide filters, all coins).
            live_csv: Path to live trade CSV (for checking current live coin performance).
            degradation_model_path: Path to degradation model JSON.

        Returns:
            {'live': ['ETH', 'XRP'], 'paper': ['BTC', 'SOL', 'DOGE', 'BNB']}
        """
        # Load degradation model if available
        degradation = None
        if degradation_model_path:
            degradation = self._load_degradation(degradation_model_path)

        # Parse paper trades and filter to those matching FIXED_FILTERS
        paper_stats = self._compute_stats_at_fixed_filters(paper_csv)

        # Parse live trades for currently-live coins (to detect demotion)
        live_stats = {}
        if live_csv:
            live_stats = self._compute_stats_at_fixed_filters(live_csv)

        # Evaluate each coin
        self._coin_stats = {}
        self._decisions = {}
        self._reasons = {}

        for coin in self.ALL_COINS:
            stats = paper_stats.get(coin, {'trades': 0, 'wins': 0, 'wr': 0.0})
            self._coin_stats[coin] = stats

            n_trades = stats['trades']
            wr = stats['wr']

            # Check degradation
            coin_degradation = self.MAX_DEGRADATION  # conservative default
            if degradation:
                coin_degradation = degradation.get(coin, self.MAX_DEGRADATION)

            # Decision logic
            if n_trades < self.MIN_TRADES_TO_EVALUATE:
                self._decisions[coin] = 'paper'
                self._reasons[coin] = (
                    f"Insufficient data: {n_trades}/{self.MIN_TRADES_TO_EVALUATE} "
                    f"trades at fixed filters"
                )
            elif wr >= self.PROMOTE_THRESHOLD and coin_degradation <= self.MAX_DEGRADATION:
                self._decisions[coin] = 'live'
                self._reasons[coin] = (
                    f"Promoted: paper WR={wr:.1%} (n={n_trades}), "
                    f"degradation={coin_degradation:.1%}"
                )
            elif wr < self.DEMOTE_THRESHOLD:
                self._decisions[coin] = 'paper'
                self._reasons[coin] = (
                    f"Demoted: paper WR={wr:.1%} < {self.DEMOTE_THRESHOLD:.0%} "
                    f"threshold (n={n_trades})"
                )
            elif coin_degradation > self.MAX_DEGRADATION:
                self._decisions[coin] = 'paper'
                self._reasons[coin] = (
                    f"High degradation: {coin_degradation:.1%} > "
                    f"{self.MAX_DEGRADATION:.0%} max (paper WR={wr:.1%}, n={n_trades})"
                )
            elif wr >= self.DEMOTE_THRESHOLD:
                # Between demote and promote thresholds: keep current status
                # If we have live data, the coin was live — keep it live
                # Otherwise, keep it on paper (not yet proven enough)
                if coin in live_stats and live_stats[coin]['trades'] > 0:
                    self._decisions[coin] = 'live'
                    self._reasons[coin] = (
                        f"Maintaining live: paper WR={wr:.1%} in buffer zone "
                        f"[{self.DEMOTE_THRESHOLD:.0%}-{self.PROMOTE_THRESHOLD:.0%}] (n={n_trades})"
                    )
                else:
                    self._decisions[coin] = 'paper'
                    self._reasons[coin] = (
                        f"Not yet proven: paper WR={wr:.1%} < "
                        f"{self.PROMOTE_THRESHOLD:.0%} promote threshold (n={n_trades})"
                    )
            else:
                self._decisions[coin] = 'paper'
                self._reasons[coin] = f"Default: paper (WR={wr:.1%}, n={n_trades})"

        live_coins = sorted([c for c, d in self._decisions.items() if d == 'live'])
        paper_coins = sorted([c for c, d in self._decisions.items() if d == 'paper'])

        return {'live': live_coins, 'paper': paper_coins}

    def get_bankroll_coin_limit(self, balance: float) -> int:
        """Returns how many coins to trade live based on current balance.

        Args:
            balance: Current USDC balance.

        Returns:
            Maximum number of coins to run live.
        """
        if balance < 5.0:
            return 0   # Stop trading entirely
        elif balance < 15.0:
            return 1   # Only the highest WR coin
        elif balance < 30.0:
            return 2   # Top 2 coins
        else:
            return len(self.ALL_COINS)  # All promoted coins

    def apply_bankroll_limit(self, decisions: Dict[str, List[str]], balance: float) -> Dict[str, List[str]]:
        """Apply bankroll-based coin limit to decisions.

        Keeps only the top N coins (by paper WR) as live, demotes the rest.

        Args:
            decisions: Output from evaluate_coins().
            balance: Current USDC balance.

        Returns:
            Updated decisions dict with bankroll limit applied.
        """
        max_coins = self.get_bankroll_coin_limit(balance)
        live_coins = decisions['live']

        if max_coins == 0:
            # Stop all live trading
            return {
                'live': [],
                'paper': sorted(self.ALL_COINS),
            }

        if len(live_coins) <= max_coins:
            return decisions

        # Rank live coins by paper WR, keep only top N
        ranked = sorted(
            live_coins,
            key=lambda c: self._coin_stats.get(c, {}).get('wr', 0.0),
            reverse=True,
        )
        keep_live = ranked[:max_coins]
        demote = ranked[max_coins:]

        for coin in demote:
            self._reasons[coin] = (
                f"Bankroll limit: ${balance:.2f} allows {max_coins} coins, "
                f"demoted (WR rank too low)"
            )

        paper_coins = sorted(set(decisions['paper']) | set(demote))

        return {'live': sorted(keep_live), 'paper': paper_coins}

    def summary(self) -> str:
        """Human-readable status summary."""
        lines = [
            "=" * 60,
            "  COIN MANAGER — STATUS",
            "=" * 60,
            "",
            "  Fixed Filters (never change):",
        ]
        for k, v in self.FIXED_FILTERS.items():
            lines.append(f"    {k}: {v}")
        lines.append("")

        live_coins = [c for c, d in self._decisions.items() if d == 'live']
        paper_coins = [c for c, d in self._decisions.items() if d == 'paper']

        lines.append(f"  LIVE coins:  {', '.join(sorted(live_coins)) if live_coins else 'NONE'}")
        lines.append(f"  PAPER coins: {', '.join(sorted(paper_coins)) if paper_coins else 'NONE'}")
        lines.append("")

        for coin in self.ALL_COINS:
            stats = self._coin_stats.get(coin, {'trades': 0, 'wins': 0, 'wr': 0.0})
            decision = self._decisions.get(coin, 'paper')
            reason = self._reasons.get(coin, 'no evaluation')
            status_tag = "LIVE" if decision == 'live' else "PAPER"

            lines.append(f"  --- {coin} [{status_tag}] ---")
            lines.append(
                f"    Paper trades at fixed filters: {stats['trades']} "
                f"({stats['wins']}W / {stats['trades'] - stats['wins']}L)"
            )
            if stats['trades'] > 0:
                lines.append(f"    Paper WR: {stats['wr']:.1%}")
                wilson_lb = self._wilson_lower(stats['wins'], stats['trades'])
                lines.append(f"    Wilson lower 95%: {wilson_lb:.1%}")
            lines.append(f"    Reason: {reason}")
            lines.append("")

        lines.append("=" * 60)
        return "\n".join(lines)

    def save_decisions(
        self,
        decisions: Dict[str, List[str]],
        balance: float,
        output_path: str = "data/coin_decisions.json",
    ) -> None:
        """Save decisions to JSON file.

        Args:
            decisions: Output from evaluate_coins() or apply_bankroll_limit().
            balance: Current USDC balance.
            output_path: Path to save JSON.
        """
        filepath = Path(output_path)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "live_coins": decisions['live'],
            "paper_coins": decisions['paper'],
            "balance": round(balance, 2),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin_stats": {},
        }

        for coin in self.ALL_COINS:
            stats = self._coin_stats.get(coin, {'trades': 0, 'wins': 0, 'wr': 0.0})
            data["coin_stats"][coin] = {
                "trades_at_fixed_filters": stats['trades'],
                "wins": stats['wins'],
                "paper_wr": round(stats['wr'], 4),
                "decision": self._decisions.get(coin, 'paper'),
                "reason": self._reasons.get(coin, ''),
            }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Coin decisions saved to {filepath}")

    def _compute_stats_at_fixed_filters(self, csv_path: str) -> Dict[str, dict]:
        """Parse a trade CSV and compute per-coin WR at the fixed filters.

        Only counts trades that match ALL of:
            - edge >= 0.20
            - entry_price in [0.60, 0.70]
            - momentum >= 0.0005
            - fair_value >= 0.75
            - window_elapsed in [120, 180] (i.e. TTE in [120, 180] for 5m)

        Returns:
            {coin: {'trades': N, 'wins': W, 'wr': W/N}}
        """
        path = Path(csv_path)
        if not path.exists():
            logger.warning(f"CSV not found: {csv_path}")
            return {}

        stats: Dict[str, dict] = {}
        try:
            with open(path, "r") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    # Only count resolved trades
                    outcome = row.get("outcome", "").strip()
                    if outcome not in ("won", "lost"):
                        continue

                    coin = row.get("coin", "").upper()
                    if not coin:
                        continue

                    # Parse fields
                    try:
                        entry_price = float(row.get("entry_price", 0))
                        fair_value = float(row.get("fair_value_at_entry", 0))
                        edge = fair_value - entry_price
                        momentum = abs(float(row.get("momentum_at_entry", 0)))
                        tte = float(row.get("time_to_expiry_at_entry", 0))
                    except (ValueError, TypeError):
                        continue

                    # Apply fixed filters
                    if edge < self.FIXED_FILTERS['min_edge']:
                        continue
                    if entry_price < self.FIXED_FILTERS['min_entry_price']:
                        continue
                    if entry_price > self.FIXED_FILTERS['max_entry_price']:
                        continue
                    if momentum < self.FIXED_FILTERS['min_momentum']:
                        continue
                    if fair_value < self.FIXED_FILTERS['min_fair_value']:
                        continue

                    # Window elapsed filter: for 5m (300s) markets
                    # elapsed = 300 - TTE
                    # We want elapsed in [min_window_elapsed, max_window_elapsed]
                    # i.e. TTE in [300 - max_window_elapsed, 300 - min_window_elapsed]
                    # TTE in [120, 180]
                    min_tte = 300 - self.FIXED_FILTERS['max_window_elapsed']  # 300-180=120
                    max_tte = 300 - self.FIXED_FILTERS['min_window_elapsed']  # 300-120=180
                    if tte < min_tte or tte > max_tte:
                        continue

                    # Trade passes all fixed filters
                    if coin not in stats:
                        stats[coin] = {'trades': 0, 'wins': 0, 'wr': 0.0}

                    stats[coin]['trades'] += 1
                    if outcome == "won":
                        stats[coin]['wins'] += 1

        except Exception as e:
            logger.error(f"Failed to parse CSV {csv_path}: {e}")
            return {}

        # Compute WR
        for coin, s in stats.items():
            s['wr'] = s['wins'] / s['trades'] if s['trades'] > 0 else 0.0

        return stats

    def _load_degradation(self, model_path: str) -> Optional[Dict[str, float]]:
        """Load per-coin degradation from degradation model JSON.

        Returns:
            {coin: degradation_pp} or None if not available.
        """
        path = Path(model_path)
        if not path.exists():
            logger.warning(f"Degradation model not found: {model_path}")
            return None

        try:
            # Use lib.degradation if available
            from lib.degradation import DegradationEstimator
            est = DegradationEstimator()
            est.load(str(path))

            result = {}
            for coin in self.ALL_COINS:
                result[coin] = est.get_degradation(coin)

            return result
        except Exception as e:
            logger.error(f"Failed to load degradation model: {e}")
            return None

    @staticmethod
    def _wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
        """Wilson score lower bound for win rate (95% CI)."""
        if total == 0:
            return 0.0
        p = wins / total
        denominator = 1 + z ** 2 / total
        centre = (p + z ** 2 / (2 * total)) / denominator
        spread = z * math.sqrt((p * (1 - p) + z ** 2 / (4 * total)) / total) / denominator
        return centre - spread
