#!/bin/bash
# Auto Coin Manager — runs periodically via cron
# Evaluates paper trade performance and promotes/demotes coins.
# Strategy filters are FIXED — only coin selection adapts.
#
# Cron entry (every 6 hours):
#   0 */6 * * * /opt/polymarket-bot/scripts/auto_manage.sh >> /var/log/auto-manage.log 2>&1

set -e
cd /opt/polymarket-bot
export $(grep -v "^#" .env | xargs)

PYTHON="/opt/polymarket-bot/venv/bin/python3"
PAPER_CSV="data/paper_collector.csv"
LIVE_CSV="data/live_trades.csv"
DEG_MODEL="data/degradation_model.json"
DECISIONS_FILE="data/coin_decisions.json"
SHADOW_CSV="data/shadow.csv"
LIVE_PID_FILE="/var/run/live-sniper.pid"
PAPER_PID_FILE="/var/run/paper-collector.pid"

echo ""
echo "=========================================="
echo "COIN MANAGER: $(date -u)"
echo "=========================================="

# Step 1: Get current USDC balance
echo "[1/4] Querying balance..."
BALANCE=$($PYTHON -c "
from dotenv import load_dotenv; load_dotenv()
from src.config import Config; from src.bot import TradingBot; import os
config = Config.from_env()
bot = TradingBot(config=config, private_key=os.environ.get('POLY_PRIVATE_KEY',''))
b = bot.get_usdc_balance() or 0.0
print(f'{b:.2f}')
" 2>/dev/null || echo "0.00")
echo "  Balance: \$$BALANCE"

# Step 2: Run coin manager
echo "[2/4] Evaluating coins..."
$PYTHON apps/run_coin_manager.py \
    --paper-csv "$PAPER_CSV" \
    --live-csv "$LIVE_CSV" \
    --degradation "$DEG_MODEL" \
    --shadow-csv "$SHADOW_CSV" \
    --balance "$BALANCE" \
    --output "$DECISIONS_FILE"

# Step 3: Read decisions and manage live bot
echo "[3/4] Managing live bot..."

if [ ! -f "$DECISIONS_FILE" ]; then
    echo "  ERROR: No decisions file found. Skipping."
    exit 1
fi

LIVE_COINS=$($PYTHON -c "import json; d=json.load(open('$DECISIONS_FILE')); print(' '.join(d.get('live_coins',[])))" 2>/dev/null || echo "")
PAPER_COINS=$($PYTHON -c "import json; d=json.load(open('$DECISIONS_FILE')); print(' '.join(d.get('paper_coins',[])))" 2>/dev/null || echo "")

echo "  Live coins:  $LIVE_COINS"
echo "  Paper coins: $PAPER_COINS"

# Check if balance is too low
BALANCE_TOO_LOW=$($PYTHON -c "print('yes' if float('$BALANCE') < 5.0 else 'no')" 2>/dev/null || echo "yes")

if [ "$BALANCE_TOO_LOW" = "yes" ]; then
    echo "  Balance < \$5.00 — stopping live bot, keeping paper collector only"
    kill $(cat "$LIVE_PID_FILE" 2>/dev/null) 2>/dev/null || true
    rm -f "$LIVE_PID_FILE"
elif [ -n "$LIVE_COINS" ]; then
    # Check if live bot is running with the right coins
    NEEDS_RESTART=false

    if ! kill -0 $(cat "$LIVE_PID_FILE" 2>/dev/null) 2>/dev/null; then
        echo "  Live bot not running — starting"
        NEEDS_RESTART=true
    else
        # Check if coin list matches
        CURRENT_PID=$(cat "$LIVE_PID_FILE" 2>/dev/null)
        CURRENT_CMD=$(ps -p "$CURRENT_PID" -o args= 2>/dev/null || echo "")
        for COIN in $LIVE_COINS; do
            if ! echo "$CURRENT_CMD" | grep -q "$COIN"; then
                echo "  Coin list changed — restarting"
                NEEDS_RESTART=true
                break
            fi
        done
    fi

    if [ "$NEEDS_RESTART" = "true" ]; then
        # Kill existing live bot
        kill $(cat "$LIVE_PID_FILE" 2>/dev/null) 2>/dev/null || true
        sleep 3

        # Clear stale pending files
        rm -f data/*.pending.json

        echo "  Starting live bot: $LIVE_COINS"
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
            --shadow-log "$SHADOW_CSV" \
            --log-file data/live_trades.csv \
            > /var/log/live-sniper.log 2>&1 &

        echo $! > "$LIVE_PID_FILE"
        echo "  Live bot started PID $(cat $LIVE_PID_FILE)"
    else
        echo "  Live bot already running correctly (PID $(cat $LIVE_PID_FILE))"
    fi
else
    echo "  No coins promoted to live — stopping live bot"
    kill $(cat "$LIVE_PID_FILE" 2>/dev/null) 2>/dev/null || true
    rm -f "$LIVE_PID_FILE"
fi

# Step 4: Ensure paper collector is running on paper coins
echo "[4/4] Managing paper collector..."

if [ -n "$PAPER_COINS" ]; then
    NEEDS_RESTART=false

    if ! kill -0 $(cat "$PAPER_PID_FILE" 2>/dev/null) 2>/dev/null; then
        echo "  Paper collector not running — starting"
        NEEDS_RESTART=true
    else
        # Check if coin list matches
        CURRENT_PID=$(cat "$PAPER_PID_FILE" 2>/dev/null)
        CURRENT_CMD=$(ps -p "$CURRENT_PID" -o args= 2>/dev/null || echo "")
        for COIN in $PAPER_COINS; do
            if ! echo "$CURRENT_CMD" | grep -q "$COIN"; then
                echo "  Paper coin list changed — restarting"
                NEEDS_RESTART=true
                break
            fi
        done
    fi

    if [ "$NEEDS_RESTART" = "true" ]; then
        kill $(cat "$PAPER_PID_FILE" 2>/dev/null) 2>/dev/null || true
        sleep 2

        echo "  Starting paper collector: $PAPER_COINS"
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
        echo "  Paper collector started PID $(cat $PAPER_PID_FILE)"
    else
        echo "  Paper collector already running correctly (PID $(cat $PAPER_PID_FILE))"
    fi
else
    echo "  All coins are live — no paper collector needed"
fi

echo ""
echo "Done. Next run in 6 hours."
