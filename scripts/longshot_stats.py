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
# Trade log was reset on 2026-02-17 after discovering phantom trades
# (GTC orders logged as filled but never actually matched). Bot now
# uses FOK (Fill Or Kill) orders — fills instantly or is cancelled.
#
# This file starts clean. Every trade is a verified fill.
# Baseline = trade #0, balance at reset time.
#
BASELINE_TRADE_NUM = 0
BASELINE_BALANCE = 0.0  # Set automatically from first trade's usdc_balance
BREAKEVEN_WR = 21.0     # Entry ~$0.21 → 21% breakeven


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

    all_coins = sorted(set(t["coin"] for t in trades))
    for coin in all_coins:
        c = coins[coin]
        ct = c["w"] + c["l"] + c["p"]
        cwr = (c["w"] / (c["w"] + c["l"]) * 100) if (c["w"] + c["l"]) > 0 else 0
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

    # Balance integrity — verify bankroll matches on-chain USDC
    # This checks that the bot's internal state matches reality.
    # A gap means phantom positions or untracked balance changes.
    if n >= 1:
        last_bankroll = float(trades[-1].get("bankroll", 0))
        last_usdc = float(trades[-1].get("usdc_balance", 0))

        print("  --- Balance Integrity Check ---")
        print(f"  Last bankroll:         ${last_bankroll:.2f}")
        print(f"  Last USDC (on-chain):  ${last_usdc:.2f}")

        if last_usdc > 0 and last_bankroll > 0:
            gap = abs(last_bankroll - last_usdc)
            if gap < 3.0:
                print(f"  Match: YES (gap ${gap:.2f})")
            else:
                print(f"  Match: NO (gap ${gap:.2f}) — investigate!")
        else:
            print(f"  (Insufficient data for balance check)")
        print()

    # Time info
    first_ts = trades[0]["timestamp"][:19]
    last_ts = trades[-1]["timestamp"][:19]
    print(f"  First trade: {first_ts}")
    print(f"  Last trade:  {last_ts}")
    print()

    # Per-hour WR breakdown
    hour_stats = defaultdict(lambda: {"w": 0, "l": 0})
    for t in trades:
        h = int(t["timestamp"][11:13])
        if t["outcome"] == "won":
            hour_stats[h]["w"] += 1
        elif t["outcome"] == "lost":
            hour_stats[h]["l"] += 1

    print("  --- Per-Hour WR (UTC) ---")
    print(f"  {'HR':>4} {'W':>3} {'L':>3} {'TOT':>4} {'WR':>6}")
    for h in sorted(hour_stats):
        w = hour_stats[h]["w"]
        l = hour_stats[h]["l"]
        tot = w + l
        wr_h = w / tot * 100 if tot else 0
        flag = " <<" if wr_h < 15 and tot >= 5 else ""
        print(f"  {h:>4} {w:>3} {l:>3} {tot:>4} {wr_h:>5.0f}%{flag}")
    print()


def main():
    trades = load_trades()
    coins, total = analyze(trades)
    print_report(trades, coins, total)


if __name__ == "__main__":
    main()
