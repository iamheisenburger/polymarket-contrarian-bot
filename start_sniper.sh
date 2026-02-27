#!/bin/bash
cd /opt/polymarket-bot
source .env
export POLYMARKET_API_KEY POLYMARKET_SECRET POLYMARKET_PASSPHRASE
export PRIVATE_KEY SAFE_ADDRESS RPC_URL
rm -f data/*.pending.json

# BTC LIVE — V4 EMA Trend config
# EMA(4,16) directs trade side: bullish → UP, bearish → DOWN
# Backtest: 81.2% WR OOS, +1.1% margin, beats EMA(6,24) by $0.28/day
nohup /opt/polymarket-bot/venv/bin/python3 apps/run_sniper.py \
  --coins BTC \
  --timeframe 5m \
  --min-window-elapsed 120 --max-window-elapsed 210 \
  --market-check-interval 10 \
  --min-edge 0.02 --min-entry-price 0.30 --max-entry-price 0.70 \
  --min-fair-value 0.65 --min-momentum 0 --fixed-vol 0.15 \
  --side trend --ema-fast 4 --ema-slow 16 \
  --block-weekends \
  --require-vatic \
  --enable-cusum --cusum-target-wr 0.63 --adaptive-kelly \
  --max-consecutive-losses 5 \
  --bankroll 25 --min-size \
  --log-file data/late_sniper_5m.csv \
  >> /var/log/late-sniper-5m.log 2>&1 &

echo "BTC live PID: $!"
