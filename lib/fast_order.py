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

import socket
import httpx
from eth_account import Account

# Pre-resolve DNS for clob.polymarket.com — avoids DNS lookup on first request.
# Cache the IP so all connections use it directly.
_CLOB_HOST = "clob.polymarket.com"
try:
    _CLOB_IP = socket.getaddrinfo(_CLOB_HOST, 443, socket.AF_INET)[0][4][0]
except Exception:
    _CLOB_IP = None

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

        # Thread-local HTTP clients — each worker thread gets its own
        # warm connection to avoid contention. Created lazily in _get_http().
        self._http_local = threading.local()

        # Keepalive HTTP client — ONLY touched by keepalive thread
        self._http_keepalive = httpx.Client(
            base_url="https://clob.polymarket.com",
            http2=True,
            timeout=10.0,
            limits=httpx.Limits(max_connections=1, max_keepalive_connections=1),
        )

        # Signal queue — WS callback and tick loop put work here
        self._signal_queue = queue.Queue()

        # Parallel signal threads — 7 workers so all coins can fire simultaneously.
        # Each thread gets its own HTTP connection via thread-local storage.
        self._signal_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=7, thread_name_prefix="fastorder-signal"
        )

        # Single keepalive thread pings one connection to detect staleness
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

        # Pre-warm ALL 7 worker thread connections at startup.
        # Without this, the first signal on each thread hits a cold connection
        # (TLS handshake = ~200ms extra). Warming them all eliminates this.
        def _warmup():
            self._get_http()  # Creates + warms connection for this thread
        warmup_futures = [self._signal_pool.submit(_warmup) for _ in range(7)]
        for f in warmup_futures:
            try:
                f.result(timeout=10)
            except Exception:
                pass
        logger.info(f"FastOrder: 7 worker connections pre-warmed (DNS={_CLOB_IP})")

        # Start keepalive pinger
        self._keepalive_executor.submit(self._keepalive_sync)

        logger.info(f"FastOrderClient initialized (parallel, 7 workers, HTTP/2, TCP_NODELAY): EOA={self._eoa_address[:10]}...")

    def _get_http(self) -> httpx.Client:
        """Get or create a thread-local HTTP client. Each thread gets its own
        warm connection to avoid contention when multiple coins fire at once."""
        if not hasattr(self._http_local, 'client') or self._http_local.client is None:
            # Use resolved IP to skip DNS lookup on connection.
            # Host header is still set correctly by httpx for TLS SNI.
            transport = httpx.HTTPTransport(
                http2=True,
                socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
            )
            self._http_local.client = httpx.Client(
                base_url="https://clob.polymarket.com",
                http2=True,
                timeout=10.0,
                limits=httpx.Limits(max_connections=2, max_keepalive_connections=2),
                transport=transport,
            )
            # Warm the new connection immediately
            try:
                self._http_local.client.get("/time")
            except Exception:
                pass
        return self._http_local.client

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
        Non-blocking order submission. Runs in parallel thread pool.
        callback(result_dict) called from worker thread when done.
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

        self._signal_pool.submit(_task)

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

        self._signal_pool.submit(_task)

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

    def presign_order(self, token_id: str, price: float, size: float,
                      side: str, order_type: str = "FAK", fee_rate_bps: int = 1000) -> str:
        """Pre-sign an order and return the body string.
        Call at window open for all likely price points."""
        maker_amount, taker_amount = self._get_amounts(side, size, price)
        signed = self._sign_order(
            token_id, maker_amount, taker_amount,
            side, fee_rate_bps=fee_rate_bps,
        )
        is_maker_order = order_type.upper() in ("GTD", "GTC")
        body = {
            "order": signed.dict(),
            "owner": self._l2_api_key,
            "orderType": order_type.upper(),
            "postOnly": is_maker_order,
        }
        return json.dumps(body, separators=(",", ":"), ensure_ascii=False)

    def presign_price_ladder(self, token_id: str, size: float, side: str,
                              price_min: float, price_max: float, step: float = 0.01,
                              order_type: str = "FAK", fee_rate_bps: int = 1000) -> dict:
        """Pre-sign orders at every price point in a range.
        Returns {price_cents: body_str} for instant POST on signal.

        Example: presign_price_ladder(token_id, 5.0, "BUY", 0.30, 0.70)
        Pre-signs 40 orders in ~360ms. Each POST is then ~23ms."""
        t0 = time.monotonic()
        cache = {}
        price = price_min
        while price <= price_max + 0.001:
            p = round(price, 2)
            key = int(p * 100)  # cents as int for fast lookup
            try:
                cache[key] = self.presign_order(token_id, p, size, side, order_type, fee_rate_bps)
            except Exception as e:
                logger.warning(f"Presign failed at ${p:.2f}: {e}")
            price += step
        total_ms = (time.monotonic() - t0) * 1000
        logger.info(f"Presigned {len(cache)} orders for {token_id[:16]}... {side} "
                     f"${price_min:.2f}-${price_max:.2f} in {total_ms:.0f}ms")
        return cache

    def post_presigned_order(self, body_str: str) -> dict:
        """POST a pre-signed order with fresh HMAC. ~23ms total."""
        t0 = time.monotonic()
        headers = self._build_headers("POST", "/order", body_str)
        try:
            resp = self._get_http().post("/order", headers=headers, content=body_str)
            result = resp.json()
        except Exception as e:
            logger.error(f"FastOrder presigned POST failed: {e}")
            return {"success": False, "error": str(e)}
        total_ms = (time.monotonic() - t0) * 1000
        logger.info(f"FastOrder PRESIGNED: total={total_ms:.0f}ms RAW={json.dumps(result)[:200]}")
        return result

    def post_batch_orders(self, body_strings: list) -> list:
        """POST multiple pre-signed orders in one request. Up to 15 orders.
        Saves Cloudflare overhead (~20ms per extra order vs individual POSTs).
        Each body_string is a JSON-encoded PostOrder object."""
        if not body_strings:
            return []
        t0 = time.monotonic()
        # Build batch payload: array of PostOrder objects
        orders = [json.loads(s) for s in body_strings]
        batch_body = json.dumps(orders, separators=(",", ":"), ensure_ascii=False)
        headers = self._build_headers("POST", "/orders", batch_body)
        try:
            resp = self._get_http().post("/orders", headers=headers, content=batch_body)
            results = resp.json()
        except Exception as e:
            logger.error(f"FastOrder batch POST failed: {e}")
            return [{"success": False, "error": str(e)}] * len(body_strings)
        total_ms = (time.monotonic() - t0) * 1000
        logger.info(f"FastOrder BATCH: {len(body_strings)} orders in {total_ms:.0f}ms")
        if isinstance(results, list):
            return results
        return [results]

    def _place_order_sync(
        self, token_id: str, price: float, size: float,
        side: str, order_type: str, fee_rate_bps: int,
        expiration: int = 0,
    ) -> dict:
        """Synchronous order: sign → HMAC → POST. ~280ms. Falls back if no presigned."""
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
            resp = self._get_http().post("/order", headers=headers, content=body_str)
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
        """Shutdown thread pool and close connections."""
        self._running = False
        self._signal_pool.shutdown(wait=True, cancel_futures=True)
        self._http_keepalive.close()
        self._keepalive_executor.shutdown(wait=False)
