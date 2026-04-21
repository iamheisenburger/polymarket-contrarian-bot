#!/usr/bin/env python3
"""Reconcile on-chain activity against live_v30.csv.

Source of truth: data-api /activity?type=TRADE. The CLOB /data/trades endpoint
is incomplete for this wallet (maker fills missing), so /activity is the only
complete picture.

Usage:
  python3 scripts/reconcile_fills.py [--since ISO8601] [--csv path]

Exits 0 if live log matches on-chain reality, 1 if ghosts or phantoms detected.
"""
import argparse
import csv
import json
import os
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

SAFE = "0x13D0684C532be5323662e851a1Bd10DF46d79806"


def fetch_activity(limit_per_page=500, max_pages=8):
    """Pull all /activity records, paginated. Data-api caps at ~3000 offset."""
    out = []
    for page in range(max_pages):
        offset = page * limit_per_page
        url = (
            f"https://data-api.polymarket.com/activity"
            f"?user={SAFE}&type=TRADE&limit={limit_per_page}&offset={offset}"
        )
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
        if len(batch) < limit_per_page:
            break
    # dedupe by (tx, asset)
    seen = set()
    uniq = []
    for t in out:
        k = (t.get("transactionHash"), t.get("asset"))
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    return uniq


def load_live_csv(path):
    with open(path) as f:
        return list(csv.DictReader(f))


def reconcile(activity, live_rows, since_ts):
    pm = [t for t in activity if t.get("side") == "BUY" and t.get("timestamp", 0) >= since_ts]
    live = [r for r in live_rows
            if datetime.fromisoformat(r["timestamp"]).timestamp() >= since_ts]

    # Aggregate activity into logical fills (group partials by slug+outcome+price)
    logical = defaultdict(lambda: {"size": 0, "price": 0, "ts": 0})
    for t in pm:
        k = (t["slug"], t["outcome"], round(t["price"], 3))
        g = logical[k]
        g["size"] += t["size"]
        g["price"] = t["price"]
        g["ts"] = max(g["ts"], t["timestamp"])

    live_cnt = Counter((r["market_slug"], r["side"]) for r in live)

    ghosts = []  # real fills missing from CSV
    for (slug, outcome, price), g in logical.items():
        side = {"Up": "up", "Down": "down"}.get(outcome, "")
        lc = live_cnt.get((slug, side), 0)
        if lc < 1:
            ghosts.append({
                "slug": slug, "side": side, "price": price,
                "size": g["size"], "ts": g["ts"],
            })
            live_cnt[(slug, side)] = max(0, lc - 1)
        else:
            live_cnt[(slug, side)] -= 1

    pm_keys = {(slug, {"Up": "up", "Down": "down"}.get(out, ""))
               for (slug, out, _) in logical.keys()}
    phantoms = []
    for r in live:
        k = (r["market_slug"], r["side"])
        if k not in pm_keys:
            phantoms.append(r)

    return ghosts, phantoms, logical


def load_shadow_predictions(shadow_path, config_id, since_ts):
    """Return list of shadow rows for given config in time window."""
    out = []
    try:
        with open(shadow_path) as f:
            for row in csv.DictReader(f):
                if row.get("config_id") != config_id:
                    continue
                ts_str = row.get("timestamp", "")
                if not ts_str:
                    continue
                try:
                    if datetime.fromisoformat(ts_str).timestamp() < since_ts:
                        continue
                except Exception:
                    continue
                out.append(row)
    except FileNotFoundError:
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-04-17T18:42:00+00:00",
                    help="ISO8601 cutoff (default: maker-GTD era start)")
    ap.add_argument("--csv", default="data/live_v30.csv",
                    help="Path to live trade log")
    ap.add_argument("--shadow", default="data/shadow_maker.csv",
                    help="Path to shadow predictions CSV (optional cross-check)")
    ap.add_argument("--live-config", default=None,
                    help="Optional live config_id (e.g. rp58_cm8_pe150) for shadow cross-check")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    since_ts = datetime.fromisoformat(args.since).timestamp()

    act = fetch_activity()
    live = load_live_csv(args.csv)

    ghosts, phantoms, logical = reconcile(act, live, since_ts)

    # Output
    print(f"Window: {args.since} → now")
    print(f"On-chain logical fills: {len(logical)}")
    print(f"Live CSV rows in window: {sum(1 for r in live if datetime.fromisoformat(r['timestamp']).timestamp() >= since_ts)}")
    print(f"GHOSTS (on-chain, missing from CSV): {len(ghosts)}")
    print(f"PHANTOMS (CSV, missing from on-chain): {len(phantoms)}")

    # Shadow cross-check: if live config given, compare live fills to shadow's
    # predictions for that config. Measures outcome alignment.
    if args.live_config:
        sh_rows = load_shadow_predictions(args.shadow, args.live_config, since_ts)
        sh_fills_by_market = {}
        for r in sh_rows:
            if r.get("was_filled") == "True":
                sh_fills_by_market[r["market_slug"] + ":" + r["side"]] = r.get("outcome", "")
        print(f"\nShadow cross-check for config={args.live_config}:")
        print(f"  Shadow predicted fills in window: {len(sh_fills_by_market)}")

        match = 0
        mismatch = []
        for (slug, outcome, price), g in logical.items():
            live_side = {"Up": "up", "Down": "down"}.get(outcome, "")
            k = f"{slug}:{live_side}"
            sh_outcome = sh_fills_by_market.get(k)
            if sh_outcome is None:
                mismatch.append(("live-only", slug, live_side, "—"))
                continue
            # Both predict fill. Compare outcome.
            # Live outcome requires Gamma settlement — approximate: if live
            # side == outcome (from PM activity.outcome which is token bought),
            # we need the actual settlement. For alignment we compare shadow's
            # outcome field vs the market's actual settlement — but since live
            # here is keyed by the token bought, not yet resolved in this
            # script, limit to matched-fill alignment only.
            match += 1
        only_shadow = set(sh_fills_by_market.keys())
        for (slug, outcome, _), _g in logical.items():
            k = f"{slug}:{ {'Up':'up','Down':'down'}.get(outcome,'') }"
            only_shadow.discard(k)
        for k in only_shadow:
            slug, s = k.split(":", 1)
            mismatch.append(("shadow-only", slug, s, sh_fills_by_market[k]))

        print(f"  Fills present in BOTH shadow & live (per market+side): {match}")
        print(f"  Mismatches: {len(mismatch)}")
        if not args.quiet and mismatch:
            print("    (label, slug, side, shadow_outcome)")
            for row in mismatch[:20]:
                print(f"    {row[0]:12s} {row[1]:45s} {row[2]:4s} {row[3]}")

    if ghosts and not args.quiet:
        print("\n--- Ghost detail ---")
        for g in sorted(ghosts, key=lambda x: x["ts"]):
            dt = datetime.fromtimestamp(g["ts"], timezone.utc).isoformat()[:19]
            print(f"  {dt} {g['slug']:45s} {g['side']:4s} ${g['price']:.4f} x {g['size']:.3f}")

    if phantoms and not args.quiet:
        print("\n--- Phantom detail ---")
        for p in phantoms:
            print(f"  {p['timestamp'][:19]} {p['market_slug']:45s} {p['side']:4s} ${p['entry_price']} x {p['num_tokens']}")

    if ghosts or phantoms:
        return 1
    print("\nCLEAN: live CSV fully matches on-chain reality.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
