#!/bin/bash
# Deploy and start Weather Edge Bot on VPS
set -e

cd /opt/polymarket-bot

# Kill any existing weather bot
pkill -f "run_weather.py" 2>/dev/null || true
sleep 2

# Start weather edge bot in paper/observe mode
PYTHONUNBUFFERED=1 PYTHONPATH=. nohup python3 apps/run_weather.py \
    --bankroll 50 \
    --min-edge 0.05 \
    --kelly 0.5 \
    --scan-interval 300 \
    > /var/log/weather-edge.log 2>&1 &

echo "Weather Edge Bot started (PID: $!)"
echo "Log: /var/log/weather-edge.log"
echo "Data: data/weather_trades.csv"
