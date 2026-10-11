"""Runs before every handler: blocked users are stopped, and one user can't flood the bot."""

import time
from collections import defaultdict, deque
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from bot.db.queries import get_user
from bot.i18n import t

RATE_WINDOW = 3.0           # seconds
RATE_LIMIT = 8              # updates per window per user (a human taps far slower)
BAN_NOTICE_EVERY = 60.0     # seconds between "you are blocked" replies to the same user
MAX_TRACKED = 50_000


class GuardMiddleware(BaseMiddleware):
    """Must be registered after DbSessionMiddleware (needs data["session"])."""

    def __init__(self, limit: int = RATE_LIMIT, window: float = RATE_WINDOW):
        self.limit, self.window = limit, window
        self._hits: dict[int, deque] = defaultdict(deque)
        self._ban_noticed: dict[int, float] = {}

    def flooding(self, user_id: int, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        hits = self._hits[user_id]
        while hits and now - hits[0] > self.window:
            hits.popleft()
        if len(hits) >= self.limit:
            return True
        hits.append(now)
        if len(self._hits) > MAX_TRACKED:
            self._hits = defaultdict(deque, {uid: q for uid, q in self._hits.items() if q and now - q[-1] <= self.window})
        return False

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        from bot.handlers.admin import is_admin
        tg_user = getattr(event, "from_user", None)
        if tg_user is None or is_admin(tg_user.id):
            return await handler(event, data)

        session = data.get("session")
        user = await get_user(session, tg_user.id) if session is not None else None
        lang = user.language if user else "ru"

        if user is not None and user.is_banned:
            now = time.monotonic()
            if isinstance(event, CallbackQuery):
                await event.answer(t("user_banned", lang), show_alert=True)
            elif isinstance(event, Message) and now - self._ban_noticed.get(tg_user.id, -1e9) > BAN_NOTICE_EVERY:
                self._ban_noticed[tg_user.id] = now
                await event.answer(t("user_banned", lang))
            return None

        if self.flooding(tg_user.id):
            if isinstance(event, CallbackQuery):
                await event.answer(t("too_fast", lang))
            return None
        return await handler(event, data)
