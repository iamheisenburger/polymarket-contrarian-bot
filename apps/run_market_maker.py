#!/usr/bin/env python3
"""
Market Maker Strategy Runner

Provides liquidity to Polymarket binary crypto markets by posting
two-sided quotes around a fair value derived from Binance real-time prices.
Kelly Criterion sizes each quote dynamically based on edge and bankroll.

Usage:
    # Live mode - BTC 5-minute markets
    python apps/run_market_maker.py --coins BTC --bankroll 20

    # All 4 coins in one process
    python apps/run_market_maker.py --coins BTC ETH SOL XRP --observe

    # Custom spread and Kelly fraction
    python apps/run_market_maker.py --coins BTC --spread 0.05 --kelly 0.25
"""

import os
import sys
import asyncio
import argparse
import logging
from pathlib import Path

# Fix Windows console encoding for ANSI colors
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

# Auto-load .env file
from dotenv import load_dotenv
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.console import Colors
from src.bot import TradingBot
from src.config import Config
from strategies.market_maker import MarketMakerStrategy, MarketMakerConfig


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Market Maker for Polymarket crypto binary markets"
    )
    parser.add_argument(
        "--coins", type=str, nargs="+", default=["BTC"],
        help="Coins to make markets on (default: BTC)"
    )
    # Keep --coin for backwards compat
    parser.add_argument(
        "--coin", type=str, default=None,
        help="(deprecated, use --coins) Single coin"
    )
    parser.add_argument(
        "--timeframe", type=str, default="15m",
        choices=["5m", "15m"],
        help="Market timeframe (default: 15m)"
    )
    parser.add_argument(
        "--spread", type=float, default=0.04,
        help="Half-spread around fair value (default: 0.04 = 4 cents)"
    )
    parser.add_argument(
        "--min-edge", type=float, default=0.02,
        help="Minimum edge to post a quote (default: 0.02)"
    )
    parser.add_argument(
        "--kelly", type=float, default=0.50,
        help="Kelly fraction (0.25=quarter, 0.5=half, 1.0=full) (default: 0.50)"
    )
    parser.add_argument(
        "--bankroll", type=float, default=10.0,
        help="Starting bankroll in USDC (default: 10.0)"
    )
    parser.add_argument(
        "--max-quote", type=float, default=10.0,
        help="Max USDC per single quote (Kelly ceiling) (default: 10.0)"
    )
    parser.add_argument(
        "--max-inv", type=int, default=5,
        help="Max fills per side per market before pausing (default: 5)"
    )
    parser.add_argument(
        "--requote-interval", type=float, default=5.0,
        help="Minimum seconds between requotes (default: 5.0)"
    )
    parser.add_argument(
        "--stop-before-expiry", type=int, default=120,
        help="Seconds before expiry to stop quoting (default: 120)"
    )
    parser.add_argument(
        "--observe", action="store_true",
        help="Observe-only mode — show fair values but don't trade"
    )
    parser.add_argument(
        "--log-file", type=str, default="data/mm_trades.csv",
        help="Trade log CSV file base (coin suffix added for multi-coin)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("src.websocket_client").setLevel(logging.DEBUG)
        logging.getLogger("lib.binance_ws").setLevel(logging.DEBUG)

    # Handle --coin (deprecated) vs --coins
    coins = args.coins
    if args.coin:
        coins = [args.coin.upper()]
    coins = [c.upper() for c in coins]

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

    # Derive per-coin log files
    base_log = args.log_file
    if len(coins) > 1:
        stem = Path(base_log).stem
        suffix = Path(base_log).suffix
        parent = Path(base_log).parent
        log_files = {coin: str(parent / f"{stem}_{coin.lower()}{suffix}") for coin in coins}
    else:
        log_files = {coins[0]: base_log}

    # Create one strategy per coin
    strategies = []
    for coin in coins:
        strategy_config = MarketMakerConfig(
            coin=coin,
            timeframe=args.timeframe,
            half_spread=args.spread,
            min_edge=args.min_edge,
            kelly_fraction=args.kelly,
            starting_bankroll=args.bankroll,
            max_quote_usdc=args.max_quote,
            max_inventory_per_side=args.max_inv,
            min_requote_interval=args.requote_interval,
            stop_quoting_seconds=args.stop_before_expiry,
            observe_only=args.observe,
            log_file=log_files[coin],
            market_check_interval=30.0,
        )
        strategies.append(MarketMakerStrategy(bot=bot, config=strategy_config))

    # Print configuration
    mode = f"{Colors.YELLOW}OBSERVE ONLY{Colors.RESET}" if args.observe else f"{Colors.GREEN}LIVE TRADING{Colors.RESET}"
    coin_str = "/".join(coins)

    print(f"\n{'='*60}")
    print(f"  MARKET MAKER — {coin_str} {args.timeframe} Binary Markets")
    print(f"{'='*60}\n")
    print(f"  Mode:              {mode}")
    print(f"  Coins:             {coin_str} ({len(coins)} coin{'s' if len(coins) > 1 else ''})")
    print(f"  Timeframe:         {args.timeframe}")
    print(f"  Half-spread:       {args.spread:.2f} ({args.spread*100:.0f} cents)")
    print(f"  Min edge:          {args.min_edge:.2f}")
    print(f"  Max fills/side:    {args.max_inv} per market")
    print(f"  Stop before expiry: {args.stop_before_expiry}s")
    for coin, lf in log_files.items():
        print(f"  Trade log ({coin}):    {lf}")
    print()

    # Kelly info
    print(f"  Kelly Criterion Sizing:")
    print(f"    Bankroll:        ${args.bankroll:.2f}")
    print(f"    Kelly fraction:  {args.kelly:.0%} (fractional Kelly)")
    print(f"    Max per quote:   ${args.max_quote:.2f}")
    print(f"    Min per quote:   $1.00 (Polymarket minimum)")
    print()

    # Risk info
    print(f"  Risk Management:")
    print(f"    Loss limit:      NONE (Kelly is the only protection)")
    print(f"    Stops quoting:   When balance < $1.00")
    print(f"    Auto-redemption: YES (on every market settlement)")
    print()

    if not args.observe:
        print(f"{Colors.YELLOW}  WARNING: This will place REAL limit orders with REAL money.{Colors.RESET}")
        print(f"  Kelly sizes bets dynamically. Max single quote: ${args.max_quote:.2f}")
        print()

    async def run_all():
        await asyncio.gather(*[s.run() for s in strategies])

    try:
        asyncio.run(run_all())
    except KeyboardInterrupt:
        print(f"\n{Colors.YELLOW}Interrupted by user{Colors.RESET}")
    except Exception as e:
        print(f"\n{Colors.RED}Error: {e}{Colors.RESET}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
