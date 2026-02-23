#!/usr/bin/env python3
"""Compare on-chain trades vs bot's pending positions."""
import json
import requests

SAFE = "0x13D0684C532be5323662e851a1Bd10DF46d79806"

# Get all on-chain trades
r = requests.get(f"https://data-api.polymarket.com/activity?user={SAFE}&limit=500", timeout=30)
all_trades = r.json()

weather = [t for t in all_trades if "temperature" in t.get("title", "").lower()]
print(f"=== ON-CHAIN WEATHER TRADES: {len(weather)} ===")
on_chain_slugs = set()
for t in sorted(weather, key=lambda x: x["timestamp"]):
    slug = t.get("slug", "")
    on_chain_slugs.add(slug)
    print(f"  {t['title'][:70]:70} {t['size']:>6} tok @${t['price']:.3f}  ${t['usdcSize']:.2f}")

# Get bot's pending positions
with open("/opt/polymarket-bot/data/weather_trades.pending.json") as f:
    pending = json.load(f)

print(f"\n=== BOT PENDING POSITIONS: {len([p for p in pending.values() if not p.get('resolved')])} ===")
missing = []
for slug, pos in sorted(pending.items(), key=lambda x: x[1].get("market_date", "")):
    if pos.get("resolved"):
        continue
    on_chain = slug in on_chain_slugs
    status = "ON-CHAIN" if on_chain else "MISSING!"
    city = pos.get("city", "?")
    date = pos.get("market_date", "?")
    bucket = pos.get("bucket_label", "?")
    cost = pos.get("cost", 0)
    print(f"  [{status:8}] {city:14} {date} {bucket:12} ${cost:.2f}")
    if not on_chain:
        missing.append(f"{city} {date} {bucket} ${cost:.2f}")

if missing:
    print(f"\n=== PHANTOM TRADES (bot thinks filled, not on chain): {len(missing)} ===")
    total_phantom = 0
    for m in missing:
        print(f"  {m}")
