"""Search, favorites, top-10, unsupported features, balance transfer, last_seen, status text, money drift."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.db.models import User, utcnow
from bot.db.queries import add_balance, deduct_balance, get_favorites, toggle_favorite, touch_last_seen
from bot.db.queries import transfer_balance as do_transfer
from bot.handlers import activation, profile
from bot.i18n import t
from tests.conftest import OTHER_USER_ID, TEST_USER_ID, alert_text, callbacks, last_caption, last_markup


# ─── Money: Float column, but every write is rounded in SQL ──────────────────

class TestNoMoneyDrift:
    async def test_thousands_of_small_operations_stay_exact(self, session, make_user, reload_user):
        await make_user(balance=0)
        for _ in range(1000):
            await add_balance(session, TEST_USER_ID, 0.1)
        assert (await reload_user()).balance == 100.0          # plain float math would give 99.9999999...
        for _ in range(1000):
            await deduct_balance(session, TEST_USER_ID, 0.07)
        assert (await reload_user()).balance == 30.0


# ─── Transfer between users ──────────────────────────────────────────────────

class TestTransfer:
    @pytest.mark.parametrize("text,parsed", [
        ("123 50", (123, 50.0)), ("123 1,5", (123, 1.5)), ("123 0.5", None), ("abc 5", None),
        ("123", None), ("123 nan", None), ("123 inf", None), ("123 -5", None), ("", None)])
    def test_parse(self, text, parsed):
        assert profile.parse_transfer(text) == parsed

    async def test_atomic_transfer(self, session, make_user, reload_user):
        await make_user(balance=100)
        await make_user(OTHER_USER_ID, balance=5)
        assert await do_transfer(session, TEST_USER_ID, OTHER_USER_ID, 40) is True
        assert (await reload_user()).balance == 60 and (await reload_user(OTHER_USER_ID)).balance == 45

    @pytest.mark.parametrize("to,amount", [(OTHER_USER_ID, 1000), (TEST_USER_ID, 10), (404, 10),
                                           (OTHER_USER_ID, -5), (OTHER_USER_ID, float("nan"))])
    async def test_transfer_refused_changes_nothing(self, session, make_user, reload_user, to, amount):
        await make_user(balance=100)
        await make_user(OTHER_USER_ID, balance=5)
        assert await do_transfer(session, TEST_USER_ID, to, amount) is False
        assert (await reload_user()).balance == 100 and (await reload_user(OTHER_USER_ID)).balance == 5

    async def test_concurrent_transfers_cannot_overspend(self, session_factory, make_user, reload_user):
        await make_user(balance=100)
        await make_user(OTHER_USER_ID, balance=0)

        async def send():
            async with session_factory() as s:
                return await do_transfer(s, TEST_USER_ID, OTHER_USER_ID, 70)

        results = await asyncio.gather(send(), send())
        assert results.count(True) == 1
        assert (await reload_user()).balance == 30 and (await reload_user(OTHER_USER_ID)).balance == 70

    async def test_full_flow_with_confirmation(self, session, make_user, make_call, make_message, make_state,
                                               reload_user):
        await make_user(balance=100)
        await make_user(OTHER_USER_ID, balance=0, language="en")
        state = make_state()
        call = make_call("profile:transfer")
        await profile.transfer_balance(call, session, state)
        assert await state.get_state() == profile.TransferFlow.input.state

        msg = make_message(f"{OTHER_USER_ID} 25")
        await profile.transfer_input(msg, session, state)
        assert callbacks(msg.answer.await_args.kwargs["reply_markup"]) == ["transfer:ok", "transfer:no"]
        assert (await reload_user()).balance == 100            # nothing moves before confirming

        ok = make_call("transfer:ok")
        ok.bot.send_message = AsyncMock()
        await profile.transfer_confirm(ok, session, state)
        assert (await reload_user()).balance == 75 and (await reload_user(OTHER_USER_ID)).balance == 25
        assert ok.bot.send_message.await_args.args[0] == OTHER_USER_ID
        assert ok.bot.send_message.await_args.args[1] == t("transfer_received", "en", amount="25.00 $",
                                                           sender=TEST_USER_ID)

    @pytest.mark.parametrize("text,key", [("abc", "transfer_invalid"), (f"{TEST_USER_ID} 5", "transfer_self"),
                                          ("404 5", "transfer_no_user")])
    async def test_bad_input(self, session, make_user, make_message, make_state, text, key):
        await make_user(balance=100)
        state = make_state()
        await state.set_state(profile.TransferFlow.input)
        msg = make_message(text)
        await profile.transfer_input(msg, session, state)
        assert msg.answer.await_args.args[0] == t(key, "ru", min="1.00 $")

    async def test_cancel(self, session, make_user, make_call, make_state, reload_user):
        await make_user(balance=100)
        state = make_state()
        await state.update_data(transfer_to=OTHER_USER_ID, transfer_amount=10)
        await profile.transfer_cancel(make_call("transfer:no"), session, state)
        assert await state.get_data() == {}
        assert (await reload_user()).balance == 100


# ─── Favorites ────────────────────────────────────────────────────────────────

class TestFavorites:
    async def test_toggle(self, session, make_user, make_call, fake_getsms):
        await make_user(selected_country_id=1, selected_country_name="Country1")
        call = make_call("fav:101")
        await activation.toggle_favorite_service(call, session)
        assert alert_text(call) == t("fav_added", "ru")
        assert [f.service_name for f in await get_favorites(session, TEST_USER_ID)] == ["Service1"]
        await activation.toggle_favorite_service(call, session)
        assert alert_text(call) == t("fav_removed", "ru")
        assert await get_favorites(session, TEST_USER_ID) == []

    async def test_list_and_open_switches_country(self, session, make_user, make_call, fake_getsms, reload_user):
        await make_user(selected_country_id=1, selected_country_name="Country1")
        await toggle_favorite(session, TEST_USER_ID, 105, "Service5", 7, "Country7")
        call = make_call("favs")
        await activation.show_favorites(call, session)
        fav_cb = callbacks(last_markup(call))[0]
        assert fav_cb.startswith("favopen:")

        open_call = make_call(fav_cb)
        await activation.open_favorite(open_call, session)
        assert (await reload_user()).selected_country_id == 7
        assert "order:create:105" in callbacks(last_markup(open_call))

    async def test_empty(self, session, make_user, make_call):
        await make_user()
        call = make_call("favs")
        await activation.show_favorites(call, session)
        assert last_caption(call) == t("favorites_empty", "ru")

    async def test_cannot_open_someone_elses_favorite(self, session, make_user, make_call, fake_getsms):
        await make_user()
        await toggle_favorite(session, OTHER_USER_ID, 105, "Service5", 7, "Country7")
        fav_id = (await get_favorites(session, OTHER_USER_ID))[0].id
        call = make_call(f"favopen:{fav_id}")
        await activation.open_favorite(call, session)
        assert alert_text(call) == t("order_not_found", "ru")


# ─── Top-10 countries ─────────────────────────────────────────────────────────

class TestTop10:
    @pytest.fixture(autouse=True)
    def _clear_cache(self):
        activation._top_cache.clear()
        yield
        activation._top_cache.clear()

    @pytest.fixture
    def priced(self, fake_getsms):
        async def services(country_id, is_rent=False):
            # Telegram costs (60 - country_id) RUB, so higher ids are cheaper; country 3 doesn't sell it
            if country_id == 3:
                return [{"id": 1, "name": "Other", "price": 1}]
            return [{"id": 1000 + country_id, "name": "Telegram", "price": 60 - country_id}]
        fake_getsms.get_services.side_effect = services
        return fake_getsms

    async def test_cheapest_first_and_cached(self, priced):
        top = await activation.cheapest_countries("telegram")
        assert [r["country_id"] for r in top] == [50, 49, 48, 47, 46, 45, 44, 43, 42, 41]
        assert top[0] == {"country_id": 50, "country": "Country50", "service_id": 1050, "price": 10.0}
        calls = priced.get_services.await_count
        await activation.cheapest_countries("Telegram ")
        assert priced.get_services.await_count == calls        # served from cache

    async def test_screen_and_pick(self, session, make_user, make_call, priced, reload_user):
        await make_user(selected_country_id=1, selected_country_name="Country1")
        call = make_call("top10:1001")
        await activation.top10_countries(call, session)
        cbs = callbacks(call.message.edit_caption.await_args.kwargs["reply_markup"])
        assert cbs[0] == "topc:50:1050" and cbs[-1] == "service:1001"

        pick = make_call("topc:50:1050")
        await activation.top10_pick(pick, session)
        assert (await reload_user()).selected_country_id == 50
        assert "order:create:1050" in callbacks(last_markup(pick))


# ─── Search ───────────────────────────────────────────────────────────────────

class TestSearch:
    async def _search(self, session, make_call, make_message, make_state, kind, query):
        state = make_state()
        await activation.search_handler(make_call(f"search:{kind}"), session, state)
        msg = make_message(query)
        msg.bot.edit_message_caption = AsyncMock()
        handler = activation.search_service_input if kind == "service" else activation.search_country_input
        await handler(msg, session, state)
        return state, msg

    async def test_service_search_uses_api(self, session, make_user, make_call, make_message, make_state,
                                           fake_getsms):
        await make_user(selected_country_id=1)
        fake_getsms.search_services.return_value = [{"id": 110, "name": "Telegram"}]
        state, msg = await self._search(session, make_call, make_message, make_state, "service", "tele")
        fake_getsms.search_services.assert_awaited_once_with(1, "tele")
        kw = msg.bot.edit_message_caption.await_args.kwargs
        assert kw["caption"] == t("search_results", "ru", count=1)
        assert "service:110" in callbacks(kw["reply_markup"])
        assert await state.get_state() is None

    async def test_service_search_falls_back_to_local_match(self, session, make_user, make_call, make_message,
                                                            make_state, fake_getsms):
        await make_user(selected_country_id=1)
        fake_getsms.search_services.return_value = []
        _, msg = await self._search(session, make_call, make_message, make_state, "service", "service1")
        cbs = callbacks(msg.bot.edit_message_caption.await_args.kwargs["reply_markup"])
        assert "service:101" in cbs and "service:110" in cbs and "service:102" not in cbs

    async def test_no_results_escapes_query(self, session, make_user, make_call, make_message, make_state,
                                            fake_getsms):
        await make_user(selected_country_id=1)
        fake_getsms.search_services.return_value = []
        _, msg = await self._search(session, make_call, make_message, make_state, "service", "<zzz>")
        assert "&lt;zzz&gt;" in msg.bot.edit_message_caption.await_args.kwargs["caption"]

    async def test_service_search_needs_country(self, session, make_user, make_call, make_state):
        await make_user()
        state = make_state()
        call = make_call("search:service")
        await activation.search_handler(call, session, state)
        assert alert_text(call) == t("error_select_country", "ru")
        assert await state.get_state() is None

    async def test_country_search(self, session, make_user, make_call, make_message, make_state, fake_getsms):
        await make_user()
        _, msg = await self._search(session, make_call, make_message, make_state, "country", "country4")
        cbs = callbacks(msg.bot.edit_message_caption.await_args.kwargs["reply_markup"])
        assert "country:4" in cbs and "country:40" in cbs and "country:5" not in cbs

    async def test_falls_back_to_new_message_if_banner_gone(self, session, make_user, make_call, make_message,
                                                            make_state, fake_getsms):
        await make_user()
        state = make_state()
        await activation.search_handler(make_call("search:country"), session, state)
        msg = make_message("country4")
        msg.bot.edit_message_caption = AsyncMock(side_effect=Exception("message to edit not found"))
        await activation.search_country_input(msg, session, state)
        msg.answer.assert_awaited_once()


# ─── Unsupported provider features, last_seen, status ────────────────────────

class TestSmallFixes:
    @pytest.mark.parametrize("data", ["na:prefix", "na:multi"])
    async def test_unsupported_features_say_so(self, session, make_user, make_call, data):
        await make_user(language="de")
        call = make_call(data)
        await activation.not_available(call, session)
        assert alert_text(call) == t("feature_unavailable", "de")

    def test_service_card_buttons_point_to_explanations(self):
        from bot.keyboards import service_card_keyboard
        cbs = callbacks(service_card_keyboard(1, 10.0, 5, "ru"))
        assert "na:prefix" in cbs and "na:multi" in cbs and "noop" not in cbs

    async def test_last_seen_throttled(self, session, make_user, reload_user):
        await make_user()
        old = utcnow() - timedelta(hours=1)
        await session.execute(User.__table__.update().values(last_seen=old))
        await session.commit()
        await touch_last_seen(session, TEST_USER_ID)
        first = (await reload_user()).last_seen
        assert first > old
        await touch_last_seen(session, TEST_USER_ID)        # within 5 min: no write
        assert (await reload_user()).last_seen == first

    async def test_middleware_updates_last_seen(self, session_factory, session, make_user, reload_user):
        from bot.middlewares.db import DbSessionMiddleware
        await make_user()
        await session.execute(User.__table__.update().values(last_seen=None))
        await session.commit()
        event = SimpleNamespace(from_user=SimpleNamespace(id=TEST_USER_ID))
        await DbSessionMiddleware()(AsyncMock(return_value=None), event, {})
        assert (await reload_user()).last_seen is not None

    @pytest.mark.parametrize("lang,expected", [("en", "Newbie"), ("de", "Neuling"), ("ru", "Новичок")])
    async def test_profile_status_translated(self, session, make_user, make_call, lang, expected):
        await make_user(language=lang)
        call = make_call("menu:profile")
        await profile.show_profile(call, session)
        assert expected in last_caption(call)
        if lang != "ru":
            assert "Новичок" not in last_caption(call)
