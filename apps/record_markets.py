#!/usr/bin/env python3
"""
Market Data Recorder — Records comprehensive snapshots of every 5-minute
crypto binary market for offline backtesting.

Runs alongside the live bot. No orders placed, no balance checks.
Records:
  1. market_snapshots.csv — time series (every N seconds) of prices, orderbook, FV
  2. market_outcomes.csv  — one row per settled market with the winner

Usage:
    # Record all 7 coins on 5m markets (default)
    python apps/record_markets.py

    # Record specific coins
    python apps/record_markets.py --coins BTC ETH SOL

    # Custom snapshot interval and output directory
    python apps/record_markets.py --snapshot-interval 5 --output-dir data/recordings_v2/

    # 15m markets instead of 5m
    python apps/record_markets.py --timeframe 15m
"""

import os
import sys
import csv
import asyncio
import argparse
import logging
import time
from pathlib import Path
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Dict, Optional, List

# Fix Windows console encoding
if sys.platform == "win32":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Suppress noisy logs
logging.getLogger("src.websocket_client").setLevel(logging.WARNING)
logging.getLogger("websockets").setLevel(logging.WARNING)

# Auto-load .env
from dotenv import load_dotenv
load_dotenv()

# Add parent to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from lib.binance_ws import BinancePriceFeed, COIN_SYMBOLS
from lib.coinbase_ws import CoinbasePriceFeed, COINBASE_SYMBOLS
from lib.direct_fv import DirectFairValue
from lib.market_manager import MarketManager, MarketInfo
from src.gamma_client import GammaClient
from src.websocket_client import OrderbookSnapshot

logger = logging.getLogger("recorder")

# Coins that use Coinbase instead of Binance
COINBASE_COINS = set(COINBASE_SYMBOLS.keys())  # {"HYPE"}

SNAPSHOT_HEADERS = [
    "timestamp", "coin", "market_slug", "seconds_to_expiry", "spot_price",
    "strike_price", "best_ask_up", "best_ask_down", "best_bid_up", "best_bid_down",
    "depth_up_tokens", "depth_down_tokens",
    "momentum", "fair_value_up", "fair_value_down",
    "ema_fast", "ema_slow", "ema_trend",
    "spread_up", "spread_down",
]

OUTCOME_HEADERS = [
    "timestamp", "coin", "market_slug", "strike_price", "strike_source",
    "outcome", "settlement_price", "open_time", "close_time",
]


class RecorderEMATracker:
    """Lightweight EMA tracker — same logic as strategy's EMATracker."""

    def __init__(self, fast_period: int = 6, slow_period: int = 24):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self._alpha_fast = 2.0 / (fast_period + 1)
        self._alpha_slow = 2.0 / (slow_period + 1)
        self._ema_fast: Dict[str, float] = {}
        self._ema_slow: Dict[str, float] = {}
        self._last_update_slug: Dict[str, str] = {}

    def initialize(self, coin: str) -> bool:
        """Fetch historical 5m klines and compute initial EMAs."""
        import requests as _req
        limit = self.slow_period * 3

        try:
            if coin.upper() in COINBASE_COINS:
                closes = self._fetch_coinbase_candles(_req, coin, limit)
            else:
                closes = self._fetch_binance_candles(_req, coin, limit)

            if closes is None or len(closes) < self.slow_period:
                return False

            ema_fast = closes[0]
            ema_slow = closes[0]
            for price in closes[1:]:
                ema_fast = self._alpha_fast * price + (1 - self._alpha_fast) * ema_fast
                ema_slow = self._alpha_slow * price + (1 - self._alpha_slow) * ema_slow

            self._ema_fast[coin] = ema_fast
            self._ema_slow[coin] = ema_slow
            return True

        except Exception as e:
            logger.warning(f"EMA init failed for {coin}: {e}")
            return False

    def _fetch_binance_candles(self, _req, coin: str, limit: int):
        symbol = f"{coin.upper()}USDT"
        resp = _req.get(
            "https://api.binance.com/api/v3/klines",
            params={"symbol": symbol, "interval": "5m", "limit": limit},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        klines = resp.json()
        return [float(k[4]) for k in klines]

    def _fetch_coinbase_candles(self, _req, coin: str, limit: int):
        product_id = COINBASE_SYMBOLS.get(coin.upper())
        if not product_id:
            return None
        resp = _req.get(
            f"https://api.exchange.coinbase.com/products/{product_id}/candles",
            params={"granularity": 300},
            timeout=10,
        )
        if resp.status_code != 200:
            return None
        candles = resp.json()
        candles.reverse()
        return [float(c[4]) for c in candles[-limit:]]

    def update(self, coin: str, price: float, slug: str = ""):
        if slug and slug == self._last_update_slug.get(coin):
            return
        if slug:
            self._last_update_slug[coin] = slug

        if coin not in self._ema_fast:
            self._ema_fast[coin] = price
            self._ema_slow[coin] = price
            return

        self._ema_fast[coin] = (
            self._alpha_fast * price + (1 - self._alpha_fast) * self._ema_fast[coin]
        )
        self._ema_slow[coin] = (
            self._alpha_slow * price + (1 - self._alpha_slow) * self._ema_slow[coin]
        )

    def get_fast(self, coin: str) -> float:
        return self._ema_fast.get(coin, 0.0)

    def get_slow(self, coin: str) -> float:
        return self._ema_slow.get(coin, 0.0)

    def trend(self, coin: str) -> str:
        fast = self._ema_fast.get(coin, 0)
        slow = self._ema_slow.get(coin, 0)
        if fast == 0 or slow == 0:
            return "unknown"
        return "bullish" if fast > slow else "bearish"


class MultiPriceFeed:
    """Routes price queries to Binance or Coinbase per coin."""

    def __init__(self, primary_feed, coinbase_feed=None):
        self._primary = primary_feed
        self._coinbase = coinbase_feed

    def _feed_for(self, coin: str):
        if self._coinbase and coin.upper() in COINBASE_COINS:
            return self._coinbase
        return self._primary

    @property
    def connected(self) -> bool:
        ok = self._primary.connected
        if self._coinbase:
            ok = ok and self._coinbase.connected
        return ok

    def get_price(self, coin: str = "BTC") -> float:
        return self._feed_for(coin).get_price(coin)

    def get_age(self, coin: str = "BTC") -> float:
        return self._feed_for(coin).get_age(coin)

    def get_volatility(self, coin: str = "BTC") -> float:
        return self._feed_for(coin).get_volatility(coin)

    def get_momentum(self, coin: str, lookback_seconds: float = 30.0) -> float:
        return self._feed_for(coin).get_momentum(coin, lookback_seconds)

    async def start(self) -> bool:
        ok = await self._primary.start()
        if self._coinbase:
            ok2 = await self._coinbase.start()
            ok = ok and ok2
        return ok

    async def stop(self):
        await self._primary.stop()
        if self._coinbase:
            await self._coinbase.stop()


@dataclass
class CoinRecorderState:
    """Per-coin recording state."""
    coin: str
    manager: MarketManager
    strike_price: float = 0.0
    strike_source: str = "unknown"
    market_end_ts: float = 0.0
    market_start_ts: float = 0.0
    current_slug: str = ""
    previous_slug: str = ""
    # Track whether we've recorded the outcome for the previous market
    pending_outcome_slug: str = ""

    def seconds_to_expiry(self) -> float:
        if self.market_end_ts <= 0:
            return 300
        return max(0, self.market_end_ts - time.time())


class MarketDataRecorder:
    """
    Records comprehensive market data for every crypto binary market window.

    Produces two CSV files:
      - market_snapshots.csv: time series of prices, orderbook, fair value
      - market_outcomes.csv: settlement outcome per market
    """

    def __init__(
        self,
        coins: List[str],
        timeframe: str = "5m",
        snapshot_interval: float = 10.0,
        output_dir: str = "data/recordings",
    ):
        self.coins = coins
        self.timeframe = timeframe
        self.snapshot_interval = snapshot_interval
        self.output_dir = Path(output_dir)

        # Ensure output directory exists
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.snapshot_path = self.output_dir / "market_snapshots.csv"
        self.outcome_path = self.output_dir / "market_outcomes.csv"

        # Components
        self.price_feed: Optional[MultiPriceFeed] = None
        self.fv_calculator = DirectFairValue()
        self.ema_tracker = RecorderEMATracker(fast_period=6, slow_period=24)

        # Per-coin state
        self.coin_states: Dict[str, CoinRecorderState] = {}

        # Vatic client for exact strikes
        self._vatic = None
        try:
            from lib.vatic_client import VaticClient
            self._vatic = VaticClient()
        except Exception:
            logger.warning("VaticClient unavailable — will use Binance strikes")

        # Stats
        self._snapshots_written = 0
        self._outcomes_written = 0
        self._start_time = time.time()

        self.running = False

    def _init_csv(self, path: Path, headers: List[str]):
        """Create CSV with headers if it doesn't exist or is empty."""
        if path.exists() and path.stat().st_size > 0:
            return  # File exists and has content — append mode
        with open(path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(headers)

    def _append_snapshot(self, row: Dict):
        """Append a single snapshot row to the CSV."""
        with open(self.snapshot_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=SNAPSHOT_HEADERS)
            writer.writerow(row)
        self._snapshots_written += 1

    def _append_outcome(self, row: Dict):
        """Append a single outcome row to the CSV."""
        with open(self.outcome_path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTCOME_HEADERS)
            writer.writerow(row)
        self._outcomes_written += 1

    def _set_strike(self, state: CoinRecorderState):
        """Determine strike price for the current market."""
        market = state.manager.current_market
        if not market:
            return

        slug = market.slug
        state.current_slug = slug

        # Parse market start/end time from slug
        duration = GammaClient.TIMEFRAME_SECONDS.get(self.timeframe, 300)
        market_start_ts = 0
        try:
            ts_str = slug.rsplit("-", 1)[-1]
            market_start_ts = int(ts_str)
            state.market_end_ts = market_start_ts + duration
            state.market_start_ts = market_start_ts
        except (ValueError, IndexError):
            end_ts = market.end_timestamp()
            if end_ts:
                state.market_end_ts = end_ts
                state.market_start_ts = end_ts - duration
                market_start_ts = state.market_start_ts
            else:
                state.market_end_ts = 0
                state.market_start_ts = 0

        spot = self.price_feed.get_price(state.coin) if self.price_feed else 0
        if spot <= 0:
            return

        strike_source = "unknown"

        # 1. Try Vatic API first
        if self._vatic and market_start_ts > 0:
            try:
                vatic_strike = self._vatic.get_strike(
                    state.coin, self.timeframe, market_start_ts
                )
                if vatic_strike and vatic_strike > 0:
                    state.strike_price = vatic_strike
                    strike_source = "vatic"
            except Exception:
                pass

        # 2. Binance spot early in window
        secs_since_open = time.time() - market_start_ts if market_start_ts > 0 else 999
        if strike_source == "unknown" and secs_since_open < 60:
            state.strike_price = spot
            strike_source = "binance"

        # 3. Fall back to current spot
        if strike_source == "unknown":
            state.strike_price = spot
            strike_source = "spot_fallback"

        state.strike_source = strike_source

        # Update EMA tracker
        if state.strike_price > 0:
            self.ema_tracker.update(state.coin, state.strike_price, state.current_slug)

        logger.info(f"{state.coin} New market {slug} | strike=${state.strike_price:,.2f} ({strike_source})")

    def _check_outcome(self, state: CoinRecorderState, old_slug: str):
        """Check settlement outcome for a completed market via Gamma API."""
        if not old_slug:
            return

        try:
            gamma = GammaClient()
            market_data = gamma.get_market_by_slug(old_slug)
            if not market_data:
                return

            prices = gamma.parse_prices(market_data)
            up_price = prices.get("up", 0)
            down_price = prices.get("down", 0)

            if up_price > 0.9:
                outcome = "up"
            elif down_price > 0.9:
                outcome = "down"
            else:
                # Not yet resolved — mark as pending for retry
                state.pending_outcome_slug = old_slug
                return

            # Parse times from slug
            duration = GammaClient.TIMEFRAME_SECONDS.get(self.timeframe, 300)
            try:
                ts_str = old_slug.rsplit("-", 1)[-1]
                start_ts = int(ts_str)
                open_time = datetime.fromtimestamp(start_ts, tz=timezone.utc).isoformat()
                close_time = datetime.fromtimestamp(start_ts + duration, tz=timezone.utc).isoformat()
            except (ValueError, IndexError):
                open_time = ""
                close_time = market_data.get("endDate", "")

            # Get the settlement price (spot at close)
            settlement_price = self.price_feed.get_price(state.coin) if self.price_feed else 0

            now_str = datetime.now(timezone.utc).isoformat()
            self._append_outcome({
                "timestamp": now_str,
                "coin": state.coin,
                "market_slug": old_slug,
                "strike_price": state.strike_price,
                "strike_source": state.strike_source,
                "outcome": outcome,
                "settlement_price": settlement_price,
                "open_time": open_time,
                "close_time": close_time,
            })
            state.pending_outcome_slug = ""
            logger.info(f"{state.coin} Outcome: {old_slug} -> {outcome.upper()}")
            self._outcomes_written += 1

        except Exception as e:
            logger.warning(f"{state.coin} Outcome check failed for {old_slug}: {e}")
            state.pending_outcome_slug = old_slug

    def _record_snapshot(self, state: CoinRecorderState):
        """Record a single snapshot for a coin's current market."""
        if not state.current_slug or not self.price_feed:
            return
        if not state.manager.current_market:
            return

        spot = self.price_feed.get_price(state.coin)
        if spot <= 0:
            return

        tte = state.seconds_to_expiry()
        strike = state.strike_price

        # Orderbook data
        ob_up: Optional[OrderbookSnapshot] = state.manager.get_orderbook("up")
        ob_down: Optional[OrderbookSnapshot] = state.manager.get_orderbook("down")

        best_ask_up = ob_up.best_ask if ob_up else 1.0
        best_bid_up = ob_up.best_bid if ob_up else 0.0
        best_ask_down = ob_down.best_ask if ob_down else 1.0
        best_bid_down = ob_down.best_bid if ob_down else 0.0

        # Depth: sum of all ask sizes
        depth_up = sum(level.size for level in ob_up.asks) if ob_up else 0.0
        depth_down = sum(level.size for level in ob_down.asks) if ob_down else 0.0

        # Spreads
        spread_up = best_ask_up - best_bid_up if best_bid_up > 0 else 0.0
        spread_down = best_ask_down - best_bid_down if best_bid_down > 0 else 0.0

        # Momentum
        momentum = self.price_feed.get_momentum(state.coin, lookback_seconds=30.0)

        # Fair value
        fv_up = 0.5
        fv_down = 0.5
        if strike > 0 and tte > 0:
            fv = self.fv_calculator.calculate(spot=spot, strike=strike, seconds_to_expiry=tte)
            fv_up = fv.fair_up
            fv_down = fv.fair_down

        # EMA
        ema_fast = self.ema_tracker.get_fast(state.coin)
        ema_slow = self.ema_tracker.get_slow(state.coin)
        ema_trend = self.ema_tracker.trend(state.coin)

        now_str = datetime.now(timezone.utc).isoformat()
        self._append_snapshot({
            "timestamp": now_str,
            "coin": state.coin,
            "market_slug": state.current_slug,
            "seconds_to_expiry": round(tte, 1),
            "spot_price": spot,
            "strike_price": strike,
            "best_ask_up": best_ask_up,
            "best_ask_down": best_ask_down,
            "best_bid_up": best_bid_up,
            "best_bid_down": best_bid_down,
            "depth_up_tokens": round(depth_up, 2),
            "depth_down_tokens": round(depth_down, 2),
            "momentum": round(momentum, 8),
            "fair_value_up": round(fv_up, 4),
            "fair_value_down": round(fv_down, 4),
            "ema_fast": round(ema_fast, 4),
            "ema_slow": round(ema_slow, 4),
            "ema_trend": ema_trend,
            "spread_up": round(spread_up, 4),
            "spread_down": round(spread_down, 4),
        })

    async def run(self):
        """Main recording loop."""
        self.running = True

        # Initialize CSVs
        self._init_csv(self.snapshot_path, SNAPSHOT_HEADERS)
        self._init_csv(self.outcome_path, OUTCOME_HEADERS)

        # Setup price feeds
        binance_coins = [c for c in self.coins if c not in COINBASE_COINS]
        coinbase_coins = [c for c in self.coins if c in COINBASE_COINS]

        primary_feed = BinancePriceFeed(coins=binance_coins) if binance_coins else None
        coinbase_feed = CoinbasePriceFeed(coins=coinbase_coins) if coinbase_coins else None

        if not primary_feed and not coinbase_feed:
            logger.error("No price feeds to start")
            return

        # If no Binance coins but have Coinbase coins, use coinbase as primary
        if not primary_feed:
            primary_feed = coinbase_feed
            coinbase_feed = None

        self.price_feed = MultiPriceFeed(primary_feed, coinbase_feed)

        logger.info("Starting price feeds...")
        ok = await self.price_feed.start()
        if not ok:
            logger.error("Failed to start price feeds")
            return
        logger.info("Price feeds connected")

        # Initialize EMA tracker for each coin
        for coin in self.coins:
            if self.ema_tracker.initialize(coin):
                logger.info(f"{coin} EMA initialized from historical klines")
            else:
                logger.warning(f"{coin} EMA init failed — will warm up from live strikes")

        # Setup market managers
        for coin in self.coins:
            manager = MarketManager(
                coin=coin,
                market_check_interval=15.0,  # Check frequently for 5m markets
                auto_switch_market=True,
                timeframe=self.timeframe,
            )

            state = CoinRecorderState(coin=coin, manager=manager)
            self.coin_states[coin] = state

            # Start the market manager
            started = await manager.start()
            if not started:
                logger.warning(f"{coin} MarketManager failed to start — will retry")
                continue

            await manager.wait_for_data(timeout=5.0)

            # Set initial strike
            self._set_strike(state)

            # Register market change callback
            def make_callback(c):
                def on_change(old_slug, new_slug):
                    self._handle_market_change(c, old_slug, new_slug)
                return on_change

            manager.on_market_change(make_callback(coin))
            logger.info(f"{coin} recording started | market: {state.current_slug}")

        active_coins = [c for c in self.coins if self.coin_states[c].current_slug]
        if not active_coins:
            logger.error("No active markets found for any coin")
            await self.price_feed.stop()
            return

        logger.info(f"Recording {len(active_coins)} coins: {', '.join(active_coins)}")
        logger.info(f"Snapshot interval: {self.snapshot_interval}s")
        logger.info(f"Snapshots -> {self.snapshot_path}")
        logger.info(f"Outcomes  -> {self.outcome_path}")

        # Main loop: take snapshots at regular intervals
        try:
            while self.running:
                loop_start = time.time()

                for coin, state in self.coin_states.items():
                    # Skip coins without active markets
                    if not state.current_slug:
                        # Try to discover market
                        try:
                            market = await asyncio.to_thread(
                                state.manager.discover_market, True
                            )
                            if market:
                                self._set_strike(state)
                        except Exception:
                            pass
                        continue

                    # Record snapshot
                    try:
                        self._record_snapshot(state)
                    except Exception as e:
                        logger.warning(f"{coin} snapshot error: {e}")

                    # Retry pending outcome checks
                    if state.pending_outcome_slug:
                        try:
                            await asyncio.to_thread(
                                self._check_outcome, state, state.pending_outcome_slug
                            )
                        except Exception:
                            pass

                # Status line
                elapsed = (time.time() - self._start_time) / 60
                active = sum(1 for s in self.coin_states.values() if s.current_slug)
                print(
                    f"\r[{elapsed:.0f}m] "
                    f"Recording {active}/{len(self.coins)} coins | "
                    f"Snapshots: {self._snapshots_written} | "
                    f"Outcomes: {self._outcomes_written}",
                    end="", flush=True,
                )

                # Sleep until next interval
                elapsed_loop = time.time() - loop_start
                sleep_time = max(0.1, self.snapshot_interval - elapsed_loop)
                await asyncio.sleep(sleep_time)

        except asyncio.CancelledError:
            pass
        finally:
            print()  # Newline after status line
            logger.info(
                f"Recorder stopped. Snapshots: {self._snapshots_written}, "
                f"Outcomes: {self._outcomes_written}"
            )

            # Cleanup
            for state in self.coin_states.values():
                try:
                    await state.manager.stop()
                except Exception:
                    pass
            if self.price_feed:
                await self.price_feed.stop()

    def _handle_market_change(self, coin: str, old_slug: str, new_slug: str):
        """Handle market transition — record outcome of old market, setup new one."""
        state = self.coin_states.get(coin)
        if not state:
            return

        logger.info(f"{coin} Market changed: {old_slug} -> {new_slug}")

        # Store old strike info before overwriting
        old_strike = state.strike_price
        old_strike_source = state.strike_source

        # Check outcome of the old market
        if old_slug:
            self._check_outcome(state, old_slug)

        # Setup new market
        state.previous_slug = old_slug
        self._set_strike(state)


def main():
    parser = argparse.ArgumentParser(
        description="Record comprehensive market data for Polymarket crypto binary markets"
    )
    parser.add_argument(
        "--coins", nargs="+", type=str,
        default=["BTC", "ETH", "SOL", "XRP", "DOGE", "BNB", "HYPE"],
        help="Coins to record (default: all 7)"
    )
    parser.add_argument(
        "--timeframe", type=str, default="5m",
        choices=["5m", "15m", "4h", "1h", "daily"],
        help="Market timeframe (default: 5m)"
    )
    parser.add_argument(
        "--snapshot-interval", type=float, default=10.0,
        help="Seconds between snapshots (default: 10)"
    )
    parser.add_argument(
        "--output-dir", type=str, default="data/recordings",
        help="Directory for CSV output files (default: data/recordings/)"
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )
    args = parser.parse_args()

    # Setup logging
    log_level = logging.DEBUG if args.debug else logging.INFO
    logging.basicConfig(
        level=log_level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Validate coins
    valid_coins = ["BTC", "ETH", "SOL", "XRP", "DOGE", "HYPE", "BNB"]
    coins = [c.upper() for c in args.coins]
    for c in coins:
        if c not in valid_coins:
            print(f"Invalid coin: {c}. Options: {valid_coins}")
            sys.exit(1)

    print()
    print("=" * 60)
    print(f"  MARKET DATA RECORDER — {'/'.join(coins)} {args.timeframe}")
    print("=" * 60)
    print()
    print(f"  Coins:             {', '.join(coins)}")
    print(f"  Timeframe:         {args.timeframe}")
    print(f"  Snapshot interval: {args.snapshot_interval}s")
    print(f"  Output dir:        {args.output_dir}")
    print()
    print(f"  No orders placed. No credentials required.")
    print(f"  Append-safe: restart any time without data loss.")
    print()

    recorder = MarketDataRecorder(
        coins=coins,
        timeframe=args.timeframe,
        snapshot_interval=args.snapshot_interval,
        output_dir=args.output_dir,
    )

    try:
        asyncio.run(recorder.run())
    except KeyboardInterrupt:
        print("\nInterrupted by user")
    except Exception as e:
        print(f"\nError: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
