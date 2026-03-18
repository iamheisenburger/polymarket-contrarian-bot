"""
Strategy Optimizer — Layer 3 of the Adaptive Trading System

Sweeps filter configurations per-coin using paper/signal trade data,
applies degradation adjustments, and outputs the EV-maximizing config.

Key design:
    - Grid sweep over min_edge, entry price range, momentum, fair value, TTE, vatic
    - Wilson lower bound filters out configs that might just be lucky
    - Degradation model adjusts paper WR to estimated live WR
    - Ranks by EV/day (not just WR) — a config that trades more at slightly lower WR can win
    - Standalone: only needs pandas, numpy, and optionally lib.degradation

Usage:
    from lib.optimizer import StrategyOptimizer

    opt = StrategyOptimizer()
    result = opt.optimize_from_csvs("data/paper_fat_edge.csv", "data/btc_live_tmp.csv")
    print(result.summary())
    result.to_json("data/optimal_config.json")
"""

import csv
import json
import logging
import math
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone, timedelta
from itertools import product
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# Conservative default degradation when no degradation model is available
DEFAULT_DEGRADATION_PP = 0.05

# Minimum trades to consider a config viable
MIN_TRADES_PER_CONFIG = 10

# Bankroll risk tiers — determines optimization objective
# Below CRITICAL: maximize WR (survival mode, avoid ruin)
# Between CRITICAL and HEALTHY: balanced (WR-weighted EV/day)
# Above HEALTHY: maximize EV/day (growth mode)
BANKROLL_CRITICAL = 10.0   # Below this: survival mode
BANKROLL_HEALTHY = 30.0    # Above this: growth mode
AVG_TRADE_COST = 3.25      # ~5 tokens at ~$0.65 avg entry

# Grid search parameter space
GRID = {
    "min_edge": [0.10, 0.15, 0.20, 0.25, 0.30],
    "entry_price": [
        (0.45, 0.70),
        (0.50, 0.70),
        (0.55, 0.70),
        (0.55, 0.65),
        (0.60, 0.70),
    ],
    "min_momentum": [0.0003, 0.0005, 0.0008, 0.001],
    "min_fv": [0.65, 0.70, 0.75, 0.80, 0.85],
    "min_tte": [100, 120, 140, 150],
    "max_tte": [180],
    "require_vatic": [True, False],
}


@dataclass
class CoinConfig:
    """Optimized config for a single coin."""

    coin: str
    min_edge: float
    min_momentum: float
    min_fair_value: float
    min_entry_price: float
    max_entry_price: float
    min_window_elapsed: float
    max_window_elapsed: float
    require_vatic: bool
    # Expected performance
    expected_wr: float  # after degradation adjustment
    expected_ev_per_trade: float
    expected_trades_per_day: float
    expected_ev_per_day: float
    paper_sample_size: int
    wilson_lower_bound: float  # 95% CI lower bound on WR

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "CoinConfig":
        return cls(**d)


@dataclass
class OptimizationResult:
    """Full optimization output."""

    timestamp: str
    window_hours: float  # how much data was used
    coin_configs: Dict[str, CoinConfig] = field(default_factory=dict)
    recommended_coins: List[str] = field(default_factory=list)
    dropped_coins: List[str] = field(default_factory=list)
    total_expected_ev_per_day: float = 0.0
    risk_mode: str = "growth"
    bankroll: float = 0.0

    def to_json(self, path: str) -> None:
        """Save result to JSON file."""
        filepath = Path(path)
        filepath.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "timestamp": self.timestamp,
            "window_hours": self.window_hours,
            "recommended_coins": self.recommended_coins,
            "dropped_coins": self.dropped_coins,
            "total_expected_ev_per_day": round(self.total_expected_ev_per_day, 4),
            "coin_configs": {
                coin: cfg.to_dict() for coin, cfg in self.coin_configs.items()
            },
        }

        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)

        logger.info(f"Optimization result saved to {filepath}")

    @classmethod
    def from_json(cls, path: str) -> "OptimizationResult":
        """Load result from JSON."""
        with open(path, "r") as f:
            data = json.load(f)

        result = cls(
            timestamp=data["timestamp"],
            window_hours=data["window_hours"],
            recommended_coins=data.get("recommended_coins", []),
            dropped_coins=data.get("dropped_coins", []),
            total_expected_ev_per_day=data.get("total_expected_ev_per_day", 0.0),
        )

        for coin, cfg_dict in data.get("coin_configs", {}).items():
            result.coin_configs[coin] = CoinConfig.from_dict(cfg_dict)

        return result

    def summary(self) -> str:
        """Human-readable summary of optimization results."""
        lines = [
            "=" * 65,
            "  STRATEGY OPTIMIZER — RESULTS",
            "=" * 65,
            f"  Timestamp:   {self.timestamp}",
            f"  Bankroll:    ${self.bankroll:.2f}" if self.bankroll > 0 else "",
            f"  Risk mode:   {self.risk_mode.upper()}" + (
                " (maximize WR, avoid ruin)" if self.risk_mode == "survival"
                else " (WR-weighted EV/day)" if self.risk_mode == "balanced"
                else " (maximize EV/day)"
            ),
            f"  Data window: {self.window_hours:.0f} hours ({self.window_hours / 24:.1f} days)",
            "",
        ]

        if self.recommended_coins:
            lines.append(f"  RECOMMENDED COINS: {', '.join(self.recommended_coins)}")
        if self.dropped_coins:
            lines.append(f"  DROPPED COINS:     {', '.join(self.dropped_coins)}")
        lines.append(f"  Total EV/day:      ${self.total_expected_ev_per_day:.2f}")
        lines.append("")

        for coin in sorted(self.coin_configs.keys()):
            cfg = self.coin_configs[coin]
            status = "RECOMMENDED" if coin in self.recommended_coins else "DROPPED"
            lines.append(f"  --- {coin} [{status}] ---")
            lines.append(f"    min_edge:          {cfg.min_edge:.2f}")
            lines.append(f"    min_momentum:      {cfg.min_momentum:.4f}")
            lines.append(f"    min_fair_value:    {cfg.min_fair_value:.2f}")
            lines.append(f"    entry_price:       [{cfg.min_entry_price:.2f}, {cfg.max_entry_price:.2f}]")
            lines.append(f"    window_elapsed:    [{cfg.min_window_elapsed:.0f}s, {cfg.max_window_elapsed:.0f}s]")
            lines.append(f"    require_vatic:     {cfg.require_vatic}")
            lines.append(f"    ---")
            lines.append(f"    paper WR:          {cfg.expected_wr + DEFAULT_DEGRADATION_PP:.1%} (n={cfg.paper_sample_size})")
            lines.append(f"    adjusted WR:       {cfg.expected_wr:.1%}")
            lines.append(f"    Wilson lower 95%:  {cfg.wilson_lower_bound:.1%}")
            lines.append(f"    EV/trade:          ${cfg.expected_ev_per_trade:.3f}")
            lines.append(f"    trades/day:        {cfg.expected_trades_per_day:.1f}")
            lines.append(f"    EV/day:            ${cfg.expected_ev_per_day:.2f}")
            lines.append("")

        # Generate CLI command for recommended coins
        if self.recommended_coins:
            lines.append("  SUGGESTED CLI COMMAND:")
            lines.append("  " + self._build_cli_command())
            lines.append("")

        lines.append("=" * 65)
        return "\n".join(lines)

    def _build_cli_command(self) -> str:
        """Build a CLI command from the recommended configs.

        Uses --per-coin-config to pass per-coin optimal values directly.
        """
        if not self.recommended_coins:
            return "# No recommended coins"

        coins_str = " ".join(self.recommended_coins)
        parts = [
            f"python apps/run_sniper.py",
            f"--coins {coins_str}",
            f"--timeframe 5m",
            f"--per-coin-config data/optimal_config.json",
            f"--side trend --ema-fast 4 --ema-slow 16",
            f"--block-weekends",
            f"--fixed-vol 0.15",
            f"--min-size",
        ]

        return " \\\n    ".join(parts)


def _load_trade_csv(filepath: str, window_hours: float = 0) -> pd.DataFrame:
    """Load a trade CSV into a DataFrame, filtering to resolved trades.

    Works with both trade CSVs and signal CSVs (same column names).
    """
    path = Path(filepath)
    if not path.exists():
        logger.warning(f"CSV not found: {filepath}")
        return pd.DataFrame()

    try:
        df = pd.read_csv(filepath)
    except Exception as e:
        logger.error(f"Failed to read CSV {filepath}: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    # Filter to resolved trades only
    if "outcome" in df.columns:
        df = df[df["outcome"].isin(["won", "lost"])].copy()

    if df.empty:
        return df

    # Parse timestamps
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")
        df = df.dropna(subset=["timestamp"])

        # Apply rolling window
        if window_hours > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=window_hours)
            # Handle timezone-naive timestamps
            if df["timestamp"].dt.tz is None:
                df["timestamp"] = df["timestamp"].dt.tz_localize("UTC")
            df = df[df["timestamp"] >= cutoff]

    # Normalize coin names
    if "coin" in df.columns:
        df["coin"] = df["coin"].str.upper()

    # Compute derived columns if not present
    if "edge" not in df.columns and "fair_value_at_entry" in df.columns and "entry_price" in df.columns:
        df["edge"] = df["fair_value_at_entry"] - df["entry_price"]

    if "won" not in df.columns and "outcome" in df.columns:
        df["won"] = (df["outcome"] == "won").astype(int)

    return df


class StrategyOptimizer:
    """Sweeps filter configs per-coin and finds EV-maximizing parameters."""

    def __init__(self, degradation_model=None, bankroll: float = 0.0):
        """
        Args:
            degradation_model: DegradationEstimator instance from lib/degradation.py.
                             If None, uses conservative 5pp default degradation.
            bankroll: Current USDC balance. Determines risk preference:
                     <$10: survival mode (maximize WR, minimize risk of ruin)
                     $10-30: balanced (WR-weighted EV/day)
                     >$30: growth mode (maximize EV/day)
        """
        self.degradation = degradation_model
        self.bankroll = bankroll
        self.risk_mode = self._determine_risk_mode(bankroll)

    @staticmethod
    def _determine_risk_mode(bankroll: float) -> str:
        """Determine risk mode based on bankroll.

        Returns:
            'survival': bankroll < CRITICAL — maximize WR, only highest-confidence trades
            'balanced': CRITICAL <= bankroll < HEALTHY — WR-weighted EV/day
            'growth': bankroll >= HEALTHY — maximize EV/day
        """
        if bankroll <= 0:
            return "growth"  # No bankroll info, default to EV maximization
        if bankroll < BANKROLL_CRITICAL:
            return "survival"
        if bankroll < BANKROLL_HEALTHY:
            return "balanced"
        return "growth"

    def _score_config(self, wr: float, ev_per_day: float, trades_per_day: float) -> float:
        """Score a config based on current risk mode.

        survival: heavily penalizes low WR, prefers fewer high-quality trades
        balanced: weights WR and EV/day equally
        growth:   pure EV/day maximization
        """
        if self.risk_mode == "survival":
            # Survival: score = WR^3 * EV/day
            # WR^3 makes 85% WR score 2.5x higher than 75% WR
            # Also penalize high trade frequency (more trades = more chances to lose)
            max_trades = self.bankroll / AVG_TRADE_COST  # max losses before ruin
            survival_prob = wr ** max_trades  # prob of surviving max_trades trades
            return (wr ** 3) * ev_per_day * survival_prob
        elif self.risk_mode == "balanced":
            # Balanced: score = WR * EV/day
            return wr * ev_per_day
        else:
            # Growth: pure EV/day
            return ev_per_day

    def optimize_from_csvs(
        self,
        paper_csv: str,
        live_csv: str = None,
        window_hours: float = 336,
    ) -> OptimizationResult:
        """Run optimization using CSV trade logs.

        Args:
            paper_csv: Path to paper trade CSV (or signal CSV).
            live_csv: Path to live trade CSV (optional, for coins with no paper data).
            window_hours: Rolling window in hours (default 336 = 14 days).

        Returns:
            OptimizationResult with per-coin optimal configs.
        """
        # Load paper data
        paper_df = _load_trade_csv(paper_csv, window_hours)
        logger.info(f"Paper CSV: {len(paper_df)} resolved trades")

        # Load live data if provided
        live_df = pd.DataFrame()
        if live_csv:
            live_df = _load_trade_csv(live_csv, window_hours)
            logger.info(f"Live CSV: {len(live_df)} resolved trades")

        # Combine all data, tagging source
        dfs = []
        if not paper_df.empty:
            paper_df = paper_df.copy()
            paper_df["_source"] = "paper"
            dfs.append(paper_df)
        if not live_df.empty:
            live_df = live_df.copy()
            live_df["_source"] = "live"
            dfs.append(live_df)

        if not dfs:
            logger.warning("No trade data found in any CSV")
            return OptimizationResult(
                timestamp=datetime.now(timezone.utc).isoformat(),
                window_hours=window_hours,
            )

        combined = pd.concat(dfs, ignore_index=True)

        # Determine time span for trades/day calculation
        if "timestamp" in combined.columns and len(combined) > 1:
            ts_sorted = combined["timestamp"].sort_values()
            total_hours = (ts_sorted.iloc[-1] - ts_sorted.iloc[0]).total_seconds() / 3600
            total_hours = max(total_hours, 24)  # Floor at 1 day
        else:
            total_hours = window_hours

        # Get unique coins
        coins = sorted(combined["coin"].unique()) if "coin" in combined.columns else []

        result = OptimizationResult(
            timestamp=datetime.now(timezone.utc).isoformat(),
            window_hours=window_hours,
            risk_mode=self.risk_mode,
            bankroll=self.bankroll,
        )

        for coin in coins:
            coin_df = combined[combined["coin"] == coin].copy()
            if len(coin_df) < MIN_TRADES_PER_CONFIG:
                logger.info(f"{coin}: only {len(coin_df)} trades, skipping (need {MIN_TRADES_PER_CONFIG})")
                result.dropped_coins.append(coin)
                continue

            # Determine primary source for this coin
            source = "paper"
            if coin_df["_source"].value_counts().get("live", 0) > coin_df["_source"].value_counts().get("paper", 0):
                source = "live"

            cfg = self._optimize_coin(coin, coin_df, total_hours, source)
            if cfg is not None:
                result.coin_configs[coin] = cfg
                if cfg.expected_ev_per_day > 0:
                    result.recommended_coins.append(coin)
                else:
                    result.dropped_coins.append(coin)
            else:
                result.dropped_coins.append(coin)

        result.total_expected_ev_per_day = sum(
            cfg.expected_ev_per_day
            for coin, cfg in result.coin_configs.items()
            if coin in result.recommended_coins
        )

        return result

    def _optimize_coin(
        self,
        coin: str,
        df: pd.DataFrame,
        total_hours: float,
        source: str,
    ) -> Optional[CoinConfig]:
        """Sweep parameters for a single coin and return best config.

        Args:
            coin: Coin symbol (e.g. "BTC").
            df: DataFrame of trades for this coin (already filtered).
            total_hours: Total observation period in hours (for trades/day).
            source: "paper" or "live" — determines if degradation is applied.

        Returns:
            Best CoinConfig or None if no profitable config found.
        """
        best_cfg = None
        best_score = -999.0

        total_combos = (
            len(GRID["min_edge"])
            * len(GRID["entry_price"])
            * len(GRID["min_momentum"])
            * len(GRID["min_fv"])
            * len(GRID["min_tte"])
            * len(GRID["max_tte"])
            * len(GRID["require_vatic"])
        )

        evaluated = 0
        for (min_edge, (min_entry, max_entry), min_mom, min_fv,
             min_tte, max_tte, req_vatic) in product(
            GRID["min_edge"],
            GRID["entry_price"],
            GRID["min_momentum"],
            GRID["min_fv"],
            GRID["min_tte"],
            GRID["max_tte"],
            GRID["require_vatic"],
        ):
            evaluated += 1

            # Apply filters to dataframe
            mask = pd.Series(True, index=df.index)

            # Edge filter
            if "edge" in df.columns:
                mask &= df["edge"] >= min_edge

            # Entry price filter
            if "entry_price" in df.columns:
                mask &= df["entry_price"] >= min_entry
                mask &= df["entry_price"] <= max_entry

            # Momentum filter
            if "momentum_at_entry" in df.columns:
                mask &= df["momentum_at_entry"].abs() >= min_mom

            # Fair value filter
            if "fair_value_at_entry" in df.columns:
                mask &= df["fair_value_at_entry"] >= min_fv

            # Time-to-expiry / window elapsed filter
            if "time_to_expiry_at_entry" in df.columns:
                # TTE is seconds remaining; window_elapsed = window_length - TTE
                # For 5m market (300s): elapsed = 300 - TTE
                # min_tte (elapsed) means TTE <= 300 - min_tte
                # max_tte (elapsed) means TTE >= 300 - max_tte
                max_tte_remaining = 300 - min_tte  # e.g. min_tte=120 -> TTE <= 180
                min_tte_remaining = 300 - max_tte  # e.g. max_tte=180 -> TTE >= 120
                mask &= df["time_to_expiry_at_entry"] <= max_tte_remaining
                mask &= df["time_to_expiry_at_entry"] >= min_tte_remaining

            # Vatic filter
            if req_vatic and "strike_source" in df.columns:
                mask &= df["strike_source"].str.lower() == "vatic"

            filtered = df[mask]
            n_trades = len(filtered)

            if n_trades < MIN_TRADES_PER_CONFIG:
                continue

            # Compute stats
            wins = int(filtered["won"].sum())
            paper_wr = wins / n_trades

            # Apply degradation to get estimated live WR
            if source == "paper":
                if self.degradation is not None:
                    avg_edge = filtered["edge"].mean() if "edge" in filtered.columns else 0.20
                    avg_mom = filtered["momentum_at_entry"].abs().mean() if "momentum_at_entry" in filtered.columns else 0.001
                    live_wr = self.degradation.adjusted_win_rate(
                        paper_wr, coin, edge=avg_edge, momentum=avg_mom
                    )
                else:
                    live_wr = paper_wr - DEFAULT_DEGRADATION_PP
            else:
                # Source is live — no degradation needed
                live_wr = paper_wr

            live_wr = max(live_wr, 0.0)

            # Compute average entry price for EV calculation
            avg_entry = filtered["entry_price"].mean() if "entry_price" in filtered.columns else 0.55

            # Breakeven WR = entry_price (for min-size mode: cost = 5*p, win = 5*(1-p) profit)
            breakeven = self._breakeven_wr(avg_entry)

            # Wilson lower bound on the paper WR (conservative estimate)
            wilson_lb = self._wilson_lower(wins, n_trades)

            # Wilson lower bound must exceed breakeven AFTER degradation
            wilson_lb_adjusted = wilson_lb - (DEFAULT_DEGRADATION_PP if source == "paper" else 0.0)
            if wilson_lb_adjusted < breakeven:
                continue

            # EV per trade: WR * avg_win - (1-WR) * avg_loss
            # With 5 tokens at price p: win = 5*(1-p), loss = 5*p
            avg_win = 5.0 * (1.0 - avg_entry)
            avg_loss = 5.0 * avg_entry
            ev_per_trade = live_wr * avg_win - (1.0 - live_wr) * avg_loss

            if ev_per_trade <= 0:
                continue

            # Trades per day
            trades_per_day = n_trades / (total_hours / 24.0)
            ev_per_day = ev_per_trade * trades_per_day

            score = self._score_config(live_wr, ev_per_day, trades_per_day)

            if score > best_score:
                best_score = score
                best_cfg = CoinConfig(
                    coin=coin,
                    min_edge=min_edge,
                    min_momentum=min_mom,
                    min_fair_value=min_fv,
                    min_entry_price=min_entry,
                    max_entry_price=max_entry,
                    min_window_elapsed=float(min_tte),
                    max_window_elapsed=float(max_tte),
                    require_vatic=req_vatic,
                    expected_wr=round(live_wr, 4),
                    expected_ev_per_trade=round(ev_per_trade, 4),
                    expected_trades_per_day=round(trades_per_day, 2),
                    expected_ev_per_day=round(ev_per_day, 4),
                    paper_sample_size=n_trades,
                    wilson_lower_bound=round(wilson_lb, 4),
                )

        if best_cfg:
            logger.info(
                f"{coin}: best config -> WR={best_cfg.expected_wr:.1%}, "
                f"EV/day=${best_cfg.expected_ev_per_day:.2f} "
                f"(n={best_cfg.paper_sample_size}, {evaluated} combos evaluated)"
            )
        else:
            logger.info(f"{coin}: no profitable config found ({evaluated} combos evaluated)")

        return best_cfg

    @staticmethod
    def _wilson_lower(wins: int, total: int, z: float = 1.96) -> float:
        """Wilson score lower bound for win rate (95% CI)."""
        if total == 0:
            return 0.0
        p = wins / total
        denominator = 1 + z ** 2 / total
        centre = (p + z ** 2 / (2 * total)) / denominator
        spread = z * ((p * (1 - p) + z ** 2 / (4 * total)) / total) ** 0.5 / denominator
        return centre - spread

    @staticmethod
    def _breakeven_wr(avg_entry_price: float) -> float:
        """Calculate breakeven WR for given average entry price.

        With 5 tokens at price p: cost = 5*p, win payout = 5, loss = 0.
        EV = WR * 5 - 5*p = 0 -> WR = p.
        """
        return avg_entry_price
