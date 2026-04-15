"""
Fast order submission — signal queue architecture for sub-50ms execution.

The order thread blocks on a queue. When a signal arrives (from WS callback
or tick loop), it's put on the queue. The thread wakes, signs, POSTs in ~34ms.
Between signals, the thread pings /time to keep the connection warm.

Usage:
    fast = FastOrderClient(private_key, safe_address, ...)
    fast.submit_order(token_id, price, size, callback)  # Non-blocking, ~0ms
    result = await fast.place_order(token_id, price, size)  # Async, ~34ms
"""

import asyncio
import base64
import concurrent.futures
import hashlib
import hmac as hmac_lib
import json
import logging
import queue
import time
import threading
from math import floor, ceil
from decimal import Decimal
from typing import Optional, Callable

import httpx
from eth_account import Account

try:
    from py_order_utils.model import OrderData, BUY, SELL, POLY_GNOSIS_SAFE
except ImportError:
    OrderData = BUY = SELL = POLY_GNOSIS_SAFE = None

logger = logging.getLogger(__name__)

# Polymarket CTF Exchange on Polygon (from py_clob_client.config)
EXCHANGE_ADDRESS = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
NEG_RISK_EXCHANGE_ADDRESS = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
CHAIN_ID = 137


class FastOrderClient:
    """Minimal order client — signal queue, dedicated thread, sub-50ms."""

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
        self._running = True

        # Order HTTP client — ONLY touched by the signal loop thread
        self._http = httpx.Client(
            base_url="https://clob.polymarket.com",
            http2=False,
            timeout=10.0,
            limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
        )

        # Keepalive HTTP client — ONLY touched by keepalive thread
        self._http_keepalive = httpx.Client(
            base_url="https://clob.polymarket.com",
            http2=False,
            timeout=10.0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

        # Signal queue — WS callback and tick loop put work here
        self._signal_queue = queue.Queue()

        # Signal loop thread — blocks on queue, processes orders
        self._signal_thread = threading.Thread(
            target=self._signal_loop, daemon=True, name="fastorder-signal"
        )

        # Keepalive thread
        self._keepalive_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="fastorder-ping"
        )

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

        # Warm up order connection, then start signal loop
        try:
            self._http.get("/time")
            logger.info("FastOrder order connection warmed up")
        except Exception:
            pass
        self._signal_thread.start()

        # Warm up keepalive connection
        self._keepalive_executor.submit(self._keepalive_sync)

        logger.info(f"FastOrderClient initialized (signal queue): EOA={self._eoa_address[:10]}...")

    def _signal_loop(self):
        """
        Runs in dedicated thread forever. Blocks on queue, processes signals.
        When idle, pings /time every 1s to keep connection warm (22ms on warm, 300ms on cold).
        """
        while self._running:
            try:
                # Block for up to 1s — ping frequently to keep connection warm
                try:
                    task = self._signal_queue.get(timeout=1.0)
                except queue.Empty:
                    # No signal — keep order connection warm
                    try:
                        t0 = time.monotonic()
                        self._http.get("/time")
                        ms = (time.monotonic() - t0) * 1000
                        logger.info(f"FastOrder order-ping: {ms:.0f}ms")
                    except Exception:
                        pass
                    continue

                if task is None:
                    break  # Shutdown sentinel

                # Execute the task (a callable)
                try:
                    task()
                except Exception as e:
                    logger.error(f"Signal loop task error: {e}")

            except Exception as e:
                logger.error(f"Signal loop error: {e}")
                time.sleep(0.1)

    def submit_order(
        self,
        token_id: str,
        price: float,
        size: float,
        side: str = "BUY",
        order_type: str = "FAK",
        fee_rate_bps: int = 1000,
        callback: Callable = None,
    ):
        """
        Non-blocking order submission. Puts work on signal queue.
        callback(result_dict) called from signal thread when done.
        Returns immediately (~0ms).
        """
        def _task():
            result = self._place_order_sync(
                token_id, price, size, side, order_type, fee_rate_bps,
            )
            if callback:
                try:
                    callback(result)
                except Exception as e:
                    logger.error(f"Order callback error: {e}")

        self._signal_queue.put(_task)

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
        Async order placement via signal queue.
        Non-blocking — event loop is free while order thread works.
        """
        result_future = concurrent.futures.Future()

        def _task():
            try:
                result = self._place_order_sync(
                    token_id, price, size, side, order_type, fee_rate_bps,
                )
                result_future.set_result(result)
            except Exception as e:
                result_future.set_exception(e)

        self._signal_queue.put(_task)

        # Wait without blocking event loop — poll the future
        loop = asyncio.get_event_loop()
        return await asyncio.wrap_future(result_future, loop=loop)

    @staticmethod
    def _get_amounts(side: str, size: float, price: float):
        """Calculate maker/taker amounts — matches SDK's exact rounding."""

        def _round_normal(x, d):
            return round(x * (10**d)) / (10**d)
        def _round_down(x, d):
            return floor(x * (10**d)) / (10**d)
        def _round_up(x, d):
            return ceil(x * (10**d)) / (10**d)
        def _decimal_places(x):
            return abs(Decimal(str(x)).as_tuple().exponent)
        def _to_token_decimals(x):
            f = (10**6) * x
            if _decimal_places(f) > 0:
                f = _round_normal(f, 0)
            return int(f)

        raw_price = _round_normal(price, 2)
        if side.upper() == "BUY":
            raw_taker = _round_down(size, 2)
            raw_maker = raw_taker * raw_price
            if _decimal_places(raw_maker) > 4:
                raw_maker = _round_up(raw_maker, 8)
                if _decimal_places(raw_maker) > 4:
                    raw_maker = _round_down(raw_maker, 4)
        else:
            raw_maker = _round_down(size, 2)
            raw_taker = raw_maker * raw_price
            if _decimal_places(raw_taker) > 4:
                raw_taker = _round_up(raw_taker, 8)
                if _decimal_places(raw_taker) > 4:
                    raw_taker = _round_down(raw_taker, 4)
        return _to_token_decimals(raw_maker), _to_token_decimals(raw_taker)

    def _sign_order(self, token_id: str, maker_amount: int, taker_amount: int,
                    side: str, fee_rate_bps: int = 1000, expiration: int = 0,
                    nonce: int = 0):
        """Sign an order using py_order_utils."""
        if not self._order_builder:
            raise RuntimeError("Order builder not available")
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
        expiration: int = 0,
    ) -> dict:
        """Synchronous order: sign → HMAC → POST. ~27ms on warm connection."""
        t0 = time.monotonic()

        maker_amount, taker_amount = self._get_amounts(side, size, price)
        try:
            signed = self._sign_order(
                token_id, maker_amount, taker_amount,
                side, fee_rate_bps=fee_rate_bps,
                expiration=expiration,
            )
        except Exception as e:
            logger.error(f"FastOrder signing failed: {e}")
            return {"success": False, "error": str(e)}

        t_sign = time.monotonic()

        # GTD orders are maker-only (postOnly=True, zero taker fees)
        is_maker_order = order_type.upper() in ("GTD", "GTC")

        body = {
            "order": signed.dict(),
            "owner": self._l2_api_key,
            "orderType": order_type.upper(),
            "postOnly": is_maker_order,
        }
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False)
        headers = self._build_headers("POST", "/order", body_str)

        t_prep = time.monotonic()

        try:
            resp = self._http.post("/order", headers=headers, content=body_str)
            result = resp.json()
        except Exception as e:
            logger.error(f"FastOrder POST failed: {e}")
            return {"success": False, "error": str(e)}

        t_post = time.monotonic()
        total_ms = (t_post - t0) * 1000
        sign_ms = (t_sign - t0) * 1000
        net_ms = (t_post - t_prep) * 1000

        logger.info(
            f"FastOrder {side} {size}@{price} {order_type}: "
            f"total={total_ms:.0f}ms (sign={sign_ms:.0f}ms net={net_ms:.0f}ms) "
            f"RAW={json.dumps(result)[:200]} "
            f"success={result.get('success')} id={result.get('orderID','')[:16]}"
        )
        return result

    def _keepalive_sync(self):
        """Keepalive ping on the keepalive client (separate thread)."""
        try:
            t0 = time.monotonic()
            self._http_keepalive.get("/time")
            ms = (time.monotonic() - t0) * 1000
            logger.info(f"FastOrder keepalive: {ms:.0f}ms")
        except Exception as e:
            logger.warning(f"FastOrder keepalive failed: {e}")

    async def keepalive(self):
        """Ping keepalive client (order client warmed by signal loop idle ping)."""
        self._keepalive_executor.submit(self._keepalive_sync)

    async def close(self):
        """Shutdown signal loop and close connections."""
        self._running = False
        self._signal_queue.put(None)  # Shutdown sentinel
        self._signal_thread.join(timeout=5)
        self._http.close()
        self._http_keepalive.close()
        self._keepalive_executor.shutdown(wait=False)
