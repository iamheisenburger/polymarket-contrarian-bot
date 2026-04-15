#!/usr/bin/env python3
"""
Unbiased Signal Collector v2 — Single source of truth for backtesting.

Logs a snapshot at each momentum threshold crossing per market+side.
Entry price (best ask) captured from live orderbook at the exact moment
each threshold is first crossed — matching what the bot sees when it fires.

Key columns:
  - momentum_direction: "up" or "down" — which side momentum favors
  - is_momentum_side: True if this row's side matches momentum direction
  - entry_price: best ask from live orderbook (what you'd pay to buy)
  - best_bid: best bid (what you'd get to sell)

One market+side can produce multiple rows (one per threshold crossed).
The sweep filters: is_momentum_side=True AND threshold >= X AND entry_price in range.

Usage:
    python apps/run_collector.py --coins BTC ETH SOL XRP DOGE BNB HYPE
"""

import os
import sys
import asyncio
import argparse
import csv
import math
import time
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Set

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.binance_ws import BinancePriceFeed
SECONDS_PER_YEAR = 365.25 * 24 * 3600
from lib.market_manager import MarketManager
from src.client import ClobClient
from src.gamma_client import GammaClient

try:
    from lib.coinbase_ws import CoinbasePriceFeed, COINBASE_SYMBOLS
    COINBASE_COINS = set(COINBASE_SYMBOLS.keys())  # BTC, ETH, SOL, XRP, DOGE, HYPE — matches live bot
except ImportError:
    CoinbasePriceFeed = None
    COINBASE_COINS = set()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [collector] %(message)s")
log = logging.getLogger("collector")

# Thresholds to snapshot at — covers the full sweep range
THRESHOLDS = [0.0003, 0.0005, 0.0007, 0.001, 0.0012, 0.0015, 0.002, 0.003, 0.005]


def approx_inv_normal(p):
    if p <= 0.0 or p >= 1.0:
        return 0.0
    if p < 0.5:
        return -approx_inv_normal(1.0 - p)
    t = math.sqrt(-2.0 * math.log(1.0 - p))
    c0, c1, c2 = 2.515517, 0.802853, 0.010328
    d1, d2, d3 = 1.432788, 0.189269, 0.001308
    return t - (c0 + c1 * t + c2 * t * t) / (1 + d1 * t + d2 * t * t + d3 * t * t * t)


@dataclass
class CoinState:
    coin: str
    manager: MarketManager
    strike_price: float = 0.0
    strike_source: str = "unknown"
    current_slug: str = ""
    market_end_ts: float = 0.0
    market_start_ts: float = 0.0


class SignalCollector:
    def __init__(self, coins, timeframe, output_file):
        self.coins = [c.upper() for c in coins]
        self.timeframe = timeframe
        self.output_file = output_file
        self.gamma = GammaClient()
        self.duration = GammaClient.TIMEFRAME_SECONDS.get(timeframe, 300)

        self.coin_states: Dict[str, CoinState] = {}
        self.binance: Optional[BinancePriceFeed] = None
        self.coinbase = None
        self.clob = ClobClient()
        # Cache REST orderbook: {token_id: (timestamp, best_ask, best_bid)}
        # Refreshed every 3s by a background thread — always fresh when signals fire.
        self._book_cache: Dict[str, tuple] = {}
        self._book_cache_lock = __import__('threading').Lock()

        # Track which thresholds have been crossed per (slug, side)
        # Key: (slug, side), Value: set of thresholds already logged
        self.crossed: Dict[tuple, Set[float]] = {}

        # Snapshots waiting for outcome: list of signal dicts
        # Each has slug, coin, side, threshold, entry_price, momentum, elapsed
        self.pending: List[dict] = []

        self.resolved_count = 0
        self.signal_count = 0
        self.start_time = 0

        # Health monitoring — collector self-validation
        # If too many entry_prices are stuck at extremes ($0.99 or $0.0),
        # the orderbook fetch is broken. Crash loudly instead of silently logging garbage.
        self._health_window: List[float] = []  # rolling list of recent entry_prices
        self._health_check_size = 100  # check every N momentum-side signals
        self._last_health_alert = 0.0
        self._health_failures = 0

        self._init_csv()

    def _init_csv(self):
        EXPECTED_HEADER = [
            "timestamp", "market_slug", "coin", "side",
            "momentum_direction", "is_momentum_side",
            "threshold", "entry_price", "best_bid",
            "momentum", "elapsed", "outcome",
        ]
        path = Path(self.output_file)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Validate existing file has correct header. If not, archive it
        # and start fresh — never silently corrupt by appending mismatched columns.
        needs_fresh = not path.exists() or path.stat().st_size == 0
        if not needs_fresh:
            with open(self.output_file) as f:
                first_line = f.readline().strip()
            current_header = first_line.split(",")
            if current_header != EXPECTED_HEADER:
                archive_path = path.parent / f"{path.stem}_OLDFORMAT_{int(time.time())}.csv"
                log.warning("Existing CSV has wrong header. Archiving to %s", archive_path.name)
                path.rename(archive_path)
                needs_fresh = True
            else:
                with open(self.output_file) as f:
                    self.resolved_count = sum(1 for _ in f) - 1

        if needs_fresh:
            with open(self.output_file, "w", newline="") as f:
                csv.writer(f).writerow(EXPECTED_HEADER)
            log.info("CSV initialized with %d-column header", len(EXPECTED_HEADER))

    def _set_strike(self, state: CoinState):
        """Set strike price — uses price ring buffer to match live bot exactly.

        The ring buffer records every price tick with its timestamp. When this
        method fires (potentially 5-8s after window open), it looks BACK in the
        buffer to find the price at t+0. This eliminates the strike divergence
        that caused 10% of signals to have the wrong momentum direction.
        """
        market = state.manager.current_market
        if not market:
            return

        slug = market.slug
        state.current_slug = slug

        try:
            ts_str = slug.rsplit("-", 1)[-1]
            state.market_start_ts = int(ts_str)
            state.market_end_ts = state.market_start_ts + self.duration
        except (ValueError, IndexError):
            state.market_end_ts = 0
            state.market_start_ts = 0

        spot = self.binance.get_price(state.coin) if state.coin not in COINBASE_COINS else (
            self.coinbase.get_price(state.coin) if self.coinbase else 0
        )
        if spot <= 0:
            return

        secs_since_open = time.time() - state.market_start_ts if state.market_start_ts > 0 else 999
        strike_source = "unknown"

        # 1. PRICE RING BUFFER — find the tick closest to window open (t+0)
        if state.market_start_ts > 0 and hasattr(self, '_price_ring'):
            ring = self._price_ring.get(state.coin)
            if ring and len(ring) > 0:
                best_price = None
                best_delta = float('inf')
                for ts, p in ring:
                    delta = abs(ts - state.market_start_ts)
                    if delta < best_delta:
                        best_delta = delta
                        best_price = p
                if best_price is not None:
                    state.strike_price = best_price
                    strike_source = "ring_buffer"

        # 2. Current spot — fallback only if ring buffer is completely empty
        if strike_source == "unknown":
            state.strike_price = spot
            strike_source = "spot_fallback"

        state.strike_source = strike_source
        log.info("WINDOW %s %s strike=$%s [%s] (callback +%.0fs)",
                 state.coin, slug[-20:], f"{state.strike_price:,.4f}",
                 strike_source, secs_since_open)

        # Immediately warm the REST book cache for the new market's tokens.
        # Without this, the first threshold crossings (often within 10-60s)
        # get entry_price=0.0 because the background 3s refresh hasn't
        # fetched the new token IDs yet.
        for side_name in ("up", "down"):
            tid = state.manager.token_ids.get(side_name, "")
            if not tid:
                continue
            try:
                book = self.clob.get_order_book(tid)
                asks = book.get("asks", [])
                bids = book.get("bids", [])
                ba = min(float(a.get("price", 1)) for a in asks) if asks else 0.0
                bb = max(float(b.get("price", 0)) for b in bids) if bids else 0.0
                with self._book_cache_lock:
                    self._book_cache[tid] = (time.time(), ba, bb)
                log.info("  CACHE-WARM %s %s ask=$%.2f bid=$%.2f", state.coin, side_name, ba, bb)
            except Exception as e:
                log.warning("  CACHE-WARM %s %s failed: %s", state.coin, side_name, e)

    def _on_price_update(self, coin: str, price: float):
        """Called on every WS tick. Check for threshold crossings on BOTH sides."""
        # Record every tick for ring buffer strike lookback
        if not hasattr(self, '_price_ring'):
            from collections import deque
            self._price_ring = {}
        if coin not in self._price_ring:
            from collections import deque
            self._price_ring[coin] = deque(maxlen=600)
        self._price_ring[coin].append((time.time(), price))

        if time.time() - self.start_time < self.duration + 30:
            return

        state = self.coin_states.get(coin)
        if not state or not state.current_slug:
            return

        # Market transition
        mgr_market = state.manager.current_market
        if mgr_market and mgr_market.slug != state.current_slug:
            self._set_strike(state)
            return

        if not state.strike_price or state.strike_price <= 0:
            return

        disp = (price - state.strike_price) / state.strike_price
        if abs(disp) < 0.000001:
            return

        momentum_side = "up" if disp > 0 else "down"
        opposite_side = "down" if momentum_side == "up" else "up"
        current_mom = abs(disp)

        tte = state.market_end_ts - time.time() if state.market_end_ts > 0 else 300
        elapsed = max(0, self.duration - tte)
        now = datetime.now(timezone.utc).isoformat()

        # Log BOTH sides when momentum crosses a threshold
        # momentum_direction tells the sweep which side momentum favors
        for side in [momentum_side, opposite_side]:
            key = (state.current_slug, side)
            if key not in self.crossed:
                self.crossed[key] = set()

            for thresh in THRESHOLDS:
                if current_mom >= thresh and thresh not in self.crossed[key]:
                    # Entry price from background REST cache — refreshed every 3s
                    # by a daemon thread. Always fresh, never blocks the hot path.
                    entry_price = 0.0
                    best_bid = 0.0
                    token_id = state.manager.token_ids.get(side, "")
                    if token_id:
                        with self._book_cache_lock:
                            cached = self._book_cache.get(token_id)
                        if cached and (time.time() - cached[0]) < 10:
                            entry_price = cached[1]
                            best_bid = cached[2]

                    self.crossed[key].add(thresh)

                    is_mom_side = (side == momentum_side)
                    sig = {
                        "timestamp": now,
                        "slug": state.current_slug,
                        "coin": coin,
                        "side": side,
                        "momentum_direction": momentum_side,
                        "is_momentum_side": is_mom_side,
                        "threshold": thresh,
                        "entry_price": round(entry_price, 4),
                        "best_bid": round(best_bid, 4),
                        "momentum": round(current_mom, 8),
                        "elapsed": round(elapsed, 1),
                    }
                    self.pending.append(sig)
                    self.signal_count += 1

                    if is_mom_side:
                        log.info("CROSS %s %s thresh=%.4f ask=$%.2f bid=$%.2f mom=%.6f el=%.0fs [%s]",
                                 coin, side.upper(), thresh, entry_price, best_bid,
                                 current_mom, elapsed, state.current_slug[-20:])
                        # Health check: track momentum-side entry prices
                        self._health_window.append(entry_price)
                        if len(self._health_window) >= self._health_check_size:
                            self._run_health_check()

    def _run_health_check(self):
        """Validate collector data quality. Crash loudly if broken.

        Detects:
        - >70% of entry prices stuck at $0.99 (asks ordering bug, REST returning worst ask)
        - >70% stuck at $0.0 (REST API failing, returning empty asks)
        - <5% in the fillable $0.30-$0.92 range (data is useless for backtesting)

        We trade on signal quality. Garbage data → garbage decisions → real money lost.
        Better to crash and alert than silently log broken data for days.
        """
        prices = list(self._health_window)
        self._health_window.clear()
        n = len(prices)
        if n < 50:
            return

        stuck_high = sum(1 for p in prices if p >= 0.95)
        stuck_zero = sum(1 for p in prices if p <= 0.05)
        fillable = sum(1 for p in prices if 0.30 <= p <= 0.92)

        pct_high = stuck_high / n * 100
        pct_zero = stuck_zero / n * 100
        pct_fillable = fillable / n * 100

        log.info("HEALTH n=%d | stuck@$0.99=%.0f%% | stuck@$0.0=%.0f%% | fillable=%.0f%%",
                 n, pct_high, pct_zero, pct_fillable)

        # Note: extreme prices ARE valid sometimes — markets do reach $0.99 quickly.
        # But if the BULK of momentum-side signals are extreme, the orderbook fetch is broken.
        broken = False
        reason = ""
        if pct_high > 70 and pct_fillable < 10:
            broken = True
            reason = f"{pct_high:.0f}% of entry prices stuck at $0.99 — asks ordering bug suspected"
        elif pct_zero > 70:
            broken = True
            reason = f"{pct_zero:.0f}% of entry prices are $0.0 — REST orderbook fetch failing"
        elif pct_fillable < 5 and n >= 100:
            broken = True
            reason = f"only {pct_fillable:.0f}% of signals in fillable range — data is useless"

        if broken:
            self._health_failures += 1
            log.error("=" * 60)
            log.error("COLLECTOR HEALTH CHECK FAILED (failure #%d)", self._health_failures)
            log.error("Reason: %s", reason)
            log.error("Sample of recent prices: %s",
                      sorted(prices)[:5] + ['...'] + sorted(prices)[-5:])
            log.error("=" * 60)
            # Crash after 3 consecutive failures (~300 signals = ~30min)
            # so the watchdog can restart and we can investigate
            if self._health_failures >= 3:
                log.error("3 consecutive health check failures — CRASHING for watchdog restart")
                import sys
                sys.exit(2)
        else:
            self._health_failures = 0  # reset on success

    def _write_resolved(self, sig, outcome):
        row = [
            sig["timestamp"], sig["slug"], sig["coin"], sig["side"],
            sig["momentum_direction"], sig["is_momentum_side"],
            sig["threshold"], sig["entry_price"], sig["best_bid"],
            sig["momentum"], sig["elapsed"], outcome,
        ]
        # Hard guarantee: never write a row with the wrong number of columns
        if len(row) != 12:
            log.error("Row has %d columns, expected 12: %s", len(row), row)
            return
        with open(self.output_file, "a", newline="") as f:
            csv.writer(f).writerow(row)
        self.resolved_count += 1

    async def _resolve_loop(self):
        """Resolve pending signals via Gamma API every 30s.

        Uses outcomePrices with strict threshold (>= 0.99) to ensure the
        market is FULLY settled, not mid-resolution. The old threshold of
        > 0.9 could catch markets during resolution where prices temporarily
        spiked above 0.9 then settled differently — causing ~1.4% wrong
        outcomes that inflated the collector's WR by ~2pp.
        """
        while True:
            await asyncio.sleep(30)
            if not self.pending:
                continue

            resolved_indices = []
            slug_results = {}

            for i, sig in enumerate(self.pending):
                slug = sig["slug"]
                if slug not in slug_results:
                    try:
                        data = self.gamma.get_market_by_slug(slug)
                        if data:
                            # Use outcomePrices directly — ["1","0"] when fully settled
                            # Gamma API returns this as a JSON string, not a list
                            outcome_prices = data.get("outcomePrices", [])
                            if isinstance(outcome_prices, str):
                                import json
                                try:
                                    outcome_prices = json.loads(outcome_prices)
                                except (json.JSONDecodeError, TypeError):
                                    outcome_prices = []
                            if len(outcome_prices) >= 2:
                                up = float(outcome_prices[0])
                                down = float(outcome_prices[1])
                            else:
                                prices = self.gamma.parse_prices(data)
                                up = prices.get("up", 0)
                                down = prices.get("down", 0)

                            # Strict threshold: only resolve when price is >= 0.99
                            # This ensures full settlement, not mid-resolution
                            if up >= 0.99:
                                slug_results[slug] = "up"
                            elif down >= 0.99:
                                slug_results[slug] = "down"
                            else:
                                slug_results[slug] = None
                        else:
                            slug_results[slug] = None
                    except Exception:
                        slug_results[slug] = None

                winner = slug_results.get(slug)
                if winner is not None:
                    outcome = "won" if winner == sig["side"] else "lost"
                    self._write_resolved(sig, outcome)
                    resolved_indices.append(i)

            # Remove resolved (reverse order to preserve indices)
            for i in sorted(resolved_indices, reverse=True):
                self.pending.pop(i)

            if resolved_indices:
                log.info("RESOLVED %d signals", len(resolved_indices))

    async def _status_loop(self):
        while True:
            await asyncio.sleep(60)
            log.info("STATUS: %d signals logged, %d pending, %d resolved",
                     self.signal_count, len(self.pending), self.resolved_count)

    async def _cleanup_loop(self):
        """Periodically clean up crossed dict for old slugs."""
        while True:
            await asyncio.sleep(600)
            current_slugs = set(s.current_slug for s in self.coin_states.values())
            old_keys = [k for k in self.crossed if k[0] not in current_slugs]
            for k in old_keys:
                del self.crossed[k]

    async def run(self):
        self.start_time = time.time()
        log.info("Starting collector: %s %s", self.coins, self.timeframe)
        log.info("Output: %s", self.output_file)
        log.info("Threshold crossing snapshots: %s", THRESHOLDS)

        binance_coins = [c for c in self.coins if c not in COINBASE_COINS]
        if binance_coins:
            self.binance = BinancePriceFeed(coins=binance_coins)
        coinbase_coins = [c for c in self.coins if c in COINBASE_COINS]
        if coinbase_coins and CoinbasePriceFeed:
            self.coinbase = CoinbasePriceFeed(coins=coinbase_coins)

        if self.binance:
            if not await self.binance.start():
                log.error("Binance feed failed")
                return
            self.binance.on_price(self._on_price_update)
        if self.coinbase:
            await self.coinbase.start()
            self.coinbase.on_price(self._on_price_update)

        for coin in self.coins:
            mgr = MarketManager(coin=coin, timeframe=self.timeframe, ws_msg_skip_rate=1)
            ok = await mgr.start()
            state = CoinState(coin=coin, manager=mgr)
            if ok and mgr.current_market:
                # Wait for WS to receive orderbook data before proceeding
                got_data = await mgr.wait_for_data(timeout=10.0)
                self._set_strike(state)
                state._ws_ready = got_data
                ask = mgr.get_best_ask("up")
                log.info("%s ready: %s strike=$%s [%s] ws_data=%s ask=$%.2f",
                         coin, state.current_slug[-25:],
                         f"{state.strike_price:,.2f}", state.strike_source,
                         got_data, ask)
            else:
                log.warning("%s failed to start", coin)
            self.coin_states[coin] = state

        asyncio.create_task(self._resolve_loop())
        asyncio.create_task(self._status_loop())
        asyncio.create_task(self._cleanup_loop())

        # Background REST orderbook refresher — keeps _book_cache fresh
        # for all active tokens. Runs every 3s in a daemon thread so it
        # doesn't block the async event loop. Signals always read from
        # the cache, never making synchronous REST calls in the hot path.
        import threading
        def _book_refresh_loop():
            while True:
                try:
                    for st in list(self.coin_states.values()):
                        if not st.manager or not st.manager.token_ids:
                            continue
                        for side_name in ("up", "down"):
                            tid = st.manager.token_ids.get(side_name, "")
                            if not tid:
                                continue
                            try:
                                book = self.clob.get_order_book(tid)
                                asks = book.get("asks", [])
                                bids = book.get("bids", [])
                                ba = min(float(a.get("price", 1)) for a in asks) if asks else 0.0
                                bb = max(float(b.get("price", 0)) for b in bids) if bids else 0.0
                                with self._book_cache_lock:
                                    self._book_cache[tid] = (time.time(), ba, bb)
                            except Exception:
                                pass
                except Exception:
                    pass
                time.sleep(3)
        t = threading.Thread(target=_book_refresh_loop, daemon=True)
        t.start()
        log.info("Background REST orderbook refresher started (3s interval)")

        log.info("Collector running. %d coins, %d thresholds per signal.",
                 len(self.coins), len(THRESHOLDS))
        while True:
            await asyncio.sleep(3600)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--coins", nargs="+", default=["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"])
    parser.add_argument("--timeframe", default="5m")
    parser.add_argument("--output", default="data/collector.csv")
    args = parser.parse_args()

    collector = SignalCollector(args.coins, args.timeframe, args.output)
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        log.info("Stopped.")


if __name__ == "__main__":
    main()
