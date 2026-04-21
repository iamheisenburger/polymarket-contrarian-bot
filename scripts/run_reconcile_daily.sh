#!/bin/bash
# Daily reconciliation — compares /activity to live_v30.csv and alerts on
# ghosts/phantoms. Run every 6h via cron. Append output to audit log.
#
# Install (when ready):
#   crontab -e
#   Add:  0 */6 * * * /opt/polymarket-bot/scripts/run_reconcile_daily.sh
#
# Output: /opt/polymarket-bot/data/reconcile_audit.log (appended)
# Exit 0 = clean. Exit 1 = ghosts/phantoms detected.

cd /opt/polymarket-bot
source venv/bin/activate
set -a; source .env; set +a

STAMP=$(date -u +%Y-%m-%dT%H:%M:%SZ)
SINCE=$(date -u -d '48 hours ago' +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || date -u -v-48H +%Y-%m-%dT%H:%M:%SZ)

OUT_LOG=data/reconcile_audit.log

{
  echo "=================================="
  echo "Reconcile run: $STAMP"
  echo "Window since: $SINCE"
  echo "=================================="
  python3 scripts/reconcile_fills.py --since "$SINCE" --csv data/live_v30.csv
  echo ""
} >> "$OUT_LOG" 2>&1

# Exit status from python propagates (0 clean, 1 divergence)
tail -1 "$OUT_LOG"
