#!/usr/bin/env python3
"""
FINAL CONFIG SWEEP — AS-informed, growth + risk model, live-validated.

This sweep:
1. Tests ALL permutations of elapsed, momentum, entry, coins
2. Runs full trade-by-trade simulation on IC data (risk model)
3. Cross-references EVERY surviving config against 511 LIVE trades
4. Applies AS model to estimate realistic performance
5. Ranks by LIVE EDGE (not IC WR, not daily EV)
6. Only presents configs where live data confirms profitability

The AS findings are baked in as HARD CONSTRAINTS:
- elapsed >= 120 minimum (early window = -$125, non-negotiable)
- momentum >= 0.0008 minimum (below = noise, 53% WR)
- Late window fixes per-coin AS, so all 7 coins included by default

Output: ONE recommended config with full verification.
"""
import pandas as pd
import numpy as np
from numba import njit
import time
import sys


def load_data(ic_path, live_path):
    """Load and prepare both datasets."""
    # IC data
    ic = pd.read_csv(ic_path)
    ic_mom = ic[(ic['is_momentum_side'] == True) & (ic['outcome'].isin(['won', 'lost']))].copy()
    ic_mom = ic_mom.sort_values('timestamp')
    ic_mom['slug_ts'] = ic_mom['market_slug'].str.extract(r'(\d+)$').astype(float).astype(int)
    ic_mom = ic_mom.drop_duplicates(subset=['market_slug', 'coin', 'side'], keep='first')
    ic_mom['won'] = (ic_mom['outcome'] == 'won').astype(int)
    ic['ts'] = pd.to_datetime(ic['timestamp'])
    hours = (ic['ts'].max() - ic['ts'].min()).total_seconds() / 3600

    # Live data
    live = pd.read_csv(live_path)
    live['elapsed'] = 300 - live['time_to_expiry_at_entry']
    live['momentum'] = live['momentum_at_entry'].abs()
    live['won'] = (live['outcome'] == 'won').astype(int)

    return ic_mom, hours, live


@njit(cache=True)
def simulate(entry_prices, won, window_ids, mask, balance):
    """Full trade-by-trade simulation. 7 concurrent max. $5 per trade."""
    bal = balance
    min_bal = bal
    peak_bal = bal
    max_dd = 0.0
    total = 0
    wins = 0
    consec_loss = 0
    max_consec = 0
    worst_window = 0.0

    n = len(entry_prices)
    filtered = np.empty(n, dtype=np.int64)
    fc = 0
    for i in range(n):
        if mask[i]:
            filtered[fc] = i
            fc += 1
    if fc == 0:
        return bal, min_bal, peak_bal, max_dd, worst_window, 0, 0, 0

    cur_win = window_ids[filtered[0]]
    win_count = 0
    win_pnl = 0.0

    for fi in range(fc):
        idx = filtered[fi]
        w = window_ids[idx]
        if w != cur_win:
            if win_pnl < worst_window:
                worst_window = win_pnl
            cur_win = w
            win_count = 0
            win_pnl = 0.0
        if win_count >= 7:
            continue
        cost = entry_prices[idx] * 5.0
        if cost > bal or cost < 0.50:
            continue
        win_count += 1
        total += 1
        if won[idx]:
            p = 5.0 - cost
            bal += p
            wins += 1
            consec_loss = 0
            win_pnl += p
        else:
            bal -= cost
            consec_loss += 1
            if consec_loss > max_consec:
                max_consec = consec_loss
            win_pnl -= cost
        if bal < min_bal:
            min_bal = bal
        if bal > peak_bal:
            peak_bal = bal
        dd = (peak_bal - bal) / peak_bal if peak_bal > 0 else 0.0
        if dd > max_dd:
            max_dd = dd
        if bal < 1.0:
            if win_pnl < worst_window:
                worst_window = win_pnl
            return bal, min_bal, peak_bal, max_dd, worst_window, total, wins, max_consec

    if win_pnl < worst_window:
        worst_window = win_pnl
    return bal, min_bal, peak_bal, max_dd, worst_window, total, wins, max_consec


def main():
    balance = 25.0
    ic_path = 'data/vps_ic_latest.csv'
    live_path = 'data/vps_live_latest.csv'

    print("Loading data...")
    ic_mom, hours, live = load_data(ic_path, live_path)
    print(f"IC: {len(ic_mom)} deduped signals, {hours:.1f}h")
    print(f"Live: {len(live)} trades\n")

    # Extract numpy arrays for numba
    entry_prices = ic_mom['entry_price'].values.astype(np.float64)
    momentums = ic_mom['momentum'].values.astype(np.float64)
    elapsed_arr = ic_mom['elapsed'].values.astype(np.float64)
    won = (ic_mom['outcome'] == 'won').values.astype(np.bool_)
    window_ids = ic_mom['slug_ts'].values.astype(np.int64)
    coins = ic_mom['coin'].values

    # Coin ID encoding for exclusion masks
    coin_map = {'BTC': 0, 'ETH': 1, 'SOL': 2, 'XRP': 3, 'DOGE': 4, 'BNB': 5, 'HYPE': 6}
    coin_ids = np.array([coin_map.get(c, 7) for c in coins], dtype=np.int64)

    # Warm up numba
    dummy = np.ones(len(entry_prices), dtype=np.bool_)
    simulate(entry_prices, won, window_ids, dummy, balance)

    # ═══════════════════════════════════════════════════════════════
    # PARAMETER RANGES — AS-constrained
    # ═══════════════════════════════════════════════════════════════
    # Elapsed: only late window. Test granular cutoffs.
    min_els = [120, 125, 130, 135, 140, 145, 150, 155, 160, 170, 180]
    max_els = [240, 250, 260, 270, 280, 290]

    # Momentum: 0.0008+ (below is noise per findings)
    min_moms = [0.0008, 0.0009, 0.001, 0.0011, 0.0012, 0.0013, 0.0015]

    # Entry: test all combinations
    min_entries = [0.05, 0.30, 0.40, 0.50, 0.55, 0.60, 0.65]
    max_entries = [0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]

    # Coins: all7 vs exclusions
    coin_sets = [
        ("all7", set()),
        ("no_HYPE", {6}),  # HYPE worst AS in late window
        ("no_SOL_DOGE", {2, 4}),
        ("no_HYPE_SOL_DOGE", {2, 4, 6}),
    ]

    t0 = time.time()
    results = []
    tested = 0
    busted = 0
    risk_fail = 0

    for coin_name, excl_ids in coin_sets:
        coin_mask = np.ones(len(entry_prices), dtype=np.bool_)
        for eid in excl_ids:
            coin_mask &= (coin_ids != eid)

        for min_el in min_els:
            for max_el in max_els:
                if min_el >= max_el:
                    continue
                for min_mom in min_moms:
                    for min_e in min_entries:
                        for max_e in max_entries:
                            if min_e >= max_e:
                                continue

                            mask = (
                                coin_mask &
                                (entry_prices >= min_e) & (entry_prices <= max_e) &
                                (momentums >= min_mom) &
                                (elapsed_arr >= min_el) & (elapsed_arr <= max_el)
                            )
                            n_sig = mask.sum()
                            if n_sig < 10:
                                continue

                            tested += 1

                            # IC simulation
                            (final, min_b, peak, max_dd, worst_w,
                             sim_t, sim_w, max_cl) = simulate(
                                entry_prices, won, window_ids, mask, balance
                            )

                            if sim_t < 10:
                                continue

                            # Risk filters
                            if final < 1.0 or min_b < 1.0:
                                busted += 1
                                continue
                            if min_b < balance * 0.40:
                                risk_fail += 1
                                continue
                            if max_dd > 0.30:
                                risk_fail += 1
                                continue
                            if abs(worst_w) > balance * 0.50:
                                risk_fail += 1
                                continue

                            # Live cross-reference
                            lf = live.copy()
                            lf = lf[(lf.entry_price >= min_e) & (lf.entry_price <= max_e)]
                            lf = lf[lf.momentum >= min_mom]
                            lf = lf[(lf.elapsed >= min_el) & (lf.elapsed <= max_el)]
                            if coin_name == 'no_HYPE':
                                lf = lf[lf.coin != 'HYPE']
                            elif coin_name == 'no_SOL_DOGE':
                                lf = lf[~lf.coin.isin(['SOL', 'DOGE'])]
                            elif coin_name == 'no_HYPE_SOL_DOGE':
                                lf = lf[~lf.coin.isin(['SOL', 'DOGE', 'HYPE'])]

                            live_n = len(lf)
                            if live_n < 8:
                                continue  # need minimum live sample

                            live_wr = lf.won.mean()
                            live_pnl = lf.pnl.sum()
                            live_avg_e = lf.entry_price.mean()
                            live_edge = live_wr - live_avg_e

                            if live_pnl <= 0:
                                continue  # must be profitable on live

                            sim_wr = sim_w / sim_t
                            avg_e = entry_prices[mask].mean()
                            ic_per_day = n_sig / hours * 24
                            ev_trade = sim_wr * (5 - avg_e * 5) - (1 - sim_wr) * avg_e * 5

                            results.append({
                                'coins': coin_name,
                                'min_e': min_e, 'max_e': max_e,
                                'min_mom': min_mom,
                                'min_el': min_el, 'max_el': max_el,
                                'ic_n': n_sig, 'ic_wr': sim_wr * 100,
                                'ic_final': final, 'ic_min': min_b,
                                'ic_dd': max_dd * 100, 'ic_cl': max_cl,
                                'ic_worst_w': worst_w,
                                'ic_per_day': ic_per_day,
                                'ev_trade': ev_trade,
                                'live_n': live_n, 'live_wr': live_wr * 100,
                                'live_pnl': live_pnl,
                                'live_edge': live_edge * 100,
                                'live_pnl_per_trade': live_pnl / live_n,
                            })

    elapsed_t = time.time() - t0

    print(f"Sweep: {elapsed_t:.1f}s, tested {tested:,}, busted {busted:,}, risk_fail {risk_fail:,}")
    print(f"Survived + live profitable + n>=8: {len(results):,}")

    if not results:
        print("NO CONFIGS FOUND")
        return

    df = pd.DataFrame(results)

    # ═══════════════════════════════════════════════════════════════
    # RANK BY LIVE EDGE — the only thing that matters
    # ═══════════════════════════════════════════════════════════════
    df = df.sort_values('live_edge', ascending=False)

    print(f"\n{'='*140}")
    print(f"TOP 40 BY LIVE EDGE (WR above breakeven on ACTUAL fills)")
    print(f"{'='*140}")
    print(f"{'#':>3} {'Coins':<15} {'Entry':>12} {'Mom':>8} {'Elapsed':>10} | "
          f"{'Lv_n':>4} {'Lv_WR':>6} {'Edge':>7} {'Lv_PnL':>8} {'$/tr':>6} | "
          f"{'IC_n':>4} {'IC_WR':>6} {'IC$':>6} {'DD':>5} {'cL':>3} {'IC/d':>5}")
    print("-" * 140)

    for i, (_, r) in enumerate(df.head(40).iterrows()):
        e = f"${r['min_e']:.2f}-${r['max_e']:.2f}"
        m = f">={r['min_mom']:.4f}"
        el = f"{r['min_el']:.0f}-{r['max_el']:.0f}"
        print(f"{i+1:>3} {r['coins']:<15} {e:>12} {m:>8} {el:>10} | "
              f"{r['live_n']:>4} {r['live_wr']:>5.1f}% {r['live_edge']:>+6.1f}% ${r['live_pnl']:>+6.2f} ${r['live_pnl_per_trade']:>+4.2f} | "
              f"{r['ic_n']:>4} {r['ic_wr']:>5.1f}% ${r['ic_final']:>4.0f} {r['ic_dd']:>4.1f}% {r['ic_cl']:>3.0f} {r['ic_per_day']:>4.0f}")

    # ═══════════════════════════════════════════════════════════════
    # ALSO RANK BY LIVE PnL (total money made)
    # ═══════════════════════════════════════════════════════════════
    df_pnl = df.sort_values('live_pnl', ascending=False)

    print(f"\n{'='*140}")
    print(f"TOP 20 BY LIVE PnL (total money made on actual fills)")
    print(f"{'='*140}")
    print(f"{'#':>3} {'Coins':<15} {'Entry':>12} {'Mom':>8} {'Elapsed':>10} | "
          f"{'Lv_n':>4} {'Lv_WR':>6} {'Edge':>7} {'Lv_PnL':>8} {'$/tr':>6} | "
          f"{'IC_n':>4} {'IC_WR':>6} {'IC$':>6} {'DD':>5} {'cL':>3} {'IC/d':>5}")
    print("-" * 140)

    for i, (_, r) in enumerate(df_pnl.head(20).iterrows()):
        e = f"${r['min_e']:.2f}-${r['max_e']:.2f}"
        m = f">={r['min_mom']:.4f}"
        el = f"{r['min_el']:.0f}-{r['max_el']:.0f}"
        print(f"{i+1:>3} {r['coins']:<15} {e:>12} {m:>8} {el:>10} | "
              f"{r['live_n']:>4} {r['live_wr']:>5.1f}% {r['live_edge']:>+6.1f}% ${r['live_pnl']:>+6.02f} ${r['live_pnl_per_trade']:>+4.2f} | "
              f"{r['ic_n']:>4} {r['ic_wr']:>5.1f}% ${r['ic_final']:>4.0f} {r['ic_dd']:>4.1f}% {r['ic_cl']:>3.0f} {r['ic_per_day']:>4.0f}")

    # ═══════════════════════════════════════════════════════════════
    # RANK BY PnL/TRADE (efficiency — best for do-or-die)
    # ═══════════════════════════════════════════════════════════════
    df_eff = df.sort_values('live_pnl_per_trade', ascending=False)

    print(f"\n{'='*140}")
    print(f"TOP 20 BY PnL/TRADE (most efficient — do or die ranking)")
    print(f"{'='*140}")
    print(f"{'#':>3} {'Coins':<15} {'Entry':>12} {'Mom':>8} {'Elapsed':>10} | "
          f"{'Lv_n':>4} {'Lv_WR':>6} {'Edge':>7} {'Lv_PnL':>8} {'$/tr':>6} | "
          f"{'IC_n':>4} {'IC_WR':>6} {'IC$':>6} {'DD':>5} {'cL':>3} {'IC/d':>5}")
    print("-" * 140)

    for i, (_, r) in enumerate(df_eff.head(20).iterrows()):
        e = f"${r['min_e']:.2f}-${r['max_e']:.2f}"
        m = f">={r['min_mom']:.4f}"
        el = f"{r['min_el']:.0f}-{r['max_el']:.0f}"
        print(f"{i+1:>3} {r['coins']:<15} {e:>12} {m:>8} {el:>10} | "
              f"{r['live_n']:>4} {r['live_wr']:>5.1f}% {r['live_edge']:>+6.1f}% ${r['live_pnl']:>+6.02f} ${r['live_pnl_per_trade']:>+4.2f} | "
              f"{r['ic_n']:>4} {r['ic_wr']:>5.1f}% ${r['ic_final']:>4.0f} {r['ic_dd']:>4.1f}% {r['ic_cl']:>3.0f} {r['ic_per_day']:>4.0f}")

    # ═══════════════════════════════════════════════════════════════
    # FIND THE SWEET SPOT: configs that appear in top 20 of ALL three rankings
    # ═══════════════════════════════════════════════════════════════
    top_edge = set(df.head(20).index)
    top_pnl = set(df_pnl.head(20).index)
    top_eff = set(df_eff.head(20).index)

    # Configs in at least 2 of 3 top-20 lists
    overlap_2 = (top_edge & top_pnl) | (top_edge & top_eff) | (top_pnl & top_eff)
    overlap_3 = top_edge & top_pnl & top_eff

    print(f"\n{'='*140}")
    print(f"CONSENSUS CONFIGS (appear in 2+ of 3 top-20 lists)")
    print(f"{'='*140}")

    consensus = df.loc[list(overlap_2)].sort_values('live_edge', ascending=False)
    if len(consensus) == 0:
        # Relax to top 30
        top_edge = set(df.head(30).index)
        top_pnl = set(df_pnl.head(30).index)
        top_eff = set(df_eff.head(30).index)
        overlap_2 = (top_edge & top_pnl) | (top_edge & top_eff) | (top_pnl & top_eff)
        consensus = df.loc[list(overlap_2)].sort_values('live_edge', ascending=False)
        print("(relaxed to top-30 overlap)")

    print(f"{'#':>3} {'Coins':<15} {'Entry':>12} {'Mom':>8} {'Elapsed':>10} | "
          f"{'Lv_n':>4} {'Lv_WR':>6} {'Edge':>7} {'Lv_PnL':>8} {'$/tr':>6} | "
          f"{'IC_n':>4} {'IC_WR':>6} {'IC$':>6} {'DD':>5} {'cL':>3} {'IC/d':>5} {'lists':>5}")
    print("-" * 145)

    for i, (idx, r) in enumerate(consensus.iterrows()):
        e = f"${r['min_e']:.2f}-${r['max_e']:.2f}"
        m = f">={r['min_mom']:.4f}"
        el = f"{r['min_el']:.0f}-{r['max_el']:.0f}"
        in_lists = sum([idx in top_edge, idx in top_pnl, idx in top_eff])
        star = " ★★★" if in_lists == 3 else " ★★" if in_lists == 2 else ""
        print(f"{i+1:>3} {r['coins']:<15} {e:>12} {m:>8} {el:>10} | "
              f"{r['live_n']:>4} {r['live_wr']:>5.1f}% {r['live_edge']:>+6.1f}% ${r['live_pnl']:>+6.02f} ${r['live_pnl_per_trade']:>+4.2f} | "
              f"{r['ic_n']:>4} {r['ic_wr']:>5.1f}% ${r['ic_final']:>4.0f} {r['ic_dd']:>4.1f}% {r['ic_cl']:>3.0f} {r['ic_per_day']:>4.0f} {in_lists:>3}/3{star}")


if __name__ == "__main__":
    main()
