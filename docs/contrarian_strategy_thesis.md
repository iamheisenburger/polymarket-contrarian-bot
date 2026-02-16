# Contrarian Cheap-Side Strategy - Full Thesis

## DO NOT MODIFY THIS FILE
This documents the live trading strategy. Any changes to parameters or logic
should be discussed and tested before touching the code.

---

## Origin

Based on research analyzing **684 Polymarket short-duration crypto binary markets**.
The original article demonstrated that buying the cheap side of these markets
(the side trading at $0.03-$0.07) yields a positive expected value because the
crowd systematically overprices the favorite side.

## Core Concept

Polymarket offers 5-minute and 15-minute binary markets on whether BTC (and other
crypto) will go up or down. These markets resolve to $1.00 (winner) or $0.00 (loser).

When one side trades at $0.05, the market is saying there's a 5% chance that side
wins. If the **actual** probability is higher (research shows ~8-15%), buying that
cheap side is profitable over hundreds of trades.

## The Math

| Entry Price | Payout Multiple | Breakeven Win Rate | Expected Win Rate | Edge |
|-------------|-----------------|--------------------|--------------------|------|
| $0.03 | 33x | 3.0% | ~8-15% | +5-12% |
| $0.04 | 25x | 4.0% | ~8-15% | +4-11% |
| $0.05 | 20x | 5.0% | ~8-15% | +3-10% |
| $0.06 | 17x | 5.9% | ~8-15% | +2-9% |
| $0.07 | 14x | 7.0% | ~8-15% | +1-8% |

**Key insight**: You lose most trades. But when you win, you win 14-33x your bet.
The strategy is profitable because the win rate exceeds the breakeven threshold.

## Parameters (Live)

```
Coin:               BTC
Timeframe:          5-minute markets
Entry price range:  $0.03 - $0.07
Bet sizing:         Kelly Criterion (half Kelly)
Minimum bet:        $1.00 (Polymarket platform minimum)
Max bet:            Kelly-calculated (scales with bankroll)
Daily loss limit:   NONE (removed - let Kelly manage risk)
Max trades/hour:    10
Hold strategy:      TO SETTLEMENT (never sell early)
TP/SL:              NONE (hold to expiry for full $1.00 payout)
```

## Why No Early Exit

The entire edge depends on holding to settlement. Selling early destroys the
asymmetric payoff structure:

- If you buy at $0.05 and sell at $0.08, you made $0.03 profit
- If you hold to settlement and win, you make $0.95 profit (19x more)
- The strategy NEEDS those full $1.00 payouts to overcome the ~88% loss rate

## Kelly Criterion Sizing

Position sizes are calculated using the Kelly Criterion to optimize long-term
growth while managing ruin risk:

```
Kelly fraction F = 0.5 * (estimated_win_rate - entry_price) / (1 - entry_price)
Bet size = F * current_bankroll
Minimum bet = $1.00 (Polymarket floor)
```

- We use **half Kelly** (0.5 fraction) for safety
- At bankrolls below ~$50, Kelly calculates less than $1, so the $1 minimum binds
- Above $50, Kelly starts truly scaling bet sizes with bankroll
- No artificial caps beyond Kelly + platform minimum

## Trade Execution

1. Bot monitors BTC 5-minute binary markets via WebSocket
2. Every tick, checks if either side (UP or DOWN) is in the $0.03-$0.07 range
3. If yes: places a market BUY order for that side
4. Holds position until the 5-minute market expires and settles
5. On settlement: winning tokens pay $1.00 each, losing tokens pay $0.00
6. Auto-redemption converts winning tokens back to USDC for the next trade

## What the Bot Does NOT Do

- Does NOT predict BTC direction (buys whichever side is cheap)
- Does NOT use technical analysis or indicators
- Does NOT set take-profit or stop-loss orders
- Does NOT sell positions early
- Does NOT have a daily loss limit (Kelly manages risk)

## Early Results (First 56 Trades)

*As of 2026-02-15, ~24 hours of live trading:*

| Metric | Value |
|--------|-------|
| Total trades | 56 |
| Resolved trades | 25 |
| Wins | 3 |
| Losses | 22 |
| Win rate | **12.0%** |
| Total wagered | $53.77 |
| Total payout | $45.25 |
| Net PnL | **+$20.25** |
| Portfolio change | ~$25 -> ~$45 (nearly doubled) |

**Win rate by entry price:**
- $0.04: 0/2 (0%) - too few trades
- $0.05: 0/13 (0%) - too few trades
- $0.06: 1/10 (10%)
- $0.07: 2/31 (6%)

**IMPORTANT**: 56 trades is far too small a sample size to draw conclusions.
Need 200-500+ trades for statistical significance. Early results are encouraging
but could easily be variance.

## Risk Acknowledgment

- This is a high-variance strategy. Long losing streaks are normal and expected.
- With an 88% loss rate, streaks of 10-20 consecutive losses will happen regularly.
- A single win at 14-20x covers many losses, but you need bankroll to survive the streaks.
- Kelly Criterion sizing is the primary risk management mechanism.
- The bot was inactive for 7 hours during initial testing because winning tokens
  weren't auto-redeemed, draining the bankroll. Auto-redemption is critical.

## Statistical Validation Plan

Before making ANY changes to the strategy:
1. Collect 200+ resolved trades
2. Calculate actual win rate with confidence interval
3. Compare to breakeven thresholds at each entry price
4. Only then consider parameter adjustments

## File Locations

| File | Purpose |
|------|---------|
| `strategies/contrarian.py` | Core strategy logic + Kelly sizing |
| `apps/run_contrarian.py` | CLI entry point with all parameters |
| `src/bot.py` | Order execution + auto-redemption |
| `lib/trade_logger.py` | CSV trade logging with outcomes |
| `data/trades.csv` | Trade history (also on server) |

## Original Research Reference

The strategy is derived from analysis of 684 Polymarket short-duration crypto
binary markets. Key findings from the research:

- Cheap sides ($0.03-$0.07) win more often than the market prices imply
- The crowd systematically overestimates the probability of the "obvious" outcome
- Buying the contrarian side and holding to settlement yields positive EV
- The effect is strongest in volatile market conditions
- 5-minute and 15-minute markets both exhibit this pattern
