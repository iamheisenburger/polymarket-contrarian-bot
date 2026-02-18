#!/usr/bin/env python3
"""Audit all ETH trades for token count anomalies and duplicate entries."""
import csv
from collections import Counter
from pathlib import Path

CSV = Path(__file__).resolve().parent.parent / "data" / "longshot_trades.csv"
trades = list(csv.DictReader(open(CSV)))

print("=== ALL ETH TRADES ===")
print(f"{'#':>3} {'TIMESTAMP':>19} {'SLUG':>35} {'SIDE':<5} {'TOK':>5} {'COST':>6} {'OUT':<5} {'PnL':>7}")
print("-" * 95)
eth_count = 0
for t in trades:
    if t["coin"] == "ETH":
        eth_count += 1
        tokens = float(t["num_tokens"])
        cost = float(t["bet_size_usdc"])
        pnl = float(t["pnl"])
        flag = " <<< ANOMALY" if tokens != 5.0 else ""
        print(f"{eth_count:>3} {t['timestamp'][:19]} {t['market_slug']:>35} {t['side']:<5} {tokens:>5.1f} ${cost:>5.2f} {t['outcome']:<5} ${pnl:>+6.2f}{flag}")

print()

# Check ALL coins for duplicates
c = Counter((t["market_slug"], t["side"]) for t in trades)
dupes = [(k, v) for k, v in c.items() if v > 1]
if dupes:
    print("=== DUPLICATES FOUND ===")
    for (slug, side), count in dupes:
        print(f"  {slug} {side} x{count}")
        for t in trades:
            if t["market_slug"] == slug and t["side"] == side:
                print(f"    {t['timestamp'][:19]} tokens={t['num_tokens']} cost={t['bet_size_usdc']}")
else:
    print("No duplicate market+side entries in entire log.")

# Also check: any trade with tokens != 5.0 across ALL coins
print()
anomalies = [t for t in trades if float(t["num_tokens"]) != 5.0]
if anomalies:
    print(f"=== TOKEN COUNT ANOMALIES ({len(anomalies)}) ===")
    for t in anomalies:
        print(f"  {t['timestamp'][:19]} {t['coin']} {t['side']} tokens={t['num_tokens']}")
else:
    print("All trades are exactly 5.0 tokens. No anomalies.")

print(f"\nTotal ETH: {eth_count} | Total all: {len(trades)}")
