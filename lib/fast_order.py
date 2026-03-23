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
            base_url=CLOB_URL,
            http2=True,
            timeout=10.0,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
        )

        # Cache the order builder from py_order_utils (reuse, don't recreate)
        try:
            from py_order_utils.builders import OrderBuilder as UtilsOrderBuilder
            from py_order_utils.signer import Signer as UtilsSigner
            from py_order_utils.config import get_contract_config

            config = get_contract_config(CHAIN_ID)
            self._order_builder = UtilsOrderBuilder(
                config.exchange, CHAIN_ID, UtilsSigner(key=private_key)
            )
        except Exception:
            self._order_builder = None
            logger.warning("py_order_utils not available — fast_order signing disabled")

        # Cache for tick sizes
        self._tick_sizes: dict = {}

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

        from py_order_utils.model import OrderData

        if nonce == 0:
            nonce = int(time.time())

        data = OrderData(
            maker=self._safe_address,
            taker="0x0000000000000000000000000000000000000000",
            tokenId=token_id,
            makerAmount=str(maker_amount),
            takerAmount=str(taker_amount),
            side="BUY" if side.upper() == "BUY" else "SELL",
            feeRateBps=str(fee_rate_bps),
            nonce=str(nonce),
            signer=self._eoa_address,
            expiration=str(expiration),
            signatureType=2,  # Gnosis Safe
        )

        return self._order_builder.build_signed_order(data)

    def _build_hmac(self, timestamp: int, method: str, path: str, body_str: str) -> str:
        """Build HMAC-SHA256 signature for L2 auth."""
        secret_bytes = base64.urlsafe_b64decode(self._api_secret)
        message = f"{timestamp}{method}{path}{body_str}"
        h = hmac_lib.new(secret_bytes, message.encode("utf-8"), hashlib.sha256)
        return base64.urlsafe_b64encode(h.digest()).decode("utf-8")

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

        # 3. Build body (~0.01ms)
        body = {
            "order": {
                "salt": signed.order.salt,
                "maker": signed.order.maker,
                "signer": signed.order.signer,
                "taker": signed.order.taker,
                "tokenId": signed.order.tokenId,
                "makerAmount": signed.order.makerAmount,
                "takerAmount": signed.order.takerAmount,
                "expiration": signed.order.expiration,
                "nonce": signed.order.nonce,
                "feeRateBps": signed.order.feeRateBps,
                "side": signed.order.side,
                "signatureType": signed.order.signatureType,
                "signature": signed.signature,
            },
            "owner": self._api_key,
            "orderType": order_type.upper(),
        }

        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)

        # 4. HMAC auth (~0.1ms)
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

    async def keepalive(self):
        """Ping CLOB to keep connection warm."""
        try:
            await self._http.get("/time")
        except Exception:
            pass

    async def close(self):
        """Close the HTTP client."""
        await self._http.aclose()
