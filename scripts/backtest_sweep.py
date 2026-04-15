#!/usr/bin/env python3
"""
Recency-Weighted Backtest Sweep — ranked by $/day.

Recent signals weighted more heavily via exponential decay.
Measures trades/day from actual data, then ranks by daily dollar growth.

Usage: python scripts/backtest_sweep.py --data /tmp/collector_v2.csv --bankroll 85
"""
import csv, random, math, argparse
from collections import defaultdict
from datetime import datetime, timezone


# === EXECUTION COST MODEL (empirical from 72 matched pairs) ===
FOK_TOLERANCE = 0.04
SCOOP_PROB = 0.10
SCOOP_AMOUNT = 0.15
TAKER_FEE_5M = 0.0176

# === RECENCY WEIGHTING ===
HALF_LIFE_HOURS = 24.0
DECAY_LAMBDA = math.log(2) / HALF_LIFE_HOURS


def apply_as_tax(paper_entry):
    live = paper_entry + FOK_TOLERANCE
    if random.random() < SCOOP_PROB:
        live += SCOOP_AMOUNT
    return min(live, 0.99)


def load_signals(path):
    signals = []
    with open(path) as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row["timestamp"])
            signals.append({
                "slug": row["market_slug"], "coin": row["coin"],
                "side": row["side"], "threshold": float(row["threshold"]),
                "entry_price": float(row["entry_price"]),
                "momentum": float(row["momentum"]),
                "elapsed": float(row["elapsed"]),
                "outcome": row["outcome"],
                "timestamp": ts,
            })
    return signals


def compute_weights(signals):
    if not signals:
        return []
    latest = max(s["timestamp"] for s in signals)
    weights = []
    for s in signals:
        age_hours = (latest - s["timestamp"]).total_seconds() / 3600
        w = math.exp(-DECAY_LAMBDA * age_hours)
        weights.append(w)
    return weights


def get_trades(signals, cfg):
    seen = set()
    filtered = []
    for s in signals:
        if s["threshold"] < cfg["mm"]: continue
        key = (s["slug"], s["side"])
        if key in seen: continue
        if s["entry_price"] <= 0: continue
        if s["entry_price"] < cfg["mnp"] or s["entry_price"] > cfg["mxp"]: continue
        if s["elapsed"] < cfg["mne"] or s["elapsed"] > cfg["mxe"]: continue
        seen.add(key)
        filtered.append(s)
    return filtered


def trades_per_day(trades):
    if not trades:
        return 0
    days = defaultdict(int)
    for t in trades:
        days[t["timestamp"].strftime("%Y-%m-%d")] += 1
    return len(trades) / max(len(days), 1)


def analyze(trades):
    if not trades or len(trades) < 100:
        return None

    n = len(trades)
    weights = compute_weights(trades)
    total_weight = sum(weights)

    win_weight = sum(w for t, w in zip(trades, weights) if t["outcome"] == "won")
    wr = win_weight / total_weight
    wins_raw = sum(1 for t in trades if t["outcome"] == "won")

    expected_as = FOK_TOLERANCE + SCOOP_PROB * SCOOP_AMOUNT
    paper_avg = sum(t["entry_price"] for t in trades) / n
    live_avg = min(paper_avg + expected_as, 0.99)
    fee = live_avg * (1 - live_avg) * TAKER_FEE_5M

    win_per_token = 1.0 - live_avg - fee
    loss_per_token = live_avg + fee
    if win_per_token <= 0:
        return None

    b = win_per_token / loss_per_token
    be = loss_per_token / (win_per_token + loss_per_token)
    buf = wr - be
    ev = wr * win_per_token - (1 - wr) * loss_per_token

    if buf <= 0 or ev <= 0:
        return None

    full_k = wr - (1 - wr) / b
    if full_k <= 0:
        return None

    qk = full_k * 0.25
    tpd = trades_per_day(trades)

    sum_w2 = sum(w * w for w in weights)
    n_eff = (total_weight ** 2) / sum_w2 if sum_w2 > 0 else n
    se = math.sqrt(wr * (1 - wr) / n_eff)
    ci_low = wr - 1.96 * se

    # Last 24h WR
    latest = max(t["timestamp"] for t in trades)
    recent = [t for t in trades if (latest - t["timestamp"]).total_seconds() < 86400]
    r_wr = sum(1 for t in recent if t["outcome"] == "won") / len(recent) if recent else 0

    return {
        "trades": trades, "weights": weights,
        "n": n, "n_eff": n_eff, "wr": wr,
        "w": wins_raw, "l": n - wins_raw,
        "paper_avg": paper_avg, "live_avg": live_avg,
        "win_tok": win_per_token, "loss_tok": loss_per_token,
        "be": be, "buf": buf, "ev": ev,
        "b": b, "full_k": full_k, "qk": qk,
        "ci_low": ci_low, "fee": fee,
        "tpd": tpd, "r_wr": r_wr, "n_recent": len(recent),
    }


def monte_carlo(stats, bankroll, n_trades=100, sims=3000, fixed_pct=0.03):
    trades = stats["trades"]
    weights = stats["weights"]

    total_w = sum(weights)
    norm_weights = [w / total_w for w in weights]

    finals = []
    for _ in range(sims):
        bal = bankroll
        sampled = random.choices(trades, weights=norm_weights, k=n_trades)
        for t in sampled:
            live_entry = apply_as_tax(t["entry_price"])
            fee = live_entry * (1 - live_entry) * TAKER_FEE_5M

            bet = bal * fixed_pct
            floor = live_entry * 5  # 5-token minimum
            if bet < floor: bet = floor
            if bet > bal * 0.5: bet = bal * 0.5
            if bet > bal: bet = bal

            if t["outcome"] == "won":
                tokens = bet / live_entry
                bal += (1.0 - live_entry - fee) * tokens
            else:
                bal -= bet
        finals.append(bal)

    finals.sort()
    med = finals[sims // 2]
    profit = med - bankroll
    tpd = stats["tpd"]
    days_for_n = n_trades / tpd if tpd > 0 else 999
    dpd = profit / days_for_n if days_for_n > 0 else 0

    return {
        "med": med,
        "p10": finals[sims // 10],
        "p90": finals[9 * sims // 10],
        "bust": sum(1 for f in finals if f < 10) / sims * 100,
        "growth_per_trade": (med / bankroll) ** (1 / n_trades) - 1,
        "dpd": dpd,
        "days_for_n": days_for_n,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", default="/tmp/collector_v2.csv")
    parser.add_argument("--bankroll", type=float, default=85.0)
    parser.add_argument("--trades", type=int, default=100, help="Trades per simulation")
    parser.add_argument("--sims", type=int, default=3000)
    parser.add_argument("--half-life", type=float, default=HALF_LIFE_HOURS,
                        help="Recency half-life in hours (default 24)")
    parser.add_argument("--fixed-pct", type=float, default=0.03)
    args = parser.parse_args()

    global DECAY_LAMBDA
    DECAY_LAMBDA = math.log(2) / args.half_life

    signals = load_signals(args.data)
    print(f"STEP 1: {len(signals)} signals loaded")

    if signals:
        weights = compute_weights(signals)
        latest = max(s["timestamp"] for s in signals)
        oldest = min(s["timestamp"] for s in signals)
        span_h = (latest - oldest).total_seconds() / 3600
        print(f"  Data span: {span_h:.1f}h ({span_h/24:.1f} days)")
        print(f"  Recency half-life: {args.half_life:.0f}h")
        print(f"  Oldest signal weight: {min(weights):.3f} | Newest: {max(weights):.3f}")
        print(f"  Effective weight of last 24h vs total: {sum(w for s, w in zip(signals, weights) if (latest - s['timestamp']).total_seconds() < 86400) / sum(weights):.0%}")

    configs = []
    for mm in [0.0003, 0.0005, 0.0007, 0.001, 0.0012, 0.0015, 0.002]:
        for mnp in [0.01, 0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60]:
            for mxp in [0.20, 0.25, 0.30, 0.35, 0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.80, 0.90, 0.99]:
                if mnp >= mxp: continue
                for mne in [0, 30, 60, 90, 120]:
                    for mxe in [120, 150, 180, 210, 240, 270, 295]:
                        if mne >= mxe: continue
                        configs.append({"mm": mm, "mnp": mnp, "mxp": mxp, "mne": mne, "mxe": mxe})
    print(f"STEP 2: {len(configs):,} permutations")

    passing = []
    for cfg in configs:
        trades = get_trades(signals, cfg)
        stats = analyze(trades)
        if stats:
            passing.append((cfg, stats))
    print(f"STEP 3: {len(passing)} pass (100+ sigs, recency-weighted buf>0, ev>0)")

    # Pre-screen by EV × trades/day (daily dollar growth proxy)
    passing.sort(key=lambda x: -x[1]["ev"] * x[1]["tpd"])
    candidates = passing[:300]
    print(f"STEP 4: Top {len(candidates)} pre-screened by EV × trades/day")

    random.seed(42)
    print(f"STEP 5: Monte Carlo {args.sims} sims × {args.trades} trades, ${args.bankroll:.0f}, {args.fixed_pct:.0%} fixed")
    print(f"  AS: +${FOK_TOLERANCE} fixed + {SCOOP_PROB:.0%} chance of +${SCOOP_AMOUNT}")
    print(f"  Recency-weighted sampling (half-life={args.half_life:.0f}h)")
    print(f"  RANKED BY $/DAY")
    print()

    results = []
    for cfg, stats in candidates:
        mc = monte_carlo(stats, args.bankroll, args.trades, args.sims, args.fixed_pct)
        results.append((cfg, stats, mc))

    # RANK BY $/DAY
    results.sort(key=lambda x: -x[2]["dpd"])

    W = 155
    print("=" * W)
    print(f"TOP 25 BY $/DAY (recency-weighted, AS-adjusted, {args.fixed_pct:.0%} fixed, ${args.bankroll:.0f} start)")
    print("=" * W)
    print(f"{'#':>2} {'Config':40s} {'wWR':>5} {'24hWR':>6} {'Buf':>5} {'t/d':>5} {'n':>4} {'nEff':>5} {'Med$':>6} {'P10$':>6} {'$/day':>6} {'Bust':>5} {'CI':>3}")
    print("-" * W)
    for i, (c, s, mc) in enumerate(results[:25]):
        cs = f"m>={c['mm']:.4f} ${c['mnp']:.2f}-${c['mxp']:.2f} e={c['mne']}-{c['mxe']}s"
        ci = "OK" if s["ci_low"] > s["be"] else "LOW"
        print(f"{i+1:2d} {cs:40s} {s['wr']:4.0%} {s['r_wr']:5.0%} {s['buf']:+4.0%} {s['tpd']:5.1f} {s['n']:4d} {s['n_eff']:5.0f} ${mc['med']:5.0f} ${mc['p10']:5.0f} ${mc['dpd']:5.1f} {mc['bust']:4.1f}% {ci}")

    if results:
        c, s, mc = results[0]
        cs = f"m>={c['mm']:.4f} ${c['mnp']:.2f}-${c['mxp']:.2f} e={c['mne']}-{c['mxe']}s"
        n_eff = s['n_eff']
        se = math.sqrt(s['wr'] * (1 - s['wr']) / n_eff)
        print(f"\n{'=' * W}")
        print(f"#1: {cs}")
        print(f"{'=' * W}")
        print(f"  {s['w']}W/{s['l']}L = {s['wr']:.1%} weighted WR on {s['n']} signals (n_eff={n_eff:.0f})")
        print(f"  Last 24h: {s['r_wr']:.0%} on {s['n_recent']} signals")
        print(f"  Trades/day: {s['tpd']:.1f}")
        print(f"  Paper entry: ${s['paper_avg']:.2f} → Live entry: ${s['live_avg']:.2f}")
        print(f"  Live win: ${s['win_tok']:.3f}/tok | Live loss: ${s['loss_tok']:.3f}/tok | Ratio: {s['b']:.3f}")
        print(f"  BE: {s['be']:.1%} | Buffer: {s['buf']:+.1%}")
        print(f"  CI: {s['wr']-1.96*se:.1%}—{s['wr']+1.96*se:.1%} vs BE {s['be']:.1%} → {'PASSES' if s['ci_low']>s['be'] else 'FAILS'}")
        print(f"\n  After {args.trades} trades ({mc['days_for_n']:.1f} days): ${args.bankroll:.0f} → median ${mc['med']:.0f} | P10 ${mc['p10']:.0f} | P90 ${mc['p90']:.0f}")
        print(f"  $/day: ${mc['dpd']:.1f}")
        print(f"  Bust: {mc['bust']:.1f}%")

        latest = max(t["timestamp"] for t in s["trades"])
        recent = [t for t in s["trades"] if (latest - t["timestamp"]).total_seconds() < 86400]
        older = [t for t in s["trades"] if (latest - t["timestamp"]).total_seconds() >= 86400]
        if recent and older:
            r_wr = sum(1 for t in recent if t["outcome"] == "won") / len(recent)
            o_wr = sum(1 for t in older if t["outcome"] == "won") / len(older)
            print(f"\n  Recency breakdown:")
            print(f"    Last 24h: {sum(1 for t in recent if t['outcome']=='won')}W/{sum(1 for t in recent if t['outcome']!='won')}L = {r_wr:.1%} (n={len(recent)})")
            print(f"    Older:    {sum(1 for t in older if t['outcome']=='won')}W/{sum(1 for t in older if t['outcome']!='won')}L = {o_wr:.1%} (n={len(older)})")

        coin_stats = defaultdict(lambda: {"w": 0, "l": 0})
        for t in s["trades"]:
            if t["outcome"] == "won": coin_stats[t["coin"]]["w"] += 1
            else: coin_stats[t["coin"]]["l"] += 1
        print(f"\n  Per-coin:")
        for coin in sorted(coin_stats):
            d = coin_stats[coin]; total = d["w"] + d["l"]
            print(f"    {coin:5s}: {d['w']:3d}W/{d['l']:3d}L = {d['w']/total:.0%} (n={total})")

    # Config comparison
    print(f"\n{'=' * W}")
    print("CONFIG COMPARISON:")
    print("=" * W)
    compare = [
        ("V18", {"mm": 0.002, "mnp": 0.65, "mxp": 0.99, "mne": 0, "mxe": 120}),
        ("V14.2", {"mm": 0.002, "mnp": 0.55, "mxp": 0.99, "mne": 0, "mxe": 210}),
        ("V15", {"mm": 0.002, "mnp": 0.55, "mxp": 0.99, "mne": 0, "mxe": 150}),
    ]
    for name, cfg_target in compare:
        for i, (c, s, mc) in enumerate(results):
            if all(c[k] == cfg_target[k] for k in cfg_target):
                print(f"  {name}: Rank #{i+1} | {s['wr']:.1%} wWR | {s['tpd']:.0f}t/d | ${mc['dpd']:.1f}/day | Med ${mc['med']:.0f}")
                break
        else:
            trades = get_trades(signals, cfg_target)
            stats = analyze(trades)
            if stats:
                print(f"  {name}: {stats['wr']:.1%} wWR | {stats['tpd']:.0f}t/d | Buf {stats['buf']:+.1%} | Not in top 300")
            else:
                print(f"  {name}: Failed recency-weighted filter")


if __name__ == "__main__":
    main()
