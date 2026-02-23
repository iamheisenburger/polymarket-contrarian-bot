#!/usr/bin/env python3
"""Verify FOK order fill status via CLOB get_order API."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.bot import TradingBot

bot = TradingBot()
client = bot._official_client

if not client:
    print("ERROR: Official client not initialized")
    sys.exit(1)

# Phantom orders (bot says filled, not on-chain)
phantom_orders = {
    "Buenos Aires 33C": "0xcde9956988de5362e5182a087d2b5e323f7826e1e55ff2dbe112c4fcc79a5b5e",
    "Buenos Aires <=27C": "0x6667eac7433a2d2c42f00eaedb3308ff9cbc8b8345309568b27f6e2f034a0231",
    "Sao Paulo 25C": None,  # Need to find from logs
    "Toronto 0C": None,
}

# Real orders (confirmed on-chain)
real_orders = {
    "Ankara 8C": "0x7b968f79bd74f000e171acd4a518dcdf0daf000af7dea49fcb7a429780d6d2ce",
    "London 15C": "0x825f4ea654c0d321c29563ea855dd395d853caa0f2c439a05622a65d05963d13",
    "Chicago 26-27F": "0x439a9ea5c33066981f71ca47a79390c84b4c528d97da9b2ea063bce56e9f92ea",
}

print("=== PHANTOM ORDERS ===")
for name, oid in phantom_orders.items():
    if not oid:
        continue
    try:
        order = client.get_order(oid)
        status = order.get("status", "?")
        size_matched = order.get("size_matched", "?")
        original_size = order.get("original_size", "?")
        otype = order.get("type", "?")
        print(f"  {name:25} status={status:10} matched={size_matched}/{original_size} type={otype}")
        if status != "MATCHED":
            print(f"    -> CONFIRMED PHANTOM: order was {status}, not filled")
    except Exception as e:
        print(f"  {name:25} ERROR: {e}")

print("\n=== REAL ORDERS ===")
for name, oid in real_orders.items():
    try:
        order = client.get_order(oid)
        status = order.get("status", "?")
        size_matched = order.get("size_matched", "?")
        original_size = order.get("original_size", "?")
        otype = order.get("type", "?")
        print(f"  {name:25} status={status:10} matched={size_matched}/{original_size} type={otype}")
    except Exception as e:
        print(f"  {name:25} ERROR: {e}")
