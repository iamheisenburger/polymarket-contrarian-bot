#!/bin/bash
# Paper Arena — 10 strategies simultaneously (PAPER TESTING)
#
# Runs alongside Oracle (Track A) and Spread (Track B).
# Single process, all coins, all 10 signal strategies.
# Settlement via Gamma API only.
#
# Goal: 50 verified trades per strategy → evaluate winners → go live.

set -e
cd /opt/polymarket-bot
source .env

PYTHON=/opt/polymarket-bot/venv/bin/python

# Kill any existing arena instances (but not sniper or market maker)
pkill -9 -f run_arena || true
sleep 2

echo "=== Paper Arena — 10-Strategy Tournament ==="
echo "  BTC/ETH/SOL/XRP 5m — 10 signal strategies"
echo "  Single process, paper trading only"
echo "  Settlement: Gamma API only"
echo ""

nohup $PYTHON apps/run_arena.py \
    --coins BTC ETH SOL XRP \
    --timeframe 5m \
    --bankroll 12.95 \
    >> /var/log/arena.log 2>&1 &

echo "  Arena started (PID: $!)"
sleep 3

PROCS=$(ps aux | grep run_arena | grep -v grep | wc -l)
echo ""
echo "=== $PROCS arena instance(s) running ==="
echo "  Logs:  tail -50 /var/log/arena.log"
echo "  Data:  ls data/arena_*.csv"
echo "  Stop:  pkill -9 -f run_arena"
