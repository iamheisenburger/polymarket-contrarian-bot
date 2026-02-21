"""
S05: Dual-Source Divergence

Signal: Compute FV from both Binance AND Chainlink. When they disagree by
> 3 cents, bet with Chainlink (settlement source). Chainlink FV >= 0.55.
Rationale: Polymarket participants watch Binance. Settlement is Chainlink.
When they diverge, the market is pricing off the wrong source.
Breakeven: ~60.8%.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S05_Dual_Source(ArenaSignal):

    @property
    def name(self) -> str:
        return "S05_DUAL"

    @property
    def description(self) -> str:
        return "Bet with Chainlink when Binance and Chainlink FV diverge > 3c"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        if not ctx.fv_chainlink or not ctx.fv_binance:
            return None

        fv_cl = ctx.fv_chainlink
        fv_bn = ctx.fv_binance

        # Check divergence
        diff = abs(fv_cl.fair_up - fv_bn.fair_up)
        if diff < 0.03:
            return None

        # Bet with Chainlink (settlement source)
        side = fv_cl.dominant_side
        if fv_cl.dominant_prob < 0.55:
            return None

        ask = ctx.best_ask(side)
        if ask >= 0.80 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=fv_cl.dominant_prob,
            max_entry_price=0.80,
            reason=f"CL={fv_cl.fair_up:.2f} vs BN={fv_bn.fair_up:.2f} diff={diff:.2f} -> {side}",
        )
