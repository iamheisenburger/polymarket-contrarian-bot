"""
S08: Consensus (Multi-Signal)

Signal: Only trade when 3+ of 4 sub-signals agree on direction:
(a) B-S FV >= 0.60
(b) Binance 60s momentum confirms
(c) Orderbook bid depth > 55% on that side
(d) Spread on that side < 5 cents
Maximum conviction trades. Fewer trades, higher expected WR.
Breakeven: ~60.8%. Target WR: 70%+.
"""

from typing import Optional
from strategies.paper_arena import ArenaSignal, MarketContext, TradeSignal


class S08_Consensus(ArenaSignal):

    @property
    def name(self) -> str:
        return "S08_CONS"

    @property
    def description(self) -> str:
        return "Multi-signal consensus (3/4 must agree)"

    def evaluate(self, ctx: MarketContext) -> Optional[TradeSignal]:
        fv = ctx.fv_binance
        if not fv:
            return None

        votes_up = 0
        votes_down = 0

        # (a) B-S FV >= 0.60
        if fv.fair_up >= 0.60:
            votes_up += 1
        elif fv.fair_down >= 0.60:
            votes_down += 1

        # (b) Binance 60s momentum
        if ctx.binance_momentum_60s > 0.0002:
            votes_up += 1
        elif ctx.binance_momentum_60s < -0.0002:
            votes_down += 1

        # (c) Orderbook depth
        total_depth = ctx.up_bid_depth + ctx.down_bid_depth
        if total_depth > 5:
            up_ratio = ctx.up_bid_depth / total_depth
            if up_ratio > 0.55:
                votes_up += 1
            elif up_ratio < 0.45:
                votes_down += 1

        # (d) Spread tightness — tighter side gets a vote
        if 0 < ctx.up_spread < 0.05 and (ctx.down_spread <= 0 or ctx.up_spread < ctx.down_spread):
            votes_up += 1
        elif 0 < ctx.down_spread < 0.05 and (ctx.up_spread <= 0 or ctx.down_spread < ctx.up_spread):
            votes_down += 1

        # Need 3+ in same direction
        if votes_up >= 3:
            side = "up"
            votes = votes_up
        elif votes_down >= 3:
            side = "down"
            votes = votes_down
        else:
            return None

        ask = ctx.best_ask(side)
        if ask >= 0.80 or ask <= 0.05:
            return None

        return TradeSignal(
            strategy_name=self.name,
            side=side,
            confidence=votes / 4.0,
            max_entry_price=0.80,
            reason=f"Consensus {votes}/4 -> {side}",
        )
