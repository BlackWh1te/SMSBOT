"""/start, TOS, language, main menu, profile and top-up screens."""

from unittest.mock import AsyncMock

import pytest
from aiogram.types import FSInputFile

import bot.utils.banner as banner
from bot.db.queries import get_banner_file_id
from bot.handlers import profile, start
from bot.i18n import t
from tests.conftest import TEST_USER_ID, callbacks, last_caption, last_markup


class TestStart:
    async def test_new_user_is_asked_for_language_first(self, session, make_message, reload_user):
        msg = make_message("/start")
        await start.cmd_start(msg, session)

        user = await reload_user()
        assert user is not None and user.username == "tester" and user.tos_accepted is False
        msg.answer_animation.assert_not_awaited()                     # no agreement before the language
        assert msg.answer.await_args.args[0] == start.LANGUAGE_PROMPT
        assert callbacks(msg.answer.await_args.kwargs["reply_markup"]) == ["lang:ru", "lang:en", "lang:de"]

    @pytest.mark.parametrize("lang", ["ru", "en", "de"])
    async def test_agreement_comes_in_the_chosen_language(self, session, make_message, make_call, reload_user, lang):
        await start.cmd_start(make_message("/start"), session)
        call = make_call(f"lang:{lang}")
        await start.set_language(call, session)

        assert (await reload_user()).language == lang
        call.message.delete.assert_awaited_once()                     # the picker
        anim = call.message.answer_animation.await_args.kwargs
        assert isinstance(anim["animation"], FSInputFile) and len(anim["caption"]) <= 1024
        tos = call.message.answer.await_args.kwargs
        assert t("tos_title", lang) in tos["text"] and t("tos_text", lang)[:40] in tos["text"]
        assert callbacks(tos["reply_markup"]) == ["tos:accept"]
        assert tos["reply_markup"].inline_keyboard[0][0].text == t("tos_accept_btn", lang)

    async def test_banner_url_sends_tos_as_single_message(self, session, make_message, make_call, monkeypatch):
        monkeypatch.setattr(start, "BANNER_URL", "https://example.com/banner.mp4")
        await start.cmd_start(make_message("/start"), session)
        call = make_call("lang:en")
        await start.set_language(call, session)

        call.message.answer_animation.assert_not_awaited()
        call.message.answer.assert_awaited_once()
        kw = call.message.answer.await_args.kwargs
        assert t("tos_title", "en") in kw["text"] and len(kw["text"]) <= 4096
        assert callbacks(kw["reply_markup"]) == ["tos:accept"]
        preview = kw["link_preview_options"]
        assert preview.url == "https://example.com/banner.mp4"
        assert preview.show_above_text and preview.prefer_large_media

    async def test_first_upload_caches_file_id(self, session, make_message, make_call):
        await start.cmd_start(make_message("/start"), session)
        await start.set_language(make_call("lang:ru"), session)
        assert banner._banner_file_id == "NEW_TG_FILE_ID"
        assert await get_banner_file_id(session) == "NEW_TG_FILE_ID"

    async def test_cached_banner_is_reused(self, session, make_user, make_call):
        await make_user(tos_accepted=False)
        banner._banner_file_id = "CACHED"
        call = make_call("lang:ru")
        await start.set_language(call, session)
        assert call.message.answer_animation.await_args.kwargs["animation"] == "CACHED"

    async def test_returning_user_sees_main_menu_and_reply_keyboard(self, session, make_message, make_user):
        await make_user(balance=123.456, language="en", selected_country_name="Narnia")
        msg = make_message("/start")
        await start.cmd_start(msg, session)

        kb = msg.answer.await_args.kwargs["reply_markup"]               # reply keyboard, not the agreement
        assert [[b.text for b in row] for row in kb.keyboard] == [
            [t("btn_buy_number", "en"), t("btn_topup", "en")], [t("btn_profile", "en"), t("btn_menu", "en")]]
        kw = msg.answer_animation.await_args.kwargs
        assert "123.46" in kw["caption"] and "Narnia" in kw["caption"]
        assert "menu:buy" in callbacks(kw["reply_markup"])

    async def test_main_menu_without_country_shows_placeholder(self, session, make_message, make_user):
        await make_user(language="de")
        msg = make_message("/start")
        await start.cmd_start(msg, session)
        assert t("country_not_selected", "de") in msg.answer_animation.await_args.kwargs["caption"]


class TestTosAndLanguage:
    async def test_accept_tos_opens_main_menu(self, session, make_user, make_call, reload_user):
        await make_user(tos_accepted=False, language="de")
        call = make_call("tos:accept")
        await start.tos_accepted(call, session)
        assert (await reload_user()).tos_accepted is True
        call.message.edit_reply_markup.assert_awaited_once_with(reply_markup=None)   # agreement text stays
        assert "menu:buy" in callbacks(call.message.answer_animation.await_args.kwargs["reply_markup"])
        assert call.message.answer.await_args_list[0].kwargs["reply_markup"].keyboard[0][0].text == \
            t("btn_buy_number", "de")

    @pytest.mark.parametrize("lang", ["ru", "en", "de"])
    async def test_change_language_shows_menu_and_closable_notice(self, session, make_user, make_call,
                                                                  reload_user, lang):
        await make_user()
        call = make_call(f"lang:{lang}")
        await start.set_language(call, session)
        assert (await reload_user()).language == lang
        call.message.delete.assert_awaited_once()
        caption = call.message.answer_animation.await_args.kwargs["caption"]
        assert caption == t("main_menu_text", lang, balance="0.00 €" if lang == "de" else "0.00 $",
                            country=t("country_not_selected", lang), operator=t("no_operator", lang))
        notice = call.message.answer.await_args
        assert notice.args[0] == t("lang_changed", lang)
        assert callbacks(notice.kwargs["reply_markup"]) == ["close"]
        assert notice.kwargs["reply_markup"].inline_keyboard[0][0].text == t("btn_close", lang)

    async def test_close_deletes_the_notice(self, make_call):
        call = make_call("close")
        await start.close_message(call)
        call.message.delete.assert_awaited_once()
        call.answer.assert_awaited_once()

    async def test_set_language_rejects_unknown_code(self, session, make_user, make_call, reload_user):
        await make_user()
        await start.set_language(make_call("lang:xx'); --"), session)
        assert (await reload_user()).language in ("ru", "en", "de")


class TestMainMenuEdit:
    async def test_back_to_main_edits_in_place(self, session, make_user, make_call):
        await make_user(balance=5)
        call = make_call("menu:main")
        await profile.back_to_main(call, session)
        call.message.edit_caption.assert_awaited_once()
        call.message.answer_animation.assert_not_awaited()

    async def test_falls_back_to_new_message_when_edit_fails(self, session, make_user, make_call):
        await make_user()
        call = make_call("menu:main")
        call.message.edit_caption = AsyncMock(side_effect=Exception("message is not an animation"))
        await profile.back_to_main(call, session)
        call.message.answer_animation.assert_awaited_once()


class TestProfile:
    async def test_profile_activation_rate(self, session, make_user, make_call):
        await make_user(total_bought=4, total_activated=3, balance=10, language="en")
        call = make_call("menu:profile")
        await profile.show_profile(call, session)
        text = last_caption(call)
        assert "75.0" in text and str(TEST_USER_ID) in text
        assert "profile:api" in callbacks(last_markup(call))

    async def test_profile_zero_purchases_no_division_error(self, session, make_user, make_call):
        await make_user(total_bought=0)
        call = make_call("menu:profile")
        await profile.show_profile(call, session)
        call.answer.assert_awaited()

    async def test_api_key_shown(self, session, make_user, make_call):
        user = await make_user()
        call = make_call("profile:api")
        await profile.show_api_key(call, session)
        assert user.api_key in last_caption(call)

    async def test_referral_link_contains_user_id(self, session, make_user, make_call):
        await make_user()
        call = make_call("profile:referrals")
        await profile.show_referrals(call, session)
        assert f"?start=ref{TEST_USER_ID}" in last_caption(call)

    @pytest.mark.parametrize("handler,data,back", [
        (profile.transfer_balance, "profile:transfer", "menu:profile"),
        (profile.show_cashback, "profile:cashback", "menu:profile"),
        (profile.show_info, "profile:info", "menu:profile"),
        (profile.disable_ads, "profile:disable_ads", "menu:profile"),
        (profile.topup_crypto, "topup:crypto", "menu:topup"),
        (profile.anon_numbers, "menu:anon", "menu:main"),
    ])
    async def test_static_screens_have_back_button(self, session, make_user, make_call, handler, data, back):
        await make_user()
        call = make_call(data)
        await handler(call, session)
        assert callbacks(last_markup(call))[0] == back
        call.answer.assert_awaited()

    async def test_fill_command_without_methods(self, session, make_message):
        msg = make_message("/fill")
        await start.cmd_fill(msg, session)
        kw = msg.answer.await_args.kwargs
        assert kw["text"] == t("topup_no_methods", "ru")
        assert callbacks(kw["reply_markup"]) == ["menu:main"]

    async def test_fill_command_lists_active_methods(self, session, make_message):
        from bot import payments
        await payments.set_credential(session, "cryptobot", "api_token", "123:ABCDEF")
        await payments.set_enabled(session, "cryptobot", True)
        msg = make_message("/fill")
        await start.cmd_fill(msg, session)
        assert callbacks(msg.answer.await_args.kwargs["reply_markup"]) == ["topup:m:cryptobot", "menu:main"]
