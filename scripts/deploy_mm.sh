#!/bin/bash
# =============================================================================
# Polymarket Market Maker - VPS Deployment Script
# Run on existing VPS (209.38.36.107) that already has the bot infrastructure
# Usage: bash scripts/deploy_mm.sh
# =============================================================================

set -e

VPS="root@209.38.36.107"
KEY="$HOME/.ssh/polymarket_bot"
REMOTE_DIR="/opt/polymarket-bot"

echo "========================================="
echo "  Market Maker — VPS Deployment"
echo "========================================="

# Step 1: Push latest code to GitHub
echo "[1/4] Pushing code to GitHub..."
git add -A
git commit -m "Add market maker strategy for binary crypto markets

- lib/binance_ws.py: Real-time Binance WebSocket price feed
- lib/fair_value.py: Black-Scholes binary option fair value calculator
- strategies/market_maker.py: Two-sided quoting with inventory management
- apps/run_market_maker.py: CLI entry point
- scripts/test_fair_value.py: Model validation
- scripts/deploy_mm.sh: VPS deployment

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>" || echo "  (nothing to commit)"

git push origin main || echo "  (push skipped)"

# Step 2: Pull on VPS
echo "[2/4] Pulling latest code on VPS..."
ssh -i "$KEY" "$VPS" "cd $REMOTE_DIR && git pull"

# Step 3: Install any new dependencies
echo "[3/4] Installing dependencies..."
ssh -i "$KEY" "$VPS" "cd $REMOTE_DIR && source venv/bin/activate && pip install -q -r requirements.txt"

# Step 4: Create/update the market maker systemd service
echo "[4/4] Updating systemd service..."
ssh -i "$KEY" "$VPS" "cat > /etc/systemd/system/polymarket-mm.service << 'SVCEOF'
[Unit]
Description=Polymarket Market Maker Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/polymarket-bot
ExecStart=/opt/polymarket-bot/venv/bin/python apps/run_market_maker.py --coin BTC --timeframe 15m --spread 0.04 --kelly 0.5 --bankroll 20 --max-quote 10 --max-inv 5
Restart=always
RestartSec=30
Environment=PYTHONIOENCODING=utf-8
StandardOutput=append:/var/log/polymarket-mm.log
StandardError=append:/var/log/polymarket-mm.log

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable polymarket-mm"

echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""
echo "Commands:"
echo "  Start (observe):  ssh -i $KEY $VPS 'cd $REMOTE_DIR && source venv/bin/activate && python apps/run_market_maker.py --coin BTC --observe'"
echo "  Start (live):     ssh -i $KEY $VPS 'systemctl start polymarket-mm'"
echo "  Status:           ssh -i $KEY $VPS 'systemctl status polymarket-mm --no-pager | head -10'"
echo "  Logs:             ssh -i $KEY $VPS 'tail -50 /var/log/polymarket-mm.log'"
echo "  Stop:             ssh -i $KEY $VPS 'systemctl stop polymarket-mm'"
echo ""
echo "IMPORTANT: Run observe mode first to validate before going live!"
echo ""
