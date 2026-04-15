#!/bin/bash
# Quick status check for the polybacktest fetcher.
# Usage: bash scripts/pbt_status.sh

CSV=data/pbt_collector.csv
LOG=/tmp/pbt_fetch_30d.log

echo "=== Polybacktest fetcher status ==="
echo

if pgrep -f "polybacktest_fetcher" > /dev/null; then
    PID=$(pgrep -f "polybacktest_fetcher" | head -1)
    PARENT=$(ps -o ppid= -p $PID 2>/dev/null | tr -d ' ')
    UPTIME=$(ps -o etime= -p $PID 2>/dev/null | tr -d ' ')
    CPU=$(ps -o %cpu= -p $PID 2>/dev/null | tr -d ' ')
    echo "Process: ALIVE (PID $PID, parent $PARENT, uptime $UPTIME, cpu ${CPU}%)"
    if [ "$PARENT" = "1" ]; then
        echo "  ✓ Detached from terminal (re-parented to launchd) — survives session close"
    fi
else
    echo "Process: NOT RUNNING"
fi

echo
if [ -f "$CSV" ]; then
    LINES=$(wc -l < "$CSV" | tr -d ' ')
    SIZE=$(stat -f%z "$CSV" 2>/dev/null || stat -c%s "$CSV" 2>/dev/null)
    SIZE_MB=$(echo "scale=1; $SIZE / 1024 / 1024" | bc)
    echo "CSV: $LINES rows ($(echo $SIZE | numfmt --to=iec 2>/dev/null || echo "${SIZE} bytes"))"

    python3 -c "
import csv
from collections import Counter
with open('$CSV') as f:
    rows = list(csv.DictReader(f))
c = Counter(r['coin'] for r in rows)
mom = [r for r in rows if r['is_momentum_side'] == 'True']
markets = len(set(r['market_slug'] for r in rows))
print(f'  Markets covered: {markets}')
print(f'  By coin: {dict(c)}')
print(f'  Mom-side resolved: {len(mom)}')
if rows:
    dates = sorted(set(r['timestamp'][:10] for r in rows))
    print(f'  Date range: {dates[0]} → {dates[-1]} ({len(dates)} days)')
"
fi

echo
if [ -f "$LOG" ]; then
    echo "=== Recent log entries ==="
    grep -E "progress|done|GRAND|ERROR|Found" "$LOG" 2>/dev/null | tail -10
fi
