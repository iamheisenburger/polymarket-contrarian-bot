#!/usr/bin/env python3
"""
Strategy Optimizer Runner — Layer 3 of the Adaptive Trading System

Sweeps filter configurations per-coin using paper/signal trade data,
applies degradation adjustments, and outputs the EV-maximizing config.

Usage:
    # Basic — paper CSV only (uses default 5pp degradation)
    python apps/run_optimizer.py --paper-csv data/paper_fat_edge.csv

    # Paper + live CSVs (for coins like BTC that have live data)
    python apps/run_optimizer.py --paper-csv data/paper_fat_edge.csv --live-csv data/btc_live_tmp.csv

    # With degradation model
    python apps/run_optimizer.py --paper-csv data/paper_fat_edge.csv --degradation data/degradation_model.json

    # Custom window (7 days instead of 14)
    python apps/run_optimizer.py --paper-csv data/paper_fat_edge.csv --window-hours 168
"""

import os
import sys
import argparse
import logging
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.optimizer import StrategyOptimizer, OptimizationResult


def main():
    parser = argparse.ArgumentParser(
        description="Strategy Optimizer — sweep filter configs to find EV-maximizing parameters"
    )
    parser.add_argument(
        "--paper-csv", type=str, required=True,
        help="Path to paper trade CSV (or signal CSV)"
    )
    parser.add_argument(
        "--live-csv", type=str, default=None,
        help="Path to live trade CSV (optional, for coins with live data)"
    )
    parser.add_argument(
        "--degradation", type=str, default=None,
        help="Path to degradation model JSON (from lib/degradation.py). "
             "If not provided, uses conservative 5pp default."
    )
    parser.add_argument(
        "--window-hours", type=float, default=336,
        help="Rolling window in hours (default: 336 = 14 days). 0 = use all data."
    )
    parser.add_argument(
        "--output", type=str, default="data/optimal_config.json",
        help="Output path for optimization result JSON (default: data/optimal_config.json)"
    )
    parser.add_argument(
        "--verbose", action="store_true",
        help="Enable verbose logging"
    )

    args = parser.parse_args()

    # Setup logging
    level = logging.DEBUG if args.verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    # Validate inputs
    if not Path(args.paper_csv).exists():
        print(f"ERROR: Paper CSV not found: {args.paper_csv}")
        sys.exit(1)

    if args.live_csv and not Path(args.live_csv).exists():
        print(f"ERROR: Live CSV not found: {args.live_csv}")
        sys.exit(1)

    # Load degradation model if provided
    degradation_model = None
    if args.degradation:
        deg_path = Path(args.degradation)
        if deg_path.exists():
            from lib.degradation import DegradationEstimator
            degradation_model = DegradationEstimator()
            degradation_model.load(str(deg_path))
            print(f"Loaded degradation model from {deg_path}")
        else:
            print(f"WARNING: Degradation model not found at {deg_path}, using default 5pp")

    # Run optimizer
    print(f"\nPaper CSV:  {args.paper_csv}")
    if args.live_csv:
        print(f"Live CSV:   {args.live_csv}")
    print(f"Window:     {args.window_hours:.0f} hours ({args.window_hours / 24:.1f} days)")
    print(f"Degradation: {'model' if degradation_model else 'default 5pp'}")
    print()

    optimizer = StrategyOptimizer(degradation_model=degradation_model)
    result = optimizer.optimize_from_csvs(
        paper_csv=args.paper_csv,
        live_csv=args.live_csv,
        window_hours=args.window_hours,
    )

    # Print summary
    print(result.summary())

    # Save to JSON
    result.to_json(args.output)
    print(f"\nResult saved to {args.output}")


if __name__ == "__main__":
    main()
