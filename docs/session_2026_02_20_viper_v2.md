# Session: Viper v1 Post-Mortem & v2 Launch — Feb 20, 2026

## What Happened

Woke up to $0.09 balance. Viper v1 ran overnight: 50 trades, 25W/25L (50% WR), $44 -> $0.

## Three Mistakes That Killed the Account

### Mistake 1: FOK Bug — The $94 Invisible Drain
The code said `order_type="FOK"` but the API never received it. In `src/bot.py`, the `order_type` parameter was accepted by `place_order()` but never passed to `PartialCreateOrderOptions`. Every order went as GTC (Good Till Cancel) instead of FOK (Fill Or Kill).

When a trade didn't fill instantly, the bot logged "didn't fill, moving on" — but the GTC order stayed live on Polymarket's orderbook. When price eventually hit that level, the order filled, spending real USDC. 45 ghost orders executed for $94.11 total. The bot had no idea.

**Result:** CSV showed +$39 profit. On-chain reality: -$28 loss.

**Fix:** Added `OrderType.FOK` to `PartialCreateOrderOptions` in `src/bot.py`. Commit `b0337e8`.

### Mistake 2: No Entry Price Floor — The 13.6% WR Death Zone
22 trades entered below $0.40. Black-Scholes said "30% chance of Up" but reality was ~14%. The "edge" looked real on paper (5+ cents gap) but was phantom — the model is hypersensitive to volatility estimation errors at low probabilities.

**Result:** 3W/19L at entry < $0.40, losing $16.54.

**Why the model fails at low probabilities:** Black-Scholes calculates fair value from distance-to-strike and volatility. The volatility estimate (from ~15min of Binance tick data) is backward-looking and never perfectly accurate. Far from the strike (low probability zone), a tiny vol error (e.g., 35% vs 40%) can swing the output from 30% to 15%. Near the strike (high probability zone), the same error barely moves the output (72% vs 69%).

The model isn't wrong — it's doing the math correctly. But the accuracy of its OUTPUT depends on the accuracy of its INPUTS, and that sensitivity is asymmetric. Low probabilities amplify input noise. High probabilities absorb it.

**Fix:** Added `--min-entry-price 0.40` CLI flag. Commit `a71b6b9`.

### Mistake 3: Two Instances on One Wallet — The Compounding Error
Both the 5m and 15m bots shared the same Polymarket wallet. On-chain balance only refreshes every ~10 seconds. Both bots would read the same stale balance, both decide to bet, and both spend money the other already spent. Kelly sizing was based on fantasy numbers.

When 15m went on its 0-for-9 losing streak overnight, it was betting money the 5m bot had already spent. The account hit $0 faster than either bot's math predicted.

**Fix:** Single instance only. One bot, one wallet, no conflicts.

## Cross-Reference Investigation

Bot CSV only captured 50 trades ($184 spent). On-chain Polymarket history showed $281 spent — 45 ghost trades ($94.11) completely invisible to the bot.

| | Bot CSV | On-chain Reality |
|---|---|---|
| Total spent | $184.21 | $280.91 |
| Total redeemed | $223.69 | $252.60 |
| Net PnL | +$39.48 | -$28.30 |

The CSV was incomplete, not wrong. The 50 logged trades' win/loss outcomes were accurate. But 45 ghost GTC trades were draining money invisibly.

## Entry Price Analysis (from 50 logged trades)

| Entry Price | Trades | W/L | Win Rate | PnL |
|-------------|--------|-----|----------|-----|
| >= $0.40 | 28 | 22W/6L | **78.6%** | +$56.02 |
| < $0.40 | 22 | 3W/19L | **13.6%** | -$16.54 |

By bucket:

| Bucket | Combined | WR |
|--------|----------|-----|
| $0.02-$0.19 | 2W/13L | 13.3% |
| $0.20-$0.39 | 1W/6L | 14.3% |
| $0.40-$0.59 | 8W/5L | 61.5% |
| $0.60-$0.85 | 14W/1L | 93.3% |

Entry price IS the model confidence signal. The model is accurate above $0.40 and unreliable below.

## Viper v2 — What Changed

| | Viper v1 | Viper v2 |
|---|---|---|
| Order type | GTC (broken) | FOK (fixed) |
| Ghost trades | 45 unlogged ($94) | Impossible |
| CSV accuracy | ~65% of reality | 100% |
| Entry range | $0.02 - $0.85 | $0.40 - $0.85 |
| Instances | 2 (wallet conflicts) | 1 (clean) |
| Blocked hours | Yes (irrational) | No (entry price handles it) |

## Viper v2 Configuration

```
--coins BTC ETH --timeframe 15m --bankroll 20
--min-edge 0.05 --min-entry-price 0.40 --max-entry-price 0.85
--max-vol 0.50 --min-momentum 0.0005 --momentum-lookback 30
--kelly 0.50 --kelly-strong 0.75 --max-bet-fraction 0.15
--log-file data/viper_15m.csv
```

## EV Estimates

Breakeven WR = average entry price. At avg entry ~$0.55, breakeven is 55%.

Expected WR: 65-75% (78.6% from v1 sample will likely regress).

| Bankroll | Bet Size | EV/Trade | Trades/Day | EV/Day |
|----------|----------|----------|------------|--------|
| $21 | $3.15 | $0.47 | ~15 | ~$7 |
| $50 | $7.50 | $1.13 | ~15 | ~$17 |
| $100 | $15.00 | $2.25 | ~15 | ~$34 |

## Roadmap to Max EV/Day

1. **Now:** Validate v2 with 20 trades on 15m BTC/ETH
2. **After validation:** Add 5m back as multi-timeframe scanner in SAME process (one wallet read, no conflicts)
3. **After 50 trades:** Add SOL/XRP if BTC/ETH fills are clean
4. **At $100+ bankroll:** Making $30+/day compounding

## Monitoring Checkpoints

| Trades | WR > 65% | WR 55-65% | WR < 55% |
|--------|----------|-----------|----------|
| 20 | On track | Review | Investigate |
| 50 | Scale up | Stay course | Tighten to $0.50 |

## Future Improvement: Deribit Implied Volatility

**Problem:** Current volatility estimate is backward-looking (15min of Binance tick data standard deviation). This is the root cause of model inaccuracy at low probabilities.

**Solution:** Pull implied volatility from Deribit options exchange API. This is forward-looking — represents what the entire options market thinks vol will be.

**Impact:**
- More accurate fair values across ALL probability ranges
- Potentially unlocks the $0.30-$0.40 zone that's currently toxic
- More qualifying trades per day = higher EV/day
- Better Kelly sizing even in the $0.40-$0.85 range

**Timeline:** Phase 2 — after v2 is validated with 20-50 trades.

## Evolution: Longshot -> Viper v1 -> Viper v2

| | Longshot v1 | Viper v1 | Viper v2 |
|---|---|---|---|
| Dates | Feb 17-19 | Feb 19-20 | Feb 20+ |
| Trades | 249 | 50 | Starting now |
| Coins | BTC/ETH/SOL/XRP | BTC/ETH | BTC/ETH |
| Timeframes | 15m | 5m + 15m | 15m only |
| Instances | 1 | 2 | 1 |
| Entry range | $0.02-$0.20 | $0.02-$0.85 | $0.40-$0.85 |
| Order type | GTC (broken) | GTC (broken) | FOK (fixed) |
| Ghost trades | Yes | Yes (45/$94) | Impossible |
| Filters | None | Vol + momentum + hours | Vol + momentum + entry price |
| CSV accurate | No | No (~65%) | Yes (100%) |
| Result | $44->$87->$36 | $44->$0 | TBD |

**Lessons learned at each stage:**
- **Longshot:** SOL/XRP are noise, cheap entries lose, no filters = gambling
- **Viper v1:** Model works above $0.40 (78.6% WR), fails below (13.6% WR), FOK bug silently draining account
- **Viper v2:** Apply everything — only trade when model is confident, only spend money we can track, one instance

Three versions, 299 trades of data, two blown accounts. Every lesson baked into the code.

## Why Blocking Hours Was Wrong

"Blocking trading windows is the most illogical thing to do. How many hours are we gonna block before realising that ALL windows make us lose money and maybe this is a strategy problem?"

The hour pattern was a SYMPTOM of cheap entries, not a cause:
- During hours 01-07 UTC there were zero trades with entry >= $0.40
- Every cheap entry lost (0/9 on 15m overnight)
- Thin overnight markets -> low model confidence -> low entry prices -> losses
- If a $0.55 entry appeared at 3am, we SHOULD take it

Fix the entry price filter and the hour problem disappears automatically.
