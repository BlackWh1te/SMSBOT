"""Owner-managed payment methods: storage, admin panel (/admin -> Payment methods), user top-up."""

import pytest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage

from bot import payments
from bot.db.models import PaymentSetting
from bot.handlers import admin, admin_payments, topup
from bot.i18n import t
from tests.conftest import ADMIN_ID, TEST_USER_ID, alert_text, callbacks, last_caption, last_markup

TOKEN = "123456:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"   # Telegram's public docs example


@pytest.fixture
def make_state():
    storage = MemoryStorage()

    def _make(user_id: int = TEST_USER_ID) -> FSMContext:
        return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    return _make


@pytest.fixture
def as_admin(monkeypatch):
    monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])


async def configure(session, code="cryptobot", enabled=True, **creds):
    for key, value in (creds or {"api_token": TOKEN}).items():
        await payments.set_credential(session, code, key, value)
    if enabled:
        assert await payments.set_enabled(session, code, True)


# ─── Storage / service ────────────────────────────────────────────────────────

class TestService:
    async def test_defaults_everything_off(self, session):
        states = await payments.all_states(session)
        assert [s.code for s in states] == list(payments.METHODS)
        assert not any(s.is_active for s in states)
        assert await payments.active_methods(session) == []

    async def test_keys_are_encrypted_in_db(self, session):
        await payments.set_credential(session, "cryptobot", "api_token", TOKEN)
        row = await session.get(PaymentSetting, "cryptobot", populate_existing=True)
        assert TOKEN not in row.credentials and "api_token" not in row.credentials
        assert (await payments.get_state(session, "cryptobot")).creds == {"api_token": TOKEN}

    async def test_cannot_enable_without_required_keys(self, session):
        assert await payments.set_enabled(session, "binance_pay", True) is False
        await payments.set_credential(session, "binance_pay", "api_key", "k" * 20)
        assert await payments.set_enabled(session, "binance_pay", True) is False   # secret still missing
        await payments.set_credential(session, "binance_pay", "secret_key", "s" * 20)
        assert await payments.set_enabled(session, "binance_pay", True) is True
        assert [m.code for m in await payments.active_methods(session)] == ["binance_pay"]

    async def test_optional_field_not_required(self, session):
        await configure(session, "sbp", shop_id="777", api_key="key-123456789")
        assert (await payments.get_state(session, "sbp")).is_active

    async def test_disable_hides_method(self, session):
        await configure(session)
        await payments.set_enabled(session, "cryptobot", False)
        assert await payments.active_methods(session) == []

    async def test_clear_keys_also_disables(self, session):
        await configure(session)
        await payments.clear_credentials(session, "cryptobot")
        s = await payments.get_state(session, "cryptobot")
        assert s.creds == {} and not s.enabled and not s.is_active

    async def test_changed_encryption_key_makes_keys_unreadable_not_crash(self, session, monkeypatch):
        await configure(session)
        monkeypatch.setattr(payments.config, "SETTINGS_SECRET", "a-different-secret")
        s = await payments.get_state(session, "cryptobot")
        assert s.keys_unreadable and not s.is_active
        # re-entering the key recovers the method
        await payments.set_credential(session, "cryptobot", "api_token", TOKEN)
        assert (await payments.get_state(session, "cryptobot")).is_active

    @pytest.mark.parametrize("value", ["", "   ", "abc", "-5", "0", "nan", "inf"])
    async def test_number_field_validation(self, session, value):
        with pytest.raises(ValueError):
            await payments.set_credential(session, "stars", "rate", value)

    async def test_number_field_accepts_comma(self, session):
        await payments.set_credential(session, "stars", "rate", "1,8")
        assert (await payments.get_state(session, "stars")).creds["rate"] == "1.8"

    async def test_title_and_min_amount(self, session):
        await payments.set_title(session, "sbp", "СБП 2")
        await payments.set_min_amount(session, "sbp", 150)
        s = await payments.get_state(session, "sbp")
        assert s.display_title("en") == "СБП 2" and s.min_amount == 150
        await payments.set_title(session, "sbp", None)
        assert (await payments.get_state(session, "sbp")).display_title("en") == "SBP (Russian fast payments)"

    async def test_unknown_method_rejected(self, session):
        with pytest.raises(KeyError):
            await payments.set_enabled(session, "paypal", True)

    @pytest.mark.parametrize("value,shown", [("", "—"), ("short", "••••"), (TOKEN, "123••••Dsaw")])
    def test_mask(self, value, shown):
        assert payments.mask(value) == shown


# ─── Admin panel ──────────────────────────────────────────────────────────────

class TestAdminPanel:
    async def test_non_admin_cannot_open_or_change(self, session, make_call, make_state, as_admin):
        handlers = {
            "admin:pay": lambda c: admin_payments.payments_list(c, session, make_state()),
            "admin:pay:m:cryptobot": lambda c: admin_payments.payment_card(c, session, make_state()),
            "admin:pay:on:cryptobot": lambda c: admin_payments.payment_toggle(c, session),
            "admin:pay:clr!:cryptobot": lambda c: admin_payments.payment_clear(c, session),
            "admin:pay:f:cryptobot:api_token": lambda c: admin_payments.payment_edit_prompt(c, make_state()),
        }
        for data, handler in handlers.items():
            call = make_call(data, user_id=TEST_USER_ID)
            await handler(call)
            call.message.edit_text.assert_not_awaited()

    async def test_non_admin_text_is_never_saved(self, session, make_message, make_state, as_admin):
        state = make_state(TEST_USER_ID)
        await state.set_state(admin_payments.AdminPayment.value)
        await state.update_data(code="cryptobot", target="cred:api_token")
        await admin_payments.payment_value_input(make_message(TOKEN, user_id=TEST_USER_ID), session, state)
        assert (await payments.get_state(session, "cryptobot")).creds == {}

    async def test_list_shows_status_and_active_names(self, session, make_call, make_state, as_admin):
        await configure(session)                                               # active
        await payments.set_credential(session, "xrocket", "api_token", TOKEN)  # configured, off
        call = make_call("admin:pay", user_id=ADMIN_ID)
        await admin_payments.payments_list(call, session, make_state(ADMIN_ID))
        text = call.message.edit_text.await_args.args[0]
        markup = call.message.edit_text.await_args.kwargs["reply_markup"]
        assert "Активны сейчас (1): <b>CryptoBot</b>" in text
        labels = [b.text for row in markup.inline_keyboard for b in row]
        assert "🟢 CryptoBot" in labels and "🔴 XRocket" in labels and "⚪ Binance Pay" in labels

    async def test_admin_main_panel_lists_active_methods(self, session, make_message, as_admin):
        await configure(session)
        await configure(session, "sbp", shop_id="1", api_key="key-123456789")
        msg = make_message("/admin", user_id=ADMIN_ID)
        await admin.admin_panel(msg, session)
        assert "Активные способы оплаты: <b>СБП, CryptoBot</b>" in msg.answer.await_args.args[0]

    async def test_full_flow_enter_key_then_turn_on(self, session, make_call, make_message, make_state, as_admin):
        state = make_state(ADMIN_ID)

        # 1. tap "✏️ Crypto Pay API token"
        call = make_call("admin:pay:f:cryptobot:api_token", user_id=ADMIN_ID)
        await admin_payments.payment_edit_prompt(call, state)
        assert await state.get_state() == admin_payments.AdminPayment.value.state
        assert "будет удалено" in call.message.edit_text.await_args.args[0]

        # 2. send the key -> saved encrypted, message deleted, card shows masked key
        msg = make_message(TOKEN, user_id=ADMIN_ID)
        await admin_payments.payment_value_input(msg, session, state)
        msg.delete.assert_awaited_once()
        assert await state.get_state() is None
        reply = msg.answer.await_args.args[0]
        assert "✅ Сохранено" in reply and "123••••Dsaw" in reply and TOKEN not in reply

        # 3. turn it on
        call = make_call("admin:pay:on:cryptobot", user_id=ADMIN_ID)
        await admin_payments.payment_toggle(call, session)
        assert [m.code for m in await payments.active_methods(session)] == ["cryptobot"]
        assert "admin:pay:off:cryptobot" in callbacks(call.message.edit_text.await_args.kwargs["reply_markup"])

    async def test_turn_on_without_keys_shows_what_is_missing(self, session, make_call, as_admin):
        call = make_call("admin:pay:on:binance_pay", user_id=ADMIN_ID)
        await admin_payments.payment_toggle(call, session)
        assert alert_text(call) == "Сначала заполните: API-ключ, Секретный ключ"
        assert await payments.active_methods(session) == []

    async def test_command_while_waiting_cancels_instead_of_saving(self, session, make_message, make_state, as_admin):
        state = make_state(ADMIN_ID)
        await state.set_state(admin_payments.AdminPayment.value)
        await state.update_data(code="cryptobot", target="cred:api_token")
        await admin_payments.payment_value_input(make_message("/cancel", user_id=ADMIN_ID), session, state)
        assert (await payments.get_state(session, "cryptobot")).creds == {}
        assert await state.get_state() is None

    async def test_invalid_value_keeps_waiting(self, session, make_message, make_state, as_admin):
        state = make_state(ADMIN_ID)
        await state.set_state(admin_payments.AdminPayment.value)
        await state.update_data(code="stars", target="cred:rate")
        msg = make_message("abc", user_id=ADMIN_ID)
        await admin_payments.payment_value_input(msg, session, state)
        assert msg.answer.await_args.args[0].startswith("❌")
        assert await state.get_state() == admin_payments.AdminPayment.value.state

    async def test_set_title_and_min(self, session, make_message, make_state, as_admin):
        state = make_state(ADMIN_ID)
        for target, value in [("title", "СБП 2"), ("min", "150")]:
            await state.set_state(admin_payments.AdminPayment.value)
            await state.update_data(code="sbp", target=target)
            msg = make_message(value, user_id=ADMIN_ID)
            await admin_payments.payment_value_input(msg, session, state)
            msg.delete.assert_not_awaited()     # not a secret - no need to delete
        s = await payments.get_state(session, "sbp")
        assert s.title == "СБП 2" and s.min_amount == 150

    async def test_clear_keys_needs_confirmation(self, session, make_call, as_admin):
        await configure(session)
        call = make_call("admin:pay:clr:cryptobot", user_id=ADMIN_ID)
        await admin_payments.payment_clear_confirm(call)
        assert (await payments.get_state(session, "cryptobot")).is_active       # nothing deleted yet
        assert "admin:pay:clr!:cryptobot" in callbacks(call.message.edit_text.await_args.kwargs["reply_markup"])

        await admin_payments.payment_clear(make_call("admin:pay:clr!:cryptobot", user_id=ADMIN_ID), session)
        assert (await payments.get_state(session, "cryptobot")).creds == {}

    def test_all_admin_callbacks_fit_64_bytes(self):
        longest = max((f"admin:pay:f:{c}:{f.key}" for c, s in payments.METHODS.items() for f in s.fields), key=len)
        assert len(longest.encode()) <= 64


# ─── User top-up ──────────────────────────────────────────────────────────────

class TestUserTopUp:
    async def test_menu_shows_only_active_methods(self, session, make_user, make_call, make_state):
        await make_user()
        await configure(session)                                               # active
        await configure(session, "xrocket", enabled=False, api_token=TOKEN)    # configured but off
        call = make_call("menu:topup")
        await topup.show_topup(call, session, make_state())
        assert last_caption(call) == t("topup_text", "ru")
        assert callbacks(last_markup(call)) == ["topup:m:cryptobot", "menu:main"]

    async def test_menu_without_methods(self, session, make_user, make_call, make_state):
        await make_user(language="en")
        call = make_call("menu:topup")
        await topup.show_topup(call, session, make_state())
        assert last_caption(call) == t("topup_no_methods", "en")

    async def test_custom_title_and_language(self, session, make_user, make_call, make_state):
        await make_user(language="de")
        await configure(session, "sbp", shop_id="1", api_key="key-123456789")
        call = make_call("menu:topup")
        await topup.show_topup(call, session, make_state())
        assert last_markup(call).inline_keyboard[0][0].text == "SBP (Russland)"
        await payments.set_title(session, "sbp", "СБП 2")
        await topup.show_topup(call, session, make_state())
        assert last_markup(call).inline_keyboard[0][0].text == "СБП 2"

    async def test_choose_method_asks_amount(self, session, make_user, make_call, make_state):
        await make_user()
        await configure(session)
        await payments.set_min_amount(session, "cryptobot", 100)
        state = make_state()
        call = make_call("topup:m:cryptobot")
        await topup.choose_method(call, session, state)
        text = last_caption(call)
        assert text.startswith("Введите сумму пополнения в USD ($).")
        assert "Метод: CryptoBot" in text and "100.00 $" in text
        assert await state.get_state() == topup.TopUp.amount.state

    @pytest.mark.parametrize("code", ["xrocket", "paypal"])
    async def test_inactive_or_unknown_method_rejected(self, session, make_user, make_call, make_state, code):
        await make_user()
        state = make_state()
        call = make_call(f"topup:m:{code}")
        await topup.choose_method(call, session, state)
        assert alert_text(call) == t("topup_method_unavailable", "ru")
        assert await state.get_state() is None

    async def _waiting_for_amount(self, session, make_user, make_state, min_amount=None):
        await make_user()
        await configure(session)
        if min_amount:
            await payments.set_min_amount(session, "cryptobot", min_amount)
        state = make_state()
        await state.set_state(topup.TopUp.amount)
        await state.update_data(code="cryptobot")
        return state

    @pytest.mark.parametrize("text", ["abc", "-100", "0", "nan", "inf", "1e999", ""])
    async def test_bad_amount(self, session, make_user, make_message, make_state, text):
        state = await self._waiting_for_amount(session, make_user, make_state)
        msg = make_message(text)
        await topup.amount_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("topup_bad_amount", "ru")
        assert await state.get_state() == topup.TopUp.amount.state     # still waiting

    async def test_amount_limits(self, session, make_user, make_message, make_state):
        state = await self._waiting_for_amount(session, make_user, make_state, min_amount=100)
        msg = make_message("50")
        await topup.amount_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("topup_below_min", "ru", min="100.00 $")
        msg = make_message("1000000")
        await topup.amount_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("topup_above_max", "ru", max="100000.00 $")

    @pytest.mark.parametrize("text,shown", [("500", "500.00 $"), ("1 500,50", "1500.50 $")])
    async def test_valid_amount(self, session, make_user, make_message, make_state, text, shown):
        state = await self._waiting_for_amount(session, make_user, make_state)
        msg = make_message(text)
        await topup.amount_input(msg, session, state)
        assert shown in msg.answer.await_args.args[0]
        assert await state.get_state() is None

    async def test_method_turned_off_while_user_types(self, session, make_user, make_message, make_state):
        state = await self._waiting_for_amount(session, make_user, make_state)
        await payments.set_enabled(session, "cryptobot", False)
        msg = make_message("500")
        await topup.amount_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t("topup_method_unavailable", "ru")
        assert await state.get_state() is None
