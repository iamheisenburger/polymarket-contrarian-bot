#!/bin/bash
# Track A — Chainlink-based Sniper (PAPER TESTING)
#
# Same directional strategy as before, but using Chainlink RTDS as price source
# instead of Binance. This aligns the model with Polymarket's settlement oracle.
# Settlement detection uses Gamma API only (no Binance fallback).
#
# OBSERVE MODE — no real money at risk. Simulates trades and logs to CSV.
# Decision: 50+ verified trades → WR >= 60% = go live, else abandon.

set -e
cd /opt/polymarket-bot
source .env

PYTHON=/opt/polymarket-bot/venv/bin/python

# Kill any existing sniper instances (but not market maker)
pkill -9 -f run_sniper || true
sleep 2

echo "=== Track A — Chainlink Sniper PAPER TEST ==="
echo "  5m (BTC/ETH/SOL/XRP) — Chainlink price source"
echo "  FV >= 0.70, edge >= 0.10, observe mode"
echo "  Settlement: Gamma API only (no Binance fallback)"
echo ""

nohup $PYTHON apps/run_sniper.py \
    --coins BTC ETH SOL XRP \
    --timeframe 5m \
    --bankroll 12.95 \
    --min-edge 0.10 \
    --min-fair-value 0.70 \
    --min-entry-price 0.40 \
    --max-entry-price 0.85 \
    --max-vol 1.00 \
    --min-size \
    --price-source chainlink \
    --observe \
    --log-file data/track_a_chainlink.csv \
    >> /var/log/track-a.log 2>&1 &

echo "  Track A started (PID: $!)"
sleep 3

PROCS=$(ps aux | grep run_sniper | grep -v grep | wc -l)
echo ""
echo "=== $PROCS sniper instance(s) running ==="
echo "  Logs: tail -50 /var/log/track-a.log"
echo "  Data: cat data/track_a_chainlink.csv"
echo "  Stop: pkill -9 -f run_sniper"
