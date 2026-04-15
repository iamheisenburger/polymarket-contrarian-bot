#!/usr/bin/env python3
"""Full permutation sweep on collector data."""
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

best = []
for th_min in [0.0003, 0.0005, 0.0007, 0.0008, 0.001, 0.0012, 0.0015, 0.002]:
    for el_min in [0, 30, 60, 90, 120, 150, 180]:
        for max_p in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80]:
            b = [r for r in rows if r['th'] >= th_min and r['el'] >= el_min and r['el'] <= 270 and r['ep'] <= max_p]
            w = sum(1 for r in b if r['o'] == 'won')
            n = len(b)
            if n < 8:
                continue
            l = n - w
            wr = w / n * 100
            avg_ep = sum(r['ep'] for r in b) / n
            be = avg_ep * 100
            margin = wr - be
            ev = wr / 100 * (1 - avg_ep) - (1 - wr / 100) * avg_ep
            sim_pnl = w * (1 - avg_ep) * 5 - l * avg_ep * 5
            if margin > 5:
                best.append((margin, ev, sim_pnl, n, wr, be, avg_ep, th_min, el_min, max_p, w, l))

best.sort(key=lambda x: (-x[1], -x[0]))
print(f'Configs with margin>5pp and n>=8: {len(best)}')
print()
header = f'{"mom":>8} {"el>":>4} {"p<":>5} | {"WR":>5} {"BE":>5} {"margin":>7} {"EV":>7} {"sim_pnl":>8} | {"n":>4} {"W/L":>6}'
print(header)
print('-' * len(header))
for m, ev, sp, n, wr, be, aep, th, el, mp, w, l in best[:40]:
    print(f'{th:>8.4f} {el:>4.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+6.0f}pp {ev:>+6.3f} {sp:>+7.2f} | {n:>4} {w:>2}W/{l}L')
