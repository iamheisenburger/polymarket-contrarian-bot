"""
S07: Early Bird

Signal: Only trade in first 90 seconds after new market opens. If spot
has already moved > 0.08% from strike, buy that direction immediately.
Rationale: Market hasn't had time to adjust prices. Stale orderbook
from previous window. First-mover advantage.
Breakeven: ~60.8%.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S07_Early_Bird(ArenaSignal):

    @property
    def name(self) -> str:
        return "S07_EARLY"

    @property
    def description(self) -> str:
        return "First 90s of market, spot moved > 0.08% from strike"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        if ctx.market_age_seconds > 90:
            return None  # only first 90 seconds
        if ctx.market_age_seconds < 5:
            return None  # too early, data settling

        if ctx.strike_price <= 0:
            return None

        spot_move = (ctx.binance_spot - ctx.strike_price) / ctx.strike_price

        if abs(spot_move) < 0.0008:  # 0.08%
            return None

        side = "up" if spot_move > 0 else "down"
        ask = ctx.best_ask(side)

        if ask >= 0.70 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=0.60,
            max_entry_price=0.70,
            reason=f"Early {ctx.market_age_seconds:.0f}s move={spot_move*100:+.3f}% -> {side}",
        )
