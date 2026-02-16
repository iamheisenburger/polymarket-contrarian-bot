"""
Deep-dive analysis of a specific wallet's BTC 5-min trading patterns.

Analyzes: entry timing, price patterns, side selection, win rates by
time-of-day, entry price buckets, and market timing.
"""

import json
import re
import time
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BTC_5M_RE = re.compile(r"^btc-updown-5m-(\d+)$")

WALLET = sys.argv[1] if len(sys.argv) > 1 else "0x856c79f3aa293c0935f82b09aee17598b8127f1c"


def log(msg):
    print(f"{msg}", flush=True)


def fetch_json(url, params=None):
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                time.sleep(10)
                continue
            resp.raise_for_status()
            time.sleep(0.3)
            return resp.json()
        except Exception as e:
            if attempt < 2:
                time.sleep(2)
    return None


def fetch_all_btc5m_trades(wallet):
    """Fetch all BTC 5-min trades for wallet."""
    trades = []
    seen_tx = set()
    page = 0

    while True:
        data = fetch_json(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": 100, "offset": page * 100},
        )
        if not data:
            break

        for t in data:
            if t.get("type") != "TRADE":
                continue
            slug = t.get("slug", "")
            if not BTC_5M_RE.match(slug):
                continue
            tx = t.get("transactionHash", "")
            if tx in seen_tx:
                continue
            seen_tx.add(tx)
            trades.append(t)

        if len(data) < 100:
            break
        page += 1
        log(f"  Page {page}: {len(trades)} BTC 5m trades so far...")

    return trades


def resolve_market(slug):
    """Returns 'Up', 'Down', or None."""
    data = fetch_json(f"{GAMMA_API}/events", params={"slug": slug})
    if not data or not isinstance(data, list) or len(data) == 0:
        return None
    event = data[0]
    markets = event.get("markets", [])
    if not markets:
        return None
    market = markets[0]
    if not market.get("closed"):
        return None
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        if len(prices) >= 2:
            if float(prices[0]) > 0.9:
                return "Up"
            elif float(prices[1]) > 0.9:
                return "Down"
    except Exception:
        pass
    return None


def main():
    log(f"=== DEEP DIVE: {WALLET[:14]}... ===\n")

    # Fetch all trades
    log("Fetching full BTC 5-min trade history...")
    raw_trades = fetch_all_btc5m_trades(WALLET)
    log(f"Found {len(raw_trades)} total BTC 5m trades\n")

    if not raw_trades:
        log("No trades found!")
        return

    # Parse trades
    trades = []
    for t in raw_trades:
        slug = t["slug"]
        market_ts = int(BTC_5M_RE.match(slug).group(1))
        trade_ts = t.get("timestamp", 0)

        trades.append({
            "slug": slug,
            "market_ts": market_ts,
            "trade_ts": trade_ts,
            "side": t.get("side", ""),
            "outcome": t.get("outcome", ""),
            "price": float(t.get("price", 0)),
            "size": float(t.get("size", 0)),
            "usdc": float(t.get("usdcSize", 0)),
        })

    # Sort by timestamp
    trades.sort(key=lambda x: x["trade_ts"])

    # === Basic Stats ===
    buys = [t for t in trades if t["side"] == "BUY"]
    sells = [t for t in trades if t["side"] == "SELL"]

    log(f"Total trades: {len(trades)}")
    log(f"  BUYs: {len(buys)}")
    log(f"  SELLs: {len(sells)}")
    log(f"  Total USDC wagered (buys): ${sum(t['usdc'] for t in buys):,.2f}")
    log(f"  Total USDC from sells: ${sum(t['usdc'] for t in sells):,.2f}")

    # === Side Selection ===
    log(f"\n--- SIDE SELECTION ---")
    up_buys = [t for t in buys if t["outcome"] == "Up"]
    down_buys = [t for t in buys if t["outcome"] == "Down"]
    log(f"Buys Up: {len(up_buys)} ({len(up_buys)/len(buys)*100:.1f}%)")
    log(f"Buys Down: {len(down_buys)} ({len(down_buys)/len(buys)*100:.1f}%)")

    # === Entry Price Distribution ===
    log(f"\n--- ENTRY PRICE DISTRIBUTION (BUYs) ---")
    price_buckets = Counter()
    for t in buys:
        bucket = round(t["price"] * 20) / 20  # 5-cent buckets
        price_buckets[bucket] += 1

    for price in sorted(price_buckets.keys()):
        count = price_buckets[price]
        bar = "#" * min(count, 60)
        log(f"  ${price:.2f}: {count:4d} {bar}")

    # === Timing Analysis ===
    log(f"\n--- TRADE TIMING (seconds before market end) ---")
    timing_buckets = Counter()
    for t in buys:
        market_end = t["market_ts"] + 300  # 5-min market
        secs_before_end = market_end - t["trade_ts"]
        if secs_before_end < 0:
            bucket = "after_close"
        elif secs_before_end < 30:
            bucket = "0-30s"
        elif secs_before_end < 60:
            bucket = "30-60s"
        elif secs_before_end < 120:
            bucket = "60-120s"
        elif secs_before_end < 180:
            bucket = "120-180s"
        elif secs_before_end < 240:
            bucket = "180-240s"
        else:
            bucket = "240-300s"
        timing_buckets[bucket] += 1

    for bucket in ["0-30s", "30-60s", "60-120s", "120-180s", "180-240s", "240-300s", "after_close"]:
        count = timing_buckets.get(bucket, 0)
        bar = "#" * min(count, 60)
        log(f"  {bucket:12s}: {count:4d} {bar}")

    # === Resolve Markets & Calculate Win Rates ===
    log(f"\n--- RESOLVING MARKETS (this takes a minute) ---")
    unique_slugs = list(set(t["slug"] for t in buys))
    log(f"Resolving {len(unique_slugs)} unique markets...")

    outcome_cache = {}
    for i, slug in enumerate(unique_slugs):
        outcome_cache[slug] = resolve_market(slug)
        if (i + 1) % 20 == 0:
            resolved = sum(1 for v in outcome_cache.values() if v)
            log(f"  {i+1}/{len(unique_slugs)} checked, {resolved} resolved...")

    resolved_count = sum(1 for v in outcome_cache.values() if v)
    log(f"Resolved: {resolved_count}/{len(unique_slugs)} markets")

    # === Win/Loss Analysis ===
    log(f"\n--- WIN/LOSS ANALYSIS ---")
    wins = losses = 0
    total_pnl = 0.0
    pnl_by_price = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0, "count": 0})
    pnl_by_timing = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    pnl_by_side = defaultdict(lambda: {"wins": 0, "losses": 0, "pnl": 0.0})
    recent_trades = []

    for t in buys:
        winner = outcome_cache.get(t["slug"])
        if not winner:
            continue

        won = (t["outcome"] == winner)
        pnl = (t["size"] - t["usdc"]) if won else -t["usdc"]
        total_pnl += pnl

        if won:
            wins += 1
        else:
            losses += 1

        # By price bucket
        price_bucket = round(t["price"] * 20) / 20
        pnl_by_price[price_bucket]["count"] += 1
        pnl_by_price[price_bucket]["pnl"] += pnl
        if won:
            pnl_by_price[price_bucket]["wins"] += 1
        else:
            pnl_by_price[price_bucket]["losses"] += 1

        # By timing
        market_end = t["market_ts"] + 300
        secs_before = market_end - t["trade_ts"]
        if secs_before < 60:
            tb = "last_60s"
        elif secs_before < 180:
            tb = "60-180s"
        else:
            tb = "180-300s"
        if won:
            pnl_by_timing[tb]["wins"] += 1
        else:
            pnl_by_timing[tb]["losses"] += 1
        pnl_by_timing[tb]["pnl"] += pnl

        # By side
        if won:
            pnl_by_side[t["outcome"]]["wins"] += 1
        else:
            pnl_by_side[t["outcome"]]["losses"] += 1
        pnl_by_side[t["outcome"]]["pnl"] += pnl

        recent_trades.append({
            "slug": t["slug"],
            "outcome": t["outcome"],
            "price": t["price"],
            "usdc": t["usdc"],
            "won": won,
            "pnl": round(pnl, 2),
            "secs_before_end": secs_before,
        })

    decided = wins + losses
    log(f"Resolved BUYs: {decided}")
    log(f"Wins: {wins} | Losses: {losses}")
    log(f"Win Rate: {wins/decided*100:.1f}%" if decided > 0 else "Win Rate: N/A")
    log(f"Total PnL: ${total_pnl:+,.2f}")

    # By price bucket
    log(f"\n--- WIN RATE BY ENTRY PRICE ---")
    for price in sorted(pnl_by_price.keys()):
        d = pnl_by_price[price]
        total = d["wins"] + d["losses"]
        wr = d["wins"] / total * 100 if total > 0 else 0
        log(f"  ${price:.2f}: {total:3d} trades | WR: {wr:5.1f}% | PnL: ${d['pnl']:+8.2f}")

    # By timing
    log(f"\n--- WIN RATE BY ENTRY TIMING ---")
    for tb in ["last_60s", "60-180s", "180-300s"]:
        d = pnl_by_timing.get(tb, {"wins": 0, "losses": 0, "pnl": 0.0})
        total = d["wins"] + d["losses"]
        wr = d["wins"] / total * 100 if total > 0 else 0
        log(f"  {tb:12s}: {total:3d} trades | WR: {wr:5.1f}% | PnL: ${d['pnl']:+8.2f}")

    # By side
    log(f"\n--- WIN RATE BY SIDE ---")
    for side in ["Up", "Down"]:
        d = pnl_by_side.get(side, {"wins": 0, "losses": 0, "pnl": 0.0})
        total = d["wins"] + d["losses"]
        wr = d["wins"] / total * 100 if total > 0 else 0
        log(f"  {side:6s}: {total:3d} trades | WR: {wr:5.1f}% | PnL: ${d['pnl']:+8.2f}")

    # Recent trades
    log(f"\n--- LAST 20 RESOLVED TRADES ---")
    for t in recent_trades[-20:]:
        emoji = "W" if t["won"] else "L"
        log(
            f"  [{emoji}] {t['outcome']:4s} @ ${t['price']:.2f} | "
            f"${t['usdc']:.2f} wagered | PnL: ${t['pnl']:+.2f} | "
            f"{t['secs_before_end']}s before end"
        )

    # === KEY INSIGHT: Trade frequency ===
    log(f"\n--- TRADE FREQUENCY ---")
    if trades:
        first_ts = trades[0]["trade_ts"]
        last_ts = trades[-1]["trade_ts"]
        span_hours = (last_ts - first_ts) / 3600
        log(f"Active period: {span_hours:.1f} hours")
        log(f"Trades per hour: {len(trades) / max(span_hours, 1):.1f}")

        # Trades per market (do they trade multiple times per 5-min window?)
        trades_per_market = Counter(t["slug"] for t in trades)
        avg_per_market = sum(trades_per_market.values()) / len(trades_per_market)
        max_per_market = max(trades_per_market.values())
        log(f"Avg trades per market: {avg_per_market:.1f}")
        log(f"Max trades in single market: {max_per_market}")

    # Save full data
    output = {
        "wallet": WALLET,
        "total_trades": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "resolved_buys": decided,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided > 0 else 0,
        "total_pnl": round(total_pnl, 2),
        "by_price": {str(k): v for k, v in pnl_by_price.items()},
        "by_timing": dict(pnl_by_timing),
        "by_side": dict(pnl_by_side),
        "recent_trades": recent_trades[-50:],
    }
    Path("data").mkdir(exist_ok=True)
    with open("data/dense_probability_analysis.json", "w") as f:
        json.dump(output, f, indent=2)
    log(f"\nFull data saved to data/dense_probability_analysis.json")


if __name__ == "__main__":
    main()
