#!/usr/bin/env python3
"""Verify CSV column alignment after header fix."""
import csv

with open('data/live_v30.csv') as f:
    reader = csv.DictReader(f)
    headers = reader.fieldnames
    print(f"Header columns ({len(headers)}): {headers}")
    print()

    for row in reader:
        if '18:27:31' in row['timestamp'] and row['coin'] == 'HYPE':
            print("HYPE 18:27:31 row:")
            print(f"  side = {row['side']}")
            print(f"  entry_price = {row['entry_price']}")
            print(f"  volatility_std = {row['volatility_std']}")
            print(f"  time_to_expiry = {row['time_to_expiry_at_entry']}")
            print(f"  momentum = {row['momentum_at_entry']}")
            print(f"  volatility_at_entry = {row['volatility_at_entry']}")
            print(f"  strike_source = {row['strike_source']}")
            break

# Also check a BTC trade
print()
with open('data/live_v30.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if '00:59:10' in row['timestamp'] and row['coin'] == 'BTC':
            print("BTC 00:59:10 row:")
            print(f"  side = {row['side']}")
            print(f"  entry_price = {row['entry_price']}")
            print(f"  volatility_std = {row['volatility_std']}")
            print(f"  time_to_expiry = {row['time_to_expiry_at_entry']}")
            print(f"  momentum = {row['momentum_at_entry']}")
            print(f"  volatility_at_entry = {row['volatility_at_entry']}")
            print(f"  strike_source = {row['strike_source']}")
            break
