#!/usr/bin/env python3
"""Manual GTD test — place order, inspect every field, check for ghost fill."""
import os, sys, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

from lib.fast_order import FastOrderClient
from src.gamma_client import GammaClient
from src.bot import TradingBot
from src.config import Config

pk = os.environ.get("POLY_PRIVATE_KEY", "")
safe = os.environ.get("POLY_SAFE_ADDRESS", "")
ak = os.environ.get("POLY_BUILDER_API_KEY", "")
ask_key = os.environ.get("POLY_BUILDER_API_SECRET", "")
ap = os.environ.get("POLY_BUILDER_API_PASSPHRASE", "")

# Get balance before
config = Config.from_env()
bot = TradingBot(config=config, private_key=pk)
bal_before = bot.get_usdc_balance()
print(f"Balance BEFORE: ${bal_before:.4f}")

# Get market
g = GammaClient()
market_data = g.get_market_info("BTC", "5m")
if not market_data:
    print("NO MARKET")
    sys.exit(1)

token_ids = market_data["token_ids"]
up_token = token_ids["up"]
down_token = token_ids["down"]
print(f"Market: {market_data['slug']}")
print(f"Prices: {market_data['prices']}")

# Init fast order
client = FastOrderClient(pk, safe, ak, ask_key, ap)

# Place GTD BUY UP at $0.52
expiry = int(time.time()) + 120
print(f"\n--- PLACING GTD BUY UP @ $0.52 ---")
result = client._place_order_sync(up_token, 0.52, 5.0, "BUY", "GTD", 1000, expiration=expiry)
print(f"RAW RESULT:")
for k, v in result.items():
    print(f"  {k}: {v}")

# Check balance after
time.sleep(2)
bal_after = bot.get_usdc_balance()
print(f"\nBalance AFTER: ${bal_after:.4f}")
print(f"Balance CHANGE: ${bal_after - bal_before:.4f}")

if abs(bal_after - bal_before) > 1.0:
    print(f"\n*** BALANCE DROPPED — GHOST FILL DETECTED ***")
    print(f"*** The 'crosses book' rejection FILLED our order ***")
else:
    print(f"\nBalance unchanged — no ghost fill")

# If order went live, cancel it
oid = result.get("orderID", "")
if oid:
    print(f"\nOrder is LIVE: {oid[:30]}...")
    time.sleep(3)
    print(f"Cancelling...")
    try:
        bot.cancel_order_sync(oid) if hasattr(bot, 'cancel_order_sync') else None
    except:
        pass
    bal_final = bot.get_usdc_balance()
    print(f"Balance FINAL: ${bal_final:.4f}")
