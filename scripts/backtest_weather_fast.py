#!/usr/bin/env python3
"""
Fast weather backtest: simulate $50 paper balance over Feb 15-21 (7 days).

Speed: bulk-fetches ALL closed events in ONE call, parallel ensemble fetches.
Entry price estimation: since historical orderbooks are gone, we estimate
market prices from the probability distribution (market ~= 1/N baseline
adjusted by how obvious the bucket is).
"""

import json
import math
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

import requests

# ── Config ────────────────────────────────────────────────────────────
MIN_EDGE = 0.05
KELLY_FRAC = 0.5
MAX_POS_PCT = 0.20
MIN_ENTRY = 0.01
MAX_ENTRY = 0.50
START_BALANCE = 50.0

CORR = {
    "NYC": (0.0, 3.0, "fahrenheit"), "London": (0.6, 0.5, "celsius"),
    "Chicago": (-0.2, 1.5, "fahrenheit"), "Miami": (0.8, 0.3, "fahrenheit"),
    "Dallas": (1.5, 1.5, "fahrenheit"), "Paris": (0.7, 0.8, "celsius"),
    "Toronto": (1.1, 1.0, "celsius"), "Atlanta": (1.4, 1.0, "fahrenheit"),
    "Ankara": (1.4, 0.5, "celsius"), "Wellington": (2.0, 0.8, "celsius"),
    "Sao Paulo": (1.1, 1.2, "celsius"), "Buenos Aires": (1.5, 0.8, "celsius"),
}
LATLONS = {
    "NYC": (40.7772, -73.8726), "London": (51.5053, 0.0553),
    "Chicago": (41.9742, -87.9073), "Miami": (25.7959, -80.2870),
    "Dallas": (32.8471, -96.8518), "Paris": (49.0097, 2.5478),
    "Toronto": (43.6772, -79.6306), "Atlanta": (33.6407, -84.4277),
    "Ankara": (40.1281, 32.9951), "Wellington": (-41.3272, 174.8051),
    "Sao Paulo": (-23.4356, -46.4731), "Buenos Aires": (-34.8222, -58.5358),
}
SLUGS = {
    "NYC": "nyc", "London": "london", "Chicago": "chicago", "Miami": "miami",
    "Dallas": "dallas", "Paris": "paris", "Toronto": "toronto", "Atlanta": "atlanta",
    "Ankara": "ankara", "Wellington": "wellington", "Sao Paulo": "sao-paulo",
    "Buenos Aires": "buenos-aires",
}
REV_SLUGS = {v: k for k, v in SLUGS.items()}


# ── Math ──────────────────────────────────────────────────────────────
def ncdf(x):
    return 0.5 * (1.0 + math.erf(x / 1.4142135623730951))

def compute_prob(members, lo, hi, bias, extra):
    c = [m + bias for m in members]
    n = len(c)
    mu = sum(c) / n
    v = sum((m - mu) ** 2 for m in c) / n
    s = math.sqrt(v + extra ** 2) if (v + extra ** 2) > 0 else 1.0
    cnt = sum(1 for m in c if lo <= m < hi) / n
    pn = ncdf((hi - mu) / s) - ncdf((lo - mu) / s)
    return 0.6 * pn + 0.4 * cnt

def parse_bucket(q):
    m = re.search(r"between\s+(-?\d+)[\u2013\-](-?\d+)\s*[^\d]*([FC])", q, re.I)
    if m: return float(m.group(1)), float(m.group(2)) + 1
    m = re.search(r"(-?\d+)\s*[^\d]*([FC])\s+or\s+higher", q, re.I)
    if m: return float(m.group(1)), 200
    m = re.search(r"(-?\d+)\s*[^\d]*([FC])\s+or\s+(?:below|lower)", q, re.I)
    if m: return -200, float(m.group(1)) + 1
    m = re.search(r"be\s+(-?\d+)\s*[^\d]*([FC])", q, re.I)
    if m: return float(m.group(1)), float(m.group(1)) + 1
    return None

def extract_city(event_slug):
    m = re.search(r"highest-temperature-in-(.+?)-on-", event_slug)
    if m:
        return REV_SLUGS.get(m.group(1))
    return None

def extract_date(event_slug):
    m = re.search(r"-on-(\w+)-(\d{1,2})-(\d{4})$", event_slug)
    if m:
        from datetime import datetime
        try:
            dt = datetime.strptime(f"{m.group(1)} {m.group(2)} {m.group(3)}", "%B %d %Y")
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass
    return None


# ── Data Fetching (FAST) ──────────────────────────────────────────────
def fetch_all_closed_events():
    """ONE call to get all closed temperature events."""
    all_events = []
    offset = 0
    while True:
        resp = requests.get("https://gamma-api.polymarket.com/events", params={
            "tag_slug": "temperature",
            "closed": "true",
            "limit": 100,
            "offset": offset,
        }, timeout=15)
        batch = resp.json()
        if not batch:
            break
        all_events.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return all_events


def fetch_ensemble_parallel(days_back=10):
    """Parallel ensemble fetches for all cities."""
    results = {}

    def _fetch(city):
        lat, lon = LATLONS[city]
        bias, extra, unit = CORR[city]
        resp = requests.get("https://ensemble-api.open-meteo.com/v1/ensemble", params={
            "latitude": lat, "longitude": lon,
            "daily": "temperature_2m_max",
            "temperature_unit": unit, "timezone": "auto",
            "models": "ecmwf_ifs025",
            "past_days": days_back, "forecast_days": 1,
        }, timeout=15)
        d = resp.json().get("daily", {})
        dates = d.get("time", [])
        city_data = {}
        for i, dt in enumerate(dates):
            mems = []
            for k, v in d.items():
                if k.startswith("temperature_2m_max_member") and i < len(v) and v[i] is not None:
                    mems.append(float(v[i]))
            if mems:
                city_data[dt] = mems
        return city, city_data

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(_fetch, c) for c in LATLONS]
        for f in as_completed(futs):
            city, data = f.result()
            results[city] = data

    return results


# ── Simulation ────────────────────────────────────────────────────────
def estimate_market_price(model_prob, all_probs, is_edge_bucket):
    """
    Estimate what the market was likely pricing this bucket at.

    Logic: Markets are semi-efficient. The favorite bucket typically trades
    at 30-50c, mid buckets at 10-20c, tails at 1-5c.

    Our edge comes from the market underpricing certain buckets.
    From live scan data, typical market price ~ model_prob * 0.60-0.80.
    Use 0.70 as central estimate (market was ~70% of our model's prob).
    """
    # Conservative estimate: market was 75% of our model price
    # This means our edge is ~25% of model_prob
    estimated = model_prob * 0.75
    # Floor at 1c (some buckets trade at 1c minimum)
    return max(0.01, min(estimated, 0.95))


def run_simulation():
    print("=" * 70)
    print("  FAST WEATHER BACKTEST — Simulated $50 Paper Trading")
    print("=" * 70)

    # Step 1: Bulk fetch
    print("\nFetching all closed temperature events (1 API call)...")
    events = fetch_all_closed_events()
    print(f"  Got {len(events)} closed events")

    print("Fetching ensemble data for 12 cities (parallel)...")
    ensemble = fetch_ensemble_parallel(days_back=10)
    total_days = sum(len(v) for v in ensemble.values())
    print(f"  Got {total_days} city-days of ensemble data\n")

    # Step 2: Parse all events into structured data
    event_data = []  # list of (city, date, buckets_with_winner)
    skipped = 0

    for evt in events:
        slug = evt.get("slug", "")
        city = extract_city(slug)
        date = extract_date(slug)
        if not city or not date or city not in CORR:
            skipped += 1
            continue

        markets = evt.get("markets", [])
        if not all(m.get("closed", False) for m in markets):
            skipped += 1
            continue

        buckets = []
        for mkt in markets:
            q = mkt.get("question", "")
            b = parse_bucket(q)
            if not b:
                continue
            lo, hi = b
            prices_raw = mkt.get("outcomePrices", "")
            try:
                prices = json.loads(prices_raw) if isinstance(prices_raw, str) else prices_raw
            except:
                continue
            if not prices or len(prices) < 2:
                continue
            yp = float(prices[0])
            label = q.split("be ")[-1].split(" on ")[0] if "be " in q else q[:25]
            buckets.append({
                "lo": lo, "hi": hi, "label": label,
                "won": yp > 0.9,
            })

        if buckets and any(b["won"] for b in buckets):
            event_data.append((city, date, buckets))

    print(f"Parsed {len(event_data)} fully-resolved events ({skipped} skipped)\n")

    # Step 3: Simulate trading with different entry price assumptions
    for scenario_name, price_mult in [
        ("CONSERVATIVE (market=80% of model)", 0.80),
        ("REALISTIC (market=70% of model)", 0.70),
        ("AGGRESSIVE (market=60% of model)", 0.60),
    ]:
        balance = START_BALANCE
        wins = 0
        losses = 0
        total_cost = 0.0
        total_payout = 0.0
        trades = []
        events_traded = 0

        for city, date, buckets in sorted(event_data, key=lambda x: x[1]):
            members = ensemble.get(city, {}).get(date)
            if not members:
                continue

            bias, extra, unit = CORR[city]

            # Compute model probs for all buckets
            for bkt in buckets:
                bkt["model_prob"] = compute_prob(members, bkt["lo"], bkt["hi"], bias, extra)

            # Sort by model prob descending
            ranked = sorted(buckets, key=lambda b: b["model_prob"], reverse=True)

            # Trade buckets where we'd have edge
            traded_this_event = False
            for bkt in ranked:
                mp = bkt["model_prob"]
                est_price = mp * price_mult
                est_price = max(MIN_ENTRY, min(est_price, MAX_ENTRY))
                edge = mp - est_price

                if edge < MIN_EDGE:
                    continue
                if est_price < MIN_ENTRY or est_price > MAX_ENTRY:
                    continue

                # Kelly sizing
                if est_price <= 0 or est_price >= 1:
                    continue
                b_odds = (1.0 - est_price) / est_price
                kf = (mp * b_odds - (1 - mp)) / b_odds if b_odds > 0 else 0
                kf = max(0, min(kf, MAX_POS_PCT)) * KELLY_FRAC

                bet = balance * kf
                if bet < 1.0:
                    bet = min(1.0, balance * 0.15)
                if bet > balance or bet < 0.50:
                    continue

                num_tokens = bet / est_price
                cost = num_tokens * est_price

                if cost > balance:
                    continue

                # Execute
                balance -= cost
                total_cost += cost

                if bkt["won"]:
                    payout = num_tokens * 1.0
                    balance += payout
                    total_payout += payout
                    wins += 1
                    result = "W"
                else:
                    payout = 0
                    losses += 1
                    result = "L"

                trades.append({
                    "city": city, "date": date, "label": bkt["label"],
                    "entry": est_price, "model": mp, "edge": edge,
                    "cost": cost, "payout": payout, "result": result,
                    "balance": balance,
                })
                traded_this_event = True

            if traded_this_event:
                events_traded += 1

        # Print results
        print(f"\n{'─' * 70}")
        print(f"  Scenario: {scenario_name}")
        print(f"{'─' * 70}")
        total = wins + losses
        wr = wins / total * 100 if total else 0
        pnl = total_payout - total_cost
        roi = pnl / total_cost * 100 if total_cost else 0

        print(f"  Events traded: {events_traded}/{len(event_data)}")
        print(f"  Total trades:  {total} ({wins}W / {losses}L)")
        print(f"  Win rate:      {wr:.1f}%")
        print(f"  Total wagered: ${total_cost:.2f}")
        print(f"  Total payout:  ${total_payout:.2f}")
        print(f"  Net P&L:       ${pnl:+.2f} ({roi:+.1f}% ROI)")
        print(f"  Final balance: ${balance:.2f} (started ${START_BALANCE:.2f})")

        # Show trade log for realistic scenario
        if price_mult == 0.70 and trades:
            print(f"\n  Trade log ({len(trades)} trades):")
            print(f"  {'#':<4} {'Date':<12} {'City':<14} {'Bucket':<14} {'Entry':>6} {'Model':>7} {'Edge':>6} {'Cost':>6} {'Pay':>6} {'R':<3} {'Balance':>8}")
            print(f"  {'─' * 95}")
            for i, t in enumerate(trades, 1):
                print(f"  {i:<4} {t['date']:<12} {t['city']:<14} {t['label']:<14} "
                      f"${t['entry']:.2f} {t['model']:>6.1%} {t['edge']:>+5.1%} "
                      f"${t['cost']:>5.2f} ${t['payout']:>5.2f} {t['result']:<3} ${t['balance']:>7.2f}")

    # ── Additional insights ──────────────────────────────────────────
    print(f"\n{'=' * 70}")
    print("  ADDITIONAL INSIGHTS")
    print(f"{'=' * 70}")

    # Which cities are most profitable?
    city_stats = defaultdict(lambda: {"w": 0, "l": 0, "cost": 0, "pay": 0})
    # Use realistic scenario trades
    balance = START_BALANCE
    for city, date, buckets in sorted(event_data, key=lambda x: x[1]):
        members = ensemble.get(city, {}).get(date)
        if not members:
            continue
        bias, extra, unit = CORR[city]
        for bkt in buckets:
            bkt["model_prob"] = compute_prob(members, bkt["lo"], bkt["hi"], bias, extra)
        for bkt in sorted(buckets, key=lambda b: b["model_prob"], reverse=True):
            mp = bkt["model_prob"]
            ep = max(MIN_ENTRY, min(mp * 0.70, MAX_ENTRY))
            edge = mp - ep
            if edge < MIN_EDGE or ep < MIN_ENTRY or ep > MAX_ENTRY:
                continue
            if ep <= 0 or ep >= 1:
                continue
            b_odds = (1.0 - ep) / ep
            kf = (mp * b_odds - (1 - mp)) / b_odds if b_odds > 0 else 0
            kf = max(0, min(kf, MAX_POS_PCT)) * KELLY_FRAC
            bet = balance * kf
            if bet < 1.0:
                bet = min(1.0, balance * 0.15)
            if bet > balance or bet < 0.50:
                continue
            cost = bet
            balance -= cost
            if bkt["won"]:
                pay = (cost / ep) * 1.0
                balance += pay
            else:
                pay = 0
            cs = city_stats[city]
            cs["w" if bkt["won"] else "l"] += 1
            cs["cost"] += cost
            cs["pay"] += pay

    print(f"\n  Per-city breakdown (realistic scenario):")
    print(f"  {'City':<14} {'W':>3} {'L':>3} {'WR':>6} {'Wagered':>8} {'Payout':>8} {'P&L':>8}")
    print(f"  {'─' * 55}")
    for city in sorted(city_stats, key=lambda c: city_stats[c]["pay"] - city_stats[c]["cost"], reverse=True):
        cs = city_stats[city]
        t = cs["w"] + cs["l"]
        wr = cs["w"] / t * 100 if t else 0
        pnl = cs["pay"] - cs["cost"]
        print(f"  {city:<14} {cs['w']:>3} {cs['l']:>3} {wr:>5.0f}% ${cs['cost']:>7.2f} ${cs['pay']:>7.2f} ${pnl:>+7.2f}")

    # Model calibration by probability bucket
    print(f"\n  Model calibration by confidence level:")
    print(f"  {'Model Prob':<14} {'Trades':>7} {'Wins':>5} {'Actual WR':>10} {'Expected':>10} {'Calibration':>12}")
    print(f"  {'─' * 60}")

    prob_bins = [(0, 0.15), (0.15, 0.25), (0.25, 0.40), (0.40, 0.60), (0.60, 1.0)]
    for lo, hi in prob_bins:
        bin_trades = 0
        bin_wins = 0
        bin_prob_sum = 0
        for city, date, buckets in event_data:
            members = ensemble.get(city, {}).get(date)
            if not members:
                continue
            bias, extra, unit = CORR[city]
            for bkt in buckets:
                mp = compute_prob(members, bkt["lo"], bkt["hi"], bias, extra)
                if lo <= mp < hi:
                    bin_trades += 1
                    bin_prob_sum += mp
                    if bkt["won"]:
                        bin_wins += 1
        if bin_trades > 0:
            actual_wr = bin_wins / bin_trades
            expected = bin_prob_sum / bin_trades
            cal = "GOOD" if abs(actual_wr - expected) < 0.10 else ("OVER" if expected > actual_wr else "UNDER")
            print(f"  {lo:.0%}-{hi:.0%}        {bin_trades:>7} {bin_wins:>5} {actual_wr:>9.1%} {expected:>9.1%}  {cal:>12}")


if __name__ == "__main__":
    run_simulation()
