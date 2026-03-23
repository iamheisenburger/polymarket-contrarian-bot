#!/usr/bin/env python3
"""
Instant grid search on cached PolyBacktest data.
Run polybacktest_download.py first, then this runs in <1 second.

Usage: python scripts/polybacktest_sweep.py --coins btc sol eth
"""
import json, os, sys, math, random, argparse

CACHE_DIR = "data/polybacktest_cache"


def load_signals(coins):
    all_signals = []
    for coin in coins:
        path = os.path.join(CACHE_DIR, f"{coin}_5m_signals.json")
        if not os.path.exists(path):
            print(f"No cached data for {coin}. Run polybacktest_download.py first.")
            continue
        with open(path) as f:
            sigs = json.load(f)
        print(f"Loaded {len(sigs)} {coin.upper()} signals")
        all_signals.extend(sigs)
    return all_signals


def grid_search(signals, min_n=50):
    mom_vals = [0.0003, 0.0004, 0.0005, 0.0006, 0.0007, 0.0008, 0.001, 0.0012, 0.0015]
    fv_vals = [0.55, 0.58, 0.60, 0.65, 0.70, 0.72, 0.75, 0.80]
    edge_vals = [0.03, 0.05, 0.08, 0.10, 0.12, 0.15, 0.18, 0.20, 0.25]
    max_entry_vals = [0.55, 0.60, 0.65, 0.70, 0.75]

    results = []
    for mm in mom_vals:
        for mf in fv_vals:
            for me in edge_vals:
                for mx in max_entry_vals:
                    w = l = 0
                    pnl = 0.0
                    wag = 0.0
                    for s in signals:
                        if s["momentum"] < mm:
                            continue
                        if s["fv"] < mf:
                            continue
                        if s["ask"] > mx:
                            continue
                        # Live edge = FV - (ask + 0.07) - fee
                        worst_fill = min(s["ask"] + 0.07, mx)
                        edge = s["fv"] - worst_fill - 0.005
                        if edge < me:
                            continue
                        if s["won"]:
                            w += 1
                        else:
                            l += 1
                        pnl += s["pnl"]
                        wag += s["ask"]
                    t = w + l
                    if t < min_n:
                        continue
                    wr = w / t * 100
                    avg_e = wag / t
                    results.append({
                        "mom": mm, "fv": mf, "edge": me, "mx": mx,
                        "n": t, "wr": wr, "be": avg_e * 100,
                        "edge_pp": wr - avg_e * 100,
                        "ev": pnl / t, "pnl": pnl,
                    })

    results.sort(key=lambda x: x["pnl"], reverse=True)
    return results


def overfit_check(signals, target_pnl, n_trials=5000, min_n=50):
    beats = 0
    for _ in range(n_trials):
        rm = random.uniform(0.0002, 0.003)
        rf = random.uniform(0.50, 0.85)
        re = random.uniform(0.02, 0.30)
        rmx = random.uniform(0.50, 0.85)
        w = l = 0
        p = 0.0
        for s in signals:
            if s["momentum"] < rm or s["fv"] < rf or s["ask"] > rmx:
                continue
            wf = min(s["ask"] + 0.07, rmx)
            edge = s["fv"] - wf - 0.005
            if edge < re:
                continue
            if s["won"]:
                w += 1
            else:
                l += 1
            p += s["pnl"]
        if (w + l) >= min_n and p >= target_pnl:
            beats += 1
    return beats, n_trials


def per_coin_breakdown(signals, config):
    from collections import defaultdict
    coins = defaultdict(lambda: {"w": 0, "l": 0, "pnl": 0.0})
    for s in signals:
        if s["momentum"] < config["mom"] or s["fv"] < config["fv"] or s["ask"] > config["mx"]:
            continue
        wf = min(s["ask"] + 0.07, config["mx"])
        edge = s["fv"] - wf - 0.005
        if edge < config["edge"]:
            continue
        c = s["coin"].upper()
        if s["won"]:
            coins[c]["w"] += 1
        else:
            coins[c]["l"] += 1
        coins[c]["pnl"] += s["pnl"]
    return dict(coins)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["btc"], help="Coins to analyze")
    parser.add_argument("--min-n", type=int, default=50, help="Minimum trades per config")
    args = parser.parse_args()

    coins = [c.lower() for c in args.coins]
    signals = load_signals(coins)
    if not signals:
        print("No signals loaded.")
        return

    total = len(signals)
    won = sum(1 for s in signals if s["won"])
    print(f"\nTotal: {total} signals, {won}W/{total-won}L, base WR: {won/total*100:.1f}%")

    print("\nRunning grid search...")
    import time
    t0 = time.time()
    results = grid_search(signals, min_n=args.min_n)
    elapsed = time.time() - t0
    print(f"Tested configs -> {len(results)} with n>={args.min_n} ({elapsed:.2f}s)")

    print(f"\n{'#':>3} {'Mom':>7} {'FV':>5} {'Edge':>5} {'MaxE':>5} {'N':>6} {'WR':>6} {'BE':>6} {'Epp':>7} {'EV/t':>7} {'PnL':>9}")
    print("-" * 80)
    for i, r in enumerate(results[:25]):
        v52 = " <--V5.2" if r["mom"] == 0.0005 and r["fv"] == 0.70 and r["edge"] == 0.15 and r["mx"] == 0.70 else ""
        print(f"{i+1:>3} {r['mom']:>7.4f} {r['fv']:>5.2f} {r['edge']:>5.2f} {r['mx']:>5.2f} {r['n']:>6} {r['wr']:>5.1f}% {r['be']:>5.1f}% {r['edge_pp']:>+5.1f}pp ${r['ev']:>5.02f} ${r['pnl']:>8.2f}{v52}")

    # V5.2 rank
    v52 = next((r for r in results if r["mom"] == 0.0005 and r["fv"] == 0.70 and r["edge"] == 0.15 and r["mx"] == 0.70), None)
    v52_rank = next((i + 1 for i, r in enumerate(results) if r is v52), None) if v52 else None
    if v52:
        print(f"\nV5.2: #{v52_rank}/{len(results)}, n={v52['n']}, WR={v52['wr']:.1f}%, +{v52['edge_pp']:.1f}pp, PnL=${v52['pnl']:.2f}")
    else:
        print(f"\nV5.2 had <{args.min_n} trades")

    # Overfit check
    if results:
        beats, trials = overfit_check(signals, results[0]["pnl"], min_n=args.min_n)
        print(f"Overfit: {beats}/{trials} ({beats/trials*100:.1f}%)")

    # Per-coin for #1
    if results:
        print(f"\n#1 config per-coin:")
        breakdown = per_coin_breakdown(signals, results[0])
        for coin in sorted(breakdown):
            d = breakdown[coin]
            t = d["w"] + d["l"]
            wr = d["w"] / t * 100 if t > 0 else 0
            print(f"  {coin:<5} {d['w']}W/{d['l']}L  WR={wr:.1f}%  PnL=${d['pnl']:.2f}")


if __name__ == "__main__":
    main()
