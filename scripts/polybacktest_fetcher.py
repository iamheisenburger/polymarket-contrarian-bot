#!/usr/bin/env python3
"""
PolyBackTest data fetcher.

Pulls historical Polymarket up-down 5m market snapshots from polybacktest.com,
derives momentum threshold crossings, and writes them to a CSV that matches
our local collector_v3.csv format. All existing backtest tools work without
modification.

Usage:
    python scripts/polybacktest_fetcher.py --coin all --days 14
    python scripts/polybacktest_fetcher.py --coin btc --days 30

Output: data/pbt_collector.csv
"""

import os
import sys
import csv
import time
import argparse
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [pbt] %(levelname)s %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pbt")

# Load API key from .env if not in environment
if not os.environ.get("POLYBACKTEST_API_KEY"):
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("POLYBACKTEST_API_KEY="):
                os.environ["POLYBACKTEST_API_KEY"] = line.split("=", 1)[1].strip().strip('"').strip("'")
                break

API_BASE = "https://api.polybacktest.com"
API_KEY = os.environ.get("POLYBACKTEST_API_KEY", "")

THRESHOLDS = (0.0003, 0.0005, 0.0007, 0.0010, 0.0012, 0.0015, 0.0020, 0.0030)
DURATION = 300  # 5m markets


import threading

# Token bucket rate limiter — 10 req/sec across all threads.
# We aim slightly under (9 req/sec) to leave headroom and avoid 429s.
_rate_lock = threading.Lock()
_rate_window: list = []  # list of recent request timestamps
_RATE_LIMIT = 9  # requests per second
_RATE_WINDOW_SEC = 1.0


def _acquire_rate_slot():
    """Block until we can make a new request without exceeding the rate limit."""
    while True:
        with _rate_lock:
            now = time.time()
            # Drop timestamps outside the window
            while _rate_window and now - _rate_window[0] > _RATE_WINDOW_SEC:
                _rate_window.pop(0)
            if len(_rate_window) < _RATE_LIMIT:
                _rate_window.append(now)
                return
            # Need to wait
            sleep_for = _RATE_WINDOW_SEC - (now - _rate_window[0]) + 0.01
        time.sleep(max(0.01, sleep_for))


def http_get(path: str, params: dict, retries: int = 10) -> dict:
    """GET with shared rate limiter and retry on 429/5xx/connection errors.

    Uses a global token bucket so concurrent threads stay under 10 req/sec
    in aggregate. Tolerates transient connection failures with backoff.
    """
    if not API_KEY:
        raise RuntimeError("POLYBACKTEST_API_KEY not set")
    headers = {"x-api-key": API_KEY}
    url = f"{API_BASE}{path}"
    last_err = None
    for attempt in range(retries):
        _acquire_rate_slot()
        try:
            r = httpx.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", "2"))
                time.sleep(wait)
                continue
            if 500 <= r.status_code < 600:
                last_err = f"HTTP {r.status_code}"
                time.sleep(min(30, 2 ** attempt))
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.HTTPError, ConnectionError, OSError) as e:
            last_err = str(e)
            # Cap backoff at 30s but still retry
            time.sleep(min(30, 2 ** attempt))
    raise RuntimeError(f"Failed after {retries} retries: {url} ({last_err})")


def list_resolved_5m_markets(coin: str, days: int) -> list:
    """List all RESOLVED 5m markets for a coin in the last N days.

    Polybacktest /v2/markets returns mixed timeframes; we filter to 5m.
    Pagination via offset until we get fewer than `limit` results or
    we walk past our cutoff date.
    """
    cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    markets = []
    offset = 0
    limit = 100
    while True:
        data = http_get(
            "/v2/markets",
            {"coin": coin, "limit": limit, "offset": offset},
        )
        items = data.get("markets", [])
        if not items:
            break

        keep_going = True
        for m in items:
            if m.get("market_type") != "5m":
                continue
            if not m.get("winner"):
                continue  # not resolved
            try:
                start_ts = datetime.fromisoformat(m["start_time"].replace("Z", "+00:00")).timestamp()
            except Exception:
                continue
            if start_ts < cutoff:
                # We've walked past our cutoff date — done
                keep_going = False
                break
            markets.append(m)

        if not keep_going or len(items) < limit:
            break
        offset += limit
        if offset > 50000:
            log.warning("Hit safety limit at offset 50000")
            break

    return markets


def fetch_snapshots(market_id: str, coin: str, max_pages: int = 1) -> list:
    """Get snapshots for a market with orderbook included.

    Default max_pages=1 because polybacktest captures 8 snapshots/sec for 5m
    markets (~2400 total). One page (1000) = ~125 seconds covering the early
    half of the window. We process snapshots sparsely so we don't need them all.

    Set max_pages=3 for full coverage if needed.
    """
    snapshots = []
    offset = 0
    limit = 1000
    pages_fetched = 0
    while pages_fetched < max_pages:
        data = http_get(
            f"/v2/markets/{market_id}/snapshots",
            {"coin": coin, "limit": limit, "offset": offset, "include_orderbook": "true"},
        )
        items = data.get("snapshots", [])
        if not items:
            break
        snapshots.extend(items)
        pages_fetched += 1
        if len(items) < limit:
            break
        offset += limit
    return snapshots


def fetch_snapshots_sampled(market_id: str, coin: str) -> list:
    """Fetch snapshots covering the full 5m window in 2 API calls.

    Polybacktest captures ~8 snapshots/sec for 5m markets (~2400 total).
    Two pages of limit=1000 gives us indices 0-1999, covering el=0s to ~250s.
    That's enough for both GROWTH (el=90-270) and FORTRESS (el=180-270),
    missing only the very last ~50 seconds where we don't enter anyway.

    2 calls/market × 6 workers ~= ~3 markets/sec = ~1000 markets in 6 minutes.
    """
    snapshots = []
    for offset in (0, 1000):
        try:
            data = http_get(
                f"/v2/markets/{market_id}/snapshots",
                {"coin": coin, "limit": 1000, "offset": offset, "include_orderbook": "true"},
            )
            items = data.get("snapshots", [])
            if not items:
                break
            snapshots.extend(items)
            if len(items) < 1000:
                break
        except Exception:
            continue
    return snapshots


def best_ask_from_orderbook(ob: dict) -> float:
    """Get best (lowest) ask. Polybacktest returns asks in ASCENDING price order."""
    if not ob:
        return 0.0
    asks = ob.get("asks", [])
    if not asks:
        return 0.0
    # Defensive: take min just in case order changes upstream
    prices = []
    for a in asks:
        try:
            prices.append(float(a.get("price", 0)))
        except (ValueError, TypeError):
            pass
    return min(prices) if prices else 0.0


def best_bid_from_orderbook(ob: dict) -> float:
    """Get best (highest) bid. Polybacktest returns bids in DESCENDING price order."""
    if not ob:
        return 0.0
    bids = ob.get("bids", [])
    if not bids:
        return 0.0
    prices = []
    for b in bids:
        try:
            prices.append(float(b.get("price", 0)))
        except (ValueError, TypeError):
            pass
    return max(prices) if prices else 0.0


def derive_signals_from_market(market: dict, snapshots: list, coin: str) -> list:
    """Convert raw snapshots into our collector signal format.

    For each snapshot in time order:
    - Compute momentum = (current_spot - strike) / strike
    - For each threshold first crossed on each side, emit signals (both up and down)

    Strike comes from market.{coin}_price_start (provided by API).
    Outcome comes from market.winner (provided by API).
    """
    slug = market.get("slug", "")
    if not slug:
        return []

    coin_lower = coin.lower()
    strike = market.get(f"{coin_lower}_price_start")
    if not strike or strike <= 0:
        return []

    winner = (market.get("winner") or "").lower()
    if winner not in ("up", "down"):
        return []

    try:
        start_ts = datetime.fromisoformat(market["start_time"].replace("Z", "+00:00")).timestamp()
    except Exception:
        return []

    crossed = {"up": set(), "down": set()}
    rows = []

    for snap in sorted(snapshots, key=lambda s: s.get("time", "")):
        t_str = snap.get("time", "")
        if not t_str:
            continue
        try:
            ts = datetime.fromisoformat(t_str.replace("Z", "+00:00"))
            ts_sec = ts.timestamp()
        except Exception:
            continue

        if ts_sec < start_ts:
            continue
        elapsed = ts_sec - start_ts
        if elapsed > DURATION + 30:
            break  # past market end

        spot = snap.get(f"{coin_lower}_price")
        if spot is None or spot <= 0:
            continue

        disp = (spot - strike) / strike
        if abs(disp) < 0.000001:
            continue

        momentum_side = "up" if disp > 0 else "down"
        current_mom = abs(disp)

        ob_up = snap.get("orderbook_up") or {}
        ob_down = snap.get("orderbook_down") or {}
        ask_up = best_ask_from_orderbook(ob_up)
        bid_up = best_bid_from_orderbook(ob_up)
        ask_down = best_ask_from_orderbook(ob_down)
        bid_down = best_bid_from_orderbook(ob_down)

        for thresh in THRESHOLDS:
            if current_mom < thresh:
                continue

            # UP side
            if thresh not in crossed["up"]:
                crossed["up"].add(thresh)
                rows.append({
                    "timestamp": ts.isoformat(),
                    "market_slug": slug,
                    "coin": coin.upper(),
                    "side": "up",
                    "momentum_direction": momentum_side,
                    "is_momentum_side": (momentum_side == "up"),
                    "threshold": thresh,
                    "entry_price": round(ask_up, 4),
                    "best_bid": round(bid_up, 4),
                    "momentum": round(disp, 8),
                    "elapsed": round(elapsed, 1),
                    "outcome": "won" if winner == "up" else "lost",
                })

            # DOWN side
            if thresh not in crossed["down"]:
                crossed["down"].add(thresh)
                rows.append({
                    "timestamp": ts.isoformat(),
                    "market_slug": slug,
                    "coin": coin.upper(),
                    "side": "down",
                    "momentum_direction": momentum_side,
                    "is_momentum_side": (momentum_side == "down"),
                    "threshold": thresh,
                    "entry_price": round(ask_down, 4),
                    "best_bid": round(bid_down, 4),
                    "momentum": round(disp, 8),
                    "elapsed": round(elapsed, 1),
                    "outcome": "won" if winner == "down" else "lost",
                })

    return rows


def fetch_market_signals(market: dict, coin: str) -> list:
    """Worker: fetch snapshots for one market and derive signal rows.

    Uses fetch_snapshots_sampled to span the full 5m window in 3 calls
    instead of 3 sequential pagination calls.
    """
    market_id = market.get("market_id")
    if not market_id:
        return []
    try:
        snaps = fetch_snapshots_sampled(market_id, coin)
    except Exception as e:
        log.warning("Failed snapshots for %s: %s", market_id, e)
        return []
    if not snaps:
        return []
    return derive_signals_from_market(market, snaps, coin)


def fetch_coin(coin: str, days: int, output_path: Path, workers: int = 12) -> int:
    log.info("=" * 60)
    log.info("Fetching %s — last %d days (parallel x%d)", coin.upper(), days, workers)
    log.info("=" * 60)

    t0 = time.time()
    markets = list_resolved_5m_markets(coin, days)
    log.info("Found %d resolved 5m markets for %s (list took %.1fs)",
             len(markets), coin.upper(), time.time() - t0)
    if not markets:
        return 0

    EXPECTED_HEADER = [
        "timestamp", "market_slug", "coin", "side",
        "momentum_direction", "is_momentum_side",
        "threshold", "entry_price", "best_bid",
        "momentum", "elapsed", "outcome",
    ]

    write_header = not output_path.exists() or output_path.stat().st_size == 0

    total_rows = 0
    fillable_count = 0
    extreme_count = 0
    completed = 0
    t_fetch = time.time()

    with open(output_path, "a", newline="") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow(EXPECTED_HEADER)

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {ex.submit(fetch_market_signals, m, coin): m for m in markets}
            for fut in as_completed(futures):
                completed += 1
                if completed % 25 == 0:
                    rate = completed / (time.time() - t_fetch)
                    eta = (len(markets) - completed) / rate if rate > 0 else 0
                    log.info("  %s: %d/%d markets (%.1f mkt/s, ETA %.0fs, %d signals)",
                             coin.upper(), completed, len(markets), rate, eta, total_rows)
                rows = fut.result()
                for r in rows:
                    writer.writerow([
                        r["timestamp"], r["market_slug"], r["coin"], r["side"],
                        r["momentum_direction"], r["is_momentum_side"],
                        r["threshold"], r["entry_price"], r["best_bid"],
                        r["momentum"], r["elapsed"], r["outcome"],
                    ])
                    total_rows += 1
                    if r["is_momentum_side"]:
                        p = r["entry_price"]
                        if 0.30 <= p <= 0.92:
                            fillable_count += 1
                        elif p >= 0.95 or p <= 0.05:
                            extreme_count += 1

    elapsed = time.time() - t0
    log.info("%s done in %.1fs: %d rows | %d fillable mom-side | %d extreme",
             coin.upper(), elapsed, total_rows, fillable_count, extreme_count)
    return total_rows


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coin", default="all", choices=["btc", "eth", "sol", "all"])
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--output", default="data/pbt_collector.csv")
    p.add_argument("--fresh", action="store_true", help="Delete existing output file before fetching")
    args = p.parse_args()

    if not API_KEY:
        log.error("POLYBACKTEST_API_KEY not set in env or .env")
        sys.exit(1)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.fresh and output_path.exists():
        log.info("Deleting existing %s", output_path)
        output_path.unlink()

    coins = ["btc", "eth", "sol"] if args.coin == "all" else [args.coin]
    grand_total = 0
    for c in coins:
        grand_total += fetch_coin(c, args.days, output_path)

    log.info("=" * 60)
    log.info("GRAND TOTAL: %d signal rows in %s", grand_total, args.output)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
