"""
S01: XRP Specialist

Signal: Black-Scholes FV >= 0.60, edge >= 0.05, XRP only.
Rationale: XRP showed 78.9% WR on 19 verified trades — the only profitable
coin in Oracle. Drop the 3 losers, trade XRP with looser filters.
Breakeven: ~60.8% at typical entry ~$0.62.
"""

from typing import Optional, List
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S01_XRP_Specialist(ArenaSignal):

    @property
    def name(self) -> str:
        return "S01_XRP"

    @property
    def description(self) -> str:
        return "XRP-only directional (FV>=0.60, edge>=0.05)"

    @property
    def coins(self) -> List[str]:
        return ["XRP"]

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        fv = ctx.fv_binance
        if not fv:
            return None

        dominant = fv.dominant_side
        prob = fv.dominant_prob

        if prob < 0.60:
            return None

        ask = ctx.best_ask(dominant)
        fee = ctx.fee_for_price(ask)
        edge = prob - ask - fee

        if edge < 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=dominant,
            confidence=prob,
            max_entry_price=0.85,
            reason=f"XRP FV={prob:.2f} ask={ask:.2f} edge={edge:.2f}",
        )
