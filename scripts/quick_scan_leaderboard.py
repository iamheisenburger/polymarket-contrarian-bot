"""
Quick scan of crypto leaderboard wallets for BTC 5-min activity.
Only checks 2 pages (200 trades) per wallet. Resolves max 15 markets per wallet.
Fast: should finish in ~5 minutes total.
"""

import json
import re
import time
import sys
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BTC_5M_RE = re.compile(r"^btc-updown-5m-(\d+)$")

WALLETS = [
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a",
    "0x1979ae6b7e6534de9c4539d0c205e582ca637c9d",
    "0xd0d6053c3c37e727402d84c14069780d360993aa",
    "0xe594336603f4fb5d3ba4125a67021ab3b4347052",
    "0x1461cc6e1a05e20710c416307db62c28f1d122d8",
    "0xf705fa045201391d9632b7f3cde06a5e24453ca7",
    "0x04a39d068f4301195c25dcb4c1fe5a4f08a65213",
    "0x1d0034134e339a309700ff2d34e99fa2d48b0313",
    "0x0ea574f3204c5c9c0cdead90392ea0990f4d17e4",
]


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch_json(url, params=None):
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 429:
                time.sleep(5)
                continue
            resp.raise_for_status()
            time.sleep(0.25)
            return resp.json()
        except Exception as e:
            if attempt < 2:
                time.sleep(1)
    return None


def resolve_market(slug):
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


def quick_scan(wallet):
    """Fetch 2 pages of activity, extract BTC 5-min trades."""
    trades = []
    seen_tx = set()

    for page in range(5):  # 5 pages = 500 trades max
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
            trades.append({
                "slug": slug,
                "side": t.get("side", ""),
                "outcome": t.get("outcome", ""),
                "price": float(t.get("price", 0)),
                "size": float(t.get("size", 0)),
                "usdc": float(t.get("usdcSize", 0)),
                "timestamp": t.get("timestamp", 0),
            })

        if len(data) < 100:
            break

    return trades


def analyze_quick(wallet, trades, max_resolve=20):
    """Quick analysis with limited market resolution."""
    buys = [t for t in trades if t["side"] == "BUY"]
    sells = [t for t in trades if t["side"] == "SELL"]

    if not buys:
        return None

    # Side selection
    up_buys = sum(1 for t in buys if t["outcome"] == "Up")
    down_buys = sum(1 for t in buys if t["outcome"] == "Down")

    # Price distribution
    prices = [t["price"] for t in buys]
    avg_price = sum(prices) / len(prices)

    # Timing
    timing = []
    for t in buys:
        m = BTC_5M_RE.match(t["slug"])
        if m:
            market_end = int(m.group(1)) + 300
            secs_before = market_end - t["timestamp"]
            timing.append(secs_before)

    avg_timing = sum(timing) / len(timing) if timing else 0

    # Resolve a SAMPLE of markets (most recent, max 15)
    unique_slugs = list(set(t["slug"] for t in buys))
    # Sort by timestamp embedded in slug (most recent first)
    # Sort OLDEST first — recent markets may not be resolved yet
    unique_slugs.sort(key=lambda s: int(BTC_5M_RE.match(s).group(1)))
    sample_slugs = unique_slugs[:max_resolve]

    outcome_cache = {}
    for slug in sample_slugs:
        outcome_cache[slug] = resolve_market(slug)

    wins = losses = 0
    total_pnl = 0.0
    total_wagered = 0.0

    for t in buys:
        winner = outcome_cache.get(t["slug"])
        if not winner:
            continue
        won = (t["outcome"] == winner)
        usdc = t["usdc"]
        total_wagered += usdc

        if won:
            pnl = t["size"] - usdc
            wins += 1
        else:
            pnl = -usdc
            losses += 1
        total_pnl += pnl

    decided = wins + losses

    return {
        "wallet": wallet,
        "btc5m_trades": len(trades),
        "buys": len(buys),
        "sells": len(sells),
        "up_buys": up_buys,
        "down_buys": down_buys,
        "avg_entry_price": round(avg_price, 3),
        "avg_secs_before_end": round(avg_timing, 0),
        "markets_sampled": len(sample_slugs),
        "markets_resolved": sum(1 for v in outcome_cache.values() if v),
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided > 0 else 0,
        "total_wagered_sample": round(total_wagered, 2),
        "pnl_sample": round(total_pnl, 2),
        "roi_sample": round(total_pnl / total_wagered * 100, 1) if total_wagered > 0 else 0,
    }


def main():
    log("=== QUICK LEADERBOARD SCAN (5 pages per wallet, 20 oldest markets resolved) ===\n")

    results = []

    for i, wallet in enumerate(WALLETS):
        log(f"[{i+1}/{len(WALLETS)}] {wallet[:14]}...")

        trades = quick_scan(wallet)
        log(f"  Found {len(trades)} BTC 5m trades in recent 500 activities")

        if not trades:
            log(f"  No BTC 5m activity. Skipping.\n")
            results.append({"wallet": wallet, "btc5m_trades": 0})
            continue

        buys = [t for t in trades if t["side"] == "BUY"]
        log(f"  {len(buys)} BUYs — resolving up to 20 oldest markets...")

        analysis = analyze_quick(wallet, trades)
        if analysis and analysis["decided"] > 0:
            log(
                f"  >> {analysis['wins']}W/{analysis['losses']}L "
                f"({analysis['win_rate']}% WR) | "
                f"PnL: ${analysis['pnl_sample']:+.2f} | "
                f"Avg price: ${analysis['avg_entry_price']} | "
                f"Timing: {analysis['avg_secs_before_end']}s before end"
            )
        elif analysis:
            log(f"  >> {analysis['buys']} buys but 0 resolved in sample")
        else:
            log(f"  >> No buys found")

        if analysis:
            results.append(analysis)
        log("")

    # Summary
    log("=" * 70)
    log("=== SUMMARY ===")
    log("=" * 70)

    active = [r for r in results if r.get("btc5m_trades", 0) > 0]
    inactive = [r for r in results if r.get("btc5m_trades", 0) == 0]

    log(f"\nBTC 5m active: {len(active)}/{len(WALLETS)}")
    log(f"Not active: {len(inactive)}/{len(WALLETS)}")

    if active:
        log("\n--- ACTIVE BTC 5M TRADERS (sorted by win rate) ---")
        ranked = sorted(active, key=lambda x: x.get("win_rate", 0), reverse=True)
        for r in ranked:
            d = r.get("decided", 0)
            if d > 0:
                log(
                    f"  {r['wallet'][:14]}... | "
                    f"{r['btc5m_trades']:3d} trades | "
                    f"{r['wins']}W/{r['losses']}L ({r['win_rate']}% WR) | "
                    f"PnL: ${r.get('pnl_sample', 0):+.2f} | "
                    f"Avg entry: ${r['avg_entry_price']} | "
                    f"Up/Down: {r['up_buys']}/{r['down_buys']} | "
                    f"Timing: {r['avg_secs_before_end']}s"
                )
            else:
                log(
                    f"  {r['wallet'][:14]}... | "
                    f"{r['btc5m_trades']:3d} trades | "
                    f"0 resolved in sample"
                )

    if inactive:
        log("\n--- NOT ACTIVE IN BTC 5M ---")
        for r in inactive:
            log(f"  {r['wallet']}")

    # Save
    Path("data").mkdir(exist_ok=True)
    with open("data/leaderboard_quick_scan.json", "w") as f:
        json.dump(results, f, indent=2)
    log(f"\nSaved to data/leaderboard_quick_scan.json")


if __name__ == "__main__":
    main()
