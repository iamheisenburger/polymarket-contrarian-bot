#!/usr/bin/env python3
"""
V30.3 Sizing Sweep — concurrent-window simulation with bootstrap validation.

For the V30.3 config (mom>=0.0005, el=150-270, price=$0.30-$0.92, window direction lock),
find the bet-size-in-tokens that maximizes geometric growth on a $100 bankroll without
exceeding 25% max drawdown.

Sizing candidates: 5, 7, 10, 12, 15, 20 tokens per trade.

Methodology:
  1. Load BTC/ETH/SOL from pbt 30d archives
  2. Apply V30.3 filters
  3. Sort signals chronologically
  4. Simulate trades in order with V30.3 window-direction lock:
     - First FILLED trade in a market window locks the direction
     - Opposite-direction signals in locked windows are skipped
  5. Compute final bankroll, geometric growth, maxDD
  6. Bootstrap: 200 random 14-day windows; per sizing, compute distribution of
     geo growth and maxDD pct
  7. Select the best sizing where maxDD_p95 <= 25% AND all samples within the same
     historical period remain solvent
  8. Head-to-head bootstrap dominance of winner vs each other sizing

Key assumptions (stated here for transparency):
  - Fixed token count per trade: cost = tokens * entry_price
  - Fill rate = 100% (raw paper, matches V30.2 internal stats of +$812/27d)
  - No adverse selection tax (current live AS = 0pp per shadow_v30.csv)
  - Bankroll check: trade skipped if tokens * entry_price > available USDC
  - SOL direction filter is approximated via "is there a same-direction BTC or ETH
    signal in the same market window already?"  (exact live filter uses continuous
    price displacement, not available in pbt signal-only data)
  - Market windows keyed by market_slug end_ts (last "-N" in slug)
  - Seed fixed for reproducibility
"""

import csv
import argparse
import math
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

PBT_FILES = [
    ("BTC", "data/pbt_archive/pbt_btc_30d_FULL_20260410_0907.csv"),
    ("ETH", "data/pbt_eth_30d.csv"),
    ("SOL", "data/pbt_sol_30d.csv"),
]

# V30.3 config
MIN_MOM = 0.0005
MIN_EL = 150
MAX_EL = 270
MIN_P = 0.30
MAX_P = 0.92

SIZINGS = [5, 7, 10, 12, 15, 20]
STARTING_BANKROLL = 100.0
MAX_DD_PCT = 0.25  # 25% max drawdown constraint
BOOTSTRAP_N = 200
BOOTSTRAP_WINDOW_DAYS = 14
SEED = 42


def load_signals():
    rows = []
    for coin, path in PBT_FILES:
        p = Path(path)
        if not p.exists():
            print(f"WARN: {path} missing")
            continue
        with open(p) as f:
            for r in csv.DictReader(f):
                try:
                    mom = abs(float(r["momentum"]))
                    el = float(r["elapsed"])
                    price = float(r["entry_price"])
                    if r["is_momentum_side"] != "True":
                        continue
                    if mom < MIN_MOM:
                        continue
                    if not (MIN_EL <= el <= MAX_EL):
                        continue
                    if not (MIN_P <= price <= MAX_P):
                        continue
                    slug = r["market_slug"]
                    try:
                        window_ts = int(slug.rsplit("-", 1)[-1])
                    except (ValueError, IndexError):
                        continue
                    rows.append({
                        "ts": datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")),
                        "slug": slug,
                        "window_ts": window_ts,
                        "coin": coin,
                        "side": r["side"],
                        "price": price,
                        "won": r["outcome"] == "won",
                    })
                except (ValueError, KeyError):
                    continue
    rows.sort(key=lambda x: x["ts"])
    return rows


def simulate(signals, tokens, starting_bankroll=STARTING_BANKROLL):
    """Run a single concurrent-window simulation. Returns dict with metrics."""
    bankroll = starting_bankroll
    peak = starting_bankroll
    max_dd_abs = 0.0
    max_dd_pct = 0.0
    trades = 0
    wins = 0
    skipped_lock = 0
    skipped_bankroll = 0
    skipped_sol_dir = 0

    window_direction = {}  # window_ts -> "up"/"down" (locked AFTER first fill)
    window_fired_sides = defaultdict(set)  # window_ts -> set of (coin, side) already fired (for SOL dir approx)

    for s in signals:
        w_ts = s["window_ts"]

        # SOL direction filter approximation: require a prior BTC or ETH signal
        # with same side already in this window (or allow if BTC/ETH is this signal)
        if s["coin"] == "SOL":
            fired = window_fired_sides[w_ts]
            has_leader_same_side = any(
                (c in ("BTC", "ETH") and side == s["side"]) for (c, side) in fired
            )
            if not has_leader_same_side:
                skipped_sol_dir += 1
                # Still register this signal for later (no, don't — it didn't fire)
                continue

        # Window direction lock: if window is locked to opposite direction, skip
        locked = window_direction.get(w_ts)
        if locked is not None and locked != s["side"]:
            skipped_lock += 1
            continue

        # Bankroll check
        cost = tokens * s["price"]
        if cost > bankroll:
            skipped_bankroll += 1
            continue

        # Fire the trade
        trades += 1
        if s["won"]:
            wins += 1
            bankroll += tokens * (1.0 - s["price"])
        else:
            bankroll -= cost  # losing tokens * price

        # Lock the window AFTER successful fill
        if locked is None:
            window_direction[w_ts] = s["side"]
        window_fired_sides[w_ts].add((s["coin"], s["side"]))

        # Drawdown tracking (track max absolute AND max pct independently)
        if bankroll > peak:
            peak = bankroll
        dd_abs = peak - bankroll
        if dd_abs > max_dd_abs:
            max_dd_abs = dd_abs
        dd_pct = dd_abs / peak if peak > 0 else 0
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

    final = bankroll
    growth = final / starting_bankroll
    wr = wins / trades if trades > 0 else 0

    return {
        "tokens": tokens,
        "start": starting_bankroll,
        "final": final,
        "growth": growth,
        "log_growth": math.log(growth) if growth > 0 else -999,
        "max_dd_abs": max_dd_abs,
        "max_dd_pct": max_dd_pct,
        "trades": trades,
        "wins": wins,
        "wr": wr,
        "skipped_lock": skipped_lock,
        "skipped_bankroll": skipped_bankroll,
        "skipped_sol_dir": skipped_sol_dir,
    }


def bootstrap(signals, tokens, n_samples=BOOTSTRAP_N, window_days=BOOTSTRAP_WINDOW_DAYS,
              seed=SEED, starting_bankroll=STARTING_BANKROLL):
    """Sample n_samples random window_days periods, run sim on each, return list of results."""
    if not signals:
        return []
    t_min = signals[0]["ts"].timestamp()
    t_max = signals[-1]["ts"].timestamp()
    window_sec = window_days * 86400
    if t_max - t_min < window_sec:
        return []

    rng = random.Random(seed + tokens)  # seed per sizing for reproducibility
    results = []
    for _ in range(n_samples):
        start = rng.uniform(t_min, t_max - window_sec)
        end = start + window_sec
        subset = [s for s in signals if start <= s["ts"].timestamp() <= end]
        if not subset:
            continue
        r = simulate(subset, tokens, starting_bankroll=starting_bankroll)
        results.append(r)
    return results


def percentile(xs, p):
    if not xs:
        return 0
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    f = int(k)
    c = min(f + 1, len(xs) - 1)
    if f == c:
        return xs[f]
    return xs[f] * (c - k) + xs[c] * (k - f)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bootstrap-n", type=int, default=BOOTSTRAP_N)
    parser.add_argument("--window-days", type=int, default=BOOTSTRAP_WINDOW_DAYS)
    parser.add_argument("--max-dd-pct", type=float, default=MAX_DD_PCT)
    args = parser.parse_args()

    print("=" * 80)
    print("V30.3 SIZING SWEEP — concurrent-window backtest + bootstrap")
    print("=" * 80)
    print(f"Config: mom>={MIN_MOM} el={MIN_EL}-{MAX_EL} price=${MIN_P}-${MAX_P}")
    print(f"Window direction lock: ON (first fill commits window direction)")
    print(f"Starting bankroll: ${STARTING_BANKROLL:.0f}")
    print(f"Max DD constraint: {args.max_dd_pct:.0%}")
    print(f"Bootstrap: {args.bootstrap_n} samples of {args.window_days}d each")
    print()

    signals = load_signals()
    if not signals:
        print("No signals loaded.")
        return
    t_min = signals[0]["ts"]
    t_max = signals[-1]["ts"]
    days = (t_max - t_min).total_seconds() / 86400
    per_coin = defaultdict(int)
    for s in signals:
        per_coin[s["coin"]] += 1
    print(f"Loaded {len(signals)} V30.3-filtered signals across {days:.1f} days")
    print(f"  By coin: {dict(per_coin)}")
    print(f"  Date range: {t_min.date()} -> {t_max.date()}")
    print()

    # ---- Full 30d run per sizing ----
    print("=" * 80)
    print("FULL 30-DAY RUN (concurrent-window sim with V30.3 lock)")
    print("=" * 80)
    print(f"{'Tok':>4} {'Final$':>8} {'Growth':>8} {'maxDD%':>8} {'maxDD$':>8} "
          f"{'Trd':>5} {'WR':>6} {'SkpLk':>6} {'SkpBR':>6} {'SkpDir':>7}")
    print("-" * 80)
    full_results = {}
    for tok in SIZINGS:
        r = simulate(signals, tok)
        full_results[tok] = r
        print(f"{tok:4d} {r['final']:8.2f} {r['growth']:8.3f}x {r['max_dd_pct']:7.1%} "
              f"{r['max_dd_abs']:8.2f} {r['trades']:5d} {r['wr']:6.1%} "
              f"{r['skipped_lock']:6d} {r['skipped_bankroll']:6d} {r['skipped_sol_dir']:7d}")
    print()

    # ---- Bootstrap per sizing ----
    print("=" * 80)
    print(f"BOOTSTRAP — {args.bootstrap_n} × {args.window_days}-day samples")
    print("=" * 80)
    print(f"{'Tok':>4} "
          f"{'GrMed':>7} {'GrP10':>7} {'GrP90':>7} "
          f"{'DDMed':>7} {'DDp75':>7} {'DDp95':>7} {'DDmax':>7} "
          f"{'BustN':>6}")
    print("-" * 80)
    bootstrap_results = {}
    for tok in SIZINGS:
        samples = bootstrap(signals, tok, args.bootstrap_n, args.window_days)
        bootstrap_results[tok] = samples
        if not samples:
            print(f"{tok:4d} no samples")
            continue
        growths = [s["growth"] for s in samples]
        dds = [s["max_dd_pct"] for s in samples]
        bust = sum(1 for s in samples if s["final"] < 10)
        print(f"{tok:4d} "
              f"{percentile(growths, 0.5):6.3f}x {percentile(growths, 0.1):6.3f}x {percentile(growths, 0.9):6.3f}x "
              f"{percentile(dds, 0.5):6.1%} {percentile(dds, 0.75):6.1%} {percentile(dds, 0.95):6.1%} {max(dds):6.1%} "
              f"{bust:6d}")
    print()

    # ---- Sizing selection ----
    print("=" * 80)
    print(f"SELECTION: max geometric growth where maxDD_p95 <= {args.max_dd_pct:.0%}")
    print("=" * 80)
    eligible = []
    for tok in SIZINGS:
        samples = bootstrap_results[tok]
        if not samples:
            continue
        dds = [s["max_dd_pct"] for s in samples]
        growths = [s["growth"] for s in samples]
        dd_p95 = percentile(dds, 0.95)
        median_growth = percentile(growths, 0.5)
        mean_log_growth = sum(s["log_growth"] for s in samples) / len(samples)
        eligible.append({
            "tokens": tok,
            "dd_p95": dd_p95,
            "median_growth": median_growth,
            "mean_log_growth": mean_log_growth,
            "passes_dd": dd_p95 <= args.max_dd_pct,
        })
    for e in eligible:
        status = "PASS" if e["passes_dd"] else "FAIL"
        print(f"  {e['tokens']:3d} tok: median_growth={e['median_growth']:.3f}x  "
              f"dd_p95={e['dd_p95']:.1%}  mean_log_g={e['mean_log_growth']:+.4f}  [{status}]")
    print()

    passing = [e for e in eligible if e["passes_dd"]]
    if not passing:
        print("NO SIZING PASSES the maxDD_p95 constraint. Showing best by log_growth anyway:")
        passing = eligible
    # Rank by mean log growth (geometric growth optimizer)
    passing.sort(key=lambda e: e["mean_log_growth"], reverse=True)
    winner = passing[0]
    print(f"WINNER: {winner['tokens']} tokens")
    print(f"  median growth: {winner['median_growth']:.3f}x over {args.window_days}d")
    print(f"  mean log growth: {winner['mean_log_growth']:+.4f}")
    print(f"  maxDD p95: {winner['dd_p95']:.1%}")
    print()

    # ---- Head-to-head bootstrap dominance vs 5 tokens (current V30.3) ----
    print("=" * 80)
    print("METHODOLOGY GATE: bootstrap dominance vs 5 tokens (current V30.3)")
    print("=" * 80)
    print("For the 200/200 deployment gate: proposed sizing must beat 5 tok in ALL samples")
    print(f"{'Proposed':>9} {'PropWins/5Tok':>15} {'Pct':>8} {'BustRate':>10} {'200/200?':>10}")
    print("-" * 60)
    base_samples = bootstrap_results[5]
    for tok in SIZINGS:
        if tok == 5:
            continue
        other_samples = bootstrap_results[tok]
        pair_n = min(len(base_samples), len(other_samples))
        wins_over = sum(
            1 for w, o in zip(other_samples[:pair_n], base_samples[:pair_n])
            if w["log_growth"] > o["log_growth"]
        )
        bust_rate = sum(1 for s in other_samples if s["final"] < 10) / len(other_samples)
        gate = "PASS" if wins_over == pair_n else "FAIL"
        print(f"{tok:>9d} {wins_over:>10d}/{pair_n:<4d} {wins_over/pair_n:7.1%} "
              f"{bust_rate:9.1%} {gate:>10s}")
    print()

    # ---- Bankroll required for each sizing to pass 25% DD on full run ----
    print("=" * 80)
    print("MIN BANKROLL needed for each sizing to satisfy 25% max DD (full 30d run)")
    print("=" * 80)
    print("Derived from absolute maxDD on each full-run sim.")
    print("min_bankroll = maxDD_abs / 0.25 (so that maxDD_abs is 25% of bankroll)")
    print(f"{'Tok':>4} {'maxDD$':>10} {'MinBR needed':>15} {'ActualBR':>10} {'OK?':>6}")
    print("-" * 60)
    for tok in SIZINGS:
        r = full_results[tok]
        if r["max_dd_abs"] == 0:
            print(f"{tok:4d} no DD data")
            continue
        min_br = r["max_dd_abs"] / args.max_dd_pct
        ok = "PASS" if STARTING_BANKROLL >= min_br else "FAIL"
        print(f"{tok:4d} {r['max_dd_abs']:10.2f} {min_br:12.0f}   "
              f"{STARTING_BANKROLL:9.0f} {ok:>6s}")
    print()
    print("=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
