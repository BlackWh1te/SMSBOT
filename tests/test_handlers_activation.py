"""Number purchase flow: country -> service -> order -> SMS polling -> finish/refund.

This is where real money moves, so the bug tests here matter most.
"""

import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from bot.db.models import Activation
from bot.db.queries import create_activation, get_activation
from bot.handlers import activation
from bot.i18n import t
from tests.conftest import (
    OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks, last_caption, last_markup, order_response,
)


@pytest.fixture
def no_poll(monkeypatch):
    """Stop create_order from spawning the real 15-minute SMS poller."""
    mock = AsyncMock()
    monkeypatch.setattr(activation, "poll_sms", mock)
    return mock


@pytest.fixture
def fast_sleep(monkeypatch):
    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))


async def _count_activations(session, user_id=TEST_USER_ID):
    return (await session.execute(
        select(func.count(Activation.id)).where(Activation.user_id == user_id))).scalar()


async def _activation(session, order_id):
    return await session.get(Activation, (await get_activation(session, order_id)).id, populate_existing=True)


# ─── Navigation ──────────────────────────────────────────────────────────────

class TestNavigation:
    async def test_buy_without_country_shows_alert(self, session, make_user, make_call, fake_getsms):
        await make_user()
        call = make_call("menu:buy")
        await activation.buy_number_menu(call, session)
        assert alert_text(call)
        fake_getsms.get_services.assert_not_awaited()

    async def test_buy_with_country_lists_services(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1, selected_country_name="Country1")
        call = make_call("menu:buy")
        await activation.buy_number_menu(call, session)
        fake_getsms.get_services.assert_awaited_once_with(1)
        cbs = callbacks(last_markup(call))
        assert "service:101" in cbs and "services_page:2" in cbs  # 30 services -> 2 pages

    @pytest.mark.parametrize("requested,shown", [(1, "1/3"), (3, "3/3"), (99, "3/3"), (-5, "1/3")])
    async def test_country_page_is_clamped(self, session, make_user, make_call, fake_getsms, requested, shown):
        await make_user()
        call = make_call(f"countries_page:{requested}")
        await activation.countries_page(call, session)
        texts = [b.text for row in last_markup(call).inline_keyboard for b in row]
        assert shown in texts  # 50 countries / 21 per page = 3 pages

    async def test_empty_country_list_does_not_crash(self, session, make_user, make_call, fake_getsms):
        await make_user()
        fake_getsms.get_countries.return_value = []
        call = make_call("menu:country")
        await activation.country_menu(call, session)
        assert any(b.text == "1/1" for row in last_markup(call).inline_keyboard for b in row)

    async def test_select_country_saves_choice(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user()
        call = make_call("country:7")
        await activation.select_country(call, session)
        u = await reload_user()
        assert (u.selected_country_id, u.selected_country_name) == (7, "Country7")
        fake_getsms.get_services.assert_awaited_once_with(7)

    async def test_select_unknown_country(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user()
        call = make_call("country:9999")
        await activation.select_country(call, session)
        assert alert_text(call)
        assert (await reload_user()).selected_country_id is None

    async def test_service_card(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1, selected_country_name="Country1", balance=77)
        fake_getsms.get_service_price.return_value = 12.5
        fake_getsms.get_service_count.return_value = None
        call = make_call("service:105")
        await activation.show_service_card(call, session)
        assert "Service5" in last_caption(call)
        first_btn = last_markup(call).inline_keyboard[0][0]
        assert first_btn.callback_data == "order:create:105" and "12.5" in first_btn.text and "0" in first_btn.text

    async def test_service_card_no_price(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1)
        fake_getsms.get_service_price.return_value = None
        call = make_call("service:105")
        await activation.show_service_card(call, session)
        assert alert_text(call) == t("error_no_numbers", "ru")
        call.message.edit_caption.assert_not_awaited()

    async def test_operator_menu_requires_country(self, session, make_user, make_call, fake_getsms):
        await make_user()
        call = make_call("operator:select")
        await activation.select_operator_menu(call, session)
        assert alert_text(call)

    async def test_operator_menu_lists_any_first(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1)
        call = make_call("operator:select")
        await activation.select_operator_menu(call, session)
        assert callbacks(last_markup(call))[:4] == [
            "operator:set:any", "operator:set:mts", "operator:set:beeline", "operator:set:megafon"]

    async def test_set_operator(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user()
        await activation.set_operator(make_call("operator:set:mts"), session)
        assert (await reload_user()).selected_operator == "mts"

    async def test_favorite_needs_a_country(self, session, make_user, make_call, fake_getsms):
        await make_user()
        fav = make_call("fav:101")
        await activation.toggle_favorite_service(fav, session)
        assert alert_text(fav) == t("error_select_country", "ru")

    async def test_live_button_is_translated(self, session, make_user, make_call):
        await make_user(language="en")
        live = make_call("live:services")
        await activation.live_count(live, session)
        assert live.answer.await_args.args[0] == t("live_refreshed", "en")


# ─── Ordering ────────────────────────────────────────────────────────────────

class TestCreateOrder:
    async def test_success_charges_user_and_saves_activation(
            self, session, make_user, make_call, fake_getsms, no_poll, reload_user):
        await make_user(balance=100, selected_operator="mts", total_bought=2)
        fake_getsms.get_service_price.return_value = 40
        fake_getsms.create_order.return_value = order_response(order_id=5001, price=40)

        call = make_call("order:create:105")
        await activation.create_order(call, session)
        await asyncio.sleep(0)  # let the scheduled poll task start

        u = await reload_user()
        assert u.balance == 60
        assert u.total_bought == 3
        fake_getsms.create_order.assert_awaited_once_with(105, operator="mts", max_price=40)
        act = await get_activation(session, 5001)
        assert (act.user_id, act.service_name, act.price, act.status) == (TEST_USER_ID, "Telegram", 40, "WAITING")
        assert "+79990001122" in last_caption(call) and "2026-10-09 12:34" in last_caption(call)
        assert "order:finish:5001" in callbacks(last_markup(call))
        no_poll.assert_awaited_once()

    async def test_insufficient_local_balance_never_calls_api(
            self, session, make_user, make_call, fake_getsms, no_poll, reload_user):
        await make_user(balance=10)
        fake_getsms.get_service_price.return_value = 40
        call = make_call("order:create:105")
        await activation.create_order(call, session)

        fake_getsms.create_order.assert_not_awaited()
        assert last_caption(call) == t("insufficient_balance", "ru")
        assert callbacks(last_markup(call))[0] == "order:create:105"  # retry
        assert (await reload_user()).balance == 10

    async def test_no_price_means_no_numbers(self, session, make_user, make_call, fake_getsms, no_poll):
        await make_user(balance=100)
        fake_getsms.get_service_price.return_value = None
        call = make_call("order:create:105")
        await activation.create_order(call, session)
        assert alert_text(call) == t("error_no_numbers", "ru")
        fake_getsms.create_order.assert_not_awaited()

    @pytest.mark.parametrize("code,key", [
        ("INSUFFICIENT_BALANCE", "error_generic"),   # the OWNER's GetSMS account is empty, not the user's
        ("NO_NUMBERS", "error_no_numbers"),
        ("SERVICE_NOT_AVAILABLE", "error_no_numbers"),
        ("SOMETHING_ELSE", "error_generic"),
    ])
    async def test_api_errors_are_mapped_and_nothing_is_charged(
            self, session, make_user, make_call, fake_getsms, no_poll, reload_user, code, key):
        await make_user(balance=100)
        fake_getsms.create_order.return_value = {"ok": False, "error_code": code}
        call = make_call("order:create:105")
        await activation.create_order(call, session)
        assert last_caption(call) == t(key, "ru")
        assert (await reload_user()).balance == 100
        assert await _count_activations(session) == 0
        no_poll.assert_not_awaited()

    async def test_price_increase_never_gives_free_number(
            self, session, make_user, make_call, fake_getsms, no_poll, reload_user):
        await make_user(balance=100)
        fake_getsms.get_service_price.return_value = 90     # quoted
        fake_getsms.create_order.return_value = order_response(price=120)  # actually charged
        await activation.create_order(make_call("order:create:105"), session)

        handed_out = await _count_activations(session)
        charged = 100 - (await reload_user()).balance
        assert charged == handed_out * 120

    async def test_double_click_does_not_buy_two_for_price_of_one(
            self, session_factory, make_user, make_call, fake_getsms, no_poll, reload_user):
        await make_user(balance=50)
        fake_getsms.get_service_price.return_value = 50
        fake_getsms.create_order.side_effect = [order_response(order_id=1, price=50),
                                                order_response(order_id=2, price=50)]

        async def click():
            async with session_factory() as s:   # each update gets its own session in prod
                await activation.create_order(make_call("order:create:105"), s)

        await asyncio.gather(click(), click())
        async with session_factory() as s:
            handed_out = await _count_activations(s)
        charged = 50 - (await reload_user()).balance
        assert charged == handed_out * 50


# ─── SMS polling ─────────────────────────────────────────────────────────────

class TestPollSms:
    async def _setup(self, session, make_user, user_balance=60, price=40):
        await make_user(balance=user_balance, total_activated=0)
        await create_activation(session, user_id=TEST_USER_ID, order_id=5001, service_id=105,
                                service_name="Telegram", phone_number="+7999", price=price)

    async def test_received_sms_is_saved_and_shown(
            self, session, make_user, make_call, fake_getsms, fast_sleep, reload_user):
        await self._setup(session, make_user)
        fake_getsms.get_order.side_effect = [
            {"ok": True, "data": {"status": "WAITING"}},
            {"ok": True, "data": {"status": "RECEIVED", "last_code": "4242", "phone_number": "+7999",
                                  "sms_list": ["old", "Your code is 4242"]}},
        ]
        call = make_call("order:create:105")
        await activation.poll_sms(call, session, 5001, "en")

        act = await _activation(session, 5001)
        assert (act.status, act.sms_code, act.sms_text) == ("RECEIVED", "4242", "Your code is 4242")
        assert (await reload_user()).total_activated == 1
        assert "4242" in last_caption(call)
        assert "order:repeat:5001" in callbacks(last_markup(call))

    async def test_stops_on_api_error(self, session, make_user, make_call, fake_getsms, fast_sleep):
        await self._setup(session, make_user)
        fake_getsms.get_order.return_value = {"ok": False}
        await activation.poll_sms(make_call("x"), session, 5001, "ru")
        assert fake_getsms.get_order.await_count == 1

    async def test_stops_after_order_lifetime(self, session, make_user, make_call, fake_getsms, fast_sleep):
        await self._setup(session, make_user)
        await activation.poll_sms(make_call("x"), session, 5001, "ru")
        assert fake_getsms.get_order.await_count == activation.POLL_ATTEMPTS
        assert activation.POLL_ATTEMPTS * activation.POLL_INTERVAL >= 20 * 60   # GetSMS order lifetime

    async def test_cancelled_status_is_recorded(self, session, make_user, make_call, fake_getsms, fast_sleep):
        await self._setup(session, make_user)
        fake_getsms.get_order.return_value = {"ok": True, "data": {"status": "CANCELLED"}}
        await activation.poll_sms(make_call("x"), session, 5001, "ru")
        assert (await _activation(session, 5001)).status == "CANCELLED"

    async def test_cancelled_order_is_refunded(
            self, session, make_user, make_call, fake_getsms, fast_sleep, reload_user):
        await self._setup(session, make_user, user_balance=60, price=40)
        fake_getsms.get_order.return_value = {"ok": True, "data": {"status": "CANCELLED"}}
        await activation.poll_sms(make_call("x"), session, 5001, "ru")
        assert (await reload_user()).balance == 100

    async def test_sms_text_is_html_escaped(self, session, make_user, make_call, fake_getsms, fast_sleep):
        await self._setup(session, make_user)
        fake_getsms.get_order.return_value = {"ok": True, "data": {
            "status": "RECEIVED", "last_code": "1", "phone_number": "+7", "sms_list": ["code <1> & more"]}}
        call = make_call("x")
        await activation.poll_sms(call, session, 5001, "ru")
        assert "code &lt;1&gt; &amp; more" in last_caption(call)


# ─── Finish / refund / repeat ────────────────────────────────────────────────

class TestFinishOrder:
    async def _own_order(self, session, make_user, balance=60, price=40, status="WAITING"):
        await make_user(balance=balance)
        await create_activation(session, user_id=TEST_USER_ID, order_id=5001, service_id=105,
                                service_name="Telegram", phone_number="+7999", price=price, status=status)

    async def test_finish_without_refund(self, session, make_user, make_call, fake_getsms, reload_user):
        await self._own_order(session, make_user)
        call = make_call("order:finish:5001")
        await activation.finish_order(call, session)
        assert (await reload_user()).balance == 60
        assert (await _activation(session, 5001)).status == "FINISHED"
        assert last_caption(call) == t("order_finished", "ru")

    async def test_finish_with_refund(self, session, make_user, make_call, fake_getsms, reload_user):
        await self._own_order(session, make_user)
        fake_getsms.finish_order.return_value = {"ok": True, "refunded": True}
        call = make_call("order:finish:5001")
        await activation.finish_order(call, session)
        assert (await reload_user()).balance == 100
        assert last_caption(call) == t("order_refunded", "ru")

    async def test_api_failure_changes_nothing(self, session, make_user, make_call, fake_getsms, reload_user):
        await self._own_order(session, make_user)
        fake_getsms.finish_order.return_value = {"ok": False}
        call = make_call("order:finish:5001")
        await activation.finish_order(call, session)
        assert alert_text(call) == t("error_generic", "ru")
        assert (await _activation(session, 5001)).status == "WAITING"

    async def test_cannot_finish_someone_elses_order(
            self, session, make_user, make_call, fake_getsms, reload_user):
        await self._own_order(session, make_user)
        await make_user(OTHER_USER_ID, balance=0)
        fake_getsms.finish_order.return_value = {"ok": True, "refunded": True}
        await activation.finish_order(make_call("order:finish:5001", user_id=OTHER_USER_ID), session)
        fake_getsms.finish_order.assert_not_awaited()
        assert (await reload_user(OTHER_USER_ID)).balance == 0

    async def test_no_double_refund(self, session, make_user, make_call, fake_getsms, reload_user):
        await self._own_order(session, make_user, status="FINISHED")
        fake_getsms.finish_order.return_value = {"ok": True, "refunded": True}
        await activation.finish_order(make_call("order:finish:5001"), session)
        assert (await reload_user()).balance == 60

    async def test_request_another_code_starts_polling(
            self, session, make_user, make_call, fake_getsms, no_poll):
        await self._own_order(session, make_user)
        call = make_call("order:repeat:5001")
        await activation.request_another_code(call, session)
        await asyncio.sleep(0)
        fake_getsms.request_another_code.assert_awaited_once_with(5001)
        no_poll.assert_awaited_once()

    async def test_cannot_repeat_someone_elses_order(self, session, make_user, make_call, fake_getsms, no_poll):
        await self._own_order(session, make_user)
        await make_user(OTHER_USER_ID)
        await activation.request_another_code(make_call("order:repeat:5001", user_id=OTHER_USER_ID), session)
        fake_getsms.request_another_code.assert_not_awaited()

    async def test_repeat_toast_is_translated(self, session, make_user, make_call, fake_getsms, no_poll):
        await self._own_order(session, make_user)
        await make_user(language="en")
        call = make_call("order:repeat:5001")
        await activation.request_another_code(call, session)
        toast = call.answer.await_args.args[0]
        assert not any("а" <= ch <= "я" for ch in toast.lower())


# ─── History ─────────────────────────────────────────────────────────────────

class TestHistory:
    async def test_empty_history(self, session, make_user, make_call):
        await make_user()
        call = make_call("menu:history")
        await activation.show_history(call, session)
        assert last_caption(call) == t("history_empty", "ru")

    async def test_history_lists_only_own_orders(self, session, make_user, make_call):
        await make_user()
        await create_activation(session, user_id=TEST_USER_ID, order_id=1, service_id=1,
                                service_name="Mine", phone_number="+1", price=1)
        await create_activation(session, user_id=OTHER_USER_ID, order_id=2, service_id=1,
                                service_name="NotMine", phone_number="+2", price=1)
        call = make_call("menu:history")
        await activation.show_history(call, session)
        assert "Mine" in last_caption(call) and "NotMine" not in last_caption(call)

    async def test_full_history_fits_caption(self, session, make_user, make_call):
        await make_user()
        for i in range(10):
            await create_activation(session, user_id=TEST_USER_ID, order_id=100000 + i, service_id=1,
                                    service_name="Telegram Messenger", phone_number="+79991234567",
                                    price=123.45, status="FINISHED")
        call = make_call("menu:history")
        await activation.show_history(call, session)
        assert len(last_caption(call)) <= 1024

    async def test_last_activation_waiting_offers_actions(self, session, make_user, make_call):
        await make_user()
        await create_activation(session, user_id=TEST_USER_ID, order_id=7, service_id=1,
                                service_name="S", phone_number="+1", price=1, status="WAITING")
        call = make_call("menu:last_act")
        await activation.last_activation(call, session)
        assert "order:finish:7" in callbacks(last_markup(call))

    async def test_last_activation_finished_has_no_actions(self, session, make_user, make_call):
        await make_user()
        await create_activation(session, user_id=TEST_USER_ID, order_id=7, service_id=1,
                                service_name="S", phone_number="+1", price=1, status="FINISHED")
        call = make_call("menu:last_act")
        await activation.last_activation(call, session)
        assert "order:finish:7" not in callbacks(last_markup(call))
