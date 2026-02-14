#!/usr/bin/env python3
"""
Contrarian Cheap-Side Strategy Runner

Entry point for running the contrarian strategy on Polymarket
short-duration crypto binary markets.

Usage:
    # Live mode - BTC 5-minute markets, $1 bets
    python apps/run_contrarian.py --coin BTC --timeframe 5m --size 1.0

    # Observe-only mode (no real trades)
    python apps/run_contrarian.py --coin BTC --observe

    # Custom entry range
    python apps/run_contrarian.py --coin ETH --min-price 0.03 --max-price 0.05

    # 15-minute markets
    python apps/run_contrarian.py --coin BTC --timeframe 15m
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

# Auto-load .env file
from dotenv import load_dotenv
load_dotenv()

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.console import Colors
from src.bot import TradingBot
from src.config import Config
from strategies.contrarian import ContrarianStrategy, ContrarianConfig


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Contrarian Cheap-Side Strategy for Polymarket crypto markets"
    )
    parser.add_argument(
        "--coin",
        type=str,
        default="BTC",
        choices=["BTC", "ETH", "SOL", "XRP"],
        help="Coin to trade (default: BTC)"
    )
    parser.add_argument(
        "--timeframe",
        type=str,
        default="5m",
        choices=["5m", "15m"],
        help="Market timeframe (default: 5m)"
    )
    parser.add_argument(
        "--size",
        type=float,
        default=1.0,
        help="Bet size in USDC per trade (default: 1.0)"
    )
    parser.add_argument(
        "--min-price",
        type=float,
        default=0.03,
        help="Minimum entry price (default: 0.03)"
    )
    parser.add_argument(
        "--max-price",
        type=float,
        default=0.07,
        help="Maximum entry price (default: 0.07)"
    )
    parser.add_argument(
        "--max-trades-per-hour",
        type=int,
        default=10,
        help="Maximum trades per hour (default: 10)"
    )
    parser.add_argument(
        "--daily-loss-limit",
        type=float,
        default=10.0,
        help="Stop trading after losing this much (default: 10.0)"
    )
    parser.add_argument(
        "--min-volatility",
        type=float,
        default=0.0,
        help="Minimum volatility std dev to trade (0=disabled, default: 0)"
    )
    parser.add_argument(
        "--bankroll",
        type=float,
        default=10.0,
        help="Starting bankroll in USDC (default: 10.0)"
    )
    parser.add_argument(
        "--kelly",
        type=float,
        default=0.25,
        help="Kelly fraction (0.25=quarter Kelly, 0.5=half, 1.0=full) (default: 0.25)"
    )
    parser.add_argument(
        "--win-rate",
        type=float,
        default=0.088,
        help="Estimated win rate for Kelly sizing (default: 0.088)"
    )
    parser.add_argument(
        "--observe",
        action="store_true",
        help="Observe-only mode - detect opportunities but don't trade"
    )
    parser.add_argument(
        "--log-file",
        type=str,
        default="data/trades.csv",
        help="Trade log CSV file path (default: data/trades.csv)"
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    # Enable debug logging if requested
    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
        logging.getLogger("src.websocket_client").setLevel(logging.DEBUG)

    # Check environment
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")

    if not private_key or not safe_address:
        print(f"{Colors.RED}Error: POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS must be set{Colors.RESET}")
        print("Set them in .env file or export as environment variables")
        print()
        print("Required environment variables:")
        print("  POLY_PRIVATE_KEY    - Your Ethereum private key (64 hex chars)")
        print("  POLY_SAFE_ADDRESS   - Your Polymarket Safe wallet address")
        sys.exit(1)

    # Create bot
    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)

    if not bot.is_initialized():
        print(f"{Colors.RED}Error: Failed to initialize bot{Colors.RESET}")
        sys.exit(1)

    # Adjust market check interval based on timeframe
    market_check_interval = 15.0 if args.timeframe == "5m" else 30.0

    # Create strategy config
    strategy_config = ContrarianConfig(
        coin=args.coin.upper(),
        timeframe=args.timeframe,
        bet_size=args.size,
        min_entry_price=args.min_price,
        max_entry_price=args.max_price,
        max_trades_per_hour=args.max_trades_per_hour,
        daily_loss_limit=args.daily_loss_limit,
        min_volatility=args.min_volatility,
        observe_only=args.observe,
        log_file=args.log_file,
        market_check_interval=market_check_interval,
        size=args.size,  # base class size
        kelly_fraction=args.kelly,
        estimated_win_rate=args.win_rate,
        starting_bankroll=args.bankroll,
    )

    # Print configuration
    mode_str = f"{Colors.YELLOW}OBSERVE ONLY{Colors.RESET}" if args.observe else f"{Colors.GREEN}LIVE TRADING{Colors.RESET}"

    print(f"\n{Colors.BOLD}{'='*60}{Colors.RESET}")
    print(f"{Colors.BOLD}  Contrarian Strategy - {strategy_config.coin} {strategy_config.timeframe} Markets{Colors.RESET}")
    print(f"{Colors.BOLD}{'='*60}{Colors.RESET}\n")

    print(f"  Mode:            {mode_str}")
    print(f"  Coin:            {strategy_config.coin}")
    print(f"  Timeframe:       {strategy_config.timeframe}")
    print(f"  Max bet size:    ${strategy_config.bet_size:.2f} per trade")
    print(f"  Entry range:     ${strategy_config.min_entry_price:.2f} - ${strategy_config.max_entry_price:.2f}")
    print(f"  Max trades/hr:   {strategy_config.max_trades_per_hour}")
    print(f"  Daily loss limit: ${strategy_config.daily_loss_limit:.2f}")
    print(f"  Trade log:       {strategy_config.log_file}")
    print()

    # Kelly Criterion info
    print(f"  Kelly Criterion Sizing:")
    print(f"    Bankroll:      ${strategy_config.starting_bankroll:.2f}")
    print(f"    Kelly fraction: {strategy_config.kelly_fraction:.0%} (fractional Kelly)")
    print(f"    Est. win rate: {strategy_config.estimated_win_rate:.1%}")
    print(f"    Max per trade: {strategy_config.max_bet_fraction:.0%} of bankroll")
    avg_entry = (strategy_config.min_entry_price + strategy_config.max_entry_price) / 2
    kelly_f = strategy_config.kelly_fraction * (strategy_config.estimated_win_rate - avg_entry) / (1 - avg_entry)
    sample_bet = kelly_f * strategy_config.starting_bankroll
    print(f"    Sample bet:    ${max(sample_bet, 0):.2f} at ${avg_entry:.2f} entry")
    print()

    if not args.observe:
        print(f"{Colors.YELLOW}  WARNING: This will place REAL trades with REAL money.{Colors.RESET}")
        print(f"  Kelly will size bets dynamically based on bankroll + edge")
        print(f"  Max risk per trade: ${min(strategy_config.bet_size, strategy_config.max_bet_fraction * strategy_config.starting_bankroll):.2f}")
        print(f"  Max daily risk: ${strategy_config.daily_loss_limit:.2f}")
        print()

    # Payout math
    payout_mult = 1.0 / avg_entry
    breakeven_wr = avg_entry * 100

    print(f"  Strategy math (at avg entry ${avg_entry:.2f}):")
    print(f"    Payout per win: {payout_mult:.0f}x")
    print(f"    Breakeven win rate: {breakeven_wr:.1f}%")
    print(f"    Expected win rate (data): ~8-15%")
    print()

    # Create and run strategy
    strategy = ContrarianStrategy(bot=bot, config=strategy_config)

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
