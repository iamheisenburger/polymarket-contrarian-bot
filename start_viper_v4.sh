#!/bin/bash
# Viper v4 — Multi-timeframe fee-aware strategy
# Primary: 5m (lowest fees, highest frequency)
# Supplementary: 1h (fee-free, moderate frequency)
#
# Paper trading mode — no real money until validated.
# Decision point: 40-50 paper trades → WR >= 60% = go live

set -e
cd /opt/polymarket-bot
source .env

# Kill any existing instances
pkill -9 -f run_sniper || true
sleep 2

echo "=== Viper v4 — Starting Paper Trade ==="
echo "  Primary:   5m (BTC/ETH/SOL/XRP) — 0.44% max fee, FV>=0.70, edge>=10c"
echo "  Secondary: 1h (BTC/ETH/SOL/XRP) — fee-free, FV>=0.65, edge>=5c"
echo ""

# Instance 1: 5m markets (primary — lowest fees, highest frequency)
# 12 windows/hr/coin × 4 coins = 48 windows/hr = 1,152/day
PYTHON=/opt/polymarket-bot/venv/bin/python

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

# Instance 2: 1h markets (supplementary — fee-free)
# 1 window/hr/coin × 4 coins = 4 windows/hr = 96/day
# Lower thresholds because no fees eat the edge
nohup $PYTHON apps/run_sniper.py \
    --coins BTC ETH SOL XRP \
    --timeframe 1h \
    --bankroll 20 \
    --min-edge 0.05 \
    --min-fair-value 0.65 \
    --min-entry-price 0.40 \
    --max-entry-price 0.85 \
    --max-vol 1.00 \
    --min-size \
    --observe \
    --log-file data/viper_v4_1h.csv \
    >> /var/log/viper-v4-1h.log 2>&1 &

echo "  1h instance started (PID: $!)"

echo ""
echo "=== Both instances running in OBSERVE mode ==="
echo "  5m logs: tail -50 /var/log/viper-v4-5m.log"
echo "  1h logs: tail -50 /var/log/viper-v4-1h.log"
echo "  5m data: cat data/viper_v4_5m.csv"
echo "  1h data: cat data/viper_v4_1h.csv"
echo "  Stop:    pkill -9 -f run_sniper"
