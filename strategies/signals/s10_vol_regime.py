"""
S10: Volatility Regime

Signal: Check realized vol.
- Low vol (< 30%): Buy dominant side (FV >= 0.55). Quiet market holds.
- High vol (> 70%): Buy cheap contrarian side (ask < $0.30). Wild = good odds.
- Medium vol (30-70%): Skip.
Breakeven: 25% for high-vol contrarian, 60.8% for low-vol directional.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S10_Vol_Regime(ArenaSignal):

    @property
    def name(self) -> str:
        return "S10_VOL"

    @property
    def description(self) -> str:
        return "Low vol: trend, high vol: contrarian"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        vol = ctx.binance_volatility
        fv = ctx.fv_binance
        if not fv:
            return None

        if vol < 0.30:
            # LOW VOL: buy dominant side (trend holds)
            side = fv.dominant_side
            if fv.dominant_prob < 0.55:
                return None

            ask = ctx.best_ask(side)
            if ask >= 0.80 or ask <= 0.05:
                return None

            return TradeSignal(
                strategy_name=self.name,
                side=side,
                confidence=fv.dominant_prob,
                max_entry_price=0.80,
                reason=f"Low vol={vol:.0%} -> {side} FV={fv.dominant_prob:.2f}",
            )

        elif vol > 0.70:
            # HIGH VOL: buy cheap contrarian side
            cheap_side = "down" if fv.dominant_side == "up" else "up"
            ask = ctx.best_ask(cheap_side)

            if ask >= 0.30 or ask <= 0.02:
                return None

            return TradeSignal(
                strategy_name=self.name,
                side=cheap_side,
                confidence=1.0 - fv.dominant_prob,
                max_entry_price=0.30,
                reason=f"High vol={vol:.0%} -> contrarian {cheap_side} @{ask:.2f}",
            )

        return None  # Medium vol: skip
