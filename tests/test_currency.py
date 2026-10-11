"""Display currency: conversion, rates (CBR / manual / fallback), user choice, money flows, admin, migration."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

from bot import currency
from bot.db import _add_missing_columns
from bot.handlers import admin, admin_pricing, profile, topup
from bot.i18n import t
from tests.conftest import ADMIN_ID, OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks, last_caption
from tests.test_payments import configure


@pytest.fixture
def rates(monkeypatch):
    monkeypatch.setattr(currency, "_auto", {"USD": 84.9048, "EUR": 94.9745})


@pytest.fixture(autouse=True)
def _admin(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])


class TestConversion:
    @pytest.mark.parametrize("cur_field,lang,expected", [
        (None, "ru", "USD"), (None, "en", "USD"), (None, "de", "EUR"), ("EUR", "ru", "EUR"), ("RUB", "de", "EUR")])
    def test_user_currency(self, cur_field, lang, expected):
        assert currency.user_currency(SimpleNamespace(currency=cur_field, language=lang)) == expected
        assert currency.user_currency(None) == "USD"

    def test_format(self, rates):
        assert currency.fmt(849.05, "USD") == "10.00 $"
        assert currency.fmt(0, "EUR") == "0.00 €"
        assert currency.fmt(None, "USD") == "0.00 $"

    def test_prices_round_up_never_shown_lower_than_charged(self, rates):
        assert currency.fmt(90.03, "USD") == "1.06 $"
        assert currency.fmt(90.03, "USD", up=True) == "1.07 $"
        assert currency.fmt(84.9048, "USD", up=True) == "1.00 $"      # exact cent: no extra cent

    def test_round_trip(self, rates):
        rub = currency.to_rub(10, "USD")
        assert rub == 849.05 and currency.from_rub(rub, "USD") == 10.0

    def test_manual_rate_wins(self, rates, monkeypatch):
        monkeypatch.setitem(currency._manual, "USD", 100.0)
        assert currency.rate("USD") == 100 and currency.rate_source("USD") == "manual"
        assert currency.rate_source("EUR") == "auto"

    def test_fallback_before_first_fetch(self):
        assert currency.rate_source("USD") == "fallback"

    def test_parse_cbr(self):
        data = {"Date": "2026-10-10T11:30:00+03:00",
                "Valute": {"USD": {"Value": 84.9048, "Nominal": 1}, "EUR": {"Value": 949.745, "Nominal": 10}}}
        assert currency.parse_cbr(data) == ({"USD": 84.9048, "EUR": 94.9745}, "2026-10-10")

    @pytest.mark.parametrize("data", [
        {}, {"Valute": {"USD": {"Value": 0, "Nominal": 1}, "EUR": {"Value": 90, "Nominal": 1}}}])
    def test_parse_cbr_rejects_garbage(self, data):
        with pytest.raises((KeyError, ValueError)):
            currency.parse_cbr(data)


class TestStorage:
    async def test_auto_and_manual_persist(self, session):
        await currency.store_auto(session, {"USD": 80.0, "EUR": 90.0}, "2026-10-10")
        await currency.set_manual(session, "EUR", 99.5)
        currency._auto.clear()
        currency._manual.clear()
        await currency.load(session)
        assert currency.rate("USD") == 80 and currency.rate("EUR") == 99.5 and currency.auto_date() == "2026-10-10"
        await currency.set_manual(session, "EUR", None)
        await currency.load(session)
        assert currency.rate("EUR") == 90

    @pytest.mark.parametrize("value", [0, 0.5, 20000, float("nan")])
    async def test_manual_rejects_bad_rates(self, session, value):
        with pytest.raises(ValueError):
            await currency.set_manual(session, "USD", value)

    async def test_refresh_keeps_last_rate_when_cbr_down(self, session_factory, monkeypatch):
        monkeypatch.setattr(currency, "_auto", {"USD": 80.0})
        monkeypatch.setattr(currency, "fetch_cbr", AsyncMock(return_value=None))
        assert await currency.refresh(session_factory) is False
        assert currency.rate("USD") == 80


class TestMigration:
    async def test_old_users_table_gets_currency_column(self, tmp_path):
        engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'old.db'}")
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE users (id BIGINT PRIMARY KEY, balance FLOAT)"))
            await conn.execute(text("INSERT INTO users VALUES (1, 12.5)"))
            await conn.run_sync(_add_missing_columns)
            await conn.run_sync(_add_missing_columns)          # idempotent
            cols = await conn.run_sync(lambda c: [x["name"] for x in inspect(c).get_columns("users")])
            row = (await conn.execute(text("SELECT balance, currency FROM users"))).one()
        await engine.dispose()
        assert "currency" in cols and tuple(row) == (12.5, None)


class TestUserChoice:
    async def test_profile_button_and_switch(self, session, make_user, make_call, reload_user, rates):
        await make_user(balance=849.05)
        call = make_call("profile:currency")
        await profile.choose_currency(call, session)
        assert callbacks(call.message.edit_caption.await_args.kwargs["reply_markup"]) == [
            "cur:USD", "cur:EUR", "menu:profile"]
        call = make_call("cur:EUR")
        await profile.set_currency(call, session)
        assert (await reload_user()).currency == "EUR"
        assert "8.94 €" in last_caption(call)                    # 849.05 / 94.9745
        assert call.answer.await_args.args[0] == t("currency_set", "ru", currency="EUR (€)")

    async def test_unknown_currency_rejected(self, session, make_user, make_call, reload_user):
        await make_user()
        call = make_call("cur:RUB")
        await profile.set_currency(call, session)
        assert alert_text(call) == t("error_generic", "ru") and (await reload_user()).currency is None


class TestMoneyInUserCurrency:
    async def test_topup_amount_is_typed_in_user_currency(self, session, make_user, make_state, make_message, rates):
        await make_user(currency="USD")
        await configure(session)
        state = make_state()
        await state.set_state(topup.TopUp.amount)
        await state.update_data(code="cryptobot")
        msg = make_message("10")
        await topup.amount_input(msg, session, state)
        assert "10.00 $" in msg.answer.await_args.args[0]

    async def test_topup_min_is_checked_in_rubles(self, session, make_user, make_state, make_message, rates):
        from bot import payments
        await make_user(currency="USD")
        await configure(session)
        await payments.set_min_amount(session, "cryptobot", 500)     # owner sets the minimum in RUB
        state = make_state()
        await state.set_state(topup.TopUp.amount)
        await state.update_data(code="cryptobot")
        msg = make_message("5")                                       # 5 $ = 424.52 RUB < 500
        await topup.amount_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("topup_below_min", "ru", min="5.89 $")

    async def test_transfer_converts_both_sides(self, session, make_user, make_message, make_call, make_state,
                                                reload_user, rates):
        await make_user(balance=1000, currency="USD")
        await make_user(OTHER_USER_ID, balance=0, currency="EUR", language="en")
        state = make_state()
        await profile.transfer_balance(make_call("profile:transfer"), session, state)
        msg = make_message(f"{OTHER_USER_ID} 5")
        await profile.transfer_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("transfer_confirm", "ru", amount="5.00 $", to=OTHER_USER_ID)
        ok = make_call("transfer:ok")
        ok.bot.send_message = AsyncMock()
        await profile.transfer_confirm(ok, session, state)
        sent = currency.to_rub(5, "USD")
        assert (await reload_user()).balance == round(1000 - sent, 2)
        assert (await reload_user(OTHER_USER_ID)).balance == sent
        assert ok.bot.send_message.await_args.args[1] == t("transfer_received", "en",
                                                           amount=currency.fmt(sent, "EUR"), sender=TEST_USER_ID)


class TestAdminRates:
    async def test_screen_shows_sources(self, make_call, make_state, rates, monkeypatch):
        monkeypatch.setattr(currency, "_auto_date", "2026-10-10")
        call = make_call("admin:fx", ADMIN_ID)
        await admin_pricing.rates_screen(call, make_state(ADMIN_ID))
        text_ = call.message.edit_text.await_args.args[0]
        assert "1 $ = <b>84.9048 ₽</b> (ЦБ РФ от 2026-10-10)" in text_

    async def test_manual_then_auto(self, session, make_call, make_message, make_state, rates):
        state = make_state(ADMIN_ID)
        await admin_pricing.rates_set_prompt(make_call("admin:fx:set:USD", ADMIN_ID), state)
        await admin_pricing.rates_input(make_message("92,5", ADMIN_ID), session, state)
        assert currency.rate("USD") == 92.5 and await state.get_state() is None
        await admin_pricing.rates_auto(make_call("admin:fx:auto:USD", ADMIN_ID), session)
        assert currency.rate("USD") == 84.9048

    async def test_bad_manual_rate_keeps_waiting(self, session, make_call, make_message, make_state, rates):
        state = make_state(ADMIN_ID)
        await admin_pricing.rates_set_prompt(make_call("admin:fx:set:EUR", ADMIN_ID), state)
        msg = make_message("0", ADMIN_ID)
        await admin_pricing.rates_input(msg, session, state)
        assert msg.answer.await_args.args[0].startswith("❌") and currency.rate("EUR") == 94.9745

    async def test_refresh_button(self, session, make_call, monkeypatch):
        monkeypatch.setattr(currency, "fetch_cbr", AsyncMock(return_value=({"USD": 81.0, "EUR": 91.0}, "2026-10-11")))
        await admin_pricing.rates_refresh(make_call("admin:fx:refresh", ADMIN_ID), session)
        assert currency.rate("USD") == 81 and currency.auto_date() == "2026-10-11"

    async def test_non_admin_cannot_change(self, session, make_call, make_state):
        state = make_state()
        await admin_pricing.rates_set_prompt(make_call("admin:fx:set:USD"), state)
        assert await state.get_state() is None

    def test_admin_menu_button(self):
        assert "admin:fx" in callbacks(admin.admin_menu_kb())
