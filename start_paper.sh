#!/bin/bash
cd /opt/polymarket-bot
source .env
export POLYMARKET_API_KEY POLYMARKET_SECRET POLYMARKET_PASSPHRASE
export PRIVATE_KEY SAFE_ADDRESS RPC_URL

# ETH/SOL/XRP PAPER — V4 EMA Trend config (observe mode)
# Same config as live but --observe. Collecting data for validation.
nohup /opt/polymarket-bot/venv/bin/python3 apps/run_sniper.py \
  --coins ETH SOL XRP \
  --timeframe 5m \
  --min-window-elapsed 120 --max-window-elapsed 210 \
  --market-check-interval 10 \
  --min-edge 0.02 --min-entry-price 0.30 --max-entry-price 0.70 \
  --min-fair-value 0.65 --min-momentum 0 --fixed-vol 0.15 \
  --side trend --ema-fast 4 --ema-slow 16 \
  --block-weekends \
  --require-vatic \
  --enable-cusum --cusum-target-wr 0.63 --adaptive-kelly \
  --bankroll 25 --min-size --observe \
  --log-file data/paper_3coin_5m.csv \
  >> /var/log/paper-3coin-5m.log 2>&1 &

echo "Paper 3-coin PID: $!"
