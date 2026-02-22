#!/usr/bin/env python3
"""
CLI entry point for the Weather Edge Bot.

Usage:
    python apps/run_weather.py --bankroll 12.95 --min-edge 0.05 --cities NYC London Seoul
    python apps/run_weather.py --scan-only          # one-shot scan, no trading loop
"""

import argparse
import asyncio
import logging
import sys

from lib.weather_api import CITIES
from strategies.weather_edge import WeatherConfig, WeatherEdgeBot


def main():
    parser = argparse.ArgumentParser(description="Weather Edge Bot")
    parser.add_argument("--bankroll", type=float, default=12.95)
    parser.add_argument("--min-edge", type=float, default=0.05,
                        help="Minimum edge (model_prob - price) to trade (default: 0.05)")
    parser.add_argument("--kelly", type=float, default=0.5,
                        help="Kelly fraction (default: 0.5 = half-Kelly)")
    parser.add_argument("--max-pos", type=float, default=0.20,
                        help="Max position as fraction of bankroll (default: 0.20)")
    parser.add_argument("--cities", nargs="+", default=list(CITIES.keys()),
                        help=f"Cities to trade (default: all). Options: {', '.join(CITIES.keys())}")
    parser.add_argument("--scan-interval", type=int, default=300,
                        help="Seconds between scans (default: 300)")
    parser.add_argument("--live", action="store_true",
                        help="Enable live trading (default: observe/paper)")
    parser.add_argument("--scan-only", action="store_true",
                        help="One-shot scan: show opportunities then exit")
    parser.add_argument("--debug", action="store_true",
                        help="Enable debug logging")
    args = parser.parse_args()

    # Logging
    level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Validate cities
    for city in args.cities:
        if city not in CITIES:
            print(f"ERROR: Unknown city '{city}'. Options: {', '.join(CITIES.keys())}")
            sys.exit(1)

    config = WeatherConfig(
        bankroll=args.bankroll,
        observe_only=not args.live,
        min_edge=args.min_edge,
        kelly_fraction=args.kelly,
        max_position_pct=args.max_pos,
        cities=args.cities,
        scan_interval=args.scan_interval,
    )

    bot = WeatherEdgeBot(config)

    if args.scan_only:
        # One-shot scan
        print(f"\nScanning {len(config.cities)} cities for weather edge...\n")
        opps = bot.scan_opportunities()
        if not opps:
            print("No opportunities found with edge >= {:.0%}".format(config.min_edge))
        else:
            print(f"Found {len(opps)} opportunities:\n")
            print(f"{'City':<8} {'Date':<12} {'Bucket':<10} {'Ask':>6} {'Model':>7} {'Edge':>7} {'Count':>7} {'Normal':>7} {'Mean':>6} {'Std':>5}")
            print("-" * 90)
            for opp in opps:
                mkt = opp["market"]
                print(
                    f"{mkt.city:<8} {mkt.date:<12} {mkt.bucket_label:<10} "
                    f"${mkt.best_ask:.3f} {opp['model_prob']:>6.1%} {opp['edge']:>+6.1%} "
                    f"{opp['prob_count']:>6.1%} {opp['prob_normal']:>6.1%} "
                    f"{opp['members_mean']:>5.1f} {opp['members_std']:>4.1f}"
                )
            print()

            # Show what trades would be placed
            print("Trades that would be placed:\n")
            for opp in opps:
                trade = bot.evaluate_opportunity(opp)
                if trade:
                    mkt = trade["market"]
                    print(
                        f"  BUY {mkt.city} {mkt.date} {mkt.bucket_label} "
                        f"@ ${mkt.best_ask:.3f} | {trade['num_tokens']:.0f} tokens, "
                        f"${trade['cost']:.2f} | Kelly={trade['kelly_f']:.1%}"
                    )
        return

    # Main loop
    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
