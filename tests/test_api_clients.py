"""GetSMS / NowPayments HTTP clients against a real local aiohttp server (no internet)."""

import hashlib
import hmac
import json

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import bot.api.nowpayments as np_module
from bot.api.getsms import GetSMSClient
from bot.api.nowpayments import NowPaymentsClient


@pytest.fixture
async def fake_server():
    """Local HTTP server that records requests; set `routes[(method, path)] = handler`."""
    requests = []
    routes = {}

    async def dispatch(request: web.Request):
        body = await request.read()
        requests.append({
            "method": request.method,
            "path": request.path,
            "query": dict(request.query),
            "headers": dict(request.headers),
            "json": json.loads(body) if body else None,
        })
        handler = routes.get((request.method, request.path))
        if handler is None:
            return web.json_response({"ok": False, "error_code": "NOT_FOUND"}, status=404)
        return handler(request)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", dispatch)
    server = TestServer(app)
    await server.start_server()
    server.requests = requests
    server.routes = routes
    server.base = str(server.make_url("")).rstrip("/")
    yield server
    await server.close()


def ok(payload):
    return lambda _req: web.json_response(payload)


@pytest.fixture
def client(fake_server):
    c = GetSMSClient()
    c.base = fake_server.base
    c.headers = {"Authorization": "Bearer TESTKEY"}
    return c


class TestGetSMSClient:
    async def test_sends_bearer_auth(self, client, fake_server):
        fake_server.routes[("GET", "/account/get")] = ok({"ok": True, "data": {"balance": 12.5}})
        assert await client.get_balance() == 12.5
        assert fake_server.requests[0]["headers"]["Authorization"] == "Bearer TESTKEY"

    async def test_balance_none_when_not_ok(self, client, fake_server):
        fake_server.routes[("GET", "/account/get")] = ok({"ok": False, "error_code": "BAD_KEY"})
        assert await client.get_balance() is None

    async def test_countries_items(self, client, fake_server):
        fake_server.routes[("GET", "/data/countries")] = ok({"ok": True, "items": [{"id": 1, "name": "RU"}]})
        assert await client.get_countries() == [{"id": 1, "name": "RU"}]

    async def test_list_endpoints_default_to_empty(self, client, fake_server):
        fake_server.routes[("GET", "/data/countries")] = ok({"ok": False})
        assert await client.get_countries() == []

    @pytest.mark.parametrize("is_rent,expected", [(False, "0"), (True, "1")])
    async def test_services_rent_flag(self, client, fake_server, is_rent, expected):
        fake_server.routes[("GET", "/data/services/7")] = ok({"ok": True, "items": []})
        await client.get_services(7, is_rent=is_rent)
        assert fake_server.requests[0]["query"] == {"is_rent": expected}

    async def test_price_and_count(self, client, fake_server):
        fake_server.routes[("GET", "/orders/price/5")] = ok({"ok": True, "price": 9.9})
        fake_server.routes[("GET", "/orders/count/5")] = ok({"ok": False})
        assert await client.get_service_price(5) == 9.9
        assert await client.get_service_count(5) is None

    async def test_create_order_body(self, client, fake_server):
        fake_server.routes[("POST", "/orders")] = ok({"ok": True})
        await client.create_order(5, operator="mts")
        await client.create_order(5, max_price=30.0)
        first, second = (r["json"] for r in fake_server.requests)
        assert first == {"service_id": 5, "operator": "mts", "bot_notifications": False}
        assert second["max_price"] == 30.0 and second["operator"] == "any"

    async def test_rent_and_email_bodies(self, client, fake_server):
        fake_server.routes[("POST", "/rent")] = ok({"ok": True})
        fake_server.routes[("POST", "/emails")] = ok({"ok": True})
        await client.create_rental(5, 24, operator="mts")
        await client.create_email("google.com", "gmail.com")
        assert fake_server.requests[0]["json"] == {"service_id": 5, "rent_time": 24, "operator": "mts"}
        assert fake_server.requests[1]["json"] == {"site": "google.com", "domain": "gmail.com"}

    async def test_order_action_paths(self, client, fake_server):
        for path in ["/orders/9/finish", "/orders/9/request-another-code", "/rent/9/finish", "/emails/e1/cancel"]:
            fake_server.routes[("POST", path)] = ok({"ok": True})
        await client.finish_order(9)
        await client.request_another_code(9)
        await client.finish_rental(9)
        await client.cancel_email("e1")
        assert [r["path"] for r in fake_server.requests] == [
            "/orders/9/finish", "/orders/9/request-another-code", "/rent/9/finish", "/emails/e1/cancel",
        ]

    async def test_error_status_with_json_body_is_returned(self, client, fake_server):
        fake_server.routes[("POST", "/orders")] = lambda _r: web.json_response(
            {"ok": False, "error_code": "NO_NUMBERS"}, status=400)
        assert (await client.create_order(1))["error_code"] == "NO_NUMBERS"

    async def test_non_json_response_does_not_raise(self, client, fake_server):
        fake_server.routes[("GET", "/data/countries")] = lambda _r: web.Response(
            text="<html>502 Bad Gateway</html>", status=502, content_type="text/html")
        assert await client.get_countries() == []


class TestNowPayments:
    @pytest.fixture
    def secret(self, monkeypatch):
        monkeypatch.setattr(np_module, "NOWPAYMENTS_IPN_SECRET", "s3cret")
        return "s3cret"

    def _sign(self, secret, payload: bytes):
        return hmac.new(secret.encode(), payload, hashlib.sha512).hexdigest()

    def test_verify_ipn_valid(self, secret):
        body = b'{"payment_id":1,"payment_status":"finished"}'
        assert NowPaymentsClient().verify_ipn(self._sign(secret, body), body) is True

    def test_verify_ipn_tampered(self, secret):
        body = b'{"payment_id":1,"payment_status":"finished"}'
        sig = self._sign(secret, body)
        assert NowPaymentsClient().verify_ipn(sig, body.replace(b"finished", b"failed")) is False

    def test_verify_ipn_wrong_secret(self, secret):
        body = b"{}"
        assert NowPaymentsClient().verify_ipn(self._sign("other", body), body) is False

    def test_verify_ipn_sorts_keys_like_nowpayments(self, secret):
        """Docs: sign JSON.stringify(params, Object.keys(params).sort()) - key order in the body must not matter."""
        unsorted_body = b'{"payment_status":"finished","payment_id":1}'
        canonical = b'{"payment_id":1,"payment_status":"finished"}'
        assert NowPaymentsClient().verify_ipn(self._sign(secret, canonical), unsorted_body) is True

    def test_verify_ipn_rejects_when_secret_not_configured(self, monkeypatch):
        monkeypatch.setattr(np_module, "NOWPAYMENTS_IPN_SECRET", "")
        body = b'{"payment_status":"finished"}'
        forged = hmac.new(b"", body, hashlib.sha512).hexdigest()
        assert NowPaymentsClient().verify_ipn(forged, body) is False

    async def test_create_payment_payload(self, fake_server):
        fake_server.routes[("POST", "/payment")] = ok({"payment_id": "p1"})
        c = NowPaymentsClient()
        c.base = fake_server.base
        res = await c.create_payment(5.0, "usdttrc20", "order-1", ipn_callback_url="https://x/ipn")
        assert res == {"payment_id": "p1"}
        sent = fake_server.requests[0]["json"]
        assert sent == {
            "price_amount": 5.0, "price_currency": "usd", "pay_currency": "usdttrc20",
            "order_id": "order-1", "order_description": "Balance top-up",
            "ipn_callback_url": "https://x/ipn",
        }
