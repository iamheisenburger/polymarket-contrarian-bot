#!/bin/bash
# Wrapper for daily polybacktest update
# Run via cron — handles cwd, logging, and python path
cd /Users/madmanhakim/polymarket-contrarian-bot
/Users/madmanhakim/polymarket-contrarian-bot/venv/bin/python3 scripts/pbt_daily_update.py
