import asyncio
from loguru import logger
from aiogram import Bot, Dispatcher
from aiogram.enums import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.types import BotCommand, ErrorEvent

from bot.config import BOT_TOKEN
from bot.db import AsyncSessionLocal, init_db
from bot.db.queries import get_user
from bot.i18n import t
from bot.middlewares.db import DbSessionMiddleware
from bot.middlewares.guard import GuardMiddleware
from bot.middlewares.requests import CaptionFallbackMiddleware
from bot import currency, emoji, pricing
from bot.handlers import (
    start, activation, profile, admin, admin_payments, admin_emoji, admin_pricing, rent, email, topup, reply_menu,
)
from bot.payments import invoice_flow, nowpayments_flow as np_flow

async def set_commands(bot: Bot):
    """The "Menu" button next to the input field: /start, /buy, /fill in the user's language."""
    for lang in ("ru", "en", "de"):
        commands = [BotCommand(command=c, description=t(f"cmd_{c}", lang)) for c in ("start", "buy", "fill")]
        try:
            await bot.set_my_commands(commands, language_code=None if lang == "ru" else lang)
        except Exception as e:                  # never block the start over the command menu
            logger.warning(f"set_my_commands({lang}) failed: {e!r}")


async def main():
    logger.info("Starting MARTOV SMS Bot...")

    # Init DB
    await init_db()
    logger.info("Database initialized")

    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    bot.session.middleware(CaptionFallbackMiddleware())     # outermost: wraps the emoji middleware
    bot.session.middleware(emoji.LiveEmojiMiddleware())
    async with AsyncSessionLocal() as s:
        if seeded := await emoji.seed_defaults(s):
            logger.info(f"Bundled live emoji stored: {seeded}")
        await emoji.load(s)
        await pricing.load(s)
        await currency.load(s)
    logger.info(f"Live emoji loaded: {len(emoji.mapping())}; number markup ×{pricing.markup():g}")

    dp = Dispatcher()

    # Middleware
    dp.message.middleware(DbSessionMiddleware())
    dp.callback_query.middleware(DbSessionMiddleware())
    guard = GuardMiddleware()                   # after the session: needs it for the ban check
    dp.message.middleware(guard)
    dp.callback_query.middleware(guard)

    # Routers
    dp.include_router(reply_menu.router)      # first: a reply button beats any pending text input
    dp.include_router(start.router)
    dp.include_router(activation.router)
    dp.include_router(topup.router)
    dp.include_router(profile.router)
    dp.include_router(rent.router)
    dp.include_router(email.router)
    # admin_payments first: while the owner is typing a key, its state handler must win
    dp.include_router(admin_payments.router)
    dp.include_router(admin_emoji.router)
    dp.include_router(admin_pricing.router)
    dp.include_router(admin.router)

    async def notify_topup(user_id: int, pay):
        async with AsyncSessionLocal() as s:
            user = await get_user(s, user_id)
        lang = user.language if user else "ru"
        amount = currency.fmt(pay.amount_rub, currency.user_currency(user))
        await bot.send_message(user_id, t("np_paid", lang, amount=amount))

    async def notify_order(user_id: int, text: str, kb):
        await bot.send_message(user_id, text, reply_markup=kb, parse_mode="HTML")

    @dp.errors()
    async def on_error(event: ErrorEvent):
        logger.opt(exception=event.exception).error(f"Unhandled error: {event.exception!r}")
        call = getattr(event.update, "callback_query", None)
        if call is not None:
            try:
                await call.answer("⚠️ Error, please try again", show_alert=True)
            except Exception:
                pass
        return True

    # Keep references so the tasks are not garbage-collected; cancel them on shutdown.
    background = [
        asyncio.create_task(np_flow.watcher(AsyncSessionLocal, notify_topup)),
        asyncio.create_task(invoice_flow.watcher(AsyncSessionLocal, notify_topup)),
        asyncio.create_task(activation.order_sweeper(AsyncSessionLocal, notify_order)),
        asyncio.create_task(currency.updater(AsyncSessionLocal)),
    ]

    await set_commands(bot)
    logger.info("Bot started. Polling...")
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        for task in background:
            task.cancel()


if __name__ == "__main__":
    asyncio.run(main())
