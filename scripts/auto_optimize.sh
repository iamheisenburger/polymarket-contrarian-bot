#!/bin/bash
# Auto-Optimizer — runs periodically via cron
# Rebuilds degradation model, runs optimizer, restarts bot if config changed
#
# Cron entry (every 6 hours):
#   0 */6 * * * /opt/polymarket-bot/scripts/auto_optimize.sh >> /var/log/auto-optimize.log 2>&1

set -e
cd /opt/polymarket-bot
export $(grep -v "^#" .env | xargs)

PYTHON="/opt/polymarket-bot/venv/bin/python3"
LOG="/var/log/auto-optimize.log"
LIVE_CSV="data/adaptive_live.csv"
SIGNAL_DIR="data/signals"
DEG_MODEL="data/degradation_model.json"
OPTIMAL_CONFIG="data/optimal_config.json"
CURRENT_CONFIG="data/current_config.json"

echo ""
echo "=========================================="
echo "AUTO-OPTIMIZE: $(date -u)"
echo "=========================================="

# Step 1: Rebuild degradation model from latest data
echo "[1/4] Rebuilding degradation model..."
$PYTHON -c "
from lib.degradation import DegradationEstimator
import os, glob

deg = DegradationEstimator(window_days=7)

# Use all available paper CSVs
paper_csvs = glob.glob('data/paper_*.csv') + glob.glob('data/signals/signals_*.csv')
live_csvs = glob.glob('data/*_live*.csv') + glob.glob('data/adaptive_live.csv')

# Find the best paper and live CSVs
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

# Step 2: Run optimizer
echo "[2/4] Running optimizer..."
$PYTHON apps/run_optimizer.py \
    --paper-csv "$LIVE_CSV" \
    --degradation "$DEG_MODEL" \
    --window-hours 0 \
    --output "$OPTIMAL_CONFIG"

# Step 3: Compare new config to current
echo "[3/4] Comparing configs..."
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

# Compare coins and key params
if set(recommended) != set(current_coins):
    print('CHANGED')
    sys.exit(0)

# Compare per-coin configs
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
    exit 0
fi

if [ "$CONFIG_CHANGED" = "UNCHANGED" ]; then
    echo "  Config unchanged. No restart needed."
    exit 0
fi

# Step 4: Restart bot with new config
echo "[4/4] Config changed — restarting bot..."

# Save new config as current
cp "$OPTIMAL_CONFIG" "$CURRENT_CONFIG"

# Extract recommended config from JSON
COINS=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); print(' '.join(d.get('recommended_coins',[])))")
MIN_EDGE=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(max(c.get('min_edge',0.20) for c in configs.values()))")
MIN_MOM=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(max(c.get('min_momentum',0.0003) for c in configs.values()))")
MIN_FV=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(max(c.get('min_fair_value',0.70) for c in configs.values()))")
MIN_PRICE=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(min(c.get('min_entry_price',0.45) for c in configs.values()))")
MAX_PRICE=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(min(c.get('max_entry_price',0.70) for c in configs.values()))")
MIN_TTE=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(int(max(c.get('min_window_elapsed',100) for c in configs.values())))")
MAX_TTE=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); print(int(min(c.get('max_window_elapsed',180) for c in configs.values())))")
REQ_VATIC=$($PYTHON -c "import json; d=json.load(open('$OPTIMAL_CONFIG')); configs=d.get('coin_configs',{}); v=any(c.get('require_vatic',False) for c in configs.values()); print('--require-vatic' if v else '')")

echo "  New config: coins=$COINS edge=$MIN_EDGE mom=$MIN_MOM fv=$MIN_FV price=$MIN_PRICE-$MAX_PRICE tte=$MIN_TTE-$MAX_TTE"

# Kill current bot
kill $(cat /var/run/adaptive.pid 2>/dev/null) 2>/dev/null || true
sleep 3

# Start with new config
nohup $PYTHON apps/run_sniper.py \
    --coins $COINS \
    --timeframe 5m \
    --bankroll 10 \
    --min-edge $MIN_EDGE \
    --min-entry-price $MIN_PRICE \
    --max-entry-price $MAX_PRICE \
    --min-momentum $MIN_MOM \
    --min-fair-value $MIN_FV \
    --min-window-elapsed $MIN_TTE \
    --max-window-elapsed $MAX_TTE \
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
    $REQ_VATIC \
    > /var/log/adaptive-sniper.log 2>&1 &

echo $! > /var/run/adaptive.pid
echo "  Bot restarted with PID $(cat /var/run/adaptive.pid)"
echo "  Done."
