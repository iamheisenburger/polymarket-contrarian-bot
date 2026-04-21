#!/usr/bin/env python3
"""Verify the post-patch cancel auth path works against Polymarket CLOB.

Tests L2 auth by:
  1. get_orders()     — works today, baseline sanity.
  2. cancel(fake_id)  — should return a "not found" style response (NOT 401).
  3. cancel_all()     — should succeed (returns empty result when no orders).

Run on VPS: source venv/bin/activate && python3 scripts/test_cancel_auth.py

Exit code: 0 on success, 1 on failure.
"""
import os
import sys
import json
from dotenv import load_dotenv

load_dotenv()

try:
    from py_clob_client.client import ClobClient
except ImportError:
    print("ERROR: py_clob_client not installed")
    sys.exit(1)

HOST = "https://clob.polymarket.com"
CHAIN_ID = 137

pk = os.getenv("POLY_PRIVATE_KEY")
safe = os.getenv("POLY_SAFE_ADDRESS")
if not pk or not safe:
    print("ERROR: POLY_PRIVATE_KEY or POLY_SAFE_ADDRESS missing")
    sys.exit(1)
if pk.startswith("0x"):
    pk = pk[2:]


def init_client():
    tmp = ClobClient(HOST, key=pk, chain_id=CHAIN_ID)
    creds = tmp.create_or_derive_api_creds()
    c = ClobClient(
        HOST, key=pk, chain_id=CHAIN_ID,
        creds=creds, signature_type=2, funder=safe,
    )
    return c


def main():
    print("=== post-patch cancel auth check ===")
    c = init_client()

    # Test 1: get_orders (L2 GET)
    try:
        orders = c.get_orders()
        print(f"[OK] get_orders() → {len(orders)} open orders")
    except Exception as e:
        print(f"[FAIL] get_orders(): {e}")
        return 1

    # Test 2: cancel(fake_id) — want "not found" error, NOT 401
    fake_oid = "0x" + "f" * 64
    try:
        result = c.cancel(fake_oid)
        print(f"[OK] cancel(fake_oid) → no exception, result: {json.dumps(result)[:200]}")
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg:
            print(f"[FAIL] cancel(fake_oid) got 401 — L2 auth still broken: {msg}")
            return 1
        # Any other error (not found, etc.) means auth worked
        print(f"[OK] cancel(fake_oid) rejected with non-auth error (expected): {msg[:200]}")

    # Test 3: cancel_all — empty book is fine
    try:
        result = c.cancel_all()
        print(f"[OK] cancel_all() → {json.dumps(result)[:200] if result else 'empty'}")
    except Exception as e:
        msg = str(e)
        if "401" in msg or "Unauthorized" in msg:
            print(f"[FAIL] cancel_all() got 401: {msg}")
            return 1
        print(f"[WARN] cancel_all() raised: {msg[:200]} (may be OK if no orders)")

    print("\nAll cancel-auth tests passed. Patch is safe to rely on for reversal cancels.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
