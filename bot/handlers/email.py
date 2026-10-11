"""
E-mail activations, same flow as the original bot:

  menu -> «Создать почту под сервис» -> user types the site (telegram.com)
  -> domains with price and stock, 10 per page -> buy
  -> card: «🔄 Проверить» / «🔄 Перезаказать (платно)» / «❌ Отменить»
  menu -> «📘 История купленных почт»

Money: reserve the domain price -> buy -> settle the difference; refunds only once.
"""

import html
import re

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot import antiabuse, currency
from bot.api.getsms import getsms
from bot.db.queries import (
    add_balance, create_email_order, deduct_balance, get_email_order, get_or_create_user,
    get_user_email_orders, settle_charge, transition_email_order, update_email_order,
)
from bot.i18n import status_label, t

router = Router()

DOMAINS_PER_PAGE = 10
CAPTION_LIMIT = 1024
SITE_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+$")


class EmailFlow(StatesGroup):
    site = State()          # waiting for the site name


def normalize_site(text: str) -> str | None:
    site = (text or "").strip().lower()
    site = re.sub(r"^https?://", "", site).split("/")[0].removeprefix("www.")
    return site if len(site) <= 64 and SITE_RE.match(site) else None


async def _render(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    """Email screens appear both on the banner (caption) and as plain text messages."""
    text = text[:CAPTION_LIMIT]
    try:
        await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        await call.message.edit_text(text=text, reply_markup=kb, parse_mode="HTML")


def _back(lang: str, cb: str = "menu:email") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=t("btn_back", lang), callback_data=cb)


# ─── Menu ─────────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:email")
async def email_menu(call: CallbackQuery, session: AsyncSession, state: FSMContext | None = None):
    if state is not None:
        await state.clear()
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_email_create", lang), callback_data="email:new")
    builder.button(text=t("btn_email_history", lang), callback_data="email:history")
    builder.adjust(1)
    builder.row(_back(lang, "menu:main"))
    await _render(call, t("email_text", lang), builder.as_markup())
    await call.answer()


@router.callback_query(F.data == "email:new")
async def email_new(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    await state.set_state(EmailFlow.site)
    await _render(call, t("email_site_prompt", lang), InlineKeyboardMarkup(inline_keyboard=[[_back(lang)]]))
    await call.answer()


@router.message(EmailFlow.site)
async def email_site_input(message: Message, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, message.from_user.id)
    lang = user.language
    site = normalize_site(message.text or "")
    if not site:
        await message.answer(t("email_site_invalid", lang), parse_mode="HTML")
        return

    domains = []
    for d in await getsms.get_email_domains(site):
        try:
            if d.get("domain") and float(d["price"]) > 0 and int(d.get("count") or 0) > 0:
                domains.append({"domain": str(d["domain"]), "price": float(d["price"]), "count": int(d["count"])})
        except (KeyError, TypeError, ValueError):
            continue
    if not domains:
        await message.answer(t("email_no_domains", lang, site=html.escape(site)), parse_mode="HTML")
        return

    await state.set_state(None)                 # stop capturing text, keep the domain list
    await state.update_data(site=site, domains=domains)
    text, kb = _domains_page(lang, site, domains, page=1, cur=currency.user_currency(user))
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


def _domains_page(lang: str, site: str, domains: list[dict], page: int, cur: str):
    pages = max(1, (len(domains) + DOMAINS_PER_PAGE - 1) // DOMAINS_PER_PAGE)
    page = max(1, min(page, pages))
    start = (page - 1) * DOMAINS_PER_PAGE
    builder = InlineKeyboardBuilder()
    for idx, d in enumerate(domains[start:start + DOMAINS_PER_PAGE], start=start):
        builder.button(text=t("email_domain_btn", lang, domain=d["domain"], price=currency.fmt(d["price"], cur, up=True), count=d["count"]),
                       callback_data=f"email:buy:{idx}")
    builder.adjust(1)
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"email:pg:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{pages}", callback_data="noop"))
    if page < pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"email:pg:{page + 1}"))
    builder.row(*nav)
    builder.row(_back(lang))
    text = t("email_domains_title", lang, site=html.escape(site), page=page, pages=pages, total=len(domains))
    return text, builder.as_markup()


@router.callback_query(F.data.startswith("email:pg:"))
async def email_domains_page(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    data = await state.get_data()
    if not data.get("domains"):
        await call.answer(t("email_session_expired", lang), show_alert=True)
        return
    text, kb = _domains_page(lang, data["site"], data["domains"], _int_part(call.data, 2, 1),
                             cur=currency.user_currency(user))
    await _render(call, text, kb)
    await call.answer()


# ─── Buy ──────────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("email:buy:"))
async def email_buy(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    if await antiabuse.refuse_purchase(call, session):
        return
    async with antiabuse.purchase_lock(call.from_user.id):
        await _email_buy(call, session, state)


async def _email_buy(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    uid = call.from_user.id
    user = await get_or_create_user(session, uid)
    lang = user.language
    data = await state.get_data()
    domains = data.get("domains") or []
    idx = _int_part(call.data, 2, -1)
    if not (0 <= idx < len(domains)):
        await call.answer(t("email_session_expired", lang), show_alert=True)
        return
    site, choice = data["site"], domains[idx]
    price = choice["price"]

    if not await deduct_balance(session, uid, price):
        await call.answer(t("insufficient_balance", lang), show_alert=True)
        return

    result = await getsms.create_email(site, choice["domain"])
    order = result.get("order") or {}
    if not result.get("ok") or not order.get("id") or not order.get("email"):
        await add_balance(session, uid, price)
        await call.answer(t("error_generic", lang), show_alert=True)
        return

    actual = order.get("price", price)
    if not await settle_charge(session, uid, price, actual):
        # The listed price changed seconds before the purchase and the user can't
        # cover the difference. An email can't be cancelled for 2 minutes, so it
        # is delivered at the reserved price and the (small) difference is logged.
        logger.warning(f"Email {order['id']}: price rose {price} -> {actual}, user {uid} charged {price}")
        actual = price

    await create_email_order(session, user_id=uid, email_id=str(order["id"]), email=str(order["email"]),
                             site=site, domain=choice["domain"], price=float(actual), status="WAIT")
    await state.clear()
    eo = await get_email_order(session, str(order["id"]))
    await _render(call, _card_text(lang, eo, currency.user_currency(user)), _card_kb(lang, eo))
    await call.answer("✅")


# ─── Card / check / reorder / cancel ──────────────────────────────────────────

def _card_text(lang: str, eo, cur: str) -> str:
    return t("email_card", lang, title=t("email_created", lang), email=html.escape(eo.email),
             site=html.escape(eo.site), price=currency.fmt(eo.price, cur), status=status_label(eo.status, lang))


def _with_code(lang: str, card: str, code: str, message: str = "") -> str:
    """Card + code + as much of the e-mail text as fits in a caption."""
    head = f"{card}\n\n" + t("email_code", lang, code=html.escape(code), message="")
    room = CAPTION_LIMIT - len(head)
    return head + html.escape(message)[:max(room, 0)]


def _card_kb(lang: str, eo) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if eo.status != "CANCELLED":
        builder.button(text=t("email_check_btn", lang), callback_data=f"email:chk:{eo.email_id}")
        builder.button(text=t("email_reorder_btn", lang), callback_data=f"email:re:{eo.email_id}")
    if eo.status == "WAIT":
        builder.button(text=t("email_cancel_btn", lang), callback_data=f"email:cancel:{eo.email_id}")
    builder.adjust(1)
    builder.row(_back(lang))
    return builder.as_markup()


async def _own_email(call: CallbackQuery, session: AsyncSession, lang: str):
    email_id = call.data.split(":", 2)[2]
    eo = await get_email_order(session, email_id)
    if eo is None or eo.user_id != call.from_user.id:
        await call.answer(t("order_not_found", lang), show_alert=True)
        return None
    return eo


@router.callback_query(F.data.startswith("email:open:"))
async def email_open(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    eo = await _own_email(call, session, lang)
    if eo:
        card = _card_text(lang, eo, currency.user_currency(user))
        await _render(call, _with_code(lang, card, eo.code) if eo.code else card, _card_kb(lang, eo))
        await call.answer()


@router.callback_query(F.data.startswith("email:chk:"))
async def check_email(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    eo = await _own_email(call, session, lang)
    if not eo:
        return

    result = await getsms.check_email(eo.email_id)
    if not result.get("ok"):
        await call.answer(t("error_generic", lang), show_alert=True)
        return
    found = result.get("result") or {}
    code, msg = found.get("code"), found.get("message")
    if not code and not msg:
        await call.answer(t("email_no_messages", lang), show_alert=True)
        return

    if eo.status in ("WAIT", "RECEIVED"):
        await update_email_order(session, eo.email_id, status="RECEIVED", code=str(code)[:64] if code else None)
        eo = await get_email_order(session, eo.email_id)
    text = _with_code(lang, _card_text(lang, eo, currency.user_currency(user)), str(code or "—"), str(msg or ""))
    await _render(call, text, _card_kb(lang, eo))
    await call.answer()


@router.callback_query(F.data.startswith("email:re:"))
async def reorder_email(call: CallbackQuery, session: AsyncSession):
    uid = call.from_user.id
    user = await get_or_create_user(session, uid)
    lang = user.language
    eo = await _own_email(call, session, lang)
    if not eo:
        return
    if eo.status == "CANCELLED":
        await call.answer(t("order_already_closed", lang), show_alert=True)
        return

    price = eo.price
    if not await deduct_balance(session, uid, price):
        await call.answer(t("insufficient_balance", lang), show_alert=True)
        return
    result = await getsms.reorder_email(eo.email_id)
    if not result.get("ok"):
        await add_balance(session, uid, price)
        await call.answer(t("error_generic", lang), show_alert=True)
        return
    charged = result.get("charged", price)
    if not await settle_charge(session, uid, price, charged):
        logger.warning(f"Email reorder {eo.email_id}: charged {charged} > reserved {price}")
        charged = price
    await update_email_order(session, eo.email_id, status="WAIT", code=None, price=float(charged))
    eo = await get_email_order(session, eo.email_id)
    await _render(call, _card_text(lang, eo, currency.user_currency(user)), _card_kb(lang, eo))
    await call.answer(t("email_reordered", lang), show_alert=True)


@router.callback_query(F.data.startswith("email:cancel:"))
async def cancel_email(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    eo = await _own_email(call, session, lang)
    if not eo:
        return
    if eo.status != "WAIT":
        await call.answer(t("order_already_closed", lang), show_alert=True)
        return

    result = await getsms.cancel_email(eo.email_id)
    if not result.get("ok"):
        await call.answer(t("email_cancel_failed", lang), show_alert=True)
        return
    # The transition succeeds once, so the refund can't be paid twice
    if await transition_email_order(session, eo.email_id, "CANCELLED", from_statuses=("WAIT",)) \
            and result.get("refunded"):
        await add_balance(session, eo.user_id, eo.price)
    await _render(call, t("email_cancelled", lang), InlineKeyboardMarkup(inline_keyboard=[[_back(lang)]]))
    await call.answer()


# ─── History ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "email:history")
async def email_history(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    orders = await get_user_email_orders(session, call.from_user.id, limit=10)
    builder = InlineKeyboardBuilder()
    if not orders:
        text = t("email_history_empty", lang)
    else:
        lines = [t("email_history_title", lang)]
        for eo in orders:
            lines.append(f"• <code>{html.escape(eo.email)}</code> — {html.escape(eo.site)} — "
                         f"{currency.fmt(eo.price, currency.user_currency(user))} — {status_label(eo.status, lang)}")
            builder.button(text=eo.email[:60], callback_data=f"email:open:{eo.email_id}")
        text = "\n".join(lines)
    builder.adjust(1)
    builder.row(_back(lang))
    await _render(call, text, builder.as_markup())
    await call.answer()


def _int_part(data: str, index: int, default: int = 0) -> int:
    try:
        return int(data.split(":")[index])
    except (IndexError, ValueError):
        return default
