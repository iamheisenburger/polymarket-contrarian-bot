"""
BTC 5-Min Wallet Scanner

Scans Polymarket trade data to find wallets that actively trade BTC 5-minute
binary markets, then analyzes their profitability.

Steps:
1. Fetch recent trades from data-api, filter for btc-updown-5m-* slugs
2. Collect unique wallet addresses
3. For each wallet, fetch their BTC 5-min trade history
4. Cross-reference with market outcomes (resolved via Gamma API)
5. Rank wallets by win rate and profit

Output: data/wallet_scan.json
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
OUTPUT_FILE = "data/wallet_scan.json"
RATE_LIMIT_DELAY = 0.5  # seconds between API calls


def log(msg):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def fetch_json(url, params=None, retries=3):
    """Fetch JSON with retries and rate limiting."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, params=params, timeout=15)
            if resp.status_code == 429:
                wait = 10 * (attempt + 1)
                log(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            time.sleep(RATE_LIMIT_DELAY)
            return resp.json()
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(2)
            else:
                log(f"  Failed after {retries} attempts: {e}")
                return None
    return None


def collect_btc_5m_wallets(pages=20, per_page=100):
    """
    Scan recent trades to find wallets active in BTC 5-min markets.
    Returns dict of wallet -> list of trade records.
    """
    wallets = defaultdict(list)
    seen_tx = set()

    log(f"Scanning {pages} pages of recent trades...")

    for page in range(pages):
        offset = page * per_page
        data = fetch_json(
            f"{DATA_API}/trades",
            params={"limit": per_page, "offset": offset},
        )
        if not data:
            break

        btc_count = 0
        for trade in data:
            slug = trade.get("slug", "")
            if not BTC_5M_RE.match(slug):
                continue

            tx = trade.get("transactionHash", "")
            if tx in seen_tx:
                continue
            seen_tx.add(tx)

            wallet = trade.get("proxyWallet", "")
            if not wallet:
                continue

            btc_count += 1
            wallets[wallet].append({
                "slug": slug,
                "side": trade.get("side", ""),
                "outcome": trade.get("outcome", ""),
                "price": float(trade.get("price", 0)),
                "size": float(trade.get("size", 0)),
                "usdc": float(trade.get("usdcSize", 0)),
                "timestamp": trade.get("timestamp", 0),
                "pseudonym": trade.get("pseudonym", ""),
            })

        log(f"  Page {page + 1}/{pages}: {len(data)} trades, {btc_count} BTC 5m")

        if len(data) < per_page:
            break

    log(f"Found {len(wallets)} unique wallets with {sum(len(v) for v in wallets.values())} BTC 5m trades")
    return wallets


def fetch_wallet_btc_history(wallet, max_pages=10, per_page=50):
    """Fetch full BTC 5-min trade history for a wallet."""
    trades = []
    seen_tx = set()

    for page in range(max_pages):
        offset = page * per_page
        data = fetch_json(
            f"{DATA_API}/activity",
            params={"user": wallet, "limit": per_page, "offset": offset},
        )
        if not data:
            break

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

            trades.append({
                "slug": slug,
                "side": trade.get("side", ""),
                "outcome": trade.get("outcome", ""),
                "price": float(trade.get("price", 0)),
                "size": float(trade.get("size", 0)),
                "usdc": float(trade.get("usdcSize", 0)),
                "timestamp": trade.get("timestamp", 0),
            })

        if len(data) < per_page:
            break

    return trades


def resolve_market(slug):
    """Check if a market is resolved and what the winning outcome is."""
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

    # Get outcome prices to determine winner
    try:
        prices = json.loads(market.get("outcomePrices", "[]"))
        if len(prices) >= 2:
            up_price = float(prices[0])
            down_price = float(prices[1])
            if up_price > 0.9:
                return "Up"
            elif down_price > 0.9:
                return "Down"
    except Exception:
        pass

    return None


def analyze_wallet(wallet, trades):
    """
    Analyze a wallet's BTC 5-min trading performance.
    Groups trades by market slug, resolves outcomes, calculates P&L.
    """
    # Group trades by slug
    by_slug = defaultdict(list)
    for t in trades:
        by_slug[t["slug"]].append(t)

    results = {
        "wallet": wallet,
        "total_markets": len(by_slug),
        "resolved": 0,
        "wins": 0,
        "losses": 0,
        "total_usdc_in": 0.0,
        "total_pnl": 0.0,
        "trades_detail": [],
    }

    # Cache resolved outcomes
    outcome_cache = {}

    for slug, slug_trades in by_slug.items():
        # Resolve market outcome
        if slug not in outcome_cache:
            outcome_cache[slug] = resolve_market(slug)

        winner = outcome_cache[slug]
        if not winner:
            continue

        results["resolved"] += 1

        # Calculate net position for this market
        # BUY = buying tokens, SELL = selling tokens
        for t in slug_trades:
            bet_on = t["outcome"]  # "Up" or "Down"
            side = t["side"]  # "BUY" or "SELL"
            price = t["price"]
            usdc = t["usdc"]
            size = t["size"]

            if side == "BUY":
                # Bought tokens at `price`, costs `usdc`
                results["total_usdc_in"] += usdc
                won = (bet_on == winner)

                if won:
                    payout = size * 1.0  # tokens pay $1 each
                    pnl = payout - usdc
                    results["wins"] += 1
                else:
                    pnl = -usdc
                    results["losses"] += 1

                results["total_pnl"] += pnl
                results["trades_detail"].append({
                    "slug": slug,
                    "bet_on": bet_on,
                    "winner": winner,
                    "side": side,
                    "price": price,
                    "usdc": usdc,
                    "pnl": round(pnl, 4),
                    "won": won,
                })

            elif side == "SELL":
                # Sold tokens at `price`, received `usdc`
                # This is closing a position or shorting
                # If they sold tokens and the outcome matches, they lost the token value
                # If they sold tokens and the outcome doesn't match, tokens are worthless anyway
                results["total_usdc_in"] += 0  # no cost to sell
                results["total_pnl"] += usdc  # they got paid

                results["trades_detail"].append({
                    "slug": slug,
                    "bet_on": bet_on,
                    "winner": winner,
                    "side": side,
                    "price": price,
                    "usdc": usdc,
                    "pnl": round(usdc, 4),
                    "won": True,  # sells always lock in profit
                })

    # Calculate stats
    decided = results["wins"] + results["losses"]
    results["win_rate"] = (results["wins"] / decided * 100) if decided > 0 else 0
    results["roi"] = (results["total_pnl"] / results["total_usdc_in"] * 100) if results["total_usdc_in"] > 0 else 0
    results["total_pnl"] = round(results["total_pnl"], 2)
    results["total_usdc_in"] = round(results["total_usdc_in"], 2)

    return results


def main():
    log("=== BTC 5-Min Wallet Scanner ===")
    log("")

    # Step 1: Collect wallets from recent trades
    wallet_trades = collect_btc_5m_wallets(pages=50, per_page=100)

    if not wallet_trades:
        log("No BTC 5-min trades found!")
        return

    # Step 2: Sort by trade count (most active first)
    sorted_wallets = sorted(wallet_trades.items(), key=lambda x: len(x[1]), reverse=True)

    # Step 3: Analyze top wallets (most active)
    # Only analyze wallets with 5+ trades in our sample
    candidates = [(w, t) for w, t in sorted_wallets if len(t) >= 3]
    log(f"\nAnalyzing {len(candidates)} wallets with 3+ trades...")

    all_results = []

    for i, (wallet, initial_trades) in enumerate(candidates):
        pseudonym = initial_trades[0].get("pseudonym", "anon") if initial_trades else "anon"
        log(f"\n[{i + 1}/{len(candidates)}] {wallet[:10]}... ({pseudonym}) - {len(initial_trades)} initial trades")

        # Fetch deeper history
        full_trades = fetch_wallet_btc_history(wallet)
        log(f"  Found {len(full_trades)} total BTC 5m trades in history")

        if len(full_trades) < 3:
            log("  Skipping (too few trades)")
            continue

        # Analyze
        result = analyze_wallet(wallet, full_trades)
        result["pseudonym"] = pseudonym
        all_results.append(result)

        decided = result["wins"] + result["losses"]
        log(
            f"  Resolved: {result['resolved']} | "
            f"W/L: {result['wins']}/{result['losses']} | "
            f"WR: {result['win_rate']:.1f}% | "
            f"PnL: ${result['total_pnl']:+.2f} | "
            f"ROI: {result['roi']:+.1f}%"
        )

    # Step 4: Rank by profitability
    log("\n" + "=" * 70)
    log("=== TOP BTC 5-MIN TRADERS (by PnL) ===")
    log("=" * 70)

    # Sort by PnL
    profitable = sorted(all_results, key=lambda x: x["total_pnl"], reverse=True)

    # Remove trades_detail for summary output
    summary = []
    for r in profitable:
        s = {k: v for k, v in r.items() if k != "trades_detail"}
        summary.append(s)
        decided = r["wins"] + r["losses"]
        if decided > 0:
            log(
                f"  {r['pseudonym'][:15]:15s} | {r['wallet'][:10]}... | "
                f"{decided:3d} decided | "
                f"WR: {r['win_rate']:5.1f}% | "
                f"PnL: ${r['total_pnl']:+8.2f} | "
                f"ROI: {r['roi']:+6.1f}% | "
                f"Wagered: ${r['total_usdc_in']:.2f}"
            )

    # Save full results
    Path(OUTPUT_FILE).parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_FILE, "w") as f:
        json.dump({
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "total_wallets_scanned": len(wallet_trades),
            "wallets_analyzed": len(all_results),
            "results": profitable,
        }, f, indent=2)

    log(f"\nFull results saved to {OUTPUT_FILE}")

    # Highlight interesting wallets
    log("\n=== MOST INTERESTING (profitable + enough trades) ===")
    interesting = [
        r for r in profitable
        if r["wins"] + r["losses"] >= 5 and r["total_pnl"] > 0
    ]
    if interesting:
        for r in interesting[:10]:
            log(
                f"  ** {r['wallet']} ({r['pseudonym']}) **\n"
                f"     {r['wins']}W/{r['losses']}L ({r['win_rate']:.1f}% WR) | "
                f"PnL: ${r['total_pnl']:+.2f} | ROI: {r['roi']:+.1f}%"
            )
    else:
        log("  No wallets found with 5+ decided trades AND positive PnL.")
        log("  This itself is useful data — confirms market efficiency.")


if __name__ == "__main__":
    main()
