#!/usr/bin/env python3
"""Calibrate shadow_maker.csv predictions against real on-chain fills.

Goal: measure how much shadow over-predicts fills and WR vs reality, per
config. Enables "trust but calibrate" config selection instead of taking
shadow numbers at face value.

Data sources:
  - data/shadow_maker.csv        (shadow predictions, one row per placement)
  - Polymarket data-api /activity (ground truth of on-chain fills)

Matching: shadow row (config_id, market_slug, side, was_filled) vs
on-chain trade (slug, outcome→side, price≈rest_price, timestamp within window).

Output per config:
  - shadow_fills, live_fills, shadow_WR, live_WR, fill_rate_ratio, WR_gap
  - flagged configs where shadow ≈ live (trustworthy) vs far apart (not)
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timezone

SAFE = "0x13D0684C532be5323662e851a1Bd10DF46d79806"


def parse_config_id(cid: str):
    """rp52_cm8_pe150 → (0.52, 0.0008, 150)"""
    parts = cid.split("_")
    if len(parts) != 3:
        return None
    try:
        rp = float(parts[0][2:]) / 100
        cm = float(parts[1][2:]) / 10000
        pe = float(parts[2][2:])
        return rp, cm, pe
    except Exception:
        return None


def fetch_activity():
    out = []
    for page in range(8):
        offset = page * 500
        url = f"https://data-api.polymarket.com/activity?user={SAFE}&type=TRADE&limit=500&offset={offset}"
        r = subprocess.run(["curl", "-s", url], capture_output=True, text=True, timeout=30)
        try:
            batch = json.loads(r.stdout)
        except Exception:
            break
        if isinstance(batch, dict) and batch.get("error"):
            break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
    # dedupe
    seen = set()
    u = []
    for t in out:
        k = (t.get("transactionHash"), t.get("asset"))
        if k in seen:
            continue
        seen.add(k)
        u.append(t)
    return u


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shadow", default="data/shadow_maker.csv")
    ap.add_argument("--since", default="2026-04-19T00:41:00+00:00",
                    help="ISO cutoff; default = shadow collection start")
    ap.add_argument("--configs", nargs="+", default=None,
                    help="Optional: limit to specific config_ids")
    args = ap.parse_args()

    since_ts = datetime.fromisoformat(args.since).timestamp()

    # Load shadow predictions
    shadow_by_config = defaultdict(list)
    with open(args.shadow) as f:
        for row in csv.DictReader(f):
            ts_str = row.get("timestamp", "")
            if not ts_str: continue
            try:
                if datetime.fromisoformat(ts_str).timestamp() < since_ts:
                    continue
            except Exception:
                continue
            shadow_by_config[row["config_id"]].append(row)

    # Load live on-chain fills, aggregate to logical fills
    pm = fetch_activity()
    logical_live = defaultdict(lambda: {"size": 0, "price": 0, "ts": 0, "outcome": "", "slug": ""})
    for t in pm:
        if t.get("side") != "BUY": continue
        ts = t.get("timestamp", 0)
        if ts < since_ts: continue
        k = (t["slug"], t["outcome"], round(t["price"], 3))
        g = logical_live[k]
        g["size"] += t["size"]
        g["price"] = t["price"]
        g["ts"] = max(g["ts"], ts)
        g["outcome"] = t["outcome"]
        g["slug"] = t["slug"]

    # Need market settlements to compute live WR (which side won)
    # Simple proxy: outcome field in activity reflects the token bought, not the
    # winner. We need Gamma for settlement. Fetch per-slug.
    live_markets = {f["slug"] for f in logical_live.values()}
    settlements = {}
    for slug in live_markets:
        url = f"https://gamma-api.polymarket.com/events?slug={slug}"
        r = subprocess.run(["curl", "-s", url], capture_output=True, text=True, timeout=15)
        try:
            data = json.loads(r.stdout)
            if isinstance(data, list) and data:
                m = data[0]["markets"][0]
                op = m.get("outcomePrices", "[\"0\",\"0\"]")
                outs = m.get("outcomes", "[\"Up\",\"Down\"]")
                if isinstance(op, str): op = json.loads(op)
                if isinstance(outs, str): outs = json.loads(outs)
                winner = None
                for j, p in enumerate(op):
                    if float(p) > 0.5:
                        winner = outs[j]
                        break
                settlements[slug] = winner
        except Exception:
            settlements[slug] = None

    # Compute live stats per price bucket
    live_by_price = defaultdict(lambda: {"fills": 0, "wins": 0, "slugs": set()})
    for (slug, outcome, price), g in logical_live.items():
        winner = settlements.get(slug)
        if winner is None: continue
        bucket = f"rp{int(round(price * 100)):02d}"
        live_by_price[bucket]["fills"] += 1
        live_by_price[bucket]["slugs"].add(slug)
        if outcome == winner:
            live_by_price[bucket]["wins"] += 1

    # Compute shadow stats per config
    print(f"Window since: {args.since}")
    print(f"Shadow configs: {len(shadow_by_config)}, live fills: {len(logical_live)}")
    print()

    target = sorted(args.configs) if args.configs else sorted(shadow_by_config)
    print(f"{'config_id':20s} {'SH_plc':>7s} {'SH_fill':>8s} {'SH_WR':>7s} | "
          f"{'LV_plc':>7s} {'LV_fill':>8s} {'LV_WR':>7s} | "
          f"{'fill_ratio':>10s} {'WR_gap':>7s}")

    rows_for_report = []
    for cid in target:
        rows = shadow_by_config.get(cid, [])
        if not rows: continue
        p = parse_config_id(cid)
        if not p: continue
        rp, cm, pe = p
        bucket = f"rp{int(round(rp * 100)):02d}"

        sh_placed = len(rows)
        sh_fills = sum(1 for r in rows if r.get("was_filled") == "True")
        sh_wins = sum(1 for r in rows if r.get("was_filled") == "True" and r.get("outcome") == "won")
        sh_wr = (sh_wins / sh_fills * 100) if sh_fills else 0.0

        live = live_by_price.get(bucket, {"fills": 0, "wins": 0, "slugs": set()})
        lv_placed = len(live["slugs"])  # distinct markets = placements proxy
        lv_fills = live["fills"]
        lv_wr = (live["wins"] / lv_fills * 100) if lv_fills else 0.0

        fill_ratio = (sh_fills / lv_fills) if lv_fills else float("inf") if sh_fills else 0.0
        wr_gap = sh_wr - lv_wr

        rows_for_report.append((cid, sh_placed, sh_fills, sh_wr, lv_placed, lv_fills, lv_wr, fill_ratio, wr_gap))

    # Sort: prioritize configs with live data
    rows_for_report.sort(key=lambda r: (-r[5], r[0]))

    for r in rows_for_report[:40]:
        cid, sh_p, sh_f, sh_wr, lv_p, lv_f, lv_wr, fr, gap = r
        fr_str = f"{fr:>9.1f}x" if fr != float("inf") else "      inf"
        lv_wr_str = f"{lv_wr:>6.1f}%" if lv_f > 0 else "    n/a"
        print(f"{cid:20s} {sh_p:>7d} {sh_f:>8d} {sh_wr:>6.1f}% | "
              f"{lv_p:>7d} {lv_f:>8d} {lv_wr_str} | "
              f"{fr_str} {gap:>+6.1f}pp")

    # Summary
    print()
    configs_with_live = [r for r in rows_for_report if r[5] > 0]
    if configs_with_live:
        avg_gap = sum(r[8] for r in configs_with_live) / len(configs_with_live)
        avg_ratio = sum(r[7] for r in configs_with_live if r[7] != float("inf")) / max(1, len([r for r in configs_with_live if r[7] != float("inf")]))
        print(f"Configs with live data: {len(configs_with_live)}")
        print(f"Avg WR gap (shadow - live): {avg_gap:+.1f}pp")
        print(f"Avg fill-rate ratio (shadow / live): {avg_ratio:.1f}x")
        print()
        print("Calibration implication:")
        print(f"  If new shadow predicts WR=X%, expected live WR ≈ X% - {avg_gap:.0f}pp")
        print(f"  If new shadow predicts N fills, expected live ≈ N / {avg_ratio:.0f}")
    else:
        print("No overlap between shadow configs and live price buckets. Calibration pending.")


if __name__ == "__main__":
    main()
