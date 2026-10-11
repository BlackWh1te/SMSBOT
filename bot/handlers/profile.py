import html
import math

from aiogram import Router, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot import countries, currency
from bot.db.queries import count_referrals, get_or_create_user, get_user, update_user
from bot.db.queries import transfer_balance as do_transfer
from bot.i18n import operator_label, t
from bot.keyboards import back_keyboard, currency_keyboard, lang_keyboard, profile_keyboard
from bot.handlers.start import show_main_menu_edit

router = Router()

DEFAULT_STATUS = "Новичок"      # stored default (models.User.status); shown translated
MIN_TRANSFER = 1                # in the sender's display currency
# Stored rank (any language) -> i18n key. Ranks not listed here are shown as stored.
RANK_KEYS = {"Новичок": "rank_newbie", "Newbie": "rank_newbie", "Neuling": "rank_newbie"}


def rank_label(status: str | None, lang: str) -> str:
    key = RANK_KEYS.get((status or DEFAULT_STATUS).strip())
    return t(key, lang) if key else status


class TransferFlow(StatesGroup):
    input = State()             # waiting for "<recipient_id> <amount>"


@router.callback_query(F.data == "menu:profile")
async def show_profile(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    await _render_profile(call, user)
    await call.answer()


def profile_screen(user) -> tuple[str, InlineKeyboardMarkup]:
    lang = user.language

    activation_rate = 0
    if user.total_bought > 0:
        activation_rate = round(user.total_activated / user.total_bought * 100, 1)

    country = countries.localize(user.selected_country_name, lang) or t("country_not_selected", lang)
    operator = operator_label(user.selected_operator, lang)

    text = t("profile_text", lang,
             country=country,
             operator=operator,
             user_id=user.id,
             status=html.escape(rank_label(user.status, lang)),
             balance=currency.fmt(user.balance, currency.user_currency(user)),
             total_bought=user.total_bought,
             activated=user.total_activated,
             total_rented=user.total_rented,
             rented_activated=user.total_rented_activated,
             activation_rate=activation_rate)
    return text, profile_keyboard(lang)


async def _render_profile(call: CallbackQuery, user):
    text, kb = profile_screen(user)
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "profile:currency")
async def choose_currency(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    text = t("choose_currency", user.language, currency=currency.label(currency.user_currency(user)))
    await call.message.edit_caption(caption=text, reply_markup=currency_keyboard(user.language))
    await call.answer()


@router.callback_query(F.data.startswith("cur:"))
async def set_currency(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    cur = call.data.split(":", 1)[1]
    if cur not in currency.CURRENCIES:
        await call.answer(t("error_generic", user.language), show_alert=True)
        return
    await update_user(session, user.id, currency=cur)
    user.currency = cur
    await _render_profile(call, user)
    await call.answer(t("currency_set", user.language, currency=currency.label(cur)))


@router.callback_query(F.data == "profile:language")
async def change_language(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    await call.message.edit_caption(
        caption=t("choose_language", user.language),
        reply_markup=lang_keyboard(),
    )
    await call.answer()


@router.callback_query(F.data == "profile:api")
async def show_api_key(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    text = t("api_info", lang, api_key=user.api_key or "\u2014")
    kb = back_keyboard(lang, back_cb="menu:profile")
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── Balance transfer between users ───────────────────────────────────────────

@router.callback_query(F.data == "profile:transfer")
async def transfer_balance(call: CallbackQuery, session: AsyncSession, state: FSMContext | None = None):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    if state is not None:
        await state.set_state(TransferFlow.input)
    kb = back_keyboard(lang, back_cb="menu:profile")
    text = t("transfer_text", lang, currency=currency.label(currency.user_currency(user)))
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


def _menu_only(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_menu", lang), callback_data="menu:main")]])


def parse_transfer(text: str) -> tuple[int, float] | None:
    parts = (text or "").split()
    if len(parts) != 2 or not parts[0].isdigit():
        return None
    try:
        amount = round(float(parts[1].replace(",", ".")), 2)
    except ValueError:
        return None
    if not math.isfinite(amount) or amount < MIN_TRANSFER:
        return None
    return int(parts[0]), amount


@router.message(TransferFlow.input)
async def transfer_input(message: Message, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, message.from_user.id)
    lang = user.language
    cur = currency.user_currency(user)
    parsed = parse_transfer(message.text)
    if parsed is None:
        await message.answer(t("transfer_invalid", lang, min=currency.fmt_amount(MIN_TRANSFER, cur)),
                             parse_mode="HTML")
        return
    to_id, shown = parsed
    amount = currency.to_rub(shown, cur)
    if to_id == user.id:
        await message.answer(t("transfer_self", lang))
        return
    if await get_user(session, to_id) is None:
        await message.answer(t("transfer_no_user", lang))
        return
    if user.balance < amount:
        await state.clear()
        await message.answer(t("insufficient_balance", lang), reply_markup=_menu_only(lang))
        return

    await state.set_state(None)
    await state.update_data(transfer_to=to_id, transfer_amount=amount)
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=t("btn_confirm", lang), callback_data="transfer:ok"),
        InlineKeyboardButton(text=t("btn_cancel", lang), callback_data="transfer:no"),
    ]])
    await message.answer(t("transfer_confirm", lang, amount=currency.fmt_amount(shown, cur), to=to_id), reply_markup=kb,
                         parse_mode="HTML")


@router.callback_query(F.data == "transfer:ok")
async def transfer_confirm(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    data = await state.get_data()
    await state.clear()
    to_id, amount = data.get("transfer_to"), data.get("transfer_amount")
    if not to_id or not amount:
        await call.answer(t("error_generic", lang), show_alert=True)
        return
    if not await do_transfer(session, user.id, to_id, amount):     # atomic; re-checks the balance
        await call.message.edit_text(t("insufficient_balance", lang), reply_markup=_menu_only(lang))
        await call.answer()
        return
    await call.message.edit_text(t("transfer_done", lang, amount=currency.fmt(amount, currency.user_currency(user)),
                                   to=to_id),
                                 reply_markup=_menu_only(lang), parse_mode="HTML")
    await call.answer("✅")
    recipient = await get_user(session, to_id)
    try:
        await call.bot.send_message(to_id, t("transfer_received", recipient.language if recipient else "ru",
                                             amount=currency.fmt(amount, currency.user_currency(recipient)),
                                             sender=user.id), parse_mode="HTML")
    except Exception as e:              # recipient blocked the bot - the money is still delivered
        logger.info(f"Transfer notice to {to_id} failed: {e!r}")


@router.callback_query(F.data == "transfer:no")
async def transfer_cancel(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    await state.clear()
    await call.message.edit_text(t("btn_cancel", user.language), reply_markup=_menu_only(user.language))
    await call.answer()


@router.callback_query(F.data == "profile:cashback")
async def show_cashback(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    text = t("cashback_text", lang, cashback=currency.fmt(user.cashback, currency.user_currency(user)))
    kb = back_keyboard(lang, back_cb="menu:profile")
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "profile:referrals")
async def show_referrals(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    from bot.config import BOT_USERNAME
    ref_link = f"https://t.me/{BOT_USERNAME}?start=ref{user.id}"
    invited = await count_referrals(session, user.id)
    text = t("referrals_text", lang, ref_link=ref_link) + "\n\n" + t("referrals_count", lang, count=invited)
    kb = back_keyboard(lang, back_cb="menu:profile")
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "profile:info")
async def show_info(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    text = t("info_text", lang)
    kb = back_keyboard(lang, back_cb="menu:profile")
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "profile:disable_ads")
async def disable_ads(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    text = t("ads_disabled_text", lang)
    kb = back_keyboard(lang, back_cb="menu:profile")
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── Top-up ───────────────────────────────────────────────────────────────────
# The top-up menu lives in bot/handlers/topup.py. This handler only serves
# "Crypto" buttons on messages sent before payment methods became configurable.

@router.callback_query(F.data == "topup:crypto")
async def topup_crypto(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    text = t("topup_crypto_text", lang)
    kb = back_keyboard(lang, back_cb="menu:topup")
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── Navigation ───────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:main")
async def back_to_main(call: CallbackQuery, session: AsyncSession, state: FSMContext | None = None):
    if state is not None:
        await state.clear()     # leave any pending input (e.g. top-up amount)
    user = await get_or_create_user(session, call.from_user.id)
    await show_main_menu_edit(call, session, user)
    await call.answer()


@router.callback_query(F.data == "menu:anon")
async def anon_numbers(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    text = t("anon_text", lang)
    kb = back_keyboard(lang)
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data == "noop")
async def noop(call: CallbackQuery):
    await call.answer()
