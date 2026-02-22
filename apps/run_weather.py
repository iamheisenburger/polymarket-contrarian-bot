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
    parser.add_argument("--min-models", type=int, default=2,
                        help="Min models that must agree on >=5%% prob (default: 2)")
    parser.add_argument("--max-bets-per-city", type=int, default=1,
                        help="Max bets per city-date combination (default: 1)")
    parser.add_argument("--no-hrrr", action="store_true",
                        help="Disable HRRR for US same-day predictions")
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
        min_models_agree=args.min_models,
        max_bets_per_city_date=args.max_bets_per_city,
        use_hrrr=not args.no_hrrr,
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
            print(f"Found {len(opps)} consensus opportunities:\n")
            print(f"{'City':<14} {'Date':<12} {'Bucket':<10} {'Ask':>6} {'Consens':>8} {'Edge':>7} {'Agree':>6} {'Per-Model Detail'}")
            print("-" * 110)
            for opp in opps:
                mkt = opp["market"]
                pm = opp.get("per_model", {})
                pm_str = " ".join(f"{k}={v:.0%}" for k, v in pm.items())
                hrrr = "+H" if opp.get("hrrr_boost") else "  "
                print(
                    f"{mkt.city:<14} {mkt.date:<12} {mkt.bucket_label:<10} "
                    f"${mkt.best_ask:.3f} {opp['model_prob']:>7.1%} {opp['edge']:>+6.1%} "
                    f"{opp.get('models_agreeing', '?'):>3}/4{hrrr} "
                    f"[{pm_str}]"
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
