#!/usr/bin/env python3
"""Analyze today's live trades vs collector for WR gap investigation."""
import csv
from collections import defaultdict

live = []
with open('data/live_v30.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        live.append(row)

today = [t for t in live if '2026-04-13' in t['timestamp']]
wins = [t for t in today if t['outcome'] == 'won']
losses = [t for t in today if t['outcome'] == 'lost']
total_pnl = sum(float(t['pnl']) for t in today)
print(f'Today: {len(wins)}W/{len(losses)}L = {len(wins)/(len(wins)+len(losses))*100:.1f}% PnL=${total_pnl:.2f}')
print()

print('=== LOSSES ===')
for t in today:
    if t['outcome'] == 'lost':
        slug = t['market_slug']
        coin = t['coin']
        side = t['side']
        entry = float(t['entry_price'])
        mom = float(t['momentum_at_entry'])
        tte_raw = t.get('time_to_expiry_at_entry', '0')
        try:
            tte = int(tte_raw)
        except Exception:
            tte = int(float(tte_raw))
        elapsed = 300 - tte
        pnl = float(t['pnl'])
        strike_src = t.get('strike_source', '?')
        print(f'  {coin:4s} {side:4s} entry=${entry:.2f} mom={abs(mom):.6f} el={elapsed}s tte={tte}s loss=${abs(pnl):.2f} [{slug[-25:]}] strike={strike_src}')

print()
print('=== WINS ===')
for t in today:
    if t['outcome'] == 'won':
        coin = t['coin']
        side = t['side']
        entry = float(t['entry_price'])
        mom = float(t['momentum_at_entry'])
        pnl = float(t['pnl'])
        print(f'  {coin:4s} {side:4s} entry=${entry:.2f} mom={abs(mom):.6f} win=+${pnl:.2f}')

print()
print('=== CLUSTER ANALYSIS (same window timestamp) ===')
slug_ts = defaultdict(list)
for t in today:
    ts = t['market_slug'].rsplit('-', 1)[-1]
    slug_ts[ts].append(t)
for ts, trades in sorted(slug_ts.items()):
    if len(trades) > 1:
        w = sum(1 for t in trades if t['outcome'] == 'won')
        l = sum(1 for t in trades if t['outcome'] == 'lost')
        pnl = sum(float(t['pnl']) for t in trades)
        coins = [t['coin'] for t in trades]
        sides = set(t['side'] for t in trades)
        print(f'  {ts}: {len(trades)}t {w}W/{l}L pnl=${pnl:+.2f} coins={",".join(coins)} sides={sides}')

print()
print('=== ENTRY PRICE vs OUTCOME ===')
loss_prices = sorted([float(t['entry_price']) for t in today if t['outcome'] == 'lost'])
win_prices = sorted([float(t['entry_price']) for t in today if t['outcome'] == 'won'])
print(f'  Loss entries: {[f"${p:.2f}" for p in loss_prices]}')
print(f'  Win entries:  {[f"${p:.2f}" for p in win_prices]}')
if loss_prices:
    print(f'  Avg loss entry: ${sum(loss_prices)/len(loss_prices):.2f}')
if win_prices:
    print(f'  Avg win entry:  ${sum(win_prices)/len(win_prices):.2f}')

# Cross-reference with collector
print()
print('=== COLLECTOR CROSS-REFERENCE ===')
coll = []
with open('data/collector_v3.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        coll.append(row)

# Build collector index: (slug, side, threshold) -> outcome
coll_idx = {}
for r in coll:
    key = (r['market_slug'], r['side'], r.get('threshold', ''))
    if r.get('is_momentum_side') == 'True':
        coll_idx[(r['market_slug'], r['side'])] = r

match = 0
mismatch = 0
no_coll = 0
for t in today:
    slug = t['market_slug']
    side = t['side']
    key = (slug, side)
    c = coll_idx.get(key)
    if c:
        live_out = t['outcome']
        coll_out = c.get('outcome', '?')
        if live_out == coll_out:
            match += 1
        else:
            mismatch += 1
            print(f'  MISMATCH: {t["coin"]} {slug[-20:]} {side} live={live_out} coll={coll_out}')
            print(f'    live entry=${t["entry_price"]} coll entry=${c.get("entry_price","?")} coll_mom={c.get("momentum","?")}')
    else:
        no_coll += 1

print(f'  Matched outcomes: {match}, Mismatched: {mismatch}, No collector data: {no_coll}')

# Collector WR for today's same config (mom>=0.001, el>=150, el<=270, price<=0.80)
print()
print('=== COLLECTOR WR AT BOT CONFIG (mom>=0.001, el=150-270, price 0.30-0.80) ===')
coll_today = [r for r in coll if '2026-04-13' in r['timestamp'] and r.get('is_momentum_side') == 'True']
filtered = []
for r in coll_today:
    try:
        mom = float(r['momentum'])
        el = float(r['elapsed'])
        ep = float(r['entry_price'])
        thresh = float(r['threshold'])
    except Exception:
        continue
    if thresh == 0.001 and el >= 150 and el <= 270 and ep >= 0.30 and ep <= 0.80:
        filtered.append(r)

cw = sum(1 for r in filtered if r['outcome'] == 'won')
cl = sum(1 for r in filtered if r['outcome'] == 'lost')
if cw + cl > 0:
    print(f'  Collector filtered: {cw}W/{cl}L = {cw/(cw+cl)*100:.1f}% (n={cw+cl})')
else:
    print(f'  No matching signals (check if collector has data for today)')

# Also check without price filter (since 60% have 0.0)
filtered_no_price = []
for r in coll_today:
    try:
        mom = float(r['momentum'])
        el = float(r['elapsed'])
        thresh = float(r['threshold'])
    except Exception:
        continue
    if thresh == 0.001 and el >= 150 and el <= 270:
        filtered_no_price.append(r)

cw2 = sum(1 for r in filtered_no_price if r['outcome'] == 'won')
cl2 = sum(1 for r in filtered_no_price if r['outcome'] == 'lost')
if cw2 + cl2 > 0:
    print(f'  Collector (no price filter): {cw2}W/{cl2}L = {cw2/(cw2+cl2)*100:.1f}% (n={cw2+cl2})')

# All thresholds >= 0.001
all_mom = [r for r in coll_today if r.get('is_momentum_side') == 'True']
for thresh_str in ['0.001', '0.0012', '0.0015', '0.002']:
    t_signals = [r for r in all_mom if r.get('threshold') == thresh_str]
    tw = sum(1 for r in t_signals if r['outcome'] == 'won')
    tl = sum(1 for r in t_signals if r['outcome'] == 'lost')
    if tw + tl > 0:
        print(f'  thresh={thresh_str}: {tw}W/{tl}L = {tw/(tw+tl)*100:.1f}% (n={tw+tl})')
