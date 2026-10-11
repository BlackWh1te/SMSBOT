"""Purchase limits: one purchase at a time per user, a rate limit, and a cap on open numbers."""

import asyncio
import time
from collections import defaultdict, deque

PURCHASE_WINDOW = 60.0          # seconds
PURCHASES_PER_WINDOW = 6        # numbers / rentals / e-mails per user per window
MAX_ACTIVE_ORDERS = 10          # numbers still waiting for an SMS

_locks: dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
_purchases: dict[int, deque] = defaultdict(deque)


def purchase_lock(user_id: int) -> asyncio.Lock:
    """Held for the whole purchase, so a double tap can't place two orders in parallel."""
    return _locks[user_id]


def purchase_allowed(user_id: int, now: float | None = None) -> bool:
    """Counts the attempt when allowed."""
    now = time.monotonic() if now is None else now
    recent = _purchases[user_id]
    while recent and now - recent[0] > PURCHASE_WINDOW:
        recent.popleft()
    if len(recent) >= PURCHASES_PER_WINDOW:
        return False
    recent.append(now)
    return True


async def refuse_purchase(call, session) -> bool:
    """True (and the user is told why) when this purchase must not start."""
    from bot.db.queries import get_user
    from bot.i18n import t
    uid = call.from_user.id
    user = await get_user(session, uid)
    lang = user.language if user else "ru"
    if purchase_lock(uid).locked():
        await call.answer(t("too_fast", lang))
        return True
    if not purchase_allowed(uid):
        await call.answer(t("order_rate_limited", lang), show_alert=True)
        return True
    return False


def reset() -> None:
    _locks.clear()
    _purchases.clear()
