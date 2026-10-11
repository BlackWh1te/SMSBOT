"""Country/rank/status translations, operator label, reply keyboard, green buttons."""

import pytest

from bot import countries
from bot.handlers import profile, reply_menu
from bot.i18n import operator_label, status_label, t
from bot.keyboards import main_menu_keyboard, profile_keyboard
from tests.conftest import TEST_USER_ID, callbacks


class TestCountries:
    @pytest.mark.parametrize("name,lang,expected", [
        ("🇷🇺 Россия", "en", "🇷🇺 Russia"), ("🇩🇪 Германия", "de", "🇩🇪 Deutschland"),
        ("🇺🇸 США", "en", "🇺🇸 USA"), ("🇷🇺 Россия", "ru", "🇷🇺 Россия"),
        ("ЦАР", "de", "Zentralafrikanische Republik"), ("США (виртуальные)", "en", "USA (virtual)"),
        ("Неизвестная", "en", "Неизвестная"), (None, "en", None)])
    def test_localize(self, name, lang, expected):
        assert countries.localize(name, lang) == expected

    @pytest.mark.parametrize("query", ["germ", "deutsch", "герм"])
    def test_search_in_any_language(self, query):
        assert countries.matches("🇩🇪 Германия", query)
        assert not countries.matches("🇫🇷 Франция", query)

    async def test_country_list_is_translated(self, session, make_user, make_call, fake_getsms):
        from bot.handlers import activation
        fake_getsms.get_countries.return_value = [{"id": 1, "name": "🇷🇺 Россия"}, {"id": 2, "name": "🇩🇪 Германия"}]
        await make_user(language="en")
        call = make_call("menu:country")
        await activation.country_menu(call, session)
        texts = [b.text for row in call.message.edit_caption.await_args.kwargs["reply_markup"].inline_keyboard
                 for b in row]
        assert "🇷🇺 Russia" in texts and "🇩🇪 Germany" in texts


class TestLabels:
    @pytest.mark.parametrize("lang", ["ru", "en", "de"])
    def test_statuses_translated(self, lang):
        for code in ("WAITING", "RECEIVED", "FINISHED", "CANCELLED", "WAIT"):
            assert status_label(code, lang) == t(f"order_status_{code}", lang) != f"order_status_{code}"
        assert status_label("WEIRD", lang) == "WEIRD" and status_label(None, lang) == "—"

    @pytest.mark.parametrize("op,lang,expected", [("any", "ru", "отсутствует"), (None, "en", "none"),
                                                   ("mts", "de", "mts")])
    def test_operator(self, op, lang, expected):
        assert operator_label(op, lang) == expected

    @pytest.mark.parametrize("stored,lang,expected", [("Новичок", "en", "Newbie"), (None, "de", "Neuling"),
                                                       ("VIP", "en", "VIP")])
    def test_rank(self, stored, lang, expected):
        assert profile.rank_label(stored, lang) == expected


class TestGreenButtons:
    def test_accent_buttons_like_the_original(self):
        styles = {b.callback_data: getattr(b, "style", None)
                  for kb in (main_menu_keyboard("ru"), profile_keyboard("ru")) for row in kb.inline_keyboard for b in row}
        assert {k for k, v in styles.items() if v == "success"} == {
            "menu:buy", "menu:topup", "profile:transfer", "profile:disable_ads"}


class TestReplyKeyboard:
    @pytest.mark.parametrize("lang", ["ru", "en", "de"])
    async def test_profile_button_in_any_language(self, session, make_user, make_message, make_state, lang):
        await make_user(language="ru")                 # keyboard from before a language change still works
        msg = make_message(t("btn_profile", lang))
        await reply_menu.reply_menu(msg, session, make_state())
        kw = msg.answer_animation.await_args.kwargs
        assert str(TEST_USER_ID) in kw["caption"] and "profile:language" in callbacks(kw["reply_markup"])

    async def test_buy_without_country_opens_country_list(self, session, make_user, make_message, make_state,
                                                          fake_getsms):
        await make_user()
        msg = make_message(t("btn_buy_number", "ru"))
        await reply_menu.reply_menu(msg, session, make_state())
        assert "country:1" in callbacks(msg.answer_animation.await_args.kwargs["reply_markup"])

    async def test_buy_with_country_opens_services(self, session, make_user, make_message, make_state, fake_getsms):
        await make_user(selected_country_id=1, selected_country_name="🇷🇺 Россия")
        msg = make_message(t("btn_buy_number", "ru"))
        await reply_menu.reply_menu(msg, session, make_state())
        assert "service:101" in callbacks(msg.answer_animation.await_args.kwargs["reply_markup"])

    async def test_topup_and_menu(self, session, make_user, make_message, make_state):
        await make_user()
        msg = make_message(t("btn_topup", "ru"))
        await reply_menu.reply_menu(msg, session, make_state())
        assert "menu:main" in callbacks(msg.answer_animation.await_args.kwargs["reply_markup"])
        msg = make_message(t("btn_menu", "ru"))
        await reply_menu.reply_menu(msg, session, make_state())
        assert "menu:buy" in callbacks(msg.answer_animation.await_args.kwargs["reply_markup"])

    async def test_button_cancels_pending_input(self, session, make_user, make_message, make_state):
        from bot.handlers.topup import TopUp
        await make_user()
        state = make_state()
        await state.set_state(TopUp.amount)
        await reply_menu.reply_menu(make_message(t("btn_profile", "ru")), session, state)
        assert await state.get_state() is None

    async def test_before_agreement_asks_language(self, session, make_user, make_message, make_state):
        from bot.handlers import start
        await make_user(tos_accepted=False)
        msg = make_message(t("btn_profile", "ru"))
        await reply_menu.reply_menu(msg, session, make_state())
        assert msg.answer.await_args.args[0] == start.LANGUAGE_PROMPT
