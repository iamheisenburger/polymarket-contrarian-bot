#!/usr/bin/env python3
"""FAK sweep — only configs with n>=100."""
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

hours = 14.1
results = []
for th_min in [0.0005, 0.0007, 0.0008, 0.001, 0.0012, 0.0015, 0.002, 0.003]:
    for el_s in [0, 30, 60, 90, 120, 150]:
        for el_e in [120, 150, 180, 210, 240, 270]:
            if el_e <= el_s:
                continue
            for max_p in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
                b = [r for r in rows if r['th'] >= th_min and r['el'] >= el_s and r['el'] <= el_e and r['ep'] <= max_p]
                w = sum(1 for r in b if r['o'] == 'won')
                n = len(b)
                if n < 100:
                    continue
                l = n - w
                wr = w / n * 100
                avg = sum(r['ep'] for r in b) / n
                be = avg * 100
                margin = wr - be
                ev = wr / 100 * (1 - avg) - (1 - wr / 100) * avg
                maxl = max_p * 5
                results.append((ev, margin, wr, be, n, w, l, th_min, el_s, el_e, max_p, n / hours, maxl, avg))

results.sort(key=lambda x: (-x[0], -x[1]))
print(f'Configs with n>=100: {len(results)}')
print()
for ev, margin, wr, be, n, w, l, th, es, ee, mp, rate, maxl, avg in results[:40]:
    bust_3 = maxl * 3
    survive = "OK" if bust_3 < 15 else "RISK"
    print(f'  mom>={th:.4f} el={es:>3}-{ee:<3} p<={mp:.2f} | {wr:.0f}% BE={be:.0f}% margin={margin:+.0f}pp EV={ev:+.3f} | n={n} {rate:.0f}/h maxL=${maxl:.2f} 3x=${bust_3:.2f} [{survive}] {w}W/{l}L')
