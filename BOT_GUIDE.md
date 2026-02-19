# Polymarket Trading Bot - Management Guide

## Strategy Overview

We trade Polymarket crypto binary markets by exploiting the latency between Binance
spot price movements and Polymarket orderbook repricing. Black-Scholes calculates
the true probability; when the market price lags behind, we buy the mispriced side
and hold to settlement ($1.00 on win, $0.00 on loss).

### Active: Viper Selective (BTC 5m + BTC/ETH 15m)
- **Two instances**: BTC on 5m markets + BTC/ETH on 15m markets
- **Half-Kelly sizing** — compound growth, not min-size
- **Five entry filters** working together:
  1. **Edge >= 5 cents** — fair_value minus buy_price must be at least $0.05
  2. **Volatility cap** — 5m: vol < 0.40 | 15m: vol < 0.50
  3. **Momentum confirmation** — Binance price moved >= 0.05% in trade direction (30s)
  4. **Entry price** — $0.02 to $0.85 (cheap AND midrange)
  5. **Hour blocking** — skip UTC hours 0, 2, 8, 9, 16, 17 (all had <20% WR)
- Starting bankroll: $35 (goal: $1000)
- Trade logs: `data/viper_5m.csv` and `data/viper_15m.csv`

### Why These Filters (from 249-trade Longshot deep analysis)

**Volatility is the #1 filter.** The edge is real but conditional on vol regime:

| Filter | 1st Half WR | 2nd Half WR | Still profitable? |
|--------|-------------|-------------|-------------------|
| Vol < 0.40 | 50.0% (30 trades) | **37.5%** (24 trades) | YES |
| Vol < 0.50 | 41.4% (58 trades) | **29.2%** (48 trades) | YES |
| Vol >= 0.50 | 29.9% (67 trades) | **19.7%** (76 trades) | NO — lost money |

**Per-coin in 2nd half** (why SOL/XRP were dropped):

| Coin | 2nd Half WR | Decision |
|------|-------------|----------|
| ETH | 30.0% | Keep |
| BTC | 26.7% | Keep |
| XRP | 19.2% | **Dropped** — below breakeven |
| SOL | 14.3% | **Dropped** — collapsed |

**Bad hours** (UTC): 0 (0% WR), 2 (0%), 8 (14%), 9 (9%), 16 (9%), 17 (9%)

### Binary Payoff Math
- Breakeven win rate = entry price (e.g., $0.21 entry -> 21% breakeven)
- Edge comes from Binance moving in ms while Polymarket reprices in seconds
- Kelly Criterion sizes bets proportional to edge and bankroll
- Hold to settlement — no early exits

## Growth Plan

Half-Kelly is already enabled. Monitoring checkpoints:

| Trades | WR > 35% | WR 25-35% | WR < 25% | WR < 21% |
|--------|----------|-----------|----------|----------|
| 30 | On track | On track | Review filters | **STOP** |
| 50 | 3/4 Kelly | Stay half-Kelly | Tighten vol to 0.30 | **STOP** |
| 100 | Full Kelly | 3/4 Kelly | Review or stop | **STOP** |

**Estimated timeline**: $35 -> $1000 in 4-14 days at 15-30 qualifying trades/day.

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
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "python3 /opt/polymarket-bot/scripts/viper_stats.py"
```

### Check running processes
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "ps aux | grep python | grep -v grep"
```

### View 5m logs
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "tail -50 /var/log/viper-5m.log"
```

### View 15m logs
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "tail -50 /var/log/viper-15m.log"
```

### Check for errors
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "grep -iE 'error|failed' /var/log/viper-5m.log /var/log/viper-15m.log | tail -10"
```

### Update bot code (after pushing to GitHub)
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "cd /opt/polymarket-bot && git pull"
```

### Check server resources
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "free -h && echo '---' && df -h / && echo '---' && uptime"
```

### Restart services (after code update)
```bash
ssh -i ~/.ssh/polymarket_bot -o ConnectTimeout=5 root@209.38.36.107 "systemctl restart polymarket-sniper.service polymarket-sniper-15m.service"
```

## Active Processes on VPS

1. **Viper Selective 5m** (BTC 5-minute markets):
   ```bash
   python apps/run_sniper.py --coins BTC --timeframe 5m --bankroll 35 --min-edge 0.05 --max-entry-price 0.85 --max-vol 0.40 --min-momentum 0.0005 --momentum-lookback 30 --kelly 0.50 --kelly-strong 0.75 --block-hours 0,2,8,9,16,17 --log-file data/viper_5m.csv
   ```
   Log: `/var/log/viper-5m.log` | Service: `polymarket-sniper.service`

2. **Viper Selective 15m** (BTC/ETH 15-minute markets):
   ```bash
   python apps/run_sniper.py --coins BTC ETH --timeframe 15m --bankroll 35 --min-edge 0.05 --max-entry-price 0.85 --max-vol 0.50 --min-momentum 0.0005 --momentum-lookback 30 --kelly 0.50 --kelly-strong 0.75 --block-hours 0,2,8,9,16,17 --log-file data/viper_15m.csv
   ```
   Log: `/var/log/viper-15m.log` | Service: `polymarket-sniper-15m.service`

## Retired Strategies

### Longshot v1 (Feb 17-19, 249 trades)
- BTC/ETH/SOL/XRP on 15m markets, entry < $0.20, no filters
- Overall 29.7% WR but decayed: 35.2% first half -> 23.4% second half
- Account: $44 -> $87 (peak) -> $36 (gave back everything)
- **Root cause**: traded indiscriminately — 76 high-vol trades at 19.7% WR bled the account
- Trade log archived: `data/longshot_trades.csv`

### Viper v1 (Feb 19, ~1 trade)
- Same engine but with filters DISABLED (max-vol 0, min-momentum 0, entry < $0.25)
- Effectively Longshot v1 with a price cap — repeated same mistakes
- Replaced by Viper Selective

## Data Integrity

### Phantom Trades Bug (fixed 2026-02-17)
- **Bug:** GTC orders accepted by CLOB were logged as filled even when unfilled
- **Fix:** FOK orders + verify status is not LIVE/OPEN before tracking
- **Code:** `strategies/momentum_sniper.py` — order_type="FOK" + fill verification

### Archived files (in `data/`)
- `archive_pre_minsize_trades.csv` — old pre-min-size era trades (do not use)
- `archive_sniper_trades_old_logger.csv` — old logger duplicate (do not use)
- `archive_longshot_phantom_trades.csv` — contaminated log with phantom fills (do not use)
- `trades.csv` — old contrarian log (retired)

## Architecture

```
Project Root/
├── apps/
│   ├── run_sniper.py          # CLI entry point
│   ├── run_market_maker.py    # Market maker (unused)
│   ├── run_contrarian.py      # Old contrarian (retired)
│   └── run_flash_crash.py     # Flash crash (unused)
├── strategies/
│   └── momentum_sniper.py     # Core strategy (SniperConfig + MomentumSniperStrategy)
├── src/
│   ├── bot.py                 # Order execution, balance, auto-redemption
│   ├── client.py              # Polymarket API client
│   ├── signer.py              # EIP-712 order signing
│   ├── websocket_client.py    # Real-time orderbook feeds
│   └── gamma_client.py        # Market discovery
├── lib/
│   ├── fair_value.py          # Black-Scholes binary pricing
│   ├── binance_ws.py          # Binance real-time price feed + volatility + momentum
│   ├── trade_logger.py        # CSV trade logging
│   ├── market_manager.py      # Market lifecycle management
│   └── console.py             # Terminal UI
├── data/
│   ├── viper_5m.csv           # Active 5m trade log (source of truth)
│   ├── viper_15m.csv          # Active 15m trade log (source of truth)
│   └── longshot_trades.csv    # Longshot v1 log (archived)
└── scripts/
    ├── viper_stats.py          # Stats — run this to check performance
    ├── longshot_stats.py       # Old longshot stats (for reference)
    └── lag_collector.py        # Binance vs Polymarket lag research
```

## Credentials
- **GitHub repo:** https://github.com/iamheisenburger/polymarket-contrarian-bot
- **Wallet EOA:** 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- **Safe address:** 0x13D0684C532be5323662e851a1Bd10DF46d79806
- **ENV file on server:** /opt/polymarket-bot/.env

## Important Notes
- Server MUST be in a non-US, non-blocked region (Amsterdam works)
- Both snipers run via systemd with `Restart=always`
- Polymarket uses Chainlink BTC/USD for settlement (not Binance/spot directly)
- USDC has 6 decimal places; Polymarket minimum order is $1.00 / 5 tokens
- Ground truth is ALWAYS `data/viper_5m.csv` and `data/viper_15m.csv`
- Both instances share the same wallet; on-chain balance refreshes every 10s
