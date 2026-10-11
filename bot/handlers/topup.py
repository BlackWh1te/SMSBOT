"""
Balance top-up: menu of active payment methods -> amount input -> invoice.

Only methods the owner turned on in /admin (with all keys filled in) are shown.
"""

import html
import math
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import currency, payments
from bot.api.nowpayments import FINAL_FAILED_STATUSES, STATUS_PARTIALLY_PAID
from bot.db.queries import get_or_create_user, get_payment
from bot.i18n import t
from bot.keyboards import back_keyboard, topup_keyboard
from bot.db.models import Payment
from bot.payments import invoice_flow, nowpayments_flow as np_flow

router = Router()

MAX_TOPUP_RUB = 100_000


class TopUp(StatesGroup):
    amount = State()    # data: code
    coin = State()      # NOWPayments: data: code, amount


async def topup_screen(session: AsyncSession, lang: str) -> tuple[str, InlineKeyboardMarkup]:
    active = await payments.active_methods(session)
    text = t("topup_text", lang) if active else t("topup_no_methods", lang)
    kb = topup_keyboard(lang, [(m.code, m.display_title(lang)) for m in active])
    return text, kb


async def _render(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Menus live in animation captions, but /fill opens the top-up as a text message."""
    try:
        await call.message.edit_caption(caption=text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "message is not modified" in str(e):
            return
        await call.message.edit_text(text=text, reply_markup=kb)


@router.callback_query(F.data == "menu:topup")
async def show_topup(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    await state.clear()
    user = await get_or_create_user(session, call.from_user.id)
    await _render(call, *await topup_screen(session, user.language))
    await call.answer()


@router.callback_query(F.data.startswith("topup:m:"))
async def choose_method(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    code = call.data.split(":", 2)[2]
    method = await payments.get_state(session, code) if code in payments.METHODS else None
    if method is None or not method.is_active:
        await call.answer(t("topup_method_unavailable", lang), show_alert=True)
        return

    cur = currency.user_currency(user)
    text = t("topup_enter_amount", lang, method=method.display_title(lang), currency=currency.label(cur))
    if method.min_amount:
        text += "\n" + t("topup_min_amount", lang, min=currency.fmt(method.min_amount, cur, up=True))
    await state.set_state(TopUp.amount)
    await state.update_data(code=code)
    await _render(call, text, back_keyboard(lang, back_cb="menu:topup"))
    await call.answer()


def parse_amount(text: str) -> float | None:
    try:
        value = float(text.strip().replace(" ", "").replace(",", "."))
    except (ValueError, AttributeError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return round(value, 2)


@router.message(TopUp.amount)
async def amount_input(message: Message, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, message.from_user.id)
    lang = user.language
    code = (await state.get_data()).get("code")

    method = await payments.get_state(session, code) if code in payments.METHODS else None
    if method is None or not method.is_active:
        await state.clear()
        await message.answer(t("topup_method_unavailable", lang),
                             reply_markup=back_keyboard(lang, back_cb="menu:topup"))
        return

    cur = currency.user_currency(user)
    shown = parse_amount(message.text or "")
    if shown is None:
        await message.answer(t("topup_bad_amount", lang))
        return
    amount = currency.to_rub(shown, cur)            # balances are kept in RUB
    if method.min_amount and amount < method.min_amount:
        await message.answer(t("topup_below_min", lang, min=currency.fmt(method.min_amount, cur, up=True)))
        return
    if amount > MAX_TOPUP_RUB:
        await message.answer(t("topup_above_max", lang, max=currency.fmt(MAX_TOPUP_RUB, cur)))
        return

    if method.code in invoice_flow.METHODS:
        await _send_invoice(message, session, state, user, method, amount, shown, cur)
        return

    if method.code == np_flow.METHOD:
        await state.set_state(TopUp.coin)
        await state.update_data(code=method.code, amount=amount)
        builder = InlineKeyboardBuilder()
        for coin, label in await np_flow.available_coins(method):
            builder.button(text=label, callback_data=f"np:coin:{coin}")
        builder.adjust(2)
        builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:topup"))
        await message.answer(t("np_choose_coin", lang, amount=currency.fmt_amount(shown, cur)),
                             reply_markup=builder.as_markup())
        return

    await state.clear()
    # Other providers (CryptoBot, SBP, ...) plug in here.
    await message.answer(
        t("topup_not_ready", lang, method=method.display_title(lang), amount=currency.fmt_amount(shown, cur)),
        reply_markup=back_keyboard(lang, back_cb="menu:topup"),
    )


# ─── CryptoBot / xRocket ──────────────────────────────────────────────────────

async def _send_invoice(message: Message, session: AsyncSession, state: FSMContext, user, method,
                        amount_rub: float, shown: float, cur: str):
    lang = user.language
    if await invoice_flow.open_invoices(session, user.id) >= invoice_flow.MAX_OPEN_INVOICES:
        await state.clear()
        await message.answer(t("inv_too_many", lang), reply_markup=back_keyboard(lang, back_cb="menu:topup"))
        return
    pay, url = await invoice_flow.create_invoice(session, user.id, method, amount_rub)
    if pay is None:                     # stay in the amount step so the user can try another amount
        await message.answer(t("topup_create_failed", lang))
        return
    await state.clear()
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_pay", lang), url=url)
    builder.button(text=t("btn_check_payment", lang), callback_data=f"inv:chk:{pay.id}")
    builder.button(text=t("btn_menu", lang), callback_data="menu:main")
    builder.adjust(1)
    await message.answer(
        t("inv_invoice", lang, amount=currency.fmt_amount(shown, cur), method=html.escape(method.display_title(lang)),
          minutes=invoice_flow.INVOICE_TTL // 60),
        reply_markup=builder.as_markup(), parse_mode="HTML")


@router.callback_query(F.data.startswith("inv:chk:"))
async def inv_check(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    row_id = call.data.split(":", 2)[2]
    pay = await session.get(Payment, int(row_id)) if row_id.isdigit() else None
    if pay is None or pay.user_id != user.id or invoice_flow.split_id(pay.payment_id)[0] not in invoice_flow.METHODS:
        await call.answer(t("topup_method_unavailable", lang), show_alert=True)
        return
    if pay.status == invoice_flow.STATUS_ACTIVE:
        await invoice_flow.refresh(session, [pay])
        pay = await session.get(Payment, pay.id, populate_existing=True)
    if pay.status == np_flow.STATUS_FINISHED:
        text = t("np_paid", lang, amount=currency.fmt(pay.amount_rub, currency.user_currency(user)))
    elif pay.status == invoice_flow.STATUS_EXPIRED:
        text = t("np_status_failed", lang)
    else:
        text = t("np_status_waiting", lang)
    await call.answer(text, show_alert=True)


# ─── NOWPayments ──────────────────────────────────────────────────────────────

def _format_expiry(iso: str | None) -> str:
    if not iso:
        return "—"
    return iso[:16].replace("T", " ") + " UTC"


@router.callback_query(TopUp.coin, F.data.startswith("np:coin:"))
async def np_choose_coin(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    coin = call.data.split(":", 2)[2]
    data = await state.get_data()
    method = await payments.get_state(session, np_flow.METHOD)
    if not method.is_active or data.get("code") != np_flow.METHOD or not data.get("amount") \
            or not re.fullmatch(r"[a-z0-9]{2,20}", coin):
        await state.clear()
        await call.answer(t("topup_method_unavailable", lang), show_alert=True)
        return

    pay, resp = await np_flow.create_invoice(session, call.from_user.id, method, float(data["amount"]), coin)
    if pay is None:
        await call.answer(t("topup_create_failed", lang), show_alert=True)
        return     # stay on the coin list so the user can pick another coin

    await state.clear()
    text = t("np_invoice", lang,
             pay_amount=resp.get("pay_amount"),
             coin=html.escape(np_flow.coin_label(coin)),
             address=html.escape(str(resp["pay_address"])),
             amount=currency.fmt(pay.amount_rub, currency.user_currency(user)),
             expires=_format_expiry(resp.get("expiration_estimate_date")))
    if resp.get("payin_extra_id"):
        text += "\n\n" + t("np_memo", lang, memo=html.escape(str(resp["payin_extra_id"])))
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_check_payment", lang), callback_data=f"np:chk:{pay.payment_id}")
    builder.button(text=t("btn_menu", lang), callback_data="menu:main")
    builder.adjust(1)
    await call.message.edit_text(text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("np:chk:"))
async def np_check(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    payment_id = call.data.split(":", 2)[2]
    pay = await get_payment(session, payment_id)
    if pay is None or pay.user_id != call.from_user.id:      # only the owner can check a payment
        await call.answer(t("topup_method_unavailable", lang), show_alert=True)
        return

    status, _ = await np_flow.refresh_payment(session, pay)
    if status == np_flow.STATUS_FINISHED:
        text = t("np_paid", lang, amount=currency.fmt(pay.amount_rub, currency.user_currency(user)))
    elif status == STATUS_PARTIALLY_PAID:
        text = t("np_status_partial", lang, payment_id=pay.payment_id)
    elif status in FINAL_FAILED_STATUSES:
        text = t("np_status_failed", lang)
    elif status in ("confirming", "confirmed", "sending"):
        text = t("np_status_confirming", lang)
    else:
        text = t("np_status_waiting", lang)
    await call.answer(text, show_alert=True)
