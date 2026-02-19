#!/usr/bin/env python3
"""
Precision Sniper v2 Stats — Professional trade analytics.

Tracks: overall WR, WR by volatility/TTE/price/hour/momentum, balance trajectory.
Run: python3 scripts/v2_stats.py [--csv data/v2_trades.csv]
"""
import csv
import sys
from collections import defaultdict
from datetime import datetime

DEFAULT_CSV = "data/v2_trades.csv"
BREAKEVEN_WR = 21.0  # % win rate needed to break even at ~$0.21 entry


def load_trades(filepath):
    trades = []
    with open(filepath) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("outcome") in ("won", "lost"):
                trades.append(row)
    return trades


def wr(wins, total):
    return wins / total * 100 if total else 0.0


def pnl_str(val):
    return f"+${val:.2f}" if val >= 0 else f"-${abs(val):.2f}"


def print_section(title):
    print(f"\n  --- {title} ---")


def bucket_analysis(trades, key_fn, bucket_label, sort_key=None):
    """Generic bucket analysis: group trades by key_fn, print WR per bucket."""
    buckets = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for t in trades:
        bucket = key_fn(t)
        if bucket is None:
            continue
        if t["outcome"] == "won":
            buckets[bucket]["w"] += 1
        else:
            buckets[bucket]["l"] += 1
        buckets[bucket]["pnl"] += float(t.get("pnl", 0))

    keys = sorted(buckets.keys(), key=sort_key) if sort_key else sorted(buckets.keys())
    print(f"  {bucket_label:>20} {'W':>4} {'L':>4} {'TOT':>5} {'WR':>6} {'PnL':>9}")
    for k in keys:
        b = buckets[k]
        tot = b["w"] + b["l"]
        w = wr(b["w"], tot)
        flag = " <<" if w < BREAKEVEN_WR and tot >= 5 else ""
        flag = " **" if w >= 35 and tot >= 5 else flag
        print(f"  {str(k):>20} {b['w']:>4} {b['l']:>4} {tot:>5} {w:>5.0f}% {pnl_str(b['pnl']):>9}{flag}")


def main():
    filepath = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_CSV

    try:
        trades = load_trades(filepath)
    except FileNotFoundError:
        print(f"  No trade file found: {filepath}")
        print("  Bot hasn't placed any trades yet.")
        return

    if not trades:
        print("  No resolved trades yet.")
        return

    wins = sum(1 for t in trades if t["outcome"] == "won")
    losses = len(trades) - wins
    total_pnl = sum(float(t["pnl"]) for t in trades)
    total_wagered = sum(float(t["bet_size_usdc"]) for t in trades)

    # Balance
    first_bal = float(trades[0].get("usdc_balance", 0))
    last_bal = float(trades[-1].get("usdc_balance", 0))

    print("=" * 60)
    print(f"  PRECISION SNIPER v2 — {filepath}")
    print("=" * 60)
    print(f"  Trades: {len(trades)} ({wins}W / {losses}L)")
    print(f"  Win Rate: {wr(wins, len(trades)):.1f}% (breakeven: {BREAKEVEN_WR:.0f}%)")
    print(f"  PnL: {pnl_str(total_pnl)} | Wagered: ${total_wagered:.2f}")
    print(f"  Balance: ${first_bal:.2f} -> ${last_bal:.2f} ({pnl_str(last_bal - first_bal)})")
    print(f"  Period: {trades[0]['timestamp'][:10]} to {trades[-1]['timestamp'][:10]}")

    # Has v2 fields?
    has_v2 = "fair_value_at_entry" in trades[0] and trades[0]["fair_value_at_entry"]

    # --- Volatility breakdown ---
    print_section("WR by Volatility")
    vol_key = "volatility_at_entry" if has_v2 else "volatility_std"

    def vol_bucket(t):
        try:
            v = float(t.get(vol_key, 0))
        except (ValueError, TypeError):
            return None
        if v < 0.30:
            return "< 0.30"
        elif v < 0.40:
            return "0.30-0.40"
        elif v < 0.50:
            return "0.40-0.50"
        elif v < 0.70:
            return "0.50-0.70"
        else:
            return ">= 0.70"

    bucket_analysis(trades, vol_bucket, "Volatility")

    # --- Entry price breakdown ---
    print_section("WR by Entry Price")

    def price_bucket(t):
        p = float(t["entry_price"])
        if p < 0.15:
            return "< $0.15"
        elif p < 0.25:
            return "$0.15-$0.25"
        elif p < 0.40:
            return "$0.25-$0.40"
        elif p < 0.60:
            return "$0.40-$0.60"
        elif p < 0.80:
            return "$0.60-$0.80"
        else:
            return ">= $0.80"

    bucket_analysis(trades, price_bucket, "Entry Price")

    # --- Time-to-expiry breakdown (v2 only) ---
    if has_v2:
        print_section("WR by Time-to-Expiry")

        def tte_bucket(t):
            try:
                tte = float(t.get("time_to_expiry_at_entry", 0))
            except (ValueError, TypeError):
                return None
            if tte < 60:
                return "0-1 min"
            elif tte < 120:
                return "1-2 min"
            elif tte < 180:
                return "2-3 min"
            elif tte < 300:
                return "3-5 min"
            else:
                return "5+ min"

        bucket_analysis(trades, tte_bucket, "Time to Expiry")

    # --- Momentum breakdown (v2 only) ---
    if has_v2:
        print_section("WR by Momentum Strength")

        def mom_bucket(t):
            try:
                m = abs(float(t.get("momentum_at_entry", 0)))
            except (ValueError, TypeError):
                return None
            if m < 0.0005:
                return "< 0.05%"
            elif m < 0.001:
                return "0.05-0.10%"
            elif m < 0.002:
                return "0.10-0.20%"
            else:
                return ">= 0.20%"

        bucket_analysis(trades, mom_bucket, "Momentum")

    # --- Hourly WR ---
    print_section("WR by Hour (UTC)")

    def hour_bucket(t):
        try:
            return f"{int(t['timestamp'][11:13]):02d}:00"
        except (ValueError, IndexError):
            return None

    bucket_analysis(trades, hour_bucket, "Hour", sort_key=lambda x: x)

    # --- Side analysis ---
    print_section("WR by Side")
    bucket_analysis(trades, lambda t: t["side"].upper(), "Side")

    # --- Consecutive analysis ---
    print_section("Streak Analysis")
    max_win = max_loss = cur_win = cur_loss = 0
    for t in trades:
        if t["outcome"] == "won":
            cur_win += 1
            cur_loss = 0
            max_win = max(max_win, cur_win)
        else:
            cur_loss += 1
            cur_win = 0
            max_loss = max(max_loss, cur_loss)

    wr_after_win = sum(1 for i in range(1, len(trades))
                       if trades[i-1]["outcome"] == "won" and trades[i]["outcome"] == "won")
    wins_before = sum(1 for i in range(1, len(trades)) if trades[i-1]["outcome"] == "won")
    wr_after_loss = sum(1 for i in range(1, len(trades))
                        if trades[i-1]["outcome"] == "lost" and trades[i]["outcome"] == "won")
    losses_before = sum(1 for i in range(1, len(trades)) if trades[i-1]["outcome"] == "lost")

    print(f"  Longest win streak:  {max_win}")
    print(f"  Longest loss streak: {max_loss}")
    if wins_before:
        print(f"  WR after a WIN:  {wr(wr_after_win, wins_before):.1f}% (n={wins_before})")
    if losses_before:
        print(f"  WR after a LOSS: {wr(wr_after_loss, losses_before):.1f}% (n={losses_before})")

    # --- Fair value accuracy (v2 only) ---
    if has_v2:
        print_section("Fair Value Accuracy")

        def fv_bucket(t):
            try:
                fv = float(t.get("fair_value_at_entry", 0))
            except (ValueError, TypeError):
                return None
            if fv < 0.30:
                return "FV < 0.30"
            elif fv < 0.50:
                return "FV 0.30-0.50"
            elif fv < 0.70:
                return "FV 0.50-0.70"
            else:
                return "FV >= 0.70"

        bucket_analysis(trades, fv_bucket, "Fair Value")

    print()
    print("=" * 60)
    edge = wr(wins, len(trades)) - BREAKEVEN_WR
    if edge > 0:
        print(f"  EDGE: +{edge:.1f}pp above breakeven. Strategy is profitable.")
    else:
        print(f"  EDGE: {edge:.1f}pp. BELOW BREAKEVEN. Review filters.")
    print("=" * 60)


if __name__ == "__main__":
    main()
