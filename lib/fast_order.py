"""
Fast order submission — bypasses py-clob-client SDK for minimum latency.

The SDK's create_order makes up to 3 HTTP lookups (tick_size, neg_risk, fee_rate)
and re-instantiates objects on every call. This module does:
  sign → HMAC → POST in one shot, ~10-20ms total (vs ~100-300ms via SDK).

Usage:
    fast = FastOrderClient(private_key, safe_address, api_key, api_secret, api_passphrase)
    result = await fast.place_order(token_id, price, size, side="BUY", order_type="FAK")
"""

import base64
import hashlib
import hmac as hmac_lib
import json
import logging
import os
import time
from typing import Optional

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct

logger = logging.getLogger(__name__)

# Polymarket CTF Exchange on Polygon
EXCHANGE_ADDRESS = "0x4d97DCd97fC247f8E344d3b8e16055CBDf569897"
CHAIN_ID = 137
CLOB_URL = "https://clob.polymarket.com"

# EIP-712 domain separator (static for Polygon mainnet)
DOMAIN = {
    "name": "Polymarket CTF Exchange",
    "version": "1",
    "chainId": CHAIN_ID,
    "verifyingContract": EXCHANGE_ADDRESS,
}

# Pre-computed EIP-712 type hash for Order struct
# keccak256("Order(uint256 salt,address maker,address signer,address taker,uint256 tokenId,uint256 makerAmount,uint256 takerAmount,uint256 expiration,uint256 nonce,uint256 feeRateBps,uint8 side,uint8 signatureType)")
# We use py_order_utils for the actual signing since the struct encoding is complex


class FastOrderClient:
    """Minimal order client that bypasses SDK overhead."""

    def __init__(
        self,
        private_key: str,
        safe_address: str,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
    ):
        self._private_key = private_key
        self._account = Account.from_key(private_key)
        self._eoa_address = self._account.address
        self._safe_address = safe_address
        self._api_key = api_key
        self._api_secret = api_secret
        self._api_passphrase = api_passphrase

        # Persistent async HTTP client — reuses TCP+TLS connection
        self._http = httpx.AsyncClient(
            base_url="https://clob.polymarket.com",
            http2=False,
            timeout=10.0,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
        )

        # Pre-signed order cache: {(token_id, price_cents): signed_order}
        self._pre_signed: dict = {}
        self._pre_sign_lock = False

        # Cache the order builder from py_order_utils (reuse, don't recreate)
        try:
            from py_order_utils.builders import OrderBuilder as UtilsOrderBuilder
            from py_order_utils.signer import Signer as UtilsSigner

            self._order_builder = UtilsOrderBuilder(
                EXCHANGE_ADDRESS, CHAIN_ID, UtilsSigner(key=private_key)
            )
        except Exception as e:
            self._order_builder = None
            logger.warning(f"py_order_utils not available — fast_order signing disabled: {e}")

        # Cache for tick sizes
        self._tick_sizes: dict = {}

        # Derive L2 API credentials (same as SDK's create_or_derive_api_creds)
        self._l2_api_key = ""
        self._l2_secret = ""
        self._l2_passphrase = ""
        try:
            from py_clob_client.client import ClobClient as _ClobClient
            temp = _ClobClient("https://clob.polymarket.com", key=private_key, chain_id=CHAIN_ID)
            l2_creds = temp.create_or_derive_api_creds()
            self._l2_api_key = l2_creds.api_key
            self._l2_secret = l2_creds.api_secret
            self._l2_passphrase = l2_creds.api_passphrase
            logger.info(f"L2 creds derived: key={self._l2_api_key[:10]}...")
        except Exception as e:
            logger.warning(f"Failed to derive L2 creds: {e}")

        logger.info(f"FastOrderClient initialized: EOA={self._eoa_address[:10]}...")

    def _get_amounts(self, side: str, size: float, price: float):
        """Calculate maker/taker amounts for a BUY order."""
        # For BUY: maker pays USDC (makerAmount), receives tokens (takerAmount)
        # USDC has 6 decimals, tokens have 6 decimals on Polymarket
        if side.upper() == "BUY":
            raw_taker = size  # tokens we want
            raw_maker = size * price  # USDC we pay
        else:
            raw_maker = size
            raw_taker = size * price

        # Round to 2 decimal places for price, then convert to integer (6 decimals)
        maker_amount = int(round(raw_maker, 4) * 1_000_000)
        taker_amount = int(round(raw_taker, 4) * 1_000_000)

        return maker_amount, taker_amount

    def _sign_order(self, token_id: str, maker_amount: int, taker_amount: int,
                    side: str, fee_rate_bps: int = 0, expiration: int = 0,
                    nonce: int = 0):
        """Sign an order using py_order_utils (cached builder)."""
        if not self._order_builder:
            raise RuntimeError("Order builder not available")

        from py_order_utils.model import OrderData, BUY, SELL, POLY_GNOSIS_SAFE

        if nonce == 0:
            nonce = int(time.time())

        data = OrderData(
            maker=self._safe_address,
            taker="0x0000000000000000000000000000000000000000",
            tokenId=token_id,
            makerAmount=str(maker_amount),
            takerAmount=str(taker_amount),
            side=BUY if side.upper() == "BUY" else SELL,
            feeRateBps=str(fee_rate_bps),
            nonce=str(nonce),
            signer=self._eoa_address,
            expiration=str(expiration),
            signatureType=POLY_GNOSIS_SAFE,
        )

        return self._order_builder.build_signed_order(data)

    def _build_hmac(self, secret: str, timestamp: int, method: str, path: str, body_str: str) -> str:
        """Build HMAC-SHA256 signature."""
        secret_bytes = base64.urlsafe_b64decode(secret)
        message = f"{timestamp}{method}{path}"
        if body_str:
            message += body_str.replace("'", '"')
        h = hmac_lib.new(secret_bytes, message.encode("utf-8"), hashlib.sha256)
        return base64.urlsafe_b64encode(h.digest()).decode("utf-8")

    def _build_headers(self, method: str, path: str, body_str: str) -> dict:
        """Build BOTH L2 + Builder auth headers."""
        timestamp = int(time.time())

        # L2 headers (derived creds) — underscores, not hyphens
        l2_sig = self._build_hmac(self._l2_secret, timestamp, method, path, body_str)
        headers = {
            "POLY_ADDRESS": self._eoa_address,
            "POLY_SIGNATURE": l2_sig,
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_API_KEY": self._l2_api_key,
            "POLY_PASSPHRASE": self._l2_passphrase,
            "Content-Type": "application/json",
        }

        # Builder headers (from env)
        builder_sig = self._build_hmac(self._api_secret, timestamp, method, path, body_str)
        headers["POLY_BUILDER_API_KEY"] = self._api_key
        headers["POLY_BUILDER_PASSPHRASE"] = self._api_passphrase
        headers["POLY_BUILDER_SIGNATURE"] = builder_sig
        headers["POLY_BUILDER_TIMESTAMP"] = str(timestamp)

        return headers

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "FAK",
        fee_rate_bps: int = 0,
    ) -> dict:
        """
        Place an order with minimum latency.
        Returns the raw API response dict.
        """
        t0 = time.monotonic()

        # 1. Calculate amounts (~0.01ms)
        maker_amount, taker_amount = self._get_amounts(side, size, price)

        # 2. Sign order (~5-10ms)
        try:
            signed = self._sign_order(
                token_id, maker_amount, taker_amount,
                side, fee_rate_bps=fee_rate_bps,
            )
        except Exception as e:
            logger.error(f"FastOrder signing failed: {e}")
            return {"success": False, "error": str(e)}

        t_sign = time.monotonic()

        # 3. Build body (~0.01ms) — use .values dict for clean Python types
        v = signed.order.values
        body = {
            "order": {
                "salt": str(v["salt"]),
                "maker": str(v["maker"]),
                "signer": str(v["signer"]),
                "taker": str(v["taker"]),
                "tokenId": str(v["tokenId"]),
                "makerAmount": str(v["makerAmount"]),
                "takerAmount": str(v["takerAmount"]),
                "expiration": str(v["expiration"]),
                "nonce": str(v["nonce"]),
                "feeRateBps": str(v["feeRateBps"]),
                "side": v["side"],
                "signatureType": v["signatureType"],
                "signature": signed.signature,
            },
            "owner": self._api_key,
            "orderType": order_type.upper(),
        }

        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        # 4. Auth headers: L2 + Builder (~0.2ms)
        headers = self._build_headers("POST", "/order", body_str)

        t_prep = time.monotonic()

        # 5. HTTP POST (~1-5ms on Dublin VPS)
        try:
            resp = await self._http.post("/order", headers=headers, content=body_str)
            result = resp.json()
        except Exception as e:
            logger.error(f"FastOrder POST failed: {e}")
            return {"success": False, "error": str(e)}

        t_post = time.monotonic()

        total_ms = (t_post - t0) * 1000
        sign_ms = (t_sign - t0) * 1000
        prep_ms = (t_prep - t_sign) * 1000
        net_ms = (t_post - t_prep) * 1000

        logger.info(
            f"FastOrder {side} {size}@{price} {order_type}: "
            f"total={total_ms:.0f}ms (sign={sign_ms:.0f}ms prep={prep_ms:.0f}ms net={net_ms:.0f}ms) "
            f"success={result.get('success')} id={result.get('orderID','')[:16]}"
        )

        return result

    async def pre_sign_orders(self, token_ids: dict, prices: list = None):
        """Pre-sign orders for active tokens at likely price points.

        Call every ~10s. When signal fires, grab pre-signed order for instant POST.

        Args:
            token_ids: {"up": "token_123", "down": "token_456"}
            prices: list of price points to pre-sign (default: 0.30-0.55 in 0.01 steps)
        """
        if self._pre_sign_lock:
            return
        self._pre_sign_lock = True

        if prices is None:
            prices = [round(p/100, 2) for p in range(30, 56)]  # $0.30-$0.55

        try:
            for side_name, token_id in token_ids.items():
                if not token_id:
                    continue
                for price in prices:
                    try:
                        maker_amount, taker_amount = self._get_amounts("BUY", 5.0, price)
                        signed = self._sign_order(
                            token_id, maker_amount, taker_amount, "BUY"
                        )
                        # Cache with price in cents as key
                        price_key = int(price * 100)
                        self._pre_signed[(token_id, price_key)] = signed
                    except Exception:
                        pass
        finally:
            self._pre_sign_lock = False

    async def place_order_fast(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "FAK",
    ) -> dict:
        """Ultra-fast order — uses pre-signed order if available, skips signing."""
        t0 = time.monotonic()

        price_key = int(price * 100)
        pre_signed = self._pre_signed.get((token_id, price_key))

        if pre_signed and size == 5.0:
            # Use pre-signed order — skip signing entirely
            signed = pre_signed
            t_sign = time.monotonic()
            # Remove from cache (nonce is single-use)
            self._pre_signed.pop((token_id, price_key), None)
        else:
            # Fall back to sign on the fly
            maker_amount, taker_amount = self._get_amounts(side, size, price)
            signed = self._sign_order(token_id, maker_amount, taker_amount, side)
            t_sign = time.monotonic()

        # Build body — use .values dict for clean Python types
        v = signed.order.values
        body = {
            "order": {
                "salt": str(v["salt"]),
                "maker": str(v["maker"]),
                "signer": str(v["signer"]),
                "taker": str(v["taker"]),
                "tokenId": str(v["tokenId"]),
                "makerAmount": str(v["makerAmount"]),
                "takerAmount": str(v["takerAmount"]),
                "expiration": str(v["expiration"]),
                "nonce": str(v["nonce"]),
                "feeRateBps": str(v["feeRateBps"]),
                "side": v["side"],
                "signatureType": v["signatureType"],
                "signature": signed.signature,
            },
            "owner": self._api_key,
            "orderType": order_type.upper(),
        }
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        timestamp = int(time.time())
        hmac_sig = self._build_hmac(timestamp, "POST", "/order", body_str)
        headers = {
            "POLY-ADDRESS": self._eoa_address,
            "POLY-SIGNATURE": hmac_sig,
            "POLY-TIMESTAMP": str(timestamp),
            "POLY-API-KEY": self._api_key,
            "POLY-PASSPHRASE": self._api_passphrase,
            "Content-Type": "application/json",
        }

        t_prep = time.monotonic()

        try:
            resp = await self._http.post("/order", headers=headers, content=body_str)
            result = resp.json()
        except Exception as e:
            return {"success": False, "error": str(e)}

        t_post = time.monotonic()
        total_ms = (t_post - t0) * 1000
        sign_ms = (t_sign - t0) * 1000
        net_ms = (t_post - t_prep) * 1000
        pre = "PRE-SIGNED" if pre_signed else "LIVE-SIGNED"

        logger.info(
            f"FastOrder [{pre}] {side} {size}@{price} {order_type}: "
            f"total={total_ms:.0f}ms (sign={sign_ms:.0f}ms net={net_ms:.0f}ms) "
            f"success={result.get('success')} id={result.get('orderID','')[:16]}"
        )

        return result

    async def keepalive(self):
        """Ping CLOB to keep connection warm."""
        try:
            await self._http.get("/time")
        except Exception:
            pass

    async def close(self):
        """Close the HTTP client."""
        await self._http.aclose()
