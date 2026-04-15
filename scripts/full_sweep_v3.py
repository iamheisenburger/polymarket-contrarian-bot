#!/usr/bin/env python3
"""Full permutation sweep with window RANGES, not just start."""
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

print(f'Total signals: {len(rows)}')

results = []
mom_vals = [0.0003, 0.0005, 0.0007, 0.0008, 0.001, 0.0012, 0.0015, 0.002, 0.003, 0.005]
el_starts = [0, 30, 60, 90, 120, 150, 180, 210]
el_ends = [120, 150, 180, 210, 240, 270, 300]
price_caps = [0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]

count = 0
for th_min in mom_vals:
    for el_min in el_starts:
        for el_max in el_ends:
            if el_max <= el_min:
                continue
            for max_p in price_caps:
                count += 1
                b = [r for r in rows if r['th'] >= th_min and r['el'] >= el_min and r['el'] <= el_max and r['ep'] <= max_p]
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
                results.append((margin, ev, sim_pnl, n, wr, be, avg_ep, th_min, el_min, el_max, max_p, w, l))

print(f'Permutations tested: {count}')
print(f'Configs with n>=5: {len(results)}')
print()

# Top 30 by EV, n>=8
print('=== TOP 30 BY EV (n>=8) ===')
top = [r for r in results if r[3] >= 8]
top.sort(key=lambda x: (-x[1], -x[0]))
header = f'{"mom":>7} {"el":>9} {"p<":>5} | {"WR":>5} {"BE":>5} {"margin":>7} {"EV":>7} {"pnl":>7} | {"n":>4} {"W/L":>6}'
print(header)
print('-' * len(header))
for m, ev, sp, n, wr, be, aep, th, el_s, el_e, mp, w, l in top[:30]:
    print(f'{th:>7.4f} {el_s:>3.0f}-{el_e:<3.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+5.0f}pp {ev:>+6.3f} {sp:>+6.1f} | {n:>4} {w:>2}W/{l}L')

# Top 30 by EV, n>=20 (robustness)
print()
print('=== TOP 30 BY EV (n>=20, ROBUST) ===')
robust = [r for r in results if r[3] >= 20]
robust.sort(key=lambda x: (-x[1], -x[0]))
print(header)
print('-' * len(header))
for m, ev, sp, n, wr, be, aep, th, el_s, el_e, mp, w, l in robust[:30]:
    print(f'{th:>7.4f} {el_s:>3.0f}-{el_e:<3.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+5.0f}pp {ev:>+6.3f} {sp:>+6.1f} | {n:>4} {w:>2}W/{l}L')

# Top by sim_pnl (most money made), n>=15
print()
print('=== TOP 20 BY SIMULATED PNL (n>=15) ===')
pnl_top = [r for r in results if r[3] >= 15]
pnl_top.sort(key=lambda x: -x[2])
print(header)
print('-' * len(header))
for m, ev, sp, n, wr, be, aep, th, el_s, el_e, mp, w, l in pnl_top[:20]:
    print(f'{th:>7.4f} {el_s:>3.0f}-{el_e:<3.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+5.0f}pp {ev:>+6.3f} {sp:>+6.1f} | {n:>4} {w:>2}W/{l}L')

# All LOSING configs n>=20
print()
print('=== ALL LOSING CONFIGS (margin<0, n>=20) ===')
losers = [r for r in results if r[0] < 0 and r[3] >= 20]
losers.sort(key=lambda x: x[1])
print(header)
print('-' * len(header))
for m, ev, sp, n, wr, be, aep, th, el_s, el_e, mp, w, l in losers[:15]:
    print(f'{th:>7.4f} {el_s:>3.0f}-{el_e:<3.0f} {mp:>5.2f} | {wr:>4.0f}% {be:>4.0f}% {m:>+5.0f}pp {ev:>+6.3f} {sp:>+6.1f} | {n:>4} {w:>2}W/{l}L')
