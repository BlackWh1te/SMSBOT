"""Price tiers, restart-safe order sweeper, settlement math, referrals, admin buttons, GetSMS parsers."""

import asyncio
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.filters import CommandObject

from bot.api.getsms import GetSMSClient
from bot.db.queries import (
    add_balance, create_activation, create_rental, deduct_balance, get_activation, settle_charge,
    transition_activation,
)
from bot.handlers import activation, admin, profile, rent, start
from bot.i18n import t
from tests.conftest import ADMIN_ID, OTHER_USER_ID, TEST_USER_ID, callbacks, last_caption, last_markup, order_response
from tests.test_api_clients import fake_server, ok  # noqa: F401  (pytest fixture + helper)


@pytest.fixture
def no_poll(monkeypatch):
    mock = AsyncMock()
    monkeypatch.setattr(activation, "poll_sms", mock)
    return mock


@pytest.fixture
def as_admin(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])


async def _act(session, order_id=5001, user_id=TEST_USER_ID, price=40.0, status="WAITING", created_at=None):
    return await create_activation(session, user_id=user_id, order_id=order_id, service_id=105,
                                   service_name="Telegram", phone_number="+7999", price=price, status=status,
                                   created_at=created_at or datetime.utcnow())


# ─── Atomic money primitives ──────────────────────────────────────────────────

class TestMoney:
    @pytest.mark.parametrize("amount", [0, -5, float("nan"), float("inf"), "10"])
    async def test_invalid_amounts_rejected(self, session, make_user, reload_user, amount):
        await make_user(balance=100)
        assert await add_balance(session, TEST_USER_ID, amount) is False
        assert await deduct_balance(session, TEST_USER_ID, amount) is False
        assert (await reload_user()).balance == 100

    @pytest.mark.parametrize("reserved,actual,start_bal,end_bal,ok_", [
        (40, 40, 60, 60, True),       # exact
        (40, 35, 60, 65, True),       # cheaper -> refund difference
        (40, 50, 60, 50, True),       # pricier, user can cover
        (40, 50, 5, 5, False),        # pricier, user can't cover -> caller must cancel
        (40, None, 60, 60, True),     # provider didn't say -> keep reservation
    ])
    async def test_settle_charge(self, session, make_user, reload_user, reserved, actual, start_bal, end_bal, ok_):
        await make_user(balance=start_bal)
        assert await settle_charge(session, TEST_USER_ID, reserved, actual) is ok_
        assert (await reload_user()).balance == end_bal

    async def test_transition_succeeds_once(self, session):
        await _act(session)
        assert await transition_activation(session, 5001, "CANCELLED") is True
        assert await transition_activation(session, 5001, "CANCELLED") is False


# ─── Price tiers (customer: "all numbers, cheap and expensive") ──────────────

class TestPriceTiers:
    async def test_card_shows_every_tier_cheapest_first(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1)
        fake_getsms.get_offers.return_value = {"ok": True, "providers": [
            {"count": 58753, "price": 94.25}, {"count": 1701, "price": 90.03}, {"count": 0, "price": 80}]}
        call = make_call("service:105")
        await activation.show_service_card(call, session)
        rows = last_markup(call).inline_keyboard
        assert [rows[0][0].callback_data, rows[1][0].callback_data] == ["order:create:105:9003",
                                                                       "order:create:105:9425"]
        assert rows[0][0].text == t("btn_price_tier", "ru", price="90.03 $", count=1701)
        assert all("8000" not in (b.callback_data or "") for r in rows for b in r)   # sold-out tier hidden

    async def test_tier_order_uses_tier_price_as_max_price(self, session, make_user, make_call, fake_getsms,
                                                           no_poll, reload_user):
        await make_user(balance=100)
        fake_getsms.get_offers.return_value = {"ok": True, "providers": [{"count": 5, "price": 94.25}]}
        fake_getsms.create_order.return_value = order_response(price=94.25)
        await activation.create_order(make_call("order:create:105:9425"), session)
        assert fake_getsms.create_order.await_args.kwargs["max_price"] == 94.25
        fake_getsms.get_service_price.assert_not_awaited()
        assert (await reload_user()).balance == 5.75

    async def test_forged_cheap_tier_still_charges_real_price(self, session, make_user, make_call, fake_getsms,
                                                              no_poll, reload_user):
        """A modified client sends order:create:105:1 (0.01 RUB). The user must still pay what GetSMS charged."""
        await make_user(balance=100)
        fake_getsms.create_order.return_value = order_response(price=90)
        await activation.create_order(make_call("order:create:105:1"), session)
        assert (await reload_user()).balance == 10

    async def test_cheaper_than_reserved_refunds_difference(self, session, make_user, make_call, fake_getsms,
                                                            no_poll, reload_user):
        await make_user(balance=100)
        fake_getsms.get_service_price.return_value = 50
        fake_getsms.create_order.return_value = order_response(price=45)
        await activation.create_order(make_call("order:create:105"), session)
        assert (await reload_user()).balance == 55
        assert (await get_activation(session, 5001)).price == 45


# ─── Polling and the restart-safe sweeper ────────────────────────────────────

class TestPollingAndSweeper:
    async def test_one_poller_per_order(self, session, make_call, monkeypatch):
        started = []

        async def fake_poll(call, s, order_id, lang):
            started.append(order_id)
            await asyncio.sleep(10)

        monkeypatch.setattr(activation, "poll_sms", fake_poll)
        first = activation.start_polling(make_call("x"), session, 77, "ru")
        second = activation.start_polling(make_call("x"), session, 77, "ru")
        await asyncio.sleep(0)
        assert first is second and started == [77]
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
        await asyncio.sleep(0)            # done-callbacks run on the next loop tick
        assert 77 not in activation._pollers

    async def test_repeat_code_counts_activation_once(self, session, make_user, reload_user):
        await make_user()
        await _act(session)
        e1, _ = await activation.apply_order_status(session, 5001, {"status": "RECEIVED", "last_code": "111"})
        same, _ = await activation.apply_order_status(session, 5001, {"status": "RECEIVED", "last_code": "111"})
        e2, act = await activation.apply_order_status(session, 5001, {"status": "RECEIVED", "last_code": "222"})
        assert (e1, same, e2) == ("received", None, "received")
        assert act.sms_code == "222"
        assert (await reload_user()).total_activated == 1

    async def test_closed_order_is_not_reopened(self, session, make_user):
        await make_user()
        await _act(session, status="CANCELLED")
        event, _ = await activation.apply_order_status(session, 5001, {"status": "RECEIVED", "last_code": "1"})
        assert event is None
        assert (await get_activation(session, 5001)).status == "CANCELLED"

    async def test_sweeper_refunds_cancelled_orders_after_restart(self, session, session_factory, make_user,
                                                                  reload_user, fake_getsms):
        await make_user(balance=0, language="en")
        await _act(session, price=40)
        fake_getsms.get_order.return_value = {"ok": True, "data": {"status": "CANCELLED"}}
        sent = []

        async def notify(uid, text, kb):
            sent.append((uid, text))

        assert await activation.sweep_orders_once(session_factory, notify) == 1
        assert (await reload_user()).balance == 40
        assert sent and sent[0][0] == TEST_USER_ID and "40" in sent[0][1]
        # second pass: already cancelled, never refunded twice
        assert await activation.sweep_orders_once(session_factory, notify) == 0
        assert (await reload_user()).balance == 40

    async def test_sweeper_delivers_codes(self, session, session_factory, make_user, fake_getsms):
        await make_user()
        await _act(session)
        fake_getsms.get_order.return_value = {"ok": True, "data": {
            "status": "RECEIVED", "last_code": "4242", "sms_list": ["code <4242>"]}}
        sent = []

        async def notify(uid, text, kb):
            sent.append(text)

        await activation.sweep_orders_once(session_factory, notify)
        assert "4242" in sent[0] and "&lt;4242&gt;" in sent[0]

    async def test_sweeper_skips_live_and_old_orders(self, session, session_factory, make_user, fake_getsms):
        await make_user()
        await _act(session, order_id=1)
        await _act(session, order_id=2, created_at=datetime.utcnow() - timedelta(days=2))
        live = asyncio.create_task(asyncio.sleep(10))
        activation._pollers[1] = live
        try:
            await activation.sweep_orders_once(session_factory, AsyncMock())
        finally:
            live.cancel()
            activation._pollers.pop(1, None)
        fake_getsms.get_order.assert_not_awaited()

    async def test_sweeper_survives_api_errors(self, session, session_factory, make_user, fake_getsms):
        await make_user()
        await _act(session)
        fake_getsms.get_order.return_value = {"ok": False, "error_code": "NETWORK_ERROR"}
        assert await activation.sweep_orders_once(session_factory, AsyncMock()) == 0
        assert (await get_activation(session, 5001)).status == "WAITING"


# ─── Forged / garbled callback data ──────────────────────────────────────────

class TestForgedCallbacks:
    @pytest.mark.parametrize("data", ["order:finish:abc", "order:finish:", "order:repeat:9999999"])
    async def test_garbage_order_ids(self, session, make_user, make_call, fake_getsms, data):
        await make_user()
        call = make_call(data)
        handler = activation.finish_order if "finish" in data else activation.request_another_code
        await handler(call, session)
        assert call.answer.await_args.kwargs.get("show_alert")
        fake_getsms.finish_order.assert_not_awaited()
        fake_getsms.request_another_code.assert_not_awaited()

    async def test_garbage_page_number(self, session, make_user, make_call, fake_getsms):
        await make_user()
        call = make_call("countries_page:xyz")
        await activation.countries_page(call, session)
        assert any(b.text == "1/3" for row in last_markup(call).inline_keyboard for b in row)


# ─── Rent ─────────────────────────────────────────────────────────────────────

class TestRentPricing:
    async def test_documented_price_format(self, session, make_user, make_call, fake_getsms):
        """Docs: {"ok": true, "data": {"price": 420}} - the old code read 'price' and always showed '?'."""
        await make_user()
        fake_getsms.get_rental_price.return_value = {"ok": True, "data": {"price": 420, "rent_time": 4}}
        call = make_call("rent_service:101")
        await rent.rent_service_card(call, session)
        assert "420.00 $" in last_markup(call).inline_keyboard[0][0].text

    async def test_unsupported_duration_rejected(self, session, make_user, make_call, fake_getsms):
        await make_user(balance=1000)
        await rent.create_rent(make_call("rent:create:101:5"), session)
        fake_getsms.create_rental.assert_not_awaited()

    async def test_finished_rental_cannot_be_finished_again(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await create_rental(session, user_id=TEST_USER_ID, order_id=7001, service_id=1, service_name="s",
                            phone_number="+7", price=1, status="FINISHED")
        call = make_call("rent:finish:7001")
        await rent.finish_rent(call, session)
        assert call.answer.await_args.args[0] == t("order_already_closed", "ru")
        fake_getsms.finish_rental.assert_not_awaited()


# ─── Referrals ────────────────────────────────────────────────────────────────

def _start(args):
    return CommandObject(prefix="/", command="start", args=args)


class TestReferrals:
    @pytest.mark.parametrize("args,expected", [("ref42", 42), ("ref", None), ("refabc", None), ("x42", None),
                                               (None, None), (f"ref{TEST_USER_ID}", None)])
    def test_parse(self, args, expected):
        assert start.parse_referrer(args, TEST_USER_ID) == expected

    async def test_new_user_is_attributed(self, session, make_user, make_message, reload_user):
        await make_user(OTHER_USER_ID)
        await start.cmd_start(make_message("/start"), session, _start(f"ref{OTHER_USER_ID}"))
        assert (await reload_user()).referral_id == OTHER_USER_ID

    async def test_unknown_referrer_ignored(self, session, make_message, reload_user):
        await start.cmd_start(make_message("/start"), session, _start("ref123456"))
        assert (await reload_user()).referral_id is None

    async def test_existing_user_is_not_reattributed(self, session, make_user, make_message, reload_user):
        await make_user(OTHER_USER_ID)
        await make_user()
        await start.cmd_start(make_message("/start"), session, _start(f"ref{OTHER_USER_ID}"))
        assert (await reload_user()).referral_id is None

    async def test_referrals_screen_counts_invited(self, session, make_user, make_call):
        await make_user()
        for uid in (1, 2, 3):
            await make_user(uid, referral_id=TEST_USER_ID)
        call = make_call("profile:referrals")
        await profile.show_referrals(call, session)
        assert t("referrals_count", "ru", count=3) in last_caption(call)


# ─── Admin buttons ────────────────────────────────────────────────────────────

class TestAdminButtons:
    @pytest.mark.parametrize("button,state", [
        ("admin:add_bal", admin.AdminAction.add_balance), ("admin:rem_bal", admin.AdminAction.remove_balance),
        ("admin:find", admin.AdminAction.find_user), ("admin:broadcast", admin.AdminAction.broadcast)])
    async def test_buttons_open_prompts(self, make_call, make_state, as_admin, button, state):
        st = make_state(ADMIN_ID)
        call = make_call(button, user_id=ADMIN_ID)
        await admin.admin_prompt(call, st)
        assert await st.get_state() == state.state
        call.message.edit_text.assert_awaited_once()

    async def test_non_admin_buttons_do_nothing(self, make_call, make_state, as_admin):
        st = make_state(TEST_USER_ID)
        call = make_call("admin:add_bal", user_id=TEST_USER_ID)
        await admin.admin_prompt(call, st)
        assert await st.get_state() is None
        call.answer.assert_awaited()            # no endless spinner

    async def test_add_balance_via_button(self, session, make_user, make_message, make_state, reload_user, as_admin):
        await make_user(balance=10)
        st = make_state(ADMIN_ID)
        await st.set_state(admin.AdminAction.add_balance)
        msg = make_message(f"{TEST_USER_ID} 90,5", user_id=ADMIN_ID)
        await admin.admin_balance_input(msg, session, st)
        assert (await reload_user()).balance == 100.5
        assert msg.answer.await_args.args[0].endswith("Новый баланс: 100.5")
        assert await st.get_state() is None

    async def test_remove_balance_bad_input_keeps_waiting(self, session, make_user, make_message,
                                                          make_state, reload_user, as_admin):
        await make_user(balance=10)
        st = make_state(ADMIN_ID)
        await st.set_state(admin.AdminAction.remove_balance)
        msg = make_message(f"{TEST_USER_ID} -5", user_id=ADMIN_ID)
        await admin.admin_balance_input(msg, session, st)
        assert "положительным" in msg.answer.await_args.args[0]
        assert await st.get_state() == admin.AdminAction.remove_balance.state
        assert (await reload_user()).balance == 10

    @pytest.mark.parametrize("query", [str(TEST_USER_ID), "@tester", "TESTER"])
    async def test_find_user_by_id_or_username(self, session, make_user, make_message, make_state, as_admin, query):
        await make_user(username="tester", full_name="<i>Bob</i>")
        st = make_state(ADMIN_ID)
        await st.set_state(admin.AdminAction.find_user)
        msg = make_message(query, user_id=ADMIN_ID)
        await admin.admin_find_input(msg, session, st)
        text = msg.answer.await_args.args[0]
        assert str(TEST_USER_ID) in text and "&lt;i&gt;Bob" in text

    async def test_recent_purchases(self, session, make_user, make_call, as_admin):
        await make_user()
        await _act(session)
        await create_rental(session, user_id=TEST_USER_ID, order_id=7001, service_id=1, service_name="WhatsApp",
                            phone_number="+7111", price=200)
        call = make_call("admin:recent", user_id=ADMIN_ID)
        await admin.admin_recent(call, session)
        text = call.message.edit_text.await_args.args[0]
        assert "Telegram" in text and "WhatsApp" in text and "+7111" in text

    async def test_broadcast_counts_failures(self, monkeypatch):
        monkeypatch.setattr(admin, "BROADCAST_DELAY", 0)
        bot = MagicMock()
        bot.copy_message = AsyncMock(side_effect=[None, Exception("bot was blocked"), None])
        bot.send_message = AsyncMock()
        sent, failed = await admin.run_broadcast(bot, [1, 2, 3], from_chat=ADMIN_ID, message_id=9,
                                                 report_chat=ADMIN_ID)
        assert (sent, failed) == (2, 1)
        assert bot.copy_message.await_args_list[0].kwargs == {"chat_id": 1, "from_chat_id": ADMIN_ID, "message_id": 9}
        assert "отправлено 2, ошибок 1" in bot.send_message.await_args.args[1]

    async def test_broadcast_requires_confirmation(self, session, make_user, make_message, make_call, make_state,
                                                   as_admin, monkeypatch):
        await make_user()
        st = make_state(ADMIN_ID)
        await st.set_state(admin.AdminAction.broadcast)
        msg = make_message("Hello everyone", user_id=ADMIN_ID)
        msg.message_id = 55
        await admin.admin_broadcast_input(msg, st)
        assert "admin:bc:send" in callbacks(msg.answer.await_args.kwargs["reply_markup"])

        runs = []

        async def fake_run(bot, user_ids, from_chat, message_id, report_chat):
            runs.append((user_ids, message_id))
            return len(user_ids), 0

        monkeypatch.setattr(admin, "run_broadcast", fake_run)
        await admin.admin_broadcast_send(make_call("admin:bc:send", user_id=ADMIN_ID), session, st)
        await asyncio.sleep(0)
        assert runs == [([TEST_USER_ID], 55)]


# ─── GetSMS client parsers (real local HTTP server) ──────────────────────────

class TestGetSMSParsers:
    @pytest.fixture
    def client(self, fake_server):
        c = GetSMSClient()
        c.base = fake_server.base
        c.headers = {"Authorization": "Bearer TEST"}
        return c

    async def test_price_tiers(self, client, fake_server):
        fake_server.routes[("GET", "/orders/offers/10")] = ok({"ok": True, "providers": [
            {"count": 58753, "price": 94.25}, {"count": 1701, "price": 90.03}, {"count": 5, "price": "bad"}]})
        assert await client.get_price_tiers(10) == [{"price": 90.03, "count": 1701}, {"price": 94.25, "count": 58753}]

    @pytest.mark.parametrize("payload,price", [({"ok": True, "data": {"price": 420}}, 420.0),
                                               ({"ok": True, "price": 99}, 99.0),
                                               ({"ok": False, "error_code": "NO_NUMBERS"}, None)])
    async def test_rental_price(self, client, fake_server, payload, price):
        fake_server.routes[("GET", "/rent/price/5")] = ok(payload)
        assert await client.rental_price(5, 24) == price

    async def test_unreachable_returns_network_error(self):
        c = GetSMSClient()
        c.base = "http://127.0.0.1:9"
        assert (await c.get_order(1))["error_code"] == "NETWORK_ERROR"
