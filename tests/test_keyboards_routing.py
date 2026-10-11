"""Keyboards: layout, callback data, and that every button is actually handled."""

from types import SimpleNamespace

import pytest

from bot import keyboards as kb
from bot.handlers import activation, admin, admin_emoji, admin_payments, admin_pricing, email, profile, rent, start, topup
from tests.conftest import callbacks

LANGS = ["ru", "en", "de"]
ROUTERS = [start.router, activation.router, topup.router, profile.router, rent.router, email.router,
           admin_payments.router, admin_emoji.router, admin_pricing.router, admin.router]
TOPUP_METHODS = [("binance_pay", "Binance Pay"), ("sbp", "СБП"), ("cryptobot", "CryptoBot")]
CALLBACK_DATA_LIMIT = 64  # bytes, Telegram hard limit


def _buttons(markup):
    return [b for row in markup.inline_keyboard for b in row]


def _all_keyboards(lang):
    countries = [{"id": i, "name": f"C{i}"} for i in range(21)]
    services = [{"id": 1000 + i, "name": f"S{i}"} for i in range(21)]
    return {
        "lang": kb.lang_keyboard(),
        "tos": kb.tos_keyboard(lang),
        "main": kb.main_menu_keyboard(lang),
        "profile": kb.profile_keyboard(lang),
        "topup": kb.topup_keyboard(lang, TOPUP_METHODS),
        "countries": kb.countries_keyboard(countries, 2, 3, lang),
        "services": kb.services_keyboard(services, 2, 3, lang),
        "rent_services": kb.services_keyboard(services, 2, 3, lang, is_rent=True),
        "service_card": kb.service_card_keyboard(123456, 99.9, 10, lang),
        "order_active": kb.order_active_keyboard(987654321, lang, can_repeat=True),
        "error": kb.error_keyboard(lang, retry_cb="order:create:123456"),
        "back": kb.back_keyboard(lang, back_cb="menu:profile"),
        "admin": admin.admin_menu_kb(),
    }


async def _is_handled(data: str) -> bool:
    event = SimpleNamespace(data=data)
    for router in ROUTERS:
        for handler in router.callback_query.handlers:
            matched, _ = await handler.check(event)
            if matched:
                return True
    return False


class TestLayout:
    def test_main_menu_rows(self):
        rows = [len(r) for r in kb.main_menu_keyboard("ru").inline_keyboard]
        assert rows == [1, 2, 2, 2, 2, 1]

    def test_main_menu_callbacks(self):
        assert callbacks(kb.main_menu_keyboard("en")) == [
            "menu:anon", "menu:buy", "menu:topup", "menu:rent", "menu:email",
            "menu:history", "menu:country", "menu:last_act", "menu:last_rent", "menu:profile",
        ]

    @pytest.mark.parametrize("n", [0, 1, 2, 3, 4, 20, 21])
    def test_countries_grid_three_per_row(self, n):
        countries = [{"id": i, "name": f"C{i}"} for i in range(n)]
        rows = kb.countries_keyboard(countries, 1, 1, "ru").inline_keyboard
        grid = [len(r) for r in rows[: -3]]  # last 3 rows: operator/search, nav, back
        assert sum(grid) == n
        assert all(x == 3 for x in grid[:-1]) and (not grid or grid[-1] <= 3)

    def test_pagination_first_middle_last(self):
        c = [{"id": 1, "name": "A"}]
        first = callbacks(kb.countries_keyboard(c, 1, 3, "ru"))
        middle = callbacks(kb.countries_keyboard(c, 2, 3, "ru"))
        last = callbacks(kb.countries_keyboard(c, 3, 3, "ru"))
        assert "countries_page:2" in first and "countries_page:0" not in first
        assert {"countries_page:1", "countries_page:3"} <= set(middle)
        assert "countries_page:2" in last and "countries_page:4" not in last

    def test_single_page_has_no_arrows(self):
        cbs = callbacks(kb.services_keyboard([{"id": 1, "name": "A"}], 1, 1, "ru"))
        assert not any(c.startswith("services_page:") for c in cbs)

    def test_rent_services_use_rent_prefixes_and_no_email_row(self):
        s = [{"id": 7, "name": "A"}]
        cbs = callbacks(kb.services_keyboard(s, 2, 3, "ru", is_rent=True))
        assert "rent_service:7" in cbs and "service:7" not in cbs
        assert "rent_services_page:1" in cbs and "rent_services_page:3" in cbs
        assert "menu:email" not in cbs

    def test_activation_services_have_email_and_search(self):
        cbs = callbacks(kb.services_keyboard([{"id": 7, "name": "A"}], 1, 1, "ru"))
        assert {"service:7", "menu:email", "search:service"} <= set(cbs)

    def test_service_card(self):
        markup = kb.service_card_keyboard(42, 85.5, 1000, "ru")
        assert {"order:create:42", "top10:42", "fav:42"} <= set(callbacks(markup))
        assert "85.5" in _buttons(markup)[0].text and "1000" in _buttons(markup)[0].text

    def test_order_active_repeat_toggle(self):
        assert "order:repeat:9" in callbacks(kb.order_active_keyboard(9, "ru", can_repeat=True))
        no_repeat = callbacks(kb.order_active_keyboard(9, "ru", can_repeat=False))
        assert "order:repeat:9" not in no_repeat and "order:finish:9" in no_repeat

    def test_error_keyboard_retry_optional(self):
        assert callbacks(kb.error_keyboard("ru", retry_cb="x:1"))[0] == "x:1"
        assert "x:1" not in callbacks(kb.error_keyboard("ru"))

    @pytest.mark.parametrize("n,rows", [(0, [1]), (1, [1, 1]), (2, [2, 1]), (3, [1, 2, 1]), (7, [1, 2, 2, 2, 1])])
    def test_topup_layout(self, n, rows):
        """Like the original bot: pairs, first method full-width when the count is odd, Back last."""
        methods = [(f"m{i}", f"Method {i}") for i in range(n)]
        markup = kb.topup_keyboard("ru", methods)
        assert [len(r) for r in markup.inline_keyboard] == rows
        assert callbacks(markup)[:n] == [f"topup:m:m{i}" for i in range(n)]
        assert callbacks(markup)[-1] == "menu:main"

    @pytest.mark.parametrize("lang", LANGS)
    def test_buttons_are_translated(self, lang):
        from bot.i18n import t
        texts = [b.text for b in _buttons(kb.main_menu_keyboard(lang))]
        assert t("btn_buy_number", lang) in texts
        assert t("btn_profile", lang) in texts


class TestCallbackData:
    @pytest.mark.parametrize("lang", LANGS)
    def test_no_button_without_text_or_callback(self, lang):
        for name, markup in _all_keyboards(lang).items():
            for b in _buttons(markup):
                assert b.text, f"{name}: empty button text"
                assert b.callback_data, f"{name}: button {b.text!r} has no callback"

    def test_callback_data_within_telegram_limit(self):
        for name, markup in _all_keyboards("de").items():
            for data in callbacks(markup):
                assert len(data.encode()) <= CALLBACK_DATA_LIMIT, f"{name}: {data}"


class TestEveryButtonIsHandled:
    """A button whose callback matches no handler leaves the user with an endless spinner."""

    @pytest.mark.parametrize("name", [
        "lang", "tos", "main", "profile", "topup", "countries", "services",
        "rent_services", "service_card", "order_active", "error", "back",
    ])
    async def test_user_keyboards(self, name):
        markup = _all_keyboards("ru")[name]
        unhandled = [d for d in callbacks(markup) if not await _is_handled(d)]
        assert unhandled == []

    async def test_dynamic_callbacks(self):
        for data in ["operator:select", "operator:set:mts", "operator:set:any", "country:5",
                     "rent:create:1:4", "rent:finish:1", "order:create:5:9003", "email:new", "email:history",
                     "email:pg:2", "email:buy:0", "email:chk:e1", "email:re:e1", "email:cancel:e1",
                     "email:open:e1", "np:chk:123", "admin:recent", "admin:find", "admin:bc:send",
                     "admin:users:2", "admin:menu"]:
            assert await _is_handled(data), data

    async def test_admin_keyboard(self):
        unhandled = [d for d in callbacks(admin.admin_menu_kb()) if not await _is_handled(d)]
        assert unhandled == []
