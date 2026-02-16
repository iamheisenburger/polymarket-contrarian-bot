"""
Binance vs Polymarket Lag Collector

Logs BTC spot price (Binance websocket) and Polymarket binary market prices
side by side every second. Goal: determine if the binary market lags behind
actual BTC price movements, and by how much.

No trading. Pure observation. Run for 24-48 hours.

Output: /opt/polymarket-bot/data/lag_data.csv
"""

import asyncio
import csv
import json
import time
import os
from datetime import datetime, timezone
from pathlib import Path

import requests
import websockets

# --- Config ---
DATA_FILE = "data/lag_data.csv"
BINANCE_WS = "wss://stream.binance.com:9443/ws/btcusdt@trade"
GAMMA_API = "https://gamma-api.polymarket.com"
CLOB_WS = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

CSV_HEADERS = [
    "timestamp",
    "epoch_ms",
    "btc_price",
    "btc_price_prev",
    "btc_delta",
    "btc_delta_pct",
    "poly_up_mid",
    "poly_down_mid",
    "poly_up_best_bid",
    "poly_up_best_ask",
    "poly_down_best_bid",
    "poly_down_best_ask",
    "market_slug",
    "market_seconds_left",
]


class LagCollector:
    def __init__(self):
        self.btc_price = 0.0
        self.btc_price_prev = 0.0
        # Sticky prices — only overwritten by actual WS updates
        self.poly_up_bid = 0.0
        self.poly_up_ask = 0.0
        self.poly_down_bid = 0.0
        self.poly_down_ask = 0.0
        self.current_market_slug = ""
        self.market_end_time = 0
        self.token_ids = {}
        self.running = True
        self.rows_written = 0
        # Event to signal polymarket_listener to reconnect with new tokens
        self._reconnect_poly = asyncio.Event()

        self._find_active_market()

        Path(DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
        write_header = not os.path.exists(DATA_FILE)
        self.csv_file = open(DATA_FILE, "a", newline="")
        self.csv_writer = csv.writer(self.csv_file)
        if write_header:
            self.csv_writer.writerow(CSV_HEADERS)

    def _find_active_market(self):
        """Find the current active BTC 5-min up/down market via slug pattern."""
        try:
            now = datetime.now(timezone.utc)
            minute = (now.minute // 5) * 5
            current_window = now.replace(minute=minute, second=0, microsecond=0)
            current_ts = int(current_window.timestamp())

            # Try current, next, and previous 5-min windows
            for offset in [0, 300, -300]:
                ts = current_ts + offset
                slug = f"btc-updown-5m-{ts}"
                resp = requests.get(
                    f"{GAMMA_API}/markets/slug/{slug}",
                    timeout=10,
                )
                if resp.status_code != 200:
                    continue
                m = resp.json()
                if not m.get("acceptingOrders"):
                    continue

                old_slug = self.current_market_slug
                self.current_market_slug = slug
                end = m.get("endDate", "")
                if end:
                    try:
                        self.market_end_time = datetime.fromisoformat(
                            end.replace("Z", "+00:00")
                        ).timestamp()
                    except Exception:
                        self.market_end_time = ts + 300

                clob_ids = m.get("clobTokenIds", "")
                outcomes_raw = m.get("outcomes", "[]")
                if clob_ids:
                    try:
                        ids = json.loads(clob_ids)
                        outcomes = json.loads(outcomes_raw)
                        self.token_ids = {
                            o.lower(): tid for o, tid in zip(outcomes, ids)
                        }
                    except Exception:
                        self.token_ids = {}

                # Reset poly prices for new market
                if slug != old_slug:
                    self.poly_up_bid = 0.0
                    self.poly_up_ask = 0.0
                    self.poly_down_bid = 0.0
                    self.poly_down_ask = 0.0
                    # Signal websocket to reconnect with new tokens
                    self._reconnect_poly.set()

                print(f"Found market: {slug} | tokens: {list(self.token_ids.keys())}")
                return

            print("No active BTC 5-min market found, will retry...")
        except Exception as e:
            print(f"Error finding market: {e}")

    async def binance_listener(self):
        """Listen to Binance BTC/USDT trades via websocket."""
        while self.running:
            try:
                async with websockets.connect(BINANCE_WS) as ws:
                    print("Binance WS connected")
                    async for msg in ws:
                        if not self.running:
                            break
                        data = json.loads(msg)
                        self.btc_price_prev = self.btc_price
                        self.btc_price = float(data.get("p", 0))
            except Exception as e:
                print(f"Binance WS error: {e}")
                await asyncio.sleep(2)

    async def polymarket_listener(self):
        """Listen to Polymarket orderbook updates. Reconnects on market transition."""
        while self.running:
            try:
                if not self.token_ids:
                    await asyncio.sleep(5)
                    self._find_active_market()
                    continue

                self._reconnect_poly.clear()
                assets = list(self.token_ids.values())
                subscribe_msg = {
                    "assets_ids": assets,
                    "type": "MARKET",
                }

                async with websockets.connect(CLOB_WS) as ws:
                    await ws.send(json.dumps(subscribe_msg))
                    print(f"Polymarket WS connected, subscribed to {len(assets)} assets")

                    while not self._reconnect_poly.is_set():
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue
                        if not self.running:
                            break
                        if not msg or msg == "[]":
                            continue
                        try:
                            data = json.loads(msg)
                            if isinstance(data, list):
                                for update in data:
                                    self._process_poly_update(update)
                            elif isinstance(data, dict):
                                self._process_poly_update(data)
                        except json.JSONDecodeError:
                            continue

                    print("Polymarket WS: reconnecting for new market...")

            except Exception as e:
                print(f"Polymarket WS error: {e}")
                await asyncio.sleep(2)

    def _process_poly_update(self, update):
        """Process a Polymarket orderbook update. Prices are sticky."""
        asset_id = update.get("asset_id", "")

        side = None
        for s, tid in self.token_ids.items():
            if tid == asset_id:
                side = s
                break

        if not side:
            return

        bids = update.get("bids", [])
        asks = update.get("asks", [])

        # Only update if data is present (sticky — keep last known value)
        if bids:
            best_bid = float(bids[0].get("price", 0))
            if best_bid > 0:
                if "yes" in side or "up" in side:
                    self.poly_up_bid = best_bid
                elif "no" in side or "down" in side:
                    self.poly_down_bid = best_bid

        if asks:
            best_ask = float(asks[0].get("price", 0))
            if best_ask > 0:
                if "yes" in side or "up" in side:
                    self.poly_up_ask = best_ask
                elif "no" in side or "down" in side:
                    self.poly_down_ask = best_ask

    @property
    def poly_up_mid(self):
        if self.poly_up_bid > 0 and self.poly_up_ask > 0:
            return (self.poly_up_bid + self.poly_up_ask) / 2
        return self.poly_up_bid or self.poly_up_ask

    @property
    def poly_down_mid(self):
        if self.poly_down_bid > 0 and self.poly_down_ask > 0:
            return (self.poly_down_bid + self.poly_down_ask) / 2
        return self.poly_down_bid or self.poly_down_ask

    async def ticker(self):
        """Write a row every second."""
        print("Ticker started - writing every 1 second")
        while self.running:
            await asyncio.sleep(1)

            if self.btc_price <= 0:
                continue

            now = datetime.now(timezone.utc)
            epoch_ms = int(time.time() * 1000)

            delta = self.btc_price - self.btc_price_prev if self.btc_price_prev > 0 else 0
            delta_pct = (delta / self.btc_price_prev * 100) if self.btc_price_prev > 0 else 0

            secs_left = max(0, self.market_end_time - time.time()) if self.market_end_time > 0 else 0

            row = [
                now.isoformat(),
                epoch_ms,
                f"{self.btc_price:.2f}",
                f"{self.btc_price_prev:.2f}",
                f"{delta:.2f}",
                f"{delta_pct:.6f}",
                f"{self.poly_up_mid:.4f}",
                f"{self.poly_down_mid:.4f}",
                f"{self.poly_up_bid:.4f}",
                f"{self.poly_up_ask:.4f}",
                f"{self.poly_down_bid:.4f}",
                f"{self.poly_down_ask:.4f}",
                self.current_market_slug,
                f"{secs_left:.0f}",
            ]

            self.csv_writer.writerow(row)
            self.rows_written += 1

            if self.rows_written % 60 == 0:
                self.csv_file.flush()
                print(
                    f"[{now.strftime('%H:%M:%S')}] "
                    f"BTC: ${self.btc_price:.2f} | "
                    f"UP: {self.poly_up_mid:.4f} (b:{self.poly_up_bid:.2f} a:{self.poly_up_ask:.2f}) "
                    f"DOWN: {self.poly_down_mid:.4f} (b:{self.poly_down_bid:.2f} a:{self.poly_down_ask:.2f}) | "
                    f"{self.rows_written} rows"
                )

            if secs_left <= 0 and self.market_end_time > 0:
                await asyncio.sleep(5)
                self._find_active_market()

    async def run(self):
        """Run all listeners concurrently."""
        print(f"Starting lag collector - writing to {DATA_FILE}")
        print(f"Current market: {self.current_market_slug}")
        print("Press Ctrl+C to stop\n")

        await asyncio.gather(
            self.binance_listener(),
            self.polymarket_listener(),
            self.ticker(),
        )


if __name__ == "__main__":
    collector = LagCollector()
    try:
        asyncio.run(collector.run())
    except KeyboardInterrupt:
        print(f"\nStopped. Wrote {collector.rows_written} rows to {DATA_FILE}")
        collector.csv_file.close()
