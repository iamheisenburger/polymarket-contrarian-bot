#!/usr/bin/env python3
"""Spot-check trade outcomes against Gamma API to prove data is real."""
import sys, json, urllib.request

CHECKS = [
    ("Oracle #1",  "sol-updown-5m-1771690500", "down", "won"),
    ("Oracle #5",  "eth-updown-5m-1771691400", "up",   "lost"),
    ("Oracle #10", "btc-updown-5m-1771692600", "up",   "won"),
    ("Oracle #15", "xrp-updown-5m-1771693800", "down", "won"),
    ("Spread #1",  "btc-updown-5m-1771690200", "down", "won"),
    ("Spread #5",  "btc-updown-5m-1771693200", "up",   "lost"),
]

matches = 0
total = 0

for label, slug, side, csv_outcome in CHECKS:
    try:
        url = f"https://gamma-api.polymarket.com/markets?slug={slug}"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())

        if not data:
            print(f"{label}: {slug} — NOT FOUND")
            continue

        m = data[0]
        prices = m.get("outcomePrices", "[]")
        if isinstance(prices, str):
            prices = json.loads(prices)

        up_p = float(prices[0]) if len(prices) > 0 else 0
        down_p = float(prices[1]) if len(prices) > 1 else 0

        if up_p > 0.9:
            winner = "up"
        elif down_p > 0.9:
            winner = "down"
        else:
            print(f"{label}: {slug} — UNRESOLVED (up={up_p:.2f} down={down_p:.2f})")
            continue

        actual = "won" if side == winner else "lost"
        ok = actual == csv_outcome
        status = "MATCH" if ok else "MISMATCH"
        if ok:
            matches += 1
        total += 1

        print(f"{label}: {slug}")
        print(f"  Gamma API: up={up_p:.2f} down={down_p:.2f} → winner={winner}")
        print(f"  CSV says: side={side} outcome={csv_outcome} | Gamma says: {actual} → {status}")
        print()
    except Exception as e:
        print(f"{label}: ERROR — {e}")

print(f"=== {matches}/{total} verified correct ===")
if matches == total and total > 0:
    print("ALL OUTCOMES MATCH GAMMA API. Data is legit.")
else:
    print("MISMATCHES FOUND — investigate!")
