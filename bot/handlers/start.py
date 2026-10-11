from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart, Command
from aiogram.types import CallbackQuery, LinkPreviewOptions, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot import countries, currency
from bot.config import BANNER_URL
from bot.db.queries import get_or_create_user, get_user, update_user
from bot.i18n import SUPPORTED_LANGS, operator_label, t
from bot.keyboards import close_keyboard, lang_keyboard, main_menu_keyboard, reply_menu_keyboard, tos_keyboard
from bot.utils.banner import get_banner, cache_banner_file_id

router = Router()

# Shown before a new user has picked a language, so it speaks all of them
LANGUAGE_PROMPT = "🌐 Выберите язык • Choose your language • Sprache wählen"

# Telegram caption limit = 1024 chars.
# TOS text is ~2500 chars, so we send banner + short caption,
# then TOS as a separate text message with the Accept button.
BANNER_CAPTION_SHORT = "👇"


def parse_referrer(args: str | None, user_id: int) -> int | None:
    """/start ref<id> -> referrer id (never the user themself)."""
    if not args or not args.startswith("ref") or not args[3:].isdigit():
        return None
    ref_id = int(args[3:])
    return ref_id if ref_id != user_id else None


@router.message(CommandStart())
async def cmd_start(message: Message, session: AsyncSession, command: CommandObject | None = None):
    uid = message.from_user.id
    extra = {}
    if await get_user(session, uid) is None:
        # Only a brand-new user can be attributed to a referrer, and only to an existing one
        ref_id = parse_referrer(command.args if command else None, uid)
        if ref_id and await get_user(session, ref_id):
            extra["referral_id"] = ref_id
    user = await get_or_create_user(
        session,
        user_id=uid,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
        **extra,
    )

    if not user.tos_accepted:
        # Language first, so the agreement is shown in the user's language
        await message.answer(LANGUAGE_PROMPT, reply_markup=lang_keyboard())
    else:
        await show_main_menu(message, session, user, with_reply_keyboard=True)


async def send_tos_screen(message: Message, session: AsyncSession, lang: str):
    """
    TOS is ~2500 chars, but animation captions are capped at 1024.
    With BANNER_URL: one text message (4096 limit), banner shown as a large preview above it.
    Without it: banner animation, then TOS as a separate message.
    """
    tos_text = f"<b>{t('tos_title', lang)}</b>\n\n{t('tos_text', lang)}"

    if BANNER_URL:
        await message.answer(
            text=tos_text,
            reply_markup=tos_keyboard(lang),
            parse_mode="HTML",
            link_preview_options=LinkPreviewOptions(
                url=BANNER_URL,
                prefer_large_media=True,
                show_above_text=True,
            ),
        )
        return

    banner = await get_banner(session)

    # Send banner animation with short caption
    msg = await message.answer_animation(
        animation=banner,
        caption=BANNER_CAPTION_SHORT,
    )

    # Cache file_id on first upload
    if msg.animation and not isinstance(banner, str):
        await cache_banner_file_id(session, msg.animation.file_id)

    # Send TOS as separate text message with the Accept button
    await message.answer(
        text=tos_text,
        reply_markup=tos_keyboard(lang),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "tos:accept")
async def tos_accepted(call: CallbackQuery, session: AsyncSession):
    await update_user(session, call.from_user.id, tos_accepted=True)
    user = await get_or_create_user(session, call.from_user.id)
    await call.answer("✅")
    try:
        await call.message.edit_reply_markup(reply_markup=None)    # keep the text, drop the button
    except TelegramBadRequest:
        pass
    await show_main_menu(call.message, session, user, with_reply_keyboard=True)


@router.callback_query(F.data.startswith("lang:"))
async def set_language(call: CallbackQuery, session: AsyncSession):
    lang = call.data.split(":", 1)[1]
    if lang not in SUPPORTED_LANGS:
        await call.answer()
        return
    await update_user(session, call.from_user.id, language=lang)
    user = await get_or_create_user(session, call.from_user.id)
    user.language = lang

    await call.answer("✅")
    await call.message.delete()             # the language picker
    if not user.tos_accepted:               # first start: agreement in the chosen language
        await send_tos_screen(call.message, session, lang)
        return
    await show_main_menu(call.message, session, user, with_reply_keyboard=True)
    await call.message.answer(t("lang_changed", lang), reply_markup=close_keyboard(lang))


@router.callback_query(F.data == "close")
async def close_message(call: CallbackQuery):
    try:
        await call.message.delete()
    except TelegramBadRequest:              # older than 48 h: just remove the button
        await call.message.edit_reply_markup(reply_markup=None)
    await call.answer()


async def show_main_menu(message: Message, session: AsyncSession, user, user_id: int = None,
                         with_reply_keyboard: bool = False):
    """Send banner + main menu caption as a fresh animation message."""
    lang = user.language or "ru"
    if with_reply_keyboard:
        # Reply keyboards can't share a message with inline buttons, so they get their own line
        await message.answer(t("reply_menu_hint", lang), reply_markup=reply_menu_keyboard(lang))
    country = _country_label(user, lang)
    operator = operator_label(user.selected_operator, lang)

    text = t("main_menu_text", lang,
             balance=currency.fmt(user.balance, currency.user_currency(user)),
             country=country,
             operator=operator)

    banner = await get_banner(session)
    msg = await message.answer_animation(
        animation=banner,
        caption=text,
        reply_markup=main_menu_keyboard(lang),
        parse_mode="HTML",
    )
    if msg.animation and not isinstance(banner, str):
        await cache_banner_file_id(session, msg.animation.file_id)


async def show_main_menu_edit(call: CallbackQuery, session: AsyncSession, user):
    """Edit existing animation caption in-place (used for back navigation)."""
    lang = user.language or "ru"
    country = _country_label(user, lang)
    operator = operator_label(user.selected_operator, lang)

    text = t("main_menu_text", lang,
             balance=currency.fmt(user.balance, currency.user_currency(user)),
             country=country,
             operator=operator)

    try:
        await call.message.edit_caption(
            caption=text,
            reply_markup=main_menu_keyboard(lang),
            parse_mode="HTML",
        )
    except Exception:
        # If message is not an animation (e.g. text), send fresh
        banner = await get_banner(session)
        msg = await call.message.answer_animation(
            animation=banner,
            caption=text,
            reply_markup=main_menu_keyboard(lang),
            parse_mode="HTML",
        )
        if msg.animation and not isinstance(banner, str):
            await cache_banner_file_id(session, msg.animation.file_id)


def _country_label(user, lang: str) -> str:
    if user.selected_country_name:
        return countries.localize(user.selected_country_name, lang)
    return t("country_not_selected", lang)


@router.message(Command("fill"))
async def cmd_fill(message: Message, session: AsyncSession):
    """Direct top-up screen via /fill command."""
    user = await get_or_create_user(
        session,
        user_id=message.from_user.id,
        username=message.from_user.username,
        full_name=message.from_user.full_name,
    )
    lang = user.language or "ru"
    from bot.handlers.topup import topup_screen
    text, kb = await topup_screen(session, lang)
    await message.answer(text=text, reply_markup=kb)
