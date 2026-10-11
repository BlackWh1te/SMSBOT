"""
Smoke tests against the REAL GetSMS and Telegram APIs (read-only calls only).

Skipped by default. Run with:
    venv\\Scripts\\python.exe -m pytest --live -m live

They never place orders or spend money, and never print secrets.
`getUpdates` is deliberately not called: it conflicts with a running bot.
"""

import pytest

pytestmark = pytest.mark.live


@pytest.fixture
def real_getsms():
    from bot.api.getsms import getsms
    from bot.config import GETSMS_API_KEY
    if not GETSMS_API_KEY:
        pytest.skip("GETSMS_API_KEY not set")
    return getsms


async def _russia(getsms):
    countries = await getsms.get_countries()
    return next((c for c in countries if "росси" in c["name"].lower() or "russia" in c["name"].lower()),
                countries[0])


class TestGetSMSLive:
    async def test_health(self):
        import aiohttp
        from bot.config import GETSMS_BASE_URL
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{GETSMS_BASE_URL.rstrip('/')}/health") as r:
                data = await r.json()
        assert data.get("ok") is True and data.get("status") == "up"

    async def test_api_key_accepted(self, real_getsms):
        balance = await real_getsms.get_balance()
        assert isinstance(balance, (int, float)), "API key rejected"

    async def test_catalog_shape(self, real_getsms):
        countries = await real_getsms.get_countries()
        assert countries and all({"id", "name"} <= set(c) for c in countries[:10])
        services = await real_getsms.get_services((await _russia(real_getsms))["id"])
        assert len(services) >= 5 and all({"id", "name"} <= set(s) for s in services[:10])

    async def test_price_for_a_service(self, real_getsms):
        services = await real_getsms.get_services((await _russia(real_getsms))["id"])
        price = await real_getsms.get_service_price(services[0]["id"])
        assert price is None or price > 0

    async def test_operators_and_active_orders(self, real_getsms):
        assert isinstance(await real_getsms.get_operators((await _russia(real_getsms))["id"]), list)
        assert isinstance(await real_getsms.get_active_orders(), list)


class TestTelegramLive:
    async def test_token_is_valid(self):
        from aiogram import Bot
        from bot.config import BOT_TOKEN
        if not BOT_TOKEN:
            pytest.skip("BOT_TOKEN not set")
        bot = Bot(token=BOT_TOKEN)
        try:
            me = await bot.get_me()
        finally:
            await bot.session.close()
        assert me.is_bot and me.username
