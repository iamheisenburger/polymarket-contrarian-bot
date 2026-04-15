#!/usr/bin/env python3
"""
Futures-Spot Spread Delta Collector.

Watches Binance spot vs futures price for BTC.
When the spread changes rapidly (futures pulling away from spot),
logs a directional signal and tracks outcome on Polymarket 5m window.

The idea: futures lead spot by seconds. When spread widens rapidly,
informed money is positioning in futures BEFORE spot moves.
"""
import asyncio
import csv
import json
import os
import sys
import time
from collections import deque
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

OUTPUT = "data/spread_signals.csv"
SPREAD_THRESHOLD = 5.0  # $5 spread change in LOOKBACK seconds = signal
LOOKBACK = 10  # seconds to measure spread change over
COINS = ["BTC"]  # start with BTC, extend later


class SpreadCollector:
    def __init__(self):
        self.spot_price = 0.0
        self.futures_price = 0.0
        self.spread_history = deque(maxlen=500)  # (timestamp, spread)
        self.signals = []
        self.signal_count = 0
        self.fired_windows = set()
        self._init_csv()

    def _init_csv(self):
        if not os.path.exists(OUTPUT):
            with open(OUTPUT, "w", newline="") as f:
                csv.writer(f).writerow([
                    "timestamp", "coin", "window_slug", "predicted_side",
                    "spread_delta", "spread_delta_pct", "spot_price", "futures_price",
                    "entry_price", "outcome", "pnl",
                ])

    def _window_ts(self):
        now = int(time.time())
        return now - (now % 300)

    def update_spot(self, price):
        self.spot_price = price

    def update_futures(self, price):
        self.futures_price = price
        if self.spot_price > 0:
            spread = self.futures_price - self.spot_price
            self.spread_history.append((time.time(), spread))
            self._check_signal()

    def _check_signal(self):
        if len(self.spread_history) < 10:
            return

        now = time.time()
        current_spread = self.spread_history[-1][1]

        # Find spread LOOKBACK seconds ago
        target_time = now - LOOKBACK
        old_spreads = [(t, s) for t, s in self.spread_history if t <= target_time]
        if not old_spreads:
            return
        old_spread = old_spreads[-1][1]

        delta = current_spread - old_spread
        delta_pct = delta / self.spot_price * 100 if self.spot_price > 0 else 0

        if abs(delta) < SPREAD_THRESHOLD:
            return

        # Signal! Futures pulling away from spot
        window_ts = self._window_ts()
        if window_ts in self.fired_windows:
            return
        self.fired_windows.add(window_ts)

        # Clean old
        stale = [w for w in self.fired_windows if w < window_ts - 600]
        for w in stale:
            self.fired_windows.discard(w)

        predicted_side = "up" if delta > 0 else "down"
        slug = f"btc-updown-5m-{window_ts}"

        signal = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin": "BTC",
            "window_slug": slug,
            "window_ts": window_ts,
            "predicted_side": predicted_side,
            "spread_delta": delta,
            "spread_delta_pct": delta_pct,
            "spot_price": self.spot_price,
            "futures_price": self.futures_price,
            "entry_price": 0.50,
            "outcome": "",
            "pnl": 0,
        }
        self.signals.append(signal)
        self.signal_count += 1

        print(
            f"[SIGNAL #{self.signal_count}] BTC {predicted_side.upper()} "
            f"spread_delta=${delta:+.1f} ({delta_pct:+.4f}%) "
            f"spot=${self.spot_price:,.2f} fut=${self.futures_price:,.2f} "
            f"window={slug}",
            flush=True,
        )

    def resolve(self):
        if not self.signals:
            return
        try:
            from src.gamma_client import GammaClient
            gamma = GammaClient()
        except Exception:
            return

        resolved = []
        for i, sig in enumerate(self.signals):
            if time.time() - sig["window_ts"] < 330:
                continue
            try:
                md = gamma.get_market_by_slug(sig["window_slug"])
                if not md:
                    continue
                prices = gamma.parse_prices(md)
                up = prices.get("up", 0)
                down = prices.get("down", 0)
                if up > 0.9:
                    winner = "up"
                elif down > 0.9:
                    winner = "down"
                else:
                    continue

                correct = sig["predicted_side"] == winner
                sig["outcome"] = "won" if correct else "lost"
                sig["pnl"] = 0.50 if correct else -0.50

                with open(OUTPUT, "a", newline="") as f:
                    csv.writer(f).writerow([
                        sig["timestamp"], sig["coin"], sig["window_slug"],
                        sig["predicted_side"], f"{sig['spread_delta']:+.2f}",
                        f"{sig['spread_delta_pct']:+.6f}",
                        f"{sig['spot_price']:.2f}", f"{sig['futures_price']:.2f}",
                        sig["entry_price"], sig["outcome"], sig["pnl"],
                    ])

                status = "WON" if correct else "LOST"
                print(f"[RESOLVED] BTC {sig['predicted_side'].upper()} → {status} (actual: {winner})", flush=True)
                resolved.append(i)
            except Exception:
                if time.time() - sig["window_ts"] > 900:
                    resolved.append(i)

        for i in sorted(resolved, reverse=True):
            self.signals.pop(i)


async def run():
    collector = SpreadCollector()

    print("=" * 60, flush=True)
    print("FUTURES-SPOT SPREAD DELTA COLLECTOR", flush=True)
    print(f"Threshold: ${SPREAD_THRESHOLD} change in {LOOKBACK}s", flush=True)
    print(f"Output: {OUTPUT}", flush=True)
    print("=" * 60, flush=True)

    last_resolve = 0
    last_status = 0

    async def watch_spot():
        while True:
            try:
                async with websockets.connect("wss://stream.binance.com/ws/btcusdt@aggTrade") as ws:
                    while True:
                        msg = await ws.recv()
                        d = json.loads(msg)
                        collector.update_spot(float(d["p"]))
            except Exception as e:
                print(f"Spot WS error: {e}", flush=True)
                await asyncio.sleep(2)

    async def watch_futures():
        nonlocal last_resolve, last_status
        while True:
            try:
                async with websockets.connect("wss://fstream.binance.com/ws/btcusdt@aggTrade") as ws:
                    while True:
                        msg = await ws.recv()
                        d = json.loads(msg)
                        collector.update_futures(float(d["p"]))

                        now = time.time()
                        if now - last_resolve > 30:
                            collector.resolve()
                            last_resolve = now
                        if now - last_status > 60:
                            pending = len(collector.signals)
                            spread = collector.futures_price - collector.spot_price if collector.spot_price > 0 else 0
                            print(
                                f"[STATUS] signals={collector.signal_count} pending={pending} "
                                f"spread=${spread:+.1f} spot=${collector.spot_price:,.2f}",
                                flush=True,
                            )
                            last_status = now
            except Exception as e:
                print(f"Futures WS error: {e}", flush=True)
                await asyncio.sleep(2)

    await asyncio.gather(watch_spot(), watch_futures())


if __name__ == "__main__":
    asyncio.run(run())
