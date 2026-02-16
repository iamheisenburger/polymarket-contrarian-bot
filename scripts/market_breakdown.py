"""
Analyze WHERE top crypto leaderboard traders make their money.
Not just BTC 5-min — ALL market types they trade.
"""

import json
import re
import time
from collections import defaultdict, Counter
from datetime import datetime, timezone
from pathlib import Path

import requests

DATA_API = "https://data-api.polymarket.com"

# Top crypto leaderboard wallets
WALLETS = {
    "0xe594336603f4fb5d3ba4125a67021ab3b4347052": "Rank#5 +$298K",
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a": "Rank#?",
    "0x0ea574f3204c5c9c0cdead90392ea0990f4d17e4": "Rank#10 +$209K",
    "0x1979ae6b7e6534de9c4539d0c205e582ca637c9d": "Rank#2 +$416K",
    "0xd0d6053c3c37e727402d84c14069780d360993aa": "Uncommon-Oat",
    "0x1d0034134e339a309700ff2d34e99fa2d48b0313": "Rank#9 +$221K",
}


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
        except Exception:
            if attempt < 2:
                time.sleep(1)
    return None


def categorize_slug(slug):
    """Categorize a market slug into a market type."""
    if re.match(r"^btc-updown-5m-\d+$", slug):
        return "BTC 5-min"
    elif re.match(r"^bitcoin-up-or-down.*hourly", slug) or re.match(r"^btc-updown-1h-\d+$", slug):
        return "BTC Hourly"
    elif re.match(r"^bitcoin-up-or-down", slug):
        return "BTC Daily/Other"
    elif re.match(r"^eth-updown", slug) or "ethereum" in slug.lower():
        return "ETH"
    elif re.match(r"^sol-updown", slug) or "solana" in slug.lower():
        return "SOL"
    elif "crypto" in slug.lower() or "bitcoin" in slug.lower() or "btc" in slug.lower():
        return "Other Crypto"
    elif any(x in slug.lower() for x in ["trump", "election", "president", "congress", "senate", "democrat", "republican"]):
        return "Politics"
    elif any(x in slug.lower() for x in ["nba", "nfl", "nhl", "mlb", "soccer", "football", "game", "match"]):
        return "Sports"
    else:
        return "Other"


def scan_wallet_all_markets(wallet, pages=5):
    """Fetch recent activity and categorize ALL trades."""
    trades = []
    seen_tx = set()

    for page in range(pages):
        data = fetch_json(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": 100, "offset": page * 100},
        )
        if not data:
            break

        for t in data:
            if t.get("type") != "TRADE":
                continue
            tx = t.get("transactionHash", "")
            if tx in seen_tx:
                continue
            seen_tx.add(tx)

            slug = t.get("slug", "")
            trades.append({
                "slug": slug,
                "category": categorize_slug(slug),
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


def main():
    log("=== WHERE DO TOP TRADERS MAKE MONEY? ===\n")

    for wallet, label in WALLETS.items():
        log(f"--- {wallet[:14]}... ({label}) ---")

        trades = scan_wallet_all_markets(wallet, pages=5)
        if not trades:
            log("  No trades found\n")
            continue

        buys = [t for t in trades if t["side"] == "BUY"]

        # Category breakdown
        by_cat = defaultdict(list)
        for t in trades:
            by_cat[t["category"]].append(t)

        log(f"  Total trades: {len(trades)} ({len(buys)} buys)")
        log(f"  Market breakdown:")

        # Sort categories by trade count
        sorted_cats = sorted(by_cat.items(), key=lambda x: len(x[1]), reverse=True)
        for cat, cat_trades in sorted_cats:
            cat_buys = [t for t in cat_trades if t["side"] == "BUY"]
            cat_usdc = sum(t["usdc"] for t in cat_buys)
            avg_price = sum(t["price"] for t in cat_buys) / len(cat_buys) if cat_buys else 0

            log(
                f"    {cat:20s}: {len(cat_trades):4d} trades "
                f"({len(cat_buys):3d} buys) | "
                f"${cat_usdc:8,.2f} wagered | "
                f"avg entry: ${avg_price:.3f}"
            )

        # Show unique slugs for non-BTC-5min categories
        non_5m = [t for t in trades if t["category"] != "BTC 5-min"]
        if non_5m:
            slug_counts = Counter(t["slug"] for t in non_5m)
            top_slugs = slug_counts.most_common(5)
            log(f"  Top non-BTC-5m markets:")
            for slug, count in top_slugs:
                cat = categorize_slug(slug)
                usdc = sum(t["usdc"] for t in non_5m if t["slug"] == slug and t["side"] == "BUY")
                log(f"    {slug[:60]:60s} ({cat}) {count:3d} trades ${usdc:,.2f}")

        log("")

    log("=== DONE ===")


if __name__ == "__main__":
    main()
