#!/usr/bin/env python3
"""
Discovery CLI — Sweeps filter combinations on paper data to find candidates.

NEVER deploys anything. Only reports candidates for human review.

Usage:
    python apps/run_discovery.py --paper-csv data/paper_collector.csv
    python apps/run_discovery.py --paper-csv data/paper_collector.csv --window-days 14
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.benchmark import StrategyBenchmark
from lib.discovery import StrategyDiscovery


def main():
    parser = argparse.ArgumentParser(description="Sweep filter combinations on paper data")
    parser.add_argument(
        "--paper-csv", type=str, default="data/paper_collector.csv",
        help="Path to paper collector CSV (default: data/paper_collector.csv)",
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
        "--window-days", type=int, default=7,
        help="Number of days of paper data to sweep (default: 7)",
    )
    parser.add_argument(
        "--output", type=str, default="data/strategy_candidates.json",
        help="Output path for candidates JSON (default: data/strategy_candidates.json)",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Load benchmark
    bench = StrategyBenchmark(args.strategy_name, args.benchmark_path)
    bench.load()

    if bench.total_trades == 0:
        print("ERROR: No benchmark data. Run apps/run_benchmark.py --save first.")
        sys.exit(1)

    # Run discovery sweep
    disco = StrategyDiscovery(bench)
    candidates = disco.sweep(args.paper_csv, window_days=args.window_days)

    # Print report
    print(disco.summary(candidates))

    # Save candidates
    disco.save_candidates(candidates, args.output)


if __name__ == "__main__":
    main()
