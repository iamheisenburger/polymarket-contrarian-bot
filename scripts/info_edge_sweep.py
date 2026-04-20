#!/usr/bin/env python3
"""
Informational-edge validation pass on IC collector data.

For each candidate filter/signal, compute WR on subset where it applies
vs WR on full config-matched pool. Only promote filters showing
>= 5pp WR lift AND preserving >= 30% of signal volume.

Baseline pool: T-config-matched signals (mom >= 0.002, elapsed in [150,270],
entry in [0.05, 0.70], is_momentum_side=True, threshold >= 0.002).

Signals tested:
  1. Time-of-day (UTC hour buckets)
  2. Day-of-week
  3. Multi-timeframe alignment (early threshold cross precedes late one)
  4. Momentum magnitude (beyond threshold)
  5. Spread tightness at signal time (entry_price - best_bid)
  6. Cross-coin confirmation (BTC moving same direction at fire time)
  7. Entry-price bins (robustness check)
  8. Late-window bin (165-270 vs 150-165)

Also reports signals requiring external data / telemetry-first.
"""
import csv
import os
import sys
from datetime import datetime, timezone
from collections import defaultdict

import numpy as np

CSV_PATH = sys.argv[1] if len(sys.argv) > 1 else "data/live_v30.collector.csv"

if not os.path.exists(CSV_PATH):
    print(f"ERROR: {CSV_PATH} not found")
    sys.exit(1)


def parse_ts(s: str) -> float:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def slug_window_id(slug: str) -> str:
    parts = slug.rsplit("-", 1)
    return parts[1] if len(parts) == 2 and parts[1].isdigit() else slug


# ---------- Load ----------
rows_raw = []
with open(CSV_PATH) as f:
    r = csv.DictReader(f)
    for row in r:
        try:
            if row["is_momentum_side"].strip().lower() not in ("true", "1", "yes"):
                continue
            outcome = row["outcome"].strip().lower()
            if outcome not in ("won", "lost"):
                continue
            ep = float(row["entry_price"])
            bb = float(row["best_bid"])
            mom = float(row["momentum"])
            el = float(row["elapsed"])
            thr = float(row["threshold"])
            if ep < 0.05 or ep >= 1.0:
                continue
            ts = parse_ts(row["timestamp"])
            rows_raw.append({
                "ts": ts,
                "slug": row["market_slug"],
                "coin": row["coin"].upper(),
                "side": row["side"],
                "thr": thr,
                "mom": mom,
                "el": el,
                "ep": ep,
                "bb": bb,
                "won": 1 if outcome == "won" else 0,
                "window_id": slug_window_id(row["market_slug"]),
            })
        except Exception:
            continue

print(f"Loaded {len(rows_raw):,} resolved momentum-side IC rows")

# Keep only rows at threshold == 0.002 (our fire threshold).
# Each such row is the moment a (window, coin, side) signal would have qualified.
# Dedupe: earliest 0.002-threshold row per (window, coin, side) — that's when we'd have fired.
pool = [r for r in rows_raw if abs(r["thr"] - 0.002) < 1e-6]
dedup = {}
for r in pool:
    key = (r["window_id"], r["coin"], r["side"])
    if key not in dedup or r["ts"] < dedup[key]["ts"]:
        dedup[key] = r
rows_ded = list(dedup.values())
print(f"Pool of 0.002-threshold events, deduped: {len(rows_ded):,} unique (window, coin, side)")

# Build numpy arrays for fast filtering
def arr(key):
    return np.array([r[key] for r in rows_ded])

ts = arr("ts")
coin = np.array([r["coin"] for r in rows_ded])
side = np.array([r["side"] for r in rows_ded])
thr = arr("thr").astype(float)
mom = arr("mom").astype(float)
el = arr("el").astype(float)
ep = arr("ep").astype(float)
bb = arr("bb").astype(float)
won = arr("won").astype(int)
window_id = np.array([r["window_id"] for r in rows_ded])

# ---------- Baseline pool ----------
# Match the deployed T-config: mom >= 0.002, el in [150,270], ep in [0.05,0.70]
base_mask = (thr >= 0.002) & (mom >= 0.002) & (el >= 150) & (el <= 270) & (ep >= 0.05) & (ep <= 0.70)
n_base = int(base_mask.sum())
wr_base = float(won[base_mask].mean()) if n_base else 0.0
buy_base = np.minimum(ep[base_mask] + 0.07, 0.70)
ev_base = float((np.where(won[base_mask] == 1, 1.0 - buy_base, -buy_base)).mean()) if n_base else 0.0
print()
print(f"=== BASELINE: cap=0.70 mom=0.002 el=[150,270] ===")
print(f"  n = {n_base:,}   WR = {wr_base:.2%}   avg_buy = {buy_base.mean():.3f}   EV = {ev_base:+.4f}")
print()


def report(title, filter_mask_name: str, include_mask: np.ndarray):
    """Compare WR/EV on include_mask vs its complement within the baseline pool."""
    inc = base_mask & include_mask
    exc = base_mask & (~include_mask)
    if inc.sum() < 20 or exc.sum() < 20:
        print(f"  [{filter_mask_name}] insufficient data (n_in={inc.sum()}, n_out={exc.sum()})")
        return None
    wr_in = float(won[inc].mean())
    wr_ex = float(won[exc].mean())
    buy_in = np.minimum(ep[inc] + 0.07, 0.70)
    buy_ex = np.minimum(ep[exc] + 0.07, 0.70)
    ev_in = float((np.where(won[inc] == 1, 1.0 - buy_in, -buy_in)).mean())
    ev_ex = float((np.where(won[exc] == 1, 1.0 - buy_ex, -buy_ex)).mean())
    delta_wr = (wr_in - wr_ex) * 100
    delta_ev = ev_in - ev_ex
    verdict = "KEEP" if (delta_wr >= 5 and inc.sum() >= 0.3 * n_base) else \
              "MIXED" if delta_wr >= 3 else "DROP"
    print(f"  [{filter_mask_name}]  n_in={inc.sum():5d}  WR_in={wr_in:.2%}  EV_in={ev_in:+.4f}  "
          f"|  n_out={exc.sum():5d}  WR_out={wr_ex:.2%}  "
          f"|  ΔWR={delta_wr:+.2f}pp ΔEV={delta_ev:+.4f}  → {verdict}")
    return (wr_in, wr_ex, delta_wr, delta_ev, int(inc.sum()), int(exc.sum()), verdict)


# ---------- Signal 1: Time-of-day (UTC hour) ----------
print("=== Signal 1: Time-of-day (UTC hour buckets) ===")
hours = np.array([datetime.fromtimestamp(t, tz=timezone.utc).hour for t in ts])
print("  Hour    n_in    WR_in    EV_in   ΔWR_vs_base")
for h_group in [(0, 6), (6, 12), (12, 18), (18, 24)]:
    m = (hours >= h_group[0]) & (hours < h_group[1])
    inc = base_mask & m
    if inc.sum() < 30:
        print(f"  {h_group[0]:02d}-{h_group[1]:02d}: n={inc.sum()} insufficient")
        continue
    wr = float(won[inc].mean())
    buy = np.minimum(ep[inc] + 0.07, 0.70)
    ev = float((np.where(won[inc] == 1, 1.0 - buy, -buy)).mean())
    print(f"  {h_group[0]:02d}-{h_group[1]:02d}:  n={inc.sum():5d}  WR={wr:.2%}   EV={ev:+.4f}   ΔWR={(wr-wr_base)*100:+.2f}pp")
print()

# Per-hour fine breakdown
print("  Per-hour (base-pool only, >=30 samples):")
for h in range(24):
    m = hours == h
    inc = base_mask & m
    if inc.sum() < 30: continue
    wr = float(won[inc].mean())
    print(f"    h={h:02d}  n={inc.sum():4d}  WR={wr:.2%}  ΔWR={(wr-wr_base)*100:+.2f}pp")
print()

# ---------- Signal 2: Day-of-week ----------
print("=== Signal 2: Day-of-week (UTC) ===")
dows = np.array([datetime.fromtimestamp(t, tz=timezone.utc).weekday() for t in ts])
names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
for d in range(7):
    m = dows == d
    inc = base_mask & m
    if inc.sum() < 20: continue
    wr = float(won[inc].mean())
    print(f"  {names[d]}:  n={inc.sum():4d}  WR={wr:.2%}   ΔWR={(wr-wr_base)*100:+.2f}pp")
print()

# ---------- Signal 3: Momentum magnitude beyond threshold ----------
print("=== Signal 3: Momentum magnitude (mom/thr ratio) ===")
# For base_mask rows, bucket by mom/thr
ratio = np.where(thr > 0, mom / np.maximum(thr, 0.0001), 1.0)
for lo, hi in [(1.0, 1.2), (1.2, 1.5), (1.5, 2.0), (2.0, 3.0), (3.0, 10.0)]:
    m = (ratio >= lo) & (ratio < hi)
    inc = base_mask & m
    if inc.sum() < 30: continue
    wr = float(won[inc].mean())
    buy = np.minimum(ep[inc] + 0.07, 0.70)
    ev = float((np.where(won[inc] == 1, 1.0 - buy, -buy)).mean())
    print(f"  mom/thr in [{lo:.1f},{hi:.1f}):  n={inc.sum():5d}  WR={wr:.2%}  EV={ev:+.4f}  ΔWR={(wr-wr_base)*100:+.2f}pp")
print()

# ---------- Signal 4: Spread tightness ----------
print("=== Signal 4: Spread tightness (entry - bid) at signal time ===")
spread = ep - bb
for lo, hi in [(-0.01, 0.01), (0.01, 0.02), (0.02, 0.03), (0.03, 0.05), (0.05, 1.0)]:
    m = (spread >= lo) & (spread < hi)
    inc = base_mask & m
    if inc.sum() < 30: continue
    wr = float(won[inc].mean())
    print(f"  spread in [{lo:.2f},{hi:.2f}):  n={inc.sum():5d}  WR={wr:.2%}  ΔWR={(wr-wr_base)*100:+.2f}pp")
print()

# ---------- Signal 5: Late-window binning (TTE) ----------
print("=== Signal 5: Elapsed window bins ===")
for lo, hi in [(150, 180), (180, 210), (210, 240), (240, 270)]:
    m = (el >= lo) & (el < hi)
    inc = base_mask & m
    if inc.sum() < 30: continue
    wr = float(won[inc].mean())
    buy = np.minimum(ep[inc] + 0.07, 0.70)
    ev = float((np.where(won[inc] == 1, 1.0 - buy, -buy)).mean())
    print(f"  elapsed [{lo},{hi}):  n={inc.sum():5d}  WR={wr:.2%}  EV={ev:+.4f}  ΔWR={(wr-wr_base)*100:+.2f}pp")
print()

# ---------- Signal 6: Multi-timeframe alignment ----------
# For each (window, coin, side) in base pool, check if there was ALSO a threshold crossing
# at 0.0005 earlier in the same window. If so, momentum has been building (aligned).
print("=== Signal 6: Multi-timeframe alignment (earlier lower-threshold cross?) ===")
# Build earlier-cross lookup
earlier_cross = defaultdict(float)  # (wid, coin, side) -> earliest_ts at thr=0.0005
for r in rows_raw:
    if r["thr"] == 0.0005:
        k = (r["window_id"], r["coin"], r["side"])
        if k not in earlier_cross or r["ts"] < earlier_cross[k]:
            earlier_cross[k] = r["ts"]

# Mask: does the baseline row have a 0.0005 cross >= 5s before its own ts?
ml_keys = [(window_id[i], coin[i], side[i]) for i in range(len(rows_ded))]
ml_mask = np.array([
    (k in earlier_cross and earlier_cross[k] + 5.0 <= rows_ded[i]["ts"])
    for i, k in enumerate(ml_keys)
])
report("Multi-TF aligned (0.0005 cross preceded by >=5s)", "multi_tf_aligned", ml_mask)
print()

# ---------- Signal 7: Cross-coin confirmation (BTC moving same dir?) ----------
print("=== Signal 7: Cross-coin BTC confirmation at signal time ===")
# For each row, check if BTC had same-direction momentum >= 0.0005 within +/- 10s
# Group BTC crosses by ts
btc_up_cross = []
btc_down_cross = []
for r in rows_raw:
    if r["coin"] == "BTC" and r["thr"] >= 0.0005:
        if r["side"] == "up":
            btc_up_cross.append(r["ts"])
        else:
            btc_down_cross.append(r["ts"])
btc_up_cross = np.array(sorted(btc_up_cross))
btc_down_cross = np.array(sorted(btc_down_cross))

def _btc_confirmed(row_ts: float, row_side: str) -> bool:
    arr_ = btc_up_cross if row_side == "up" else btc_down_cross
    if len(arr_) == 0:
        return False
    idx = np.searchsorted(arr_, row_ts)
    # check neighbors within 20s
    for i in (idx - 1, idx):
        if 0 <= i < len(arr_) and abs(arr_[i] - row_ts) <= 20.0:
            return True
    return False

btc_mask = np.array([
    _btc_confirmed(rows_ded[i]["ts"], side[i]) for i in range(len(rows_ded))
])
report("BTC same-side momentum within +/-20s", "btc_confirmed", btc_mask)
# But EXCLUDE rows where coin==BTC itself (trivially true)
nonbtc_mask = coin != "BTC"
inc = base_mask & btc_mask & nonbtc_mask
exc = base_mask & (~btc_mask) & nonbtc_mask
if inc.sum() >= 30 and exc.sum() >= 30:
    wr_in = float(won[inc].mean())
    wr_ex = float(won[exc].mean())
    print(f"  (altcoins only) BTC-confirmed:  n={inc.sum()}  WR={wr_in:.2%}  vs not:  n={exc.sum()}  WR={wr_ex:.2%}  ΔWR={(wr_in-wr_ex)*100:+.2f}pp")
print()

# ---------- Signal 8: Alt-leads-BTC filter ----------
# From prior analysis (project_btc_doesnt_lead_alts.md), ETH/SOL/XRP/DOGE lead BTC by 8-20s.
# If *alt* already crossed 0.0005, and we see BTC 0.002+ fire, does WR differ?
print("=== Signal 8: Alt-preceded-BTC (BTC trades only) ===")
# For each BTC row in base pool, check if ANY alt had same-side 0.0005 cross in prior 30s
alt_up_cross = defaultdict(list)
alt_down_cross = defaultdict(list)
for r in rows_raw:
    if r["coin"] in ("ETH", "SOL", "XRP", "DOGE") and r["thr"] >= 0.0005:
        key = r["side"]
        (alt_up_cross if key == "up" else alt_down_cross)[r["coin"]].append(r["ts"])
for c in alt_up_cross:
    alt_up_cross[c] = np.array(sorted(alt_up_cross[c]))
for c in alt_down_cross:
    alt_down_cross[c] = np.array(sorted(alt_down_cross[c]))

def _alt_preceded(row_ts: float, row_side: str) -> bool:
    pool = alt_up_cross if row_side == "up" else alt_down_cross
    for c, arr_ in pool.items():
        idx = np.searchsorted(arr_, row_ts)
        # Check ANY alt cross in [row_ts - 30, row_ts)
        for i in (idx - 1, idx - 2):
            if 0 <= i < len(arr_) and 0 < (row_ts - arr_[i]) <= 30.0:
                return True
    return False

btc_only = coin == "BTC"
alt_pre_mask = np.array([
    _alt_preceded(rows_ded[i]["ts"], side[i]) if btc_only[i] else False
    for i in range(len(rows_ded))
])
inc = base_mask & btc_only & alt_pre_mask
exc = base_mask & btc_only & (~alt_pre_mask)
if inc.sum() >= 20 and exc.sum() >= 20:
    wr_in = float(won[inc].mean())
    wr_ex = float(won[exc].mean())
    print(f"  BTC with alt leading in prior 30s:  n={inc.sum()}  WR={wr_in:.2%}  vs not:  n={exc.sum()}  WR={wr_ex:.2%}  ΔWR={(wr_in-wr_ex)*100:+.2f}pp")
else:
    print(f"  insufficient data (n_in={inc.sum()}, n_out={exc.sum()})")
print()

# ---------- Signal 9: Per-coin baseline WR ----------
print("=== Signal 9: Per-coin WR (are some coins structurally worse?) ===")
for c in ("BTC","ETH","SOL","XRP","DOGE","BNB","HYPE"):
    m = coin == c
    inc = base_mask & m
    if inc.sum() < 20: continue
    wr = float(won[inc].mean())
    buy = np.minimum(ep[inc] + 0.07, 0.70)
    ev = float((np.where(won[inc] == 1, 1.0 - buy, -buy)).mean())
    print(f"  {c}:  n={inc.sum():4d}  WR={wr:.2%}  EV={ev:+.4f}  ΔWR_vs_base={(wr-wr_base)*100:+.2f}pp")
print()

# ---------- Signal 10: Entry-price bins (robustness check) ----------
print("=== Signal 10: Entry-price bins (sanity / EV cliff detection) ===")
for lo, hi in [(0.05,0.45),(0.45,0.55),(0.55,0.60),(0.60,0.65),(0.65,0.70)]:
    m = (ep >= lo) & (ep < hi)
    inc = base_mask & m
    if inc.sum() < 20: continue
    wr = float(won[inc].mean())
    buy = np.minimum(ep[inc] + 0.07, 0.70)
    ev = float((np.where(won[inc] == 1, 1.0 - buy, -buy)).mean())
    print(f"  ep in [{lo:.2f},{hi:.2f}):  n={inc.sum():4d}  WR={wr:.2%}  EV={ev:+.4f}")
print()

print("=" * 80)
print("VERDICT LEGEND:")
print("  KEEP  → ΔWR >= 5pp AND retains >= 30% of volume → implement as gate")
print("  MIXED → ΔWR 3-5pp → telemetry only, re-evaluate with more data")
print("  DROP  → ΔWR < 3pp → no measurable edge")
