#!/usr/bin/env python3
"""Full permutation sweep — ALL configs, no filtering bias."""
import csv

rows = []
with open('data/collector_v3.csv') as f:
    reader = csv.DictReader(f)
    for r in reader:
        if r.get('is_momentum_side') == 'True':
            try:
                ep = float(r['entry_price'])
                el = float(r['elapsed'])
                th = float(r['threshold'])
                if ep > 0:
                    rows.append({'ep': ep, 'el': el, 'th': th, 'o': r['outcome'], 'coin': r['coin']})
            except Exception:
                pass

print(f'Total signals with prices: {len(rows)}')
print()

results = []
for th_min in [0.0003, 0.0005, 0.0007, 0.0008, 0.001, 0.0012, 0.0015, 0.002, 0.003]:
    for el_min in [0, 30, 60, 90, 120, 150, 180, 210, 240]:
        for max_p in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
            b = [r for r in rows if r['th'] >= th_min and r['el'] >= el_min and r['el'] <= 270 and r['ep'] <= max_p]
            w = sum(1 for r in b if r['o'] == 'won')
            n = len(b)
            if n < 5:
                continue
            l = n - w
            wr = w / n * 100
            avg_ep = sum(r['ep'] for r in b) / n
            be = avg_ep * 100
            margin = wr - be
            ev = wr / 100 * (1 - avg_ep) - (1 - wr / 100) * avg_ep
            sim_pnl = w * (1 - avg_ep) * 5 - l * avg_ep * 5
            results.append((margin, ev, sim_pnl, n, wr, be, avg_ep, th_min, el_min, max_p, w, l))

# Show ALL results grouped by price cap
print('=== BY PRICE CAP (best config per price level, n>=5) ===')
print(f'{"p<":>5} {"mom":>8} {"el>":>4} | {"WR":>5} {"BE":>5} {"margin":>7} {"EV":>7} {"sim_pnl":>8} | {"n":>4} {"W/L":>6}')
print('-' * 75)
for max_p in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
    bucket = [r for r in results if r[9] == max_p]
    if not bucket:
        print(f'{max_p:>5.2f}   --- no configs with n>=5 ---')
        continue
    # Best by EV
    bucket.sort(key=lambda x: -x[1])
    m, ev, sp, n, wr, be, aep, th, el, mp, w, l = bucket[0]
    print(f'{mp:>5.2f} {th:>8.4f} {el:>4.0f} | {wr:>4.0f}% {be:>4.0f}% {m:>+6.0f}pp {ev:>+6.3f} {sp:>+7.2f} | {n:>4} {w:>2}W/{l}L')

# Top 50 by EV across ALL price ranges
print()
print('=== TOP 50 CONFIGS BY EV (all prices, n>=8) ===')
top = [r for r in results if r[3] >= 8]
top.sort(key=lambda x: (-x[1], -x[0]))
print(f'{"mom":>8} {"el>":>4} {"p<":>5} | {"WR":>5} {"BE":>5} {"margin":>7} {"EV":>7} {"sim_pnl":>8} | {"n":>4} {"W/L":>6}')
print('-' * 75)
for m, ev, sp, n, wr, be, aep, th, el, mp, w, l in top[:50]:
    print(f'{th:>8.4f} {el:>4.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+6.0f}pp {ev:>+6.3f} {sp:>+7.2f} | {n:>4} {w:>2}W/{l}L')

# LOSING configs — where does edge disappear?
print()
print('=== LOSING CONFIGS (margin < 0, n>=20) ===')
losers = [r for r in results if r[0] < 0 and r[3] >= 20]
losers.sort(key=lambda x: x[1])  # worst EV first
print(f'{"mom":>8} {"el>":>4} {"p<":>5} | {"WR":>5} {"BE":>5} {"margin":>7} {"EV":>7} {"sim_pnl":>8} | {"n":>4} {"W/L":>6}')
print('-' * 75)
for m, ev, sp, n, wr, be, aep, th, el, mp, w, l in losers[:20]:
    print(f'{th:>8.4f} {el:>4.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+6.0f}pp {ev:>+6.3f} {sp:>+7.2f} | {n:>4} {w:>2}W/{l}L')

# Trade frequency estimate
print()
print('=== TRADE FREQUENCY (trades per hour at each price cap, mom>=0.001) ===')
runtime_hours = 1.27  # ~76 minutes of data
for max_p in [0.45, 0.55, 0.65, 0.75, 0.85, 0.95]:
    b = [r for r in rows if r['th'] >= 0.001 and r['el'] >= 0 and r['el'] <= 270 and r['ep'] <= max_p]
    n = len(b)
    per_hour = n / runtime_hours
    print(f'  p<={max_p:.2f}: {n} trades in {runtime_hours:.1f}h = {per_hour:.0f}/hour')
