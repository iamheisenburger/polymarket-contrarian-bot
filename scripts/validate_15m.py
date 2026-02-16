"""
Phase 1a+1c: Validate 15-minute BTC market hypothesis.

1a) Check if top traders are profitable on 15-min specifically
1c) Check if BTC momentum in first 5 min predicts 15-min outcome

Fast: resolves oldest markets only, max ~40 API calls total.
"""

import json
import re
import time
import requests
from collections import defaultdict
from datetime import datetime, timezone

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"
BINANCE_API = "https://api.binance.com"
BTC_15M_RE = re.compile(r"^btc-updown-15m-(\d+)$")
BTC_5M_RE = re.compile(r"^btc-updown-5m-(\d+)$")

WALLETS = {
    "0x1d0034134e339a309700ff2d34e99fa2d48b0313": "Rank#9 +$221K (most 15m trades)",
    "0x1979ae6b7e6534de9c4539d0c205e582ca637c9d": "Rank#2 +$416K (diversified)",
    "0x0ea574f3204c5c9c0cdead90392ea0990f4d17e4": "Rank#10 +$209K",
    "0x63ce342161250d705dc0b16df89036c8e5f9ba9a": "Active 15m trader",
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


def get_btc_kline(start_ts_ms, end_ts_ms):
    """Get BTC price at specific timestamps using Binance klines."""
    data = fetch_json(
        f"{BINANCE_API}/api/v3/klines",
        params={
            "symbol": "BTCUSDT",
            "interval": "1m",
            "startTime": start_ts_ms,
            "endTime": end_ts_ms,
            "limit": 20,
        },
    )
    if not data:
        return []
    # Each kline: [open_time, open, high, low, close, volume, ...]
    return [(int(k[0]), float(k[1]), float(k[4])) for k in data]  # (ts_ms, open, close)


def scan_wallet_15m(wallet, pages=5):
    """Fetch activity, return 15-min BTC trades."""
    trades_15m = []
    trades_5m = []
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
            trade = {
                "slug": slug,
                "side": t.get("side", ""),
                "outcome": t.get("outcome", ""),
                "price": float(t.get("price", 0)),
                "size": float(t.get("size", 0)),
                "usdc": float(t.get("usdcSize", 0)),
                "timestamp": t.get("timestamp", 0),
            }

            if BTC_15M_RE.match(slug):
                trades_15m.append(trade)
            elif BTC_5M_RE.match(slug):
                trades_5m.append(trade)

        if len(data) < 100:
            break

    return trades_15m, trades_5m


def analyze_trades(trades, slug_re, market_duration, max_resolve=20):
    """Analyze trades: resolve oldest markets, compute WR and PnL."""
    buys = [t for t in trades if t["side"] == "BUY"]
    if not buys:
        return None

    # Get unique slugs, sort oldest first
    unique_slugs = list(set(t["slug"] for t in buys))
    unique_slugs.sort(key=lambda s: int(slug_re.match(s).group(1)))
    sample = unique_slugs[:max_resolve]

    # Resolve
    cache = {}
    for slug in sample:
        cache[slug] = resolve_market(slug)

    resolved_count = sum(1 for v in cache.values() if v)

    wins = losses = 0
    total_pnl = 0.0
    total_wagered = 0.0
    up_wins = up_losses = down_wins = down_losses = 0

    for t in buys:
        winner = cache.get(t["slug"])
        if not winner:
            continue
        won = t["outcome"] == winner
        usdc = t["usdc"]
        total_wagered += usdc

        if won:
            pnl = t["size"] - usdc
            wins += 1
            if t["outcome"] == "Up":
                up_wins += 1
            else:
                down_wins += 1
        else:
            pnl = -usdc
            losses += 1
            if t["outcome"] == "Up":
                up_losses += 1
            else:
                down_losses += 1

        total_pnl += pnl

    decided = wins + losses
    return {
        "total_buys": len(buys),
        "markets_sampled": len(sample),
        "markets_resolved": resolved_count,
        "decided": decided,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / decided * 100, 1) if decided > 0 else 0,
        "pnl": round(total_pnl, 2),
        "wagered": round(total_wagered, 2),
        "roi": round(total_pnl / total_wagered * 100, 1) if total_wagered > 0 else 0,
        "up_record": f"{up_wins}W/{up_losses}L",
        "down_record": f"{down_wins}W/{down_losses}L",
        "avg_price": round(sum(t["price"] for t in buys) / len(buys), 3),
    }


def momentum_backtest():
    """
    Step 1c: Check if BTC direction in first 5 min predicts 15-min outcome.
    Fetch resolved 15-min markets and compare BTC price at start vs 5-min vs end.
    """
    log("\n" + "=" * 70)
    log("=== STEP 1c: MOMENTUM PREDICTIVENESS CHECK ===")
    log("=" * 70)

    # Get a bunch of recent 15-min market slugs by scanning several wallets
    all_slugs = set()
    for wallet in WALLETS:
        trades_15m, _ = scan_wallet_15m(wallet, pages=2)
        for t in trades_15m:
            all_slugs.add(t["slug"])
        log(f"  Collected {len(all_slugs)} unique 15m slugs so far...")

    # Sort oldest first, take 50
    sorted_slugs = sorted(all_slugs, key=lambda s: int(BTC_15M_RE.match(s).group(1)))
    test_slugs = sorted_slugs[:50]
    log(f"  Testing momentum on {len(test_slugs)} oldest 15m markets...")

    # For each market, resolve outcome and get BTC price data
    momentum_correct = 0
    momentum_wrong = 0
    flat_markets = 0

    for slug in test_slugs:
        winner = resolve_market(slug)
        if not winner:
            continue

        market_ts = int(BTC_15M_RE.match(slug).group(1))
        start_ms = market_ts * 1000
        end_ms = (market_ts + 900) * 1000  # 15 min = 900s

        # Get BTC 1-min klines for this window
        klines = get_btc_kline(start_ms, end_ms)
        if len(klines) < 6:  # Need at least 6 minutes of data
            continue

        # Price at market start
        price_start = klines[0][1]  # open of first candle
        # Price at 5-min mark
        price_5m = klines[4][2] if len(klines) > 4 else klines[-1][2]  # close of 5th candle
        # Price at end
        price_end = klines[-1][2]  # close of last candle

        # BTC direction in first 5 min
        delta_5m = price_5m - price_start
        delta_pct = abs(delta_5m) / price_start * 100

        # Skip if movement is too small (< 0.01%)
        if delta_pct < 0.01:
            flat_markets += 1
            continue

        # Momentum prediction: first 5 min direction = final direction
        if delta_5m > 0:
            momentum_prediction = "Up"
        else:
            momentum_prediction = "Down"

        if momentum_prediction == winner:
            momentum_correct += 1
        else:
            momentum_wrong += 1

    total_tested = momentum_correct + momentum_wrong
    if total_tested > 0:
        momentum_wr = momentum_correct / total_tested * 100
        log(f"\n  Momentum results ({total_tested} markets tested):")
        log(f"    Correct: {momentum_correct} | Wrong: {momentum_wrong}")
        log(f"    Momentum Win Rate: {momentum_wr:.1f}%")
        log(f"    Flat/skipped: {flat_markets}")
        log(f"    VERDICT: {'MOMENTUM HAS EDGE' if momentum_wr > 53 else 'NO MOMENTUM EDGE'}")
        return momentum_wr
    else:
        log("  Could not test momentum (no resolved markets with kline data)")
        return 0


def main():
    log("=" * 70)
    log("=== PHASE 1: 15-MINUTE MARKET VALIDATION ===")
    log("=" * 70)

    # === STEP 1a: Win Rate Validation ===
    log("\n=== STEP 1a: TOP TRADER 15-MIN WIN RATES ===\n")

    all_15m_results = []
    all_5m_results = []

    for wallet, label in WALLETS.items():
        log(f"--- {wallet[:14]}... ({label}) ---")

        trades_15m, trades_5m = scan_wallet_15m(wallet, pages=5)
        log(f"  Found: {len(trades_15m)} BTC 15m trades, {len(trades_5m)} BTC 5m trades")

        if trades_15m:
            result_15m = analyze_trades(trades_15m, BTC_15M_RE, 900, max_resolve=20)
            if result_15m and result_15m["decided"] > 0:
                log(
                    f"  15m: {result_15m['wins']}W/{result_15m['losses']}L "
                    f"({result_15m['win_rate']}% WR) | "
                    f"PnL: ${result_15m['pnl']:+.2f} | "
                    f"ROI: {result_15m['roi']:+.1f}% | "
                    f"Avg entry: ${result_15m['avg_price']} | "
                    f"Up: {result_15m['up_record']} Down: {result_15m['down_record']}"
                )
                all_15m_results.append(result_15m)
            else:
                log(f"  15m: {result_15m['total_buys'] if result_15m else 0} buys, 0 resolved")

        if trades_5m:
            result_5m = analyze_trades(trades_5m, BTC_5M_RE, 300, max_resolve=10)
            if result_5m and result_5m["decided"] > 0:
                log(
                    f"   5m: {result_5m['wins']}W/{result_5m['losses']}L "
                    f"({result_5m['win_rate']}% WR) | "
                    f"PnL: ${result_5m['pnl']:+.2f} | "
                    f"Comparison baseline"
                )
                all_5m_results.append(result_5m)

        log("")

    # Summary
    log("=" * 70)
    log("=== STEP 1a SUMMARY ===")

    if all_15m_results:
        total_wins = sum(r["wins"] for r in all_15m_results)
        total_losses = sum(r["losses"] for r in all_15m_results)
        total_decided = total_wins + total_losses
        total_pnl = sum(r["pnl"] for r in all_15m_results)
        combined_wr = total_wins / total_decided * 100 if total_decided > 0 else 0

        log(f"  Combined 15m: {total_wins}W/{total_losses}L ({combined_wr:.1f}% WR) | PnL: ${total_pnl:+.2f}")
        log(f"  DECISION GATE: {'PASS - WR > 52%' if combined_wr > 52 else 'FAIL - WR <= 52%'}")
    else:
        log("  No 15m results resolved. INCONCLUSIVE.")

    if all_5m_results:
        total_5m_wins = sum(r["wins"] for r in all_5m_results)
        total_5m_losses = sum(r["losses"] for r in all_5m_results)
        total_5m_decided = total_5m_wins + total_5m_losses
        combined_5m_wr = total_5m_wins / total_5m_decided * 100 if total_5m_decided > 0 else 0
        log(f"  Combined 5m:  {total_5m_wins}W/{total_5m_losses}L ({combined_5m_wr:.1f}% WR) (baseline)")

    # === STEP 1c: Momentum Check ===
    momentum_wr = momentum_backtest()

    # === FINAL VERDICT ===
    log("\n" + "=" * 70)
    log("=== FINAL VERDICT ===")
    log("=" * 70)

    passed = True
    if all_15m_results:
        total_wins = sum(r["wins"] for r in all_15m_results)
        total_losses = sum(r["losses"] for r in all_15m_results)
        total_decided = total_wins + total_losses
        combined_wr = total_wins / total_decided * 100 if total_decided > 0 else 0
        if combined_wr <= 52:
            log("  GATE 1a FAILED: 15m win rate <= 52%")
            passed = False
        else:
            log(f"  GATE 1a PASSED: 15m win rate = {combined_wr:.1f}%")
    else:
        log("  GATE 1a INCONCLUSIVE: no resolved data")
        passed = False

    if momentum_wr > 53:
        log(f"  GATE 1c PASSED: Momentum WR = {momentum_wr:.1f}%")
    elif momentum_wr > 0:
        log(f"  GATE 1c FAILED: Momentum WR = {momentum_wr:.1f}%")
        passed = False
    else:
        log("  GATE 1c INCONCLUSIVE: no data")

    if passed:
        log("\n  >>> PROCEED TO PHASE 2: BUILD STRATEGY <<<")
    else:
        log("\n  >>> DO NOT PROCEED — INSUFFICIENT EDGE <<<")
        log("  Consider: market making, longer timeframes, or event markets")


if __name__ == "__main__":
    main()
