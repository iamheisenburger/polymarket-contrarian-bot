#!/usr/bin/env python3
"""
Download ALL PolyBacktest data locally for instant backtesting.
Run once, then use polybacktest_sweep.py for analysis.

Usage: python scripts/polybacktest_download.py --coins btc sol eth
"""
import json, os, sys, time, argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import requests

API_KEY = os.environ.get("POLYBACKTEST_API_KEY", "")
BASE = "https://api.polybacktest.com"
CACHE_DIR = "data/polybacktest_cache"

def api_get(path, retries=3):
    for attempt in range(retries):
        try:
            resp = requests.get(
                f"{BASE}{path}",
                headers={"X-API-Key": API_KEY},
                timeout=15,
            )
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 429:  # Rate limited
                wait = 2 ** attempt
                print(f"  Rate limited, waiting {wait}s...")
                time.sleep(wait)
                continue
            print(f"  HTTP {resp.status_code} on {path}")
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(1)
    return None


def download_markets(coin, market_type="5m"):
    """Download all resolved markets for a coin."""
    print(f"\n[{coin.upper()}] Downloading {market_type} markets...")
    all_markets = []
    offset = 0
    while True:
        data = api_get(f"/v2/markets?coin={coin}&market_type={market_type}&limit=100&offset={offset}&resolved=true")
        if not data:
            break
        markets = data.get("markets", [])
        if not markets:
            break
        all_markets.extend(markets)
        offset += len(markets)
        if offset % 500 == 0:
            print(f"  ...{offset} markets")
    print(f"  Total: {len(all_markets)} {coin.upper()} {market_type} markets")
    return all_markets


def download_snapshots(markets, coin, target_elapsed=120):
    """Download one snapshot per market at target_elapsed seconds into window."""
    print(f"\n[{coin.upper()}] Downloading snapshots ({len(markets)} markets, {target_elapsed}s elapsed)...")

    def fetch_one(m):
        try:
            start = datetime.fromisoformat(m["start_time"].replace("Z", "+00:00"))
            snap_ts = start.timestamp() + target_elapsed
            snap_iso = datetime.fromtimestamp(snap_ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = api_get(f"/v2/markets/{m['market_id']}/snapshot-at/{snap_iso}?coin={coin}")
            if data and data.get("snapshots"):
                return (m["market_id"], data["snapshots"][0])
        except Exception:
            pass
        return (m["market_id"], None)

    snapshots = {}
    t0 = time.time()

    # Sequential with 0.3s delay — respects API rate limits
    for i, m in enumerate(markets):
        mid, snap = fetch_one(m)
        if snap:
            snapshots[mid] = snap

        if (i + 1) % 100 == 0:
            elapsed = time.time() - t0
            rate = (i + 1) / elapsed if elapsed > 0 else 0
            got = len(snapshots)
            eta = (len(markets) - i - 1) / rate if rate > 0 else 0
            print(f"  ...{i+1}/{len(markets)} ({got} snaps, {rate:.1f}/s, ETA {eta/60:.0f}m)", flush=True)

        time.sleep(0.3)  # ~3 req/s, safe for API

    print(f"  Got {len(snapshots)} snapshots in {time.time()-t0:.0f}s")
    return snapshots


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["btc"], help="Coins to download (btc sol eth)")
    parser.add_argument("--timeframe", default="5m", help="Market timeframe")
    parser.add_argument("--elapsed", type=int, default=120, help="Snapshot at N seconds into window")
    args = parser.parse_args()

    if not API_KEY:
        print("Set POLYBACKTEST_API_KEY environment variable")
        sys.exit(1)

    os.makedirs(CACHE_DIR, exist_ok=True)

    for coin in args.coins:
        coin = coin.lower()

        # Download markets
        markets = download_markets(coin, args.timeframe)
        if not markets:
            continue

        # Save markets
        markets_file = os.path.join(CACHE_DIR, f"{coin}_{args.timeframe}_markets.json")
        with open(markets_file, "w") as f:
            json.dump(markets, f)
        print(f"  Saved {markets_file}")

        # Download snapshots
        resolved = [m for m in markets if m.get("winner")]
        snapshots = download_snapshots(resolved, coin, args.elapsed)

        # Save snapshots
        snaps_file = os.path.join(CACHE_DIR, f"{coin}_{args.timeframe}_snapshots.json")
        with open(snaps_file, "w") as f:
            json.dump(snapshots, f)
        print(f"  Saved {snaps_file}")

        # Build combined signals file for instant backtesting
        import math
        def norm_cdf(x):
            t = 1.0/(1.0+0.2316419*abs(x))
            p = math.exp(-x*x/2)/math.sqrt(2*math.pi)
            c = 1-p*t*(0.319381530+t*(-0.356563782+t*(1.781477937+t*(-1.821255978+t*1.330274429))))
            return c if x >= 0 else 1-c

        signals = []
        for m in resolved:
            mid = m["market_id"]
            if mid not in snapshots:
                continue
            s = snapshots[mid]
            strike = m.get("btc_price_start") or m.get("sol_price_start") or m.get("eth_price_start", 0)
            winner = m["winner"]
            spot = s.get("btc_price") or s.get("sol_price") or s.get("eth_price", 0)
            ask_up = s.get("price_up", 0)
            ask_down = s.get("price_down", 0)
            if not all([strike, spot, ask_up, ask_down]):
                continue
            if strike <= 0:
                continue

            disp = (spot - strike) / strike
            if abs(disp) < 0.00005:
                continue

            side = "Up" if disp > 0 else "Down"
            ask = ask_up if side == "Up" else ask_down
            if ask <= 0 or ask >= 1:
                continue

            tte_s = 300 - args.elapsed  # seconds remaining
            tte_yr = max(tte_s, 1) / (365.25 * 24 * 3600)
            d_val = disp / (0.15 * math.sqrt(tte_yr))
            fv_up = norm_cdf(d_val)
            fair_prob = fv_up if side == "Up" else (1 - fv_up)

            signals.append({
                "coin": coin,
                "market": m["slug"],
                "side": side,
                "winner": winner,
                "won": side == winner,
                "spot": spot,
                "strike": strike,
                "momentum": abs(disp),
                "fv": fair_prob,
                "ask": ask,
                "pnl": (1.0 - ask) if (side == winner) else -ask,
                "start_time": m["start_time"],
            })

        signals_file = os.path.join(CACHE_DIR, f"{coin}_{args.timeframe}_signals.json")
        with open(signals_file, "w") as f:
            json.dump(signals, f)
        print(f"  Built {len(signals)} signals -> {signals_file}")
        print(f"  Won: {sum(1 for s in signals if s['won'])}, Lost: {sum(1 for s in signals if not s['won'])}")

    print("\nDone. Run: python scripts/polybacktest_sweep.py --coins " + " ".join(args.coins))


if __name__ == "__main__":
    main()
