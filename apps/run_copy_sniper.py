#!/usr/bin/env python3
"""
Copy Sniper — Alpha Wallet Copy-Trade System

Follows proven top Polymarket traders across SPORTS, POLITICS, ECONOMICS,
and other categories. When an alpha wallet makes a new BUY trade, copies
it after safety checks (slippage, age, liquidity, price range).

Usage:
    # Paper trading (default)
    python apps/run_copy_sniper.py --bankroll 50

    # Custom categories
    python apps/run_copy_sniper.py --categories SPORTS POLITICS --bankroll 50

    # Faster polling
    python apps/run_copy_sniper.py --poll-interval 30 --bankroll 50

    # Live trading (when ready)
    python apps/run_copy_sniper.py --live --bankroll 12.95
"""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from strategies.copy_sniper import CopySniper, CopySniperConfig


def parse_args():
    parser = argparse.ArgumentParser(
        description="Copy Sniper — follow proven Polymarket winners",
    )
    parser.add_argument(
        "--bankroll", type=float, default=50.0,
        help="Starting paper bankroll (default: $50)",
    )
    parser.add_argument(
        "--categories", nargs="+",
        default=["SPORTS", "POLITICS", "ECONOMICS", "CULTURE", "TECH", "FINANCE"],
        help="Leaderboard categories to track (default: all non-crypto)",
    )
    parser.add_argument(
        "--poll-interval", type=int, default=60,
        help="Seconds between polling alpha wallets (default: 60)",
    )
    parser.add_argument(
        "--settle-interval", type=int, default=60,
        help="Seconds between settlement checks (default: 60)",
    )
    parser.add_argument(
        "--min-weekly-pnl", type=float, default=1000.0,
        help="Minimum weekly PnL to qualify as alpha wallet (default: $1000)",
    )
    parser.add_argument(
        "--wallets-per-category", type=int, default=10,
        help="Top N wallets per category to track (default: 10)",
    )
    parser.add_argument(
        "--max-slippage", type=float, default=0.05,
        help="Max price slippage from alpha entry (default: $0.05)",
    )
    parser.add_argument(
        "--max-trade-age", type=int, default=300,
        help="Max seconds since alpha trade to copy (default: 300)",
    )
    parser.add_argument(
        "--min-entry", type=float, default=0.10,
        help="Min entry price (default: $0.10)",
    )
    parser.add_argument(
        "--max-entry", type=float, default=0.90,
        help="Max entry price (default: $0.90)",
    )
    parser.add_argument(
        "--kelly", type=float, default=0.5,
        help="Kelly fraction (0.5 = half-Kelly, 1.0 = full Kelly). Default: 0.5",
    )
    parser.add_argument(
        "--max-hours", type=float, default=24.0,
        help="Max hours to market resolution (default: 24). 0 = no filter.",
    )
    parser.add_argument(
        "--live", action="store_true",
        help="Enable live trading (default: paper/observe only)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    config = CopySniperConfig(
        bankroll=args.bankroll,
        observe_only=not args.live,
        categories=args.categories,
        poll_interval=args.poll_interval,
        settle_interval=args.settle_interval,
        min_weekly_pnl=args.min_weekly_pnl,
        wallets_per_category=args.wallets_per_category,
        max_slippage=args.max_slippage,
        max_trade_age=args.max_trade_age,
        min_entry_price=args.min_entry,
        max_entry_price=args.max_entry,
        kelly_fraction=args.kelly,
        max_hours_to_resolution=args.max_hours,
    )

    if args.live:
        print("\n⚠️  LIVE TRADING MODE — Real money will be used!")
        print("    Make sure your .env is configured correctly.\n")

    sniper = CopySniper(config)

    try:
        asyncio.run(sniper.run())
    except KeyboardInterrupt:
        print("\nShutdown requested.")


if __name__ == "__main__":
    main()
