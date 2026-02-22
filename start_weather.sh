#!/bin/bash
# Deploy and start Weather Edge Bot v3 on VPS
set -e

cd /opt/polymarket-bot

# Kill any existing weather bot
pkill -f "run_weather.py" 2>/dev/null || true
sleep 2

# Start weather edge v3 — 4-model consensus
PYTHONUNBUFFERED=1 PYTHONPATH=. nohup python3 apps/run_weather.py \
    --bankroll 50 \
    --min-edge 0.05 \
    --kelly 0.5 \
    --min-models 2 \
    --max-bets-per-city 1 \
    --scan-interval 300 \
    > /var/log/weather-edge-v3.log 2>&1 &

echo "Weather Edge v3 started (PID: $!)"
echo "Log: /var/log/weather-edge-v3.log"
echo "Data: data/weather_trades.csv"
