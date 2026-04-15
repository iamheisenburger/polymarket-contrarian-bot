#!/usr/bin/env python3
"""Check if the bot is executing the strategy correctly."""
import csv

live = []
with open('data/live_v30.csv') as f:
    reader = csv.DictReader(f)
    for row in reader:
        live.append(row)

today = [t for t in live if '2026-04-13' in t['timestamp']]

# Check: did the bot respect max-entry-price 0.80?
print('=== ENTRY PRICE CHECK (did bot respect $0.80 cap?) ===')
violations = 0
for t in today:
    ep = float(t['entry_price'])
    ts = t['timestamp'][11:19]
    coin = t['coin']
    side = t['side']
    outcome = t['outcome']
    pnl = float(t['pnl'])
    if ep > 0.80:
        violations += 1
        print(f'  VIOLATION: {ts} {coin} {side} entry=${ep:.2f} {outcome} pnl=${pnl:+.2f}')
if violations == 0:
    print('  All trades within $0.80 cap')
else:
    # How much did violations cost?
    v_pnl = sum(float(t['pnl']) for t in today if float(t['entry_price']) > 0.80)
    print(f'  {violations} violations, combined PnL: ${v_pnl:+.2f}')

print()

# Check: momentum at entry vs min_momentum 0.0010
print('=== MOMENTUM CHECK (did bot respect 0.0010 min?) ===')
mom_violations = 0
for t in today:
    mom = abs(float(t['momentum_at_entry']))
    if mom < 0.0009:  # small tolerance for float
        mom_violations += 1
        ts = t['timestamp'][11:19]
        coin = t['coin']
        print(f'  BELOW MIN: {ts} {coin} mom={mom:.6f}')
if mom_violations == 0:
    print('  All trades meet 0.0010 momentum minimum')

print()

# Check: elapsed window 150-270
print('=== ELAPSED CHECK (did bot respect 150-270s window?) ===')
el_violations = 0
for t in today:
    try:
        tte = int(float(t.get('time_to_expiry_at_entry', '0')))
    except Exception:
        tte = 0
    elapsed = 300 - tte
    if elapsed < 145 or elapsed > 275:  # small tolerance
        el_violations += 1
        ts = t['timestamp'][11:19]
        coin = t['coin']
        outcome = t['outcome']
        ep = float(t['entry_price'])
        print(f'  OUT: {ts} {coin} el={elapsed}s tte={tte}s entry=${ep:.2f} {outcome}')
if el_violations == 0:
    print('  All trades within 150-270s window')

print()

# Check: side matches momentum direction
print('=== SIDE CHECK (did bot pick correct momentum side?) ===')
side_violations = 0
for t in today:
    mom = float(t['momentum_at_entry'])
    side = t['side']
    expected = 'up' if mom > 0 else 'down'
    if side != expected:
        side_violations += 1
        ts = t['timestamp'][11:19]
        coin = t['coin']
        print(f'  WRONG SIDE: {ts} {coin} side={side} but mom={mom:+.6f} (should be {expected})')
if side_violations == 0:
    print('  All sides match momentum direction')

print()

# The key economics question
print('=== ECONOMICS: WHY 70% WR LOSES MONEY ===')
wins = [t for t in today if t['outcome'] == 'won']
losses = [t for t in today if t['outcome'] == 'lost']

total_pnl = sum(float(t['pnl']) for t in today)

avg_win_entry = sum(float(t['entry_price']) for t in wins) / len(wins)
avg_loss_entry = sum(float(t['entry_price']) for t in losses) / len(losses)
avg_win_profit = sum(float(t['pnl']) for t in wins) / len(wins)
avg_loss_amount = sum(abs(float(t['pnl'])) for t in losses) / len(losses)

print(f'  {len(wins)}W / {len(losses)}L = {len(wins)/(len(wins)+len(losses))*100:.1f}% WR')
print(f'  Total PnL: ${total_pnl:+.2f}')
print()
print(f'  Avg WIN entry price:  ${avg_win_entry:.2f}')
print(f'  Avg LOSS entry price: ${avg_loss_entry:.2f}')
print(f'  Avg profit per win:   ${avg_win_profit:+.2f}')
print(f'  Avg cost per loss:    ${avg_loss_amount:.2f}')
print(f'  Risk/reward ratio:    {avg_loss_amount/avg_win_profit:.1f}:1 (you risk ${avg_loss_amount:.2f} to make ${avg_win_profit:.2f})')
print()
be_wr = avg_loss_amount / (avg_loss_amount + avg_win_profit) * 100
print(f'  BREAKEVEN WR: {be_wr:.1f}%')
print(f'  ACTUAL WR:    {len(wins)/(len(wins)+len(losses))*100:.1f}%')
print(f'  GAP:          {len(wins)/(len(wins)+len(losses))*100 - be_wr:+.1f}pp')
print()

# Per-entry-price breakdown
print('=== PER-TRADE BREAKDOWN (every trade, sorted by entry price) ===')
all_trades = sorted(today, key=lambda t: float(t['entry_price']))
for t in all_trades:
    ep = float(t['entry_price'])
    coin = t['coin']
    side = t['side']
    outcome = t['outcome']
    pnl = float(t['pnl'])
    mom = abs(float(t['momentum_at_entry']))
    be = ep / (1) * 100  # breakeven WR for this entry
    print(f'  ${ep:.2f} {coin:4s} {side:4s} {outcome:4s} pnl=${pnl:+.2f} mom={mom:.4f} (need {be:.0f}% WR to break even at this price)')

# What would PnL be if we only traded below $0.70?
print()
print('=== HYPOTHETICAL: WHAT IF MAX ENTRY = $0.70? ===')
below70 = [t for t in today if float(t['entry_price']) <= 0.70]
w70 = sum(1 for t in below70 if t['outcome'] == 'won')
l70 = sum(1 for t in below70 if t['outcome'] == 'lost')
pnl70 = sum(float(t['pnl']) for t in below70)
if w70 + l70 > 0:
    print(f'  {w70}W/{l70}L = {w70/(w70+l70)*100:.1f}% WR, PnL=${pnl70:+.2f}')
    for t in below70:
        ep = float(t['entry_price'])
        print(f'    ${ep:.2f} {t["coin"]:4s} {t["outcome"]:4s} pnl=${float(t["pnl"]):+.2f}')
else:
    print('  No trades below $0.70')

print()
print('=== HYPOTHETICAL: WHAT IF MAX ENTRY = $0.75? ===')
below75 = [t for t in today if float(t['entry_price']) <= 0.75]
w75 = sum(1 for t in below75 if t['outcome'] == 'won')
l75 = sum(1 for t in below75 if t['outcome'] == 'lost')
pnl75 = sum(float(t['pnl']) for t in below75)
if w75 + l75 > 0:
    print(f'  {w75}W/{l75}L = {w75/(w75+l75)*100:.1f}% WR, PnL=${pnl75:+.2f}')
    for t in below75:
        ep = float(t['entry_price'])
        print(f'    ${ep:.2f} {t["coin"]:4s} {t["outcome"]:4s} pnl=${float(t["pnl"]):+.2f}')
else:
    print('  No trades below $0.75')
