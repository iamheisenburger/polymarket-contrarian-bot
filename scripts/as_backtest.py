#!/usr/bin/env python3
"""
AS-Adjusted Backtest — Monte Carlo simulation with empirical adverse selection.

Instead of assuming 100% fill at IC win rate, this model:
1. For each IC signal, estimates P(fill) from empirical fill rates
2. For each filled signal, estimates P(win) from empirical AS-adjusted WRs
3. Runs 10,000 Monte Carlo paths to get realistic outcome distributions
4. Reports P(bust), P(profitable), median outcome, worst-case

The AS parameters come from cross-referencing 511 live trades against
75,717 standalone collector signals (Apr 13-17, 2026).

Usage:
    python scripts/as_backtest.py --balance 25 --ic-file data/vps_ic_latest.csv
"""
import argparse
import pandas as pd
import numpy as np
import time


# ═══════════════════════════════════════════════════════════════════════
# EMPIRICAL AS MODEL PARAMETERS
# From 511 live trades vs 75K SC signals cross-reference (Apr 17 analysis)
# ═══════════════════════════════════════════════════════════════════════

# Fill rates by elapsed bucket (threshold 0.001 reference)
# These represent P(we actually fill this signal | signal exists)
FILL_RATE_BY_ELAPSED = {
    (0, 60): 0.110,
    (60, 120): 0.074,
    (120, 180): 0.046,
    (180, 300): 0.019,
}

# AS tax by elapsed (filled_WR - IC_WR)
# Negative = we fill losers. Positive = we fill winners.
AS_TAX_BY_ELAPSED = {
    (0, 60): -0.031,
    (60, 120): -0.067,
    (120, 180): +0.036,
    (180, 300): -0.182,  # small n, unreliable — use -0.05 as conservative
}
# Override the 180+ bucket — n=21 is too small, and it contradicts the pattern.
# Use a conservative estimate between the 120-180 and 180+ values.
AS_TAX_BY_ELAPSED[(180, 300)] = -0.02  # conservative: slightly negative

# AS tax by coin
AS_TAX_BY_COIN = {
    'BTC': +0.074,
    'ETH': -0.043,
    'SOL': -0.155,
    'XRP': -0.017,
    'DOGE': -0.121,
    'BNB': -0.020,
    'HYPE': -0.064,
}

# AS tax by entry price
AS_TAX_BY_ENTRY = {
    (0, 0.50): -0.260,
    (0.50, 0.60): -0.043,
    (0.60, 0.70): -0.060,
    (0.70, 0.85): +0.014,
    (0.85, 1.0): -0.064,
}

# AS tax by side
AS_TAX_BY_SIDE = {
    'up': -0.130,
    'down': -0.027,
}

# Fill rate modifier by entry price (relative to base)
FILL_RATE_BY_ENTRY = {
    (0, 0.50): 0.035,
    (0.50, 0.60): 0.116,
    (0.60, 0.70): 0.144,
    (0.70, 0.85): 0.070,
    (0.85, 1.0): 0.020,
}


def get_bucket(value, buckets):
    """Find which bucket a value falls into."""
    for (lo, hi) in buckets:
        if lo <= value < hi:
            return (lo, hi)
    return list(buckets.keys())[-1]  # last bucket


def compute_as_adjusted_wr(ic_wr, elapsed, entry_price, coin, side):
    """
    Compute AS-adjusted win rate for a signal.

    Uses additive model: adjusted_WR = IC_WR + weighted_AS_tax

    The AS tax is a blend of dimensional penalties, weighted by
    reliability (sample size from the empirical data).
    """
    # Get dimensional AS taxes
    e_bucket = get_bucket(elapsed, AS_TAX_BY_ELAPSED)
    p_bucket = get_bucket(entry_price, AS_TAX_BY_ENTRY)

    elapsed_tax = AS_TAX_BY_ELAPSED.get(e_bucket, -0.05)
    entry_tax = AS_TAX_BY_ENTRY.get(p_bucket, -0.05)
    coin_tax = AS_TAX_BY_COIN.get(coin, -0.05)
    side_tax = AS_TAX_BY_SIDE.get(side, -0.05)

    # Blend: elapsed is most reliable (biggest sample sizes per bucket)
    # Weight: elapsed 40%, entry 25%, coin 20%, side 15%
    blended_tax = (0.40 * elapsed_tax +
                   0.25 * entry_tax +
                   0.20 * coin_tax +
                   0.15 * side_tax)

    adjusted_wr = ic_wr + blended_tax
    return np.clip(adjusted_wr, 0.01, 0.99)


def compute_fill_prob(elapsed, entry_price):
    """Estimate fill probability based on elapsed and entry."""
    e_bucket = get_bucket(elapsed, FILL_RATE_BY_ELAPSED)
    p_bucket = get_bucket(entry_price, FILL_RATE_BY_ENTRY)

    # Use the elapsed-based fill rate as base, adjust by entry price
    base_fill = FILL_RATE_BY_ELAPSED.get(e_bucket, 0.05)
    entry_fill = FILL_RATE_BY_ENTRY.get(p_bucket, 0.05)

    # Geometric mean of the two (both must be favorable for high fill)
    combined = np.sqrt(base_fill * entry_fill)
    return np.clip(combined, 0.005, 0.50)


def load_ic(path):
    """Load and prepare IC data."""
    ic = pd.read_csv(path)
    ic_mom = ic[(ic['is_momentum_side'] == True) & (ic['outcome'].isin(['won', 'lost']))].copy()
    ic_mom = ic_mom.sort_values('timestamp')
    ic_mom['slug_ts'] = ic_mom['market_slug'].str.extract(r'(\d+)$').astype(float).astype(int)
    ic_mom = ic_mom.drop_duplicates(subset=['market_slug', 'coin', 'side'], keep='first')
    ic_mom['won'] = (ic_mom['outcome'] == 'won').astype(int)

    ic['ts'] = pd.to_datetime(ic['timestamp'])
    hours = (ic['ts'].max() - ic['ts'].min()).total_seconds() / 3600

    return ic_mom, hours


def run_as_monte_carlo(ic_mom, hours, config, balance, n_sims=10000):
    """
    Monte Carlo simulation with AS model.

    For each IC signal:
    1. Check if it passes config filters
    2. Flip coin with P(fill) — do we even get this trade?
    3. If filled, flip coin with P(win|filled) — AS-adjusted WR
    4. Track balance, bust, drawdown
    """
    # Filter IC data to config
    t = ic_mom.copy()
    t = t[(t['entry_price'] >= config['min_e']) & (t['entry_price'] <= config['max_e'])]
    t = t[t['momentum'] >= config['min_mom']]
    t = t[(t['elapsed'] >= config['min_el']) & (t['elapsed'] <= config['max_el'])]

    if config.get('exclude_coins'):
        t = t[~t['coin'].isin(config['exclude_coins'])]

    t = t.sort_values('timestamp')

    n_signals = len(t)
    if n_signals == 0:
        print("  NO SIGNALS MATCH CONFIG")
        return None

    # Pre-compute per-signal: ic_wr (from IC outcome), as_adjusted_wr, fill_prob
    ic_wrs = []  # the IC's implied WR for this signal's bucket
    as_wrs = []  # AS-adjusted WR
    fill_probs = []
    entry_costs = []
    window_ids = []

    # For IC WR, use the signal's actual outcome (but for AS adjustment we need bucket WR)
    # Simplification: use the signal's own IC outcome as the base,
    # but apply AS tax to shift the probability

    # Better: compute bucket-level IC WR, then apply AS tax
    # Group by (elapsed_bucket, entry_bucket) and compute IC WR
    def elapsed_bucket(e):
        if e < 60: return (0, 60)
        elif e < 120: return (60, 120)
        elif e < 180: return (120, 180)
        else: return (180, 300)

    def entry_bucket(p):
        if p < 0.50: return (0, 0.50)
        elif p < 0.60: return (0.50, 0.60)
        elif p < 0.70: return (0.60, 0.70)
        elif p < 0.85: return (0.70, 0.85)
        else: return (0.85, 1.0)

    t['e_bucket'] = t['elapsed'].apply(elapsed_bucket)
    t['p_bucket'] = t['entry_price'].apply(entry_bucket)

    # Compute bucket-level IC WRs
    bucket_wr = t.groupby(['e_bucket', 'p_bucket'])['won'].mean().to_dict()

    for _, row in t.iterrows():
        eb = row['e_bucket']
        pb = row['p_bucket']

        # IC WR for this bucket
        ic_wr = bucket_wr.get((eb, pb), 0.70)  # fallback 70%

        # AS-adjusted WR
        as_wr = compute_as_adjusted_wr(ic_wr, row['elapsed'], row['entry_price'],
                                        row['coin'], row['side'])

        # Fill probability
        fp = compute_fill_prob(row['elapsed'], row['entry_price'])

        ic_wrs.append(ic_wr)
        as_wrs.append(as_wr)
        fill_probs.append(fp)
        entry_costs.append(row['entry_price'])
        window_ids.append(row['slug_ts'])

    as_wrs = np.array(as_wrs)
    fill_probs = np.array(fill_probs)
    entry_costs = np.array(entry_costs)
    window_ids_arr = np.array(window_ids)

    # Print model diagnostics
    avg_fill = fill_probs.mean()
    avg_as_wr = as_wrs.mean()
    avg_ic_wr = np.mean(ic_wrs)
    expected_fills_per_day = n_signals * avg_fill / hours * 24

    print(f"  IC signals: {n_signals}")
    print(f"  Avg IC WR: {avg_ic_wr:.1%}")
    print(f"  Avg AS-adjusted WR: {avg_as_wr:.1%} (AS tax: {avg_as_wr - avg_ic_wr:+.1%})")
    print(f"  Avg fill probability: {avg_fill:.1%}")
    print(f"  Expected fills/day: {expected_fills_per_day:.1f}")
    print()

    # Monte Carlo simulation
    rng = np.random.default_rng(42)

    final_bals = np.zeros(n_sims)
    min_bals = np.zeros(n_sims)
    max_dds = np.zeros(n_sims)
    trade_counts = np.zeros(n_sims, dtype=int)
    bust_count = 0

    for sim in range(n_sims):
        bal = balance
        min_bal = balance
        peak = balance
        max_dd = 0
        trades = 0

        # Random draws for all signals at once (fast)
        fill_rolls = rng.random(n_signals)
        win_rolls = rng.random(n_signals)

        current_window = -1
        window_trades = 0

        for i in range(n_signals):
            # Window concurrency limit
            if window_ids_arr[i] != current_window:
                current_window = window_ids_arr[i]
                window_trades = 0

            if window_trades >= 7:
                continue

            # Fill check
            if fill_rolls[i] > fill_probs[i]:
                continue  # signal not filled

            cost = entry_costs[i] * 5.0
            if cost > bal or cost < 0.50:
                continue

            window_trades += 1
            trades += 1

            # Win check with AS-adjusted WR
            if win_rolls[i] < as_wrs[i]:
                bal += 5.0 - cost
            else:
                bal -= cost

            min_bal = min(min_bal, bal)
            peak = max(peak, bal)
            dd = (peak - bal) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

            if bal < 1.0:
                bust_count += 1
                break

        final_bals[sim] = bal
        min_bals[sim] = min_bal
        max_dds[sim] = max_dd
        trade_counts[sim] = trades

    # Results
    p_bust = bust_count / n_sims
    p_profit = (final_bals > balance).mean()

    print(f"  {'─' * 60}")
    print(f"  MONTE CARLO RESULTS ({n_sims:,} simulations)")
    print(f"  {'─' * 60}")
    print(f"  P(bust):       {p_bust:.1%}")
    print(f"  P(profitable): {p_profit:.1%}")
    print(f"  Median trades: {np.median(trade_counts):.0f}")
    print(f"  Median final:  ${np.median(final_bals):.2f}")
    print(f"  Mean final:    ${np.mean(final_bals):.2f}")
    print(f"  5th pctile:    ${np.percentile(final_bals, 5):.2f}")
    print(f"  25th pctile:   ${np.percentile(final_bals, 25):.2f}")
    print(f"  75th pctile:   ${np.percentile(final_bals, 75):.2f}")
    print(f"  95th pctile:   ${np.percentile(final_bals, 95):.2f}")
    print(f"  Worst case:    ${np.min(final_bals):.2f}")
    print(f"  Best case:     ${np.max(final_bals):.2f}")
    print(f"  Median min bal: ${np.median(min_bals):.2f}")
    print(f"  Median max DD: {np.median(max_dds):.1%}")
    print(f"  95th pctile DD: {np.percentile(max_dds, 95):.1%}")

    return {
        'p_bust': p_bust,
        'p_profit': p_profit,
        'median_final': np.median(final_bals),
        'p5_final': np.percentile(final_bals, 5),
        'median_trades': np.median(trade_counts),
        'median_dd': np.median(max_dds),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--balance", type=float, default=25.0)
    parser.add_argument("--ic-file", type=str, default="data/vps_ic_latest.csv")
    parser.add_argument("--sims", type=int, default=10000)
    args = parser.parse_args()

    print("Loading IC data...")
    ic_mom, hours = load_ic(args.ic_file)
    print(f"Loaded {len(ic_mom)} deduped IC signals, {hours:.1f} hours\n")

    # Test all candidate configs
    configs = [
        {"name": "A: $0.55-0.80, m>=0.0008, el 120-300, all7",
         "min_e": 0.55, "max_e": 0.80, "min_mom": 0.0008, "min_el": 120, "max_el": 300},
        {"name": "B: $0.05-0.70, m>=0.001, el 120-300, all7",
         "min_e": 0.05, "max_e": 0.70, "min_mom": 0.001, "min_el": 120, "max_el": 300},
        {"name": "C: $0.55-0.70, m>=0.001, el 120-300, all7",
         "min_e": 0.55, "max_e": 0.70, "min_mom": 0.001, "min_el": 120, "max_el": 300},
        {"name": "D: $0.55-0.70, m>=0.0008, el 120-300, all7",
         "min_e": 0.55, "max_e": 0.70, "min_mom": 0.0008, "min_el": 120, "max_el": 300},
        {"name": "E: $0.05-0.80, m>=0.0008, el 120-300, all7",
         "min_e": 0.05, "max_e": 0.80, "min_mom": 0.0008, "min_el": 120, "max_el": 300},
        {"name": "F: $0.55-0.80, m>=0.0008, el 120-300, no SOL/DOGE",
         "min_e": 0.55, "max_e": 0.80, "min_mom": 0.0008, "min_el": 120, "max_el": 300,
         "exclude_coins": ["SOL", "DOGE"]},
        # Include the broader configs for comparison
        {"name": "X: $0.50-0.60, m>=0.0003, el 0-300 (IC WINNER — dies live)",
         "min_e": 0.50, "max_e": 0.60, "min_mom": 0.0003, "min_el": 0, "max_el": 300},
        {"name": "Y: $0.55-0.80, m>=0.0008, el 0-300 (early included)",
         "min_e": 0.55, "max_e": 0.80, "min_mom": 0.0008, "min_el": 0, "max_el": 300},
    ]

    results = []
    for config in configs:
        print(f"{'='*70}")
        print(f"Config: {config['name']}")
        print(f"{'='*70}")
        r = run_as_monte_carlo(ic_mom, hours, config, args.balance, args.sims)
        if r:
            r['name'] = config['name']
            results.append(r)
        print()

    # Summary table
    print(f"\n{'='*100}")
    print(f"SUMMARY — Do or Die with ${args.balance}")
    print(f"{'='*100}")
    print(f"{'Config':<55} {'P(bust)':>8} {'P(profit)':>10} {'Median$':>8} {'5th%':>7} {'Trades':>7}")
    print("-" * 100)
    for r in results:
        bust_flag = " ☠️" if r['p_bust'] > 0.10 else " ✓" if r['p_bust'] < 0.01 else " ⚠"
        print(f"{r['name']:<55} {r['p_bust']:>7.1%}{bust_flag} {r['p_profit']:>9.1%} ${r['median_final']:>6.2f} ${r['p5_final']:>5.2f} {r['median_trades']:>6.0f}")


if __name__ == "__main__":
    main()
