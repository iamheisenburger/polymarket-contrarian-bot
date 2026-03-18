#!/usr/bin/env python3
"""
Benchmark CLI — Updates and displays the rolling strategy benchmark.

Usage:
    python apps/run_benchmark.py --trade-csv data/live_trades.csv
    python apps/run_benchmark.py --trade-csv data/live_trades.csv --save
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.benchmark import StrategyBenchmark
from lib.coin_manager import CoinManager


def main():
    parser = argparse.ArgumentParser(description="Update strategy benchmark from trade CSV")
    parser.add_argument(
        "--trade-csv", type=str, default="data/live_trades.csv",
        help="Path to resolved trade CSV (default: data/live_trades.csv)",
    )
    parser.add_argument(
        "--benchmark-path", type=str, default="data/benchmark.json",
        help="Path to benchmark JSON (default: data/benchmark.json)",
    )
    parser.add_argument(
        "--strategy-name", type=str, default="v4_ema_4_16",
        help="Strategy name (default: v4_ema_4_16)",
    )
    parser.add_argument(
        "--save", action="store_true",
        help="Save benchmark state to JSON",
    )
    args = parser.parse_args()

    bench = StrategyBenchmark(args.strategy_name, args.benchmark_path)

    # Try loading existing state first
    bench.load()

    # Update from trade CSV
    bench.update_from_trades(args.trade_csv, CoinManager.FIXED_FILTERS)

    # Print summary
    print(bench.summary())

    if args.save:
        bench.save()
        print(f"\nBenchmark saved to {args.benchmark_path}")


if __name__ == "__main__":
    main()
