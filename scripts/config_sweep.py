"""
Config Sweep — Growth Model + Risk Model

Growth Model: daily_EV = trades_per_day × EV_per_trade
Risk Model: Full trade-by-trade simulation at starting balance.
            If balance drops below $1 at ANY point → config rejected.

Uses numba JIT for ~100x speedup on simulation loops.

Usage:
    python scripts/config_sweep.py --balance 25.62 --ic-file data/live_v30.collector.csv
    python scripts/config_sweep.py --balance 50 --ic-file data/live_v30.collector.csv --top 10
"""

import argparse
import pandas as pd
import numpy as np
from numba import njit
import time


def load_ic(path: str):
    """Load and prepare IC data. Returns arrays for numba + metadata."""
    ic = pd.read_csv(path)
    ic_mom = ic[(ic['is_momentum_side'] == True) & (ic['outcome'].isin(['won', 'lost']))].copy()
    ic_mom = ic_mom.sort_values('timestamp')
    ic_mom['slug_ts'] = ic_mom['market_slug'].str.extract(r'(\d+)$').astype(float).astype(int)
    ic_mom = ic_mom.drop_duplicates(subset=['market_slug', 'coin', 'side'], keep='first')
    ic['ts'] = pd.to_datetime(ic['timestamp'])
    hours = (ic['ts'].max() - ic['ts'].min()).total_seconds() / 3600

    # Extract numpy arrays for numba
    entry_prices = ic_mom['entry_price'].values.astype(np.float64)
    momentums = ic_mom['momentum'].values.astype(np.float64)
    elapsed = ic_mom['elapsed'].values.astype(np.float64)
    won = (ic_mom['outcome'] == 'won').values.astype(np.bool_)
    window_ids = ic_mom['slug_ts'].values.astype(np.int64)

    return entry_prices, momentums, elapsed, won, window_ids, hours, len(ic_mom)


@njit(cache=True)
def simulate(entry_prices, won, window_ids, mask, balance, max_conc):
    """
    Numba-JIT simulation. Replays every trade sequentially.
    Returns (final_balance, min_balance, bust, total_trades, wins).
    """
    bal = balance
    min_bal = bal
    total_trades = 0
    wins = 0
    n = len(entry_prices)

    # Build filtered indices
    filtered = np.empty(n, dtype=np.int64)
    fcount = 0
    for i in range(n):
        if mask[i]:
            filtered[fcount] = i
            fcount += 1

    if fcount == 0:
        return bal, min_bal, False, 0, 0

    # Process window by window
    current_window = window_ids[filtered[0]]
    window_count = 0

    for fi in range(fcount):
        idx = filtered[fi]
        w = window_ids[idx]

        if w != current_window:
            current_window = w
            window_count = 0

        if window_count >= max_conc:
            continue

        cost = entry_prices[idx] * 5.0
        if cost > bal or cost < 0.50:
            continue

        window_count += 1
        total_trades += 1

        if won[idx]:
            bal += (5.0 - cost)
            wins += 1
        else:
            bal -= cost

        if bal < min_bal:
            min_bal = bal

        if bal < 1.0:
            return bal, min_bal, True, total_trades, wins

    return bal, min_bal, False, total_trades, wins


def sweep(entry_prices, momentums, elapsed_arr, won, window_ids, hours, n_signals, balance, top_n=25):
    """Run full sweep with numba-accelerated simulation."""

    min_entries = [0.05, 0.10, 0.20, 0.30, 0.40, 0.50]
    max_entries = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
    min_moms = [0.0003, 0.0005, 0.0007, 0.0008, 0.001, 0.0012, 0.0015, 0.002]
    min_els = [0, 30, 60, 90, 120, 150, 180]
    max_els = [150, 180, 210, 240, 270, 300]
    max_concs = [1, 2, 3, 4, 5, 7]

    # Warm up numba JIT
    dummy_mask = np.ones(len(entry_prices), dtype=np.bool_)
    simulate(entry_prices, won, window_ids, dummy_mask, balance, 1)

    configs = []
    tested = 0
    busted = 0

    t0 = time.time()

    for min_e in min_entries:
        for max_e in max_entries:
            if min_e >= max_e:
                continue
            for min_mom in min_moms:
                for min_el in min_els:
                    for max_el in max_els:
                        if min_el >= max_el:
                            continue

                        # Build mask (vectorized numpy)
                        mask = (
                            (entry_prices >= min_e) & (entry_prices <= max_e) &
                            (momentums >= min_mom) &
                            (elapsed_arr >= min_el) & (elapsed_arr <= max_el)
                        )
                        n = mask.sum()
                        if n < 30:
                            continue

                        wr = won[mask].mean()
                        avg_e = entry_prices[mask].mean()
                        ev = wr * (5 - avg_e * 5) - (1 - wr) * avg_e * 5
                        if ev <= 0:
                            continue

                        for max_conc in max_concs:
                            tested += 1

                            # RISK MODEL: full simulation
                            final_bal, min_bal, bust, sim_trades, sim_wins = simulate(
                                entry_prices, won, window_ids, mask, balance, max_conc
                            )

                            if bust:
                                busted += 1
                                continue

                            # GROWTH MODEL
                            signals_day = n / hours * 24
                            trades_day = min(signals_day / 4, 288 * max_conc)
                            daily_ev = trades_day * ev

                            sim_wr = sim_wins / sim_trades * 100 if sim_trades > 0 else 0

                            configs.append({
                                'min_e': min_e, 'max_e': max_e, 'min_mom': min_mom,
                                'min_el': min_el, 'max_el': max_el, 'max_conc': max_conc,
                                'n': int(n), 'wr': wr * 100, 'avg_entry': avg_e,
                                'ev_trade': ev, 'trades_day': trades_day,
                                'daily_ev': daily_ev,
                                'final_bal': final_bal, 'min_bal': min_bal,
                                'sim_trades': sim_trades, 'sim_wr': sim_wr,
                            })

    elapsed_time = time.time() - t0

    df = pd.DataFrame(configs)
    df = df.sort_values('daily_ev', ascending=False)

    print(f"Sweep complete in {elapsed_time:.1f}s")
    print(f"Tested: {tested} configs with full simulation")
    print(f"Busted: {busted} configs rejected by risk model")
    print(f"Survived: {len(df)} configs")
    print(f"Balance: ${balance:.2f}")
    print(f"IC data: {n_signals} deduped signals, {hours:.1f} hours")
    print()

    if len(df) == 0:
        print("NO CONFIGS SURVIVED. Balance too low or data too limited.")
        return df

    print(f"Top {top_n} by daily growth (GUARANTEED no bust on IC data):")
    header = f"{'Config':>50s} {'c':>2s} | {'n':>4s} {'WR':>6s} {'EV':>5s} {'t/d':>4s} {'$/d':>6s} {'final':>7s} {'min':>6s}"
    print(header)
    for _, r in df.head(top_n).iterrows():
        config = f"e=${r['min_e']:.2f}-${r['max_e']:.2f} m>={r['min_mom']:.4f} el={r['min_el']:.0f}-{r['max_el']:.0f}"
        print(f"{config:>50s} {r['max_conc']:>2.0f} | {r['n']:>4.0f} {r['wr']:>5.1f}% ${r['ev_trade']:.2f} {r['trades_day']:>3.0f} ${r['daily_ev']:>+5.0f} ${r['final_bal']:>6.0f} ${r['min_bal']:>5.1f}")

    return df


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Config sweep with growth + risk models")
    parser.add_argument("--balance", type=float, required=True, help="Starting balance in USD")
    parser.add_argument("--ic-file", type=str, default="data/live_v30.collector.csv", help="IC data file")
    parser.add_argument("--top", type=int, default=25, help="Show top N configs")
    args = parser.parse_args()

    entry_prices, momentums, elapsed_arr, won, window_ids, hours, n_signals = load_ic(args.ic_file)
    df = sweep(entry_prices, momentums, elapsed_arr, won, window_ids, hours, n_signals, args.balance, args.top)
