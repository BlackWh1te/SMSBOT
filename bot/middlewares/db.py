from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject
from loguru import logger

from bot.db import AsyncSessionLocal
from bot.db.queries import touch_last_seen


class DbSessionMiddleware(BaseMiddleware):
    """Gives every handler its own DB session and records when the user was last active."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        async with AsyncSessionLocal() as session:
            data["session"] = session
            result = await handler(event, data)
            user = getattr(event, "from_user", None)
            if user is not None:
                try:
                    await touch_last_seen(session, user.id)
                except Exception as e:      # never break the user's action over a timestamp
                    logger.debug(f"last_seen update failed for {user.id}: {e!r}")
            return result
