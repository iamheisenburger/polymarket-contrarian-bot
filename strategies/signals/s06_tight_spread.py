"""
S06: Tight Spread Follower

Signal: Only trade when the dominant side's spread (ask-bid) < 3 cents.
Market makers are confident. Buy the side where mid-price > 0.55 AND
spread is tightest.
Rationale: Tight spread = MMs have conviction about direction.
Breakeven: ~60.8%.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S06_Tight_Spread(ArenaSignal):

    @property
    def name(self) -> str:
        return "S06_SPRD"

    @property
    def description(self) -> str:
        return "Follow MM conviction when spread < 3c"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        # Compute mid prices
        up_mid = (ctx.up_best_bid + ctx.up_best_ask) / 2 if ctx.up_best_bid > 0 else 0.5

        if up_mid > 0.55 and 0 < ctx.up_spread < 0.03:
            side = "up"
            spread = ctx.up_spread
            mid = up_mid
        elif up_mid < 0.45 and 0 < ctx.down_spread < 0.03:
            side = "down"
            spread = ctx.down_spread
            mid = 1.0 - up_mid
        else:
            return None

        ask = ctx.best_ask(side)
        if ask >= 0.75 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=mid,
            max_entry_price=0.75,
            reason=f"Tight spread={spread:.3f} mid={mid:.2f} -> {side}",
        )
