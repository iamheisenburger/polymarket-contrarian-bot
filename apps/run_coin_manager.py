#!/usr/bin/env python3
"""
Coin Manager Runner — Decides which coins trade live vs paper.

Evaluates paper trade performance at the FIXED strategy filters and
promotes/demotes coins accordingly. Does NOT change strategy parameters.

Usage:
    # Basic — paper CSV only
    python apps/run_coin_manager.py --paper-csv data/paper_collector.csv --balance 18.00

    # With live CSV and degradation model
    python apps/run_coin_manager.py \
        --paper-csv data/paper_collector.csv \
        --live-csv data/live_trades.csv \
        --degradation data/degradation_model.json \
        --balance 18.00

    # Custom output path
    python apps/run_coin_manager.py \
        --paper-csv data/paper_collector.csv \
        --balance 18.00 \
        --output data/coin_decisions.json
"""

import os
import sys
import argparse
import logging
from pathlib import Path

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.coin_manager import CoinManager


def main():
    parser = argparse.ArgumentParser(
        description="Coin Manager — decide which coins trade live vs paper"
    )
    parser.add_argument(
        "--paper-csv", type=str, required=True,
        help="Path to paper collector CSV (wide filters, all coins)"
    )
    parser.add_argument(
        "--live-csv", type=str, default="",
        help="Path to live trade CSV (optional, for demotion detection)"
    )
    parser.add_argument(
        "--degradation", type=str, default="",
        help="Path to degradation model JSON (from lib/degradation.py)"
    )
    parser.add_argument(
        "--balance", type=float, default=0.0,
        help="Current USDC balance. Determines max live coins: "
             "<$5=0, <$15=1, <$30=2, >=$30=all. "
             "0 = auto-detect from Polymarket API."
    )
    parser.add_argument(
        "--output", type=str, default="data/coin_decisions.json",
        help="Output path for coin decisions JSON (default: data/coin_decisions.json)"
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
        print(f"WARNING: Live CSV not found: {args.live_csv}")
        args.live_csv = ""

    # Auto-detect balance if not provided
    balance = args.balance
    if balance <= 0:
        try:
            from dotenv import load_dotenv
            load_dotenv()
            from src.config import Config
            from src.bot import TradingBot
            config = Config.from_env()
            private_key = os.environ.get("POLY_PRIVATE_KEY", "")
            if private_key:
                bot = TradingBot(config=config, private_key=private_key)
                balance = bot.get_usdc_balance() or 0.0
                print(f"Auto-detected balance: ${balance:.2f}")
        except Exception:
            print("WARNING: Could not auto-detect balance. Using $0.00 (no live trading).")

    # Run coin manager
    print(f"\nPaper CSV:    {args.paper_csv}")
    if args.live_csv:
        print(f"Live CSV:     {args.live_csv}")
    if args.degradation:
        print(f"Degradation:  {args.degradation}")
    print(f"Balance:      ${balance:.2f}")

    cm = CoinManager()
    max_coins = cm.get_bankroll_coin_limit(balance)
    print(f"Max live coins: {max_coins} (based on ${balance:.2f} balance)")
    print()

    # Evaluate coins
    decisions = cm.evaluate_coins(
        paper_csv=args.paper_csv,
        live_csv=args.live_csv,
        degradation_model_path=args.degradation,
    )

    # Apply bankroll limit
    decisions = cm.apply_bankroll_limit(decisions, balance)

    # Print summary
    print(cm.summary())

    # Save decisions
    cm.save_decisions(decisions, balance, args.output)
    print(f"\nDecisions saved to {args.output}")

    # Print actionable output
    print()
    if decisions['live']:
        print(f"LIVE COINS: {' '.join(decisions['live'])}")
    else:
        if balance < 5.0:
            print("NO LIVE COINS — balance too low (< $5.00)")
        else:
            print("NO LIVE COINS — no coins meet promotion criteria yet")

    if decisions['paper']:
        print(f"PAPER COINS: {' '.join(decisions['paper'])}")


if __name__ == "__main__":
    main()
