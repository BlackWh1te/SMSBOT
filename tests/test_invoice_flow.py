"""CryptoBot / xRocket top-ups: request format, invoice screen, exactly-once credit, amount checks, watcher."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update

from bot import currency, payments
from bot.db.models import Payment, utcnow
from bot.handlers import topup
from bot.i18n import t
from bot.payments import invoice_flow
from bot.payments.invoice_flow import CryptoBotClient, XRocketClient
from tests.conftest import OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks
from tests.test_api_clients import fake_server, ok  # noqa: F401  (pytest fixture + helper)


async def enable(session, code):
    await payments.set_credential(session, code, "api_token", "TOKEN-" + code)
    assert await payments.set_enabled(session, code, True)
    return await payments.get_state(session, code)


async def enter_amount(session, make_state, make_message, code, text):
    state = make_state()
    await state.set_state(topup.TopUp.amount)
    await state.update_data(code=code)
    msg = make_message(text)
    await topup.amount_input(msg, session, state)
    return state, msg


class TestCryptoBotClient:
    async def test_create_is_a_rub_invoice_with_token_header(self, fake_server):
        fake_server.routes[("GET", "/createInvoice")] = ok(
            {"ok": True, "result": {"invoice_id": 42, "bot_invoice_url": "https://t.me/CryptoBot?start=IVabc"}})
        inv = await CryptoBotClient("TOK", base=fake_server.base).create(20.0, "tg1-x", "Top-up")
        assert inv == {"id": "42", "url": "https://t.me/CryptoBot?start=IVabc"}
        req = fake_server.requests[0]
        assert req["headers"]["Crypto-Pay-API-Token"] == "TOK"
        assert req["query"]["currency_type"] == "fiat" and req["query"]["fiat"] == "RUB"
        assert req["query"]["amount"] == "20.00" and req["query"]["payload"] == "tg1-x"

    @pytest.mark.parametrize("reply", [{"ok": False, "error": {"code": 400, "name": "AMOUNT_TOO_SMALL"}},
                                       {"ok": True, "result": {}}])
    async def test_refused(self, fake_server, reply):
        fake_server.routes[("GET", "/createInvoice")] = ok(reply)
        assert await CryptoBotClient("TOK", base=fake_server.base).create(1, "p", "d") is None

    async def test_statuses(self, fake_server):
        fake_server.routes[("GET", "/getInvoices")] = ok({"ok": True, "result": {"items": [
            {"invoice_id": 42, "status": "paid", "fiat": "RUB", "amount": "20"}]}})
        got = await CryptoBotClient("TOK", base=fake_server.base).statuses(["42", "43"])
        assert got["42"]["status"] == "paid" and "43" not in got
        assert fake_server.requests[0]["query"]["invoice_ids"] == "42,43"

    async def test_network_error_is_not_an_exception(self):
        assert await CryptoBotClient("TOK", base="http://127.0.0.1:9").statuses(["1"]) == {}


class TestXRocketClient:
    async def test_create_usdt_invoice(self, fake_server):
        fake_server.routes[("POST", "/tg-invoices")] = ok(
            {"success": True, "data": {"id": 7, "link": "https://t.me/xRocket?start=inv_x"}})
        inv = await XRocketClient("KEY", base=fake_server.base).create(0.2356, "tg1-x", "Top-up")
        assert inv == {"id": "7", "url": "https://t.me/xRocket?start=inv_x"}
        req = fake_server.requests[0]
        assert req["headers"]["Rocket-Pay-Key"] == "KEY"
        assert req["json"]["currency"] == "USDT" and req["json"]["amount"] == 0.2356 and req["json"]["numPayments"] == 1

    async def test_status(self, fake_server):
        fake_server.routes[("GET", "/tg-invoices/7")] = ok({"success": True, "data": {"id": 7, "status": "paid"}})
        assert (await XRocketClient("KEY", base=fake_server.base).statuses(["7"]))["7"]["status"] == "paid"


class TestInvoiceScreen:
    @pytest.mark.parametrize("code", ["cryptobot", "xrocket"])
    async def test_twenty_rubles_is_enough(self, session, make_user, make_state, make_message, fake_invoices, code):
        """The customer's requirement: even a 20 RUB purchase can be paid for (tests run at 1 RUB = 1 $)."""
        await make_user(currency="USD")
        await enable(session, code)
        state, msg = await enter_amount(session, make_state, make_message, code, "20")
        kb = msg.answer.await_args.kwargs["reply_markup"]
        assert kb.inline_keyboard[0][0].url.startswith("https://t.me/pay?start=IV")
        assert callbacks(kb)[1].startswith("inv:chk:")
        assert "20.00 $" in msg.answer.await_args.args[0] and await state.get_state() is None
        pay = (await session.execute(select(Payment))).scalar_one()
        assert pay.payment_id == f"{code}:101" and pay.amount_rub == 20 and pay.status == "active"

    async def test_cryptobot_is_priced_in_rubles_xrocket_in_usdt(self, session, make_user, fake_invoices, monkeypatch):
        monkeypatch.setitem(currency._manual, "USD", 85.0)
        await make_user()
        await invoice_flow.create_invoice(session, TEST_USER_ID, await enable(session, "cryptobot"), 20.0)
        await invoice_flow.create_invoice(session, TEST_USER_ID, await enable(session, "xrocket"), 20.0)
        assert fake_invoices.created[0][0] == 20.0                   # RUB
        assert fake_invoices.created[1][0] == round(20 / 85, 4)      # USDT

    async def test_open_invoice_cap(self, session, make_user, make_state, make_message, fake_invoices):
        await make_user()
        await enable(session, "cryptobot")
        for _ in range(invoice_flow.MAX_OPEN_INVOICES):
            await enter_amount(session, make_state, make_message, "cryptobot", "1")
        _, msg = await enter_amount(session, make_state, make_message, "cryptobot", "1")
        assert msg.answer.await_args.args[0] == t("inv_too_many", "ru")
        assert len(fake_invoices.created) == invoice_flow.MAX_OPEN_INVOICES

    async def test_provider_refuses_keeps_amount_step(self, session, make_user, make_state, make_message, fake_invoices):
        await make_user()
        await enable(session, "cryptobot")
        fake_invoices.refuse = True
        state, msg = await enter_amount(session, make_state, make_message, "cryptobot", "1")
        assert msg.answer.await_args.args[0] == t("topup_create_failed", "ru")
        assert await state.get_state() == topup.TopUp.amount.state


async def _invoice(session, code="cryptobot", user_id=TEST_USER_ID, rub=20.0, usd=0.2353):
    state = await enable(session, code)
    pay, _ = await invoice_flow.create_invoice(session, user_id, state, rub)
    await session.execute(update(Payment).where(Payment.id == pay.id).values(amount_usd=usd))
    await session.commit()
    return await session.get(Payment, pay.id, populate_existing=True)


class TestCrediting:
    async def test_paid_is_credited_once(self, session, make_user, reload_user, fake_invoices):
        await make_user(balance=5)
        pay = await _invoice(session)
        fake_invoices.invoices["101"] = {"status": "paid", "fiat": "RUB", "amount": "20.00"}
        assert [p.id for p in await invoice_flow.refresh(session, [pay])] == [pay.id]
        assert await invoice_flow.refresh(session, [pay]) == []
        assert (await reload_user()).balance == 25

    async def test_concurrent_checks_credit_once(self, session_factory, session, make_user, reload_user, fake_invoices):
        await make_user(balance=0)
        pay = await _invoice(session)
        fake_invoices.invoices["101"] = {"status": "paid", "fiat": "RUB", "amount": "20"}

        async def check():
            async with session_factory() as s:
                return await invoice_flow.refresh(s, [await s.get(Payment, pay.id)])

        results = await asyncio.gather(check(), check())
        assert sum(len(r) for r in results) == 1 and (await reload_user()).balance == 20

    @pytest.mark.parametrize("inv", [{"status": "paid", "fiat": "RUB", "amount": "2.00"},
                                     {"status": "paid", "fiat": "USD", "amount": "20.00"},
                                     {"status": "paid"}])
    async def test_wrong_amount_is_never_credited(self, session, make_user, reload_user, fake_invoices, inv):
        await make_user(balance=0)
        pay = await _invoice(session)
        fake_invoices.invoices["101"] = inv
        assert await invoice_flow.refresh(session, [pay]) == []
        assert (await reload_user()).balance == 0

    async def test_xrocket_checks_usdt_amount(self, session, make_user, reload_user, fake_invoices):
        await make_user(balance=0)
        pay = await _invoice(session, code="xrocket", usd=0.2353)
        fake_invoices.invoices["101"] = {"status": "paid", "currency": "USDT", "amount": 0.2}
        assert await invoice_flow.refresh(session, [pay]) == []
        fake_invoices.invoices["101"] = {"status": "paid", "currency": "USDT", "amount": 0.2353}
        assert len(await invoice_flow.refresh(session, [pay])) == 1
        assert (await reload_user()).balance == 20

    async def test_expired(self, session, make_user, fake_invoices):
        await make_user()
        pay = await _invoice(session)
        fake_invoices.invoices["101"] = {"status": "expired"}
        await invoice_flow.refresh(session, [pay])
        assert (await session.get(Payment, pay.id, populate_existing=True)).status == "expired"


class TestCheckButtonAndWatcher:
    async def test_check_button(self, session, make_user, make_call, fake_invoices):
        await make_user()
        pay = await _invoice(session)
        call = make_call(f"inv:chk:{pay.id}")
        await topup.inv_check(call, session)
        assert alert_text(call) == t("np_status_waiting", "ru")
        fake_invoices.invoices["101"] = {"status": "paid", "fiat": "RUB", "amount": "20"}
        call = make_call(f"inv:chk:{pay.id}")
        await topup.inv_check(call, session)
        assert alert_text(call) == t("np_paid", "ru", amount="20.00 $")

    @pytest.mark.parametrize("data", ["inv:chk:999", "inv:chk:abc"])
    async def test_check_unknown(self, session, make_user, make_call, data):
        await make_user()
        call = make_call(data)
        await topup.inv_check(call, session)
        assert alert_text(call) == t("topup_method_unavailable", "ru")

    async def test_cannot_check_someone_elses_invoice(self, session, make_user, make_call, fake_invoices,
                                                      reload_user):
        await make_user()
        await make_user(OTHER_USER_ID, balance=0)
        pay = await _invoice(session, user_id=OTHER_USER_ID)
        fake_invoices.invoices["101"] = {"status": "paid", "fiat": "RUB", "amount": "20"}
        call = make_call(f"inv:chk:{pay.id}")
        await topup.inv_check(call, session)
        assert alert_text(call) == t("topup_method_unavailable", "ru")
        assert (await reload_user(OTHER_USER_ID)).balance == 0

    async def test_watcher_credits_and_notifies(self, session, session_factory, make_user, reload_user, fake_invoices):
        await make_user(balance=0)
        await _invoice(session)
        fake_invoices.invoices["101"] = {"status": "paid", "fiat": "RUB", "amount": "20"}
        notify = AsyncMock()
        assert await invoice_flow.watch_once(session_factory, notify) == 1
        assert notify.await_args.args[0] == TEST_USER_ID
        assert await invoice_flow.watch_once(session_factory, notify) == 0
        assert (await reload_user()).balance == 20

    async def test_watcher_ignores_old_and_nowpayments_rows(self, session, make_user, fake_invoices):
        await make_user()
        pay = await _invoice(session)
        await session.execute(update(Payment).where(Payment.id == pay.id).values(created_at=utcnow() - timedelta(days=1)))
        session.add(Payment(user_id=TEST_USER_ID, payment_id="555", amount_usd=1, amount_rub=90, status="waiting"))
        await session.commit()
        assert await invoice_flow.pending(session) == []
