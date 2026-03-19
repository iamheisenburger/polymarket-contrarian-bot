# Session Context — Read This First

## What This Project Is

A Polymarket crypto binary trading bot that buys UP or DOWN on 5-minute markets (BTC, ETH, SOL, XRP, DOGE, BNB). It exploits the lag between Binance (real-time prices) and Polymarket (slower to reprice).

## Current Status (March 19 2026)

**IT'S WORKING. DON'T CHANGE THE STRATEGY.**

- Balance: ~$23.77 USDC (deposited $18.39, up +$5.38)
- Live results: 5 wins, 0 losses, 100% WR
- Shadow (paper) results: 8 signals, 8 wins (3 were FOK rejects that couldn't fill but would have won)
- Running on VPS: 134.209.91.109

## The Strategy (Direct FV / Cheap Entry Framework)

We reverse-engineered the approach used by the famous $313→$438K Polymarket bot (0x8dxd). The strategy:

1. Watch Binance for strong price moves
2. When the price has moved significantly from strike (gap >0.05%), the outcome is nearly decided
3. But Polymarket asks are still cheap ($0.40-0.60) because the market hasn't caught up
4. Buy the cheap side, wait for 5-minute settlement, collect $1.00 per token

**Key parameters (DO NOT CHANGE):**
- Entry price: $0.40-0.60 (cheap entries = low breakeven ~53%)
- Min edge: 0.15 (fair value - entry price)
- Min momentum: 0.0005 (strong moves only, not noise)
- Min fair value: 0.75
- TTE window: 120-180 seconds into the 5-minute market
- EMA trend: (4,16) — only trade in trend direction
- Direct FV model (NOT Black-Scholes — Black-Scholes was proven wrong, see below)
- FOK tolerance: +$0.02 (pay up to 2 cents more to ensure fill)
- Min depth: 4 tokens
- Circuit breaker: 3 consecutive losses = 1hr pause, 5/10 losses = full stop

## Why This Strategy Works (and what failed before)

### What Failed ($180+ lost)
- **Black-Scholes model** said 90% confidence on tiny price gaps (<0.05%). Real WR at small gaps = 45-54%. It was calling coin flips "sure things."
- **Filter optimization** swept thousands of combos on the same dataset. Found 84% WR configs that were just overfitting noise (2.9% of random filters produce 84% by chance on a 62% base rate dataset).
- **Expensive entries** ($0.65-0.80) had breakeven WR of 68-78%. Even a "good" 70% WR lost money. Risk $3.50 to win $1.50.
- **Every deployment** showed great paper WR (80%+) then collapsed to 50-60% live. Shadow logging proved this wasn't execution degradation — paper was wrong too.

### What Works Now
- **Direct FV** uses actual price gap instead of theoretical model. Calibrated from 1,457 real trades.
- **Cheap entries** ($0.40-0.60) have breakeven at ~53%. WR only needs to be 54% to profit. Wins and losses are nearly equal size (~$2.50 each).
- **Large gaps only** (>0.05% from strike). No more betting on tiny movements that are actually noise.
- **Shadow logging** confirmed ZERO paper-to-live degradation. Paper and live agree on every single trade.

## VPS Access

```bash
# SSH
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109

# Check status
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 "strings /var/log/live-v3.log | grep -E 'Balance:|Trades:' | tail -2"

# Check trades
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 "cat /opt/polymarket-bot/data/live_v3.csv"

# Check shadow (paper vs live matched pairs)
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 "cat /opt/polymarket-bot/data/shadow_v3.csv"

# Check if bot running
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 "ps aux | grep run_sniper | grep -v grep"

# Kill bot
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 "killall -9 python3"

# Check real USDC balance
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 'cd /opt/polymarket-bot && export $(grep -v "^#" .env | xargs) && /opt/polymarket-bot/venv/bin/python3 -c "from src.bot import TradingBot; from src.config import Config; import os; config=Config.from_env(); bot=TradingBot(config=config, private_key=os.environ[\"POLY_PRIVATE_KEY\"]); print(f\"USDC: {bot.get_usdc_balance():.2f}\")"'
```

## Live Bot Command (what's running now)

```bash
python apps/run_sniper.py \
    --coins ETH BTC XRP SOL DOGE BNB \
    --timeframe 5m \
    --bankroll 23 \
    --min-edge 0.15 \
    --min-entry-price 0.40 \
    --max-entry-price 0.60 \
    --min-momentum 0.0005 \
    --min-fair-value 0.75 \
    --min-window-elapsed 120 \
    --max-window-elapsed 180 \
    --fixed-vol 0.15 \
    --min-size \
    --side trend \
    --ema-fast 4 --ema-slow 16 \
    --block-weekends \
    --enable-cusum --cusum-target-wr 0.60 \
    --adaptive-kelly \
    --market-check-interval 10 \
    --signal-log-dir data/signals \
    --shadow-log data/shadow_v3.csv \
    --enable-circuit-breaker \
    --use-direct-fv \
    --direct-fv-calibration data/calibration.json \
    --fok-tolerance 0.02 \
    --log-file data/live_v3.csv
```

## Key Files

| File | Purpose |
|------|---------|
| `strategies/momentum_sniper.py` | Main strategy — signal detection, filters, execution |
| `lib/direct_fv.py` | Direct fair value calculator (replaces Black-Scholes) |
| `lib/fair_value.py` | OLD Black-Scholes — DO NOT USE |
| `lib/shadow_logger.py` | Records paper vs live on same market for degradation measurement |
| `lib/coin_manager.py` | Promotes/demotes coins between live and paper |
| `lib/circuit_breaker` | Built into momentum_sniper.py — 3 losses = pause |
| `apps/run_sniper.py` | CLI entry point |
| `apps/calibrate_fv.py` | Calibrate direct FV from trade data |
| `data/live_v3.csv` | Current live trade log |
| `data/shadow_v3.csv` | Shadow matched pairs (paper vs live) |
| `data/calibration.json` | Direct FV calibration parameters |
| `data/paper_collector.csv` | 1,457 historical paper trades |

## Cron Jobs on VPS

```
*/5 * * * * /opt/polymarket-bot/scripts/watchdog.sh      # Restarts crashed bots
0 */6 * * * /opt/polymarket-bot/scripts/auto_manage.sh   # Coin management
0 6 * * * /opt/polymarket-bot/scripts/run_discovery.sh    # Strategy discovery
```

## Critical Rules

1. **NEVER go back to Black-Scholes.** It's wrong on 5-minute binaries.
2. **NEVER optimize filters on the same data you test on.** That's overfitting.
3. **NEVER buy expensive ($0.65+).** The risk/reward is terrible.
4. **Always use shadow logging** to verify paper = live.
5. **Circuit breaker stays ON.** 3 consecutive losses = pause.
6. **Think independently.** Don't just agree with the user. Push back when data says otherwise.

## Background Systems (running alongside live bot)

### Paper Collector
NOT currently running — was killed during last restart. Should be restarted to collect loose-filter data on all coins. This feeds the discovery layer and coin manager.

```bash
# Start paper collector (observe mode, loose filters, all coins)
ssh -i ~/.ssh/polymarket_bot root@134.209.91.109 'cd /opt/polymarket-bot && export $(grep -v "^#" .env | xargs) && nohup /opt/polymarket-bot/venv/bin/python3 apps/run_sniper.py --coins BTC ETH SOL XRP DOGE BNB --timeframe 5m --bankroll 10 --min-edge 0.10 --min-entry-price 0.40 --max-entry-price 0.80 --min-momentum 0.0001 --min-fair-value 0.60 --min-window-elapsed 60 --max-window-elapsed 240 --fixed-vol 0.15 --min-size --side trend --ema-fast 4 --ema-slow 16 --market-check-interval 10 --signal-log-dir data/signals --shadow-log data/shadow_paper.csv --log-file data/paper_collector.csv --observe > /var/log/paper-collector.log 2>&1 & echo $! > /var/run/paper-collector.pid && echo "PAPER: $(cat /var/run/paper-collector.pid)"'
```

### Coin Manager (`lib/coin_manager.py`)
- Evaluates which coins qualify for live based on paper WR at fixed filters
- Promotes coins with >75% WR + 20 trades + <5pp degradation
- Demotes coins below 65% WR
- Bankroll-aware: <$5 stop, <$15 one coin, <$30 two coins, $30+ all
- Runs via `scripts/auto_manage.sh` every 6 hours (cron)
- Currently: all 6 coins deployed live because the strategy changed to cheap entries

### Discovery Layer (`lib/discovery.py`)
- Daily sweep of 2,500 filter combos on paper data
- Finds candidates that beat current benchmark
- NEVER auto-deploys — only reports to `data/strategy_candidates.json`
- Runs via `scripts/run_discovery.sh` daily at 6 AM UTC (cron)

### Shadow Logger (`lib/shadow_logger.py`)
- Records paper entry alongside every live trade
- Same market, same signal, same second
- Measures: slippage, adverse selection, fill rate (FOK rejects)
- CRITICAL FINDING: zero degradation confirmed on all matched pairs so far
- Data in `data/shadow_v3.csv`

### Benchmark (`lib/benchmark.py`)
- Tracks live strategy WR over time
- Provisional until 100 trades, then "official"
- Discovery layer must beat benchmark by 5pp to flag a candidate

### Signal Logger (`lib/signal_logger.py`)
- Captures EVERY signal on all coins, pass or fail
- Per-coin CSVs in `data/signals/`
- Feeds discovery layer and future analysis

## Historical Data

- `data/paper_collector.csv` — 1,457+ merged paper trades from all sessions
- `data/live_v3.csv` — current live trades (5W/0L)
- `data/shadow_v3.csv` — shadow matched pairs (8 signals, 5 filled, 3 FOK rejected)
- `data/archive/` — old CSVs from previous failed strategies
- `data/calibration.json` — direct FV calibration parameters (k=900, alpha=0.40)

## What NOT To Do

1. **Don't switch back to Black-Scholes.** We lost $180 on it.
2. **Don't optimize filters by sweeping combos on the dataset.** That's overfitting — 2.9% of random filters show 84% on a 62% base rate.
3. **Don't buy at $0.65+.** Breakeven jumps to 68%+ and risk/reward is terrible.
4. **Don't relax momentum below 0.0005.** Low momentum = noise = coin flips.
5. **Don't trust any paper WR claim without train/test validation.** Paper always looks good. Validate on out-of-sample data.
6. **Don't restart the live bot unless there's a bug.** It's working. Leave it alone.

## Wallet Details

- EOA: 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- Safe: 0x13D0684C532be5323662e851a1Bd10DF46d79806
- Platform: Polymarket (polymarket.com)
- Chain: Polygon
