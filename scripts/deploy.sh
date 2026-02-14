#!/bin/bash
# =============================================================================
# Polymarket Contrarian Bot - VPS Deployment Script
# Run this on a fresh Ubuntu 24.04 server
# Usage: bash deploy.sh
# =============================================================================

set -e

echo "========================================="
echo "  Polymarket Contrarian Bot - Deploying"
echo "========================================="

# Update system
echo "[1/6] Updating system..."
apt-get update -qq && apt-get upgrade -y -qq

# Install Python 3.11+ and pip
echo "[2/6] Installing Python..."
apt-get install -y -qq python3 python3-pip python3-venv git

# Clone the repo
echo "[3/6] Cloning repository..."
cd /opt
if [ -d "polymarket-bot" ]; then
    cd polymarket-bot && git pull
else
    git clone https://github.com/iamheisenburger/polymarket-contrarian-bot.git polymarket-bot
    cd polymarket-bot
fi

# Create virtual environment and install deps
echo "[4/6] Installing dependencies..."
python3 -m venv venv
source venv/bin/activate
pip install -q -r requirements.txt

# Create .env file (user fills in keys)
echo "[5/6] Setting up environment..."
if [ ! -f .env ]; then
    cat > .env << 'ENVEOF'
POLY_PRIVATE_KEY=
POLY_SAFE_ADDRESS=
ENVEOF
    echo "  >> Created .env - you need to fill in your keys!"
else
    echo "  >> .env already exists, keeping it"
fi

# Create systemd service for 24/7 operation
echo "[6/6] Creating systemd service..."
cat > /etc/systemd/system/polymarket-bot.service << 'SVCEOF'
[Unit]
Description=Polymarket Contrarian Trading Bot
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=root
WorkingDirectory=/opt/polymarket-bot
ExecStart=/opt/polymarket-bot/venv/bin/python apps/run_contrarian.py --coin BTC --timeframe 5m --size 1.0 --daily-loss-limit 10.0
Restart=always
RestartSec=30
Environment=PYTHONIOENCODING=utf-8
StandardOutput=append:/var/log/polymarket-bot.log
StandardError=append:/var/log/polymarket-bot.log

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable polymarket-bot

# Create data directory
mkdir -p /opt/polymarket-bot/data

echo ""
echo "========================================="
echo "  Deployment Complete!"
echo "========================================="
echo ""
echo "Next steps:"
echo "  1. Edit your keys:  nano /opt/polymarket-bot/.env"
echo "  2. Start the bot:   systemctl start polymarket-bot"
echo "  3. Check status:    systemctl status polymarket-bot"
echo "  4. View logs:       tail -f /var/log/polymarket-bot.log"
echo "  5. Stop the bot:    systemctl stop polymarket-bot"
echo ""
echo "The bot will auto-restart if it crashes and"
echo "auto-start when the server reboots."
echo ""
