#!/usr/bin/env python3
"""
Daily incremental polybacktest fetcher.

Reads the existing CSV files, finds the latest market_slug timestamp per coin,
and fetches ONLY markets that resolved AFTER that timestamp. Appends new
signal rows to the existing CSVs without touching old data.

Designed to run daily via cron. Idempotent — safe to run multiple times per day.

Usage:
    python scripts/pbt_daily_update.py
    python scripts/pbt_daily_update.py --dry-run

Files updated:
    data/pbt_archive/pbt_btc_30d_FULL_20260410_0907.csv
    data/pbt_eth_30d.csv
    data/pbt_sol_30d.csv
"""

import argparse
import csv
import logging
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

# Reuse the existing fetcher's helpers — DO NOT duplicate the rate limiter,
# market parsing, or signal derivation logic. We import them.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.polybacktest_fetcher import (
    http_get,
    fetch_market_signals,
    derive_signals_from_market,
    fetch_snapshots_sampled,
    API_KEY,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [pbt-daily] %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
log = logging.getLogger("pbt-daily")

EXPECTED_HEADER = [
    "timestamp", "market_slug", "coin", "side",
    "momentum_direction", "is_momentum_side",
    "threshold", "entry_price", "best_bid",
    "momentum", "elapsed", "outcome",
]

# Coin -> CSV path
TARGETS = {
    "btc": "data/pbt_archive/pbt_btc_30d_FULL_20260410_0907.csv",
    "eth": "data/pbt_eth_30d.csv",
    "sol": "data/pbt_sol_30d.csv",
}


def get_latest_timestamp(csv_path: Path, coin_upper: str) -> int:
    """Find the latest market END timestamp already in the CSV.

    Market slugs end with the unix start timestamp (e.g., btc-updown-5m-1775836800).
    We track the highest start_ts we've seen — anything older is already in the file.
    """
    if not csv_path.exists() or csv_path.stat().st_size == 0:
        return 0

    latest = 0
    seen_slugs = set()
    with open(csv_path) as f:
        reader = csv.DictReader(f)
        for r in reader:
            if r.get("coin") != coin_upper:
                continue
            slug = r.get("market_slug", "")
            if slug in seen_slugs:
                continue
            seen_slugs.add(slug)
            try:
                ts = int(slug.rsplit("-", 1)[-1])
                if ts > latest:
                    latest = ts
            except (ValueError, IndexError):
                pass
    return latest


def list_new_markets(coin: str, after_ts: int) -> list:
    """List 5m resolved markets with start_ts > after_ts."""
    markets = []
    offset = 0
    limit = 100
    seen_slugs = set()
    while True:
        data = http_get(
            "/v2/markets",
            {"coin": coin, "limit": limit, "offset": offset},
        )
        items = data.get("markets", [])
        if not items:
            break

        keep_going = True
        new_in_batch = 0
        for m in items:
            if m.get("market_type") != "5m":
                continue
            if not m.get("winner"):
                continue  # not yet resolved
            slug = m.get("slug", "")
            if slug in seen_slugs:
                continue
            try:
                start_ts = int(slug.rsplit("-", 1)[-1])
            except (ValueError, IndexError):
                continue
            if start_ts <= after_ts:
                # Hit existing data — done paging
                keep_going = False
                break
            seen_slugs.add(slug)
            markets.append(m)
            new_in_batch += 1

        if not keep_going or len(items) < limit:
            break
        offset += limit
        if offset > 50000:
            log.warning("Safety limit reached at offset 50000")
            break

    return markets


def append_new_signals(csv_path: Path, new_markets: list, coin: str, workers: int = 6) -> int:
    """Fetch snapshots for new markets in parallel and append signals to CSV."""
    if not new_markets:
        return 0

    # Make sure header exists
    needs_header = not csv_path.exists() or csv_path.stat().st_size == 0
    csv_path.parent.mkdir(parents=True, exist_ok=True)

    # Process in parallel
    rows_to_write = []
    completed = 0
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(fetch_market_signals, m, coin): m for m in new_markets}
        for fut in as_completed(futures):
            completed += 1
            if completed % 25 == 0:
                rate = completed / (time.time() - t0)
                eta = (len(new_markets) - completed) / rate if rate > 0 else 0
                log.info("  %s progress: %d/%d markets (%.1f mkt/s, ETA %.0fs)",
                         coin.upper(), completed, len(new_markets), rate, eta)
            try:
                rows_to_write.extend(fut.result())
            except Exception as e:
                log.warning("Worker error: %s", e)

    if not rows_to_write:
        log.warning("%s: No signals derived from %d markets", coin.upper(), len(new_markets))
        return 0

    with open(csv_path, "a", newline="") as f:
        writer = csv.writer(f)
        if needs_header:
            writer.writerow(EXPECTED_HEADER)
        for r in rows_to_write:
            writer.writerow([
                r["timestamp"], r["market_slug"], r["coin"], r["side"],
                r["momentum_direction"], r["is_momentum_side"],
                r["threshold"], r["entry_price"], r["best_bid"],
                r["momentum"], r["elapsed"], r["outcome"],
            ])
    return len(rows_to_write)


def update_coin(coin: str, csv_relative_path: str, dry_run: bool = False) -> dict:
    coin_upper = coin.upper()
    csv_path = Path(csv_relative_path)
    log.info("=" * 60)
    log.info("UPDATING %s -> %s", coin_upper, csv_path)
    log.info("=" * 60)

    latest_ts = get_latest_timestamp(csv_path, coin_upper)
    if latest_ts > 0:
        latest_dt = datetime.fromtimestamp(latest_ts, tz=timezone.utc)
        log.info("Latest existing market: %s (start_ts=%d)", latest_dt.isoformat(), latest_ts)
    else:
        log.info("No existing data for %s — file will be created from scratch (last 30d)", coin_upper)
        # If file doesn't exist, fetch last 30d as a baseline
        latest_ts = int(datetime.now(timezone.utc).timestamp() - 30 * 86400)

    new_markets = list_new_markets(coin, latest_ts)
    log.info("%s: %d new resolved 5m markets after %d", coin_upper, len(new_markets), latest_ts)

    if dry_run:
        log.info("DRY RUN — not writing")
        return {"coin": coin_upper, "new_markets": len(new_markets), "rows_added": 0}

    if not new_markets:
        return {"coin": coin_upper, "new_markets": 0, "rows_added": 0}

    rows_added = append_new_signals(csv_path, new_markets, coin)
    log.info("%s: appended %d signal rows", coin_upper, rows_added)
    return {"coin": coin_upper, "new_markets": len(new_markets), "rows_added": rows_added}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--coin", default="all", choices=["btc", "eth", "sol", "all"])
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not API_KEY:
        log.error("POLYBACKTEST_API_KEY not set")
        sys.exit(1)

    coins = ["btc", "eth", "sol"] if args.coin == "all" else [args.coin]
    summary = []
    for c in coins:
        if c not in TARGETS:
            log.warning("No target file mapped for %s", c)
            continue
        try:
            r = update_coin(c, TARGETS[c], dry_run=args.dry_run)
            summary.append(r)
        except Exception as e:
            log.exception("Failed to update %s: %s", c, e)

    log.info("=" * 60)
    log.info("SUMMARY")
    log.info("=" * 60)
    for r in summary:
        log.info("  %s: %d new markets, %d rows added", r["coin"], r["new_markets"], r["rows_added"])


if __name__ == "__main__":
    main()
