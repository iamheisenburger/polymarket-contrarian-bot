#!/usr/bin/env python3
"""
Momentum Sniper Strategy Runner

Scans multiple crypto binary markets for mispricings between Binance spot
price and Polymarket odds, then snipes the cheap side before the market
adjusts. Holds to settlement and compounds winnings.

Usage:
    # Live mode — BTC only, $20 bankroll
    python apps/run_sniper.py --coins BTC --bankroll 20

    # Multi-coin scanning (more opportunities)
    python apps/run_sniper.py --coins BTC ETH SOL --bankroll 20

    # Observe mode (see signals, no real trades)
    python apps/run_sniper.py --coins BTC ETH --observe

    # Aggressive settings (tighter edge, stronger Kelly)
    python apps/run_sniper.py --coins BTC --bankroll 20 --min-edge 0.04 --kelly 0.6

    # Conservative (wider edge requirement)
    python apps/run_sniper.py --coins BTC ETH --bankroll 20 --min-edge 0.08
"""

import os
import sys
import asyncio
import argparse
import logging
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Suppress noisy logs
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("src.bot").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# Auto-load .env
from dotenv import load_dotenv
load_dotenv()

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.console import Colors
from src.bot import TradingBot
from src.config import Config
from strategies.momentum_sniper import MomentumSniperStrategy, SniperConfig


def main():
    parser = argparse.ArgumentParser(
        description="Momentum Sniper for Polymarket crypto binary markets"
    )
    parser.add_argument(
        "--coins", nargs="+", type=str, default=["BTC"],
        help="Coins to scan (default: BTC). Options: BTC ETH SOL XRP"
    )
    parser.add_argument(
        "--timeframe", type=str, default="15m",
        choices=["5m", "15m"],
        help="Market timeframe (default: 15m)"
    )
    parser.add_argument(
        "--bankroll", type=float, default=20.0,
        help="Starting bankroll in USDC (default: 20.0)"
    )
    parser.add_argument(
        "--min-edge", type=float, default=0.05,
        help="Minimum edge to enter a trade (default: 0.05 = 5 cents)"
    )
    parser.add_argument(
        "--strong-edge", type=float, default=0.10,
        help="Strong edge threshold for larger Kelly (default: 0.10)"
    )
    parser.add_argument(
        "--kelly", type=float, default=0.50,
        help="Kelly fraction for normal signals (default: 0.50)"
    )
    parser.add_argument(
        "--kelly-strong", type=float, default=0.75,
        help="Kelly fraction for strong signals (default: 0.75)"
    )
    parser.add_argument(
        "--max-bet", type=float, default=100.0,
        help="Max USDC per single trade — Kelly handles sizing (default: 100)"
    )
    parser.add_argument(
        "--min-size", action="store_true",
        help="Conservative mode — always bet minimum 5 tokens per trade for data gathering"
    )
    parser.add_argument(
        "--max-entry-price", type=float, default=0.85,
        help="Max entry price to trade at (default: 0.85). Lower = cheaper trades, more survival."
    )
    parser.add_argument(
        "--observe", action="store_true",
        help="Observe-only mode — show signals but don't trade"
    )
    parser.add_argument(
        "--log-file", type=str, default="data/sniper_trades.csv",
        help="Trade log CSV file (default: data/sniper_trades.csv)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)

    # Validate coins
    valid_coins = ["BTC", "ETH", "SOL", "XRP"]
    coins = [c.upper() for c in args.coins]
    for c in coins:
        if c not in valid_coins:
            print(f"{Colors.RED}Invalid coin: {c}. Options: {valid_coins}{Colors.RESET}")
            sys.exit(1)

    # Check environment
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")

    if not private_key or not safe_address:
        print(f"{Colors.RED}Error: POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS must be set{Colors.RESET}")
        print("Set them in .env file or export as environment variables")
        sys.exit(1)

    # Create bot
    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)

    if not bot.is_initialized():
        print(f"{Colors.RED}Error: Failed to initialize bot{Colors.RESET}")
        sys.exit(1)

    # Create strategy config
    strategy_config = SniperConfig(
        coins=coins,
        timeframe=args.timeframe,
        min_edge=args.min_edge,
        strong_edge=args.strong_edge,
        kelly_fraction=args.kelly,
        kelly_strong=args.kelly_strong,
        bankroll=args.bankroll,
        max_bet_usdc=args.max_bet,
        max_entry_price=args.max_entry_price,
        min_size_mode=args.min_size,
        observe_only=args.observe,
        log_file=args.log_file,
    )

    # Print config
    mode = f"{Colors.YELLOW}OBSERVE ONLY{Colors.RESET}" if args.observe else f"{Colors.GREEN}LIVE TRADING{Colors.RESET}"

    print(f"\n{'='*60}")
    print(f"  MOMENTUM SNIPER — {'/'.join(coins)} {args.timeframe}")
    print(f"{'='*60}\n")
    print(f"  Mode:           {mode}")
    print(f"  Coins:          {', '.join(coins)}")
    print(f"  Timeframe:      {args.timeframe}")
    print(f"  Bankroll:       ${args.bankroll:.2f}")
    print()

    # Edge info
    print(f"  Edge Thresholds:")
    print(f"    Min edge:     {args.min_edge:.2f} ({args.min_edge*100:.0f} cents)")
    print(f"    Strong edge:  {args.strong_edge:.2f} ({args.strong_edge*100:.0f} cents)")
    print()

    # Position sizing info
    if args.min_size:
        print(f"  Position Sizing: MINIMUM SIZE MODE (conservative)")
        print(f"    Every trade: 5 tokens (Polymarket minimum)")
        print(f"    Cost per trade: ~$1.00-$4.25 depending on entry price")
        print(f"    Purpose: gather data on whether edge is real")
        print()
        print(f"  Example trade (10c edge):")
        print(f"    Buy 5 tokens at $0.40 = $2.00 cost")
        print(f"    Win: $5.00 payout (+$3.00 profit)")
        print(f"    Loss: $0.00 payout (-$2.00 loss)")
    else:
        print(f"  Position Sizing (Kelly Criterion):")
        print(f"    Normal Kelly: {args.kelly:.0%}")
        print(f"    Strong Kelly: {args.kelly_strong:.0%}")
        print(f"    Min per trade: $1.00 (Polymarket floor)")
        print()
        ex_price = 0.40
        ex_fair = 0.50
        b = (1.0 / ex_price) - 1.0
        kelly_f = (ex_fair * b - 0.50) / b
        ex_bet = kelly_f * args.kelly * args.bankroll
        print(f"  Example trade (10c edge):")
        print(f"    Buy at $0.40, fair value $0.50")
        print(f"    Payout if win: ${1.0/ex_price:.2f} per token (2.5x)")
        print(f"    Kelly fraction: {kelly_f:.3f}, bet = ${max(1.0, ex_bet):.2f}")
    print()

    print(f"  Auto-redeems winning tokens to USDC on every market settlement")
    print()

    if not args.observe:
        print(f"{Colors.YELLOW}  WARNING: This will place REAL orders with REAL money.{Colors.RESET}")
        print(f"  Starting with ${args.bankroll:.2f} USDC.")
        print()

    strategy = MomentumSniperStrategy(bot=bot, config=strategy_config)

    try:
        asyncio.run(strategy.run())
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Interrupted by user{Colors.RESET}")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
