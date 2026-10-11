"""
NOWPayments API client (https://documenter.getpostman.com/view/7907941/S1a32n38).

Keys come from the owner's /admin settings, so a client is created per call:
    NowPaymentsClient(api_key=..., ipn_secret=...)

Network/HTTP problems never raise: every call returns a dict, and errors are
reported as {"error": "..."} so handlers can show a friendly message.
"""

import asyncio
import hashlib
import hmac
import json

import aiohttp
from loguru import logger

from bot.config import NOWPAYMENTS_API_KEY, NOWPAYMENTS_BASE_URL, NOWPAYMENTS_IPN_SECRET

TIMEOUT = aiohttp.ClientTimeout(total=20)

# Statuses from the "Get payment status" docs
STATUS_FINISHED = "finished"
PENDING_STATUSES = {"waiting", "confirming", "confirmed", "sending"}
FINAL_FAILED_STATUSES = {"failed", "refunded", "expired"}
STATUS_PARTIALLY_PAID = "partially_paid"


class NowPaymentsClient:
    def __init__(self, api_key: str | None = None, ipn_secret: str | None = None, base: str | None = None):
        self.base = (base or NOWPAYMENTS_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else NOWPAYMENTS_API_KEY
        self.ipn_secret = ipn_secret
        self.headers = {"x-api-key": self.api_key, "Content-Type": "application/json"}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.request(method, f"{self.base}{path}", headers=self.headers, **kwargs) as r:
                    try:
                        data = await r.json(content_type=None)
                    except (json.JSONDecodeError, aiohttp.ContentTypeError, ValueError):
                        return {"error": f"HTTP {r.status}: non-JSON response"}
                    if r.status >= 400:
                        msg = data.get("message") if isinstance(data, dict) else None
                        return {"error": msg or f"HTTP {r.status}", "status_code": r.status}
                    return data if isinstance(data, dict) else {"data": data}
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"NOWPayments {method} {path} failed: {e!r}")
            return {"error": "network error"}

    async def _get(self, path: str, params: dict = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, data: dict) -> dict:
        return await self._request("POST", path, json=data)

    async def get_merchant_coins(self) -> list[str]:
        """Coins the owner enabled in the NOWPayments dashboard ("coins settings")."""
        data = await self._get("/merchant/coins")
        return [c.lower() for c in data.get("selectedCurrencies") or data.get("currencies") or []]

    async def get_currencies(self) -> list[str]:
        data = await self._get("/currencies")
        return data.get("currencies", [])

    async def get_minimum_amount(self, currency: str) -> float:
        data = await self._get("/min-amount", params={"currency_from": currency, "currency_to": "usd"})
        return data.get("min_amount", 0)

    async def create_payment(
        self,
        amount_usd: float,
        currency: str,
        order_id: str,
        order_description: str = "Balance top-up",
        ipn_callback_url: str = None,
    ) -> dict:
        """POST /payment -> payment_id, pay_address, pay_amount, payin_extra_id, expiration_estimate_date."""
        payload = {
            "price_amount": amount_usd,
            "price_currency": "usd",
            "pay_currency": currency,
            "order_id": order_id,
            "order_description": order_description,
        }
        if ipn_callback_url:
            payload["ipn_callback_url"] = ipn_callback_url
        return await self._post("/payment", payload)

    async def get_payment_status(self, payment_id: str) -> dict:
        return await self._get(f"/payment/{payment_id}")

    def verify_ipn(self, received_hmac: str, payload_bytes: bytes) -> bool:
        """
        IPN signature per docs: HMAC-SHA512 over the JSON body with keys sorted
        alphabetically (JSON.stringify(params, Object.keys(params).sort())).
        Always False when no IPN secret is configured - otherwise anyone could
        sign a fake "finished" notification with an empty key.
        """
        secret = self.ipn_secret if self.ipn_secret is not None else NOWPAYMENTS_IPN_SECRET
        if not secret or not received_hmac:
            return False
        try:
            params = json.loads(payload_bytes)
        except (ValueError, TypeError):
            return False
        canonical = json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        expected = hmac.new(secret.encode(), canonical.encode(), hashlib.sha512).hexdigest()
        return hmac.compare_digest(expected, received_hmac.lower())


nowpayments = NowPaymentsClient()
