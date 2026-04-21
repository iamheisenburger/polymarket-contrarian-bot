#!/usr/bin/env python3
"""
Deep Adverse Selection Analysis
================================
Cross-references live fills against collector signals to understand
WHY we fill bad signals and miss good ones.

Key question: Is adverse selection (AS) a universal tax, or concentrated
in specific conditions we can avoid?
"""
import pandas as pd
import numpy as np
from collections import defaultdict
import sys

pd.set_option('display.max_columns', 50)
pd.set_option('display.width', 200)
pd.set_option('display.float_format', '{:.4f}'.format)

# ─── Load data ───────────────────────────────────────────────────────
live = pd.read_csv('data/live_v30_vps.csv')
cv3 = pd.read_csv('data/collector_v3_full.csv')

print(f"Live trades: {len(live)} | Collector signals: {len(cv3)}")
print(f"Live time: {live.timestamp.min()} → {live.timestamp.max()}")
print(f"CV3 time:  {cv3.timestamp.min()} → {cv3.timestamp.max()}")
print()

# ─── Prepare live data ──────────────────────────────────────────────
# live has time_to_expiry_at_entry (TTE) and momentum_at_entry
# cv3 has elapsed and momentum and threshold
# elapsed = 300 - TTE for 5m markets
live['elapsed'] = 300 - live['time_to_expiry_at_entry']
live['momentum'] = live['momentum_at_entry'].abs()
live['won'] = (live['outcome'] == 'won').astype(int)

# ─── Prepare collector data ─────────────────────────────────────────
# Only keep momentum_side=True (the side the bot would actually trade)
cv3_mom = cv3[cv3['is_momentum_side'] == True].copy()
cv3_mom['won'] = (cv3_mom['outcome'] == 'won').astype(int)
cv3_mom['momentum'] = cv3_mom['momentum'].abs()

print(f"CV3 momentum-side signals: {len(cv3_mom)}")
print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 1: Match live trades to collector signals
# For each live trade, find the matching CV3 signal (same market, same side,
# closest threshold to the live momentum)
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 1: MATCHING LIVE TRADES TO COLLECTOR SIGNALS")
print("=" * 80)

# For matching, group CV3 by market_slug + side
cv3_by_market = cv3_mom.groupby(['market_slug', 'side'])

matched = []
unmatched_markets = []

for _, trade in live.iterrows():
    slug = trade['market_slug']
    side = trade['side']
    key = (slug, side)

    if key in cv3_by_market.groups:
        signals = cv3_by_market.get_group(key)
        # Find signal with closest threshold to live momentum
        signals = signals.copy()
        signals['mom_diff'] = (signals['momentum'] - trade['momentum']).abs()
        best = signals.loc[signals['mom_diff'].idxmin()]

        matched.append({
            'market_slug': slug,
            'coin': trade['coin'],
            'side': side,
            'live_entry': trade['entry_price'],
            'live_elapsed': trade['elapsed'],
            'live_momentum': trade['momentum'],
            'live_outcome': trade['outcome'],
            'live_won': trade['won'],
            'live_latency': trade['total_latency_ms'],
            'ic_entry': best['entry_price'],
            'ic_momentum': best['momentum'],
            'ic_elapsed': best['elapsed'],
            'ic_outcome': best['outcome'],
            'ic_won': best['won'],
            'ic_threshold': best['threshold'],
        })
    else:
        unmatched_markets.append(slug)

matched_df = pd.DataFrame(matched)
print(f"Matched: {len(matched_df)} / {len(live)} live trades")
print(f"Unmatched: {len(unmatched_markets)} (market not in collector)")
print()

if len(matched_df) > 0:
    print(f"Live WR (matched): {matched_df['live_won'].mean():.1%}")
    print(f"IC WR (same markets): {matched_df['ic_won'].mean():.1%}")
    outcome_match = (matched_df['live_outcome'] == matched_df['ic_outcome']).mean()
    print(f"Outcome alignment: {outcome_match:.1%}")
    print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 2: THE CORE AS QUESTION
# For markets where we had a collector signal, what was the WR of
# signals we FILLED vs signals we MISSED?
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 2: FILLED vs MISSED SIGNALS (THE CORE AS QUESTION)")
print("=" * 80)

# Get live markets (slug + side combos that we actually traded)
live_traded = set(zip(live['market_slug'], live['side']))

# For each CV3 signal at specific thresholds that would trigger the bot
for thresh in [0.0003, 0.0005, 0.0007, 0.001, 0.0012, 0.0015]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh].copy()

    # Mark which signals were filled live
    cv3_t['filled'] = cv3_t.apply(
        lambda r: (r['market_slug'], r['side']) in live_traded, axis=1
    )

    filled = cv3_t[cv3_t['filled']]
    missed = cv3_t[~cv3_t['filled']]

    if len(filled) >= 5 and len(missed) >= 5:
        print(f"\nThreshold {thresh}:")
        print(f"  Filled:  n={len(filled):>4}, WR={filled['won'].mean():.1%}, "
              f"avg_entry=${filled['entry_price'].mean():.2f}, avg_elapsed={filled['elapsed'].mean():.0f}s")
        print(f"  Missed:  n={len(missed):>4}, WR={missed['won'].mean():.1%}, "
              f"avg_entry=${missed['entry_price'].mean():.2f}, avg_elapsed={missed['elapsed'].mean():.0f}s")
        print(f"  AS gap:  {filled['won'].mean() - missed['won'].mean():+.1%}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 3: ENTRY PRICE AS ANALYSIS
# Is AS worse at certain price levels?
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 3: ADVERSE SELECTION BY ENTRY PRICE")
print("=" * 80)

# Use threshold 0.001 as reference (closest to live configs)
for thresh in [0.0005, 0.001]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh].copy()
    cv3_t['filled'] = cv3_t.apply(
        lambda r: (r['market_slug'], r['side']) in live_traded, axis=1
    )

    bins = [0, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90, 1.0]
    labels = ['0-0.30', '0.30-0.40', '0.40-0.50', '0.50-0.55', '0.55-0.60',
              '0.60-0.65', '0.65-0.70', '0.70-0.80', '0.80-0.90', '0.90-1.0']
    cv3_t['price_bin'] = pd.cut(cv3_t['entry_price'], bins=bins, labels=labels, right=False)

    print(f"\nThreshold: {thresh}")
    print(f"{'Price Bin':<12} {'Filled_n':>8} {'Filled_WR':>10} {'Missed_n':>8} {'Missed_WR':>10} {'AS_Gap':>8} {'Fill%':>6}")
    print("-" * 70)

    for pbin in labels:
        subset = cv3_t[cv3_t['price_bin'] == pbin]
        f = subset[subset['filled']]
        m = subset[~subset['filled']]

        if len(f) >= 3 or len(m) >= 3:
            f_wr = f['won'].mean() if len(f) > 0 else float('nan')
            m_wr = m['won'].mean() if len(m) > 0 else float('nan')
            gap = f_wr - m_wr if not (np.isnan(f_wr) or np.isnan(m_wr)) else float('nan')
            fill_pct = len(f) / len(subset) if len(subset) > 0 else 0

            gap_str = f"{gap:+.1%}" if not np.isnan(gap) else "N/A"
            f_wr_str = f"{f_wr:.1%}" if not np.isnan(f_wr) else "N/A"
            m_wr_str = f"{m_wr:.1%}" if not np.isnan(m_wr) else "N/A"

            print(f"{pbin:<12} {len(f):>8} {f_wr_str:>10} {len(m):>8} {m_wr_str:>10} {gap_str:>8} {fill_pct:>5.0%}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 4: ELAPSED TIME AS ANALYSIS
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 4: ADVERSE SELECTION BY ELAPSED TIME")
print("=" * 80)

for thresh in [0.0005, 0.001]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh].copy()
    cv3_t['filled'] = cv3_t.apply(
        lambda r: (r['market_slug'], r['side']) in live_traded, axis=1
    )

    bins = [0, 60, 120, 150, 180, 210, 240, 270, 300]
    labels = ['0-60', '60-120', '120-150', '150-180', '180-210', '210-240', '240-270', '270-300']
    cv3_t['elapsed_bin'] = pd.cut(cv3_t['elapsed'], bins=bins, labels=labels, right=False)

    print(f"\nThreshold: {thresh}")
    print(f"{'Elapsed':<12} {'Filled_n':>8} {'Filled_WR':>10} {'Missed_n':>8} {'Missed_WR':>10} {'AS_Gap':>8}")
    print("-" * 62)

    for ebin in labels:
        subset = cv3_t[cv3_t['elapsed_bin'] == ebin]
        f = subset[subset['filled']]
        m = subset[~subset['filled']]

        if len(f) >= 3 or len(m) >= 3:
            f_wr = f['won'].mean() if len(f) > 0 else float('nan')
            m_wr = m['won'].mean() if len(m) > 0 else float('nan')
            gap = f_wr - m_wr if not (np.isnan(f_wr) or np.isnan(m_wr)) else float('nan')

            gap_str = f"{gap:+.1%}" if not np.isnan(gap) else "N/A"
            f_wr_str = f"{f_wr:.1%}" if not np.isnan(f_wr) else "N/A"
            m_wr_str = f"{m_wr:.1%}" if not np.isnan(m_wr) else "N/A"

            print(f"{ebin:<12} {len(f):>8} {f_wr_str:>10} {len(m):>8} {m_wr_str:>10} {gap_str:>8}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 5: PER-COIN AS ANALYSIS
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 5: ADVERSE SELECTION BY COIN")
print("=" * 80)

for thresh in [0.0005, 0.001]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh].copy()
    cv3_t['filled'] = cv3_t.apply(
        lambda r: (r['market_slug'], r['side']) in live_traded, axis=1
    )

    print(f"\nThreshold: {thresh}")
    print(f"{'Coin':<8} {'Filled_n':>8} {'Filled_WR':>10} {'Missed_n':>8} {'Missed_WR':>10} {'AS_Gap':>8}")
    print("-" * 55)

    for coin in sorted(cv3_t['coin'].unique()):
        subset = cv3_t[cv3_t['coin'] == coin]
        f = subset[subset['filled']]
        m = subset[~subset['filled']]

        f_wr = f['won'].mean() if len(f) > 0 else float('nan')
        m_wr = m['won'].mean() if len(m) > 0 else float('nan')
        gap = f_wr - m_wr if not (np.isnan(f_wr) or np.isnan(m_wr)) else float('nan')

        gap_str = f"{gap:+.1%}" if not np.isnan(gap) else "N/A"
        f_wr_str = f"{f_wr:.1%}" if not np.isnan(f_wr) else "N/A"
        m_wr_str = f"{m_wr:.1%}" if not np.isnan(m_wr) else "N/A"

        print(f"{coin:<8} {len(f):>8} {f_wr_str:>10} {len(m):>8} {m_wr_str:>10} {gap_str:>8}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 6: BOOK DEPTH PROXY
# entry_price is a proxy for book depth — prices near 0.50 have thinnest
# books (most competition). Prices near 0 or 1 have deepest books.
# The hypothesis: AS is worst where books are thin.
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 6: BOOK DEPTH PROXY (DISTANCE FROM 0.50)")
print("=" * 80)

for thresh in [0.0005, 0.001]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh].copy()
    cv3_t['filled'] = cv3_t.apply(
        lambda r: (r['market_slug'], r['side']) in live_traded, axis=1
    )
    cv3_t['dist_from_50'] = (cv3_t['entry_price'] - 0.50).abs()

    bins = [0, 0.05, 0.10, 0.15, 0.20, 0.30, 0.50]
    labels = ['0-0.05', '0.05-0.10', '0.10-0.15', '0.15-0.20', '0.20-0.30', '0.30-0.50']
    cv3_t['dist_bin'] = pd.cut(cv3_t['dist_from_50'], bins=bins, labels=labels, right=False)

    print(f"\nThreshold: {thresh}")
    print(f"{'Dist 0.50':<12} {'Filled_n':>8} {'Filled_WR':>10} {'Missed_n':>8} {'Missed_WR':>10} {'AS_Gap':>8} {'Avg Price':>10}")
    print("-" * 72)

    for dbin in labels:
        subset = cv3_t[cv3_t['dist_bin'] == dbin]
        f = subset[subset['filled']]
        m = subset[~subset['filled']]

        if len(f) >= 2 or len(m) >= 2:
            f_wr = f['won'].mean() if len(f) > 0 else float('nan')
            m_wr = m['won'].mean() if len(m) > 0 else float('nan')
            gap = f_wr - m_wr if not (np.isnan(f_wr) or np.isnan(m_wr)) else float('nan')
            avg_price = subset['entry_price'].mean()

            gap_str = f"{gap:+.1%}" if not np.isnan(gap) else "N/A"
            f_wr_str = f"{f_wr:.1%}" if not np.isnan(f_wr) else "N/A"
            m_wr_str = f"{m_wr:.1%}" if not np.isnan(m_wr) else "N/A"

            print(f"{dbin:<12} {len(f):>8} {f_wr_str:>10} {len(m):>8} {m_wr_str:>10} {gap_str:>8} {avg_price:>10.2f}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 7: LIVE TRADE DEEP DIVE
# Analyze actual live trade performance by dimensions
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 7: LIVE TRADE PERFORMANCE (ACTUAL FILLS)")
print("=" * 80)

print("\n--- By Entry Price ---")
bins = [0, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90, 1.0]
labels = ['0-0.30', '0.30-0.40', '0.40-0.50', '0.50-0.55', '0.55-0.60',
          '0.60-0.65', '0.65-0.70', '0.70-0.80', '0.80-0.90', '0.90-1.0']
live['price_bin'] = pd.cut(live['entry_price'], bins=bins, labels=labels, right=False)

print(f"{'Price Bin':<12} {'n':>6} {'WR':>8} {'Avg PnL':>10} {'Total PnL':>10} {'Avg Latency':>12}")
print("-" * 62)
for pbin in labels:
    subset = live[live['price_bin'] == pbin]
    if len(subset) >= 1:
        wr = subset['won'].mean()
        avg_pnl = subset['pnl'].mean()
        total_pnl = subset['pnl'].sum()
        avg_lat = subset['total_latency_ms'].mean()
        print(f"{pbin:<12} {len(subset):>6} {wr:>7.1%} {avg_pnl:>+10.2f} {total_pnl:>+10.2f} {avg_lat:>11.0f}ms")

print("\n--- By Elapsed Time ---")
bins = [0, 60, 120, 150, 180, 210, 240, 270, 300]
labels = ['0-60', '60-120', '120-150', '150-180', '180-210', '210-240', '240-270', '270-300']
live['elapsed_bin'] = pd.cut(live['elapsed'], bins=bins, labels=labels, right=False)

print(f"{'Elapsed':<12} {'n':>6} {'WR':>8} {'Avg PnL':>10} {'Total PnL':>10}")
print("-" * 50)
for ebin in labels:
    subset = live[live['elapsed_bin'] == ebin]
    if len(subset) >= 1:
        wr = subset['won'].mean()
        avg_pnl = subset['pnl'].mean()
        total_pnl = subset['pnl'].sum()
        print(f"{ebin:<12} {len(subset):>6} {wr:>7.1%} {avg_pnl:>+10.2f} {total_pnl:>+10.2f}")

print("\n--- By Momentum ---")
bins = [0, 0.0005, 0.001, 0.0015, 0.002, 0.003, 0.005, 0.01, 1.0]
labels = ['0-0.05%', '0.05-0.1%', '0.1-0.15%', '0.15-0.2%', '0.2-0.3%', '0.3-0.5%', '0.5-1%', '1%+']
live['mom_bin'] = pd.cut(live['momentum'], bins=bins, labels=labels, right=False)

print(f"{'Momentum':<12} {'n':>6} {'WR':>8} {'Avg PnL':>10} {'Avg Entry':>10}")
print("-" * 50)
for mbin in labels:
    subset = live[live['mom_bin'] == mbin]
    if len(subset) >= 1:
        wr = subset['won'].mean()
        avg_pnl = subset['pnl'].mean()
        avg_entry = subset['entry_price'].mean()
        print(f"{mbin:<12} {len(subset):>6} {wr:>7.1%} {avg_pnl:>+10.2f} {avg_entry:>10.2f}")

print("\n--- By Coin ---")
print(f"{'Coin':<8} {'n':>6} {'WR':>8} {'Total PnL':>10} {'Avg Entry':>10} {'Avg Latency':>12}")
print("-" * 58)
for coin in sorted(live['coin'].unique()):
    subset = live[live['coin'] == coin]
    wr = subset['won'].mean()
    total_pnl = subset['pnl'].sum()
    avg_entry = subset['entry_price'].mean()
    avg_lat = subset['total_latency_ms'].mean()
    print(f"{coin:<8} {len(subset):>6} {wr:>7.1%} {total_pnl:>+10.2f} {avg_entry:>10.2f} {avg_lat:>11.0f}ms")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 8: THE SCOOPING MECHANISM
# When momentum fires, the entry_price tells us where the book was.
# Compare IC entry_price to live entry_price for the SAME market.
# If live entry > IC entry, we paid more = the book got lifted before us.
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 8: PRICE SLIPPAGE (IC ENTRY vs LIVE ENTRY)")
print("=" * 80)

if len(matched_df) > 0:
    matched_df['slippage'] = matched_df['live_entry'] - matched_df['ic_entry']
    # Positive slippage = we paid more than IC saw

    print(f"\nTotal matched: {len(matched_df)}")
    print(f"Mean slippage: {matched_df['slippage'].mean():+.4f}")
    print(f"Median slippage: {matched_df['slippage'].median():+.4f}")
    print(f"Slippage > 0 (we paid more): {(matched_df['slippage'] > 0).sum()} ({(matched_df['slippage'] > 0).mean():.1%})")
    print(f"Slippage = 0 (same price): {(matched_df['slippage'] == 0).sum()} ({(matched_df['slippage'] == 0).mean():.1%})")
    print(f"Slippage < 0 (we paid less): {(matched_df['slippage'] < 0).sum()} ({(matched_df['slippage'] < 0).mean():.1%})")

    # WR by slippage direction
    for label, mask in [('Paid more', matched_df['slippage'] > 0),
                         ('Same price', matched_df['slippage'] == 0),
                         ('Paid less', matched_df['slippage'] < 0)]:
        subset = matched_df[mask]
        if len(subset) >= 2:
            print(f"  {label}: n={len(subset)}, WR={subset['live_won'].mean():.1%}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 9: LATENCY AS ANALYSIS
# Does order latency predict outcome?
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 9: LATENCY vs OUTCOME")
print("=" * 80)

bins = [0, 200, 300, 400, 500, 1000, 2000, 5000]
labels = ['<200ms', '200-300', '300-400', '400-500', '500-1000', '1000-2000', '2000+']
live['lat_bin'] = pd.cut(live['total_latency_ms'], bins=bins, labels=labels, right=False)

print(f"{'Latency':<12} {'n':>6} {'WR':>8} {'Avg Entry':>10} {'Avg Mom':>10}")
print("-" * 50)
for lbin in labels:
    subset = live[live['lat_bin'] == lbin]
    if len(subset) >= 1:
        wr = subset['won'].mean()
        avg_entry = subset['entry_price'].mean()
        avg_mom = subset['momentum'].mean()
        print(f"{lbin:<12} {len(subset):>6} {wr:>7.1%} {avg_entry:>10.2f} {avg_mom:>10.5f}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 10: THE REAL FILL RATE
# For each market the bot was running during, how many IC signals
# did the bot generate vs actually fill?
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 10: FILL RATE ANALYSIS")
print("=" * 80)

# Get unique markets we traded
live_markets_set = set(live['market_slug'].unique())

# For each threshold, count how many unique markets had signals
# and how many of those we actually traded
for thresh in [0.0003, 0.0005, 0.0007, 0.001, 0.0015]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh]
    signal_markets = set(cv3_t['market_slug'].unique())

    # Markets where BOTH had activity
    overlap = signal_markets & live_markets_set
    # But live_markets_set is all markets we traded, not filtered by threshold
    # Better: of markets with signals at this threshold, how many did we trade?
    traded_with_signal = len(overlap)
    total_signals = len(signal_markets)

    print(f"Threshold {thresh}: {total_signals} markets with signals, "
          f"{traded_with_signal} we also traded ({traded_with_signal/total_signals:.1%} overlap)")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 11: TEMPORAL PATTERN
# Are there time-of-day effects in AS?
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 11: TIME-OF-DAY EFFECTS")
print("=" * 80)

live['ts'] = pd.to_datetime(live['timestamp'])
live['hour'] = live['ts'].dt.hour

print(f"{'Hour (UTC)':<12} {'n':>6} {'WR':>8} {'Avg Entry':>10} {'Total PnL':>10}")
print("-" * 50)
for hour in sorted(live['hour'].unique()):
    subset = live[live['hour'] == hour]
    if len(subset) >= 2:
        wr = subset['won'].mean()
        avg_entry = subset['entry_price'].mean()
        total_pnl = subset['pnl'].sum()
        print(f"  {hour:02d}:00     {len(subset):>6} {wr:>7.1%} {avg_entry:>10.2f} {total_pnl:>+10.2f}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 12: EXPECTED VALUE BY CONFIG
# For each (entry_bin x elapsed_bin x momentum_bin), what's the live EV?
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 12: MULTIDIMENSIONAL EV (ENTRY x ELAPSED)")
print("=" * 80)

price_bins = [0, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 1.0]
price_labels = ['<0.50', '0.50-0.55', '0.55-0.60', '0.60-0.65', '0.65-0.70', '0.70-0.80', '0.80+']
elapsed_bins = [0, 120, 150, 180, 210, 240, 300]
elapsed_labels = ['<120s', '120-150', '150-180', '180-210', '210-240', '240+']

live['pbin2'] = pd.cut(live['entry_price'], bins=price_bins, labels=price_labels, right=False)
live['ebin2'] = pd.cut(live['elapsed'], bins=elapsed_bins, labels=elapsed_labels, right=False)

print(f"{'Entry':<12} {'Elapsed':<10} {'n':>5} {'WR':>8} {'AvgPnL':>8} {'TotPnL':>8}")
print("-" * 55)
for pbin in price_labels:
    for ebin in elapsed_labels:
        subset = live[(live['pbin2'] == pbin) & (live['ebin2'] == ebin)]
        if len(subset) >= 3:
            wr = subset['won'].mean()
            avg_pnl = subset['pnl'].mean()
            total_pnl = subset['pnl'].sum()
            marker = " <<<" if avg_pnl > 0 else ""
            print(f"{pbin:<12} {ebin:<10} {len(subset):>5} {wr:>7.1%} {avg_pnl:>+7.2f} {total_pnl:>+7.2f}{marker}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 13: IC BASELINE — WHAT THE STRATEGY ACTUALLY DOES
# Show IC WR by the same dimensions to compare with live
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 13: IC BASELINE (WHAT THE STRATEGY DOES WITHOUT AS)")
print("=" * 80)

for thresh in [0.0005, 0.001]:
    cv3_t = cv3_mom[cv3_mom['threshold'] == thresh].copy()

    # Filter to common entry price / elapsed ranges
    print(f"\nThreshold: {thresh}")

    bins = [0, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90, 1.0]
    labels = ['0-0.30', '0.30-0.40', '0.40-0.50', '0.50-0.55', '0.55-0.60',
              '0.60-0.65', '0.65-0.70', '0.70-0.80', '0.80-0.90', '0.90-1.0']
    cv3_t['price_bin'] = pd.cut(cv3_t['entry_price'], bins=bins, labels=labels, right=False)

    print(f"{'Price Bin':<12} {'n':>6} {'WR':>8} {'Avg Mom':>10} {'Avg Elapsed':>12}")
    print("-" * 52)
    for pbin in labels:
        subset = cv3_t[cv3_t['price_bin'] == pbin]
        if len(subset) >= 3:
            wr = subset['won'].mean()
            avg_mom = subset['momentum'].mean()
            avg_el = subset['elapsed'].mean()
            # Expected PnL per $1 bet at this entry price
            # Win: get (1 - entry_price) profit. Lose: lose entry_price.
            avg_entry = subset['entry_price'].mean()
            ev = wr * (1 - avg_entry) - (1 - wr) * avg_entry
            print(f"{pbin:<12} {len(subset):>6} {wr:>7.1%} {avg_mom:>10.5f} {avg_el:>11.1f}s  EV={ev:+.3f}")

print()

# ═══════════════════════════════════════════════════════════════════════
# SECTION 14: SUMMARY & RECOMMENDATIONS
# ═══════════════════════════════════════════════════════════════════════
print("=" * 80)
print("SECTION 14: SUMMARY")
print("=" * 80)

total_live = len(live)
live_wr = live['won'].mean()
live_pnl = live['pnl'].sum()

print(f"Total live trades: {total_live}")
print(f"Live WR: {live_wr:.1%}")
print(f"Total PnL: ${live_pnl:+.2f}")
print(f"Avg entry price: ${live['entry_price'].mean():.2f}")
print(f"Avg latency: {live['total_latency_ms'].mean():.0f}ms")
print()

# Identify the best and worst buckets
print("PROFITABLE BUCKETS (n >= 5):")
for pbin in price_labels:
    for ebin in elapsed_labels:
        subset = live[(live['pbin2'] == pbin) & (live['ebin2'] == ebin)]
        if len(subset) >= 5 and subset['pnl'].mean() > 0:
            print(f"  {pbin} x {ebin}: n={len(subset)}, WR={subset['won'].mean():.1%}, "
                  f"PnL=${subset['pnl'].sum():+.2f}")

print("\nLOSING BUCKETS (n >= 5):")
for pbin in price_labels:
    for ebin in elapsed_labels:
        subset = live[(live['pbin2'] == pbin) & (live['ebin2'] == ebin)]
        if len(subset) >= 5 and subset['pnl'].mean() < 0:
            print(f"  {pbin} x {ebin}: n={len(subset)}, WR={subset['won'].mean():.1%}, "
                  f"PnL=${subset['pnl'].sum():+.2f}")
