#!/usr/bin/env python3
"""Analyze latency breakdown from today's live trades."""
import csv

trades = []
with open('data/live_v30.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        if '2026-04-13' in row['timestamp']:
            trades.append(row)

print('=== LATENCY BREAKDOWN (today) ===')
print(f'{"coin":>5} {"sig>ord":>8} {"ord_lat":>8} {"total":>8} {"entry":>7}')
for t in trades:
    coin = t['coin']
    sig = t.get('signal_to_order_ms', '0')
    order = t.get('order_latency_ms', '0')
    total = t.get('total_latency_ms', '0')
    entry = t['entry_price']
    print(f'{coin:>5} {sig:>7}ms {order:>7}ms {total:>7}ms ${entry}')

print()
sigs = [int(float(t.get('signal_to_order_ms', '0'))) for t in trades]
orders = [int(float(t.get('order_latency_ms', '0'))) for t in trades]
totals = [int(float(t.get('total_latency_ms', '0'))) for t in trades]
print(f'Signal-to-Order: min={min(sigs)}ms  max={max(sigs)}ms  avg={sum(sigs)//len(sigs)}ms')
print(f'Order latency:   min={min(orders)}ms  max={max(orders)}ms  avg={sum(orders)//len(orders)}ms')
print(f'Total:           min={min(totals)}ms  max={max(totals)}ms  avg={sum(totals)//len(totals)}ms')

# Group by latency bucket
print()
print('=== WHERE IS THE TIME GOING? ===')
zero_sig = sum(1 for s in sigs if s == 0)
print(f'Trades with signal_to_order=0ms: {zero_sig}/{len(sigs)} (logged at execution, not signal time)')
print()

# Check if order latency correlates with outcome
print('=== LATENCY vs OUTCOME ===')
win_lats = [int(float(t.get('total_latency_ms', '0'))) for t in trades if t['outcome'] == 'won']
loss_lats = [int(float(t.get('total_latency_ms', '0'))) for t in trades if t['outcome'] == 'lost']
if win_lats:
    print(f'  Wins:   avg={sum(win_lats)//len(win_lats)}ms  min={min(win_lats)}ms  max={max(win_lats)}ms  (n={len(win_lats)})')
if loss_lats:
    print(f'  Losses: avg={sum(loss_lats)//len(loss_lats)}ms  min={min(loss_lats)}ms  max={max(loss_lats)}ms  (n={len(loss_lats)})')
