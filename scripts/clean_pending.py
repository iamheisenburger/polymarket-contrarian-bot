#!/usr/bin/env python3
"""Remove phantom positions from pending file based on on-chain verification."""
import json
import sys
import requests

SAFE = "0x13D0684C532be5323662e851a1Bd10DF46d79806"
PENDING_FILE = "/opt/polymarket-bot/data/weather_trades.pending.json"

# Get all on-chain trades
r = requests.get(f"https://data-api.polymarket.com/activity?user={SAFE}&limit=500", timeout=30)
all_trades = r.json()
on_chain_slugs = set(t.get("slug", "") for t in all_trades)

# Load pending
with open(PENDING_FILE) as f:
    pending = json.load(f)

# Find and remove phantoms
removed = []
cleaned = {}
for slug, pos in pending.items():
    if pos.get("resolved"):
        cleaned[slug] = pos
        continue
    if slug in on_chain_slugs:
        cleaned[slug] = pos
    else:
        removed.append(f"{pos.get('city','?')} {pos.get('market_date','?')} {pos.get('bucket_label','?')} ${pos.get('cost',0):.2f}")

if removed:
    print(f"Found {len(removed)} phantom positions:")
    for r_item in removed:
        print(f"  {r_item}")

    if '--apply' in sys.argv:
        # Backup first
        with open(PENDING_FILE + ".backup", "w") as f:
            json.dump(pending, f, indent=2)
        # Write cleaned
        with open(PENDING_FILE, "w") as f:
            json.dump(cleaned, f, indent=2)
        print(f"\nRemoved {len(removed)} phantoms, kept {len(cleaned)} real positions")
        print(f"Backup saved to {PENDING_FILE}.backup")
    else:
        print("\nDRY RUN. Pass --apply to write changes.")
else:
    print("No phantoms found - all positions verified on-chain")
