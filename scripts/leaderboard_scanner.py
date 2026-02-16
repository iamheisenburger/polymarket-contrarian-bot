"""
Leaderboard-First BTC 5-Min Trader Scanner

Instead of scanning random wallets, starts from the Polymarket leaderboard
(top profitable traders) and checks which ones trade BTC 5-min markets.

Much more efficient: only ~50 API calls to find profitable BTC 5m traders
vs thousands of calls scanning random wallets.
"""

import json
import re
import time
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Config ---
DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BTC_5M_RE = re.compile(r"^btc-updown-5m-(\d+)$")
OUTPUT_FILE = "data/leaderboard_btc5m.json"

# Top leaderboard wallets (scraped from polymarket.com/leaderboard)
LEADERBOARD_WALLETS = [
    "0x492442eab586f242b53bda933fd5de859c8a3782",
    "0xc2e7800b5af46e6093872b177b7a5e7f0563be51",
    "0x6a72f61820b26b1fe4d956e17b6dc2a1ea3033ee",
    "0xd25c72ac0928385610611c8148803dc717334d20",
    "0x876426b52898c295848f56760dd24b55eda2604a",
    "0xdb27bf2ac5d428a9c63dbc914611036855a6c56e",
    "0x03e8a544e97eeff5753bc1e90d46e5ef22af1697",
    "0x204f72f35326db932158cba6adff0b9a1da95e14",
    "0x14964aefa2cd7caff7878b3820a690a03c5aa429",
    "0x96489abcb9f583d6835c8ef95ffc923d05a86825",
    "0x1d8a377c5020f612ce63a0a151970df64baae842",
    "0xd0b4c4c020abdc88ad9a884f999f3d8cff8ffed6",
    "0x9c16127eccf031df45461ef1e04b52ea286a09cb",
    "0x13414a77a4be48988851c73dfd824d0168e70853",
    "0x4bd74aef0ee5f1ec0718890f55c15f047e28373e",
    "0x9976874011b081e1e408444c579f48aa5b5967da",
    "0x2c335066fe58fe9237c3d3dc7b275c2a034a0563",
    "0x23796015f5159c76921dc869b7f95a7c57e2bf16",
    "0xccb290b1c145d1c95695d3756346bba9f1398586",
    "0x003932bc605249fbfeb9ea6c3e15ec6e868a6beb",
    "0x02227b8f5a9636e895607edd3185ed6ee5598ff7",
    "0xa5ea13a81d2b7e8e424b182bdc1db08e756bd96a",
    "0x0b9cae2b0dfe7a71c413e0604eaac1c352f87e44",
    "0xb90494d9a5d8f71f1930b2aa4b599f95c344c255",
    "0xe90bec87d9ef430f27f9dcfe72c34b76967d5da2",
    "0xe594336603f4fb5d3ba4125a67021ab3b4347052",
    "0x1979ae6b7e6534de9c4539d0c205e582ca637c9d",
    "0xee613b3fc183ee44f9da9c05f53e2da107e3debf",
    "0xd0d6053c3c37e727402d84c14069780d360993aa",
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a",
    "0xfedc381bf3fb5d20433bb4a0216b15dbbc5c6398",
    "0x2005d16a84ceefa912d4e380cd32e7ff827875ea",
    "0x5d189e816b4149be00977c1a3c8840374aec4972",
    "0xc257ea7e3a81ca8e16df8935d44d513959fa358e",
    "0x507e52ef684ca2dd91f90a9d26d149dd3288beae",
    "0x6031b6eed1c97e853c6e0f03ad3ce3529351f96d",
    "0x6d3c5bd13984b2de47c3a88ddc455309aab3d294",
    "0x6480542954b70a674a74bd1a6015dec362dc8dc5",
    "0x4133bcbad1d9c41de776646696f41c34d0a65e70",
    "0xe20a1538293903b746ffe6c4ce2d5c3c0300e469",
    "0x0ea574f3204c5c9c0cdead90392ea0990f4d17e4",
    "0x2663daca3cecf3767ca1c3b126002a8578a8ed1f",
]


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch_json(url, params=None, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                time.sleep(10 * (attempt + 1))
                continue
            resp.raise_for_status()
            time.sleep(0.3)
            return resp.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                return None
    return None


def scan_wallet_for_btc5m(wallet, max_pages=20):
    """
    Scan a wallet's activity for BTC 5-min trades.
    Returns list of BTC 5m trades found.
    """
    trades = []
    seen_tx = set()

    for page in range(max_pages):
        offset = page * 100
        data = fetch_json(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": 100, "offset": offset},
        )
        if not data:
            break

        btc_5m_count = 0
        for trade in data:
            if trade.get("type") != "TRADE":
                continue
            slug = trade.get("slug", "")
            if not BTC_5M_RE.match(slug):
                continue

            tx = trade.get("transactionHash", "")
            if tx in seen_tx:
                continue
            seen_tx.add(tx)

            btc_5m_count += 1
            trades.append({
                "slug": slug,
                "side": trade.get("side", ""),
                "outcome": trade.get("outcome", ""),
                "price": float(trade.get("price", 0)),
                "size": float(trade.get("size", 0)),
                "usdc": float(trade.get("usdcSize", 0)),
                "timestamp": trade.get("timestamp", 0),
            })

        if len(data) < 100:
            break

        # If no BTC 5m trades in first 2 pages, skip this wallet
        if page == 1 and len(trades) == 0:
            break

    return trades


def resolve_market(slug):
    """Check market outcome. Returns 'Up', 'Down', or None."""
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


def analyze_trades(wallet, trades):
    """Analyze BTC 5-min trades for a wallet."""
    by_slug = defaultdict(list)
    for t in trades:
        by_slug[t["slug"]].append(t)

    # Resolve markets (batch-friendly with caching)
    outcome_cache = {}
    buys = []
    total_wagered = 0.0
    total_pnl = 0.0
    wins = 0
    losses = 0

    for slug, slug_trades in by_slug.items():
        if slug not in outcome_cache:
            outcome_cache[slug] = resolve_market(slug)
        winner = outcome_cache[slug]
        if not winner:
            continue

        for t in slug_trades:
            if t["side"] != "BUY":
                continue

            bet_on = t["outcome"]
            won = (bet_on == winner)
            usdc = t["usdc"]
            size = t["size"]
            total_wagered += usdc

            if won:
                pnl = size - usdc
                wins += 1
            else:
                pnl = -usdc
                losses += 1

            total_pnl += pnl
            buys.append({
                "slug": slug,
                "bet_on": bet_on,
                "winner": winner,
                "price": t["price"],
                "usdc": usdc,
                "pnl": round(pnl, 4),
                "won": won,
            })

    decided = wins + losses
    return {
        "wallet": wallet,
        "total_btc5m_trades": len(trades),
        "unique_markets": len(by_slug),
        "resolved_buys": decided,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided > 0 else 0,
        "total_wagered": round(total_wagered, 2),
        "total_pnl": round(total_pnl, 2),
        "roi": round(total_pnl / total_wagered * 100, 1) if total_wagered > 0 else 0,
        "avg_entry_price": round(
            sum(b["price"] for b in buys) / len(buys), 4
        ) if buys else 0,
        "sample_trades": buys[:10],
    }


def main():
    log("=== Leaderboard BTC 5-Min Trader Scanner ===")
    log(f"Checking {len(LEADERBOARD_WALLETS)} leaderboard wallets...")
    log("")

    btc_5m_traders = []
    non_traders = []

    for i, wallet in enumerate(LEADERBOARD_WALLETS):
        log(f"[{i + 1}/{len(LEADERBOARD_WALLETS)}] {wallet[:14]}...")

        # Quick scan — first 2 pages of activity
        trades = scan_wallet_for_btc5m(wallet, max_pages=2)

        if not trades:
            log(f"  No BTC 5m trades found. Skipping.")
            non_traders.append(wallet)
            continue

        log(f"  Found {len(trades)} BTC 5m trades! Fetching full history...")

        # Get deeper history for this trader
        full_trades = scan_wallet_for_btc5m(wallet, max_pages=20)
        log(f"  Full history: {len(full_trades)} BTC 5m trades")

        # Analyze
        result = analyze_trades(wallet, full_trades)
        btc_5m_traders.append(result)

        decided = result["resolved_buys"]
        if decided > 0:
            log(
                f"  W/L: {result['wins']}/{result['losses']} | "
                f"WR: {result['win_rate']}% | "
                f"PnL: ${result['total_pnl']:+.2f} | "
                f"ROI: {result['roi']:+.1f}% | "
                f"Avg entry: ${result['avg_entry_price']:.4f}"
            )
        else:
            log(f"  {len(full_trades)} trades but none resolved yet")

    # Results
    log("")
    log("=" * 70)
    log(f"=== RESULTS: {len(btc_5m_traders)} of {len(LEADERBOARD_WALLETS)} leaderboard wallets trade BTC 5m ===")
    log("=" * 70)

    # Sort by PnL
    profitable = sorted(btc_5m_traders, key=lambda x: x["total_pnl"], reverse=True)

    for r in profitable:
        if r["resolved_buys"] > 0:
            log(
                f"  {r['wallet'][:14]}... | "
                f"{r['total_btc5m_trades']:4d} trades | "
                f"{r['resolved_buys']:3d} resolved | "
                f"WR: {r['win_rate']:5.1f}% | "
                f"PnL: ${r['total_pnl']:+10.2f} | "
                f"ROI: {r['roi']:+6.1f}% | "
                f"Wagered: ${r['total_wagered']:,.0f}"
            )

    # Save
    Path(OUTPUT_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump({
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "leaderboard_wallets_checked": len(LEADERBOARD_WALLETS),
            "btc_5m_traders_found": len(btc_5m_traders),
            "non_btc5m_wallets": non_traders,
            "results": profitable,
        }, f, indent=2)

    log(f"\nSaved to {OUTPUT_FILE}")

    # Highlight the best candidates for copytrading
    log("")
    log("=== COPYTRADE CANDIDATES ===")
    candidates = [
        r for r in profitable
        if r["resolved_buys"] >= 10 and r["win_rate"] > 52
    ]
    if candidates:
        for r in candidates:
            log(
                f"  ** {r['wallet']} **\n"
                f"     {r['wins']}W/{r['losses']}L ({r['win_rate']}% WR) | "
                f"PnL: ${r['total_pnl']:+.2f} | "
                f"Avg entry: ${r['avg_entry_price']:.4f} | "
                f"{r['total_btc5m_trades']} total trades"
            )
    else:
        log("  No candidates with 10+ resolved BUYs and >52% win rate.")
        log("  This tells us: even top Polymarket traders may not beat BTC 5m markets.")


if __name__ == "__main__":
    main()
