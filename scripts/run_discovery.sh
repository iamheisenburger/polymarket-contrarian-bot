#!/bin/bash
# Daily Discovery Sweep — finds filter candidates from paper data.
# NEVER deploys anything. Only logs candidates for human review.
#
# Cron entry (daily at 6am UTC):
#   0 6 * * * /opt/polymarket-bot/scripts/run_discovery.sh >> /var/log/discovery.log 2>&1

set -e
cd /opt/polymarket-bot
export $(grep -v "^#" .env | xargs)

PYTHON="/opt/polymarket-bot/venv/bin/python3"
PAPER_CSV="data/paper_collector.csv"
BENCHMARK_PATH="data/benchmark.json"
CANDIDATES_PATH="data/strategy_candidates.json"

echo ""
echo "=========================================="
echo "DISCOVERY SWEEP: $(date -u)"
echo "=========================================="

# Step 1: Update benchmark from live trades
echo "[1/2] Updating benchmark..."
$PYTHON apps/run_benchmark.py \
    --trade-csv data/live_trades.csv \
    --benchmark-path "$BENCHMARK_PATH" \
    --save

# Step 2: Run discovery sweep on paper data (last 7 days)
echo "[2/2] Running discovery sweep..."
$PYTHON apps/run_discovery.py \
    --paper-csv "$PAPER_CSV" \
    --benchmark-path "$BENCHMARK_PATH" \
    --window-days 7 \
    --output "$CANDIDATES_PATH"

# Check if any candidates were found
if [ -f "$CANDIDATES_PATH" ]; then
    N_CANDIDATES=$($PYTHON -c "import json; d=json.load(open('$CANDIDATES_PATH')); print(len(d.get('candidates',[])))" 2>/dev/null || echo "0")
    if [ "$N_CANDIDATES" -gt "0" ]; then
        echo ""
        echo "CANDIDATES FOUND: $N_CANDIDATES filter combinations beat the benchmark."
        echo "Review: cat $CANDIDATES_PATH"
        echo "NOTE: Discovery NEVER deploys. Human review required."
    else
        echo "No candidates found. Current filters are optimal."
    fi
fi

echo ""
echo "Done."
