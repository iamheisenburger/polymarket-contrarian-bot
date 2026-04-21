#!/usr/bin/env python3
"""
Config Sweep V2 — Full Mandatory Backtest Process
===================================================
All permutations with BTC block filter, coin exclusions, and strict risk model.

Growth Model: daily_EV = (sim_trades / IC_hours) × 24 × EV_per_trade
Risk Model: Full trade-by-trade simulation at $25. Reject if ANY fail:
  - Balance drops below $1 (bust)
  - Min balance < 40% of start ($10)
  - Max drawdown from peak > 30%
  - Worst single window PnL > 50% of start ($12.50)

Usage:
    python scripts/config_sweep_v2.py --balance 25 --ic-file data/vps_ic_latest.csv
"""
import argparse
import pandas as pd
import numpy as np
from numba import njit
from numba.typed import List as NumbaList
import time
import sys


def load_and_prepare(ic_path: str):
    """Load IC data, deduplicate, extract arrays."""
    ic = pd.read_csv(ic_path)

    # Filter: momentum side only, valid outcomes
    ic_mom = ic[(ic['is_momentum_side'] == True) & (ic['outcome'].isin(['won', 'lost']))].copy()
    ic_mom = ic_mom.sort_values('timestamp')

    # Extract window ID from slug
    ic_mom['slug_ts'] = ic_mom['market_slug'].str.extract(r'(\d+)$').astype(float).astype(int)

    # Deduplicate: first signal per coin per side per market window
    ic_mom = ic_mom.drop_duplicates(subset=['market_slug', 'coin', 'side'], keep='first')

    # Time span
    ic['ts'] = pd.to_datetime(ic['timestamp'])
    hours = (ic['ts'].max() - ic['ts'].min()).total_seconds() / 3600

    # Coin encoding: BTC=0, ETH=1, SOL=2, XRP=3, DOGE=4, BNB=5, HYPE=6
    coin_map = {'BTC': 0, 'ETH': 1, 'SOL': 2, 'XRP': 3, 'DOGE': 4, 'BNB': 5, 'HYPE': 6}
    ic_mom['coin_id'] = ic_mom['coin'].map(coin_map).fillna(7).astype(np.int64)

    # Side encoding: up=0, down=1
    ic_mom['side_id'] = (ic_mom['side'] == 'down').astype(np.int64)

    # For BTC block: need BTC's momentum direction and entry price per window
    # For each window, find BTC's state (if BTC has a signal)
    btc_signals = ic_mom[ic_mom['coin'] == 'BTC'][['slug_ts', 'momentum', 'entry_price', 'side']].copy()
    btc_signals = btc_signals.rename(columns={
        'momentum': 'btc_momentum',
        'entry_price': 'btc_entry',
        'side': 'btc_side'
    })
    # Take first BTC signal per window
    btc_signals = btc_signals.drop_duplicates(subset='slug_ts', keep='first')
    btc_signals['btc_dir'] = (btc_signals['btc_side'] == 'down').astype(np.int64)  # 0=up, 1=down

    # Merge BTC info into all signals
    ic_mom = ic_mom.merge(
        btc_signals[['slug_ts', 'btc_momentum', 'btc_entry', 'btc_dir']],
        on='slug_ts', how='left'
    )
    ic_mom['btc_momentum'] = ic_mom['btc_momentum'].fillna(0.0)
    ic_mom['btc_entry'] = ic_mom['btc_entry'].fillna(0.0)
    ic_mom['btc_dir'] = ic_mom['btc_dir'].fillna(-1).astype(np.int64)  # -1 = no BTC signal

    # Extract numpy arrays
    entry_prices = ic_mom['entry_price'].values.astype(np.float64)
    momentums = ic_mom['momentum'].values.astype(np.float64)
    elapsed_arr = ic_mom['elapsed'].values.astype(np.float64)
    won = (ic_mom['outcome'] == 'won').values.astype(np.bool_)
    window_ids = ic_mom['slug_ts'].values.astype(np.int64)
    coin_ids = ic_mom['coin_id'].values.astype(np.int64)
    side_ids = ic_mom['side_id'].values.astype(np.int64)
    btc_momentums = ic_mom['btc_momentum'].values.astype(np.float64)
    btc_entries = ic_mom['btc_entry'].values.astype(np.float64)
    btc_dirs = ic_mom['btc_dir'].values.astype(np.int64)

    return (entry_prices, momentums, elapsed_arr, won, window_ids,
            coin_ids, side_ids, btc_momentums, btc_entries, btc_dirs,
            hours, len(ic_mom), ic_mom)


@njit(cache=True)
def simulate_full(entry_prices, won, window_ids, coin_ids, side_ids,
                  btc_momentums, btc_entries, btc_dirs,
                  mask, balance, max_conc,
                  btc_block_entry, btc_block_momentum):
    """
    Full simulation with BTC block filter and detailed tracking.
    Returns (final_bal, min_bal, peak_bal, max_dd, worst_window_pnl,
             total_trades, wins, max_consec_losses).
    """
    bal = balance
    min_bal = bal
    peak_bal = bal
    max_dd = 0.0
    total_trades = 0
    wins = 0
    consec_losses = 0
    max_consec_losses = 0
    worst_window_pnl = 0.0

    n = len(entry_prices)

    # Build filtered indices
    filtered = np.empty(n, dtype=np.int64)
    fcount = 0
    for i in range(n):
        if mask[i]:
            filtered[fcount] = i
            fcount += 1

    if fcount == 0:
        return bal, min_bal, peak_bal, max_dd, worst_window_pnl, 0, 0, 0

    # Process window by window
    current_window = window_ids[filtered[0]]
    window_count = 0
    window_pnl = 0.0

    for fi in range(fcount):
        idx = filtered[fi]
        w = window_ids[idx]

        if w != current_window:
            # Finalize previous window
            if window_pnl < worst_window_pnl:
                worst_window_pnl = window_pnl
            current_window = w
            window_count = 0
            window_pnl = 0.0

        if window_count >= max_conc:
            continue

        # BTC block filter: block alt trades disagreeing with BTC
        coin = coin_ids[idx]
        side = side_ids[idx]
        if btc_block_entry > 0 and coin != 0 and coin != 1:  # not BTC, not ETH
            btc_dir = btc_dirs[idx]
            btc_mom = btc_momentums[idx]
            btc_ent = btc_entries[idx]
            if btc_dir >= 0 and btc_mom >= btc_block_momentum and btc_ent >= btc_block_entry:
                if side != btc_dir:  # alt disagrees with BTC
                    continue

        cost = entry_prices[idx] * 5.0
        if cost > bal or cost < 0.50:
            continue

        window_count += 1
        total_trades += 1

        if won[idx]:
            profit = 5.0 - cost
            bal += profit
            wins += 1
            consec_losses = 0
            window_pnl += profit
        else:
            bal -= cost
            consec_losses += 1
            if consec_losses > max_consec_losses:
                max_consec_losses = consec_losses
            window_pnl -= cost

        if bal < min_bal:
            min_bal = bal
        if bal > peak_bal:
            peak_bal = bal

        dd = (peak_bal - bal) / peak_bal if peak_bal > 0 else 0.0
        if dd > max_dd:
            max_dd = dd

        if bal < 1.0:
            # Finalize worst window
            if window_pnl < worst_window_pnl:
                worst_window_pnl = window_pnl
            return bal, min_bal, peak_bal, max_dd, worst_window_pnl, total_trades, wins, max_consec_losses

    # Finalize last window
    if window_pnl < worst_window_pnl:
        worst_window_pnl = window_pnl

    return bal, min_bal, peak_bal, max_dd, worst_window_pnl, total_trades, wins, max_consec_losses


def run_sweep(entry_prices, momentums, elapsed_arr, won, window_ids,
              coin_ids, side_ids, btc_momentums, btc_entries, btc_dirs,
              hours, n_signals, balance, top_n=30):
    """Full permutation sweep."""

    # Variable ranges per spec
    min_entries = [0.05, 0.20, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65]
    max_entries = [0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
    min_moms = [0.0003, 0.0005, 0.0008, 0.001, 0.0012, 0.0015]
    min_els = [0, 30, 60, 90, 120, 150, 180]
    max_els = [210, 240, 270, 300]
    btc_block_entries = [0.0, 0.40, 0.50, 0.60, 0.70, 0.80]
    btc_block_moms = [0.0003, 0.0005, 0.001]

    # Coin exclusion sets: (name, excluded_coin_ids)
    # BTC=0, ETH=1, SOL=2, XRP=3, DOGE=4, BNB=5, HYPE=6
    coin_sets = [
        ("all7", set()),
        ("no_SOL", {2}),
        ("no_DOGE", {4}),
        ("no_SOL_DOGE", {2, 4}),
    ]

    max_conc = 7  # fixed per spec

    # Warm up numba
    dummy_mask = np.ones(len(entry_prices), dtype=np.bool_)
    simulate_full(entry_prices, won, window_ids, coin_ids, side_ids,
                  btc_momentums, btc_entries, btc_dirs,
                  dummy_mask, balance, 7, 0.0, 0.0)

    configs = []
    tested = 0
    busted = 0
    risk_rejected = 0

    t0 = time.time()

    total_combos = (len(min_entries) * len(max_entries) * len(min_moms) *
                    len(min_els) * len(max_els) * len(btc_block_entries) *
                    len(btc_block_moms) * len(coin_sets))
    print(f"Max theoretical combos: {total_combos:,} (many pruned)")

    for coin_name, excluded_coins in coin_sets:
        # Build coin mask once per coin set
        coin_mask = np.ones(len(entry_prices), dtype=np.bool_)
        for exc in excluded_coins:
            coin_mask &= (coin_ids != exc)

        for min_e in min_entries:
            for max_e in max_entries:
                if min_e >= max_e:
                    continue
                for min_mom in min_moms:
                    for min_el in min_els:
                        for max_el in max_els:
                            if min_el >= max_el:
                                continue

                            # Build base mask (vectorized)
                            base_mask = (
                                coin_mask &
                                (entry_prices >= min_e) & (entry_prices <= max_e) &
                                (momentums >= min_mom) &
                                (elapsed_arr >= min_el) & (elapsed_arr <= max_el)
                            )
                            n = base_mask.sum()
                            if n < 15:
                                continue

                            # Quick EV check before BTC block sweep
                            wr = won[base_mask].mean()
                            avg_e = entry_prices[base_mask].mean()
                            base_ev = wr * (5 - avg_e * 5) - (1 - wr) * avg_e * 5
                            if base_ev <= 0:
                                continue

                            for btc_be in btc_block_entries:
                                for btc_bm in btc_block_moms:
                                    # Skip redundant: if btc_be=0, only need one btc_bm
                                    if btc_be == 0 and btc_bm != 0.0003:
                                        continue

                                    tested += 1

                                    # Full simulation
                                    (final_bal, min_bal, peak_bal, max_dd,
                                     worst_win_pnl, sim_trades, sim_wins,
                                     max_consec) = simulate_full(
                                        entry_prices, won, window_ids,
                                        coin_ids, side_ids,
                                        btc_momentums, btc_entries, btc_dirs,
                                        base_mask, balance, max_conc,
                                        btc_be, btc_bm
                                    )

                                    if sim_trades < 15:
                                        continue

                                    # RISK FILTERS
                                    # 1. Never bust
                                    if final_bal < 1.0 or min_bal < 1.0:
                                        busted += 1
                                        continue

                                    # 2. Min balance >= 40% of start
                                    if min_bal < balance * 0.40:
                                        risk_rejected += 1
                                        continue

                                    # 3. Max drawdown < 30%
                                    if max_dd > 0.30:
                                        risk_rejected += 1
                                        continue

                                    # 4. Worst window < 50% of start
                                    if abs(worst_win_pnl) > balance * 0.50:
                                        risk_rejected += 1
                                        continue

                                    # GROWTH MODEL
                                    sim_wr = sim_wins / sim_trades if sim_trades > 0 else 0
                                    avg_entry = entry_prices[base_mask].mean()
                                    ev_trade = sim_wr * (5 - avg_entry * 5) - (1 - sim_wr) * avg_entry * 5
                                    trades_per_day = sim_trades / hours * 24
                                    daily_ev = trades_per_day * ev_trade

                                    configs.append({
                                        'coins': coin_name,
                                        'min_e': min_e, 'max_e': max_e,
                                        'min_mom': min_mom,
                                        'min_el': min_el, 'max_el': max_el,
                                        'btc_be': btc_be, 'btc_bm': btc_bm,
                                        'n_signals': int(n),
                                        'sim_trades': sim_trades,
                                        'sim_wr': sim_wr * 100,
                                        'avg_entry': avg_entry,
                                        'ev_trade': ev_trade,
                                        'trades_day': trades_per_day,
                                        'daily_ev': daily_ev,
                                        'final_bal': final_bal,
                                        'min_bal': min_bal,
                                        'peak_bal': peak_bal,
                                        'max_dd': max_dd * 100,
                                        'worst_window': worst_win_pnl,
                                        'max_consec_losses': max_consec,
                                    })

    elapsed_time = time.time() - t0

    print(f"\nSweep complete in {elapsed_time:.1f}s")
    print(f"Tested: {tested:,} configs with full simulation")
    print(f"Busted: {busted:,} configs (balance < $1)")
    print(f"Risk-rejected: {risk_rejected:,} configs (min_bal/DD/worst_window)")
    print(f"Survived: {len(configs):,} configs")
    print(f"Balance: ${balance:.2f}")
    print(f"IC data: {n_signals} deduped signals, {hours:.1f} hours")
    print()

    if not configs:
        print("NO CONFIGS SURVIVED.")
        return pd.DataFrame()

    df = pd.DataFrame(configs)
    df = df.sort_values('daily_ev', ascending=False)

    print(f"{'='*120}")
    print(f"TOP {top_n} BY DAILY EV (all passed risk filters)")
    print(f"{'='*120}")
    print(f"{'#':>3} {'Coins':<13} {'Entry':>13} {'Mom':>8} {'Elapsed':>10} {'BTC_blk':>10} | "
          f"{'n':>4} {'sim':>4} {'WR':>6} {'EV':>6} {'t/d':>5} {'$/d':>6} | "
          f"{'final':>7} {'min':>6} {'DD':>5} {'worst':>6} {'cL':>3}")
    print("-" * 120)

    for i, (_, r) in enumerate(df.head(top_n).iterrows()):
        entry = f"${r['min_e']:.2f}-${r['max_e']:.2f}"
        mom = f">={r['min_mom']:.4f}"
        el = f"{r['min_el']:.0f}-{r['max_el']:.0f}"
        btc = f"{r['btc_be']:.1f}/{r['btc_bm']:.4f}" if r['btc_be'] > 0 else "off"
        print(f"{i+1:>3} {r['coins']:<13} {entry:>13} {mom:>8} {el:>10} {btc:>10} | "
              f"{r['n_signals']:>4} {r['sim_trades']:>4} {r['sim_wr']:>5.1f}% ${r['ev_trade']:>.2f} {r['trades_day']:>4.1f} ${r['daily_ev']:>+5.1f} | "
              f"${r['final_bal']:>6.1f} ${r['min_bal']:>5.1f} {r['max_dd']:>4.1f}% ${r['worst_window']:>5.1f} {r['max_consec_losses']:>3.0f}")

    return df


def verify_config(ic_mom_df, config, balance, hours):
    """Detailed verification of a single config."""
    print(f"\n{'='*80}")
    print(f"VERIFICATION: {config}")
    print(f"{'='*80}")

    # Filter IC data to this config
    t = ic_mom_df.copy()
    t = t[(t['entry_price'] >= config['min_e']) & (t['entry_price'] <= config['max_e'])]
    t = t[t['momentum'] >= config['min_mom']]
    t = t[(t['elapsed'] >= config['min_el']) & (t['elapsed'] <= config['max_el'])]

    # Coin exclusion
    if config['coins'] == 'no_SOL':
        t = t[t['coin'] != 'SOL']
    elif config['coins'] == 'no_DOGE':
        t = t[t['coin'] != 'DOGE']
    elif config['coins'] == 'no_SOL_DOGE':
        t = t[~t['coin'].isin(['SOL', 'DOGE'])]

    t = t.sort_values('timestamp')
    t['won'] = (t['outcome'] == 'won').astype(int)

    # BTC block filter (approximate — apply in Python)
    if config['btc_be'] > 0:
        btc_sigs = t[t['coin'] == 'BTC'][['slug_ts', 'momentum', 'entry_price', 'side']].copy()
        btc_sigs = btc_sigs.drop_duplicates(subset='slug_ts', keep='first')
        btc_sigs = btc_sigs.rename(columns={'momentum': 'btc_blk_mom', 'entry_price': 'btc_blk_entry', 'side': 'btc_blk_side'})
        t = t.merge(btc_sigs[['slug_ts', 'btc_blk_mom', 'btc_blk_entry', 'btc_blk_side']], on='slug_ts', how='left')

        def btc_block(row):
            if row['coin'] in ('BTC', 'ETH'):
                return False
            if pd.isna(row.get('btc_blk_mom')):
                return False
            if row['btc_blk_mom'] >= config['btc_bm'] and row['btc_blk_entry'] >= config['btc_be']:
                btc_dir = row['btc_blk_side']
                if row['side'] != btc_dir:
                    return True
            return False

        blocked = t.apply(btc_block, axis=1)
        print(f"\nBTC block filter: blocked {blocked.sum()} trades ({blocked.mean():.1%})")
        blocked_trades = t[blocked]
        if len(blocked_trades) > 0:
            print(f"  Blocked WR: {blocked_trades.won.mean():.1%} (these would have been {'losses' if blocked_trades.won.mean() < 0.5 else 'wins'})")
        t = t[~blocked]

    print(f"\nFiltered signals: {len(t)}")
    print(f"WR: {t.won.mean():.1%}")
    print(f"Avg entry: ${t.entry_price.mean():.3f}")
    print(f"Avg momentum: {t.momentum.mean():.5f}")
    print(f"Avg elapsed: {t.elapsed.mean():.1f}s")

    # Window concurrency
    window_counts = t.groupby('slug_ts').size()
    print(f"\n--- Window Concurrency ---")
    for n_trades in sorted(window_counts.unique()):
        count = (window_counts == n_trades).sum()
        print(f"  {n_trades} trades: {count} windows")

    # Per-coin breakdown
    print(f"\n--- Per-Coin ---")
    print(f"{'Coin':<8} {'n':>5} {'WR':>7} {'AvgEntry':>9}")
    print("-" * 32)
    for coin in sorted(t.coin.unique()):
        s = t[t.coin == coin]
        print(f"{coin:<8} {len(s):>5} {s.won.mean():>6.1%} {s.entry_price.mean():>9.3f}")

    # Trade-by-trade simulation with full tracking
    bal = balance
    min_bal = bal
    peak_bal = bal
    max_dd = 0
    consec_losses = 0
    max_consec = 0
    trajectory = []
    window_pnls = {}

    current_window = None
    window_count = 0

    for i, (_, trade) in enumerate(t.iterrows()):
        w = trade['slug_ts']
        if w != current_window:
            current_window = w
            window_count = 0
            if w not in window_pnls:
                window_pnls[w] = {'pnl': 0, 'trades': 0, 'coins': []}

        if window_count >= 7:
            continue

        cost = trade['entry_price'] * 5.0
        if cost > bal or cost < 0.50:
            continue

        window_count += 1
        window_pnls[w]['trades'] += 1
        window_pnls[w]['coins'].append(trade['coin'])

        if trade['won']:
            profit = 5.0 - cost
            bal += profit
            consec_losses = 0
        else:
            bal -= cost
            consec_losses += 1
            max_consec = max(max_consec, consec_losses)

        window_pnls[w]['pnl'] += (5.0 - cost) if trade['won'] else -cost

        min_bal = min(min_bal, bal)
        peak_bal = max(peak_bal, bal)
        dd = (peak_bal - bal) / peak_bal if peak_bal > 0 else 0
        max_dd = max(max_dd, dd)

        trajectory.append(bal)

    # Balance trajectory (every 25 trades)
    print(f"\n--- Balance Trajectory ---")
    for j in range(0, len(trajectory), 25):
        print(f"  Trade {j+1:>4}: ${trajectory[j]:.2f}")
    if len(trajectory) > 0:
        print(f"  Trade {len(trajectory):>4}: ${trajectory[-1]:.2f} (final)")

    print(f"\n--- Risk Summary ---")
    print(f"  Final balance: ${bal:.2f}")
    print(f"  Min balance: ${min_bal:.2f} ({min_bal/balance:.1%} of start)")
    print(f"  Peak balance: ${peak_bal:.2f}")
    print(f"  Max drawdown: {max_dd:.1%}")
    print(f"  Max consecutive losses: {max_consec}")

    # Worst 5 windows
    wpnl = pd.DataFrame([
        {'window': w, 'pnl': d['pnl'], 'trades': d['trades'], 'coins': ','.join(d['coins'])}
        for w, d in window_pnls.items()
    ]).sort_values('pnl')

    print(f"\n--- Worst 5 Windows ---")
    for _, w in wpnl.head(5).iterrows():
        print(f"  Window {w['window']}: PnL=${w['pnl']:+.2f}, {w['trades']} trades, coins={w['coins']}")

    print(f"\n--- Best 5 Windows ---")
    for _, w in wpnl.tail(5).iterrows():
        print(f"  Window {w['window']}: PnL=${w['pnl']:+.2f}, {w['trades']} trades, coins={w['coins']}")

    # Consecutive loss distribution
    consec_runs = []
    run = 0
    for _, trade in t.iterrows():
        if trade['won']:
            if run > 0:
                consec_runs.append(run)
            run = 0
        else:
            run += 1
    if run > 0:
        consec_runs.append(run)

    if consec_runs:
        print(f"\n--- Consecutive Loss Distribution ---")
        for length in sorted(set(consec_runs)):
            count = consec_runs.count(length)
            print(f"  {length} in a row: {count} times")

    # Trades per day
    trades_per_day = len(trajectory) / hours * 24
    avg_entry = t.entry_price.mean()
    sim_wr = t.won.mean()
    ev_trade = sim_wr * (5 - avg_entry * 5) - (1 - sim_wr) * avg_entry * 5
    daily_ev = trades_per_day * ev_trade

    print(f"\n--- Growth Estimate ---")
    print(f"  Trades: {len(trajectory)} over {hours:.1f}h = {trades_per_day:.1f}/day")
    print(f"  EV/trade: ${ev_trade:+.3f}")
    print(f"  Daily EV (IC rate): ${daily_ev:+.2f}")
    print(f"  *** CAVEAT: Live fill rate is ~4-16%. Actual daily EV will be lower. ***")

    return {
        'max_consec': max_consec,
        'max_dd': max_dd,
        'min_bal': min_bal,
        'worst_window': wpnl.iloc[0]['pnl'] if len(wpnl) > 0 else 0,
    }


def cross_reference_live(live_path, config):
    """Run the config against actual live fill data."""
    print(f"\n{'='*80}")
    print(f"CROSS-REFERENCE: LIVE FILL DATA")
    print(f"{'='*80}")

    live = pd.read_csv(live_path)
    live['elapsed'] = 300 - live['time_to_expiry_at_entry']
    live['momentum'] = live['momentum_at_entry'].abs()
    live['won'] = (live['outcome'] == 'won').astype(int)

    # Apply config filters
    t = live.copy()
    t = t[(t['entry_price'] >= config['min_e']) & (t['entry_price'] <= config['max_e'])]
    t = t[t['momentum'] >= config['min_mom']]
    t = t[(t['elapsed'] >= config['min_el']) & (t['elapsed'] <= config['max_el'])]

    if config['coins'] == 'no_SOL':
        t = t[t['coin'] != 'SOL']
    elif config['coins'] == 'no_DOGE':
        t = t[t['coin'] != 'DOGE']
    elif config['coins'] == 'no_SOL_DOGE':
        t = t[~t['coin'].isin(['SOL', 'DOGE'])]

    print(f"\nLive trades matching config: {len(t)} / {len(live)}")

    if len(t) == 0:
        print("NO LIVE TRADES MATCH THIS CONFIG")
        return

    print(f"WR: {t.won.mean():.1%}")
    print(f"PnL: ${t.pnl.sum():+.2f}")
    print(f"Avg entry: ${t.entry_price.mean():.3f}")
    print(f"Avg elapsed: {t.elapsed.mean():.1f}s")

    # Breakeven analysis
    avg_e = t.entry_price.mean()
    edge = t.won.mean() - avg_e
    print(f"Breakeven WR: {avg_e:.1%}, Actual WR: {t.won.mean():.1%}, Edge: {edge:+.1%}")

    # Simulate on live fills
    bal = 25.0
    min_bal = 25.0
    for _, trade in t.sort_values('timestamp').iterrows():
        cost = trade['entry_price'] * 5.0
        if cost > bal or cost < 0.50:
            continue
        if trade['won']:
            bal += 5.0 - cost
        else:
            bal -= cost
        min_bal = min(min_bal, bal)

    print(f"\nLive simulation: $25 → ${bal:.2f}")
    print(f"Min balance: ${min_bal:.2f}")

    days = (pd.to_datetime(t.timestamp).max() - pd.to_datetime(t.timestamp).min()).total_seconds() / 86400
    print(f"Over {days:.1f} days = ${(bal - 25) / days:+.2f}/day")

    # Check if config falls in live-profitable zones
    print(f"\n--- Live-Profitable Zone Check ---")
    print(f"  Entry range ${config['min_e']:.2f}-${config['max_e']:.2f} vs live sweet spot $0.55-$0.65: ", end="")
    if config['min_e'] <= 0.55 and config['max_e'] >= 0.65:
        print("OVERLAPS")
    elif config['max_e'] <= 0.55 or config['min_e'] >= 0.65:
        print("NO OVERLAP - WARNING")
    else:
        print("PARTIAL OVERLAP")

    print(f"  Elapsed {config['min_el']}-{config['max_el']} vs live sweet spot 120+: ", end="")
    if config['min_el'] >= 120:
        print("MATCHES")
    else:
        print(f"INCLUDES EARLY TRADES - WARNING")

    print(f"  Momentum >={config['min_mom']} vs live sweet spot 0.0005-0.001: ", end="")
    if config['min_mom'] >= 0.0005:
        print("MATCHES")
    else:
        print("INCLUDES NOISE - WARNING")

    # Per-coin on live
    print(f"\n--- Live Per-Coin ---")
    for coin in sorted(t.coin.unique()):
        s = t[t.coin == coin]
        print(f"  {coin}: n={len(s)}, WR={s.won.mean():.1%}, PnL=${s.pnl.sum():+.2f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--balance", type=float, default=25.0)
    parser.add_argument("--ic-file", type=str, default="data/vps_ic_latest.csv")
    parser.add_argument("--live-file", type=str, default="data/vps_live_latest.csv")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--verify-rank", type=int, default=1, help="Verify the Nth ranked config")
    args = parser.parse_args()

    print("Loading IC data...")
    (entry_prices, momentums, elapsed_arr, won, window_ids,
     coin_ids, side_ids, btc_momentums, btc_entries, btc_dirs,
     hours, n_signals, ic_mom_df) = load_and_prepare(args.ic_file)

    print(f"Loaded {n_signals} deduped IC signals, {hours:.1f} hours\n")

    # Run sweep
    df = run_sweep(entry_prices, momentums, elapsed_arr, won, window_ids,
                   coin_ids, side_ids, btc_momentums, btc_entries, btc_dirs,
                   hours, n_signals, args.balance, args.top)

    if len(df) == 0:
        sys.exit(1)

    # Verify top config
    rank = min(args.verify_rank, len(df))
    winner = df.iloc[rank - 1]
    config = {
        'coins': winner['coins'],
        'min_e': winner['min_e'], 'max_e': winner['max_e'],
        'min_mom': winner['min_mom'],
        'min_el': winner['min_el'], 'max_el': winner['max_el'],
        'btc_be': winner['btc_be'], 'btc_bm': winner['btc_bm'],
    }

    risk_stats = verify_config(ic_mom_df, config, args.balance, hours)
    cross_reference_live(args.live_file, config)

    # Guardrail calibration
    print(f"\n{'='*80}")
    print(f"GUARDRAIL CALIBRATION")
    print(f"{'='*80}")
    print(f"  Consecutive losses: backtest max {risk_stats['max_consec']} + 2 = {risk_stats['max_consec'] + 2}")
    print(f"  Drawdown from peak: backtest max {risk_stats['max_dd']:.1%} + 5pp = {risk_stats['max_dd'] + 0.05:.1%}")
    print(f"  Balance floor: $5")
    print(f"\n  CLI flags:")
    print(f"    --max-consecutive-losses {risk_stats['max_consec'] + 2}")
    print(f"    --max-drawdown {risk_stats['max_dd'] + 0.05:.2f}")
    print(f"    --balance-floor 5")
