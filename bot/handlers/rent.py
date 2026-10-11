import html

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot import antiabuse, currency, pricing
from bot.api.getsms import getsms
from bot.handlers import admin
from bot.db.queries import (
    ACTIVE_ORDER_STATUSES, add_balance, create_rental, deduct_balance, get_last_rental,
    get_or_create_user, get_rental, increment_user_counter, settle_charge, transition_rental,
)
from bot.i18n import status_label, t
from bot.keyboards import back_keyboard, error_keyboard, services_keyboard

router = Router()
PAGE_SIZE = 21
RENT_HOURS = (4, 12, 24, 48, 72)     # API: 4, 12 or multiples of 24


@router.callback_query(F.data == "menu:rent")
async def rent_menu(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    if not user.selected_country_id:
        await call.answer(t("error_select_country", user.language), show_alert=True)
        return
    await show_rent_services(call, session, user, page=1)


@router.callback_query(F.data.startswith("rent_services_page:"))
async def rent_services_page(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    page = _int_part(call.data, 1, default=1)
    await show_rent_services(call, session, user, page=page)


async def show_rent_services(call, session, user, page):
    lang = user.language
    services = await getsms.get_services(user.selected_country_id, is_rent=True)
    total = len(services)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    chunk = services[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    text = t("rent_text", lang)
    kb = services_keyboard(chunk, page, total_pages, lang, is_rent=True)
    await call.message.edit_caption(caption=text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("rent_service:"))
async def rent_service_card(call: CallbackQuery, session: AsyncSession):
    service_id = _int_part(call.data, 1)
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language

    builder = InlineKeyboardBuilder()
    for hours in RENT_HOURS:
        price = pricing.sell(await getsms.rental_price(service_id, hours))
        builder.button(
            text=t("rent_duration_btn", lang, hours=hours,
                   price=currency.fmt(price, currency.user_currency(user), up=True) if price is not None else "?"),
            callback_data=f"rent:create:{service_id}:{hours}",
        )
    builder.adjust(1)
    builder.row(
        InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:rent"),
        InlineKeyboardButton(text=t("btn_menu", lang), callback_data="menu:main"),
    )

    await call.message.edit_caption(caption=t("rent_duration_text", lang), reply_markup=builder.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("rent:create:"))
async def create_rent(call: CallbackQuery, session: AsyncSession):
    if await antiabuse.refuse_purchase(call, session):
        return
    async with antiabuse.purchase_lock(call.from_user.id):
        await _create_rent(call, session)


async def _create_rent(call: CallbackQuery, session: AsyncSession):
    """Same money flow as activations: reserve -> order -> settle the difference / refund."""
    service_id = _int_part(call.data, 2)
    rent_time = _int_part(call.data, 3)
    uid = call.from_user.id
    user = await get_or_create_user(session, uid)
    lang = user.language
    retry = error_keyboard(lang, retry_cb=f"rent:create:{service_id}:{rent_time}")

    if rent_time not in RENT_HOURS:
        await call.answer(t("error_generic", lang), show_alert=True)
        return

    price = pricing.sell(await getsms.rental_price(service_id, rent_time))
    if not price:
        await call.message.edit_caption(caption=t("error_no_numbers", lang), reply_markup=error_keyboard(lang))
        await call.answer()
        return

    if not await deduct_balance(session, uid, price):
        await call.message.edit_caption(caption=t("insufficient_balance", lang), reply_markup=retry)
        await call.answer()
        return

    result = await getsms.create_rental(service_id, rent_time, operator=user.selected_operator or "any")
    order = result.get("order") or result.get("data") or {}
    order_id = order.get("id") or result.get("order_id")

    if not result.get("ok") or not order_id:
        await add_balance(session, uid, price)
        if result.get("ok"):
            logger.error(f"GetSMS rent ok but no order id: {result}")
        err = result.get("error_code")
        await admin.alert_provider_empty(call.bot, err)
        text = t("error_no_numbers", lang) if err in ("NO_NUMBERS", "SERVICE_NOT_AVAILABLE") else t("error_generic", lang)
        await call.message.edit_caption(caption=text, reply_markup=retry)
        await call.answer()
        return

    actual = pricing.sell(order.get("price")) or price
    if not await settle_charge(session, uid, price, actual):
        await getsms.finish_rental(order_id)
        await add_balance(session, uid, price)
        await call.message.edit_caption(caption=t("insufficient_balance", lang), reply_markup=retry)
        await call.answer()
        return

    phone = order.get("phone_number", "?")
    services = await getsms.get_services(user.selected_country_id, is_rent=True)
    name = next((s["name"] for s in services if s.get("id") == service_id), str(service_id))
    await create_rental(session,
        user_id=uid,
        order_id=order_id,
        service_id=service_id,
        service_name=name,
        phone_number=phone,
        price=actual,
        status="WAITING",
    )
    await increment_user_counter(session, uid, "total_rented")

    text = t("rent_created", lang, phone=html.escape(str(phone)),
             price=currency.fmt(actual, currency.user_currency(user)), hours=rent_time)
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_finish_order", lang), callback_data=f"rent:finish:{order_id}")
    builder.row(
        InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:main"),
        InlineKeyboardButton(text=t("btn_menu", lang), callback_data="menu:main"),
    )
    await call.message.edit_caption(caption=text, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer("✅")


@router.callback_query(F.data.startswith("rent:finish:"))
async def finish_rent(call: CallbackQuery, session: AsyncSession):
    order_id = _int_part(call.data, 2)
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language

    rental = await get_rental(session, order_id)
    if rental is None or rental.user_id != call.from_user.id:
        await call.answer(t("order_not_found", lang), show_alert=True)
        return
    if rental.status not in ACTIVE_ORDER_STATUSES:
        await call.answer(t("order_already_closed", lang), show_alert=True)
        return

    result = await getsms.finish_rental(order_id)
    if result.get("ok"):
        await transition_rental(session, order_id, "FINISHED")
        text = t("order_finished", lang)
    else:
        text = t("error_generic", lang)
    await call.message.edit_caption(caption=text, reply_markup=back_keyboard(lang))
    await call.answer()


# ─── Last rental ──────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:last_rent")
async def last_rental(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    rent = await get_last_rental(session, call.from_user.id)

    if not rent:
        text = t("history_empty", lang)
    else:
        text = t("last_rent_text", lang,
                 phone=html.escape(rent.phone_number or ""), service=html.escape(rent.service_name or ""),
                 price=currency.fmt(rent.price, currency.user_currency(user)), status=status_label(rent.status, lang), date=str(rent.created_at)[:16])

    await call.message.edit_caption(caption=text, reply_markup=back_keyboard(lang), parse_mode="HTML")
    await call.answer()


def _int_part(data: str, index: int, default: int = 0) -> int:
    try:
        return int(data.split(":")[index])
    except (IndexError, ValueError):
        return default
