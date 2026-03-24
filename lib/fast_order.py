"""
Fast order submission — bypasses py-clob-client SDK for minimum latency.

Uses a DEDICATED THREAD with synchronous HTTP client to avoid async event
loop contention from WebSocket feeds. Sign → HMAC → POST in ~27ms total
(7ms sign + 20ms network on warm connection).

Usage:
    fast = FastOrderClient(private_key, safe_address, api_key, api_secret, api_passphrase)
    result = await fast.place_order(token_id, price, size, side="BUY", order_type="FAK")
"""

import asyncio
import base64
import concurrent.futures
import hashlib
import hmac as hmac_lib
import json
import logging
import time
import threading
from typing import Optional

import httpx
from eth_account import Account

logger = logging.getLogger(__name__)

# Polymarket CTF Exchange on Polygon (from py_clob_client.config)
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CHAIN_ID = 137


class FastOrderClient:
    """Minimal order client — dedicated thread, zero event loop contention."""

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

        # SYNCHRONOUS HTTP client — runs in dedicated thread, not event loop
        self._http = httpx.Client(
            base_url="https://clob.polymarket.com",
            http2=False,
            timeout=10.0,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=3),
        )

        # Dedicated single-thread executor — FastOrder owns this thread
        self._executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fastorder"
        )

        # Warm up connection immediately in the dedicated thread
        self._executor.submit(self._warmup).result(timeout=5)

        # Pre-signed order cache
        self._pre_signed: dict = {}
        self._pre_sign_lock = threading.Lock()

        # Cache the order builder from py_order_utils
        try:
            from py_order_utils.builders import OrderBuilder as UtilsOrderBuilder
            from py_order_utils.signer import Signer as UtilsSigner

            self._order_builder = UtilsOrderBuilder(
                EXCHANGE_ADDRESS, CHAIN_ID, UtilsSigner(key=private_key)
            )
        except Exception as e:
            self._order_builder = None
            logger.warning(f"py_order_utils not available: {e}")

        # Derive L2 API credentials
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

        logger.info(f"FastOrderClient initialized (dedicated thread): EOA={self._eoa_address[:10]}...")

    def _warmup(self):
        """Warm up TCP+TLS connection in the dedicated thread."""
        try:
            self._http.get("/time")
            logger.info("FastOrder connection warmed up")
        except Exception as e:
            logger.warning(f"FastOrder warmup failed: {e}")

    def _get_amounts(self, side: str, size: float, price: float):
        """Calculate maker/taker amounts."""
        if side.upper() == "BUY":
            raw_taker = size
            raw_maker = size * price
        else:
            raw_maker = size
            raw_taker = size * price

        maker_amount = int(round(raw_maker, 4) * 1_000_000)
        taker_amount = int(round(raw_taker, 4) * 1_000_000)
        return maker_amount, taker_amount

    def _sign_order(self, token_id: str, maker_amount: int, taker_amount: int,
                    side: str, fee_rate_bps: int = 1000, expiration: int = 0,
                    nonce: int = 0):
        """Sign an order using py_order_utils."""
        if not self._order_builder:
            raise RuntimeError("Order builder not available")

        from py_order_utils.model import OrderData, BUY, SELL, POLY_GNOSIS_SAFE

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
        """Build L2 auth headers."""
        timestamp = int(time.time())
        l2_sig = self._build_hmac(self._l2_secret, timestamp, method, path, body_str)
        return {
            "POLY_ADDRESS": self._eoa_address,
            "POLY_SIGNATURE": l2_sig,
            "POLY_TIMESTAMP": str(timestamp),
            "POLY_API_KEY": self._l2_api_key,
            "POLY_PASSPHRASE": self._l2_passphrase,
            "Content-Type": "application/json",
        }

    def _place_order_sync(
        self, token_id: str, price: float, size: float,
        side: str, order_type: str, fee_rate_bps: int,
    ) -> dict:
        """
        Synchronous order placement — runs in dedicated thread.
        Zero event loop contention. ~27ms total on warm connection.
        """
        t0 = time.monotonic()

        # 1. Sign (~7ms)
        maker_amount, taker_amount = self._get_amounts(side, size, price)
        try:
            signed = self._sign_order(
                token_id, maker_amount, taker_amount,
                side, fee_rate_bps=fee_rate_bps,
            )
        except Exception as e:
            logger.error(f"FastOrder signing failed: {e}")
            return {"success": False, "error": str(e)}

        t_sign = time.monotonic()

        # 2. Build body + headers (~0.1ms)
        body = {
            "order": signed.dict(),
            "owner": self._l2_api_key,
            "orderType": order_type.upper(),
            "postOnly": False,
        }
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        headers = self._build_headers("POST", "/order", body_str)

        t_prep = time.monotonic()

        # 3. HTTP POST (~20ms on warm connection)
        try:
            resp = self._http.post("/order", headers=headers, content=body_str)
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
            f"RAW={json.dumps(result)[:200]} "
            f"success={result.get('success')} id={result.get('orderID','')[:16]}"
        )

        return result

    async def place_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "FAK",
        fee_rate_bps: int = 1000,
    ) -> dict:
        """
        Place an order — runs in dedicated thread, polls for result to avoid
        event loop latency from run_in_executor callback delay.
        """
        result_box = [None]
        done_event = threading.Event()

        def _run():
            result_box[0] = self._place_order_sync(
                token_id, price, size, side, order_type, fee_rate_bps,
            )
            done_event.set()

        self._executor.submit(_run)

        # Tight poll — avoids event loop callback delay (~300ms savings)
        while not done_event.wait(timeout=0.001):  # 1ms poll
            await asyncio.sleep(0)  # yield to event loop
        return result_box[0]

    def _keepalive_sync(self):
        """Keepalive in dedicated thread — keeps TCP+TLS warm."""
        try:
            t0 = time.monotonic()
            self._http.get("/time")
            ms = (time.monotonic() - t0) * 1000
            logger.info(f"FastOrder keepalive: {ms:.0f}ms")
        except Exception as e:
            logger.warning(f"FastOrder keepalive failed: {e}")

    async def keepalive(self):
        """Ping CLOB in dedicated thread to keep connection warm."""
        done = threading.Event()
        self._executor.submit(lambda: (self._keepalive_sync(), done.set()))
        while not done.wait(timeout=0.001):
            await asyncio.sleep(0)


    async def pre_sign_orders(self, token_ids: dict, prices: list = None):
        """Pre-sign orders for active tokens at likely price points."""
        if not self._pre_sign_lock.acquire(blocking=False):
            return

        if prices is None:
            prices = [round(p/100, 2) for p in range(30, 56)]

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
                        price_key = int(price * 100)
                        self._pre_signed[(token_id, price_key)] = signed
                    except Exception:
                        pass
        finally:
            self._pre_sign_lock.release()

    async def close(self):
        """Close the HTTP client and thread pool."""
        self._http.close()
        self._executor.shutdown(wait=False)
