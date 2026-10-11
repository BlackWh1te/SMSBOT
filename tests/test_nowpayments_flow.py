"""NOWPayments top-up: client, invoice creation, exactly-once crediting, watcher, bot screens."""

import asyncio
from datetime import datetime, timedelta

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiohttp import web
from sqlalchemy import func, select

from bot import currency, payments
from bot.api.nowpayments import NowPaymentsClient
from bot.db.models import Payment
from bot.db.queries import get_payment
from bot.handlers import topup
from bot.i18n import t
from bot.payments import nowpayments_flow as np_flow
from tests.conftest import OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks
from tests.test_api_clients import fake_server, ok  # noqa: F401  (pytest fixture + helper)

API_KEY = "NP-TEST-KEY-0000000000"


class FakeNP:
    """Stands in for NowPaymentsClient inside the flow."""

    def __init__(self):
        self.coins = ["btc", "usdttrc20", "ltc", "doge"]
        self.create_resp = {
            "payment_id": "5745459419", "payment_status": "waiting",
            "pay_address": "TXyzAddress123", "pay_amount": 12.345678, "pay_currency": "usdttrc20",
            "payin_extra_id": None, "expiration_estimate_date": "2026-10-11T15:00:22.742Z",
        }
        self.status = {"payment_status": "waiting"}
        self.created = []
        self.checked = []

    async def get_merchant_coins(self):
        return self.coins

    async def create_payment(self, amount_usd, currency, order_id, order_description="", ipn_callback_url=None):
        self.created.append((amount_usd, currency, order_id))
        return self.create_resp

    async def get_payment_status(self, payment_id):
        self.checked.append(payment_id)
        return self.status


@pytest.fixture
def np_fake(monkeypatch):
    fake = FakeNP()
    monkeypatch.setattr(np_flow, "client_for", lambda state: fake)
    return fake


@pytest.fixture
async def np_enabled(session):
    await payments.set_credential(session, "nowpayments", "api_key", API_KEY)
    assert await payments.set_enabled(session, "nowpayments", True)
    return await payments.get_state(session, "nowpayments")


@pytest.fixture
def make_state():
    storage = MemoryStorage()

    def _make(user_id: int = TEST_USER_ID) -> FSMContext:
        return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    return _make


async def _payment(session, user_id=TEST_USER_ID, pid="p1", rub=500.0, status="waiting", created_at=None):
    row = Payment(user_id=user_id, payment_id=pid, amount_usd=rub / 100, amount_rub=rub,
                  currency="btc", status=status, created_at=created_at or datetime.utcnow())
    session.add(row)
    await session.commit()
    return row


async def _noop_notify(*_):
    pass


# ─── HTTP client (real local server) ──────────────────────────────────────────

class TestClient:
    async def test_uses_owner_key_and_documented_payload(self, fake_server):
        fake_server.routes[("POST", "/payment")] = ok({"payment_id": "1", "pay_address": "A"})
        c = NowPaymentsClient(api_key=API_KEY, base=fake_server.base)
        await c.create_payment(5.0, "btc", "tg1-ab")
        req = fake_server.requests[0]
        assert req["headers"]["x-api-key"] == API_KEY
        assert req["json"]["price_currency"] == "usd" and req["json"]["pay_currency"] == "btc"

    async def test_merchant_coins_lowercased(self, fake_server):
        fake_server.routes[("GET", "/merchant/coins")] = ok({"selectedCurrencies": ["BTC", "USDTTRC20"]})
        c = NowPaymentsClient(api_key=API_KEY, base=fake_server.base)
        assert await c.get_merchant_coins() == ["btc", "usdttrc20"]

    async def test_http_error_returns_error_dict(self, fake_server):
        fake_server.routes[("POST", "/payment")] = lambda _r: web.json_response(
            {"statusCode": 400, "message": "amountTo is too small"}, status=400)
        c = NowPaymentsClient(api_key=API_KEY, base=fake_server.base)
        assert (await c.create_payment(0.1, "btc", "x"))["error"] == "amountTo is too small"

    async def test_non_json_does_not_raise(self, fake_server):
        fake_server.routes[("GET", "/payment/1")] = lambda _r: web.Response(
            text="<html>502</html>", status=502, content_type="text/html")
        c = NowPaymentsClient(api_key=API_KEY, base=fake_server.base)
        assert "error" in await c.get_payment_status("1")

    async def test_unreachable_host_does_not_raise(self):
        c = NowPaymentsClient(api_key=API_KEY, base="http://127.0.0.1:9")
        assert (await c.get_payment_status("1"))["error"] == "network error"


# ─── Flow ─────────────────────────────────────────────────────────────────────

class TestFlow:
    def test_rub_per_usd_is_no_longer_a_required_key(self):
        assert [f.key for f in payments.METHODS["nowpayments"].fields if f.required] == ["api_key"]

    async def test_coins_preferred_order_filtered_by_merchant(self, np_enabled, np_fake):
        assert await np_flow.available_coins(np_enabled) == [("btc", "BTC"), ("usdttrc20", "USDT (TRC20)"),
                                                             ("ltc", "LTC")]

    async def test_coins_fallbacks(self, np_enabled, np_fake):
        np_fake.coins = []
        assert await np_flow.available_coins(np_enabled) == np_flow.PREFERRED_COINS
        np_fake.coins = ["doge", "xmr"]
        assert await np_flow.available_coins(np_enabled) == [("doge", "DOGE"), ("xmr", "XMR")]

    async def test_create_invoice_stores_payment(self, session, np_enabled, np_fake, monkeypatch):
        monkeypatch.setitem(currency._manual, "USD", 100.0)        # same rate the user sees
        pay, _ = await np_flow.create_invoice(session, TEST_USER_ID, np_enabled, 1500, "usdttrc20")
        assert (pay.payment_id, pay.amount_rub, pay.amount_usd, pay.currency, pay.status) == \
               ("5745459419", 1500, 15.0, "usdttrc20", "waiting")
        amount_usd, coin, order_id = np_fake.created[0]
        assert (amount_usd, coin) == (15.0, "usdttrc20") and order_id.startswith(f"tg{TEST_USER_ID}-")

    @pytest.mark.parametrize("resp", [{"error": "amountTo is too small"}, {"payment_id": "1"}, {}])
    async def test_create_invoice_refused_stores_nothing(self, session, np_enabled, np_fake, resp):
        np_fake.create_resp = resp
        pay, _ = await np_flow.create_invoice(session, TEST_USER_ID, np_enabled, 1500, "btc")
        assert pay is None
        assert (await session.execute(select(func.count(Payment.id)))).scalar() == 0

    async def test_credit_is_exactly_once(self, session, make_user, reload_user):
        await make_user(balance=100)
        await _payment(session, rub=500)
        assert await np_flow.credit_payment(session, "p1") is not None
        assert await np_flow.credit_payment(session, "p1") is None
        assert (await reload_user()).balance == 600

    async def test_concurrent_credit_only_once(self, session_factory, make_user, reload_user, session):
        """The watcher and the 'check' button can race - the user must be credited once."""
        await make_user(balance=0)
        await _payment(session, rub=500)

        async def credit():
            async with session_factory() as s:
                return await np_flow.credit_payment(s, "p1")

        results = await asyncio.gather(credit(), credit())
        assert sum(r is not None for r in results) == 1
        assert (await reload_user()).balance == 500

    async def test_refresh_finished_credits(self, session, make_user, reload_user, np_enabled, np_fake):
        await make_user(balance=0)
        pay = await _payment(session, rub=500)
        np_fake.status = {"payment_status": "finished"}
        assert await np_flow.refresh_payment(session, pay) == ("finished", True)
        assert (await reload_user()).balance == 500

    async def test_refresh_updates_intermediate_status(self, session, make_user, reload_user, np_enabled, np_fake):
        await make_user(balance=0)
        pay = await _payment(session)
        np_fake.status = {"payment_status": "confirming"}
        assert await np_flow.refresh_payment(session, pay) == ("confirming", False)
        assert (await session.get(Payment, pay.id, populate_existing=True)).status == "confirming"
        assert (await reload_user()).balance == 0

    @pytest.mark.parametrize("status", ["expired", "failed", "partially_paid"])
    async def test_refresh_never_credits_unfinished(self, session, make_user, reload_user, np_enabled, np_fake, status):
        await make_user(balance=0)
        pay = await _payment(session)
        np_fake.status = {"payment_status": status}
        assert (await np_flow.refresh_payment(session, pay))[1] is False
        assert (await reload_user()).balance == 0

    async def test_refresh_api_error_keeps_status(self, session, np_enabled, np_fake):
        pay = await _payment(session)
        np_fake.status = {"error": "network error"}
        assert await np_flow.refresh_payment(session, pay) == ("waiting", False)

    async def test_refresh_without_key_does_not_call_api(self, session, np_fake):
        pay = await _payment(session)
        assert await np_flow.refresh_payment(session, pay) == ("waiting", False)
        assert np_fake.checked == []


class TestWatcher:
    async def test_credits_and_notifies(self, session, session_factory, make_user, reload_user, np_enabled, np_fake):
        await make_user(balance=10)
        await _payment(session, pid="p1", rub=500)
        np_fake.status = {"payment_status": "finished"}
        notified = []

        async def notify(user_id, pay):
            notified.append((user_id, pay.amount_rub))

        assert await np_flow.watch_once(session_factory, notify) == 1
        assert notified == [(TEST_USER_ID, 500)]
        assert (await reload_user()).balance == 510
        # second pass: nothing pending, nothing credited twice
        assert await np_flow.watch_once(session_factory, notify) == 0
        assert (await reload_user()).balance == 510

    async def test_ignores_old_and_finished_payments(self, session, session_factory, np_enabled, np_fake):
        await _payment(session, pid="old", created_at=datetime.utcnow() - timedelta(days=3))
        await _payment(session, pid="done", status="finished")
        await np_flow.watch_once(session_factory, _noop_notify)
        assert np_fake.checked == []

    async def test_one_failing_payment_does_not_stop_others(self, session, session_factory, make_user,
                                                           np_enabled, np_fake, monkeypatch):
        await make_user(balance=0)
        await make_user(OTHER_USER_ID, balance=0)
        await _payment(session, pid="bad", user_id=OTHER_USER_ID)
        await _payment(session, pid="good", user_id=TEST_USER_ID, rub=300)

        async def status(payment_id):
            if payment_id == "bad":
                raise RuntimeError("boom")
            return {"payment_status": "finished"}

        monkeypatch.setattr(np_fake, "get_payment_status", status)
        assert await np_flow.watch_once(session_factory, _noop_notify) == 1


# ─── Bot screens ──────────────────────────────────────────────────────────────

class TestTopUpScreens:
    async def _amount_entered(self, session, make_user, make_state, make_message, amount="1500"):
        await make_user()
        state = make_state()
        await state.set_state(topup.TopUp.amount)
        await state.update_data(code="nowpayments")
        msg = make_message(amount)
        await topup.amount_input(msg, session, state)
        return state, msg

    async def test_amount_then_coin_list(self, session, make_user, make_state, make_message, np_enabled, np_fake):
        state, msg = await self._amount_entered(session, make_user, make_state, make_message)
        text = msg.answer.await_args.args[0]
        assert currency.fmt_amount(1500, "USD") in text
        assert callbacks(msg.answer.await_args.kwargs["reply_markup"]) == [
            "np:coin:btc", "np:coin:usdttrc20", "np:coin:ltc", "menu:topup"]
        assert await state.get_state() == topup.TopUp.coin.state
        assert (await state.get_data())["amount"] == 1500

    async def test_coin_creates_invoice(self, session, make_user, make_state, make_message, make_call,
                                        np_enabled, np_fake):
        state, _ = await self._amount_entered(session, make_user, make_state, make_message)
        call = make_call("np:coin:usdttrc20")
        await topup.np_choose_coin(call, session, state)

        text = call.message.edit_text.await_args.args[0]
        assert "<code>12.345678</code> USDT (TRC20)" in text and "<code>TXyzAddress123</code>" in text
        assert "1500.00 $" in text and "2026-10-11 15:00 UTC" in text
        assert callbacks(call.message.edit_text.await_args.kwargs["reply_markup"]) == [
            "np:chk:5745459419", "menu:main"]
        assert await state.get_state() is None
        assert (await get_payment(session, "5745459419")).user_id == TEST_USER_ID

    async def test_memo_is_shown_when_required(self, session, make_user, make_state, make_message, make_call,
                                               np_enabled, np_fake):
        np_fake.create_resp = {**np_fake.create_resp, "payin_extra_id": "MEMO-42"}
        state, _ = await self._amount_entered(session, make_user, make_state, make_message)
        call = make_call("np:coin:btc")
        await topup.np_choose_coin(call, session, state)
        assert "MEMO-42" in call.message.edit_text.await_args.args[0]

    async def test_refused_invoice_keeps_coin_list(self, session, make_user, make_state, make_message, make_call,
                                                   np_enabled, np_fake):
        np_fake.create_resp = {"error": "amountTo is too small"}
        state, _ = await self._amount_entered(session, make_user, make_state, make_message)
        call = make_call("np:coin:btc")
        await topup.np_choose_coin(call, session, state)
        assert alert_text(call) == t("topup_create_failed", "ru")
        assert await state.get_state() == topup.TopUp.coin.state
        call.message.edit_text.assert_not_awaited()

    @pytest.mark.parametrize("coin", ["BTC!", "../x", "a"])
    async def test_bad_coin_rejected(self, session, make_user, make_state, make_message, make_call,
                                     np_enabled, np_fake, coin):
        state, _ = await self._amount_entered(session, make_user, make_state, make_message)
        call = make_call(f"np:coin:{coin}")
        await topup.np_choose_coin(call, session, state)
        assert alert_text(call) == t("topup_method_unavailable", "ru")
        assert np_fake.created == []

    async def test_method_turned_off_before_coin(self, session, make_user, make_state, make_message, make_call,
                                                 np_enabled, np_fake):
        state, _ = await self._amount_entered(session, make_user, make_state, make_message)
        await payments.set_enabled(session, "nowpayments", False)
        call = make_call("np:coin:btc")
        await topup.np_choose_coin(call, session, state)
        assert alert_text(call) == t("topup_method_unavailable", "ru")
        assert np_fake.created == []

    @pytest.mark.parametrize("status,key", [
        ({"payment_status": "waiting"}, "np_status_waiting"),
        ({"payment_status": "confirming"}, "np_status_confirming"),
        ({"payment_status": "expired"}, "np_status_failed"),
    ])
    async def test_check_button_statuses(self, session, make_user, make_call, np_enabled, np_fake, status, key):
        await make_user()
        await _payment(session)
        np_fake.status = status
        call = make_call("np:chk:p1")
        await topup.np_check(call, session)
        assert alert_text(call) == t(key, "ru")

    async def test_check_button_credits(self, session, make_user, make_call, reload_user, np_enabled, np_fake):
        await make_user(balance=0)
        await _payment(session, rub=750)
        np_fake.status = {"payment_status": "finished"}
        call = make_call("np:chk:p1")
        await topup.np_check(call, session)
        assert alert_text(call) == t("np_paid", "ru", amount="750.00 $")
        assert (await reload_user()).balance == 750

    async def test_partial_payment_mentions_id(self, session, make_user, make_call, np_enabled, np_fake):
        await make_user()
        await _payment(session)
        np_fake.status = {"payment_status": "partially_paid"}
        call = make_call("np:chk:p1")
        await topup.np_check(call, session)
        assert alert_text(call) == t("np_status_partial", "ru", payment_id="p1")

    async def test_cannot_check_someone_elses_payment(self, session, make_user, make_call, np_enabled, np_fake):
        await make_user()
        await make_user(OTHER_USER_ID)
        await _payment(session, user_id=TEST_USER_ID)
        np_fake.status = {"payment_status": "finished"}
        call = make_call("np:chk:p1", user_id=OTHER_USER_ID)
        await topup.np_check(call, session)
        assert alert_text(call) == t("topup_method_unavailable", "ru")
        assert np_fake.checked == []
