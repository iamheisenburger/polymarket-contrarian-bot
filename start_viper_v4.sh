#!/bin/bash
# Viper v4 — LIVE — Fee-aware strategy on 5m markets
# 51-trade paper validation: 84.3% WR vs 56.5% breakeven. GO LIVE.
#
# Half-Kelly sizing with 15% max bet fraction.
# Compounding enabled — bankroll grows, bet sizes grow.

set -e
cd /opt/polymarket-bot
source .env

PYTHON=/opt/polymarket-bot/venv/bin/python

# Kill any existing instances
pkill -9 -f run_sniper || true
sleep 2

echo "=== Viper v4 — LIVE TRADING ==="
echo "  5m (BTC/ETH/SOL/XRP) — 0.44% max fee, FV>=0.70, edge>=10c"
echo "  Half-Kelly (0.50) | Max 15% per trade | $20 bankroll"
echo ""

# 5m markets: 12 windows/hr/coin × 4 coins = 48 windows/hr = 1,152/day
nohup $PYTHON apps/run_sniper.py \
    --coins BTC ETH SOL XRP \
    --timeframe 5m \
    --bankroll 20 \
    --min-edge 0.10 \
    --min-fair-value 0.70 \
    --min-entry-price 0.40 \
    --max-entry-price 0.85 \
    --max-vol 1.00 \
    --kelly 0.50 \
    --kelly-strong 0.75 \
    --max-bet-fraction 0.15 \
    --log-file data/viper_v4_live.csv \
    >> /var/log/viper-v4-live.log 2>&1 &

echo "  5m instance started (PID: $!)"
sleep 3

PROCS=$(ps aux | grep run_sniper | grep -v grep | wc -l)
echo ""
echo "=== $PROCS instance running LIVE ==="
echo "  Logs: tail -50 /var/log/viper-v4-live.log"
echo "  Data: cat data/viper_v4_live.csv"
echo "  Stop: pkill -9 -f run_sniper"
