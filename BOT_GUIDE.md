# Polymarket Trading Bot - Management Guide

## Strategy Overview

We trade Polymarket crypto binary markets by exploiting the latency between Binance
spot price movements and Polymarket orderbook repricing. Black-Scholes calculates
the true probability; when the market price lags behind, we buy the mispriced side
and hold to settlement ($1.00 on win, $0.00 on loss).

### Active: Precision Sniper v2 (BTC 5m)
- **BTC only, 5-minute markets** — 288 market windows per day
- **Any price range** — entry up to $0.85 (trades cheap AND midrange sides)
- **Three entry filters** working together:
  1. **Edge ≥ 15%** — fair_value minus buy_price must be at least $0.15
  2. **Volatility < 0.50** — realized vol from Binance below 0.50
  3. **Momentum confirmation** — Binance price moved ≥ 0.05% in trade direction (30s)
- Breakeven WR: ~21% (varies by entry price)
- Trade log: `data/v2_trades.csv`

### Why These Filters (from 249-trade Longshot analysis)
| Filter | Evidence |
|--------|----------|
| Vol < 0.50 | 35.8% WR on 106 trades vs 22.4% when vol > 0.50 |
| Vol < 0.40 | 44.4% WR on 54 trades |
| Vol > 0.70 | 22.5% WR — barely breakeven, should NEVER trade |
| Short TTE (<3 min) | 40.4% WR on 57 trades |
| Win clustering | 38.4% WR after a win vs 25.7% after a loss |

### Binary Payoff Math
- Breakeven win rate = entry price (e.g., $0.21 entry → 21% breakeven)
- Edge comes from Binance moving in ms while Polymarket reprices in seconds
- Kelly Criterion sizes bets; currently locked to min-size (5 tokens) for data collection
- Hold to settlement — no early exits

## V2 Growth Plan

1. **[CURRENT]** Collect 50 trades at min-size (data collection)
2. If WR > 30% at 50 trades → half-Kelly sizing
3. If WR > 30% at 100 trades → 3/4 Kelly sizing
4. If WR < 21% at 50 trades → STOP, review filters

**Rules during data collection:** No strategy changes. No parameter tweaking. Let
the data come in. Losing streaks are mathematically expected. Quiet periods mean
no edge is available, not a bug.

### Retired: Longshot v1 (Feb 17-19, 249 trades)
- BTC/ETH/SOL/XRP on 15m markets, entry < $0.20
- Overall 29.7% WR but decayed from 36.8% pre-peak to 22.7% post-peak
- No volatility filter, no momentum filter — traded too often with weak signals
- Trade log archived: `data/longshot_trades.csv`

## Server Details
- **Provider:** DigitalOcean
- **Region:** Amsterdam (AMS3) — must be non-US
- **IP:** 209.38.36.107
- **OS:** Ubuntu 24.04 LTS
- **Plan:** $6/month (1GB RAM, 1 vCPU)
- **SSH Key:** `~/.ssh/polymarket_bot`

## Data Integrity & Baseline

### Single source of truth: `data/longshot_trades.csv`
- Contains ONLY min-size trades (5 tokens, entry ~$0.21)
- Every outcome is settled on-chain via Chainlink oracle — wins and losses are factual
- To judge longshot profitability: **use win rate vs 21% breakeven**, not account balance

### Phantom Trades Bug (fixed 2026-02-17)
- **Bug:** GTC orders accepted by CLOB (`success=True`) were logged as filled trades
  even when they sat on the orderbook unfilled. This inflated PnL by ~$15-20.
- **Fix:** Switched to FOK (Fill Or Kill) orders — fills instantly or is cancelled.
  Bot now verifies order status before tracking position (rejects LIVE/OPEN status).
- **Code:** `strategies/momentum_sniper.py` — order_type="FOK" + fill verification

### Baseline (restored 2026-02-17)
- **40 verified trades restored** from the archive (trades that had USDC balance
  confirmation, bankroll-USDC gap = $0.00 at end of period).
- First 62 trades from original log were discarded — no USDC balance data to verify,
  plus contaminated by pre-min-size settlements and phantom trades.
- All new trades use FOK orders — verified fills only.
- Integrity check: bankroll vs on-chain USDC gap (not PnL sum, which fails mid-stream).

### Archived files (in `data/`)
- `archive_pre_minsize_trades.csv` — old pre-min-size era trades (do not use)
- `archive_sniper_trades_old_logger.csv` — old logger duplicate (do not use)
- `archive_longshot_phantom_trades.csv` — contaminated log with phantom fills (do not use)
- `trades.csv` — old contrarian log (retired)

## Quick Commands

### Run v2 stats (THE command for checking performance)
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "python3 /opt/polymarket-bot/scripts/v2_stats.py"
```

### Run old longshot stats (archived, for reference)
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "python3 /opt/polymarket-bot/scripts/longshot_stats.py"
```

### Check running processes
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "ps aux | grep python | grep -v grep"
```

### View sniper logs (last 50 lines)
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "tail -50 /var/log/polymarket-sniper.log"
```

### View raw longshot trade log
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "cat /opt/polymarket-bot/data/longshot_trades.csv"
```

### Check for errors
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "grep -iE 'error|failed' /var/log/polymarket-sniper.log | tail -10"
```

### Update bot code (after pushing to GitHub)
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "cd /opt/polymarket-bot && git pull"
```

### Check server resources
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "free -h && echo '---' && df -h / && echo '---' && uptime"
```

## Active Processes on VPS

1. **Precision Sniper v2** (main bot):
   ```bash
   python apps/run_sniper.py --coins BTC --timeframe 5m --bankroll 35 --min-edge 0.15 --min-size --max-entry-price 0.85 --max-vol 0.50 --min-momentum 0.0005 --momentum-lookback 30 --log-file data/v2_trades.csv
   ```
   Log: `/var/log/polymarket-sniper.log`

2. **Lag collector** (research data):
   `scripts/lag_collector.py` (screen session)

## Architecture

```
Project Root/
├── apps/
│   ├── run_sniper.py          # CLI entry point for Longshot/Midshot
│   ├── run_market_maker.py    # Market maker entry point
│   ├── run_contrarian.py      # Old contrarian (retired)
│   └── run_flash_crash.py     # Flash crash (unused)
├── strategies/
│   ├── momentum_sniper.py     # Core strategy for Longshot & Midshot
│   ├── market_maker.py        # Market maker strategy
│   ├── contrarian.py          # Old contrarian (retired)
│   └── flash_crash.py         # Flash crash (unused)
├── src/
│   ├── bot.py                 # Order execution, balance, auto-redemption
│   ├── client.py              # Polymarket API client
│   ├── signer.py              # EIP-712 order signing
│   ├── websocket_client.py    # Real-time orderbook feeds
│   └── gamma_client.py        # Market discovery
├── lib/
│   ├── fair_value.py          # Black-Scholes binary pricing
│   ├── binance_ws.py          # Binance real-time price feed
│   ├── trade_logger.py        # CSV trade logging
│   ├── market_manager.py      # Market lifecycle management
│   └── console.py             # Terminal UI
├── data/
│   ├── v2_trades.csv          # V2 trade log (ONLY source of truth)
│   ├── longshot_trades.csv    # Longshot v1 log (archived)
│   ├── longshot_trades.pending.json  # Pending trade tracker
│   ├── archive_*.csv          # Old files, do not use
│   └── trades.csv             # Old contrarian log (retired)
└── scripts/
    ├── v2_stats.py             # V2 stats — run this to check performance
    ├── longshot_stats.py       # Old longshot stats (for reference)
    ├── lag_collector.py        # Binance vs Polymarket lag research
    ├── analyze_top_traders.py  # Leaderboard wallet analysis
    └── scan_daily_crypto.py    # Daily crypto market scanner
```

## Credentials
- **GitHub repo:** https://github.com/iamheisenburger/polymarket-contrarian-bot
- **Wallet EOA:** 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- **Safe address:** 0x13D0684C532be5323662e851a1Bd10DF46d79806
- **ENV file on server:** /opt/polymarket-bot/.env

## Statistical Validation

V2 needs 50 trades before conclusions:
- Compare observed WR to ~21% breakeven WR
- If WR > 30% at 50 trades → half-Kelly sizing
- If WR > 30% at 100 trades → 3/4 Kelly sizing
- If WR < 21% at 50 trades → STOP

**Ground truth is always `data/v2_trades.csv`.** Not the terminal display, not gut feel.

### V2 Decision Checkpoints
| Checkpoint | WR > 35% | WR 25-35% | WR 21-25% | WR < 21% |
|------------|----------|-----------|-----------|----------|
| 50 trades | Half-Kelly | Stay min-size | Stay min-size, review | **Stop** |
| 100 trades | 3/4 Kelly | Half-Kelly | Review filters | **Stop** |

### Active Run Command (v2)
```bash
python apps/run_sniper.py --coins BTC --timeframe 5m --bankroll 35 --min-edge 0.15 --min-size --max-entry-price 0.85 --max-vol 0.50 --min-momentum 0.0005 --momentum-lookback 30 --log-file data/v2_trades.csv
```

## Longshot v1 Results (archived)

### Step 2 Results (evaluated at 246 trades, 2026-02-19)
| Coin | Trades | WR | Decision |
|------|--------|-----|----------|
| BTC | 56 | 35.7% | Strong edge |
| ETH | 75 | 29.3% | Thin edge |
| SOL | 65 | 29.2% | Thin edge |
| XRP | 50 | 24.0% | Below breakeven |

**Post-mortem:** Overall 29.7% WR masked a pre-peak 36.8% → post-peak 22.7% decay.
No volatility or momentum filters meant the bot traded every signal regardless of
conditions, including high-vol noise periods that destroyed gains.

## Important Notes
- Server MUST be in a non-US, non-blocked region (Amsterdam works)
- The sniper process is NOT managed by systemd — it runs directly
- Polymarket uses Chainlink BTC/USD for settlement (not Binance/spot directly)
- USDC has 6 decimal places; Polymarket minimum order is $1.00 / 5 tokens
