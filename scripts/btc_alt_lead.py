#!/usr/bin/env python3
"""
Cross-correlation check: does BTC momentum lead alt momentum on Polymarket
5m markets? Per upgrade-#7 verification gate — SKIP implementing BTC->alt
signal if no measurable lead.

Data: data/live_v30.collector.csv (~90K rows, 5 days). Columns per
CLAUDE.md: timestamp, market_slug, coin, side, momentum_direction,
is_momentum_side, threshold, entry_price, best_bid, momentum, elapsed.

Method:
  For each (timeframe-aligned) 5-min window, find the FIRST crossing of
  |momentum| >= threshold_T per coin. Compare BTC's first-cross-time to
  alt's first-cross-time. Positive delta = BTC leads. Aggregate per alt.

A measurable lead = median delta > +5 seconds with n >= 30 windows.

Output: per-alt lead stats at multiple momentum thresholds.

Run: python3 scripts/btc_alt_lead.py [path_to_csv]
"""
import csv
import sys
import os
from collections import defaultdict
from datetime import datetime
import numpy as np

PATH = sys.argv[1] if len(sys.argv) > 1 else "data/live_v30.collector.csv"
COINS = ["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
ALTS = ["ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"]
THRESHOLDS = [0.0005, 0.0010, 0.0015]


def slug_window_key(slug: str) -> str:
    """Extract window identifier from market slug. Polymarket slugs end in
    a numeric window id (epoch minute). Strip coin prefix to align per window."""
    parts = slug.rsplit("-", 1)
    return parts[1] if len(parts) == 2 and parts[1].isdigit() else slug


def parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def main():
    if not os.path.exists(PATH):
        print(f"ERROR: {PATH} not found")
        sys.exit(1)

    # first_cross[(window_id, coin, threshold)] = earliest timestamp at which
    # this coin crossed this threshold in this window
    first_cross = {}

    n_rows = 0
    with open(PATH) as f:
        r = csv.DictReader(f)
        for row in r:
            n_rows += 1
            try:
                if row["is_momentum_side"].strip().lower() not in ("true", "1", "yes"):
                    continue
                coin = row["coin"].upper()
                if coin not in COINS:
                    continue
                ts = parse_ts(row["timestamp"])
                if ts == 0:
                    continue
                mom = float(row["momentum"])
                thr = float(row["threshold"])
                # Only care about actual threshold-crossing rows at our gates
                if thr not in THRESHOLDS:
                    continue
                if mom < thr:
                    continue
                wk = slug_window_key(row["market_slug"])
                key = (wk, coin, thr)
                if key not in first_cross or ts < first_cross[key]:
                    first_cross[key] = ts
            except Exception:
                continue

    print(f"Parsed {n_rows:,} rows. {len(first_cross):,} first-cross records.")

    # For each (window, threshold), if BTC crossed, compute delta for each alt.
    for thr in THRESHOLDS:
        print(f"\n===== Threshold {thr} =====")
        windows_with_btc = {wk for (wk, coin, t) in first_cross if coin == "BTC" and t == thr}
        print(f"  Windows where BTC crossed: {len(windows_with_btc):,}")
        for alt in ALTS:
            deltas = []
            for wk in windows_with_btc:
                btc_ts = first_cross.get((wk, "BTC", thr))
                alt_ts = first_cross.get((wk, alt, thr))
                if btc_ts and alt_ts:
                    deltas.append(alt_ts - btc_ts)
            if not deltas:
                print(f"  {alt:5s}: no matched windows")
                continue
            arr = np.array(deltas)
            n = len(arr)
            med = float(np.median(arr))
            p25 = float(np.percentile(arr, 25))
            p75 = float(np.percentile(arr, 75))
            frac_btc_leads = float((arr > 0).mean())
            print(
                f"  {alt:5s}: n={n:4d}  median={med:+7.2f}s  p25={p25:+7.2f}s  "
                f"p75={p75:+7.2f}s  frac_BTC_leads={frac_btc_leads:.2%}"
            )

    print()
    print("Interpretation:")
    print("  median > +5s AND n >= 30 AND frac_BTC_leads > 55% => measurable BTC lead.")
    print("  Otherwise: no actionable lead, SKIP upgrade #7.")


if __name__ == "__main__":
    main()
