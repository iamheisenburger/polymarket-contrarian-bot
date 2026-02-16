"""Quick analysis of lag collector data."""
import csv
from collections import Counter

rows = []
with open("data/lag_data.csv", "r") as f:
    reader = csv.DictReader(f)
    for row in reader:
        rows.append(row)

# Check unique Polymarket prices seen
up_bids = Counter()
up_asks = Counter()
down_bids = Counter()
down_asks = Counter()

for r in rows:
    up_bids[r["poly_up_best_bid"]] += 1
    up_asks[r["poly_up_best_ask"]] += 1
    down_bids[r["poly_down_best_bid"]] += 1
    down_asks[r["poly_down_best_ask"]] += 1

print("Polymarket Up Best Bid values:")
for v, c in up_bids.most_common(10):
    print(f"  {v}: {c} times")

print("\nPolymarket Up Best Ask values:")
for v, c in up_asks.most_common(10):
    print(f"  {v}: {c} times")

print("\nPolymarket Down Best Bid values:")
for v, c in down_bids.most_common(10):
    print(f"  {v}: {c} times")

print("\nPolymarket Down Best Ask values:")
for v, c in down_asks.most_common(10):
    print(f"  {v}: {c} times")

# Check BTC price range
btc_prices = [float(r["btc_price"]) for r in rows]
avg = sum(btc_prices) / len(btc_prices)
std = (sum((p - avg) ** 2 for p in btc_prices) / len(btc_prices)) ** 0.5
print(f"\nBTC price range: ${min(btc_prices):,.2f} - ${max(btc_prices):,.2f}")
print(f"BTC price std dev: ${std:,.2f}")
print(f"Total rows: {len(rows)}")
