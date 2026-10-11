"""/admin -> 💹 Наценка (multiplier on GetSMS prices) and 💱 Курсы валют (display currency rates)."""

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import currency, pricing
from bot.handlers import admin

router = Router()
EXAMPLE_COST = 100


class AdminMarkup(StatesGroup):
    value = State()


class AdminRate(StatesGroup):
    value = State()             # data: cur


def screen_text() -> str:
    m = pricing.markup()
    profit = round(EXAMPLE_COST * (m - 1), 2)
    return (
        "💹 <b>Наценка на номера</b>\n\n"
        f"Сейчас: <b>×{m:g}</b>\n"
        f"Пример: номер за {EXAMPLE_COST} ₽ у поставщика → пользователь платит "
        f"<b>{pricing.sell(EXAMPLE_COST):g} ₽</b>, ваша прибыль <b>{profit:g} ₽</b>.\n\n"
        "Действует на покупку и аренду номеров. Цены на кнопках и списания меняются сразу."
    )


def screen_kb() -> InlineKeyboardMarkup:
    current = pricing.markup()
    builder = InlineKeyboardBuilder()
    for p in pricing.PRESETS:
        mark = "✅ " if p == current else ""
        label = "без наценки" if p == 1 else f"×{p:g}"
        builder.button(text=f"{mark}{label}", callback_data=f"admin:mk:set:{p:g}")
    builder.button(text="✏️ Своя наценка", callback_data="admin:mk:custom")
    builder.button(text="🔙 Назад", callback_data="admin:menu")
    builder.adjust(len(pricing.PRESETS), 1, 1)
    return builder.as_markup()


async def _show(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data == "admin:mk")
async def markup_screen(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    await _show(call, screen_text(), screen_kb())
    await call.answer()


@router.callback_query(F.data.startswith("admin:mk:set:"))
async def markup_set(call: CallbackQuery, session: AsyncSession):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    try:
        await pricing.set_markup(session, pricing.parse_number(call.data.rsplit(":", 1)[1]))
    except ValueError as e:
        return await call.answer(str(e), show_alert=True)
    await _show(call, screen_text(), screen_kb())
    await call.answer(f"Наценка ×{pricing.markup():g}")


@router.callback_query(F.data == "admin:mk:custom")
async def markup_custom(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminMarkup.value)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="admin:mk")]])
    await _show(call, (
        f"✏️ Отправьте множитель от {pricing.MIN_MARKUP:g} до {pricing.MAX_MARKUP:g}, например "
        "<code>1.8</code> или <code>2,5</code>."
    ), kb)
    await call.answer()


@router.message(AdminMarkup.value)
async def markup_input(message: Message, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(message.from_user.id):
        return
    text = (message.text or "").strip().lstrip("xXхХ×")
    if text.startswith("/"):
        await state.clear()
        await message.answer("✖️ Отменено.")
        return
    try:
        await pricing.set_markup(session, pricing.parse_number(text))
    except ValueError as e:
        await message.answer(f"❌ {html.escape(str(e))}. Попробуйте ещё раз.")
        return
    await state.clear()
    await message.answer("✅ Сохранено\n\n" + screen_text(), reply_markup=screen_kb(), parse_mode="HTML")


# ─── Exchange rates ───────────────────────────────────────────────────────────

SOURCE_LABEL = {"manual": "вручную", "auto": "ЦБ РФ", "fallback": "по умолчанию — курс ещё не загружен"}


def rates_text() -> str:
    lines = ["💱 <b>Курсы валют</b>", "",
             "Балансы и цены хранятся в рублях, пользователи видят их в $ или € (выбирают в профиле).", ""]
    for cur in currency.CURRENCIES:
        src = currency.rate_source(cur)
        note = SOURCE_LABEL[src] + (f" от {currency.auto_date()}" if src == "auto" and currency.auto_date() else "")
        lines.append(f"1 {currency.CURRENCIES[cur]} = <b>{currency.rate(cur):g} ₽</b> ({note})")
    lines += ["", "Курс ЦБ обновляется автоматически каждый час. Ручной курс заменяет его, пока вы не вернёте «Авто».",
              "Пополнение криптой выставляется в USD по этому же курсу."]
    return "\n".join(lines)


def rates_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cur in currency.CURRENCIES:
        builder.button(text=f"✏️ Курс {cur}", callback_data=f"admin:fx:set:{cur}")
    for cur in currency.CURRENCIES:
        if currency.rate_source(cur) == "manual":
            builder.button(text=f"🔄 {cur}: авто (ЦБ)", callback_data=f"admin:fx:auto:{cur}")
    builder.button(text="🔄 Обновить с ЦБ сейчас", callback_data="admin:fx:refresh")
    builder.button(text="🔙 Назад", callback_data="admin:menu")
    builder.adjust(len(currency.CURRENCIES), 1)
    return builder.as_markup()


def _cur_from(data: str) -> str | None:
    cur = data.rsplit(":", 1)[1]
    return cur if cur in currency.CURRENCIES else None


@router.callback_query(F.data == "admin:fx")
async def rates_screen(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    await _show(call, rates_text(), rates_kb())
    await call.answer()


@router.callback_query(F.data == "admin:fx:refresh")
async def rates_refresh(call: CallbackQuery, session: AsyncSession):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    got = await currency.fetch_cbr()
    if got is None:
        return await call.answer("ЦБ сейчас недоступен, используется последний сохранённый курс", show_alert=True)
    await currency.store_auto(session, *got)
    await _show(call, rates_text(), rates_kb())
    await call.answer("Курс обновлён")


@router.callback_query(F.data.startswith("admin:fx:auto:"))
async def rates_auto(call: CallbackQuery, session: AsyncSession):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    if (cur := _cur_from(call.data)) is None:
        return await call.answer("Неизвестная валюта", show_alert=True)
    await currency.set_manual(session, cur, None)
    await _show(call, rates_text(), rates_kb())
    await call.answer(f"{cur}: автоматический курс")


@router.callback_query(F.data.startswith("admin:fx:set:"))
async def rates_set_prompt(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    if (cur := _cur_from(call.data)) is None:
        return await call.answer("Неизвестная валюта", show_alert=True)
    await state.set_state(AdminRate.value)
    await state.update_data(cur=cur)
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✖️ Отмена", callback_data="admin:fx")]])
    await _show(call, f"✏️ Сколько рублей стоит 1 {currency.CURRENCIES[cur]}? Например <code>92.5</code>.", kb)
    await call.answer()


@router.message(AdminRate.value)
async def rates_input(message: Message, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(message.from_user.id):
        return
    cur = (await state.get_data()).get("cur")
    text = (message.text or "").strip()
    if text.startswith("/") or cur not in currency.CURRENCIES:
        await state.clear()
        await message.answer("✖️ Отменено.")
        return
    try:
        await currency.set_manual(session, cur, pricing.parse_number(text))
    except ValueError as e:
        await message.answer(f"❌ {html.escape(str(e))}. Попробуйте ещё раз.")
        return
    await state.clear()
    await message.answer("✅ Сохранено\n\n" + rates_text(), reply_markup=rates_kb(), parse_mode="HTML")
