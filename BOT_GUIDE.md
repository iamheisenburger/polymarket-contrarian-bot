# Polymarket Trading Bot - Management Guide

## Strategy Overview

We trade Polymarket crypto binary markets (BTC/ETH/SOL/XRP Up/Down, 15-minute) by
exploiting the latency between Binance spot price movements and Polymarket orderbook
repricing. Black-Scholes calculates the true probability; when the market price lags
behind, we buy the mispriced side and hold to settlement ($1.00 on win, $0.00 on loss).

Both strategies use the same engine (`strategies/momentum_sniper.py`) but target
different price regimes:

### Strategy 1: LONGSHOT (live)
- **Buys the CHEAP side** — entry price < $0.20 (execution ~$0.21)
- Payout: **4.76x** | Breakeven WR: **21%**
- Per trade: risk $1.05, win $3.95 (5 tokens at $0.21)
- Fires when crypto trends strongly (one side gets pushed cheap)
- Trade log: `data/longshot_trades.csv`

### Strategy 2: MIDSHOT (not yet live)
- **Buys the MIDRANGE side** — entry price ~$0.40-$0.60
- Payout: **~2x** | Breakeven WR: **50%**
- Per trade: risk ~$2.50, win ~$2.50 (5 tokens at ~$0.50)
- Fires when markets are uncertain (near 50/50), higher frequency
- Trade log: `data/midshot_trades.csv` (to be created)

### Binary Payoff Math
- Breakeven win rate = entry price (e.g., $0.21 entry → 21% breakeven)
- Edge comes from Binance moving in ms while Polymarket reprices in seconds
- Kelly Criterion sizes bets; currently locked to min-size (5 tokens) for data collection
- Both strategies hold to settlement — no early exits

## Master Plan (7 Steps)

1. **[CURRENT]** Collect 50 trades/coin on Longshot (BTC/ETH/SOL/XRP = 200 total)
2. If profitable (WR > 21%), remove `--min-size` and let Kelly scale
3. Grow account to fund Midshot campaign
4. Start Midshot at `--min-size` (50 trades/coin data collection)
5. Determine if Midshot is profitable (WR > 50%)
6. If profitable, remove `--min-size` on Midshot too
7. Explore 5m markets for both strategies

**Rules during data collection:** No strategy changes. No parameter tweaking. Let
the data come in. Losing streaks are mathematically expected. Quiet periods mean
no edge is available, not a bug.

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

### Run longshot stats (THE command for checking performance)
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

1. **Longshot sniper** (main bot):
   `apps/run_sniper.py --coins BTC ETH SOL XRP --timeframe 15m --bankroll 25 --min-edge 0.05 --min-size --max-entry-price 0.20`
   Log: `/var/log/polymarket-sniper.log`

2. **Market maker** (observe only, separate from Longshot/Midshot):
   `apps/run_market_maker.py --coin BTC --timeframe 15m --bankroll 19 --observe`
   Log: `/var/log/polymarket-mm.log`

3. **Lag collector** (research data):
   `scripts/lag_collector.py` (screen session)

4. **Old contrarian service**: STOPPED (systemd configured but inactive)

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
│   ├── longshot_trades.csv    # Longshot trade log (ONLY source of truth)
│   ├── longshot_trades.pending.json  # Pending trade tracker
│   ├── mm_trades.csv          # Market maker log (empty, observe only)
│   ├── archive_*.csv          # Old files, do not use
│   └── trades.csv             # Old contrarian log (retired)
└── scripts/
    ├── longshot_stats.py       # Stats script — run this to check performance
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

Each strategy needs 50 trades per coin (200 total) before conclusions:
- **50/coin:** Minimum for directional confidence
- Compare observed WR to breakeven WR
- If WR > breakeven with margin, remove `--min-size` and let Kelly scale
- If WR < breakeven, strategy is unprofitable — stop or retune

**Ground truth is always the CSV trade log.** Not the terminal display, not gut feel.

## Step 2 Decision Framework & Results

### Framework (agreed 2026-02-19)
| Per-Coin WR | Action |
|-------------|--------|
| **Above 30%** | Remove `--min-size`, let Kelly scale |
| **25-30%** | Keep at `--min-size` for another 50 trades |
| **Below 25%** | Drop that coin |

### Results (evaluated at 246 trades, 2026-02-19)
| Coin | Trades | WR | Decision |
|------|--------|-----|----------|
| BTC | 56 | 35.7% | **Kelly (half-Kelly)** — clear winner |
| ETH | 75 | 29.3% | **Stay min-size** — thin edge, needs data |
| SOL | 65 | 29.2% | **Stay min-size** — thin edge, needs data |
| XRP | 50 | 24.0% | **Dropped** — below threshold |

### Active Run Command (Step 2)
```bash
python apps/run_sniper.py --coins BTC ETH SOL --timeframe 15m --bankroll 39 --min-edge 0.05 --min-size --kelly-coins BTC --max-entry-price 0.20
```

### Hour Blocking (available but disabled)
Analysis of 246 trades showed 5 worst UTC hours (00,02,09,16,17) at 7% WR (3W-40L).
Feature implemented via `--block-hours` but not enabled pending more per-hour data.
The stats script now shows per-hour WR for ongoing monitoring.

## Important Notes
- Server MUST be in a non-US, non-blocked region (Amsterdam works)
- The sniper process is NOT managed by systemd — it runs directly
- Market maker (observe mode) is a SEPARATE concept from Longshot/Midshot
- Both Longshot and Midshot use the same `momentum_sniper.py` engine
- Polymarket uses Chainlink BTC/USD for settlement (not Binance/spot directly)
- USDC has 6 decimal places; Polymarket minimum order is $1.00 / 5 tokens
