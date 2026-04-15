#!/usr/bin/env python3
"""Full FAK config sweep — all permutations, 14hr collector data."""
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
print(f'Total signals with prices: {len(rows)} ({len(rows)/hours:.0f}/hr)')
print()

# Full sweep
results = []
for th_min in [0.0007, 0.0008, 0.001, 0.0012, 0.0015, 0.002, 0.003]:
    for el_s in [0, 30, 60, 90, 120, 150, 180]:
        for el_e in [120, 150, 180, 210, 240, 270]:
            if el_e <= el_s:
                continue
            for max_p in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]:
                b = [r for r in rows if r['th'] >= th_min and r['el'] >= el_s and r['el'] <= el_e and r['ep'] <= max_p]
                w = sum(1 for r in b if r['o'] == 'won')
                n = len(b)
                if n < 15:
                    continue
                l = n - w
                wr = w / n * 100
                avg = sum(r['ep'] for r in b) / n
                be = avg * 100
                margin = wr - be
                ev = wr / 100 * (1 - avg) - (1 - wr / 100) * avg
                sim_pnl = w * (1 - avg) * 5 - l * avg * 5
                # Max loss per trade
                max_loss = max_p * 5
                results.append((ev, margin, wr, be, n, w, l, th_min, el_s, el_e, max_p, n / hours, sim_pnl, max_loss, avg))

count = len(results)
print(f'Permutations with n>=15: {count}')
print()

# Top 30 by EV
print('=== TOP 30 BY EV (n>=15) ===')
results.sort(key=lambda x: (-x[0], -x[1]))
header = f'{"mom":>7} {"el":>9} {"p<":>5} | {"WR":>4} {"BE":>4} {"margin":>6} {"EV":>6} {"pnl":>7} | {"n":>4} {"rate":>5} {"maxL":>5} {"W/L":>7}'
print(header)
print('-' * len(header))
for ev, margin, wr, be, n, w, l, th, es, ee, mp, rate, pnl, maxl, avg in results[:30]:
    print(f'{th:>7.4f} {es:>3}-{ee:<3} {mp:>5.2f} | {wr:>3.0f}% {be:>3.0f}% {margin:>+5.0f}pp {ev:>+5.3f} {pnl:>+6.1f} | {n:>4} {rate:>4.1f}/h ${maxl:>4.2f} {w:>3}W/{l}L')

# Top 30 by EV with n>=30 (robust)
print()
print('=== TOP 30 BY EV (n>=30, ROBUST) ===')
robust = [r for r in results if r[4] >= 30]
robust.sort(key=lambda x: (-x[0], -x[1]))
print(header)
print('-' * len(header))
for ev, margin, wr, be, n, w, l, th, es, ee, mp, rate, pnl, maxl, avg in robust[:30]:
    print(f'{th:>7.4f} {es:>3}-{ee:<3} {mp:>5.2f} | {wr:>3.0f}% {be:>3.0f}% {margin:>+5.0f}pp {ev:>+5.3f} {pnl:>+6.1f} | {n:>4} {rate:>4.1f}/h ${maxl:>4.2f} {w:>3}W/{l}L')

# Bust risk analysis for top configs at $15 balance
print()
print('=== BUST RISK AT $15 BALANCE ===')
for ev, margin, wr, be, n, w, l, th, es, ee, mp, rate, pnl, maxl, avg in robust[:5]:
    # P(3 consecutive losses) = (1-wr/100)^3
    loss_prob = (1 - wr / 100)
    p3 = loss_prob ** 3 * 100
    total_3loss = maxl * 3
    survives = total_3loss < 15
    print(f'  el={es}-{ee} p<={mp:.2f}: max_loss=${maxl:.2f} | 3 consec losses=${total_3loss:.2f} ({"SURVIVE" if survives else "BUST"}) | P(3 consec)={p3:.1f}%')

# Per-coin breakdown at best robust config
print()
if robust:
    best = robust[0]
    th, es, ee, mp = best[7], best[8], best[9], best[10]
    print(f'=== PER COIN AT BEST ROBUST CONFIG (mom>={th}, el={es}-{ee}, p<={mp}) ===')
    for coin in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'HYPE']:
        b = [r for r in rows if r['coin'] == coin and r['th'] >= th and r['el'] >= es and r['el'] <= ee and r['ep'] <= mp]
        cw = sum(1 for r in b if r['o'] == 'won')
        cn = len(b)
        if cn == 0:
            continue
        cl = cn - cw
        avg_ep = sum(r['ep'] for r in b) / cn
        print(f'  {coin}: {cw}W/{cl}L = {cw/cn*100:.0f}% avg_ep=${avg_ep:.2f} (n={cn})')
