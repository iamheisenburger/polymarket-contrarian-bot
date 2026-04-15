#!/usr/bin/env python3
"""Entry price vs WR economics analysis using collector data with valid prices."""
import csv

coll = []
with open('data/collector_v3.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        coll.append(row)

# Filter: momentum side, threshold=0.001, valid entry price > 0
mom_side = []
for r in coll:
    if r.get('is_momentum_side') != 'True':
        continue
    try:
        ep = float(r['entry_price'])
        el = float(r['elapsed'])
        thresh = float(r['threshold'])
        mom = float(r['momentum'])
    except Exception:
        continue
    if thresh != 0.001:
        continue
    if ep <= 0:
        continue
    r['_ep'] = ep
    r['_el'] = el
    r['_mom'] = mom
    mom_side.append(r)

print(f'Total momentum-side signals with valid prices at thresh=0.001: {len(mom_side)}')
print()

# === WR by entry price bucket ===
print('=== WR BY ENTRY PRICE BUCKET ===')
buckets = [(0.30, 0.50), (0.50, 0.60), (0.60, 0.70), (0.70, 0.80), (0.80, 0.90), (0.90, 1.00)]
for lo, hi in buckets:
    b = [r for r in mom_side if lo <= r['_ep'] < hi]
    w = sum(1 for r in b if r['outcome'] == 'won')
    l = sum(1 for r in b if r['outcome'] == 'lost')
    n = w + l
    if n == 0:
        continue
    wr = w / n * 100
    # Breakeven WR = entry_price (for binary payoff)
    avg_ep = sum(r['_ep'] for r in b) / n
    be_wr = avg_ep * 100
    # Expected profit per $1 bet
    ev = wr/100 * (1 - avg_ep) - (1 - wr/100) * avg_ep
    status = 'PROFIT' if wr > be_wr else 'LOSS'
    print(f'  ${lo:.2f}-${hi:.2f}: {w}W/{l}L = {wr:.1f}% (n={n}) | BE={be_wr:.0f}% | EV=${ev:.3f} | {status}')

# === WR by elapsed bucket ===
print()
print('=== WR BY ELAPSED BUCKET (with valid prices) ===')
el_buckets = [(0, 60), (60, 120), (120, 180), (180, 240), (240, 270), (270, 300)]
for lo, hi in el_buckets:
    b = [r for r in mom_side if lo <= r['_el'] < hi]
    w = sum(1 for r in b if r['outcome'] == 'won')
    l = sum(1 for r in b if r['outcome'] == 'lost')
    n = w + l
    if n == 0:
        continue
    wr = w / n * 100
    avg_ep = sum(r['_ep'] for r in b) / n
    print(f'  el={lo}-{hi}s: {w}W/{l}L = {wr:.1f}% (n={n}) avg_entry=${avg_ep:.2f}')

# === Combined: entry price <= 0.70 AND elapsed >= 150 ===
print()
print('=== SWEET SPOT CONFIGS ===')
configs = [
    ('price<=0.70, el>=150-270', lambda r: r['_ep'] <= 0.70 and 150 <= r['_el'] <= 270),
    ('price<=0.60, el>=150-270', lambda r: r['_ep'] <= 0.60 and 150 <= r['_el'] <= 270),
    ('price<=0.70, el>=180-270', lambda r: r['_ep'] <= 0.70 and 180 <= r['_el'] <= 270),
    ('price<=0.60, el>=180-270', lambda r: r['_ep'] <= 0.60 and 180 <= r['_el'] <= 270),
    ('price<=0.80, el>=150-270', lambda r: r['_ep'] <= 0.80 and 150 <= r['_el'] <= 270),
    ('price<=0.80, el>=200-270', lambda r: r['_ep'] <= 0.80 and 200 <= r['_el'] <= 270),
    ('price<=0.70, el>=200-270', lambda r: r['_ep'] <= 0.70 and 200 <= r['_el'] <= 270),
]
for desc, filt in configs:
    b = [r for r in mom_side if filt(r)]
    w = sum(1 for r in b if r['outcome'] == 'won')
    l = sum(1 for r in b if r['outcome'] == 'lost')
    n = w + l
    if n == 0:
        print(f'  {desc}: NO DATA')
        continue
    wr = w / n * 100
    avg_ep = sum(r['_ep'] for r in b) / n
    be_wr = avg_ep * 100
    ev = wr/100 * (1 - avg_ep) - (1 - wr/100) * avg_ep
    # Simulated PnL per trade (5 tokens)
    sim_win = (1 - avg_ep) * 5
    sim_loss = avg_ep * 5
    sim_pnl = w * sim_win - l * sim_loss
    print(f'  {desc}: {w}W/{l}L = {wr:.1f}% (n={n}) avg_ep=${avg_ep:.2f} BE={be_wr:.0f}% EV/trade=${ev:.3f} sim_pnl=${sim_pnl:.2f}')

# === Higher momentum thresholds ===
print()
print('=== HIGHER MOMENTUM THRESHOLDS (price<=0.80, el=150-270, valid prices) ===')
for thresh_val in [0.001, 0.0012, 0.0015, 0.002, 0.003]:
    b = []
    for r in coll:
        if r.get('is_momentum_side') != 'True':
            continue
        try:
            ep = float(r['entry_price'])
            el = float(r['elapsed'])
            thresh = float(r['threshold'])
        except Exception:
            continue
        if thresh != thresh_val or ep <= 0 or ep > 0.80 or el < 150 or el > 270:
            continue
        b.append({'ep': ep, 'outcome': r['outcome']})
    w = sum(1 for r in b if r['outcome'] == 'won')
    l = sum(1 for r in b if r['outcome'] == 'lost')
    n = w + l
    if n == 0:
        continue
    wr = w / n * 100
    avg_ep = sum(r['ep'] for r in b) / n
    be_wr = avg_ep * 100
    ev = wr/100 * (1 - avg_ep) - (1 - wr/100) * avg_ep
    print(f'  mom>={thresh_val}: {w}W/{l}L = {wr:.1f}% (n={n}) avg_ep=${avg_ep:.2f} BE={be_wr:.0f}% EV=${ev:.3f}')

# === Per-coin WR (thresh=0.001, valid prices, el=150-270) ===
print()
print('=== PER-COIN WR (thresh=0.001, price>0, el=150-270) ===')
for coin in ['BTC', 'ETH', 'SOL', 'XRP', 'DOGE', 'BNB', 'HYPE']:
    b = [r for r in mom_side if r['coin'] == coin and 150 <= r['_el'] <= 270]
    w = sum(1 for r in b if r['outcome'] == 'won')
    l = sum(1 for r in b if r['outcome'] == 'lost')
    n = w + l
    if n == 0:
        print(f'  {coin}: NO DATA')
        continue
    wr = w / n * 100
    avg_ep = sum(r['_ep'] for r in b) / n
    print(f'  {coin}: {w}W/{l}L = {wr:.1f}% (n={n}) avg_ep=${avg_ep:.2f}')
