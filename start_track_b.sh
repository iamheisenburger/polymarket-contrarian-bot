#!/bin/bash
# Track B — Market Maker (PAPER TESTING) — ALL 4 COINS
#
# Non-directional strategy: posts two-sided quotes around fair value.
# Profits from bid-ask spread, not from predicting direction.
# Settlement detection uses Gamma API only (no Binance fallback).
#
# OBSERVE MODE — simulates fills when market best ask <= our quote price.
# Decision: 50+ simulated fills → positive PnL = go live, else abandon.
#
# Runs 4 separate instances (one per coin) since market maker is single-coin.

set -e
cd /opt/polymarket-bot
source .env

PYTHON=/opt/polymarket-bot/venv/bin/python

# Kill any existing market maker instances (but not sniper)
pkill -9 -f run_market_maker || true
sleep 2

echo "=== Track B — Market Maker PAPER TEST (4 coins) ==="
echo "  BTC/ETH/SOL/XRP 5m — spread=0.04, min-edge=0.02"
echo "  Observe mode with simulated fills"
echo "  Settlement: Gamma API only (no Binance fallback)"
echo ""

for COIN in BTC ETH SOL XRP; do
    LOGFILE="data/track_b_${COIN,,}.csv"
    nohup $PYTHON apps/run_market_maker.py \
        --coin $COIN \
        --timeframe 5m \
        --spread 0.04 \
        --min-edge 0.02 \
        --kelly 0.50 \
        --bankroll 12.95 \
        --max-quote 5.0 \
        --max-inv 5 \
        --stop-before-expiry 120 \
        --observe \
        --log-file $LOGFILE \
        >> /var/log/track-b.log 2>&1 &
    echo "  $COIN started (PID: $!) → $LOGFILE"
    sleep 1
done

sleep 3

PROCS=$(ps aux | grep run_market_maker | grep -v grep | wc -l)
echo ""
echo "=== $PROCS market maker instance(s) running ==="
echo "  Logs: tail -50 /var/log/track-b.log"
echo "  Data: ls data/track_b_*.csv"
echo "  Stop: pkill -9 -f run_market_maker"
