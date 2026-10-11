"""
/admin -> 💳 Payment methods

The owner manages every payment method from Telegram:
  * enter / replace API keys (the message with the key is deleted right away)
  * turn a method on / off (only possible once all required keys are set)
  * rename the button users see, set a minimum top-up amount
  * clear the keys
"""

import html

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import payments
from bot.handlers import admin

router = Router()


class AdminPayment(StatesGroup):
    value = State()     # waiting for a key / title / min amount; data: code, target


def _status(state: payments.MethodState) -> tuple[str, str]:
    if state.is_active:
        return "🟢", "Включён — виден пользователям"
    if not state.is_configured:
        if state.keys_unreadable:
            return "⚪", "Сохранённые ключи не расшифровываются — введите их заново"
        missing = ", ".join(f.label for f in state.missing_fields)
        return "⚪", f"Не настроен — не хватает: {missing}"
    return "🔴", "Выключен — скрыт от пользователей"


# ─── Screens ──────────────────────────────────────────────────────────────────

async def list_screen(session: AsyncSession) -> tuple[str, InlineKeyboardMarkup]:
    states = await payments.all_states(session)
    active = [s for s in states if s.is_active]
    names = ", ".join(html.escape(s.display_title()) for s in active) if active else "нет"
    text = (
        "💳 <b>Способы оплаты</b>\n\n"
        f"Активны сейчас ({len(active)}): <b>{names}</b>\n\n"
        "🟢 включён · 🔴 выключен · ⚪ нет ключей\n"
        "Нажмите на способ, чтобы ввести ключи или включить/выключить его."
    )
    builder = InlineKeyboardBuilder()
    for s in states:
        builder.button(text=f"{_status(s)[0]} {s.display_title()}", callback_data=f"admin:pay:m:{s.code}")
    builder.adjust(2)
    builder.row(InlineKeyboardButton(text="🔙 Назад", callback_data="admin:menu"))
    return text, builder.as_markup()


async def method_screen(session: AsyncSession, code: str) -> tuple[str, InlineKeyboardMarkup]:
    s = await payments.get_state(session, code)
    icon, status = _status(s)
    lines = [
        f"💳 <b>{html.escape(s.display_title())}</b>  <code>{code}</code>",
        f"{icon} {html.escape(status)}",
        f"Мин. сумма: {s.min_amount:g} руб." if s.min_amount else "Мин. сумма: —",
    ]
    if s.spec.hint:
        lines.append(f"ℹ️ {html.escape(s.spec.hint)}")
    lines.append("")
    lines.append("<b>Ключи</b>")
    for f in s.spec.fields:
        value = s.creds.get(f.key, "")
        shown = payments.mask(value) if f.secret else (html.escape(value) or "—")
        optional = " (необязательно)" if not f.required else ""
        lines.append(f"• {html.escape(f.label)}{optional}: <code>{shown}</code>")

    builder = InlineKeyboardBuilder()
    if s.enabled:
        builder.button(text="❌ Выключить", callback_data=f"admin:pay:off:{code}")
    else:
        builder.button(text="✅ Включить", callback_data=f"admin:pay:on:{code}")
    for f in s.spec.fields:
        builder.button(text=f"✏️ {f.label}", callback_data=f"admin:pay:f:{code}:{f.key}")
    builder.button(text="✏️ Название кнопки", callback_data=f"admin:pay:title:{code}")
    builder.button(text="✏️ Мин. сумма", callback_data=f"admin:pay:min:{code}")
    if s.spec.fields:
        builder.button(text="🗑 Удалить ключи", callback_data=f"admin:pay:clr:{code}")
    builder.button(text="🔙 Назад", callback_data="admin:pay")
    builder.adjust(1)
    return "\n".join(lines), builder.as_markup()


async def _show(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


def _parse_code(data: str, prefix: str) -> str | None:
    code = data[len(prefix):]
    return code if code in payments.METHODS else None


# ─── Callbacks ────────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:pay")
async def payments_list(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    await _show(call, *await list_screen(session))
    await call.answer()


@router.callback_query(F.data.startswith("admin:pay:m:"))
async def payment_card(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    code = _parse_code(call.data, "admin:pay:m:")
    if not code:
        return await call.answer("Неизвестный способ", show_alert=True)
    await state.clear()
    await _show(call, *await method_screen(session, code))
    await call.answer()


@router.callback_query(F.data.startswith("admin:pay:on:") | F.data.startswith("admin:pay:off:"))
async def payment_toggle(call: CallbackQuery, session: AsyncSession):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    turn_on = call.data.startswith("admin:pay:on:")
    code = _parse_code(call.data, "admin:pay:on:" if turn_on else "admin:pay:off:")
    if not code:
        return await call.answer("Неизвестный способ", show_alert=True)
    if not await payments.set_enabled(session, code, turn_on):
        s = await payments.get_state(session, code)
        missing = ", ".join(f.label for f in s.missing_fields) or "корректные ключи"
        return await call.answer(f"Сначала заполните: {missing}", show_alert=True)
    await _show(call, *await method_screen(session, code))
    await call.answer("🟢 Включено" if turn_on else "🔴 Выключено")


@router.callback_query(F.data.startswith("admin:pay:clr:"))
async def payment_clear_confirm(call: CallbackQuery):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    code = _parse_code(call.data, "admin:pay:clr:")
    if not code:
        return await call.answer("Неизвестный способ", show_alert=True)
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить ключи", callback_data=f"admin:pay:clr!:{code}")
    builder.button(text="✖️ Отмена", callback_data=f"admin:pay:m:{code}")
    builder.adjust(1)
    await _show(call, f"Удалить все сохранённые ключи <b>{code}</b> и выключить способ?", builder.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("admin:pay:clr!:"))
async def payment_clear(call: CallbackQuery, session: AsyncSession):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    code = _parse_code(call.data, "admin:pay:clr!:")
    if not code:
        return await call.answer("Неизвестный способ", show_alert=True)
    await payments.clear_credentials(session, code)
    await _show(call, *await method_screen(session, code))
    await call.answer("Ключи удалены")


@router.callback_query(
    F.data.startswith("admin:pay:f:") | F.data.startswith("admin:pay:title:") | F.data.startswith("admin:pay:min:")
)
async def payment_edit_prompt(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    parts = call.data.split(":")       # admin:pay:<what>:<code>[:<field>]
    what, code = parts[2], parts[3]
    spec = payments.METHODS.get(code)
    if spec is None:
        return await call.answer("Неизвестный способ", show_alert=True)

    if what == "f":
        fld = next((f for f in spec.fields if len(parts) == 5 and f.key == parts[4]), None)
        if fld is None:
            return await call.answer("Неизвестное поле", show_alert=True)
        target = f"cred:{fld.key}"
        prompt = f"✏️ Отправьте <b>{html.escape(fld.label)}</b> для <b>{code}</b>."
        if fld.secret:
            prompt += "\n\n🔒 Ваше сообщение будет удалено сразу после сохранения."
    elif what == "title":
        target = "title"
        prompt = (f"✏️ Отправьте новое название кнопки для <b>{code}</b>.\n"
                  f"Отправьте <code>-</code>, чтобы вернуть «{html.escape(spec.default_title())}».")
    else:
        target = "min"
        prompt = f"✏️ Отправьте минимальную сумму пополнения в рублях для <b>{code}</b>.\nОтправьте <code>0</code>, чтобы убрать минимум."

    await state.set_state(AdminPayment.value)
    await state.update_data(code=code, target=target)
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Отмена", callback_data=f"admin:pay:m:{code}")
    await _show(call, prompt, builder.as_markup())
    await call.answer()


# ─── Value input ──────────────────────────────────────────────────────────────

@router.message(AdminPayment.value)
async def payment_value_input(message: Message, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(message.from_user.id):
        return
    data = await state.get_data()
    code, target = data.get("code"), data.get("target", "")
    if code not in payments.METHODS:
        await state.clear()
        return

    if target.startswith("cred:"):
        # Remove the key from the chat history before anything else
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

    text = (message.text or "").strip()
    if not text:
        await message.answer("Отправьте значение текстовым сообщением.")
        return
    if text.startswith("/"):        # /cancel, /admin, ... - never save a command as a key
        await state.clear()
        await message.answer("✖️ Отменено, ничего не сохранено.")
        return

    try:
        if target.startswith("cred:"):
            await payments.set_credential(session, code, target[5:], text)
        elif target == "title":
            await payments.set_title(session, code, None if text == "-" else text)
        elif target == "min":
            await payments.set_min_amount(session, code, payments.parse_number(text))
        else:
            await state.clear()
            return
    except ValueError as e:
        await message.answer(f"❌ {html.escape(str(e))}. Попробуйте ещё раз или нажмите «Отмена».", parse_mode="HTML")
        return

    await state.clear()
    card_text, kb = await method_screen(session, code)
    await message.answer("✅ Сохранено\n\n" + card_text, reply_markup=kb, parse_mode="HTML")
