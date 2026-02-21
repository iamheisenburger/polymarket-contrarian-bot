#!/bin/bash
# Viper v4 — Fee-aware strategy on 5m markets
# 5m = lowest fees (0.44% max), highest frequency (1,152 windows/day)
#
# Paper trading mode — no real money until validated.
# Decision point: 40-50 paper trades → WR >= 60% = go live

set -e
cd /opt/polymarket-bot
source .env

PYTHON=/opt/polymarket-bot/venv/bin/python

# Kill any existing instances
pkill -9 -f run_sniper || true
sleep 2

echo "=== Viper v4 — Starting Paper Trade ==="
echo "  5m (BTC/ETH/SOL/XRP) — 0.44% max fee, FV>=0.70, edge>=10c"
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
    --min-size \
    --observe \
    --log-file data/viper_v4_5m.csv \
    >> /var/log/viper-v4-5m.log 2>&1 &

echo "  5m instance started (PID: $!)"
sleep 3

PROCS=$(ps aux | grep run_sniper | grep -v grep | wc -l)
echo ""
echo "=== $PROCS instance running in OBSERVE mode ==="
echo "  Logs: tail -50 /var/log/viper-v4-5m.log"
echo "  Data: cat data/viper_v4_5m.csv"
echo "  Stop: pkill -9 -f run_sniper"
