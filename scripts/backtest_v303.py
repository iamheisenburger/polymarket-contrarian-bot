#!/usr/bin/env python3
"""
V30.3 full-methodology backtest on BTC/ETH/SOL.

Follows the locked methodology:
  1. Pre-flight: intersect common date range across BTC/ETH/SOL
  2. Concurrent-window simulation with V30.3 window direction lock
  3. Cluster-level metrics: all-win% / all-lose% / mixed%, max losing cluster
     streak, worst single cluster
  4. 3-coin agreement check (each coin independently >=200 signals, >=85% WR, EV>0)
  5. Bootstrap 200 x 14-day samples: distribution of PnL, maxDD, worst 14d
  6. Late TTE rule: el_lo=150 enforced (non-negotiable)
  7. Window direction lock: ON (first filled trade commits window side)

This is V30.3 standing alone — there's no comparison variant, so the
bootstrap dominance gate is informational only.
"""

import csv
import random
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---- V30.3 config (MATCHES LIVE CODE momentum_sniper.py:1132-1137) ----
MIN_MOM = 0.0005
MIN_EL = 150  # non-negotiable late TTE floor
MAX_EL = 270
MIN_P = 0.30
MAX_P = 0.92
TOKENS = 5

# ---- Data ----
PBT_FILES = [
    ("BTC", "data/pbt_archive/pbt_btc_30d_FULL_20260410_0907.csv"),
    ("ETH", "data/pbt_eth_30d.csv"),
    ("SOL", "data/pbt_sol_30d.csv"),
]

# ---- Bootstrap ----
BOOTSTRAP_N = 200
BOOTSTRAP_WINDOW_DAYS = 14
SEED = 42

# ---- Gates ----
MIN_SIGNALS_PER_COIN = 200
MIN_WR_PER_COIN = 0.85


def load_coin(coin: str, path: str):
    """Load & V30.3-filter one coin's signals."""
    rows = []
    p = Path(path)
    if not p.exists():
        print(f"  WARN: {path} missing")
        return rows
    with open(p) as f:
        for r in csv.DictReader(f):
            try:
                if r["is_momentum_side"] != "True":
                    continue
                mom = abs(float(r["momentum"]))
                el = float(r["elapsed"])
                price = float(r["entry_price"])
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
                ts = datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00"))
                rows.append({
                    "ts": ts,
                    "date": ts.date(),
                    "slug": slug,
                    "window_ts": window_ts,
                    "coin": coin,
                    "side": r["side"],
                    "price": price,
                    "won": r["outcome"] == "won",
                })
            except (ValueError, KeyError):
                continue
    return rows


def simulate_concurrent_window(signals, apply_sol_dir_filter=True):
    """Run the V30.3 concurrent-window simulation with window direction lock.

    Returns:
        dict with trade-level and CLUSTER-LEVEL metrics.
    """
    signals_sorted = sorted(signals, key=lambda s: s["ts"])
    pnl_total = 0.0
    trades_fired = []
    window_direction = {}           # window_ts -> locked side
    window_fired_sides = defaultdict(set)  # window_ts -> set of (coin, side) that have fired

    skipped_lock = 0
    skipped_sol_dir = 0

    for s in signals_sorted:
        w_ts = s["window_ts"]

        # SOL direction filter approximation: require a prior same-side BTC/ETH
        # signal to have already fired in this window. This mirrors the live
        # code where alt-coins skip if no BTC/ETH leader consensus exists.
        if apply_sol_dir_filter and s["coin"] == "SOL":
            fired = window_fired_sides[w_ts]
            has_leader = any((c in ("BTC", "ETH") and side == s["side"])
                             for (c, side) in fired)
            if not has_leader:
                skipped_sol_dir += 1
                continue

        locked = window_direction.get(w_ts)
        if locked is not None and locked != s["side"]:
            skipped_lock += 1
            continue

        # Fire the trade
        if s["won"]:
            pnl = TOKENS * (1.0 - s["price"])
        else:
            pnl = -TOKENS * s["price"]
        pnl_total += pnl
        trades_fired.append({**s, "pnl": pnl})

        # Lock the window after first fill
        if locked is None:
            window_direction[w_ts] = s["side"]
        window_fired_sides[w_ts].add((s["coin"], s["side"]))

    n_trades = len(trades_fired)
    n_wins = sum(1 for t in trades_fired if t["won"])
    wr_trade = n_wins / n_trades if n_trades else 0
    avg_entry = sum(t["price"] for t in trades_fired) / n_trades if n_trades else 0
    ev_per_trade = pnl_total / n_trades if n_trades else 0

    # ---- Running cumulative PnL: max DD ----
    peak = 0.0
    cum = 0.0
    max_dd_abs = 0.0
    for t in trades_fired:
        cum += t["pnl"]
        if cum > peak:
            peak = cum
        dd = peak - cum
        if dd > max_dd_abs:
            max_dd_abs = dd

    # ---- Cluster analysis: group fired trades by window_ts ----
    clusters = defaultdict(list)
    for t in trades_fired:
        clusters[t["window_ts"]].append(t)

    n_clusters = len(clusters)
    all_win_clusters = 0
    all_lose_clusters = 0
    mixed_clusters = 0
    cluster_pnls = []
    for w_ts, cts in clusters.items():
        wins_in = sum(1 for t in cts if t["won"])
        losses_in = len(cts) - wins_in
        cluster_pnl = sum(t["pnl"] for t in cts)
        cluster_pnls.append((w_ts, cluster_pnl, len(cts), wins_in))
        if losses_in == 0:
            all_win_clusters += 1
        elif wins_in == 0:
            all_lose_clusters += 1
        else:
            mixed_clusters += 1

    worst_cluster_pnl = min(c[1] for c in cluster_pnls) if cluster_pnls else 0
    worst_cluster_size = 0
    for _, pnl, sz, _ in cluster_pnls:
        if pnl == worst_cluster_pnl:
            worst_cluster_size = sz
            break

    # ---- Max consecutive losing clusters (CLUSTER-LEVEL streak) ----
    cluster_pnls_sorted = sorted(cluster_pnls, key=lambda x: x[0])  # by window_ts
    max_losing_streak = 0
    cur_streak = 0
    for _, pnl, _, _ in cluster_pnls_sorted:
        if pnl < 0:
            cur_streak += 1
            if cur_streak > max_losing_streak:
                max_losing_streak = cur_streak
        else:
            cur_streak = 0

    return {
        "pnl_total": pnl_total,
        "n_trades": n_trades,
        "n_wins": n_wins,
        "wr_trade": wr_trade,
        "avg_entry": avg_entry,
        "ev_per_trade": ev_per_trade,
        "max_dd_abs": max_dd_abs,
        "n_clusters": n_clusters,
        "all_win_clusters": all_win_clusters,
        "all_lose_clusters": all_lose_clusters,
        "mixed_clusters": mixed_clusters,
        "all_win_pct": all_win_clusters / n_clusters if n_clusters else 0,
        "all_lose_pct": all_lose_clusters / n_clusters if n_clusters else 0,
        "mixed_pct": mixed_clusters / n_clusters if n_clusters else 0,
        "worst_cluster_pnl": worst_cluster_pnl,
        "worst_cluster_size": worst_cluster_size,
        "max_losing_cluster_streak": max_losing_streak,
        "skipped_lock": skipped_lock,
        "skipped_sol_dir": skipped_sol_dir,
    }


def per_coin_stats(signals):
    """Trade-level stats for a single coin (no lock, no dir filter)."""
    if not signals:
        return None
    n = len(signals)
    w = sum(1 for s in signals if s["won"])
    pnl = sum(TOKENS * (1.0 - s["price"]) if s["won"] else -TOKENS * s["price"]
              for s in signals)
    return {
        "n": n, "wins": w, "wr": w / n,
        "pnl": pnl,
        "ev_per_trade": pnl / n,
        "avg_entry": sum(s["price"] for s in signals) / n,
    }


def bootstrap_14d(signals, n_samples=BOOTSTRAP_N, window_days=BOOTSTRAP_WINDOW_DAYS):
    """Run n_samples random 14-day concurrent-window sims."""
    if not signals:
        return []
    ts_min = min(s["ts"] for s in signals).timestamp()
    ts_max = max(s["ts"] for s in signals).timestamp()
    window_sec = window_days * 86400
    if ts_max - ts_min < window_sec:
        return []
    rng = random.Random(SEED)
    results = []
    for _ in range(n_samples):
        start = rng.uniform(ts_min, ts_max - window_sec)
        end = start + window_sec
        subset = [s for s in signals if start <= s["ts"].timestamp() <= end]
        if not subset:
            continue
        r = simulate_concurrent_window(subset)
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
    print("=" * 85)
    print("V30.3 BACKTEST — full methodology on BTC/ETH/SOL")
    print("=" * 85)
    print(f"Config: mom>={MIN_MOM}  el={MIN_EL}-{MAX_EL}  price=${MIN_P}-${MAX_P}  tokens={TOKENS}")
    print(f"Late TTE rule: el_lo={MIN_EL} (non-negotiable)")
    print(f"Window direction lock: ON (first filled trade commits side)")
    print(f"Gates: n>={MIN_SIGNALS_PER_COIN} per coin, WR>={MIN_WR_PER_COIN:.0%}, EV>0")
    print()

    # ---- Pre-flight: load each coin separately, find common date range ----
    per_coin_signals = {}
    for coin, path in PBT_FILES:
        rows = load_coin(coin, path)
        if not rows:
            print(f"ABORT: {coin} has no signals")
            return
        per_coin_signals[coin] = rows
        print(f"  Loaded {coin}: {len(rows)} V30.3-filtered signals "
              f"({rows[0]['date']} -> {rows[-1]['date']})")

    # Common date range = max(min_date) to min(max_date)
    min_dates = {c: min(s["date"] for s in sigs) for c, sigs in per_coin_signals.items()}
    max_dates = {c: max(s["date"] for s in sigs) for c, sigs in per_coin_signals.items()}
    common_start = max(min_dates.values())
    common_end = min(max_dates.values())
    print(f"\n  Common date range: {common_start} -> {common_end} "
          f"({(common_end - common_start).days + 1} days)")

    # Trim to common range
    trimmed = {}
    for c, sigs in per_coin_signals.items():
        trimmed[c] = [s for s in sigs if common_start <= s["date"] <= common_end]
        print(f"  {c}: {len(trimmed[c])} signals in common range")
    print()

    # Combined signal pool for concurrent sim
    all_signals = []
    for sigs in trimmed.values():
        all_signals.extend(sigs)

    days_span = (common_end - common_start).days + 1

    # ===================================================================
    # GATE 1: 3-COIN AGREEMENT (per-coin independent stats, no lock)
    # ===================================================================
    print("=" * 85)
    print("GATE 1 — 3-COIN AGREEMENT (per-coin stats, each must pass independently)")
    print("=" * 85)
    print(f"{'Coin':>5} {'N':>6} {'Wins':>6} {'WR':>7} {'avgEP':>7} {'EV/trd':>9} "
          f"{'$total':>9} {'Status':>10}")
    print("-" * 85)
    gate1_pass = True
    gate1_fails = []
    for coin in ("BTC", "ETH", "SOL"):
        sigs = trimmed[coin]
        st = per_coin_stats(sigs)
        if st is None:
            print(f"{coin:>5} NO SIGNALS")
            gate1_pass = False
            gate1_fails.append(f"{coin}: no signals")
            continue
        passes_n = st["n"] >= MIN_SIGNALS_PER_COIN
        passes_wr = st["wr"] >= MIN_WR_PER_COIN
        passes_ev = st["ev_per_trade"] > 0
        ok = passes_n and passes_wr and passes_ev
        status = "PASS" if ok else "FAIL"
        if not ok:
            gate1_pass = False
            reasons = []
            if not passes_n: reasons.append(f"n={st['n']}<{MIN_SIGNALS_PER_COIN}")
            if not passes_wr: reasons.append(f"wr={st['wr']:.1%}<{MIN_WR_PER_COIN:.0%}")
            if not passes_ev: reasons.append(f"ev=${st['ev_per_trade']:+.3f}")
            gate1_fails.append(f"{coin}: {', '.join(reasons)}")
        print(f"{coin:>5} {st['n']:>6} {st['wins']:>6} {st['wr']:>6.1%} "
              f"${st['avg_entry']:>5.2f}  ${st['ev_per_trade']:>+6.3f}  "
              f"${st['pnl']:>+7.2f} {status:>10}")
    print()
    print(f"Gate 1: {'PASS' if gate1_pass else 'FAIL'}")
    if gate1_fails:
        for f in gate1_fails:
            print(f"  - {f}")
    print()

    # ===================================================================
    # GATE 2: CONCURRENT-WINDOW SIMULATION (with V30.3 direction lock)
    # ===================================================================
    print("=" * 85)
    print("GATE 2 — CONCURRENT-WINDOW SIM (V30.3 window direction lock ON)")
    print("=" * 85)
    sim = simulate_concurrent_window(all_signals)
    dollars_per_day = sim["pnl_total"] / days_span

    print(f"Date span: {days_span} days    Total filtered signals: {len(all_signals)}")
    print(f"Signals skipped by window lock: {sim['skipped_lock']}")
    print(f"Signals skipped by SOL dir filter approx: {sim['skipped_sol_dir']}")
    print()
    print(f"Trade-level (fired trades):")
    print(f"  Trades fired:   {sim['n_trades']}")
    print(f"  Wins:           {sim['n_wins']}")
    print(f"  Trade WR:       {sim['wr_trade']:.1%}")
    print(f"  Avg entry:      ${sim['avg_entry']:.2f}")
    print(f"  EV/trade:       ${sim['ev_per_trade']:+.3f}")
    print(f"  Total PnL:      ${sim['pnl_total']:+.2f}")
    print(f"  $/day:          ${dollars_per_day:+.2f}")
    print(f"  Max DD (abs):   ${sim['max_dd_abs']:.2f}")
    print()
    print(f"Cluster-level (windows):")
    print(f"  Total clusters:    {sim['n_clusters']}")
    print(f"  All-win %:         {sim['all_win_pct']:.1%}   ({sim['all_win_clusters']})")
    print(f"  All-lose %:        {sim['all_lose_pct']:.1%}   ({sim['all_lose_clusters']})")
    print(f"  Mixed %:           {sim['mixed_pct']:.1%}   ({sim['mixed_clusters']})")
    print(f"  Worst cluster:     ${sim['worst_cluster_pnl']:+.2f} "
          f"({sim['worst_cluster_size']} trades)")
    print(f"  Max losing cluster streak: {sim['max_losing_cluster_streak']}")
    print()

    # ===================================================================
    # GATE 3: BOOTSTRAP 200 x 14-DAY (informational for standalone V30.3)
    # ===================================================================
    print("=" * 85)
    print(f"GATE 3 — BOOTSTRAP {BOOTSTRAP_N} x {BOOTSTRAP_WINDOW_DAYS}-day samples")
    print("=" * 85)
    samples = bootstrap_14d(all_signals)
    if not samples:
        print("  Not enough data span for bootstrap")
    else:
        pnls = [s["pnl_total"] for s in samples]
        dds = [s["max_dd_abs"] for s in samples]
        worst_windows = [s["worst_cluster_pnl"] for s in samples]
        all_lose_pcts = [s["all_lose_pct"] for s in samples]

        print(f"Bootstrap sample count: {len(samples)}")
        print()
        print(f"{'Metric':>20} {'Min':>10} {'P10':>10} {'P50':>10} "
              f"{'P90':>10} {'Max':>10}")
        print("-" * 85)
        def show(label, xs, fmt="{:+.2f}"):
            print(f"{label:>20} {fmt.format(min(xs)):>10} {fmt.format(percentile(xs, 0.1)):>10} "
                  f"{fmt.format(percentile(xs, 0.5)):>10} {fmt.format(percentile(xs, 0.9)):>10} "
                  f"{fmt.format(max(xs)):>10}")
        show("14d PnL ($)", pnls)
        show("Max DD ($)", dds)
        show("Worst cluster ($)", worst_windows)
        show("All-lose %", all_lose_pcts, "{:.1%}")
        print()
        worst_14d = min(pnls)
        median_14d = percentile(pnls, 0.5)
        print(f"  Worst 14d sample: ${worst_14d:+.2f}")
        print(f"  Median 14d: ${median_14d:+.2f}")
        print(f"  Samples with negative PnL: "
              f"{sum(1 for p in pnls if p < 0)}/{len(samples)}")
        print()

    # ===================================================================
    # FINAL VERDICT
    # ===================================================================
    print("=" * 85)
    print("VERDICT")
    print("=" * 85)
    # Since V30.3 is the currently-deployed baseline, gates 4 (dominance vs
    # current) and 5 (worst-case better than current) are not applicable here.
    # We report status on the gates we CAN evaluate:
    checklist = [
        ("Late TTE rule (el_lo >= 150)", MIN_EL >= 150),
        ("Window direction lock ON", True),
        ("Gate 1: 3-coin agreement", gate1_pass),
        ("Gate 2: concurrent-window sim positive EV", sim["pnl_total"] > 0),
        ("Gate 2: cluster all-lose % < 15%", sim["all_lose_pct"] < 0.15),
    ]
    for label, ok in checklist:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}")
    print()
    print("V30.3 is CURRENTLY DEPLOYED — this run establishes the baseline for future")
    print("head-to-head comparisons. No deployment decision needed; pure health check.")


if __name__ == "__main__":
    main()
