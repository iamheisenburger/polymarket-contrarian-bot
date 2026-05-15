#!/bin/bash
# Hardened launcher for PAAM live runs. Enforces all 6 operational rules
# established after the 2026-05-15 live losses:
#
#   1. Never launch with cargo/rustc orphans running (memory pressure).
#   2. Pre-flight all of: orphan-kill, mem ≥800MB free, disk ≥10GB,
#      binary present + recent, theo params loaded, l2 creds present.
#   3. Watchdog: if pmmm dies during the run, pause flag is set
#      automatically and we exit non-zero so caller can alert.
#   4. Heartbeat: print a "still alive" line every 60s with current
#      balance + event counters so a silent-stream stall is obvious.
#   5. Hard kill switch: if pUSD balance drops more than $MAX_DRAW
#      from launch baseline, kill the bot and pause flag.
#   6. Audit file is announced so the monitor can be re-armed on the
#      correct file path.
#
# Usage:
#   launch_paam_live.sh <coin> <duration_s> [max_drawdown_usd]
#
# Example:
#   launch_paam_live.sh BTC 1800 3
#
set -u

COIN="${1:-BTC}"
DURATION_S="${2:-1800}"
MAX_DRAW="${3:-3}"

cd /opt/polymarket-bot

echo "==== PRE-FLIGHT ===="

# Rule 1: clear cargo/rustc orphans.
for proc in cargo rustc; do
    n=$(pgrep -c -f "$proc" || echo 0)
    if [ "$n" -gt 0 ]; then
        echo "  killing $n stray $proc processes"
        pkill -9 -f "$proc" 2>/dev/null
    fi
done
sleep 1

# Memory check.
MEM_FREE=$(free -m | awk '/^Mem:/ {print $7}')
if [ "$MEM_FREE" -lt 800 ]; then
    echo "  [FAIL] available memory ${MEM_FREE}MB < 800MB threshold"
    exit 1
fi
echo "  [OK] memory: ${MEM_FREE}MB available"

# Disk check.
DISK_FREE_GB=$(df -BG /opt/polymarket-bot | awk 'NR==2 {gsub("G",""); print $4}')
if [ "$DISK_FREE_GB" -lt 10 ]; then
    echo "  [FAIL] disk free ${DISK_FREE_GB}GB < 10GB threshold"
    exit 1
fi
echo "  [OK] disk: ${DISK_FREE_GB}GB free"

# Binary check.
BIN=/opt/polymarket-bot/pmmm/target/release/pmmm
if [ ! -x "$BIN" ]; then
    echo "  [FAIL] binary missing: $BIN"
    exit 1
fi
BIN_AGE_S=$(($(date -u +%s) - $(stat -c %Y "$BIN")))
if [ "$BIN_AGE_S" -gt 86400 ]; then
    echo "  [WARN] binary is $((BIN_AGE_S/3600))h old — rebuild recommended"
fi
echo "  [OK] binary: $BIN (age $((BIN_AGE_S/60))min)"

# Theo params.
THEO=/opt/polymarket-bot/data/pricing_model/theo_v6_paam_current.json
[ -s "$THEO" ] || { echo "  [FAIL] theo missing"; exit 1; }
echo "  [OK] theo: $(jq -r '._meta.edge_vs_pm' $THEO) edge vs PM"

# L2 creds.
L2=/opt/polymarket-bot/data/l2_creds.json
[ -s "$L2" ] || { echo "  [FAIL] l2 creds missing"; exit 1; }
echo "  [OK] l2 creds present"

# No other pmmm running.
EXISTING=$(pgrep -c -f "target/release/pmmm" || echo 0)
if [ "$EXISTING" -gt 0 ]; then
    echo "  [FAIL] $EXISTING pmmm processes already running"
    exit 1
fi
echo "  [OK] no concurrent pmmm"

# Collector v2 alive (we want theo data fresh).
COLLECTOR_ALIVE=$(pgrep -c -f integrated_collector_v2 || echo 0)
if [ "$COLLECTOR_ALIVE" -eq 0 ]; then
    echo "  [WARN] collector_v2 not running — watchdog cron will restart"
fi

# Baseline balance.
PUSD_BASELINE=$(curl -s -X POST "https://polygon.drpc.org" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB","data":"0x70a0823100000000000000000000000013D0684C532be5323662e851a1Bd10DF46d79806"},"latest"]}' \
    | python3 -c "import sys,json; print(int(json.load(sys.stdin)['result'],16)/1e6)")
echo "  [OK] baseline balance: \$$PUSD_BASELINE"

# Remove pause flag now that pre-flight passed.
rm -f /opt/polymarket-bot/data/.bot_paused

echo ""
echo "==== LAUNCH ===="
TS=$(date -u +%Y%m%dT%H%M%SZ)
LOG=/opt/polymarket-bot/logs/paam_live_${TS}.log
PREFIX=/opt/polymarket-bot/data/pricing_model/paam_live_${TS}
mkdir -p /opt/polymarket-bot/logs

set -a; source .env 2>/dev/null; set +a

nohup "$BIN" run \
    --mode live \
    --coins "$COIN" \
    --timeframes 5m \
    --quoter paam \
    --duration-s "$DURATION_S" \
    --theo-params "$THEO" \
    --audit-prefix "$PREFIX" \
    --l2-creds "$L2" \
    > "$LOG" 2>&1 &
PID=$!
disown

echo "  PID=$PID"
echo "  LOG=$LOG"
AUDIT_FILE="${PREFIX}_$(date -u +%Y%m%d).jsonl"
echo "  AUDIT=$AUDIT_FILE"
echo "  MAX_DRAW=\$$MAX_DRAW"

# Wait for warmup.
for i in $(seq 1 30); do
    if grep -q "SpotPulse: warmup complete" "$LOG" 2>/dev/null; then
        echo "  warmup complete after ${i}s"
        break
    fi
    sleep 1
done

if ! ps -p $PID > /dev/null; then
    echo "==== PMMM DIED DURING WARMUP ===="
    touch /opt/polymarket-bot/data/.bot_paused
    tail -20 "$LOG"
    exit 2
fi

echo ""
echo "==== RUN MONITORING (heartbeat every 60s) ===="
echo "  Hard rules: kill+pause if pUSD drops > \$$MAX_DRAW from baseline"
echo "             kill+pause if pmmm dies"
echo ""

START_TS=$(date -u +%s)
END_TS=$((START_TS + DURATION_S))
LAST_BAL_CHECK=$START_TS

while [ "$(date -u +%s)" -lt "$END_TS" ]; do
    sleep 60
    NOW=$(date -u +%s)
    ELAPSED=$((NOW - START_TS))

    # Rule 3: bot must still be alive.
    if ! ps -p $PID > /dev/null; then
        echo "[$(date -u +%H:%M:%SZ) +${ELAPSED}s] PMMM DIED — pausing + exiting"
        touch /opt/polymarket-bot/data/.bot_paused
        tail -10 "$LOG"
        exit 2
    fi

    # Count events from audit.
    if [ -f "$AUDIT_FILE" ]; then
        N_ATTEMPT=$(grep -c '"event":"place_attempted"' "$AUDIT_FILE" 2>/dev/null || echo 0)
        N_ACK=$(grep -c '"event":"place_acked"' "$AUDIT_FILE" 2>/dev/null || echo 0)
        N_FILL=$(grep -c '"event":"fill_applied"' "$AUDIT_FILE" 2>/dev/null || echo 0)
        N_FAIL=$(grep -c '"event":"place_failed"' "$AUDIT_FILE" 2>/dev/null || echo 0)
    else
        N_ATTEMPT=0; N_ACK=0; N_FILL=0; N_FAIL=0
    fi

    # Rule 5: balance check.
    PUSD_NOW=$(curl -s -X POST "https://polygon.drpc.org" \
        -H "Content-Type: application/json" \
        -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB","data":"0x70a0823100000000000000000000000013D0684C532be5323662e851a1Bd10DF46d79806"},"latest"]}' \
        | python3 -c "import sys,json; print(int(json.load(sys.stdin)['result'],16)/1e6)" 2>/dev/null || echo "0")
    DRAW=$(python3 -c "print(round($PUSD_BASELINE - $PUSD_NOW, 4))")

    echo "[$(date -u +%H:%M:%SZ) +${ELAPSED}s] attempts=$N_ATTEMPT acks=$N_ACK fails=$N_FAIL fills=$N_FILL bal=\$$PUSD_NOW draw=\$$DRAW"

    # Hard kill.
    if python3 -c "import sys; sys.exit(0 if $DRAW > $MAX_DRAW else 1)" 2>/dev/null; then
        echo "  >>> HARD KILL: drawdown \$$DRAW > \$$MAX_DRAW <<<"
        kill -9 $PID 2>/dev/null
        touch /opt/polymarket-bot/data/.bot_paused
        exit 3
    fi
done

echo ""
echo "==== RUN COMPLETED ===="
kill $PID 2>/dev/null
sleep 1
touch /opt/polymarket-bot/data/.bot_paused

PUSD_FINAL=$(curl -s -X POST "https://polygon.drpc.org" \
    -H "Content-Type: application/json" \
    -d '{"jsonrpc":"2.0","id":1,"method":"eth_call","params":[{"to":"0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB","data":"0x70a0823100000000000000000000000013D0684C532be5323662e851a1Bd10DF46d79806"},"latest"]}' \
    | python3 -c "import sys,json; print(int(json.load(sys.stdin)['result'],16)/1e6)")
echo "  baseline:  \$$PUSD_BASELINE"
echo "  final:     \$$PUSD_FINAL"
echo "  net P&L:   \$$(python3 -c "print(round($PUSD_FINAL - $PUSD_BASELINE, 4))")"
