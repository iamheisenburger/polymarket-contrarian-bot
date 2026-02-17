#!/usr/bin/env python3
"""
Longshot Stats — Clean, unambiguous analysis of min-size trade data.

Run:  python3 scripts/longshot_stats.py
VPS:  python3 /opt/polymarket-bot/scripts/longshot_stats.py

Reads: data/longshot_trades.csv (min-size trades only)
"""

import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

# Find data file relative to script location
SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_DIR = SCRIPT_DIR.parent
CSV_PATH = PROJECT_DIR / "data" / "longshot_trades.csv"

# ── Baseline ──────────────────────────────────────────────────────────
# All pre-min-size positions settled before trade #1 in this file.
# The account balance was polluted by old losses during trades 1-80.
# As of trade #80 (2026-02-17 ~12:25 UTC), old positions are fully
# settled and balance tracks longshot-only going forward.
#
# Historical note:
#   Trades 1-80:  longshot PnL = +$72.45, but account only grew ~$19
#                 because ~$53 in old pre-min-size losses settled simultaneously.
#   Trades 81+:   balance delta = longshot PnL (clean, no outside interference).
#
BASELINE_TRADE_NUM = 80
BASELINE_BALANCE = 43.17  # Account balance at trade #80
BREAKEVEN_WR = 21.0       # Entry ~$0.21 → 21% breakeven


def load_trades():
    if not CSV_PATH.exists():
        print(f"ERROR: {CSV_PATH} not found")
        sys.exit(1)

    trades = []
    with open(CSV_PATH) as f:
        for row in csv.DictReader(f):
            trades.append(row)
    return trades


def analyze(trades):
    coins = defaultdict(lambda: {"w": 0, "l": 0, "p": 0, "pnl": 0.0, "cost": 0.0})
    total = {"w": 0, "l": 0, "p": 0, "pnl": 0.0, "cost": 0.0}

    for t in trades:
        coin = t["coin"]
        outcome = t["outcome"]
        pnl = float(t["pnl"])
        cost = float(t["bet_size_usdc"])

        coins[coin]["cost"] += cost
        coins[coin]["pnl"] += pnl
        total["cost"] += cost
        total["pnl"] += pnl

        if outcome == "won":
            coins[coin]["w"] += 1
            total["w"] += 1
        elif outcome == "lost":
            coins[coin]["l"] += 1
            total["l"] += 1
        else:
            coins[coin]["p"] += 1
            total["p"] += 1

    return coins, total


def print_report(trades, coins, total):
    n = len(trades)
    resolved = total["w"] + total["l"]
    wr = (total["w"] / resolved * 100) if resolved > 0 else 0
    edge = wr - BREAKEVEN_WR

    print()
    print("=" * 65)
    print("  LONGSHOT STATS — Min-Size Trades Only")
    print("=" * 65)
    print()

    # Per coin table
    header = f"{'COIN':<6} {'TRADES':>6} {'W':>4} {'L':>4} {'WR':>7} {'PnL':>10} {'PROGRESS':>10}"
    print(header)
    print("-" * 65)

    for coin in ["BTC", "ETH", "SOL", "XRP"]:
        c = coins[coin]
        ct = c["w"] + c["l"] + c["p"]
        cwr = (c["w"] / (c["w"] + c["l"]) * 100) if (c["w"] + c["l"]) > 0 else 0
        status = "OK" if cwr > BREAKEVEN_WR else "BELOW"
        print(f"{coin:<6} {ct:>6} {c['w']:>4} {c['l']:>4} {cwr:>6.1f}% ${c['pnl']:>+8.2f}   {ct}/50")

    print("-" * 65)
    print(f"{'TOTAL':<6} {n:>6} {total['w']:>4} {total['l']:>4} {wr:>6.1f}% ${total['pnl']:>+8.2f}   {n}/200")
    print()

    if total["p"] > 0:
        print(f"  WARNING: {total['p']} trades still pending resolution")
        print()

    # Verdict
    print(f"  Breakeven WR needed:  {BREAKEVEN_WR:.0f}%")
    print(f"  Observed WR:          {wr:.1f}%")
    print(f"  Edge:                 {edge:+.1f}pp", end="")
    if edge > 10:
        print("  << STRONG EDGE")
    elif edge > 0:
        print("  << POSITIVE EDGE")
    else:
        print("  << NO EDGE")

    print()
    print(f"  Total wagered:        ${total['cost']:.2f}")
    print(f"  Total returned:       ${total['cost'] + total['pnl']:.2f}")
    print(f"  Longshot PnL:         ${total['pnl']:+.2f}")
    print()

    # Balance tracking (post-baseline only)
    if n > BASELINE_TRADE_NUM:
        post_baseline = trades[BASELINE_TRADE_NUM:]
        post_pnl = sum(float(t["pnl"]) for t in post_baseline)
        expected_bal = BASELINE_BALANCE + post_pnl
        last_usdc = float(trades[-1].get("usdc_balance", 0))

        print("  --- Post-Baseline Balance Check ---")
        print(f"  Baseline (trade #{BASELINE_TRADE_NUM}):  ${BASELINE_BALANCE:.2f}")
        print(f"  PnL since baseline:    ${post_pnl:+.2f}  ({n - BASELINE_TRADE_NUM} trades)")
        print(f"  Expected balance:      ${expected_bal:.2f}")
        if last_usdc > 0:
            print(f"  Last logged USDC:      ${last_usdc:.2f}")
            diff = abs(expected_bal - last_usdc)
            if diff < 2.0:
                print(f"  Match: YES (diff ${diff:.2f})")
            else:
                print(f"  Match: NO (diff ${diff:.2f}) — investigate!")
        print()
    else:
        print(f"  Note: First {BASELINE_TRADE_NUM} trades overlap with old pre-min-size")
        print(f"  position settlements. Balance reconciliation starts after trade #{BASELINE_TRADE_NUM}.")
        print()

    # Time info
    first_ts = trades[0]["timestamp"][:19]
    last_ts = trades[-1]["timestamp"][:19]
    print(f"  First trade: {first_ts}")
    print(f"  Last trade:  {last_ts}")
    print()


def main():
    trades = load_trades()
    coins, total = analyze(trades)
    print_report(trades, coins, total)


if __name__ == "__main__":
    main()
