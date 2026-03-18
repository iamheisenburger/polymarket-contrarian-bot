"""
Strategy Discovery Layer — Sweeps filter combinations on paper data.

Finds candidates that beat the current benchmark. NEVER deploys anything.
Only reports candidates for human review.

Usage:
    from lib.benchmark import StrategyBenchmark
    from lib.discovery import StrategyDiscovery

    bench = StrategyBenchmark("v4", "data/benchmark.json")
    bench.load()
    disco = StrategyDiscovery(bench)
    candidates = disco.sweep("data/paper_collector.csv", window_days=7)
    print(disco.summary(candidates))
"""

import csv
import json
import logging
from datetime import datetime, timezone, timedelta
from itertools import product
from pathlib import Path
from typing import List, Optional

from lib.benchmark import StrategyBenchmark, _wilson_lower, PROVISIONAL_THRESHOLD

logger = logging.getLogger(__name__)


class StrategyDiscovery:
    """Sweeps filter combinations on paper data. NEVER deploys anything."""

    EDGE_RANGE = [0.10, 0.15, 0.20, 0.25, 0.30]
    MOMENTUM_RANGE = [0.0003, 0.0005, 0.0008, 0.001]
    ENTRY_PRICE_RANGES = [
        (0.50, 0.65), (0.55, 0.70), (0.60, 0.70), (0.60, 0.75), (0.65, 0.75),
    ]
    FV_RANGE = [0.65, 0.70, 0.75, 0.80, 0.85]
    TTE_RANGES = [(90, 180), (100, 180), (120, 180), (120, 200), (150, 180)]

    MAX_DEGRADATION_ASSUMPTION = 0.20  # assume 20pp worst case live degradation
    BREAKEVEN_WR = 0.62  # approximate breakeven at avg entry prices

    def __init__(self, benchmark: StrategyBenchmark):
        self.benchmark = benchmark

    def sweep(self, paper_csv: str, window_days: int = 7) -> list:
        """Sweep all filter combinations on last N days of paper data.

        Returns list of candidates that:
        1. Have 100+ trades in the window
        2. Beat benchmark WR by >5pp (using Wilson lower bounds)
        3. Are profitable after assuming 20pp worst-case degradation
        """
        path = Path(paper_csv)
        if not path.exists():
            logger.warning(f"Paper CSV not found: {paper_csv}")
            return []

        # Load and filter trades to time window
        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        trades = self._load_trades(paper_csv, cutoff)
        if not trades:
            logger.warning("No trades found in time window")
            return []

        logger.info(f"Loaded {len(trades)} trades from last {window_days} days")

        # Sweep all combinations
        candidates = []
        combos = list(product(
            self.EDGE_RANGE,
            self.MOMENTUM_RANGE,
            self.ENTRY_PRICE_RANGES,
            self.FV_RANGE,
            self.TTE_RANGES,
        ))
        logger.info(f"Sweeping {len(combos)} filter combinations...")

        for min_edge, min_mom, (min_ep, max_ep), min_fv, (min_tte, max_tte) in combos:
            filtered = []
            for t in trades:
                if t['edge'] < min_edge:
                    continue
                if t['momentum'] < min_mom:
                    continue
                if t['entry_price'] < min_ep or t['entry_price'] > max_ep:
                    continue
                if t['fair_value'] < min_fv:
                    continue
                if t['tte'] < min_tte or t['tte'] > max_tte:
                    continue
                filtered.append(t)

            n = len(filtered)
            if n < PROVISIONAL_THRESHOLD:
                continue

            wins = sum(1 for t in filtered if t['won'])
            wr = wins / n
            wilson = _wilson_lower(wins, n)

            # Check 1: beats benchmark by 5pp Wilson lower
            if not self.benchmark.beats_benchmark(wr, n, margin=0.05):
                continue

            # Check 2: profitable after worst-case degradation
            degraded_wr = wr - self.MAX_DEGRADATION_ASSUMPTION
            if degraded_wr < self.BREAKEVEN_WR:
                continue

            # Calculate EV
            avg_entry = sum(t['entry_price'] for t in filtered) / n
            avg_payout = 1.0 / avg_entry if avg_entry > 0 else 0
            ev_per_trade = wr * (avg_payout - 1) * avg_entry - (1 - wr) * avg_entry
            trades_per_day = n / max(window_days, 1)
            ev_per_day = ev_per_trade * trades_per_day

            candidates.append({
                'filters': {
                    'min_edge': min_edge,
                    'min_momentum': min_mom,
                    'min_entry_price': min_ep,
                    'max_entry_price': max_ep,
                    'min_fair_value': min_fv,
                    'min_tte': min_tte,
                    'max_tte': max_tte,
                },
                'wr': round(wr, 4),
                'n': n,
                'wilson_lower': round(wilson, 4),
                'degraded_wr': round(degraded_wr, 4),
                'ev_per_trade': round(ev_per_trade, 4),
                'ev_per_day': round(ev_per_day, 4),
                'avg_entry': round(avg_entry, 4),
            })

        # Sort by Wilson lower descending
        candidates.sort(key=lambda c: c['wilson_lower'], reverse=True)
        logger.info(f"Found {len(candidates)} candidates beating benchmark")
        return candidates

    def save_candidates(self, candidates: list, path: str):
        """Save to JSON file."""
        filepath = Path(path)
        filepath.parent.mkdir(parents=True, exist_ok=True)
        data = {
            'benchmark': {
                'strategy': self.benchmark.strategy_name,
                'wr': round(self.benchmark.get_wr(), 4),
                'wilson_lower': round(self.benchmark.get_wilson_lower(), 4),
                'n': self.benchmark.total_trades,
                'is_official': self.benchmark.is_official(),
            },
            'candidates': candidates,
            'generated_at': datetime.now(timezone.utc).isoformat(),
        }
        with open(filepath, "w") as f:
            json.dump(data, f, indent=2)
        logger.info(f"Saved {len(candidates)} candidates to {filepath}")

    def summary(self, candidates: list) -> str:
        """Human readable report."""
        bench_status = "OFFICIAL" if self.benchmark.is_official() else "PROVISIONAL"
        lines = [
            "=" * 60,
            "  STRATEGY DISCOVERY REPORT",
            "=" * 60,
            f"  Benchmark: {self.benchmark.strategy_name} ({bench_status})",
            f"  Benchmark WR: {self.benchmark.get_wr():.1%} (Wilson: {self.benchmark.get_wilson_lower():.1%})",
            f"  Benchmark N:  {self.benchmark.total_trades}",
            "",
            f"  Candidates found: {len(candidates)}",
            f"  Degradation assumption: {self.MAX_DEGRADATION_ASSUMPTION:.0%}",
            f"  Breakeven WR: {self.BREAKEVEN_WR:.0%}",
            "",
        ]

        if not candidates:
            lines.append("  No candidates beat the benchmark. Current filters are optimal.")
        else:
            for i, c in enumerate(candidates[:10]):  # top 10
                f = c['filters']
                lines.append(f"  --- Candidate #{i+1} ---")
                lines.append(f"    WR: {c['wr']:.1%} (Wilson: {c['wilson_lower']:.1%}), N={c['n']}")
                lines.append(f"    Degraded WR: {c['degraded_wr']:.1%} (after {self.MAX_DEGRADATION_ASSUMPTION:.0%} assumed)")
                lines.append(f"    EV/trade: ${c['ev_per_trade']:.4f}, EV/day: ${c['ev_per_day']:.2f}")
                lines.append(
                    f"    Filters: edge>={f['min_edge']}, mom>={f['min_momentum']}, "
                    f"price=[{f['min_entry_price']},{f['max_entry_price']}], "
                    f"fv>={f['min_fair_value']}, tte=[{f['min_tte']},{f['max_tte']}]"
                )
                lines.append("")

            if len(candidates) > 10:
                lines.append(f"  ... and {len(candidates) - 10} more candidates")

        lines.append("")
        lines.append("  NOTE: Discovery NEVER deploys. Human review required.")
        lines.append("=" * 60)
        return "\n".join(lines)

    def _load_trades(self, csv_path: str, cutoff: datetime) -> list:
        """Load resolved trades from CSV, filtered to cutoff date."""
        trades = []
        with open(csv_path, "r") as f:
            reader = csv.DictReader(f)
            for row in reader:
                outcome = row.get("outcome", "").strip()
                if outcome not in ("won", "lost"):
                    continue

                # Parse timestamp
                ts_str = row.get("timestamp", "")
                try:
                    if "T" in ts_str:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    else:
                        ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts < cutoff:
                        continue
                except (ValueError, TypeError):
                    continue  # skip unparseable timestamps

                try:
                    entry_price = float(row.get("entry_price", 0))
                    fair_value = float(row.get("fair_value_at_entry", 0))
                    edge = fair_value - entry_price
                    momentum = abs(float(row.get("momentum_at_entry", 0)))
                    tte = float(row.get("time_to_expiry_at_entry", 0))
                except (ValueError, TypeError):
                    continue

                trades.append({
                    'entry_price': entry_price,
                    'fair_value': fair_value,
                    'edge': edge,
                    'momentum': momentum,
                    'tte': tte,
                    'won': outcome == "won",
                })

        return trades
