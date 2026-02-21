"""
S09: Last-Minute Gamma

Signal: Only trade in last 120 seconds before expiry. If B-S FV >= 0.75
AND Binance 30s momentum confirms direction, buy dominant side.
Rationale: Near expiry, gamma is huge — small price moves create big
probability swings. Market may underreact to accelerating convergence.
Breakeven: ~60.8%.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S09_Last_Minute(ArenaSignal):

    @property
    def name(self) -> str:
        return "S09_GAMMA"

    @property
    def description(self) -> str:
        return "Last 120s: FV >= 0.75 + momentum confirms"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        if ctx.seconds_to_expiry > 120:
            return None  # only last 120 seconds
        if ctx.seconds_to_expiry < 10:
            return None  # too close, risky

        fv = ctx.fv_binance
        if not fv:
            return None
        if fv.dominant_prob < 0.75:
            return None

        # Momentum must confirm
        side = fv.dominant_side
        mom = ctx.binance_momentum_30s
        if side == "up" and mom < 0:
            return None
        if side == "down" and mom > 0:
            return None

        ask = ctx.best_ask(side)
        if ask >= 0.90 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=fv.dominant_prob,
            max_entry_price=0.90,
            reason=f"Last {ctx.seconds_to_expiry:.0f}s FV={fv.dominant_prob:.2f} mom={mom*100:+.3f}%",
        )
