"""Abuse protection: purchase limits and price checks, blocked users, flooding, admin block button."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import CallbackQuery, Message

from bot import antiabuse
from bot.db.queries import create_activation, get_activation
from bot.handlers import activation, admin, email, rent
from bot.i18n import t
from bot.middlewares.guard import GuardMiddleware
from tests.conftest import ADMIN_ID, OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks, order_response


@pytest.fixture
def no_poll(monkeypatch):
    monkeypatch.setattr(activation, "poll_sms", AsyncMock())


@pytest.fixture
def tiers(fake_getsms):
    fake_getsms.get_offers.return_value = {"ok": True, "providers": [{"count": 9, "price": 50.0},
                                                                      {"count": 3, "price": 80.0}]}
    return fake_getsms


class TestPriceIsNeverTakenFromTheButton:
    async def test_forged_tier_is_refused_before_any_order(self, session, make_user, make_call, tiers,
                                                           no_poll, reload_user):
        await make_user(balance=100, selected_country_id=1)
        call = make_call("order:create:105:1")                       # 0.01 RUB, not in the price list
        await activation.create_order(call, session)
        tiers.create_order.assert_not_awaited()
        assert alert_text(call) == t("price_changed", "ru")
        assert "order:create:105:5000" in callbacks(call.message.edit_caption.await_args.kwargs["reply_markup"])
        assert (await reload_user()).balance == 100

    async def test_listed_tier_is_used(self, session, make_user, make_call, tiers, no_poll, reload_user):
        await make_user(balance=100)
        tiers.create_order.return_value = order_response(price=80)
        await activation.create_order(make_call("order:create:105:8000"), session)
        assert tiers.create_order.await_args.kwargs["max_price"] == 80
        assert (await reload_user()).balance == 20

    async def test_without_price_list_never_below_default_price(self, session, make_user, make_call, fake_getsms,
                                                                no_poll, reload_user):
        await make_user(balance=100)
        fake_getsms.get_service_price.return_value = 50
        fake_getsms.create_order.return_value = {"ok": True, "order": {"id": 1, "phone_number": "+7"}}  # no price
        await activation.create_order(make_call("order:create:105:1"), session)
        assert fake_getsms.create_order.await_args.kwargs["max_price"] == 50
        assert (await reload_user()).balance == 50                  # charged the verified price, not 0.01


class TestPurchaseLimits:
    async def test_open_numbers_cap(self, session, make_user, make_call, fake_getsms, no_poll):
        await make_user(balance=10_000)
        for i in range(antiabuse.MAX_ACTIVE_ORDERS):
            await create_activation(session, user_id=TEST_USER_ID, order_id=900 + i, service_id=1, service_name="S",
                                    phone_number="+7", price=1, status="WAITING")
        call = make_call("order:create:105")
        await activation.create_order(call, session)
        fake_getsms.create_order.assert_not_awaited()
        assert alert_text(call) == t("too_many_active_orders", "ru", count=antiabuse.MAX_ACTIVE_ORDERS)

    async def test_finished_orders_do_not_count(self, session, make_user, make_call, fake_getsms, no_poll):
        await make_user(balance=10_000)
        for i in range(antiabuse.MAX_ACTIVE_ORDERS):
            await create_activation(session, user_id=TEST_USER_ID, order_id=900 + i, service_id=1, service_name="S",
                                    phone_number="+7", price=1, status="FINISHED")
        fake_getsms.create_order.return_value = order_response()
        await activation.create_order(make_call("order:create:105"), session)
        assert await get_activation(session, 5001) is not None

    async def test_rate_limit(self, session, make_user, make_call, fake_getsms, no_poll):
        await make_user(balance=10_000)
        for _ in range(antiabuse.PURCHASES_PER_WINDOW):
            await activation.create_order(make_call("order:create:105"), session)
        call = make_call("order:create:105")
        await activation.create_order(call, session)
        assert alert_text(call) == t("order_rate_limited", "ru")
        assert fake_getsms.create_order.await_count == antiabuse.PURCHASES_PER_WINDOW

    def test_rate_window_slides(self):
        for i in range(antiabuse.PURCHASES_PER_WINDOW):
            assert antiabuse.purchase_allowed(1, now=float(i))
        assert not antiabuse.purchase_allowed(1, now=10.0)
        assert antiabuse.purchase_allowed(1, now=antiabuse.PURCHASE_WINDOW + 0.5)
        assert antiabuse.purchase_allowed(2, now=10.0)              # per user

    async def test_double_tap_while_buying(self, session, make_user, make_call, fake_getsms, no_poll):
        await make_user(balance=10_000)
        async with antiabuse.purchase_lock(TEST_USER_ID):
            call = make_call("order:create:105")
            await activation.create_order(call, session)
        call.answer.assert_awaited_once_with(t("too_fast", "ru"))
        fake_getsms.create_order.assert_not_awaited()

    @pytest.mark.parametrize("which,data", [("rent", "rent:create:101:24"), ("email", "email:buy:0")])
    async def test_rent_and_email_share_the_limits(self, session, make_user, make_call, make_state, fake_getsms,
                                                   which, data):
        await make_user(balance=10_000)
        for _ in range(antiabuse.PURCHASES_PER_WINDOW):
            antiabuse.purchase_allowed(TEST_USER_ID)
        call = make_call(data)
        if which == "rent":
            await rent.create_rent(call, session)
        else:
            await email.email_buy(call, session, make_state())
        assert alert_text(call) == t("order_rate_limited", "ru")
        fake_getsms.create_rental.assert_not_awaited()
        fake_getsms.create_email.assert_not_awaited()


def _event(kind, user_id=TEST_USER_ID):
    ev = AsyncMock(spec=CallbackQuery if kind == "call" else Message)
    ev.from_user = SimpleNamespace(id=user_id)
    ev.answer = AsyncMock()
    return ev


class TestGuardMiddleware:
    @pytest.fixture(autouse=True)
    def _admins(self, monkeypatch):
        monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])

    async def test_blocked_user_is_stopped(self, session, make_user):
        await make_user(is_banned=True, language="en")
        handler = AsyncMock()
        guard = GuardMiddleware()
        call = _event("call")
        await guard(handler, call, {"session": session})
        handler.assert_not_awaited()
        call.answer.assert_awaited_once_with(t("user_banned", "en"), show_alert=True)
        msg = _event("msg")
        await guard(handler, msg, {"session": session})
        await guard(handler, msg, {"session": session})             # reminded at most once a minute
        msg.answer.assert_awaited_once()

    async def test_normal_user_passes(self, session, make_user):
        await make_user()
        handler = AsyncMock(return_value="ok")
        assert await GuardMiddleware()(handler, _event("call"), {"session": session}) == "ok"

    async def test_flooding(self, session, make_user):
        await make_user()
        guard, handler = GuardMiddleware(limit=3, window=10), AsyncMock()
        events = [_event("call") for _ in range(4)]
        for ev in events:
            await guard(handler, ev, {"session": session})
        assert handler.await_count == 3
        events[-1].answer.assert_awaited_once_with(t("too_fast", "ru"))

    async def test_admin_is_never_limited(self, session):
        guard, handler = GuardMiddleware(limit=1, window=10), AsyncMock()
        for _ in range(5):
            await guard(handler, _event("call", ADMIN_ID), {"session": session})
        assert handler.await_count == 5


class TestAdminBlock:
    @pytest.fixture(autouse=True)
    def _admins(self, monkeypatch):
        monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])

    async def test_block_and_unblock(self, session, make_user, make_call, reload_user):
        await make_user(OTHER_USER_ID)
        call = make_call(f"admin:ban:{OTHER_USER_ID}", ADMIN_ID)
        await admin.admin_ban_toggle(call, session)
        assert (await reload_user(OTHER_USER_ID)).is_banned is True
        kw = call.message.edit_text.await_args
        assert "заблокирован" in kw.args[0] and f"admin:unban:{OTHER_USER_ID}" in callbacks(kw.kwargs["reply_markup"])
        await admin.admin_ban_toggle(make_call(f"admin:unban:{OTHER_USER_ID}", ADMIN_ID), session)
        assert (await reload_user(OTHER_USER_ID)).is_banned is False

    async def test_cannot_block_admin_or_as_non_admin(self, session, make_user, make_call, reload_user):
        await make_user(ADMIN_ID)
        await make_user(OTHER_USER_ID)
        call = make_call(f"admin:ban:{ADMIN_ID}", ADMIN_ID)
        await admin.admin_ban_toggle(call, session)
        assert alert_text(call) and (await reload_user(ADMIN_ID)).is_banned is False
        await admin.admin_ban_toggle(make_call(f"admin:ban:{OTHER_USER_ID}", OTHER_USER_ID), session)
        assert (await reload_user(OTHER_USER_ID)).is_banned is False

    async def test_user_card_has_block_button(self, session, make_user, make_message):
        await make_user(OTHER_USER_ID)
        msg = make_message(f"/userinfo {OTHER_USER_ID}", ADMIN_ID)
        await admin.cmd_userinfo(msg, session)
        assert f"admin:ban:{OTHER_USER_ID}" in callbacks(msg.answer.await_args.kwargs["reply_markup"])
