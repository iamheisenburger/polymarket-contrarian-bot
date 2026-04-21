#!/usr/bin/env python3
"""Test what happens when GTD crosses the book — does it ghost fill?"""
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

config = Config.from_env()
bot = TradingBot(config=config, private_key=pk)
bal_before = bot.get_usdc_balance()
print(f"Balance BEFORE: ${bal_before:.6f}")

g = GammaClient()
market_data = g.get_market_info("BTC", "5m")
if not market_data:
    print("NO MARKET")
    sys.exit(1)

token_ids = market_data["token_ids"]
up_token = token_ids["up"]
down_token = token_ids["down"]
prices = market_data["prices"]
print(f"Market: {market_data['slug']}")
print(f"UP price: {prices.get('up', '?')}")
print(f"DOWN price: {prices.get('down', '?')}")

client = FastOrderClient(pk, safe, ak, ask_key, ap)

# Find which side has ask BELOW $0.52 (the crossing side)
up_price = float(prices.get("up", 0.5))
down_price = float(prices.get("down", 0.5))

if down_price < 0.52:
    cross_side = "down"
    cross_token = down_token
    cross_price = down_price
    print(f"\nDOWN ask ({cross_price}) < $0.52 — this side would cross the book")
elif up_price < 0.52:
    cross_side = "up"
    cross_token = up_token
    cross_price = up_price
    print(f"\nUP ask ({cross_price}) < $0.52 — this side would cross the book")
else:
    print(f"\nNeither side crosses at $0.52 (UP={up_price}, DOWN={down_price})")
    print("Cannot test crossing scenario right now")
    sys.exit(0)

# Place on the CROSSING side — this should get rejected with "crosses book"
expiry = int(time.time()) + 60
print(f"\n--- PLACING GTD BUY {cross_side.upper()} @ $0.52 (WILL CROSS) ---")
result = client._place_order_sync(cross_token, 0.52, 5.0, "BUY", "GTD", 1000, expiration=expiry)
print(f"\nRAW RESULT (every field):")
for k, v in result.items():
    print(f"  {k}: '{v}' (type={type(v).__name__})")

# Wait and check balance
time.sleep(3)
bal_after = bot.get_usdc_balance()
change = bal_after - bal_before
print(f"\nBalance BEFORE: ${bal_before:.6f}")
print(f"Balance AFTER:  ${bal_after:.6f}")
print(f"Balance CHANGE: ${change:.6f}")

if abs(change) > 0.50:
    print(f"\n*** GHOST FILL CONFIRMED ***")
    print(f"*** 'Crosses book' rejection DID fill our order ***")
    print(f"*** Lost ${abs(change):.2f} on a 'rejected' order ***")
else:
    print(f"\nNo ghost fill — balance unchanged")
    print(f"'Crosses book' rejection is clean — no match happened")
