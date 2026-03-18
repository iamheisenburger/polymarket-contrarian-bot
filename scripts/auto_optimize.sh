#!/bin/bash
# Auto-Optimizer — runs periodically via cron
# Fully autonomous: collects data on ALL coins, optimizes, promotes/demotes coins
#
# Cron entry (every 6 hours):
#   0 */6 * * * /opt/polymarket-bot/scripts/auto_optimize.sh >> /var/log/auto-optimize.log 2>&1

set -e
cd /opt/polymarket-bot
export $(grep -v "^#" .env | xargs)

PYTHON="/opt/polymarket-bot/venv/bin/python3"
LIVE_CSV="data/adaptive_live.csv"
SIGNAL_DIR="data/signals"
DEG_MODEL="data/degradation_model.json"
OPTIMAL_CONFIG="data/optimal_config.json"
CURRENT_CONFIG="data/current_config.json"
ALL_COINS="BTC ETH SOL XRP DOGE BNB"

echo ""
echo "=========================================="
echo "AUTO-OPTIMIZE: $(date -u)"
echo "=========================================="

# Step 1: Rebuild degradation model from ALL available data
echo "[1/5] Rebuilding degradation model..."
$PYTHON -c "
from lib.degradation import DegradationEstimator
import os, glob

deg = DegradationEstimator(window_days=7)

paper_csvs = glob.glob('data/paper_*.csv') + glob.glob('data/signals/signals_*.csv')
live_csvs = [f for f in glob.glob('data/*_live*.csv') + ['data/adaptive_live.csv'] if os.path.exists(f)]

paper_csv = max(paper_csvs, key=os.path.getsize) if paper_csvs else None
live_csv = max(live_csvs, key=os.path.getsize) if live_csvs else None

if paper_csv and live_csv:
    print(f'  Paper: {paper_csv}')
    print(f'  Live: {live_csv}')
    deg.update_from_csvs(paper_csv, live_csv)
    deg.save('$DEG_MODEL')
    print(deg.summary())
elif live_csv:
    print(f'  Live only: {live_csv} (no paper data yet)')
else:
    print('  No data yet — skipping')
    exit(0)
"

# Step 2: Get current balance
echo "[2/5] Querying balance..."
BANKROLL=$($PYTHON -c "
from dotenv import load_dotenv; load_dotenv()
from src.config import Config; from src.bot import TradingBot; import os
config = Config.from_env()
bot = TradingBot(config=config, private_key=os.environ.get('POLY_PRIVATE_KEY',''))
b = bot.get_usdc_balance() or 0.0
print(f'{b:.2f}')
" 2>/dev/null || echo "0.00")
echo "  Balance: \$$BANKROLL"

# Step 3: Run optimizer on ALL available data (paper + live for all coins)
echo "[3/5] Running optimizer (bankroll=\$$BANKROLL)..."
$PYTHON apps/run_optimizer.py \
    --paper-csv "$LIVE_CSV" \
    --degradation "$DEG_MODEL" \
    --window-hours 0 \
    --bankroll "$BANKROLL" \
    --output "$OPTIMAL_CONFIG"

# Step 4: Compare and restart live bot if config changed
echo "[4/5] Comparing configs..."
CONFIG_CHANGED=$($PYTHON -c "
import json, os, sys

optimal_path = '$OPTIMAL_CONFIG'
current_path = '$CURRENT_CONFIG'

if not os.path.exists(optimal_path):
    print('NO_OPTIMAL')
    sys.exit(0)

optimal = json.load(open(optimal_path))
recommended = optimal.get('recommended_coins', [])

if not recommended:
    print('NO_RECOMMENDED')
    sys.exit(0)

if not os.path.exists(current_path):
    print('CHANGED')
    sys.exit(0)

current = json.load(open(current_path))
current_coins = current.get('recommended_coins', [])

if set(recommended) != set(current_coins):
    print('CHANGED')
    sys.exit(0)

for coin in recommended:
    opt_cfg = optimal.get('coin_configs', {}).get(coin, {})
    cur_cfg = current.get('coin_configs', {}).get(coin, {})
    for key in ['min_edge', 'min_momentum', 'min_fair_value', 'min_entry_price', 'max_entry_price', 'require_vatic']:
        if opt_cfg.get(key) != cur_cfg.get(key):
            print('CHANGED')
            sys.exit(0)

print('UNCHANGED')
")

echo "  Config status: $CONFIG_CHANGED"

if [ "$CONFIG_CHANGED" = "NO_OPTIMAL" ] || [ "$CONFIG_CHANGED" = "NO_RECOMMENDED" ]; then
    echo "  No viable config found. Keeping current bot running."
else
    if [ "$CONFIG_CHANGED" = "CHANGED" ]; then
        echo "  Config changed — restarting live bot..."
        cp "$OPTIMAL_CONFIG" "$CURRENT_CONFIG"
        COINS=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); print(' '.join(d.get('recommended_coins',[])))")
        echo "  Live coins: $COINS"

        kill $(cat /var/run/adaptive.pid 2>/dev/null) 2>/dev/null || true
        sleep 3

        nohup $PYTHON apps/run_sniper.py \
            --coins $COINS \
            --timeframe 5m \
            --bankroll 10 \
            --per-coin-config "$OPTIMAL_CONFIG" \
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
            --log-file data/adaptive_live.csv \
            > /var/log/adaptive-sniper.log 2>&1 &

        echo $! > /var/run/adaptive.pid
        echo "  Live bot restarted PID $(cat /var/run/adaptive.pid)"
    else
        echo "  Config unchanged. No restart needed."
    fi
fi

# Step 5: Ensure paper collector is running on ALL coins (including non-live ones)
echo "[5/5] Checking paper collector for all coins..."
LIVE_COINS=$($PYTHON -c "import json,os; d=json.load(open('$OPTIMAL_CONFIG')) if os.path.exists('$OPTIMAL_CONFIG') else {}; print(' '.join(d.get('recommended_coins',[])))" 2>/dev/null || echo "")
PAPER_COINS=""
for COIN in $ALL_COINS; do
    IS_LIVE=false
    for LC in $LIVE_COINS; do
        if [ "$COIN" = "$LC" ]; then
            IS_LIVE=true
            break
        fi
    done
    if [ "$IS_LIVE" = "false" ]; then
        PAPER_COINS="$PAPER_COINS $COIN"
    fi
done
PAPER_COINS=$(echo $PAPER_COINS | xargs)

if [ -n "$PAPER_COINS" ]; then
    # Check if paper collector is running
    if ! kill -0 $(cat /var/run/paper-collector.pid 2>/dev/null) 2>/dev/null; then
        echo "  Starting paper collector for: $PAPER_COINS"
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

        echo $! > /var/run/paper-collector.pid
        echo "  Paper collector started PID $(cat /var/run/paper-collector.pid)"
    else
        echo "  Paper collector already running (PID $(cat /var/run/paper-collector.pid))"
        # Check if coin list needs updating
        CURRENT_PAPER_COINS=$(ps aux | grep "paper_collector.csv" | grep -v grep | grep -o "\-\-coins [A-Z ]*" | sed 's/--coins //' || echo "")
        if [ "$CURRENT_PAPER_COINS" != "$PAPER_COINS" ]; then
            echo "  Paper coins changed: $CURRENT_PAPER_COINS -> $PAPER_COINS"
            echo "  Restarting paper collector..."
            kill $(cat /var/run/paper-collector.pid 2>/dev/null) 2>/dev/null || true
            sleep 2

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

            echo $! > /var/run/paper-collector.pid
            echo "  Paper collector restarted PID $(cat /var/run/paper-collector.pid)"
        fi
    fi
else
    echo "  All coins are live — no paper collector needed"
fi

echo "  Done. Next run in 6 hours."
