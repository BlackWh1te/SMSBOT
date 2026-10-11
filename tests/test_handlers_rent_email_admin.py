"""Rent, e-mail activation and admin handlers."""

import math

import pytest

from bot.db.queries import create_email_order, create_rental, get_email_order, get_last_rental
from bot.handlers import admin, email, rent
from bot.i18n import t
from tests.conftest import (
    ADMIN_ID, OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks, last_caption, last_markup,
)


# ─── Rent ────────────────────────────────────────────────────────────────────

def rent_response(order_id=7001, price=200.0, phone="+79995554433"):
    return {"ok": True, "order": {"id": order_id, "phone_number": phone, "price": price}}


class TestRent:
    async def test_rent_requires_country(self, session, make_user, make_call, fake_getsms):
        await make_user(language="en")
        call = make_call("menu:rent")
        await rent.rent_menu(call, session)
        assert alert_text(call) == t("error_select_country", "en")
        fake_getsms.get_services.assert_not_awaited()

    async def test_rent_lists_rent_services(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=3)
        call = make_call("menu:rent")
        await rent.rent_menu(call, session)
        fake_getsms.get_services.assert_awaited_once_with(3, is_rent=True)
        assert "rent_service:101" in callbacks(last_markup(call))

    async def test_duration_menu_prices(self, session, make_user, make_call, fake_getsms):
        await make_user()
        fake_getsms.get_rental_price.side_effect = [
            {"ok": True, "price": 10}, {"ok": False}, {"ok": True, "price": 30},
            {"ok": True, "price": 40}, {"ok": True, "price": 50},
        ]
        call = make_call("rent_service:101")
        await rent.rent_service_card(call, session)
        rows = last_markup(call).inline_keyboard
        assert [r[0].callback_data for r in rows[:5]] == [
            f"rent:create:101:{h}" for h in (4, 12, 24, 48, 72)]
        assert "10.00 $" in rows[0][0].text and rows[1][0].text.endswith("— ?")

    async def test_create_rent_success(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user(balance=500, total_rented=1)
        fake_getsms.create_rental.return_value = rent_response(price=200)
        call = make_call("rent:create:101:24")
        await rent.create_rent(call, session)

        fake_getsms.create_rental.assert_awaited_once_with(101, 24, operator="any")
        u = await reload_user()
        assert u.balance == 300 and u.total_rented == 2
        r = await get_last_rental(session, TEST_USER_ID)
        assert (r.order_id, r.phone_number, r.price) == (7001, "+79995554433", 200)
        assert "rent:finish:7001" in callbacks(last_markup(call))

    @pytest.mark.parametrize("code,key", [("INSUFFICIENT_BALANCE", "error_generic"),
                                          ("WHATEVER", "error_generic")])
    async def test_create_rent_api_error(self, session, make_user, make_call, fake_getsms, reload_user, code, key):
        await make_user(balance=500)
        fake_getsms.create_rental.return_value = {"ok": False, "error_code": code}
        call = make_call("rent:create:101:24")
        await rent.create_rent(call, session)
        assert last_caption(call) == t(key, "ru")
        assert callbacks(last_markup(call))[0] == "rent:create:101:24"
        assert (await reload_user()).balance == 500

    async def test_cannot_rent_without_money(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user(balance=0)
        fake_getsms.create_rental.return_value = rent_response(price=200)
        await rent.create_rent(make_call("rent:create:101:24"), session)
        fake_getsms.create_rental.assert_not_awaited()

    async def test_finish_rent(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await create_rental(session, user_id=TEST_USER_ID, order_id=7001, service_id=1,
                            service_name="s", phone_number="+7", price=1)
        call = make_call("rent:finish:7001")
        await rent.finish_rent(call, session)
        assert last_caption(call) == t("order_finished", "ru")

    async def test_cannot_finish_someone_elses_rental(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await make_user(OTHER_USER_ID)
        await create_rental(session, user_id=TEST_USER_ID, order_id=7001, service_id=1,
                            service_name="s", phone_number="+7", price=1)
        await rent.finish_rent(make_call("rent:finish:7001", user_id=OTHER_USER_ID), session)
        fake_getsms.finish_rental.assert_not_awaited()

    async def test_last_rental(self, session, make_user, make_call):
        await make_user()
        call = make_call("menu:last_rent")
        await rent.last_rental(call, session)
        assert last_caption(call) == t("history_empty", "ru")

        await create_rental(session, user_id=TEST_USER_ID, order_id=1, service_id=1,
                            service_name="s", phone_number="+7123", price=9.5)
        await rent.last_rental(call, session)
        assert "+7123" in last_caption(call) and "9.5" in last_caption(call)


# ─── E-mail (flow of the original bot) ──────────────────────────────────────

async def _email_order(session, user_id=TEST_USER_ID, email_id="e1", price=3.39, status="WAIT", code=None):
    return await create_email_order(session, user_id=user_id, email_id=email_id, email=f"{email_id}@gmail.com",
                                    site="telegram.com", domain="gmail.com", price=price, status=status, code=code)


async def _domains_listed(session, make_state, make_message, user_id=TEST_USER_ID):
    """Simulate: «Создать почту» -> user typed telegram.com -> domain list shown."""
    state = make_state(user_id)
    await state.set_state(email.EmailFlow.site)
    msg = make_message("telegram.com", user_id=user_id)
    await email.email_site_input(msg, session, state)
    return state, msg


class TestEmail:
    async def test_menu_like_original(self, session, make_user, make_call, make_state):
        await make_user()
        call = make_call("menu:email")
        await email.email_menu(call, session, make_state())
        assert last_caption(call) == t("email_text", "ru")
        assert callbacks(last_markup(call)) == ["email:new", "email:history", "menu:main"]

    async def test_create_asks_for_site(self, session, make_user, make_call, make_state):
        await make_user()
        state = make_state()
        call = make_call("email:new")
        await email.email_new(call, session, state)
        assert "telegram.com" in last_caption(call)
        assert await state.get_state() == email.EmailFlow.site.state

    @pytest.mark.parametrize("raw,site", [("telegram.com", "telegram.com"),
                                          ("https://www.Gmail.com/login", "gmail.com"),
                                          ("  vk.ru ", "vk.ru")])
    def test_site_normalization(self, raw, site):
        assert email.normalize_site(raw) == site

    @pytest.mark.parametrize("raw", ["", "hello", "not a site", "a" * 70 + ".com", "<script>.com"])
    def test_site_rejected(self, raw):
        assert email.normalize_site(raw) is None

    async def test_invalid_site_keeps_waiting(self, session, make_user, make_message, make_state, fake_getsms):
        await make_user()
        state = make_state()
        await state.set_state(email.EmailFlow.site)
        msg = make_message("hello")
        await email.email_site_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("email_site_invalid", "ru")
        assert await state.get_state() == email.EmailFlow.site.state
        fake_getsms.get_email_domains.assert_not_awaited()

    async def test_domain_list_with_price_and_stock(self, session, make_user, make_message, make_state, fake_getsms):
        await make_user()
        state, msg = await _domains_listed(session, make_state, make_message)
        fake_getsms.get_email_domains.assert_awaited_once_with("telegram.com")
        text = msg.answer.await_args.args[0]
        assert "<code>telegram.com</code>" in text and "<code>1/1</code>" in text and "<code>2</code>" in text
        markup = msg.answer.await_args.kwargs["reply_markup"]
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "gmail.com (3.39 $ • 617608)" in labels
        assert callbacks(markup)[:2] == ["email:buy:0", "email:buy:1"]
        assert await state.get_state() is None          # stops capturing text
        assert (await state.get_data())["site"] == "telegram.com"

    async def test_domain_pagination_10_per_page(self, session, make_user, make_message, make_state, make_call,
                                                 fake_getsms):
        await make_user()
        fake_getsms.get_email_domains.return_value = [
            {"domain": f"d{i}.com", "price": 1 + i, "count": 10} for i in range(26)]
        state, msg = await _domains_listed(session, make_state, make_message)
        first = callbacks(msg.answer.await_args.kwargs["reply_markup"])
        assert first[:10] == [f"email:buy:{i}" for i in range(10)] and "email:pg:2" in first
        assert "<code>1/3</code>" in msg.answer.await_args.args[0]

        call = make_call("email:pg:3")
        await email.email_domains_page(call, session, state)
        last = callbacks(last_markup(call))
        assert last[:6] == [f"email:buy:{i}" for i in range(20, 26)] and "email:pg:2" in last

    async def test_no_domains(self, session, make_user, make_message, make_state, fake_getsms):
        await make_user()
        fake_getsms.get_email_domains.return_value = []
        _, msg = await _domains_listed(session, make_state, make_message)
        assert msg.answer.await_args.args[0] == t("email_no_domains", "ru", site="telegram.com")

    async def test_buy_charges_and_stores(self, session, make_user, make_message, make_state, make_call,
                                          fake_getsms, reload_user):
        await make_user(balance=10)
        state, _ = await _domains_listed(session, make_state, make_message)
        call = make_call("email:buy:0")
        await email.email_buy(call, session, state)

        fake_getsms.create_email.assert_awaited_once_with("telegram.com", "gmail.com")
        assert (await reload_user()).balance == 6.61
        eo = await get_email_order(session, "e1")
        assert (eo.user_id, eo.email, eo.price, eo.status) == (TEST_USER_ID, "x@gmail.com", 3.39, "WAIT")
        assert "x@gmail.com" in last_caption(call)
        assert callbacks(last_markup(call)) == ["email:chk:e1", "email:re:e1", "email:cancel:e1", "menu:email"]

    async def test_buy_without_money_never_calls_api(self, session, make_user, make_message, make_state,
                                                     make_call, fake_getsms):
        await make_user(balance=0)
        state, _ = await _domains_listed(session, make_state, make_message)
        call = make_call("email:buy:0")
        await email.email_buy(call, session, state)
        assert alert_text(call) == t("insufficient_balance", "ru")
        fake_getsms.create_email.assert_not_awaited()

    async def test_buy_failure_refunds(self, session, make_user, make_message, make_state, make_call,
                                       fake_getsms, reload_user):
        await make_user(balance=10)
        fake_getsms.create_email.return_value = {"ok": False, "error_code": "NO_NUMBERS"}
        state, _ = await _domains_listed(session, make_state, make_message)
        await email.email_buy(make_call("email:buy:0"), session, state)
        assert (await reload_user()).balance == 10
        assert await get_email_order(session, "e1") is None

    async def test_buy_with_expired_list(self, session, make_user, make_call, make_state, fake_getsms):
        await make_user(balance=10)
        call = make_call("email:buy:0")
        await email.email_buy(call, session, make_state())
        assert alert_text(call) == t("email_session_expired", "ru")
        fake_getsms.create_email.assert_not_awaited()

    async def test_check_shows_code_escaped(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await _email_order(session)
        fake_getsms.check_email.return_value = {"ok": True, "order": {"status": "RECEIVED"},
                                                "result": {"code": "482913", "message": "<b>Code</b> 482913"}}
        call = make_call("email:chk:e1")
        await email.check_email(call, session)
        caption = last_caption(call)
        assert "<code>482913</code>" in caption and "&lt;b&gt;Code&lt;/b&gt;" in caption
        eo = await get_email_order(session, "e1")
        assert (eo.status, eo.code) == ("RECEIVED", "482913")

    async def test_check_long_email_fits_caption(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await _email_order(session)
        fake_getsms.check_email.return_value = {"ok": True, "result": {"code": "1", "message": "x" * 5000}}
        call = make_call("email:chk:e1")
        await email.check_email(call, session)
        assert len(last_caption(call)) <= 1024

    async def test_check_nothing_yet(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await _email_order(session)
        call = make_call("email:chk:e1")
        await email.check_email(call, session)
        assert alert_text(call) == t("email_no_messages", "ru")

    @pytest.mark.parametrize("data,handler", [
        ("email:chk:e1", "check_email"), ("email:re:e1", "reorder_email"),
        ("email:cancel:e1", "cancel_email"), ("email:open:e1", "email_open")])
    async def test_cannot_touch_someone_elses_email(self, session, make_user, make_call, fake_getsms, data, handler):
        await make_user()
        await make_user(OTHER_USER_ID, balance=100)
        await _email_order(session, user_id=TEST_USER_ID)
        call = make_call(data, user_id=OTHER_USER_ID)
        await getattr(email, handler)(call, session)
        assert alert_text(call) == t("order_not_found", "ru")
        fake_getsms.check_email.assert_not_awaited()
        fake_getsms.reorder_email.assert_not_awaited()
        fake_getsms.cancel_email.assert_not_awaited()

    async def test_reorder_is_paid(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user(balance=10)
        await _email_order(session, status="RECEIVED", code="111")
        call = make_call("email:re:e1")
        await email.reorder_email(call, session)
        assert (await reload_user()).balance == 6.61
        eo = await get_email_order(session, "e1")
        assert (eo.status, eo.code) == ("WAIT", None)
        assert alert_text(call) == t("email_reordered", "ru")

    async def test_reorder_without_money(self, session, make_user, make_call, fake_getsms):
        await make_user(balance=0)
        await _email_order(session)
        call = make_call("email:re:e1")
        await email.reorder_email(call, session)
        assert alert_text(call) == t("insufficient_balance", "ru")
        fake_getsms.reorder_email.assert_not_awaited()

    async def test_cancel_refunds_exactly_once(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user(balance=0)
        await _email_order(session, price=3.39)
        call = make_call("email:cancel:e1")
        await email.cancel_email(call, session)
        assert last_caption(call) == t("email_cancelled", "ru")
        assert (await reload_user()).balance == 3.39

        again = make_call("email:cancel:e1")
        await email.cancel_email(again, session)
        assert alert_text(again) == t("order_already_closed", "ru")
        assert (await reload_user()).balance == 3.39

    async def test_cancel_too_early(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user(balance=0)
        await _email_order(session)
        fake_getsms.cancel_email.return_value = {"ok": False, "error_code": "TOO_EARLY"}
        call = make_call("email:cancel:e1")
        await email.cancel_email(call, session)
        assert alert_text(call) == t("email_cancel_failed", "ru")
        assert (await get_email_order(session, "e1")).status == "WAIT"
        assert (await reload_user()).balance == 0

    async def test_history(self, session, make_user, make_call):
        await make_user()
        call = make_call("email:history")
        await email.email_history(call, session)
        assert last_caption(call) == t("email_history_empty", "ru")

        await _email_order(session, email_id="e1")
        await _email_order(session, email_id="e2")
        await _email_order(session, user_id=OTHER_USER_ID, email_id="e3")
        await email.email_history(call, session)
        cbs = callbacks(last_markup(call))
        assert set(cbs[:2]) == {"email:open:e1", "email:open:e2"} and "email:open:e3" not in cbs


# ─── Admin ───────────────────────────────────────────────────────────────────

@pytest.fixture
def as_admin(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])


def reply(msg) -> str:
    return msg.answer.await_args.args[0]


class TestAdminAccess:
    @pytest.mark.parametrize("cmd", ["/admin", "/addbal 1 5", "/rembal 1 5", "/userinfo 1"])
    async def test_non_admin_commands_are_ignored(self, session, make_message, as_admin, cmd):
        msg = make_message(cmd, user_id=TEST_USER_ID)
        handler = {"/admin": admin.admin_panel, "/addbal": admin.cmd_addbal,
                   "/rembal": admin.cmd_rembal, "/userinfo": admin.cmd_userinfo}[cmd.split()[0]]
        await handler(msg, session)
        msg.answer.assert_not_awaited()

    async def test_non_admin_cannot_view_stats(self, session, make_call, as_admin):
        call = make_call("admin:stats", user_id=TEST_USER_ID)
        await admin.admin_stats(call, session)
        call.message.edit_text.assert_not_awaited()

    async def test_admin_panel(self, session, make_message, as_admin):
        msg = make_message("/admin", user_id=ADMIN_ID)
        await admin.admin_panel(msg, session)
        assert {"admin:stats", "admin:pay"} <= set(callbacks(msg.answer.await_args.kwargs["reply_markup"]))
        assert "Активные способы оплаты: <b>нет</b>" in msg.answer.await_args.args[0]


class TestAdminBalance:
    async def test_addbal(self, session, make_user, make_message, reload_user, as_admin):
        await make_user(balance=100)
        msg = make_message(f"/addbal {TEST_USER_ID} 50", user_id=ADMIN_ID)
        await admin.cmd_addbal(msg, session)
        assert (await reload_user()).balance == 150
        assert reply(msg).startswith("✅")

    async def test_addbal_reports_correct_new_balance(self, session, make_user, make_message, as_admin):
        await make_user(balance=100)
        msg = make_message(f"/addbal {TEST_USER_ID} 50", user_id=ADMIN_ID)
        await admin.cmd_addbal(msg, session)
        assert reply(msg).endswith("Новый баланс: 150.0")

    @pytest.mark.parametrize("text,expected", [
        ("/addbal", "Формат: /addbal"), ("/addbal 1", "Формат"), ("/addbal a b", "Неверные"),
        (f"/addbal 404 10", "не найден"),
    ])
    async def test_addbal_bad_input(self, session, make_message, as_admin, text, expected):
        msg = make_message(text, user_id=ADMIN_ID)
        await admin.cmd_addbal(msg, session)
        assert expected in reply(msg)

    @pytest.mark.parametrize("amount", ["-1000", "nan", "inf"])
    async def test_addbal_rejects_invalid_amounts(self, session, make_user, make_message, reload_user,
                                                  as_admin, amount):
        await make_user(balance=100)
        await admin.cmd_addbal(make_message(f"/addbal {TEST_USER_ID} {amount}", user_id=ADMIN_ID), session)
        balance = (await reload_user()).balance
        assert balance == 100 and math.isfinite(balance)

    async def test_rembal(self, session, make_user, make_message, reload_user, as_admin):
        await make_user(balance=100)
        msg = make_message(f"/rembal {TEST_USER_ID} 30", user_id=ADMIN_ID)
        await admin.cmd_rembal(msg, session)
        assert (await reload_user()).balance == 70

    async def test_rembal_insufficient(self, session, make_user, make_message, reload_user, as_admin):
        await make_user(balance=10)
        msg = make_message(f"/rembal {TEST_USER_ID} 30", user_id=ADMIN_ID)
        await admin.cmd_rembal(msg, session)
        assert "Недостаточно" in reply(msg)
        assert (await reload_user()).balance == 10


class TestAdminViews:
    async def test_userinfo(self, session, make_user, make_message, as_admin):
        await make_user(balance=12.5, total_bought=3, total_activated=2)
        msg = make_message(f"/userinfo {TEST_USER_ID}", user_id=ADMIN_ID)
        await admin.cmd_userinfo(msg, session)
        assert str(TEST_USER_ID) in reply(msg) and "12.5" in reply(msg) and "3 (активировано 2)" in reply(msg)

    async def test_userinfo_bad_id(self, session, make_message, as_admin):
        msg = make_message("/userinfo @someone", user_id=ADMIN_ID)
        await admin.cmd_userinfo(msg, session)
        msg.answer.assert_awaited()

    async def test_userinfo_escapes_full_name(self, session, make_user, make_message, as_admin):
        await make_user(full_name="<b>Eve")
        msg = make_message(f"/userinfo {TEST_USER_ID}", user_id=ADMIN_ID)
        await admin.cmd_userinfo(msg, session)
        assert "&lt;b&gt;Eve" in reply(msg)

    async def test_stats(self, session, make_user, make_call, as_admin):
        await make_user(TEST_USER_ID, balance=10)
        await make_user(OTHER_USER_ID, balance=5.5)
        call = make_call("admin:stats", user_id=ADMIN_ID)
        await admin.admin_stats(call, session)
        text = call.message.edit_text.await_args.args[0]
        assert "<b>2</b>" in text and "15.5" in text and "<b>0</b>" in text

    async def test_users_pagination(self, session, make_user, make_call, as_admin):
        for uid in range(1, 26):
            await make_user(uid)
        call = make_call("admin:users:2", user_id=ADMIN_ID)
        await admin.admin_users(call, session)
        text = call.message.edit_text.await_args.args[0]
        assert "стр. 2/3" in text and text.count("•") == 10
        cbs = callbacks(call.message.edit_text.await_args.kwargs["reply_markup"])
        assert {"admin:users:1", "admin:users:3", "admin:menu"} <= set(cbs)
