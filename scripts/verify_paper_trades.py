#!/usr/bin/env python3
"""
Verify ALL trade outcomes against actual Gamma API resolution.

The bot's settlement detection had a bug: it used Binance spot as fallback
when Gamma API wasn't ready. Polymarket settles on Chainlink, not Binance.
This script queries the authoritative Gamma API for every trade to produce
the REAL win rates.

Usage (VPS):
    cd /opt/polymarket-bot && source .env
    python scripts/verify_paper_trades.py

Usage (local — needs network access to Gamma API):
    python scripts/verify_paper_trades.py --local
"""
import csv
import os
import sys
import argparse

# Allow running from repo root or VPS
if os.path.exists("/opt/polymarket-bot"):
    sys.path.insert(0, "/opt/polymarket-bot")
else:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.gamma_client import GammaClient


def audit_csv(filepath: str, g: GammaClient, slug_cache: dict) -> dict:
    """Audit a single CSV file against Gamma API outcomes."""
    if not os.path.exists(filepath):
        return None

    with open(filepath) as f:
        reader = csv.DictReader(f)
        trades = list(reader)

    if not trades:
        return None

    results = {
        "file": os.path.basename(filepath),
        "total": len(trades),
        "checked": 0,
        "unknown": 0,
        "wins": 0,
        "losses": 0,
        "mismatches": 0,
        "wagered": 0.0,
        "payout": 0.0,
        "by_fv": {},
        "by_coin": {},
        "by_timeframe": {},
        "mismatch_details": [],
    }

    for t in trades:
        slug = t["market_slug"]
        side = t["side"]
        entry = float(t["entry_price"])
        tokens = float(t["num_tokens"])
        cost = entry * tokens
        fv = float(t.get("fair_value_at_entry", 0))
        coin = t.get("coin", "?")
        tf = t.get("timeframe", "?")
        logged_outcome = t.get("outcome", "")

        # Query Gamma API (cached per slug)
        if slug not in slug_cache:
            m = g.get_market_by_slug(slug)
            if m:
                prices = g.parse_prices(m)
                if prices.get("up", 0) > 0.9:
                    slug_cache[slug] = "up"
                elif prices.get("down", 0) > 0.9:
                    slug_cache[slug] = "down"
                else:
                    slug_cache[slug] = "unknown"
            else:
                slug_cache[slug] = "unknown"

        actual_winner = slug_cache[slug]
        if actual_winner == "unknown":
            results["unknown"] += 1
            continue

        results["checked"] += 1
        actually_won = (side == actual_winner)
        payout = tokens * 1.0 if actually_won else 0.0
        results["wagered"] += cost
        results["payout"] += payout

        if actually_won:
            results["wins"] += 1
        else:
            results["losses"] += 1

        # Check for mismatch with logged outcome
        logged_won = (logged_outcome == "won")
        if actually_won != logged_won and logged_outcome:
            results["mismatches"] += 1
            results["mismatch_details"].append(
                f"  {slug} {coin} {side} logged={logged_outcome} "
                f"actual={'won' if actually_won else 'lost'} fv={fv:.2f} entry={entry:.2f}"
            )

        # FV bucket
        if fv >= 0.90:
            bucket = "0.90+"
        elif fv >= 0.80:
            bucket = "0.80-0.89"
        elif fv >= 0.70:
            bucket = "0.70-0.79"
        elif fv >= 0.60:
            bucket = "0.60-0.69"
        elif fv >= 0.50:
            bucket = "0.50-0.59"
        else:
            bucket = "<0.50"

        bk = results["by_fv"].setdefault(bucket, [0, 0, 0.0, 0.0])
        bk[0 if actually_won else 1] += 1
        bk[2] += cost
        bk[3] += payout

        # Coin breakdown
        ck = results["by_coin"].setdefault(coin, [0, 0, 0.0, 0.0])
        ck[0 if actually_won else 1] += 1
        ck[2] += cost
        ck[3] += payout

        # Timeframe breakdown
        tk = results["by_timeframe"].setdefault(tf, [0, 0, 0.0, 0.0])
        tk[0 if actually_won else 1] += 1
        tk[2] += cost
        tk[3] += payout

    return results


def print_results(r: dict):
    """Print audit results for one CSV."""
    total = r["checked"]
    if total == 0:
        print(f"\n=== {r['file']}: {r['total']} trades, all unresolved ===")
        return

    wr = r["wins"] / total * 100
    pnl = r["payout"] - r["wagered"]
    avg_entry = r["wagered"] / total / 5 if total > 0 else 0

    print(f"\n{'='*70}")
    print(f"  {r['file']}: {r['total']} trades ({r['unknown']} unresolved)")
    print(f"{'='*70}")
    print(f"  ACTUAL: {r['wins']}W/{r['losses']}L ({wr:.1f}% WR)")
    print(f"  Wagered: ${r['wagered']:.2f} | Payout: ${r['payout']:.2f} | PnL: ${pnl:+.2f}")
    print(f"  Avg entry: ${avg_entry:.2f}")
    print(f"  Mismatches (logged vs actual): {r['mismatches']}")

    print(f"\n  BY FAIR VALUE:")
    for bucket in sorted(r["by_fv"].keys()):
        w, l, wag, pay = r["by_fv"][bucket]
        t2 = w + l
        bwr = w / t2 * 100 if t2 > 0 else 0
        print(f"    FV {bucket:>9}: {w:>3}W/{l:<3}L ({bwr:5.1f}% WR) "
              f"wagered=${wag:>7.2f} pnl=${pay-wag:>+7.2f}")

    print(f"\n  BY COIN:")
    for coin in sorted(r["by_coin"].keys()):
        w, l, wag, pay = r["by_coin"][coin]
        t2 = w + l
        cwr = w / t2 * 100 if t2 > 0 else 0
        print(f"    {coin:>4}: {w:>3}W/{l:<3}L ({cwr:5.1f}% WR) pnl=${pay-wag:>+7.2f}")

    if r["by_timeframe"]:
        print(f"\n  BY TIMEFRAME:")
        for tf in sorted(r["by_timeframe"].keys()):
            w, l, wag, pay = r["by_timeframe"][tf]
            t2 = w + l
            twr = w / t2 * 100 if t2 > 0 else 0
            print(f"    {tf:>4}: {w:>3}W/{l:<3}L ({twr:5.1f}% WR) pnl=${pay-wag:>+7.2f}")

    if r["mismatch_details"]:
        print(f"\n  MISMATCHES (first 10):")
        for d in r["mismatch_details"][:10]:
            print(d)


def main():
    parser = argparse.ArgumentParser(description="Audit trades against Gamma API")
    parser.add_argument("--local", action="store_true", help="Run from local repo")
    args = parser.parse_args()

    if args.local:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    else:
        base = "/opt/polymarket-bot"

    csv_files = [
        os.path.join(base, "data", "viper_15m.csv"),
        os.path.join(base, "data", "viper_5m.csv"),
        os.path.join(base, "data", "viper_v4_5m.csv"),
        os.path.join(base, "data", "viper_v4_live.csv"),
    ]

    g = GammaClient()
    slug_cache = {}  # Shared cache across files
    all_results = []

    for filepath in csv_files:
        if not os.path.exists(filepath):
            print(f"Skipping {filepath} (not found)")
            continue
        r = audit_csv(filepath, g, slug_cache)
        if r:
            all_results.append(r)
            print_results(r)

    # Grand total
    if len(all_results) > 1:
        total_w = sum(r["wins"] for r in all_results)
        total_l = sum(r["losses"] for r in all_results)
        total_wag = sum(r["wagered"] for r in all_results)
        total_pay = sum(r["payout"] for r in all_results)
        total_n = total_w + total_l
        total_mm = sum(r["mismatches"] for r in all_results)

        print(f"\n{'='*70}")
        print(f"  GRAND TOTAL: {total_n} verified trades across {len(all_results)} files")
        print(f"{'='*70}")
        if total_n > 0:
            print(f"  ACTUAL: {total_w}W/{total_l}L ({total_w/total_n*100:.1f}% WR)")
            print(f"  Wagered: ${total_wag:.2f} | Payout: ${total_pay:.2f} | PnL: ${total_pay-total_wag:+.2f}")
            print(f"  Total mismatches: {total_mm}")


if __name__ == "__main__":
    main()
