# Polymarket Contrarian Bot - Management Guide

## Server Details
- **Provider:** DigitalOcean
- **Region:** Amsterdam (AMS3)
- **IP:** 209.38.36.107
- **OS:** Ubuntu 24.04 LTS
- **Plan:** $6/month (1GB RAM, 1 vCPU)
- **SSH Key:** `~/.ssh/polymarket_bot`

## Quick Commands (run from any Claude Code session)

### Check if bot is running
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "systemctl status polymarket-bot --no-pager | head -10"
```

### View live logs (last 30 lines)
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "tail -30 /var/log/polymarket-bot.log"
```

### Check for errors
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "grep -iE 'error|failed|403|401' /var/log/polymarket-bot.log | tail -10"
```

### Check trade stats
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "grep 'Trades:' /var/log/polymarket-bot.log | tail -1"
```

### View trade log CSV
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "cat /opt/polymarket-bot/data/trades.csv 2>/dev/null || echo 'No trades yet'"
```

### Restart the bot
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "systemctl restart polymarket-bot && echo 'Restarted'"
```

### Stop the bot
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "systemctl stop polymarket-bot && echo 'Stopped'"
```

### Update bot code (after pushing to GitHub)
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "cd /opt/polymarket-bot && git pull && systemctl restart polymarket-bot && echo 'Updated'"
```

### Check server resources
```bash
ssh -i ~/.ssh/polymarket_bot root@209.38.36.107 "free -h && echo '---' && df -h / && echo '---' && uptime"
```

## Prompts for New Chat Sessions

### Verify everything is working
> "Check the status of my Polymarket trading bot on 209.38.36.107. SSH key is at ~/.ssh/polymarket_bot. Check if the service is running, look for recent errors in /var/log/polymarket-bot.log, and show me the latest trade stats."

### Check trade performance
> "SSH into my Polymarket bot at 209.38.36.107 (key: ~/.ssh/polymarket_bot) and show me: 1) trade log from /opt/polymarket-bot/data/trades.csv, 2) current session stats from the log, 3) any errors in the last hour."

### Update the bot
> "I need to update my Polymarket bot. The code is in this directory and the bot runs on 209.38.36.107 (SSH key: ~/.ssh/polymarket_bot). Push the changes to GitHub and update the server."

### Debug issues
> "My Polymarket bot on 209.38.36.107 might have issues. SSH in (key: ~/.ssh/polymarket_bot) and check: systemctl status, recent errors in /var/log/polymarket-bot.log, memory usage, and whether WebSocket is connected."

## Architecture

```
Project Root/
├── apps/run_contrarian.py    # CLI entry point
├── strategies/contrarian.py  # Core strategy logic
├── src/bot.py                # Order execution
├── src/client.py             # Polymarket API client
├── src/signer.py             # EIP-712 order signing
├── src/websocket_client.py   # Real-time price feeds
├── src/gamma_client.py       # Market discovery
├── lib/trade_logger.py       # CSV trade logging
├── lib/volatility_tracker.py # BTC volatility tracking
├── lib/market_manager.py     # Market lifecycle management
├── lib/console.py            # Terminal UI
├── scripts/deploy.sh         # VPS deployment script
└── data/trades.csv           # Trade log (on server)
```

## Strategy Thesis & Research Foundation

**Source:** Analysis of 684 historical Polymarket crypto binary markets (BTC/ETH Up/Down 5-min and 15-min markets). The article found that tokens priced at ~$0.05 (the cheap/unpopular side) won approximately **8.8% of the time**.

**The Edge:**
- At $0.05 entry, payout is 20x ($1.00 per token on a $0.05 buy)
- Breakeven win rate at $0.05 = 5.0%
- Observed win rate from data = ~8.8%
- **Excess edge = 3.8%** (8.8% - 5.0%)
- Every trade has positive expected value: EV = +$0.76 per $1 bet at $0.05

**Why it works:** In short-duration BTC binary markets, the crowd overreacts to recent price momentum. When BTC is trending up, "Up" gets pushed to $0.90+ and "Down" drops to $0.03-$0.07. But BTC reverses direction ~8.8% of the time within 5 minutes, paying 14-20x on the cheap side.

**Key principle:** This is a high-volume, small-edge strategy. Each individual trade is likely to lose (91.2% of the time), but the wins are so large (14-20x) that they more than cover all losses over hundreds of trades. Consistency and volume are everything.

**Kelly Criterion:** Bets are sized using half Kelly (50% fractional) based on live Polymarket balance. This scales automatically — small bets on small bankrolls, larger bets as bankroll grows. Platform minimum is $1/trade.

**IMPORTANT:** The 8.8% win rate is from one historical study. Our own trade log (data/trades.csv) tracks every trade with bankroll snapshots. After ~200 trades we can statistically validate whether the real win rate matches. The CSV is the ground truth.

## Strategy Parameters
- **Coin:** BTC
- **Timeframe:** 5-minute markets
- **Entry range:** $0.03 - $0.07 (buy when token is cheap)
- **Bet size:** $1.00 minimum (Kelly scales up with bankroll)
- **Kelly fraction:** 50% (half Kelly - conservative)
- **Max trades/hour:** 10
- **Payout:** Hold to settlement for $1.00 (14-20x return)
- **Auto-redemption:** Winning tokens auto-redeemed to USDC via poly-web3

## Credentials
- **GitHub repo:** https://github.com/iamheisenburger/polymarket-contrarian-bot
- **Wallet EOA:** 0x788AdB6eaDa73377F7b63F59b2fF0573C86A65E5
- **Safe address:** 0x13D0684C532be5323662e851a1Bd10DF46d79806
- **ENV file on server:** /opt/polymarket-bot/.env

## Statistical Validation

The 8.8% win rate needs to be validated with our own data. Sample size requirements:
- **50 trades:** Too early. Wide confidence interval. Don't draw conclusions.
- **100 trades:** Starting to be useful. Expected ~9 wins. If you see 5-13, you're in range.
- **200 trades:** Statistically significant. If win rate is above 5%, the edge is confirmed at 95% confidence.
- **500 trades:** Strong conclusions. Can refine entry price buckets and optimize parameters.

**How to check:** `cat /opt/polymarket-bot/data/trades.csv` on the server. The CSV now tracks bankroll at each trade for equity curve analysis.

## Important Notes
- Server MUST be in a non-US, non-blocked region (Amsterdam works)
- Bot auto-restarts on crash (systemd Restart=always)
- Bot auto-starts on server reboot
- Destroy the old NYC droplet to save costs (if not already done)
