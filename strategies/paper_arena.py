"""
Paper Arena — Unified framework for testing multiple strategies simultaneously.

Shared infrastructure handles market discovery, price feeds, settlement,
and trade logging. Each strategy is a thin signal class (~50-100 lines)
that evaluates a MarketContext and returns a TradeSignal or None.

One process. One BinancePriceFeed. One ChainlinkPriceFeed. 4 MarketManagers.
10 signal evaluators. 10 CSV files. All observe mode.
"""

import asyncio
import math
import time
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional

from lib.binance_ws import BinancePriceFeed
from lib.chainlink_ws import ChainlinkPriceFeed
from lib.fair_value import BinaryFairValue, FairValue
from lib.market_manager import MarketManager, MarketInfo
from lib.trade_logger import TradeLogger
from src.gamma_client import GammaClient
from src.bot import TradingBot

logger = logging.getLogger(__name__)

# Fee rates by timeframe
TAKER_FEE_RATES = {
    "5m": 0.0176,
    "15m": 0.0624,
    "4h": 0.0,
    "1h": 0.0,
    "daily": 0.0,
}

# Fixed tokens per paper trade (min-size data collection)
TOKENS_PER_TRADE = 5.0

# Timeframe durations in seconds
TIMEFRAME_SECONDS = {"5m": 300, "15m": 900, "4h": 14400, "1h": 3600}


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class MarketContext:
    """All market data for a single coin at a single moment."""

    coin: str
    slug: str
    strike_price: float
    seconds_to_expiry: float
    market_age_seconds: float
    timeframe: str

    # Binance
    binance_spot: float = 0.0
    binance_momentum_30s: float = 0.0
    binance_momentum_60s: float = 0.0
    binance_volatility: float = 0.50

    # Chainlink (None if unavailable)
    chainlink_spot: Optional[float] = None
    chainlink_momentum_30s: Optional[float] = None

    # Fair values
    fv_binance: Optional[FairValue] = None
    fv_chainlink: Optional[FairValue] = None

    # Orderbook — UP side
    up_best_bid: float = 0.0
    up_best_ask: float = 1.0
    up_bid_depth: float = 0.0
    up_ask_depth: float = 0.0
    up_spread: float = 1.0

    # Orderbook — DOWN side
    down_best_bid: float = 0.0
    down_best_ask: float = 1.0
    down_bid_depth: float = 0.0
    down_ask_depth: float = 0.0
    down_spread: float = 1.0

    # Fee
    taker_fee_rate: float = 0.0

    def best_ask(self, side: str) -> float:
        return self.up_best_ask if side == "up" else self.down_best_ask

    def best_bid(self, side: str) -> float:
        return self.up_best_bid if side == "up" else self.down_best_bid

    def spread(self, side: str) -> float:
        return self.up_spread if side == "up" else self.down_spread

    def bid_depth(self, side: str) -> float:
        return self.up_bid_depth if side == "up" else self.down_bid_depth

    def ask_depth(self, side: str) -> float:
        return self.up_ask_depth if side == "up" else self.down_ask_depth

    def fee_for_price(self, price: float) -> float:
        if self.taker_fee_rate <= 0:
            return 0.0
        return price * (1.0 - price) * self.taker_fee_rate


@dataclass
class TradeSignal:
    """A trade decision from a signal strategy."""

    strategy_name: str
    side: str  # "up" or "down"
    confidence: float  # 0-1
    max_entry_price: float
    reason: str


class ArenaSignal(ABC):
    """Base class for arena signal strategies."""

    @property
    @abstractmethod
    def name(self) -> str:
        ...

    @property
    def description(self) -> str:
        return ""

    @property
    def coins(self) -> List[str]:
        """Which coins this strategy trades. Default: all 4."""
        return ["BTC", "ETH", "SOL", "XRP"]

    @abstractmethod
    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        ...


# ---------------------------------------------------------------------------
# Internal state tracking
# ---------------------------------------------------------------------------

@dataclass
class CoinState:
    """Per-coin market state."""

    coin: str
    manager: MarketManager
    current_slug: str = ""
    strike_price: float = 0.0
    market_end_ts: float = 0.0
    market_start_ts: float = 0.0
    is_startup: bool = True


@dataclass
class StrategyPosition:
    """Paper position for one strategy on one coin in the current market."""

    slug: str = ""
    side: str = ""
    tokens: float = 0.0
    cost: float = 0.0
    entry_price: float = 0.0


@dataclass
class StrategyState:
    """Per-strategy tracking. Stats come from logger.stats."""

    name: str
    balance: float
    logger: TradeLogger
    positions: Dict[str, StrategyPosition] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Paper Arena engine
# ---------------------------------------------------------------------------

class PaperArena:
    """
    Unified paper trading engine.

    Register signal classes, then call run(). The arena evaluates every
    signal for every coin on every tick, logs paper trades per-strategy,
    and resolves settlements via the Gamma API.
    """

    def __init__(
        self,
        bot: TradingBot,
        coins: List[str],
        timeframe: str = "5m",
        bankroll: float = 12.95,
    ):
        self.bot = bot
        self.coins = [c.upper() for c in coins]
        self.timeframe = timeframe
        self.bankroll = bankroll

        # Shared infrastructure
        self.binance = BinancePriceFeed(coins=self.coins)
        self.chainlink: Optional[ChainlinkPriceFeed] = None
        self.fv_calc = BinaryFairValue()
        self.gamma = GammaClient()

        # Per-coin state
        self.coin_states: Dict[str, CoinState] = {}

        # Registered signals
        self.signals: List[ArenaSignal] = []
        self.strategy_states: Dict[str, StrategyState] = {}

        # Timing
        self._last_settle_time: float = 0.0
        self._last_status_time: float = 0.0
        self.running = False

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, signal: ArenaSignal):
        """Register a signal strategy."""
        self.signals.append(signal)
        log_file = f"data/arena_{signal.name}.csv"
        state = StrategyState(
            name=signal.name,
            balance=self.bankroll,
            logger=TradeLogger(log_file),
            positions={coin: StrategyPosition() for coin in self.coins},
        )
        self.strategy_states[signal.name] = state

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> bool:
        """Initialize all shared infrastructure."""
        # Binance
        print("[Arena] Starting Binance price feed...")
        if not await self.binance.start():
            print("[Arena] ERROR: Binance feed failed")
            return False
        print("[Arena] Binance feed connected")

        # Chainlink (optional)
        try:
            self.chainlink = ChainlinkPriceFeed(coins=self.coins)
            print("[Arena] Starting Chainlink price feed...")
            await self.chainlink.start()
            print("[Arena] Chainlink feed connected")
        except Exception as e:
            print(f"[Arena] Chainlink unavailable ({e}), continuing without it")
            self.chainlink = None

        # MarketManagers per coin
        for coin in self.coins:
            mgr = MarketManager(
                coin=coin,
                timeframe=self.timeframe,
                market_check_interval=30.0,
            )

            @mgr.on_market_change
            def handle_change(old_slug, new_slug, c=coin):
                self._on_market_change(c, old_slug, new_slug)

            if not await mgr.start():
                print(f"[Arena] WARNING: {coin} market manager failed to start")
                continue

            await mgr.wait_for_data(timeout=10.0)

            cs = CoinState(coin=coin, manager=mgr)
            if mgr.current_market:
                cs.current_slug = mgr.current_market.slug
                cs.is_startup = True
                self._set_strike(cs)

            self.coin_states[coin] = cs
            print(f"[Arena] {coin} ready: {cs.current_slug} strike=${cs.strike_price:,.2f}")

        if not self.coin_states:
            print("[Arena] ERROR: no coins available")
            return False

        self.running = True
        return True

    async def run(self):
        """Main arena loop."""
        if not await self.start():
            return

        n_signals = len(self.signals)
        n_coins = len(self.coins)
        print(f"\n[Arena] Running: {n_signals} strategies x {n_coins} coins on {self.timeframe}")
        print(f"[Arena] Strategies: {', '.join(s.name for s in self.signals)}")
        print(f"[Arena] Bankroll per strategy: ${self.bankroll:.2f}")
        print(f"[Arena] Tokens per trade: {TOKENS_PER_TRADE:.0f} (min-size)")
        print()

        while self.running:
            try:
                for coin in self.coins:
                    ctx = self._build_context(coin)
                    if ctx is None:
                        continue

                    for signal in self.signals:
                        if coin not in signal.coins:
                            continue

                        sstate = self.strategy_states[signal.name]
                        pos = sstate.positions[coin]

                        # One trade per strategy per coin per market
                        if pos.slug == ctx.slug and pos.tokens > 0:
                            continue

                        try:
                            result = signal.evaluate(ctx)
                        except Exception as e:
                            logger.debug(f"Signal {signal.name} error: {e}")
                            continue

                        if result:
                            self._execute_paper_trade(signal, coin, result, ctx)

                self._periodic_settle()
                self._render_status()
                await asyncio.sleep(1.0)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Arena tick error: {e}")
                await asyncio.sleep(5.0)

        # Cleanup
        for cs in self.coin_states.values():
            await cs.manager.stop()
        await self.binance.stop()
        if self.chainlink:
            await self.chainlink.stop()

    # ------------------------------------------------------------------
    # Market state
    # ------------------------------------------------------------------

    def _set_strike(self, cs: CoinState):
        """Set strike price from Binance spot."""
        spot = self.binance.get_price(cs.coin)
        if spot > 0:
            cs.strike_price = spot

        market = cs.manager.current_market
        if market:
            end_ts = market.end_timestamp()
            if end_ts:
                cs.market_end_ts = float(end_ts)
                tf_secs = TIMEFRAME_SECONDS.get(self.timeframe, 300)
                cs.market_start_ts = cs.market_end_ts - tf_secs

    def _on_market_change(self, coin: str, old_slug: str, new_slug: str):
        """Handle market transition."""
        cs = self.coin_states.get(coin)
        if not cs:
            return

        was_startup = cs.is_startup
        cs.is_startup = False
        cs.current_slug = new_slug

        # Reset all strategy positions for this coin
        for sstate in self.strategy_states.values():
            sstate.positions[coin] = StrategyPosition()

        # Update market timing
        self._set_strike(cs)

        # Trigger immediate settlement attempt
        self._last_settle_time = 0

        if not was_startup:
            print(
                f"[Arena] {coin} market changed: "
                f"{old_slug[-12:]} -> {new_slug[-12:]} | "
                f"strike=${cs.strike_price:,.2f}"
            )

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(self, coin: str) -> Optional[MarketContext]:
        """Build MarketContext for a coin. Returns None if data unavailable."""
        cs = self.coin_states.get(coin)
        if not cs or cs.is_startup or not cs.current_slug:
            return None

        if cs.strike_price <= 0:
            return None

        now = time.time()
        seconds_to_expiry = max(0, cs.market_end_ts - now)
        market_age = now - cs.market_start_ts if cs.market_start_ts > 0 else 0.0

        if seconds_to_expiry <= 5:
            return None

        # Binance
        bin_spot = self.binance.get_price(coin)
        if bin_spot <= 0:
            return None
        bin_mom_30 = self.binance.get_momentum(coin, 30.0)
        bin_mom_60 = self.binance.get_momentum(coin, 60.0)
        bin_vol = self.binance.get_volatility(coin)

        # Chainlink
        cl_spot = None
        cl_mom_30 = None
        if self.chainlink and self.chainlink.connected:
            p = self.chainlink.get_price(coin)
            if p and p > 0:
                cl_spot = p
                cl_mom_30 = self.chainlink.get_momentum(coin, 30.0)

        # Fair values
        fv_bin = self.fv_calc.calculate(
            spot=bin_spot,
            strike=cs.strike_price,
            seconds_to_expiry=seconds_to_expiry,
            volatility=bin_vol,
        )

        fv_cl = None
        if cl_spot and cl_spot > 0:
            fv_cl = self.fv_calc.calculate(
                spot=cl_spot,
                strike=cs.strike_price,
                seconds_to_expiry=seconds_to_expiry,
                volatility=bin_vol,
            )

        # Orderbook
        mgr = cs.manager
        up_ob = mgr.get_orderbook("up")
        down_ob = mgr.get_orderbook("down")

        up_bb = up_ob.best_bid if up_ob else 0.0
        up_ba = up_ob.best_ask if up_ob else 1.0
        up_sp = (up_ba - up_bb) if up_bb > 0 else 1.0
        up_bd = sum(l.size for l in up_ob.bids[:5]) if up_ob and up_ob.bids else 0.0
        up_ad = sum(l.size for l in up_ob.asks[:5]) if up_ob and up_ob.asks else 0.0

        dn_bb = down_ob.best_bid if down_ob else 0.0
        dn_ba = down_ob.best_ask if down_ob else 1.0
        dn_sp = (dn_ba - dn_bb) if dn_bb > 0 else 1.0
        dn_bd = sum(l.size for l in down_ob.bids[:5]) if down_ob and down_ob.bids else 0.0
        dn_ad = sum(l.size for l in down_ob.asks[:5]) if down_ob and down_ob.asks else 0.0

        return MarketContext(
            coin=coin,
            slug=cs.current_slug,
            strike_price=cs.strike_price,
            seconds_to_expiry=seconds_to_expiry,
            market_age_seconds=market_age,
            timeframe=self.timeframe,
            binance_spot=bin_spot,
            binance_momentum_30s=bin_mom_30,
            binance_momentum_60s=bin_mom_60,
            binance_volatility=bin_vol,
            chainlink_spot=cl_spot,
            chainlink_momentum_30s=cl_mom_30,
            fv_binance=fv_bin,
            fv_chainlink=fv_cl,
            up_best_bid=up_bb,
            up_best_ask=up_ba,
            up_bid_depth=up_bd,
            up_ask_depth=up_ad,
            up_spread=up_sp,
            down_best_bid=dn_bb,
            down_best_ask=dn_ba,
            down_bid_depth=dn_bd,
            down_ask_depth=dn_ad,
            down_spread=dn_sp,
            taker_fee_rate=TAKER_FEE_RATES.get(self.timeframe, 0.0),
        )

    # ------------------------------------------------------------------
    # Paper trading
    # ------------------------------------------------------------------

    def _execute_paper_trade(
        self,
        signal: ArenaSignal,
        coin: str,
        trade: TradeSignal,
        ctx: MarketContext,
    ):
        """Execute a paper trade."""
        sstate = self.strategy_states[signal.name]
        pos = sstate.positions[coin]

        # Already traded this market
        if pos.slug == ctx.slug and pos.tokens > 0:
            return

        # Get entry price from orderbook
        entry_price = ctx.best_ask(trade.side)
        if entry_price >= trade.max_entry_price:
            return
        if entry_price <= 0.01 or entry_price >= 0.99:
            return

        # Fixed 5 tokens
        tokens = TOKENS_PER_TRADE
        cost = tokens * entry_price
        fee = ctx.fee_for_price(entry_price) * tokens
        total_cost = cost + fee

        if total_cost > sstate.balance:
            return

        # Deduct
        sstate.balance -= total_cost

        # Track position
        pos.slug = ctx.slug
        pos.side = trade.side
        pos.tokens = tokens
        pos.cost = total_cost
        pos.entry_price = entry_price

        # Log
        fv = ctx.fv_binance
        sstate.logger.log_trade(
            market_slug=ctx.slug,
            coin=coin,
            timeframe=ctx.timeframe,
            side=trade.side,
            entry_price=entry_price,
            bet_size_usdc=total_cost,
            num_tokens=tokens,
            bankroll=sstate.balance,
            fair_value_at_entry=fv.fair_up if fv else 0.0,
            time_to_expiry_at_entry=ctx.seconds_to_expiry,
            volatility_at_entry=ctx.binance_volatility,
            momentum_at_entry=ctx.binance_momentum_30s,
            vol_source="arena",
        )

        print(
            f"[Arena] TRADE {signal.name} | {coin} {trade.side.upper()} "
            f"@${entry_price:.2f} | {trade.reason} | "
            f"#{sstate.logger.stats.total_trades}"
        )

    # ------------------------------------------------------------------
    # Settlement
    # ------------------------------------------------------------------

    def _periodic_settle(self):
        """Every 30s, resolve pending trades via Gamma API."""
        now = time.time()
        if now - self._last_settle_time < 30.0:
            return
        self._last_settle_time = now

        total_pending = 0
        total_resolved = 0

        for sname, sstate in self.strategy_states.items():
            pending_slugs = sstate.logger.get_pending_slugs()
            if not pending_slugs:
                continue

            total_pending += len(pending_slugs)

            for slug in list(pending_slugs):
                winner = self._determine_winner(slug)
                if not winner:
                    continue

                total_resolved += 1
                pending = sstate.logger.get_pending_for_market(slug)
                for side, record in pending:
                    won = (side == winner)
                    payout = record.num_tokens if won else 0.0
                    sstate.balance += payout

                    sstate.logger.log_outcome(
                        market_slug=slug,
                        side=side,
                        won=won,
                        payout=payout,
                    )

                    pnl = payout - record.bet_size_usdc
                    print(
                        f"[Arena] SETTLE {sname} | {record.coin} "
                        f"{'WON' if won else 'LOST'} {side} | "
                        f"pnl=${pnl:+.2f} bal=${sstate.balance:.2f}"
                    )

        if total_pending > 0:
            print(
                f"[Arena] Settle check: {total_pending} pending slugs, "
                f"{total_resolved} resolved"
            )

    def _determine_winner(self, slug: str) -> Optional[str]:
        """Query Gamma API for market outcome."""
        try:
            market = self.gamma.get_market_by_slug(slug)
            if not market:
                logger.warning(f"Settle: Gamma returned None for {slug}")
                return None
            prices = self.gamma.parse_prices(market)
            up_p = prices.get("up", 0.5)
            down_p = prices.get("down", 0.5)
            if up_p > 0.9:
                return "up"
            elif down_p > 0.9:
                return "down"
            return None
        except Exception as e:
            logger.error(f"Settle error for {slug}: {e}")
            return None

    # ------------------------------------------------------------------
    # Status display
    # ------------------------------------------------------------------

    def _render_status(self):
        """Print status every 60 seconds."""
        now = time.time()
        if now - self._last_status_time < 60.0:
            return
        self._last_status_time = now

        lines = ["", "=" * 72, "  PAPER ARENA STATUS", "=" * 72]

        # Coin markets
        for coin, cs in self.coin_states.items():
            market = cs.manager.current_market
            cd = market.get_countdown_str() if market else "--:--"
            lines.append(
                f"  {coin}: {cs.current_slug[-15:]} | "
                f"{cd} | strike=${cs.strike_price:,.2f}"
            )

        lines.append("-" * 72)
        lines.append(
            f"  {'Strategy':<20} {'Trades':>6} {'W':>4} {'L':>4} "
            f"{'Pend':>5} {'WR':>7} {'PnL':>8} {'Bal':>8}"
        )
        lines.append("-" * 72)

        for signal in self.signals:
            ss = self.strategy_states[signal.name]
            stats = ss.logger.stats
            decided = stats.wins + stats.losses
            wr = (stats.wins / decided * 100) if decided > 0 else 0.0
            lines.append(
                f"  {signal.name:<20} {stats.total_trades:>6} "
                f"{stats.wins:>4} {stats.losses:>4} {stats.pending:>5} "
                f"{wr:>6.1f}% ${stats.total_pnl:>+7.2f} ${ss.balance:>7.2f}"
            )

        lines.append("=" * 72)
        print("\n".join(lines))
