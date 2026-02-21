"""
S04: Orderbook Imbalance

Signal: Total bid depth on UP side > 60% of combined (UP+DOWN) bid depth.
Buy UP. Vice versa for DOWN. Skip if 40-60% (no clear imbalance).
Rationale: Informed traders accumulate before expiry. Depth reveals direction.
Breakeven: ~60.8% at typical entry.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S04_OB_Imbalance(ArenaSignal):

    @property
    def name(self) -> str:
        return "S04_OBI"

    @property
    def description(self) -> str:
        return "Orderbook bid depth imbalance > 60%"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        total_depth = ctx.up_bid_depth + ctx.down_bid_depth

        if total_depth < 10:  # too thin to trust
            return None

        up_ratio = ctx.up_bid_depth / total_depth

        if up_ratio > 0.60:
            side = "up"
            ratio = up_ratio
        elif up_ratio < 0.40:
            side = "down"
            ratio = 1.0 - up_ratio
        else:
            return None  # no clear imbalance

        ask = ctx.best_ask(side)
        if ask >= 0.80 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=ratio,
            max_entry_price=0.80,
            reason=f"Bid depth {side} {ratio:.0%} ({ctx.up_bid_depth:.0f}:{ctx.down_bid_depth:.0f})",
        )
