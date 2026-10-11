"""Banner file_id caching and the DB-session middleware."""

from unittest.mock import AsyncMock

from aiogram.types import FSInputFile

import bot.utils.banner as banner
from bot.db.queries import get_banner_file_id, set_banner_file_id


class TestBanner:
    async def test_no_cache_returns_local_file(self, session):
        result = await banner.get_banner(session)
        assert isinstance(result, FSInputFile)

    async def test_db_cache_is_used_and_promoted_to_memory(self, session):
        await set_banner_file_id(session, "DB_ID")
        assert await banner.get_banner(session) == "DB_ID"
        assert banner._banner_file_id == "DB_ID"

    async def test_memory_cache_wins_over_db(self, session):
        await set_banner_file_id(session, "DB_ID")
        banner._banner_file_id = "MEM_ID"
        assert await banner.get_banner(session) == "MEM_ID"

    async def test_cache_banner_file_id_persists(self, session):
        await banner.cache_banner_file_id(session, "NEW")
        assert banner._banner_file_id == "NEW"
        assert await get_banner_file_id(session) == "NEW"


class TestDbSessionMiddleware:
    async def test_injects_session_and_returns_handler_result(self, session_factory):
        from bot.middlewares.db import DbSessionMiddleware
        handler = AsyncMock(return_value="handled")
        data = {}
        result = await DbSessionMiddleware()(handler, object(), data)
        assert result == "handled"
        handler.assert_awaited_once()
        assert "session" in data
