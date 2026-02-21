#!/usr/bin/env python3
"""
Paper Arena Runner — Run 10 strategies simultaneously on paper.

Usage:
    python apps/run_arena.py --coins BTC ETH SOL XRP --timeframe 5m --bankroll 12.95

    # Specific strategies only
    python apps/run_arena.py --strategies S01_XRP S02_FADE S03_MOM
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

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.bot import TradingBot
from src.config import Config
from strategies.paper_arena import PaperArena
from strategies.signals import ALL_SIGNALS


def main():
    parser = argparse.ArgumentParser(
        description="Paper Arena: 10 strategies, paper trading simultaneously"
    )
    parser.add_argument(
        "--coins", type=str, nargs="+", default=["BTC", "ETH", "SOL", "XRP"],
        help="Coins to trade (default: all 4)",
    )
    parser.add_argument(
        "--timeframe", type=str, default="5m", choices=["5m", "15m"],
        help="Market timeframe (default: 5m)",
    )
    parser.add_argument(
        "--bankroll", type=float, default=12.95,
        help="Virtual bankroll per strategy (default: 12.95)",
    )
    parser.add_argument(
        "--strategies", type=str, nargs="*", default=None,
        help="Specific strategy names to run (default: all 10)",
    )
    parser.add_argument(
        "--observe", action="store_true", default=True,
        help="Paper trading mode (always on for arena)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging",
    )

    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(name)s] %(message)s",
            datefmt="%H:%M:%S",
        )

    coins = [c.upper() for c in args.coins]

    # Check environment
    private_key = os.environ.get("POLY_PRIVATE_KEY")
    safe_address = os.environ.get("POLY_SAFE_ADDRESS")
    if not private_key or not safe_address:
        print("Error: POLY_PRIVATE_KEY and POLY_SAFE_ADDRESS must be set")
        sys.exit(1)

    # Create bot (needed for future live mode, used for balance checks)
    config = Config.from_env()
    bot = TradingBot(config=config, private_key=private_key)
    if not bot.is_initialized():
        print("Error: Failed to initialize bot")
        sys.exit(1)

    # Create arena
    arena = PaperArena(
        bot=bot,
        coins=coins,
        timeframe=args.timeframe,
        bankroll=args.bankroll,
    )

    # Register strategies
    selected = set(args.strategies) if args.strategies else None
    registered = []

    for signal_cls in ALL_SIGNALS:
        signal = signal_cls()
        if selected and signal.name not in selected:
            continue
        arena.register(signal)
        registered.append(signal)

    if not registered:
        print("Error: No strategies registered")
        if selected:
            all_names = [s().name for s in ALL_SIGNALS]
            print(f"Available: {', '.join(all_names)}")
        sys.exit(1)

    # Print config
    coin_str = "/".join(coins)
    print()
    print("=" * 60)
    print("  PAPER ARENA — Strategy Tournament")
    print("=" * 60)
    print()
    print(f"  Coins:         {coin_str} ({len(coins)} coins)")
    print(f"  Timeframe:     {args.timeframe}")
    print(f"  Bankroll:      ${args.bankroll:.2f} per strategy")
    print(f"  Tokens/trade:  5 (min-size)")
    print(f"  Mode:          PAPER (observe only)")
    print()
    print(f"  Strategies ({len(registered)}):")
    for s in registered:
        coin_info = f" [{'/'.join(s.coins)}]" if s.coins != ["BTC", "ETH", "SOL", "XRP"] else ""
        print(f"    {s.name:<15} {s.description}{coin_info}")
    print()
    print(f"  CSV logs:      data/arena_<strategy>.csv")
    print(f"  Settlement:    Gamma API only (every 30s)")
    print()
    print("  Goal: 50 verified trades per strategy → evaluate → winners go live")
    print()

    try:
        asyncio.run(arena.run())
    except KeyboardInterrupt:
        print("\nArena interrupted by user")
    except Exception as e:
        print(f"\nArena error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
