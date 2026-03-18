#!/bin/bash
# Watchdog — ensures live bot and paper collector stay running.
# Reads coin_decisions.json to know which coins are live vs paper.
# Run every 5 minutes via cron:
#   */5 * * * * /opt/polymarket-bot/scripts/watchdog.sh >> /var/log/watchdog.log 2>&1

cd /opt/polymarket-bot
export $(grep -v "^#" .env | xargs)

PYTHON="/opt/polymarket-bot/venv/bin/python3"
DECISIONS_FILE="data/coin_decisions.json"
LIVE_PID_FILE="/var/run/live-sniper.pid"
PAPER_PID_FILE="/var/run/paper-collector.pid"

# If no decisions file exists, do nothing (wait for auto_manage.sh to create it)
if [ ! -f "$DECISIONS_FILE" ]; then
    exit 0
fi

LIVE_COINS=$($PYTHON -c "import json; d=json.load(open('$DECISIONS_FILE')); print(' '.join(d.get('live_coins',[])))" 2>/dev/null || echo "")
PAPER_COINS=$($PYTHON -c "import json; d=json.load(open('$DECISIONS_FILE')); print(' '.join(d.get('paper_coins',[])))" 2>/dev/null || echo "")
BALANCE=$($PYTHON -c "import json; d=json.load(open('$DECISIONS_FILE')); print(d.get('balance', 0))" 2>/dev/null || echo "0")

# Check balance floor
BALANCE_TOO_LOW=$($PYTHON -c "print('yes' if float('$BALANCE') < 5.0 else 'no')" 2>/dev/null || echo "yes")

# Live bot watchdog
if [ -n "$LIVE_COINS" ] && [ "$BALANCE_TOO_LOW" = "no" ]; then
    if ! kill -0 $(cat "$LIVE_PID_FILE" 2>/dev/null) 2>/dev/null; then
        echo "$(date -u) WATCHDOG: Live bot died — restarting with: $LIVE_COINS"

        rm -f data/*.pending.json

        nohup $PYTHON apps/run_sniper.py \
            --coins $LIVE_COINS \
            --timeframe 5m \
            --bankroll "$BALANCE" \
            --min-edge 0.20 \
            --min-entry-price 0.60 \
            --max-entry-price 0.70 \
            --min-momentum 0.0005 \
            --min-fair-value 0.75 \
            --min-window-elapsed 120 \
            --max-window-elapsed 180 \
            --fixed-vol 0.15 \
            --min-size \
            --side trend \
            --ema-fast 4 \
            --ema-slow 16 \
            --block-weekends \
            --enable-cusum \
            --cusum-target-wr 0.70 \
            --adaptive-kelly \
            --market-check-interval 10 \
            --signal-log-dir data/signals \
            --log-file data/live_trades.csv \
            > /var/log/live-sniper.log 2>&1 &

        echo $! > "$LIVE_PID_FILE"
        echo "$(date -u) WATCHDOG: Live bot restarted PID $(cat $LIVE_PID_FILE)"
    fi
fi

# Paper collector watchdog
if [ -n "$PAPER_COINS" ]; then
    if ! kill -0 $(cat "$PAPER_PID_FILE" 2>/dev/null) 2>/dev/null; then
        echo "$(date -u) WATCHDOG: Paper collector died — restarting with: $PAPER_COINS"

        nohup $PYTHON apps/run_sniper.py \
            --coins $PAPER_COINS \
            --timeframe 5m \
            --bankroll 10 \
            --min-edge 0.10 \
            --min-entry-price 0.40 \
            --max-entry-price 0.80 \
            --min-momentum 0.0001 \
            --min-fair-value 0.60 \
            --min-window-elapsed 60 \
            --max-window-elapsed 240 \
            --fixed-vol 0.15 \
            --min-size \
            --side trend \
            --ema-fast 4 \
            --ema-slow 16 \
            --market-check-interval 10 \
            --signal-log-dir data/signals \
            --log-file data/paper_collector.csv \
            --observe \
            > /var/log/paper-collector.log 2>&1 &

        echo $! > "$PAPER_PID_FILE"
        echo "$(date -u) WATCHDOG: Paper collector restarted PID $(cat $PAPER_PID_FILE)"
    fi
fi
