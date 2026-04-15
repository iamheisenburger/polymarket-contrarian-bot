#!/usr/bin/env python3
"""
Binance Futures Order Flow Collector for Polymarket 5m Edge Discovery.

Connects to Binance Futures aggTrade WebSocket stream for BTC, ETH, SOL, XRP, DOGE, BNB.
Measures buy/sell aggression imbalance in rolling windows.
When imbalance exceeds threshold, logs a shadow signal with current Polymarket ask prices.
Compares predicted direction to actual 5m window outcome.

Output: data/flow_signals.csv
"""
import asyncio
import csv
import json
import os
import sys
import time
from collections import defaultdict, deque
from datetime import datetime, timezone

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import websockets
except ImportError:
    print("pip install websockets")
    sys.exit(1)

# Config
COINS = {
    "BTCUSDT": "BTC",
    "ETHUSDT": "ETH",
    "SOLUSDT": "SOL",
    "XRPUSDT": "XRP",
    "DOGEUSDT": "DOGE",
    "BNBUSDT": "BNB",
}
IMBALANCE_THRESHOLD = 0.70  # 70% buy or sell = signal
WINDOW_SECONDS = 30  # rolling window for imbalance calculation
MIN_TRADES_IN_WINDOW = 20  # need enough trades to be meaningful
MIN_VOLUME_USD = 50000  # minimum dollar volume in window
OUTPUT_FILE = "data/flow_signals.csv"
GAMMA_POLL_INTERVAL = 30  # seconds between Gamma resolution checks


class FlowCollector:
    def __init__(self):
        self.trades = defaultdict(deque)  # symbol -> deque of (timestamp, side, usd_value)
        self.signals = []  # pending signals waiting for resolution
        self.signal_count = 0
        self.csv_file = OUTPUT_FILE
        self._init_csv()
        self.polymarket_asks = {}  # coin -> {"up": price, "down": price}
        self.current_window_ts = {}  # coin -> window timestamp
        self.fired_this_window = {}  # (coin, window_ts) -> True if already fired

    def _init_csv(self):
        if not os.path.exists(self.csv_file):
            with open(self.csv_file, "w", newline="") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "timestamp", "coin", "window_slug", "predicted_side",
                    "imbalance_pct", "buy_vol_usd", "sell_vol_usd", "num_trades",
                    "entry_price", "outcome", "pnl",
                ])

    def _get_window_ts(self):
        """Current 5m window timestamp."""
        now = int(time.time())
        return now - (now % 300)

    def on_agg_trade(self, symbol: str, data: dict):
        """Process a single aggregated trade from Binance futures."""
        price = float(data["p"])
        qty = float(data["q"])
        usd_value = price * qty
        is_buyer_maker = data["m"]  # True = seller aggressed (bearish), False = buyer aggressed (bullish)
        side = "sell" if is_buyer_maker else "buy"
        ts = time.time()

        self.trades[symbol].append((ts, side, usd_value))

        # Trim old trades outside window
        cutoff = ts - WINDOW_SECONDS
        while self.trades[symbol] and self.trades[symbol][0][0] < cutoff:
            self.trades[symbol].popleft()

    def check_imbalance(self, symbol: str) -> dict:
        """Calculate current order flow imbalance for a symbol."""
        trades = self.trades[symbol]
        if len(trades) < MIN_TRADES_IN_WINDOW:
            return None

        buy_vol = sum(v for _, s, v in trades if s == "buy")
        sell_vol = sum(v for _, s, v in trades if s == "sell")
        total_vol = buy_vol + sell_vol

        if total_vol < MIN_VOLUME_USD:
            return None

        buy_pct = buy_vol / total_vol
        sell_pct = sell_vol / total_vol

        if buy_pct >= IMBALANCE_THRESHOLD:
            return {
                "direction": "up",
                "imbalance": buy_pct,
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "num_trades": len(trades),
            }
        elif sell_pct >= IMBALANCE_THRESHOLD:
            return {
                "direction": "down",
                "imbalance": sell_pct,
                "buy_vol": buy_vol,
                "sell_vol": sell_vol,
                "num_trades": len(trades),
            }
        return None

    def fire_signal(self, coin: str, imbalance_data: dict):
        """Record a signal when imbalance threshold is crossed."""
        window_ts = self._get_window_ts()
        key = (coin, window_ts)

        # Don't fire twice in same window for same coin
        if key in self.fired_this_window:
            return
        self.fired_this_window[key] = True

        # Clean old fired records
        current_ts = self._get_window_ts()
        stale_keys = [k for k in self.fired_this_window if k[1] < current_ts - 600]
        for k in stale_keys:
            del self.fired_this_window[k]

        predicted_side = imbalance_data["direction"]
        slug = f"{coin.lower()}-updown-5m-{window_ts}"

        # Get current Polymarket ask price for this side
        entry_price = 0.50  # default if we don't have orderbook data

        signal = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "coin": coin,
            "window_slug": slug,
            "window_ts": window_ts,
            "predicted_side": predicted_side,
            "imbalance_pct": imbalance_data["imbalance"],
            "buy_vol_usd": imbalance_data["buy_vol"],
            "sell_vol_usd": imbalance_data["sell_vol"],
            "num_trades": imbalance_data["num_trades"],
            "entry_price": entry_price,
            "outcome": "",  # filled on resolution
            "pnl": 0,
        }

        self.signals.append(signal)
        self.signal_count += 1

        print(
            f"[SIGNAL #{self.signal_count}] {coin} {predicted_side.upper()} "
            f"imbalance={imbalance_data['imbalance']:.0%} "
            f"buy=${imbalance_data['buy_vol']:,.0f} sell=${imbalance_data['sell_vol']:,.0f} "
            f"trades={imbalance_data['num_trades']} "
            f"window={slug}"
        )

    def resolve_signals(self):
        """Check if any pending signals can be resolved via Gamma API."""
        if not self.signals:
            return

        try:
            from src.gamma_client import GammaClient
            gamma = GammaClient()
        except Exception:
            return

        resolved = []
        for i, signal in enumerate(self.signals):
            # Only resolve if window has ended (>5 minutes old)
            if time.time() - signal["window_ts"] < 330:  # 5m + 30s buffer
                continue

            try:
                market_data = gamma.get_market_by_slug(signal["window_slug"])
                if not market_data:
                    continue

                prices = gamma.parse_prices(market_data)
                up_price = prices.get("up", 0)
                down_price = prices.get("down", 0)

                if up_price > 0.9:
                    winning_side = "up"
                elif down_price > 0.9:
                    winning_side = "down"
                else:
                    continue  # not resolved yet

                # Did we predict correctly?
                predicted_correct = signal["predicted_side"] == winning_side
                signal["outcome"] = "won" if predicted_correct else "lost"
                signal["pnl"] = 0.50 if predicted_correct else -0.50  # approximate at 50c entry

                # Write to CSV
                with open(self.csv_file, "a", newline="") as f:
                    writer = csv.writer(f)
                    writer.writerow([
                        signal["timestamp"], signal["coin"], signal["window_slug"],
                        signal["predicted_side"], f"{signal['imbalance_pct']:.3f}",
                        f"{signal['buy_vol_usd']:.0f}", f"{signal['sell_vol_usd']:.0f}",
                        signal["num_trades"], signal["entry_price"],
                        signal["outcome"], signal["pnl"],
                    ])

                status = "WON" if predicted_correct else "LOST"
                print(
                    f"[RESOLVED] {signal['coin']} {signal['predicted_side'].upper()} → {status} "
                    f"(actual: {winning_side})"
                )

                resolved.append(i)

            except Exception as e:
                if time.time() - signal["window_ts"] > 900:  # 15 min timeout
                    resolved.append(i)

        # Remove resolved signals (reverse order to preserve indices)
        for i in sorted(resolved, reverse=True):
            self.signals.pop(i)


async def run_collector():
    collector = FlowCollector()

    # Build WebSocket URL for all symbols
    streams = "/".join(f"{s.lower()}@aggTrade" for s in COINS.keys())
    uri = f"wss://fstream.binance.com/stream?streams={streams}"

    print(f"Connecting to Binance Futures aggTrade for {list(COINS.values())}...")
    print(f"Imbalance threshold: {IMBALANCE_THRESHOLD:.0%}")
    print(f"Rolling window: {WINDOW_SECONDS}s")
    print(f"Output: {OUTPUT_FILE}")
    print()

    last_resolve = 0

    while True:
        try:
            async with websockets.connect(uri) as ws:
                print("Connected to Binance Futures WebSocket")

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=30)
                        data = json.loads(msg)

                        # Combined stream format: {"stream": "btcusdt@aggTrade", "data": {...}}
                        stream_data = data.get("data", data)
                        symbol = stream_data.get("s", "")

                        if symbol in COINS:
                            collector.on_agg_trade(symbol, stream_data)

                            # Check imbalance on every trade
                            imbalance = collector.check_imbalance(symbol)
                            if imbalance:
                                collector.fire_signal(COINS[symbol], imbalance)

                        # Periodic resolution check
                        now = time.time()
                        if now - last_resolve > GAMMA_POLL_INTERVAL:
                            collector.resolve_signals()
                            last_resolve = now

                            # Print status
                            total_trades = sum(len(q) for q in collector.trades.values())
                            pending = len(collector.signals)
                            print(
                                f"[STATUS] {total_trades} trades in window | "
                                f"{collector.signal_count} signals fired | "
                                f"{pending} pending resolution"
                            )

                    except asyncio.TimeoutError:
                        collector.resolve_signals()
                        last_resolve = time.time()

        except Exception as e:
            print(f"WebSocket error: {e}. Reconnecting in 5s...")
            await asyncio.sleep(5)


if __name__ == "__main__":
    print("=" * 60)
    print("BINANCE FUTURES ORDER FLOW COLLECTOR")
    print("Shadow-testing order flow imbalance edge")
    print("=" * 60)
    asyncio.run(run_collector())
