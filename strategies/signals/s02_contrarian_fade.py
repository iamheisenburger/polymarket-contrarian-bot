"""
S02: Contrarian Fade

Signal: When B-S FV >= 0.80 (model very confident), buy the CHEAP opposite side
at ask < $0.30. Exploits systematic model overconfidence.
Rationale: Model says 85%+ but actual WR is ~58%. If true P(UP) is 58%,
then P(DOWN) = 42%. At $0.25 entry, breakeven is 25%. 42% >> 25%.
Breakeven: ~25% at $0.25 entry (4:1 payout).
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S02_Contrarian_Fade(ArenaSignal):

    @property
    def name(self) -> str:
        return "S02_FADE"

    @property
    def description(self) -> str:
        return "Buy cheap side when model overconfident (FV>=0.80)"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        fv = ctx.fv_binance
        if not fv:
            return None

        if fv.dominant_prob < 0.80:
            return None

        # Buy the opposite (cheap) side
        cheap_side = "down" if fv.dominant_side == "up" else "up"
        ask = ctx.best_ask(cheap_side)

        if ask >= 0.30:
            return None
        if ask <= 0.02:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=cheap_side,
            confidence=1.0 - fv.dominant_prob,
            max_entry_price=0.30,
            reason=f"Model={fv.dominant_prob:.0%} overconfident -> buy {cheap_side} @{ask:.2f}",
        )
