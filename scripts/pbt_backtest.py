#!/usr/bin/env python3
"""
Backtest configs against polybacktest data.

Reads data/pbt_collector.csv (and optionally data/collector_v3.csv for live coins)
and runs the same momentum-snipe backtest logic that the live bot uses, then reports:

  - WR / EV / max losing streak per coin
  - Combined (signal-weighted) results
  - Top configs by $/day with safety constraints
  - Comparison: pbt data vs local collector data for sanity checking

Usage:
    python scripts/pbt_backtest.py
    python scripts/pbt_backtest.py --pbt-only
    python scripts/pbt_backtest.py --local-only
"""

import csv
import argparse
from datetime import datetime
from pathlib import Path
from collections import defaultdict


def load_signals(path: Path, label: str) -> list:
    if not path.exists():
        print(f"  {label}: file not found ({path})")
        return []
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            try:
                r["_mom"] = abs(float(r["momentum"]))
                r["_elapsed"] = float(r["elapsed"])
                r["_price"] = float(r["entry_price"])
                r["_won"] = r["outcome"] == "won"
                r["_is_mom"] = r["is_momentum_side"] == "True"
                r["_hour"] = int(r["timestamp"][11:13])
                r["_date"] = r["timestamp"][:10]
                rows.append(r)
            except (ValueError, KeyError):
                pass
    return rows


def filter_signals(rows, mom_min, el_lo, el_hi, p_lo, p_hi, coins=None, hours=None):
    out = []
    for r in rows:
        if not r["_is_mom"]:
            continue
        if r["_mom"] < mom_min:
            continue
        if not (el_lo <= r["_elapsed"] <= el_hi):
            continue
        if not (p_lo <= r["_price"] <= p_hi):
            continue
        if coins and r["coin"] not in coins:
            continue
        if hours is not None and r["_hour"] not in hours:
            continue
        out.append(r)
    return out


def stats(sigs, label="", show=True):
    if not sigs:
        if show:
            print(f"  {label}: 0 signals")
        return None
    wins = [s for s in sigs if s["_won"]]
    losses = [s for s in sigs if not s["_won"]]
    wr = len(wins) / len(sigs)
    avg_ep = sum(s["_price"] for s in sigs) / len(sigs)
    avg_win = sum(1 - s["_price"] for s in wins) / len(wins) * 5 if wins else 0
    avg_loss = sum(s["_price"] for s in losses) / len(losses) * 5 if losses else 0
    ev = wr * avg_win - (1 - wr) * avg_loss
    pnl = sum((1 - s["_price"]) * 5 if s["_won"] else -s["_price"] * 5 for s in sigs)

    sigs_s = sorted(sigs, key=lambda x: x["timestamp"])
    streak = 0
    max_s = 0
    bal = 0
    peak = 0
    max_dd = 0
    for s in sigs_s:
        if not s["_won"]:
            streak += 1
            max_s = max(max_s, streak)
        else:
            streak = 0
        if s["_won"]:
            bal += (1 - s["_price"]) * 5
        else:
            bal -= s["_price"] * 5
        peak = max(peak, bal)
        max_dd = max(max_dd, peak - bal)

    # signals/day
    if len(sigs_s) >= 2:
        try:
            t1 = datetime.fromisoformat(sigs_s[0]["timestamp"][:19])
            t2 = datetime.fromisoformat(sigs_s[-1]["timestamp"][:19])
            days = (t2 - t1).total_seconds() / 86400
            spd = len(sigs_s) / days if days > 0 else 0
        except Exception:
            spd = 0
    else:
        spd = 0

    fills_day_53 = spd * 0.53
    pnl_day = fills_day_53 * ev

    result = {
        "n": len(sigs),
        "wr": wr,
        "avg_ep": avg_ep,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "ev": ev,
        "pnl_total": pnl,
        "max_streak": max_s,
        "max_dd": max_dd,
        "spd": spd,
        "pnl_day_53fr": pnl_day,
    }

    if show:
        print(f"  {label}: n={len(sigs)} WR={wr:.1%} avgEP=${avg_ep:.2f} "
              f"EV/trade=${ev:+.2f} maxLS={max_s} maxDD=${max_dd:.1f} "
              f"spd={spd:.0f} ${pnl_day:+.1f}/day")
    return result


def per_coin(rows, mom, el_lo, el_hi, p_lo, p_hi, coins, label):
    print(f"\n=== {label} ===")
    print(f"Filter: mom>={mom} el={el_lo}-{el_hi} price=${p_lo}-${p_hi}")
    all_sigs = filter_signals(rows, mom, el_lo, el_hi, p_lo, p_hi, coins=coins)
    stats(all_sigs, "ALL")
    print()
    for c in coins:
        sigs = filter_signals(rows, mom, el_lo, el_hi, p_lo, p_hi, coins={c})
        stats(sigs, f"{c:5s}")


def sweep_top(rows, coins, label, max_dd_limit=15.0, max_streak_limit=4, n_min=30):
    print(f"\n=== {label} ===")
    print(f"Constraints: maxDD<=${max_dd_limit} maxLS<={max_streak_limit} n>={n_min}")
    results = []
    for mom in [0.0005, 0.0007, 0.0010, 0.0012, 0.0015]:
        for el_lo, el_hi in [(60, 270), (90, 270), (120, 270), (150, 270), (180, 270), (180, 240), (240, 270)]:
            for p_lo, p_hi in [(0.30, 0.55), (0.30, 0.65), (0.30, 0.75), (0.30, 0.85), (0.30, 0.92),
                               (0.50, 0.80), (0.60, 0.85), (0.70, 0.92)]:
                sigs = filter_signals(rows, mom, el_lo, el_hi, p_lo, p_hi, coins=coins)
                if len(sigs) < n_min:
                    continue
                s = stats(sigs, "", show=False)
                if s is None:
                    continue
                if s["max_dd"] > max_dd_limit:
                    continue
                if s["max_streak"] > max_streak_limit:
                    continue
                results.append({
                    "label": f"mom>={mom:.4f} el={el_lo}-{el_hi} p={p_lo}-{p_hi}",
                    **s,
                    "mom": mom, "el_lo": el_lo, "el_hi": el_hi, "p_lo": p_lo, "p_hi": p_hi,
                })
    results.sort(key=lambda x: x["pnl_day_53fr"], reverse=True)
    print(f"\n{'Rank':>4} {'$/day':>8} {'WR':>6} {'n':>5} {'EV/trd':>7} {'maxLS':>5} {'maxDD':>6}  Config")
    for i, r in enumerate(results[:12]):
        print(f"  {i+1:2d}  ${r['pnl_day_53fr']:6.1f}  {r['wr']:.1%}  {r['n']:4d}   ${r['ev']:+.2f}    {r['max_streak']:2d}   ${r['max_dd']:5.1f}  {r['label']}")
    return results


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pbt-only", action="store_true")
    p.add_argument("--local-only", action="store_true")
    args = p.parse_args()

    pbt_path = Path("data/pbt_collector.csv")
    local_path = Path("data/collector_v3.csv")

    pbt_rows = [] if args.local_only else load_signals(pbt_path, "PBT")
    local_rows = [] if args.pbt_only else load_signals(local_path, "LOCAL")

    print(f"PBT signals: {len(pbt_rows)} (resolved momentum-side: "
          f"{sum(1 for r in pbt_rows if r['_is_mom'])})")
    print(f"LOCAL signals: {len(local_rows)} (resolved momentum-side: "
          f"{sum(1 for r in local_rows if r['_is_mom'])})")

    pbt_coins = sorted(set(r["coin"] for r in pbt_rows))
    local_coins = sorted(set(r["coin"] for r in local_rows))
    print(f"PBT coins: {pbt_coins}")
    print(f"LOCAL coins: {local_coins}")

    if pbt_rows:
        # Backtest the current V29.1 config on each PBT coin
        per_coin(pbt_rows, 0.001, 180, 270, 0.30, 0.70, pbt_coins,
                 "V29.1 FORTRESS on POLYBACKTEST data")
        per_coin(pbt_rows, 0.0007, 90, 270, 0.30, 0.92, pbt_coins,
                 "V29.1 GROWTH on POLYBACKTEST data")
        sweep_top(pbt_rows, pbt_coins, "TOP CONFIGS — POLYBACKTEST DATA (BTC/ETH/SOL)")

    if local_rows:
        all_local_coins = {"BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"}
        live_coins = {"BTC", "SOL", "XRP", "DOGE", "BNB", "HYPE"}
        per_coin(local_rows, 0.001, 180, 270, 0.30, 0.70, live_coins,
                 "V29.1 FORTRESS on LOCAL collector data")
        per_coin(local_rows, 0.0007, 90, 270, 0.30, 0.92, live_coins,
                 "V29.1 GROWTH on LOCAL collector data")
        sweep_top(local_rows, live_coins, "TOP CONFIGS — LOCAL DATA (live coins)")

    # Cross-validation: do BTC results in PBT match BTC results in local?
    if pbt_rows and local_rows:
        print("\n" + "=" * 70)
        print("CROSS-VALIDATION: BTC PBT vs BTC LOCAL")
        print("=" * 70)
        for label_cfg, mom, el_lo, el_hi, p_lo, p_hi in [
            ("FORTRESS", 0.001, 180, 270, 0.30, 0.70),
            ("GROWTH", 0.0007, 90, 270, 0.30, 0.92),
        ]:
            print(f"\n{label_cfg}:")
            pbt_btc = filter_signals(pbt_rows, mom, el_lo, el_hi, p_lo, p_hi, coins={"BTC"})
            local_btc = filter_signals(local_rows, mom, el_lo, el_hi, p_lo, p_hi, coins={"BTC"})
            stats(pbt_btc, "  PBT BTC  ")
            stats(local_btc, "  LOCAL BTC")


if __name__ == "__main__":
    main()
