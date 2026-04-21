#!/bin/bash
# Pull latest IC data from VPS Dublin to local machine
# Run daily via cron to keep edge model calibrated

SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$SCRIPT_DIR"

VPS_KEY="$SCRIPT_DIR/polymarket-dublin.pem"
VPS_HOST="ubuntu@3.252.121.33"
VPS_IC="/opt/polymarket-bot/data/collector_v3.csv"
LOCAL_IC="$SCRIPT_DIR/data/vps_ic_latest.csv"

# Backup current
if [ -f "$LOCAL_IC" ]; then
    cp "$LOCAL_IC" "${LOCAL_IC}.bak"
fi

# Pull fresh data
scp -i "$VPS_KEY" -o StrictHostKeyChecking=no -o ConnectTimeout=15 \
    "$VPS_HOST:$VPS_IC" "$LOCAL_IC" 2>/dev/null

if [ $? -eq 0 ]; then
    ROWS=$(wc -l < "$LOCAL_IC")
    echo "$(date): IC data pulled successfully - $ROWS rows"
else
    echo "$(date): FAILED to pull IC data from VPS"
    # Restore backup
    if [ -f "${LOCAL_IC}.bak" ]; then
        mv "${LOCAL_IC}.bak" "$LOCAL_IC"
    fi
fi
