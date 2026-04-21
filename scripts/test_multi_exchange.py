#!/usr/bin/env python3
"""Smoke test for Kraken + Bybit-Spot feeds — 15s run, print prices + ages.

Usage:  cd /opt/polymarket-bot && source venv/bin/activate && python3 scripts/test_multi_exchange.py
"""
import asyncio
import os
import sys
import time

# Allow running from scripts/ directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def main():
    from lib.kraken_ws import KrakenPriceFeed
    from lib.bybit_spot_ws import BybitSpotPriceFeed

    coins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB"]
    kr = KrakenPriceFeed(coins=coins)
    by = BybitSpotPriceFeed(coins=coins)

    print(f"Starting Kraken (coins={kr.coins}) + Bybit (coins={by.coins}) ...")
    await asyncio.gather(kr.start(), by.start(), return_exceptions=True)
    print(f"Kraken connected: {kr.connected}, Bybit connected: {by.connected}")

    await asyncio.sleep(10)

    print(f"\n{'coin':6s} {'Kraken':>12s} {'age_s':>6s} | {'Bybit':>12s} {'age_s':>6s}")
    print("-" * 60)
    for c in coins:
        kp = kr.get_price(c) if c in kr.coins else 0
        ka = kr.get_age(c) if c in kr.coins else float("inf")
        bp = by.get_price(c) if c in by.coins else 0
        ba = by.get_age(c) if c in by.coins else float("inf")
        ka_s = f"{ka:.2f}" if ka < 1000 else "n/a"
        ba_s = f"{ba:.2f}" if ba < 1000 else "n/a"
        print(f"{c:6s} {kp:>12.4f} {ka_s:>6s} | {bp:>12.4f} {ba_s:>6s}")

    await asyncio.gather(kr.stop(), by.stop(), return_exceptions=True)
    # Verdict
    kr_ok = sum(1 for c in kr.coins if kr.get_price(c) > 0)
    by_ok = sum(1 for c in by.coins if by.get_price(c) > 0)
    print(f"\nKraken got prices for {kr_ok}/{len(kr.coins)} coins")
    print(f"Bybit  got prices for {by_ok}/{len(by.coins)} coins")
    ok = kr_ok >= 3 and by_ok >= 3
    print("RESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    asyncio.run(main())
