#!/bin/bash
# Copy Sniper — Alpha Wallet Copy-Trade System (PAPER TESTING)
#
# Follows proven Polymarket winners across SPORTS, POLITICS, ECONOMICS.
# Single process, polls every 60s, Kelly-sized paper trades.
# Settlement via Gamma API.
#
# Goal: 50 verified trades → evaluate → go live.

set -e
cd /opt/polymarket-bot
source .env

PYTHON=/opt/polymarket-bot/venv/bin/python

# Kill any existing copy sniper instances
pkill -9 -f run_copy_sniper || true
sleep 2

echo "=== Copy Sniper — Alpha Wallet Copy-Trade ==="
echo "  Categories: SPORTS, POLITICS, ECONOMICS, CULTURE, TECH, FINANCE"
echo "  Poll: every 60s | Settle: every 60s"
echo "  Paper trading only"
echo ""

nohup env PYTHONUNBUFFERED=1 $PYTHON apps/run_copy_sniper.py \
    --bankroll 50.00 \
    --poll-interval 60 \
    --settle-interval 60 \
    --wallets-per-category 10 \
    --min-weekly-pnl 1000 \
    --max-slippage 0.05 \
    --max-trade-age 300 \
    --kelly 0.5 \
    >> /var/log/copy-sniper.log 2>&1 &

echo "  Copy Sniper started (PID: $!)"
sleep 3

PROCS=$(ps aux | grep run_copy_sniper | grep -v grep | wc -l)
echo ""
echo "=== $PROCS copy sniper instance(s) running ==="
echo "  Logs:  tail -50 /var/log/copy-sniper.log"
echo "  Data:  cat data/copy_sniper.csv"
echo "  Stop:  pkill -9 -f run_copy_sniper"
