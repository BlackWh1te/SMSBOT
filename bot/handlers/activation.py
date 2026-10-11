import asyncio
import html
import time
from datetime import timedelta

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot import antiabuse, countries as country_names, currency, pricing
from bot.api.getsms import getsms
from bot.handlers import admin
from bot.db.models import utcnow
from bot.db.queries import (
    ACTIVE_ORDER_STATUSES, add_balance, count_active_activations, create_activation, deduct_balance, get_activation,
    get_active_activations, get_favorite, get_favorites, get_last_activation, get_or_create_user, get_user,
    get_user_activations, increment_user_counter, settle_charge, toggle_favorite, transition_activation,
    update_activation, update_user,
)
from bot.i18n import operator_label, status_label, t
from bot.keyboards import (
    back_keyboard, countries_keyboard, error_keyboard, order_active_keyboard, service_card_keyboard,
    services_keyboard,
)

router = Router()
PAGE_SIZE = 21                  # 7 rows × 3 cols
POLL_INTERVAL = 5               # seconds between live status checks
POLL_ATTEMPTS = 21 * 60 // POLL_INTERVAL   # GetSMS orders live 20 min by default
SWEEP_INTERVAL = 60             # restart-safe background check of all open orders
SWEEP_WINDOW = timedelta(hours=24)
CALLBACK_DATA_LIMIT = 64

# order_id -> live polling task. Keeping references stops the tasks from being
# garbage-collected and prevents two pollers for the same order.
_pollers: dict[int, asyncio.Task] = {}


# ─── Country selection ────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:buy")
async def buy_number_menu(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    if not user.selected_country_id:
        await call.answer(t("error_select_country", user.language), show_alert=True)
        return
    await show_services(call, session, user, page=1)


@router.callback_query(F.data == "menu:country")
async def country_menu(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    await show_countries(call, session, user, page=1)


@router.callback_query(F.data.startswith("countries_page:"))
async def countries_page(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    page = _int_part(call.data, 1, default=1)
    await show_countries(call, session, user, page=page)


async def countries_screen(user, page: int) -> tuple[str, InlineKeyboardMarkup]:
    lang = user.language
    all_countries = await getsms.get_countries()
    total = len(all_countries)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    chunk = all_countries[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]
    return t("select_country_text", lang), countries_keyboard(chunk, page, total_pages, lang)


async def show_countries(call: CallbackQuery, session: AsyncSession, user, page: int):
    text, kb = await countries_screen(user, page)
    await call.message.edit_caption(caption=text, reply_markup=kb)
    await call.answer()


@router.callback_query(F.data.startswith("country:"))
async def select_country(call: CallbackQuery, session: AsyncSession):
    country_id = _int_part(call.data, 1)
    user = await get_or_create_user(session, call.from_user.id)

    countries = await getsms.get_countries()
    country = next((c for c in countries if c["id"] == country_id), None)
    if not country:
        await call.answer(t("error_country_not_found", user.language), show_alert=True)
        return

    await update_user(session, call.from_user.id,
                      selected_country_id=country_id,
                      selected_country_name=country["name"])
    user.selected_country_id = country_id
    user.selected_country_name = country["name"]

    await call.answer(f"✅ {country_names.localize(country['name'], user.language)}")
    await show_services(call, session, user, page=1, answer=False)


# ─── Service selection ────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("services_page:"))
async def services_page(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    page = _int_part(call.data, 1, default=1)
    await show_services(call, session, user, page=page)


async def services_screen(user, page: int) -> tuple[str, InlineKeyboardMarkup]:
    lang = user.language
    services = await getsms.get_services(user.selected_country_id)
    total = len(services)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    chunk = services[(page - 1) * PAGE_SIZE: page * PAGE_SIZE]

    text = t("select_service_text", lang,
             balance=currency.fmt(user.balance, currency.user_currency(user)),
             country=country_names.localize(user.selected_country_name, lang) or "?",
             operator=operator_label(user.selected_operator, lang),
             cashback=currency.fmt(user.cashback, currency.user_currency(user)))
    return text, services_keyboard(chunk, page, total_pages, lang)


async def show_services(call: CallbackQuery, session: AsyncSession, user, page: int, answer: bool = True):
    text, kb = await services_screen(user, page)
    await call.message.edit_caption(caption=text, reply_markup=kb)
    if answer:
        await call.answer()


# ─── Service card (all price tiers, cheap and expensive) ─────────────────────

@router.callback_query(F.data.startswith("service:"))
async def show_service_card(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    await render_service_card(call, session, user, _int_part(call.data, 1))


async def render_service_card(call: CallbackQuery, session: AsyncSession, user, service_id: int,
                              answer: bool = True):
    lang = user.language
    price = await getsms.get_service_price(service_id)
    count = await getsms.get_service_count(service_id)

    if price is None:
        await call.answer(t("error_no_numbers", lang), show_alert=True)
        return

    tiers = await getsms.get_price_tiers(service_id)
    services = await getsms.get_services(user.selected_country_id)
    service = next((s for s in services if s["id"] == service_id), {"name": "?"})

    text = t("service_card_text", lang,
             service=service["name"],
             country=country_names.localize(user.selected_country_name, lang) or "?",
             balance=currency.fmt(user.balance, currency.user_currency(user)))

    kb = service_card_keyboard(service_id, price, "?" if count is None else count, lang, tiers=tiers,
                               cur=currency.user_currency(user))
    await call.message.edit_caption(caption=text, reply_markup=kb)
    if answer:
        await call.answer()


# ─── Create order ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("order:create:"))
async def create_order(call: CallbackQuery, session: AsyncSession):
    """
    order:create:<service_id>            -> default (cheapest) price
    order:create:<service_id>:<kopecks>  -> a specific price tier

    Money flow: reserve the price atomically BEFORE ordering, pass it as max_price,
    then settle the difference. A user can never get a number they didn't pay for.
    """
    parts = call.data.split(":")
    service_id = _int_part(call.data, 2)
    tier_kopecks = _int_part(call.data, 3) if len(parts) > 3 else None
    uid = call.from_user.id
    user = await get_or_create_user(session, uid)
    lang = user.language

    lock = antiabuse.purchase_lock(uid)
    if lock.locked():                       # a second tap while the first purchase is still running
        await call.answer(t("too_fast", lang))
        return
    async with lock:
        await _place_order(call, session, user, service_id, tier_kopecks)


STALE_PRICE = "stale"


async def verified_cost(service_id: int, tier_kopecks: int | None) -> float | str | None:
    """
    The GetSMS cost for this purchase, never taken on trust from the button:
    a tier must be in GetSMS's current price list; without a list, never below the default price.
    Returns STALE_PRICE when the list changed since the card was shown.
    """
    if not tier_kopecks:
        return await getsms.get_service_price(service_id)
    tiers = await getsms.get_price_tiers(service_id)
    if tiers:
        if tier_kopecks in {round(tier["price"] * 100) for tier in tiers}:
            return round(tier_kopecks / 100, 2)
        return STALE_PRICE
    default = await getsms.get_service_price(service_id)
    return max(round(tier_kopecks / 100, 2), float(default)) if default else None


async def _place_order(call: CallbackQuery, session: AsyncSession, user, service_id: int, tier_kopecks: int | None):
    uid, lang = user.id, user.language

    active = await count_active_activations(session, uid)
    if active >= antiabuse.MAX_ACTIVE_ORDERS:
        await call.answer(t("too_many_active_orders", lang, count=active), show_alert=True)
        return
    if not antiabuse.purchase_allowed(uid):
        await call.answer(t("order_rate_limited", lang), show_alert=True)
        return

    cost = await verified_cost(service_id, tier_kopecks)
    if cost == STALE_PRICE:
        await render_service_card(call, session, user, service_id, answer=False)
        await call.answer(t("price_changed", lang), show_alert=True)
        return
    price = pricing.sell(cost)
    if not price:
        await call.answer(t("error_no_numbers", lang), show_alert=True)
        return

    if not await deduct_balance(session, uid, price):
        await call.message.edit_caption(caption=t("insufficient_balance", lang),
                                        reply_markup=error_keyboard(lang, retry_cb=call.data))
        await call.answer()
        return

    result = await getsms.create_order(service_id, operator=user.selected_operator or "any", max_price=cost)

    if not result.get("ok"):
        await add_balance(session, uid, price)          # nothing bought - give the reservation back
        err = result.get("error_code", "UNKNOWN")
        await admin.alert_provider_empty(call.bot, err)
        if err in ("NO_NUMBERS", "SERVICE_NOT_AVAILABLE"):
            text, kb = t("error_no_numbers", lang), error_keyboard(lang)
        else:
            # incl. INSUFFICIENT_BALANCE: that's the owner's GetSMS account, not the user's balance
            text, kb = t("error_generic", lang), error_keyboard(lang, retry_cb=call.data)
        await call.message.edit_caption(caption=text, reply_markup=kb)
        await call.answer()
        return

    order = result["order"]
    service_name = (result.get("service") or {}).get("name", "?")
    order_id = order["id"]
    phone = order["phone_number"]
    order_price = pricing.sell(order.get("price")) or price

    if not await settle_charge(session, uid, price, order_price):
        # GetSMS charged more than we reserved and the user can't cover the rest:
        # cancel the number and refund, never hand it out for free.
        await getsms.finish_order(order_id)
        await add_balance(session, uid, price)
        await call.message.edit_caption(caption=t("insufficient_balance", lang),
                                        reply_markup=error_keyboard(lang, retry_cb=call.data))
        await call.answer()
        return

    await increment_user_counter(session, uid, "total_bought")
    await create_activation(session,
        user_id=uid,
        order_id=order_id,
        service_id=service_id,
        service_name=service_name,
        phone_number=phone,
        price=order_price,
        status="WAITING",
    )

    expire = order.get("expire_date", "")
    expire_fmt = expire[:16].replace("T", " ") if expire else "—"
    text = t("order_created", lang, phone=phone, price=currency.fmt(order_price, currency.user_currency(user)),
             expire=expire_fmt)
    kb = order_active_keyboard(order_id, lang, can_repeat=False)
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer("✅")

    start_polling(call, session, order_id, lang)


# ─── Order status (shared by the live poller and the background sweeper) ─────

async def apply_order_status(session: AsyncSession, order_id: int, data: dict) -> tuple[str | None, object]:
    """
    Store what GetSMS reports. Returns (event, activation):
      "received"  - a NEW code arrived (repeat codes count once for statistics)
      "cancelled" - GetSMS cancelled the order; the user was refunded exactly once
      "finished"  - order closed
      None        - nothing new
    """
    act = await get_activation(session, order_id)
    if act is None:
        return None, None
    status = data.get("status")
    code = data.get("last_code")

    if status == "RECEIVED" and code:
        if act.sms_code == str(code) or act.status not in ACTIVE_ORDER_STATUSES:
            return None, act
        first_code = act.sms_code is None
        sms_list = data.get("sms_list") or []
        await update_activation(session, order_id, status="RECEIVED",
                                sms_code=str(code), sms_text=str(sms_list[-1]) if sms_list else "")
        if first_code:
            await increment_user_counter(session, act.user_id, "total_activated")
        await session.refresh(act)
        return "received", act

    if status == "CANCELLED":
        if await transition_activation(session, order_id, "CANCELLED"):
            await add_balance(session, act.user_id, act.price)
            return "cancelled", act
        return None, act

    if status == "FINISHED":
        if await transition_activation(session, order_id, "FINISHED"):
            return "finished", act
    return None, act


def received_text(lang: str, phone: str, code: str, sms_text: str) -> str:
    return t("sms_received", lang,
             phone=html.escape(str(phone)), code=html.escape(str(code)), sms_text=html.escape(str(sms_text or "")))


# ─── Live polling (edits the order message) ──────────────────────────────────

def start_polling(call: CallbackQuery, session: AsyncSession, order_id: int, lang: str) -> asyncio.Task:
    running = _pollers.get(order_id)
    if running and not running.done():
        return running
    task = asyncio.create_task(poll_sms(call, session, order_id, lang))
    _pollers[order_id] = task

    def _forget(t_: asyncio.Task):
        if _pollers.get(order_id) is t_:
            _pollers.pop(order_id, None)
    task.add_done_callback(_forget)
    return task


async def poll_sms(call: CallbackQuery, session: AsyncSession, order_id: int, lang: str):
    """Poll GetSMS every POLL_INTERVAL seconds for the order's lifetime and update the message."""
    from bot.db import AsyncSessionLocal

    async def show(text, kb):
        try:
            await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
        except Exception as e:          # e.g. the user deleted the message - keep the data correct anyway
            logger.warning(f"Order {order_id}: could not update message: {e!r}")

    for _ in range(POLL_ATTEMPTS):
        await asyncio.sleep(POLL_INTERVAL)
        result = await getsms.get_order(order_id)
        if not result.get("ok"):
            if result.get("error_code") == "NETWORK_ERROR":
                continue                # temporary problem - keep polling
            break
        data = result.get("data") or {}
        try:
            async with AsyncSessionLocal() as s:
                event, act = await apply_order_status(s, order_id, data)
                owner = await get_user(s, act.user_id) if event == "cancelled" and act else None
        except Exception as e:
            logger.error(f"SMS poll error for order {order_id}: {e!r}")
            continue

        if event == "received":
            await show(received_text(lang, data.get("phone_number", act.phone_number), act.sms_code, act.sms_text),
                       order_active_keyboard(order_id, lang, can_repeat=True))
            return
        if event == "cancelled":
            await show(refund_text(lang, act, currency.user_currency(owner)), back_keyboard(lang))
            return
        if event == "finished" or data.get("status") in ("FINISHED", "CANCELLED"):
            return


# ─── Background sweeper: refunds and codes even after a restart ──────────────

async def sweep_orders_once(session_factory, notify) -> int:
    """Check every open order that has no live poller. `notify(user_id, text, kb)` sends a message."""
    handled = 0
    async with session_factory() as session:
        for act in await get_active_activations(session, utcnow() - SWEEP_WINDOW):
            poller = _pollers.get(act.order_id)
            if poller and not poller.done():
                continue
            result = await getsms.get_order(act.order_id)
            if not result.get("ok"):
                continue
            data = result.get("data") or {}
            event, act = await apply_order_status(session, act.order_id, data)
            if not event or event == "finished":
                continue
            user = await get_user(session, act.user_id)
            lang = user.language if user else "ru"
            if event == "received":
                text = received_text(lang, act.phone_number, act.sms_code, act.sms_text)
                kb = order_active_keyboard(act.order_id, lang, can_repeat=True)
            else:
                text = refund_text(lang, act, currency.user_currency(user))
                kb = back_keyboard(lang)
            handled += 1
            try:
                await notify(act.user_id, text, kb)
            except Exception as e:
                logger.warning(f"Could not notify user {act.user_id} about order {act.order_id}: {e!r}")
    return handled


async def order_sweeper(session_factory, notify, interval: int = SWEEP_INTERVAL):
    logger.info("Order sweeper started")
    while True:
        try:
            await sweep_orders_once(session_factory, notify)
        except Exception as e:
            logger.error(f"Order sweeper pass failed: {e!r}")
        await asyncio.sleep(interval)


# ─── Request another code ─────────────────────────────────────────────────────

async def _own_active_order(call: CallbackQuery, session: AsyncSession, order_id: int, lang: str):
    """The caller's own, still-open activation - or None (and an alert was shown)."""
    act = await get_activation(session, order_id)
    if act is None or act.user_id != call.from_user.id:
        await call.answer(t("order_not_found", lang), show_alert=True)
        return None
    if act.status not in ACTIVE_ORDER_STATUSES:
        await call.answer(t("order_already_closed", lang), show_alert=True)
        return None
    return act


@router.callback_query(F.data.startswith("order:repeat:"))
async def request_another_code(call: CallbackQuery, session: AsyncSession):
    order_id = _int_part(call.data, 2)
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    if not await _own_active_order(call, session, order_id, lang):
        return

    result = await getsms.request_another_code(order_id)
    if result.get("ok"):
        await call.answer(t("another_code_requested", lang))
        start_polling(call, session, order_id, lang)
    else:
        await call.answer(t("error_generic", lang), show_alert=True)


# ─── Finish order ─────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("order:finish:"))
async def finish_order(call: CallbackQuery, session: AsyncSession):
    order_id = _int_part(call.data, 2)
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    act = await _own_active_order(call, session, order_id, lang)
    if not act:
        return

    result = await getsms.finish_order(order_id)
    if not result.get("ok"):
        await call.answer(t("error_generic", lang), show_alert=True)
        return

    if result.get("refunded"):
        # The status transition succeeds only once, so the refund can't be paid twice
        if await transition_activation(session, order_id, "CANCELLED"):
            await add_balance(session, act.user_id, act.price)
        text = t("order_refunded", lang)
    else:
        await transition_activation(session, order_id, "FINISHED")
        text = t("order_finished", lang)
    await call.message.edit_caption(caption=text, reply_markup=back_keyboard(lang))
    await call.answer("✅")


# ─── History ──────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:history")
async def show_history(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    history = await get_user_activations(session, call.from_user.id, limit=10)

    if not history:
        text = t("history_empty", lang)
    else:
        cur = currency.user_currency(user)
        lines = [_history_line(a, lang, cur) for a in history]
        text = "\n\n".join(lines)
        while len(text) > 1024 and len(lines) > 1:     # caption limit
            lines.pop()
            text = "\n\n".join(lines)

    kb = back_keyboard(lang)
    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


def refund_text(lang: str, act, cur: str) -> str:
    return t("order_cancelled_refunded", lang, phone=html.escape(act.phone_number), amount=currency.fmt(act.price, cur))


def _history_line(a, lang: str, cur: str) -> str:
    return t("history_item", lang,
             id=a.order_id,
             service=html.escape(a.service_name or ""),
             phone=html.escape(a.phone_number or ""),
             status=status_label(a.status, lang),
             price=currency.fmt(a.price, cur),
             date=str(a.created_at)[:16])


# ─── Last activation ──────────────────────────────────────────────────────────

@router.callback_query(F.data == "menu:last_act")
async def last_activation(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    act = await get_last_activation(session, call.from_user.id)

    if not act:
        text = t("history_empty", lang)
        kb = back_keyboard(lang)
    else:
        text = _history_line(act, lang, currency.user_currency(user))
        kb = order_active_keyboard(act.order_id, lang) if act.status == "WAITING" else back_keyboard(lang)

    await call.message.edit_caption(caption=text, reply_markup=kb, parse_mode="HTML")
    await call.answer()


# ─── Top 10 countries for a service (cheapest first) ─────────────────────────

TOP_CACHE_TTL = 600             # seconds
TOP_CONCURRENCY = 8             # parallel GetSMS requests
_top_cache: dict[str, tuple[float, list[dict]]] = {}


async def cheapest_countries(service_name: str, limit: int = 10) -> list[dict]:
    """
    Cheapest countries for a service. Service ids differ per country, so the
    service is matched by name in every country's list. Cached for 10 minutes.
    """
    key = service_name.strip().lower()
    now = time.monotonic()
    cached = _top_cache.get(key)
    if cached and now - cached[0] < TOP_CACHE_TTL:
        return cached[1]

    sem = asyncio.Semaphore(TOP_CONCURRENCY)

    async def lookup(country: dict) -> dict | None:
        async with sem:
            services = await getsms.get_services(country["id"])
        for s in services:
            if str(s.get("name", "")).strip().lower() != key:
                continue
            try:
                price = float(s["price"])
            except (KeyError, TypeError, ValueError):
                return None
            if price > 0:
                return {"country_id": country["id"], "country": country["name"], "service_id": s["id"], "price": price}
        return None

    found = [r for r in await asyncio.gather(*(lookup(c) for c in await getsms.get_countries())) if r]
    found.sort(key=lambda r: r["price"])
    _top_cache[key] = (now, found[:limit])
    return found[:limit]


@router.callback_query(F.data.startswith("top10:"))
async def top10_countries(call: CallbackQuery, session: AsyncSession):
    service_id = _int_part(call.data, 1)
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language

    services = await getsms.get_services(user.selected_country_id)
    svc = next((s for s in services if s["id"] == service_id), None)
    if svc is None:
        await call.answer(t("error_no_numbers", lang), show_alert=True)
        return
    await call.answer(t("top10_loading", lang))

    top = await cheapest_countries(svc["name"])
    builder = InlineKeyboardBuilder()
    for r in top:
        builder.button(text=t("top10_item", lang, country=country_names.localize(r["country"], lang), price=currency.fmt(pricing.sell(r["price"]), currency.user_currency(user), up=True)),
                       callback_data=f"topc:{r['country_id']}:{r['service_id']}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data=f"service:{service_id}"))
    text = t("top10_title", lang, service=html.escape(svc["name"])) if top else t("top10_empty", lang)
    await call.message.edit_caption(caption=text, reply_markup=builder.as_markup())


async def _switch_country(session: AsyncSession, user, country_id: int) -> bool:
    countries = await getsms.get_countries()
    country = next((c for c in countries if c["id"] == country_id), None)
    if not country:
        return False
    await update_user(session, user.id, selected_country_id=country_id, selected_country_name=country["name"])
    user.selected_country_id, user.selected_country_name = country_id, country["name"]
    return True


@router.callback_query(F.data.startswith("topc:"))
async def top10_pick(call: CallbackQuery, session: AsyncSession):
    """Picked a country from the top-10 list: switch to it and open the service there."""
    user = await get_or_create_user(session, call.from_user.id)
    if not await _switch_country(session, user, _int_part(call.data, 1)):
        await call.answer(t("error_country_not_found", user.language), show_alert=True)
        return
    await render_service_card(call, session, user, _int_part(call.data, 2))


# ─── Favorites ────────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("fav:"))
async def toggle_favorite_service(call: CallbackQuery, session: AsyncSession):
    service_id = _int_part(call.data, 1)
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    if not user.selected_country_id:
        await call.answer(t("error_select_country", lang), show_alert=True)
        return
    services = await getsms.get_services(user.selected_country_id)
    svc = next((s for s in services if s["id"] == service_id), None)
    if svc is None:
        await call.answer(t("error_no_numbers", lang), show_alert=True)
        return
    added = await toggle_favorite(session, user.id, service_id, svc["name"],
                                  user.selected_country_id, user.selected_country_name or "?")
    await call.answer(t("fav_added" if added else "fav_removed", lang), show_alert=True)


@router.callback_query(F.data == "favs")
async def show_favorites(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    favs = await get_favorites(session, user.id)
    builder = InlineKeyboardBuilder()
    for f in favs:
        builder.button(text=f"⭐ {f.service_name} · {country_names.localize(f.country_name, lang)}"[:60], callback_data=f"favopen:{f.id}")
    builder.adjust(1)
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:buy"))
    text = t("favorites_title", lang) if favs else t("favorites_empty", lang)
    await call.message.edit_caption(caption=text, reply_markup=builder.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("favopen:"))
async def open_favorite(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    fav = await get_favorite(session, _int_part(call.data, 1))
    if fav is None or fav.user_id != user.id:
        await call.answer(t("order_not_found", user.language), show_alert=True)
        return
    if not await _switch_country(session, user, fav.country_id):
        await call.answer(t("error_country_not_found", user.language), show_alert=True)
        return
    await render_service_card(call, session, user, fav.service_id)


# ─── Search (services in the selected country, or countries) ─────────────────

class SearchFlow(StatesGroup):
    service = State()
    country = State()


@router.callback_query(F.data.startswith("search:"))
async def search_handler(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language
    if call.data == "search:service":
        if not user.selected_country_id:
            await call.answer(t("error_select_country", lang), show_alert=True)
            return
        await state.set_state(SearchFlow.service)
        prompt, back = t("search_prompt", lang), "menu:buy"
    else:
        await state.set_state(SearchFlow.country)
        prompt, back = t("search_country_prompt", lang), "menu:country"
    # Remember the banner message: results are shown there, so the flow keeps working
    await state.update_data(chat_id=call.message.chat.id, message_id=call.message.message_id)
    await call.message.edit_caption(caption=prompt, reply_markup=back_keyboard(lang, back_cb=back))
    await call.answer()


async def _show_search_results(message: Message, state: FSMContext, text: str, kb):
    data = await state.get_data()
    await state.clear()
    try:
        await message.bot.edit_message_caption(chat_id=data["chat_id"], message_id=data["message_id"],
                                               caption=text, reply_markup=kb)
    except Exception:                   # banner deleted/too old - fall back to a new message
        await message.answer(text, reply_markup=kb)


@router.message(SearchFlow.service)
async def search_service_input(message: Message, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, message.from_user.id)
    lang = user.language
    query = (message.text or "").strip()[:40]
    if not query:
        return
    results = await getsms.search_services(user.selected_country_id, query)
    if not results:                     # API search is locale-dependent - also match names locally
        services = await getsms.get_services(user.selected_country_id)
        results = [s for s in services if query.lower() in str(s.get("name", "")).lower()]
    if not results:
        await _show_search_results(message, state, t("search_no_results", lang, query=html.escape(query)),
                                   back_keyboard(lang, back_cb="menu:buy"))
        return
    await _show_search_results(message, state, t("search_results", lang, count=len(results)),
                               services_keyboard(results[:PAGE_SIZE], 1, 1, lang))


@router.message(SearchFlow.country)
async def search_country_input(message: Message, session: AsyncSession, state: FSMContext):
    user = await get_or_create_user(session, message.from_user.id)
    lang = user.language
    query = (message.text or "").strip().lower()[:40]
    if not query:
        return
    matches = [c for c in await getsms.get_countries() if country_names.matches(str(c.get("name", "")), query)]
    if not matches:
        await _show_search_results(message, state, t("search_no_results", lang, query=html.escape(query)),
                                   back_keyboard(lang, back_cb="menu:country"))
        return
    await _show_search_results(message, state, t("search_results", lang, count=len(matches)),
                               countries_keyboard(matches[:PAGE_SIZE], 1, 1, lang))


# ─── Features the number provider doesn't support ───────────────────────────

@router.callback_query(F.data.startswith("na:"))
async def not_available(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    await call.answer(t("feature_unavailable", user.language), show_alert=True)


# ─── Operator selection ───────────────────────────────────────────────────────

@router.callback_query(F.data == "operator:select")
async def select_operator_menu(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    lang = user.language

    if not user.selected_country_id:
        await call.answer(t("error_select_country", lang), show_alert=True)
        return

    ops = await getsms.get_operators(user.selected_country_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001f30d any", callback_data="operator:set:any")
    for op in ops[:15]:
        data = f"operator:set:{op}"
        if op != "any" and len(data.encode()) <= CALLBACK_DATA_LIMIT:
            builder.button(text=op, callback_data=data)
    builder.adjust(3)
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:country"))

    await call.message.edit_caption(caption=t("select_operator", lang), reply_markup=builder.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("operator:set:"))
async def set_operator(call: CallbackQuery, session: AsyncSession):
    operator = call.data.split(":", 2)[2][:32]
    await update_user(session, call.from_user.id, selected_operator=operator)
    await call.answer(f"✅ {operator}")
    user = await get_or_create_user(session, call.from_user.id)
    await show_countries(call, session, user, page=1)


# ─── LIVE button ──────────────────────────────────────────────────────────────

@router.callback_query(F.data.startswith("live:"))
async def live_count(call: CallbackQuery, session: AsyncSession):
    user = await get_or_create_user(session, call.from_user.id)
    await call.answer(t("live_refreshed", user.language), show_alert=False)


def _int_part(data: str, index: int, default: int = 0) -> int:
    """Parse the n-th ':'-separated part of callback data; forged/garbled data -> default."""
    try:
        return int(data.split(":")[index])
    except (IndexError, ValueError):
        return default
