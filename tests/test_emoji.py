"""Live (custom) emoji: text/button rewriting, extraction from forwards, fallback, admin section."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, SendMessage, SendPhoto
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, MessageEntity

from bot import emoji
from bot.handlers import admin, admin_emoji
from tests.conftest import ADMIN_ID, callbacks

CHECK, HEART = "111", "222"


@pytest.fixture(autouse=True)
def _mapping():
    emoji.set_mapping({"✅": CHECK, "❤️": HEART})
    yield
    emoji.set_mapping({})


def bot(parse_mode="HTML"):
    return SimpleNamespace(default=DefaultBotProperties(parse_mode=parse_mode))


def kb(*texts):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data="x")] for t in texts])


class TestRewrite:
    def test_text(self):
        assert emoji.apply_html("✅ <b>ok</b> ❤️ ❤") == (
            f'<tg-emoji emoji-id="{CHECK}">✅</tg-emoji> <b>ok</b> '
            f'<tg-emoji emoji-id="{HEART}">❤️</tg-emoji> <tg-emoji emoji-id="{HEART}">❤</tg-emoji>')

    def test_skips_code_links_and_attributes(self):
        text = '<code>✅</code> <a href="https://x/✅">✅</a> <pre>✅</pre>'
        assert emoji.apply_html(text) == text

    def test_no_mapping_is_noop(self):
        emoji.set_mapping({})
        assert emoji.apply_html("✅") == "✅"
        assert emoji.transform(bot(), SendMessage(chat_id=1, text="✅")) is None

    def test_button_gets_icon(self):
        b = emoji.apply_markup(kb("✅ Оплатить", "✅", "Оплатить ✅")).inline_keyboard
        assert (b[0][0].text, b[0][0].icon_custom_emoji_id, b[0][0].callback_data) == ("Оплатить", CHECK, "x")
        assert b[1][0].text == "✅" and getattr(b[1][0], "icon_custom_emoji_id", None) is None
        assert b[2][0].text == "Оплатить ✅" and getattr(b[2][0], "icon_custom_emoji_id", None) is None

    def test_icon_is_serialized(self):
        b = emoji.apply_button(InlineKeyboardButton(text="✅ Да", callback_data="x"))
        assert b.model_dump(exclude_none=True)["icon_custom_emoji_id"] == CHECK

    def test_transform_default_and_explicit_html(self):
        m = emoji.transform(bot(), SendMessage(chat_id=1, text="✅ hi", reply_markup=kb("✅ a")))
        assert "tg-emoji" in m.text and m.reply_markup.inline_keyboard[0][0].text == "a"
        p = emoji.transform(bot(None), SendPhoto(chat_id=1, photo="f", caption="✅", parse_mode="HTML"))
        assert "tg-emoji" in p.caption

    def test_plain_text_is_not_touched(self):
        assert emoji.transform(bot(None), SendMessage(chat_id=1, text="✅")) is None
        assert emoji.transform(bot(), AnswerCallbackQuery(callback_query_id="1", text="✅")) is None


class TestMiddleware:
    async def test_sends_live_version(self):
        make = AsyncMock(return_value="ok")
        assert await emoji.LiveEmojiMiddleware()(make, bot(), SendMessage(chat_id=1, text="✅")) == "ok"
        assert "tg-emoji" in make.await_args.args[1].text

    async def test_falls_back_to_plain_when_rejected(self):
        method = SendMessage(chat_id=1, text="✅")
        make = AsyncMock(side_effect=[TelegramBadRequest(method=method, message="DOCUMENT_INVALID"), "ok"])
        assert await emoji.LiveEmojiMiddleware()(make, bot(), method) == "ok"
        assert make.await_args.args[1] is method

    async def test_not_modified_is_not_retried(self):
        method = SendMessage(chat_id=1, text="✅")
        make = AsyncMock(side_effect=TelegramBadRequest(method=method, message="message is not modified"))
        with pytest.raises(TelegramBadRequest):
            await emoji.LiveEmojiMiddleware()(make, bot(), method)
        assert make.await_count == 1


class TestStorageAndAdmin:
    def test_extract(self):
        msg = SimpleNamespace(text="🔥 Привет ❤️", caption=None, caption_entities=None, entities=[
            MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="900"),
            MessageEntity(type="custom_emoji", offset=10, length=2, custom_emoji_id="901")])
        assert emoji.extract(msg) == {"🔥": "900", "❤": "901"}

    async def test_save_load_remove(self, session):
        emoji.set_mapping({})
        await emoji.save(session, {"🔥": "900", "✅": "901"})
        emoji.set_mapping({})
        await emoji.load(session)
        assert emoji.mapping() == {"🔥": "900", "✅": "901"}
        await emoji.remove(session, "🔥")
        assert emoji.mapping() == {"✅": "901"}
        await emoji.remove(session)
        assert emoji.mapping() == {}

    def test_admin_menu_has_button(self):
        assert "admin:emoji" in callbacks(admin.admin_menu_kb())

    async def test_collect_flow(self, session, make_call, make_message, make_state, monkeypatch):
        monkeypatch.setattr(admin, "ADMIN_IDS", [ADMIN_ID])
        emoji.set_mapping({})
        state = make_state(ADMIN_ID)
        await admin_emoji.emoji_add(make_call("admin:emoji:add", ADMIN_ID), state)
        assert await state.get_state() == admin_emoji.AdminEmoji.collect.state

        msg = make_message("🔥 hi", ADMIN_ID)
        msg.entities = [MessageEntity(type="custom_emoji", offset=0, length=2, custom_emoji_id="900")]
        msg.caption, msg.caption_entities = None, None
        await admin_emoji.emoji_collect(msg, session, state)
        assert emoji.mapping() == {"🔥": "900"}
        assert "Сохранено 1" in msg.answer.await_args.args[0]

    async def test_non_admin_ignored(self, make_call, make_state):
        state = make_state()
        await admin_emoji.emoji_add(make_call("admin:emoji:add"), state)
        assert await state.get_state() is None


class TestBundledDefaults:
    async def test_seeded_once_and_admin_delete_is_kept(self, session):
        import json
        bundled = json.loads(emoji.DEFAULTS_FILE.read_text(encoding="utf-8"))
        assert len(bundled) >= 60 and all(v.isdigit() for v in bundled.values())
        assert await emoji.seed_defaults(session) == len(bundled)
        await emoji.load(session)
        assert emoji.mapping()["🔙"] == bundled["🔙"]
        await emoji.remove(session)                       # owner pressed "delete all"
        assert await emoji.seed_defaults(session) == 0    # restart does not bring them back
        await emoji.load(session)
        assert emoji.mapping() == {}

    def test_every_menu_button_gets_an_icon(self):
        import json
        from bot.i18n import t
        emoji.set_mapping(json.loads(emoji.DEFAULTS_FILE.read_text(encoding="utf-8")))
        for key in ("btn_buy_number", "btn_profile", "btn_back", "btn_email_create"):
            btn = emoji.apply_button(InlineKeyboardButton(text=t(key, "ru"), callback_data="x"))
            assert getattr(btn, "icon_custom_emoji_id", None), key
