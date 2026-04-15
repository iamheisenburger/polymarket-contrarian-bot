#!/usr/bin/env python3
import csv

slugs = [
    ('doge-updown-5m-1776174000', 'DOGE', 'up', 'WON'),
    ('xrp-updown-5m-1776174300', 'XRP', 'down', 'LOST'),
    ('xrp-updown-5m-1776174900', 'XRP', 'down', 'LOST'),
    ('hype-updown-5m-1776175200', 'HYPE', 'down', 'WON'),
    ('doge-updown-5m-1776175500', 'DOGE', 'up', 'LOST'),
    ('doge-updown-5m-1776175800', 'DOGE', 'down', 'LOST'),
]

with open('data/collector_v3.csv') as f:
    rows = list(csv.DictReader(f))

for slug, coin, side, live_result in slugs:
    matches = [r for r in rows if r['market_slug'] == slug and r['coin'] == coin
               and r['side'] == side and r.get('is_momentum_side') == 'True'
               and float(r.get('threshold', 0)) >= 0.001]
    if matches:
        for m in matches:
            ep = float(m['entry_price'])
            el = float(m['elapsed'])
            th = float(m['threshold'])
            coll_out = m['outcome']
            match_str = "MATCH" if coll_out == live_result.lower() else "MISMATCH"
            print(f'{coin:4s} {side:4s} {slug[-15:]} | coll=${ep:.2f} el={el:.0f}s coll={coll_out} live={live_result} [{match_str}]')
    else:
        print(f'{coin:4s} {side:4s} {slug[-15:]} | NOT IN COLLECTOR at mom>=0.001')
