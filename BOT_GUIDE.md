# Polymarket Trading Bot - Management Guide

## Strategy Overview

We trade Polymarket crypto binary markets by exploiting the latency between Binance
spot price movements and Polymarket orderbook repricing. Black-Scholes calculates
the true probability; when the market price lags behind, we buy the mispriced side
and hold to settlement ($1.00 on win, $0.00 on loss).

### Active: Viper v4 Paper Trading (starting 2026-02-21)

**What changed:** Fee-aware edge calculation, higher confidence thresholds.
Polymarket introduced taker fees Jan 19 (15m: 1.56% max) and Feb 12 (5m: 0.44% max). Old 2c edge
was negative after fees. v4 uses net edge (after fee deduction) and higher FV thresholds.
5m chosen over 15m: 3.5x lower fees, 3x higher frequency.

**5m Instance:**
- BTC/ETH/SOL/XRP, 12 windows/hr/coin = 1,152/day total
- Fee: 0.44% max | Net edge >= 10c | FV >= 0.70 | Price $0.40-$0.85
- Log: `data/viper_v4_5m.csv`

**Decision point:** 40-50 trades → WR >= 60% = go live, WR < 50% = reassess

### Why $0.40 Min Entry (from 50-trade Viper v1 + 249-trade Longshot)
- Entry >= $0.40: 22W/6L = **78.6% WR**, +$56.02
- Entry < $0.40: 3W/19L = **13.6% WR**, -$16.54
- Black-Scholes overestimates low probabilities (says 30%, actual ~14%)
- Black-Scholes is accurate at high confidence (says 70%+, actual ~85%+)
- Entry price IS the confidence signal — NEVER trade below $0.40

### Binary Payoff Math
- Breakeven win rate = entry price (e.g., $0.55 entry -> 55% breakeven)
- Edge comes from Binance moving in ms while Polymarket reprices in seconds
- Kelly Criterion sizes bets proportional to edge and bankroll
- Hold to settlement — no early exits

## Roadmap to Max EV/Day

1. **Now:** Validate v2 with 20 trades on 15m BTC/ETH
2. **After validation:** Add 5m back as multi-timeframe scanner in SAME process (one wallet read, no conflicts)
3. **After 50 trades:** Add SOL/XRP if BTC/ETH fills are clean
4. **At $100+ bankroll:** Making $30+/day compounding

## Growth Plan

Half-Kelly with 15% cap. Monitoring checkpoints:

| Trades | WR > 65% | WR 55-65% | WR < 55% |
|--------|----------|-----------|----------|
| 20 | On track | Review | Investigate |
| 50 | Scale up | Stay course | Tighten to $0.50 |

### EV Estimates (at 70% WR, avg entry $0.55)

| Bankroll | Bet Size | EV/Trade | Trades/Day | EV/Day |
|----------|----------|----------|------------|--------|
| $21 | $3.15 | $0.47 | ~15 | ~$7 |
| $50 | $7.50 | $1.13 | ~15 | ~$17 |
| $100 | $15.00 | $2.25 | ~15 | ~$34 |

## Server Details
- **Provider:** DigitalOcean
- **Region:** Amsterdam (AMS3) — must be non-US
- **IP:** 209.38.36.107
- **OS:** Ubuntu 24.04 LTS
- **Plan:** $6/month (1GB RAM, 1 vCPU)
- **SSH Key:** `~/.ssh/polymarket_bot`

## Quick Commands

### Run Viper stats (THE command for checking performance)
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "cd /opt/polymarket-bot && python3 scripts/viper_stats.py"
```

### Check running processes
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "ps aux | grep python | grep -v grep"
```

### View 15m logs
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "tail -50 /var/log/viper-15m.log"
```

### Check for errors
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "grep -iE 'error|failed' /var/log/viper-15m.log | tail -10"
```

### Update bot code (after pushing to GitHub)
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "cd /opt/polymarket-bot && git pull"
```

### Start bot
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "bash /opt/polymarket-bot/start_viper.sh"
```

### Stop bot
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "pkill -9 -f run_sniper"
```

### Check server resources
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "free -h && echo '---' && df -h / && echo '---' && uptime"
```

## Active Process on VPS

**Viper v2 15m** (BTC/ETH 15-minute markets):
```bash
python apps/run_sniper.py --coins BTC ETH --timeframe 15m --bankroll 20 \
  --min-edge 0.05 --min-entry-price 0.40 --max-entry-price 0.85 \
  --max-vol 0.75 --min-momentum 0.0005 --momentum-lookback 30 \
  --kelly 0.50 --kelly-strong 0.75 --max-bet-fraction 0.15 \
  --log-file data/viper_15m.csv
```
Log: `/var/log/viper-15m.log` | Deploy: `bash start_viper.sh`

## Retired Strategies

### Viper v1 / Viper Selective (Feb 19-20, 50 trades)
- Two instances: BTC 5m + BTC/ETH 15m, entry $0.02-$0.85, blocked hours
- 25W/25L (50% WR), account $44 -> $0.09
- **Three root causes:**
  1. **FOK bug**: orders sent as GTC, 45 ghost trades ($94) drained wallet invisibly
  2. **Cheap entries**: trades below $0.40 went 3W/19L (13.6% WR)
  3. **Dual instances**: two bots sharing one wallet, stale balance reads
- Post-mortem data: `data/viper_5m_v1_postmortem.csv`, `data/viper_15m_v1_postmortem.csv`

### Longshot v1 (Feb 17-19, 249 trades)
- BTC/ETH/SOL/XRP on 15m markets, entry < $0.20, no filters
- Overall 29.7% WR but decayed: 35.2% first half -> 23.4% second half
- Account: $44 -> $87 (peak) -> $36 (gave back everything)
- **Root cause**: traded indiscriminately — high-vol trades + bad coins bled the account
- Trade log archived: `data/longshot_trades.csv`

## Bugs Found & Fixed

### FOK Order Type Bug (fixed 2026-02-20) — THE critical bug
- **Bug:** `src/bot.py` accepted `order_type="FOK"` but never passed it to Polymarket API. Every order went as GTC (default). Unfilled GTC orders sat on the book, filled later, spent money with zero logging.
- **Impact:** 45 ghost trades, $94.11 drained invisibly. Bot CSV showed +$39 profit while on-chain showed -$28 loss.
- **Fix:** Import `OrderType` from py_clob_client, map string to enum, pass to `PartialCreateOrderOptions(order_type=ot)`.
- **Commit:** `b0337e8`

### Phantom Trades Bug (fixed 2026-02-17)
- **Bug:** GTC orders accepted by CLOB were logged as filled even when unfilled
- **Fix:** FOK orders + verify status is not LIVE/OPEN before tracking
- **Code:** `strategies/momentum_sniper.py` — order_type="FOK" + fill verification

### Missing CLI Flag (fixed 2026-02-20)
- **Bug:** `--min-entry-price` flag didn't exist in CLI. Config default was $0.02.
- **Fix:** Added argparse argument and passed to SniperConfig constructor.
- **Commit:** `a71b6b9`

## Data Integrity

CSV is ground truth. With FOK bug fixed, every dollar spent is now logged. No more ghost trades.

### Archived files (in `data/`)
- `viper_5m_v1_postmortem.csv` — Viper v1 5m trades (for analysis only)
- `viper_15m_v1_postmortem.csv` — Viper v1 15m trades (for analysis only)
- `longshot_trades.csv` — Longshot v1 log (archived)
- `archive_pre_minsize_trades.csv` — old pre-min-size era trades (do not use)
- `archive_sniper_trades_old_logger.csv` — old logger duplicate (do not use)
- `archive_longshot_phantom_trades.csv` — contaminated log with phantom fills (do not use)
- `trades.csv` — old contrarian log (retired)

## Architecture

```
Project Root/
├── apps/
│   ├── run_sniper.py          # CLI entry point (has --min-entry-price flag)
│   ├── run_market_maker.py    # Market maker (unused)
│   ├── run_contrarian.py      # Old contrarian (retired)
│   └── run_flash_crash.py     # Flash crash (unused)
├── strategies/
│   └── momentum_sniper.py     # Core strategy (SniperConfig + MomentumSniperStrategy)
├── src/
│   ├── bot.py                 # Order execution, balance, auto-redemption (FOK fix here)
│   ├── client.py              # Polymarket API client
│   ├── signer.py              # EIP-712 order signing
│   ├── websocket_client.py    # Real-time orderbook feeds
│   └── gamma_client.py        # Market discovery
├── lib/
│   ├── fair_value.py          # Black-Scholes binary pricing
│   ├── binance_ws.py          # Binance real-time price feed + volatility + momentum
│   ├── deribit_vol.py         # Deribit DVOL implied volatility feed
│   ├── trade_logger.py        # CSV trade logging (+ latency fields)
│   ├── market_manager.py      # Market lifecycle management
│   └── console.py             # Terminal UI
├── data/
│   └── viper_15m.csv          # Active trade log (source of truth)
├── scripts/
│   ├── viper_stats.py         # Stats — run this to check performance
│   ├── longshot_stats.py      # Old longshot stats (for reference)
│   └── lag_collector.py       # Binance vs Polymarket lag research
└── start_viper.sh             # VPS deploy script
```

## Credentials
- **GitHub repo:** https://github.com/iamheisenburger/polymarket-contrarian-bot
- **Wallet EOA:** 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- **Safe address:** 0x13D0684C532be5323662e851a1Bd10DF46d79806
- **ENV file on server:** /opt/polymarket-bot/.env

## Important Notes
- Server MUST be in a non-US, non-blocked region (Amsterdam works)
- Bot runs as background process (NOT systemd) via `start_viper.sh`
- Polymarket uses Chainlink BTC/USD for settlement (not Binance/spot directly)
- USDC has 6 decimal places; Polymarket minimum order is $1.00 / 5 tokens
- Ground truth is ALWAYS `data/viper_15m.csv`
- Single instance — no concurrent wallet conflicts
- NEVER trade below $0.40 entry price
