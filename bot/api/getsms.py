"""
GetSMS user API client (docs: PROJECT.md -> "Методы API").

Every call returns a dict. Network errors, timeouts and non-JSON replies (e.g. a
502 HTML page from a proxy) become {"ok": False, "error_code": "NETWORK_ERROR"}
instead of exceptions, so a handler never leaves the user with an endless spinner.
"""

import asyncio
import json

import aiohttp
from loguru import logger

from bot.config import GETSMS_API_KEY, GETSMS_BASE_URL

TIMEOUT = aiohttp.ClientTimeout(total=20)
NETWORK_ERROR = {"ok": False, "error_code": "NETWORK_ERROR"}


class GetSMSClient:
    def __init__(self):
        self.base = GETSMS_BASE_URL.rstrip("/")
        self.headers = {"Authorization": f"Bearer {GETSMS_API_KEY}"}

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as s:
                async with s.request(method, f"{self.base}{path}", headers=self.headers, **kwargs) as r:
                    try:
                        data = await r.json(content_type=None)
                    except (json.JSONDecodeError, aiohttp.ContentTypeError, ValueError):
                        logger.warning(f"GetSMS {method} {path}: HTTP {r.status}, non-JSON reply")
                        return dict(NETWORK_ERROR)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"GetSMS {method} {path} failed: {e!r}")
            return dict(NETWORK_ERROR)
        if not isinstance(data, dict):
            return dict(NETWORK_ERROR)
        if not data.get("ok"):
            logger.warning(f"GetSMS {method} {path} error: {data}")
        return data

    async def _get(self, path: str, params: dict = None) -> dict:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, json: dict = None) -> dict:
        return await self._request("POST", path, json=json)

    # ─── Platform ────────────────────────────────────────────────────
    async def get_balance(self) -> float | None:
        data = await self._get("/account/get")
        return data.get("data", {}).get("balance") if data.get("ok") else None

    # ─── Catalog ─────────────────────────────────────────────────────
    async def get_countries(self) -> list[dict]:
        data = await self._get("/data/countries")
        return data.get("items", [])

    async def get_services(self, country_id: int, is_rent: bool = False) -> list[dict]:
        params = {"is_rent": 1 if is_rent else 0}
        data = await self._get(f"/data/services/{country_id}", params=params)
        return data.get("items", [])

    async def get_operators(self, country_id: int) -> list[str]:
        data = await self._get(f"/data/operators/{country_id}")
        return data.get("items", [])

    async def search_services(self, country_id: int, query: str, is_rent: bool = False) -> list[dict]:
        params = {"country_id": country_id, "q": query, "is_rent": 1 if is_rent else 0}
        data = await self._get("/data/search", params=params)
        return data.get("items", [])

    async def get_service_price(self, service_id: int) -> float | None:
        data = await self._get(f"/orders/price/{service_id}")
        return data.get("price") if data.get("ok") else None

    async def get_service_count(self, service_id: int) -> int | None:
        data = await self._get(f"/orders/count/{service_id}")
        return data.get("count") if data.get("ok") else None

    async def get_offers(self, service_id: int) -> dict:
        """{"ok", "service": {...}, "providers": [{"count", "price"}, ...]} - price tiers."""
        return await self._get(f"/orders/offers/{service_id}")

    async def get_price_tiers(self, service_id: int) -> list[dict]:
        """Price tiers sorted cheapest first: [{"price": 90.03, "count": 1701}, ...]."""
        data = await self.get_offers(service_id)
        tiers = []
        for p in data.get("providers") or []:
            try:
                price, count = float(p["price"]), int(p.get("count") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if price > 0 and count > 0:
                tiers.append({"price": price, "count": count})
        return sorted(tiers, key=lambda x: x["price"])

    # ─── Activation Orders ───────────────────────────────────────────
    async def create_order(self, service_id: int, operator: str = "any", max_price: float = None) -> dict:
        body = {"service_id": service_id, "operator": operator, "bot_notifications": False}
        if max_price:
            body["max_price"] = max_price
        return await self._post("/orders", json=body)

    async def get_order(self, order_id: int) -> dict:
        return await self._get(f"/orders/{order_id}")

    async def get_active_orders(self) -> list[dict]:
        data = await self._get("/orders")
        return data.get("items", [])

    async def request_another_code(self, order_id: int) -> dict:
        return await self._post(f"/orders/{order_id}/request-another-code")

    async def finish_order(self, order_id: int) -> dict:
        return await self._post(f"/orders/{order_id}/finish")

    # ─── Rent ────────────────────────────────────────────────────────
    async def create_rental(self, service_id: int, rent_time: int, operator: str = "any") -> dict:
        return await self._post("/rent", json={"service_id": service_id, "rent_time": rent_time, "operator": operator})

    async def get_rental(self, order_id: int) -> dict:
        return await self._get(f"/rent/{order_id}")

    async def finish_rental(self, order_id: int) -> dict:
        return await self._post(f"/rent/{order_id}/finish")

    async def get_rental_price(self, service_id: int, rent_time: int) -> dict:
        return await self._get(f"/rent/price/{service_id}", params={"rent_time": rent_time})

    async def rental_price(self, service_id: int, rent_time: int) -> float | None:
        """Docs: {"ok": true, "data": {"price": 420, "rent_time": 24}}."""
        data = await self.get_rental_price(service_id, rent_time)
        if not data.get("ok"):
            return None
        price = (data.get("data") or {}).get("price", data.get("price"))
        try:
            return float(price) if price is not None else None
        except (TypeError, ValueError):
            return None

    # ─── Email ───────────────────────────────────────────────────────
    async def get_email_domains(self, site: str) -> list[dict]:
        """[{"domain": "gmx.com", "price": 3.39, "count": 733918}, ...]"""
        data = await self._get("/emails/domains", params={"site": site})
        return data.get("items", [])

    async def create_email(self, site: str, domain: str) -> dict:
        """{"ok": true, "order": {"id", "email", "status", "price"}, "balance": ...}"""
        return await self._post("/emails", json={"site": site, "domain": domain})

    async def check_email(self, email_id: str) -> dict:
        """{"ok": true, "order": {"status"}, "result": {"code", "message"}}"""
        return await self._get(f"/emails/{email_id}")

    async def reorder_email(self, email_id: str) -> dict:
        """Paid re-request for the same site+domain: {"ok", "order": {...}, "charged": 65}"""
        return await self._post(f"/emails/{email_id}/reorder")

    async def cancel_email(self, email_id: str) -> dict:
        """Allowed 2 minutes after purchase if nothing arrived: {"ok", "refunded", "refund_amount"}"""
        return await self._post(f"/emails/{email_id}/cancel")


getsms = GetSMSClient()
