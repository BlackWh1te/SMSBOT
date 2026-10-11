"""Owner's markup on numbers: shown prices, charges, refunds, admin screen."""

from unittest.mock import AsyncMock

import pytest

from bot import pricing
from bot.db.queries import get_activation, get_last_rental
from bot.handlers import activation, admin, admin_pricing, rent
from bot.i18n import t
from tests.conftest import ADMIN_ID, TEST_USER_ID, alert_text, callbacks, last_markup, order_response


@pytest.fixture(autouse=True)
def _reset_markup(monkeypatch):
    monkeypatch.setattr(pricing, "_markup", 1.0)
    monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])


@pytest.fixture
def x2(monkeypatch):
    monkeypatch.setattr(pricing, "_markup", 2.0)


@pytest.fixture
def no_poll(monkeypatch):
    monkeypatch.setattr(activation, "poll_sms", AsyncMock())


class TestSell:
    @pytest.mark.parametrize("m,cost,expected", [(1, 90.03, 90.03), (1.5, 90.03, 135.05), (2, 45, 90),
                                                 (3, 0.1, 0.3), (2, "12.5", 25)])
    def test_sell(self, monkeypatch, m, cost, expected):
        monkeypatch.setattr(pricing, "_markup", float(m))
        assert pricing.sell(cost) == expected

    @pytest.mark.parametrize("cost", [None, "abc", 0, -5, float("nan"), float("inf")])
    def test_invalid_cost(self, cost):
        assert pricing.sell(cost) is None

    @pytest.mark.parametrize("value", [0.5, 0.99, 10.01, float("nan"), float("inf")])
    def test_validate_rejects(self, value):
        with pytest.raises(ValueError):
            pricing.validate(value)

    async def test_persisted(self, session):
        await pricing.set_markup(session, 1.5)
        pricing._markup = 1.0
        await pricing.load(session)
        assert pricing.markup() == 1.5


class TestNumbers:
    async def test_card_shows_marked_up_price_but_keeps_cost_in_callback(self, session, make_user, make_call,
                                                                         fake_getsms, x2):
        await make_user(selected_country_id=1)
        fake_getsms.get_offers.return_value = {"ok": True, "providers": [{"count": 10, "price": 90.03}]}
        call = make_call("service:105")
        await activation.show_service_card(call, session)
        btn = last_markup(call).inline_keyboard[0][0]
        assert btn.callback_data == "order:create:105:9003"
        assert btn.text == t("btn_price_tier", "ru", price="180.06 $", count=10)

    async def test_purchase_charges_markup_and_orders_at_cost(self, session, make_user, make_call, fake_getsms,
                                                              no_poll, reload_user, x2):
        await make_user(balance=200)
        fake_getsms.create_order.return_value = order_response(price=45)
        await activation.create_order(make_call("order:create:105:5000"), session)
        assert fake_getsms.create_order.await_args.kwargs["max_price"] == 50
        assert (await reload_user()).balance == 110                  # reserved 100, real cost 45 -> pays 90
        assert (await get_activation(session, 5001)).price == 90     # refunds return what the user paid

    async def test_cannot_buy_with_only_cost_price(self, session, make_user, make_call, fake_getsms, no_poll,
                                                   reload_user, x2):
        await make_user(balance=60)
        await activation.create_order(make_call("order:create:105:5000"), session)
        fake_getsms.create_order.assert_not_awaited()
        assert (await reload_user()).balance == 60

    async def test_rent(self, session, make_user, make_call, fake_getsms, reload_user, x2):
        await make_user(balance=500)
        fake_getsms.get_rental_price.return_value = {"ok": True, "price": 100}
        fake_getsms.create_rental.return_value = {"ok": True, "order": {"id": 7001, "phone_number": "+7", "price": 100}}
        await rent.create_rent(make_call("rent:create:101:24"), session)
        assert (await reload_user()).balance == 300
        assert (await get_last_rental(session, TEST_USER_ID)).price == 200

    async def test_rent_menu_prices(self, session, make_user, make_call, fake_getsms, x2):
        await make_user()
        fake_getsms.get_rental_price.return_value = {"ok": True, "price": 10}
        call = make_call("rent_service:101")
        await rent.rent_service_card(call, session)
        assert "20.00 $" in last_markup(call).inline_keyboard[0][0].text


class TestAdmin:
    def test_menu_button(self):
        assert "admin:mk" in callbacks(admin.admin_menu_kb())

    async def test_preset(self, session, make_call):
        call = make_call("admin:mk:set:1.5", ADMIN_ID)
        await admin_pricing.markup_set(call, session)
        assert pricing.markup() == 1.5
        kb = call.message.edit_text.await_args.kwargs["reply_markup"]
        assert "✅ ×1.5" in [b.text for row in kb.inline_keyboard for b in row]

    async def test_forged_preset_rejected(self, session, make_call):
        call = make_call("admin:mk:set:0.1", ADMIN_ID)
        await admin_pricing.markup_set(call, session)
        assert pricing.markup() == 1.0 and alert_text(call)

    async def test_non_admin(self, session, make_call):
        await admin_pricing.markup_set(make_call("admin:mk:set:3"), session)
        assert pricing.markup() == 1.0

    @pytest.mark.parametrize("text,expected", [("1,8", 1.8), ("x2", 2.0), ("×2.5", 2.5)])
    async def test_custom(self, session, make_call, make_message, make_state, text, expected):
        state = make_state(ADMIN_ID)
        await admin_pricing.markup_custom(make_call("admin:mk:custom", ADMIN_ID), state)
        await admin_pricing.markup_input(make_message(text, ADMIN_ID), session, state)
        assert pricing.markup() == expected
        assert await state.get_state() is None

    @pytest.mark.parametrize("text", ["abc", "0.5", "50"])
    async def test_custom_invalid_keeps_waiting(self, session, make_call, make_message, make_state, text):
        state = make_state(ADMIN_ID)
        await admin_pricing.markup_custom(make_call("admin:mk:custom", ADMIN_ID), state)
        msg = make_message(text, ADMIN_ID)
        await admin_pricing.markup_input(msg, session, state)
        assert pricing.markup() == 1.0
        assert msg.answer.await_args.args[0].startswith("❌")
        assert await state.get_state() == admin_pricing.AdminMarkup.value.state
