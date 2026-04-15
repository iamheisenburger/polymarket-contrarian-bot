#!/usr/bin/env python3
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
                ts = r['timestamp']
                if ep > 0 and '2026-04-14T01:' in ts:
                    rows.append({'ep': ep, 'el': el, 'th': th, 'o': r['outcome'], 'coin': r['coin'], 'ts': ts[:19]})
            except Exception:
                pass

print(f'Signals in last ~hour: {len(rows)}')
print()

matched = [r for r in rows if r['th'] >= 0.0012 and 30 <= r['el'] <= 270 and r['ep'] <= 0.80]
print(f'=== AT V32 CONFIG (mom>=0.0012, el=30-270, ask<=0.80) ===')
for r in matched:
    maker_price = round(r['ep'] - 0.05, 2)
    print(f"  {r['ts'][11:]} {r['coin']:4s} ask=${r['ep']:.2f} maker=${maker_price:.2f} el={r['el']:.0f}s {r['o']}")

w = sum(1 for r in matched if r['o'] == 'won')
l = len(matched) - w
print(f'\nTotal: {len(matched)} | {w}W/{l}L = {w/(w+l)*100:.0f}% WR' if matched else '\nTotal: 0')

# Also check what maker price would have been (ask - 0.05) and if <= 0.75
print()
print(f'=== MAKER FILLS THAT WOULD PASS price<=0.75 ===')
for r in matched:
    mp = round(r['ep'] - 0.05, 2)
    if mp <= 0.75:
        print(f"  {r['ts'][11:]} {r['coin']:4s} ask=${r['ep']:.2f} maker=${mp:.2f} el={r['el']:.0f}s {r['o']}")
