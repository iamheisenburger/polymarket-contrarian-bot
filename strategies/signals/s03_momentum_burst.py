"""
S03: Momentum Burst

Signal: Binance price moved > 0.12% in last 60 seconds. Bet on continuation.
No Black-Scholes — pure momentum signal.
Rationale: Short-term crypto momentum is real. Market prices lag spot moves.
Breakeven: ~60.8% at typical mid-range entry.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S03_Momentum_Burst(ArenaSignal):

    @property
    def name(self) -> str:
        return "S03_MOM"

    @property
    def description(self) -> str:
        return "Pure momentum: Binance 60s move > 0.12%"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        mom = ctx.binance_momentum_60s

        if abs(mom) < 0.0012:  # 0.12% threshold
            return None

        side = "up" if mom > 0 else "down"
        ask = ctx.best_ask(side)

        if ask >= 0.75 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=min(0.9, 0.5 + abs(mom) * 100),
            max_entry_price=0.75,
            reason=f"Momentum {mom*100:+.3f}% in 60s -> {side}",
        )
