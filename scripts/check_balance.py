#!/usr/bin/env python3
"""Check balance from bot's own log output. Instant, no API call."""
import subprocess
result = subprocess.run(
    ["grep", "-a", "Balance:", "/opt/polymarket-bot/data/live-v25.log"],
    capture_output=True, text=True
)
lines = result.stdout.strip().split("\n")
if lines and lines[-1]:
    print(lines[-1].split("Balance:")[1].strip().split("|")[0].strip() if "Balance:" in lines[-1] else "NO BALANCE IN LOG")
else:
    print("NO BALANCE IN LOG")
