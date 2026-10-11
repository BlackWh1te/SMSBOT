"""/buy command, caption-less screens, GetSMS-empty alert, command menu."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageText, SendMessage

from bot import emoji
from bot.handlers import activation, admin, reply_menu
from bot.i18n import t
from bot.middlewares.requests import CaptionFallbackMiddleware
from tests.conftest import ADMIN_ID, callbacks

NO_CAPTION = "Bad Request: there is no caption in the message to edit"


class TestBuyCommand:
    async def test_buy_opens_services(self, session, make_user, make_message, make_state, fake_getsms):
        await make_user(selected_country_id=1, selected_country_name="🇷🇺 Россия")
        msg = make_message("/buy")
        await reply_menu.cmd_buy(msg, session, make_state())
        assert "service:101" in callbacks(msg.answer_animation.await_args.kwargs["reply_markup"])

    async def test_buy_for_brand_new_user_asks_language(self, session, make_message, make_state, reload_user):
        msg = make_message("/buy")
        await reply_menu.cmd_buy(msg, session, make_state())
        assert (await reload_user()).username == "tester"
        assert callbacks(msg.answer.await_args.kwargs["reply_markup"]) == ["lang:ru", "lang:en", "lang:de"]


class TestCaptionFallback:
    async def test_text_message_gets_its_text_edited(self):
        method = EditMessageCaption(chat_id=1, message_id=2, caption="<b>hi</b>")
        make = AsyncMock(side_effect=[TelegramBadRequest(method=method, message=NO_CAPTION), "ok"])
        assert await CaptionFallbackMiddleware()(make, MagicMock(), method) == "ok"
        retry = make.await_args.args[1]
        assert isinstance(retry, EditMessageText) and (retry.chat_id, retry.message_id, retry.text) == (1, 2, "<b>hi</b>")

    @pytest.mark.parametrize("method,error", [
        (EditMessageCaption(chat_id=1, message_id=2, caption="x"), "message is not modified"),
        (SendMessage(chat_id=1, text="x"), NO_CAPTION)])
    async def test_other_errors_pass_through(self, method, error):
        make = AsyncMock(side_effect=TelegramBadRequest(method=method, message=error))
        with pytest.raises(TelegramBadRequest):
            await CaptionFallbackMiddleware()(make, MagicMock(), method)
        assert make.await_count == 1

    async def test_emoji_middleware_does_not_retry_unrelated_errors(self):
        emoji.set_mapping({"✅": "1"})
        try:
            method = EditMessageCaption(chat_id=1, message_id=2, caption="✅ x", parse_mode="HTML")
            make = AsyncMock(side_effect=TelegramBadRequest(method=method, message=NO_CAPTION))
            with pytest.raises(TelegramBadRequest):
                await emoji.LiveEmojiMiddleware()(make, MagicMock(default=None), method)
            assert make.await_count == 1
        finally:
            emoji.set_mapping({})


class TestProviderEmptyAlert:
    @pytest.fixture(autouse=True)
    def _admins(self, monkeypatch):
        monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])
        monkeypatch.setattr(admin, "_last_provider_alert", -1e9)

    async def test_admins_are_told_once_per_window(self):
        bot = MagicMock(send_message=AsyncMock())
        assert await admin.alert_provider_empty(bot, "INSUFFICIENT_BALANCE") is True
        assert bot.send_message.await_args.args[0] == ADMIN_ID
        assert await admin.alert_provider_empty(bot, "INSUFFICIENT_BALANCE") is False
        assert bot.send_message.await_count == 1

    async def test_other_errors_do_not_alert(self):
        bot = MagicMock(send_message=AsyncMock())
        assert await admin.alert_provider_empty(bot, "NO_NUMBERS") is False
        bot.send_message.assert_not_awaited()

    async def test_purchase_refunds_and_alerts(self, session, make_user, make_call, fake_getsms, reload_user,
                                               monkeypatch):
        monkeypatch.setattr(activation, "poll_sms", AsyncMock())
        await make_user(balance=100)
        fake_getsms.create_order.return_value = {"ok": False, "error_code": "INSUFFICIENT_BALANCE"}
        call = make_call("order:create:105")
        call.bot.send_message = AsyncMock()
        await activation.create_order(call, session)
        assert (await reload_user()).balance == 100                         # user keeps the money
        assert call.bot.send_message.await_args.args[0] == ADMIN_ID
        assert call.message.edit_caption.await_args.kwargs["caption"] == t("error_generic", "ru")


class TestServiceCardCount:
    async def test_unknown_count_is_not_shown_as_zero(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1)
        fake_getsms.get_service_count.return_value = None
        call = make_call("service:105")
        await activation.show_service_card(call, session)
        assert call.message.edit_caption.await_args.kwargs["reply_markup"].inline_keyboard[0][0].text.endswith("? шт")
