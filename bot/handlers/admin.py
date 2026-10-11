import asyncio
import html
import math
import time

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import ADMIN_IDS
from bot.db.models import Activation, EmailOrder, Rental, User
from bot.db.queries import (
    add_balance, count_referrals, deduct_balance, get_all_users, get_recent_purchases, get_user, update_user,
)
from bot import currency, pricing
from bot.payments import active_methods

router = Router()

ADMIN_PAGE_SIZE = 10
MAX_ADMIN_AMOUNT = 1_000_000
BROADCAST_DELAY = 0.05          # ~20 msg/s, under Telegram's 30 msg/s limit
_broadcasts: set[asyncio.Task] = set()


class AdminAction(StatesGroup):
    add_balance = State()
    remove_balance = State()
    find_user = State()
    broadcast = State()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


PROVIDER_EMPTY_ERRORS = {"INSUFFICIENT_BALANCE", "NO_BALANCE", "NOT_ENOUGH_BALANCE"}
PROVIDER_ALERT_EVERY = 600      # seconds
_last_provider_alert = 0.0
PROVIDER_ALERT_TEXT = ("⚠️ <b>На балансе GetSMS не хватает денег</b>\n\n"
                       "Покупки номеров у пользователей не проходят (деньги им возвращаются). "
                       "Пополните аккаунт GetSMS.")


async def alert_provider_empty(bot, error_code: str | None) -> bool:
    """Tell the admins (at most every 10 min) that the GetSMS account can't pay for orders."""
    global _last_provider_alert
    now = time.monotonic()
    if error_code not in PROVIDER_EMPTY_ERRORS or now - _last_provider_alert < PROVIDER_ALERT_EVERY:
        return False
    _last_provider_alert = now
    logger.error(f"GetSMS refused an order: {error_code} - the provider account needs a top-up")
    for admin_id in ADMIN_IDS:
        try:
            await bot.send_message(admin_id, PROVIDER_ALERT_TEXT, parse_mode="HTML")
        except Exception as e:
            logger.warning(f"Could not alert admin {admin_id}: {e!r}")
    return True


def admin_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи", callback_data="admin:users:1")
    builder.button(text="📊 Статистика", callback_data="admin:stats")
    builder.button(text="💰 Начислить баланс", callback_data="admin:add_bal")
    builder.button(text="➖ Списать баланс", callback_data="admin:rem_bal")
    builder.button(text="🧾 Последние покупки", callback_data="admin:recent")
    builder.button(text="🔍 Найти пользователя", callback_data="admin:find")
    builder.button(text="📨 Рассылка", callback_data="admin:broadcast")
    builder.button(text="💳 Способы оплаты", callback_data="admin:pay")
    builder.button(text="💹 Наценка", callback_data="admin:mk")
    builder.button(text="💱 Курсы валют", callback_data="admin:fx")
    builder.button(text="🎨 Живые эмодзи", callback_data="admin:emoji")
    builder.adjust(2, 2, 2, 1, 1, 2, 1)
    return builder.as_markup()


def back_to_menu_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="🔙 Назад", callback_data="admin:menu")
    return builder.as_markup()


async def admin_panel_text(session: AsyncSession) -> str:
    active = await active_methods(session)
    names = ", ".join(html.escape(m.display_title()) for m in active) if active else "нет"
    return (f"🔧 <b>Админ-панель</b>\n\n💳 Активные способы оплаты: <b>{names}</b>\n"
            f"💹 Наценка на номера: <b>×{pricing.markup():g}</b>\n"
            "💱 Курсы: " + " · ".join(f"1 {s} = {currency.rate(c):g} ₽" for c, s in currency.CURRENCIES.items()))


@router.message(Command("admin"))
async def admin_panel(message: Message, session: AsyncSession, state: FSMContext | None = None):
    if not is_admin(message.from_user.id):
        return
    if state is not None:
        await state.clear()
    await message.answer(await admin_panel_text(session), reply_markup=admin_menu_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin:menu")
async def admin_menu_cb(call: CallbackQuery, session: AsyncSession, state: FSMContext | None = None):
    if not is_admin(call.from_user.id):
        return await call.answer()
    if state is not None:
        await state.clear()
    await call.message.edit_text(await admin_panel_text(session), reply_markup=admin_menu_kb(), parse_mode="HTML")
    await call.answer()


# ─── Stats / users ────────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:stats")
async def admin_stats(call: CallbackQuery, session: AsyncSession):
    if not is_admin(call.from_user.id):
        return await call.answer()
    total_users = (await session.execute(select(func.count(User.id)))).scalar() or 0
    total_balance = (await session.execute(select(func.coalesce(func.sum(User.balance), 0)))).scalar() or 0
    total_orders = (await session.execute(select(func.count(Activation.id)))).scalar() or 0

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Пользователей: <b>{total_users}</b>\n"
        f"💰 Сумма балансов: <b>{round(total_balance, 2)} руб.</b>\n"
        f"📦 Заказов: <b>{total_orders}</b>"
    )
    await call.message.edit_text(text, reply_markup=back_to_menu_kb(), parse_mode="HTML")
    await call.answer()


@router.callback_query(F.data.startswith("admin:users:"))
async def admin_users(call: CallbackQuery, session: AsyncSession):
    if not is_admin(call.from_user.id):
        return await call.answer()
    try:
        page = int(call.data.split(":")[2])
    except (IndexError, ValueError):
        page = 1
    users = await get_all_users(session)
    total = len(users)
    total_pages = max(1, (total + ADMIN_PAGE_SIZE - 1) // ADMIN_PAGE_SIZE)
    page = max(1, min(page, total_pages))
    chunk = users[(page - 1) * ADMIN_PAGE_SIZE: page * ADMIN_PAGE_SIZE]

    lines = [f"👥 <b>Пользователи</b> (всего {total}) — стр. {page}/{total_pages}\n"]
    for u in chunk:
        lines.append(
            f"• <code>{u.id}</code> @{html.escape(u.username or '—')} | "
            f"💰{u.balance} | {html.escape(u.language or '')} | {html.escape(u.status or '')}"
        )

    builder = InlineKeyboardBuilder()
    if page > 1:
        builder.button(text="◀️", callback_data=f"admin:users:{page-1}")
    builder.button(text=f"{page}/{total_pages}", callback_data="noop")
    if page < total_pages:
        builder.button(text="▶️", callback_data=f"admin:users:{page+1}")
    builder.button(text="🔙 Назад", callback_data="admin:menu")
    builder.adjust(3, 1)

    await call.message.edit_text("\n".join(lines), reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()


async def user_info_text(session: AsyncSession, user: User) -> str:
    invited = await count_referrals(session, user.id)
    return (
        f"👤 <b>Пользователь</b>\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"Username: @{html.escape(user.username or '—')}\n"
        f"Имя: {html.escape(user.full_name or '—')}\n"
        f"Язык: {html.escape(user.language or '')}\n"
        f"Баланс: {user.balance} руб.\n"
        f"Статус: {html.escape(user.status or '')}\n"
        f"Страна: {html.escape(user.selected_country_name or '—')}\n"
        f"Куплено: {user.total_bought} (активировано {user.total_activated})\n"
        f"Аренд: {user.total_rented}\n"
        f"Пригласил: {user.referral_id or '—'} | Приглашено: {invited}\n"
        f"Регистрация: {str(user.created_at)[:16]}\n"
        f"Доступ: {'🚫 заблокирован' if user.is_banned else '✅ открыт'}"
    )


def user_card_kb(user: User) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if user.is_banned:
        builder.button(text="✅ Разблокировать", callback_data=f"admin:unban:{user.id}")
    else:
        builder.button(text="🚫 Заблокировать", callback_data=f"admin:ban:{user.id}")
    builder.button(text="🔙 Назад", callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("admin:ban:") | F.data.startswith("admin:unban:"))
async def admin_ban_toggle(call: CallbackQuery, session: AsyncSession):
    if not is_admin(call.from_user.id):
        return await call.answer()
    ban = call.data.startswith("admin:ban:")
    uid_text = call.data.rsplit(":", 1)[1]
    user = await get_user(session, int(uid_text)) if uid_text.isdigit() else None
    if user is None:
        return await call.answer("Пользователь не найден", show_alert=True)
    if ban and is_admin(user.id):
        return await call.answer("Нельзя заблокировать администратора", show_alert=True)
    await update_user(session, user.id, is_banned=ban)
    user.is_banned = ban
    logger.info(f"Admin {call.from_user.id} {'banned' if ban else 'unbanned'} user {user.id}")
    await call.message.edit_text(await user_info_text(session, user), reply_markup=user_card_kb(user),
                                 parse_mode="HTML")
    await call.answer("🚫 Заблокирован" if ban else "✅ Разблокирован")


# ─── Balance changes (commands and buttons share one implementation) ─────────

def parse_balance_args(text: str) -> tuple[int, float] | str:
    """'<user_id> <amount>' -> (uid, amount) or an error message."""
    args = (text or "").split()
    if len(args) != 2:
        return "Формат: <user_id> <сумма>"
    try:
        uid, amount = int(args[0]), float(args[1].replace(",", "."))
    except ValueError:
        return "❌ Неверные данные"
    if not math.isfinite(amount) or amount <= 0:
        return "❌ Сумма должна быть положительным числом"
    if amount > MAX_ADMIN_AMOUNT:
        return f"❌ Слишком большая сумма (макс. {MAX_ADMIN_AMOUNT})"
    return uid, round(amount, 2)


async def change_balance(session: AsyncSession, text: str, add: bool) -> str:
    parsed = parse_balance_args(text)
    if isinstance(parsed, str):
        return parsed
    uid, amount = parsed
    if await get_user(session, uid) is None:
        return "❌ Пользователь не найден"
    ok = await add_balance(session, uid, amount) if add else await deduct_balance(session, uid, amount)
    if not ok:
        return "❌ Недостаточно средств или пользователь не найден"
    user = await session.get(User, uid, populate_existing=True)
    action = f"Начислено {amount} руб. пользователю" if add else f"Списано {amount} руб. у пользователя"
    return f"✅ {action} {uid}. Новый баланс: {round(user.balance, 2)}"


@router.message(Command("addbal"))
async def cmd_addbal(message: Message, session: AsyncSession):
    """Usage: /addbal <user_id> <amount>"""
    if not is_admin(message.from_user.id):
        return
    args = " ".join(message.text.split()[1:])
    reply = await change_balance(session, args, add=True)
    await message.answer(reply.replace("Формат: ", "Формат: /addbal "))


@router.message(Command("rembal"))
async def cmd_rembal(message: Message, session: AsyncSession):
    """Usage: /rembal <user_id> <amount>"""
    if not is_admin(message.from_user.id):
        return
    args = " ".join(message.text.split()[1:])
    reply = await change_balance(session, args, add=False)
    await message.answer(reply.replace("Формат: ", "Формат: /rembal "))


@router.message(Command("userinfo"))
async def cmd_userinfo(message: Message, session: AsyncSession):
    """Формат: /userinfo <user_id>"""
    if not is_admin(message.from_user.id):
        return
    args = message.text.split()[1:]
    if not args:
        await message.answer("Формат: /userinfo <user_id>")
        return
    if not args[0].isdigit():
        await message.answer("❌ Неверный ID")
        return
    user = await get_user(session, int(args[0]))
    if not user:
        await message.answer("❌ Пользователь не найден")
        return
    await message.answer(await user_info_text(session, user), parse_mode="HTML", reply_markup=user_card_kb(user))


_PROMPTS = {
    "admin:add_bal": (AdminAction.add_balance, "💰 Отправьте: <code>user_id сумма</code>\nПример: <code>123456789 500</code>"),
    "admin:rem_bal": (AdminAction.remove_balance, "➖ Отправьте: <code>user_id сумма</code>\nПример: <code>123456789 100</code>"),
    "admin:find": (AdminAction.find_user, "🔍 Отправьте Telegram ID или @username пользователя."),
    "admin:broadcast": (AdminAction.broadcast,
                        "📨 Отправьте сообщение для рассылки (текст, фото, видео...).\n"
                        "Перед отправкой будет предпросмотр и подтверждение."),
}


@router.callback_query(F.data.in_(set(_PROMPTS)))
async def admin_prompt(call: CallbackQuery, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    target_state, prompt = _PROMPTS[call.data]
    await state.set_state(target_state)
    builder = InlineKeyboardBuilder()
    builder.button(text="✖️ Отмена", callback_data="admin:menu")
    await call.message.edit_text(prompt, reply_markup=builder.as_markup(), parse_mode="HTML")
    await call.answer()


@router.message(AdminAction.add_balance)
@router.message(AdminAction.remove_balance)
async def admin_balance_input(message: Message, session: AsyncSession, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("✖️ Отменено.")
        return
    add = await state.get_state() == AdminAction.add_balance.state
    reply = await change_balance(session, message.text or "", add=add)
    if reply.startswith("✅"):
        await state.clear()
        await message.answer(reply, reply_markup=back_to_menu_kb())
    else:
        await message.answer(reply + "\nПопробуйте ещё раз или нажмите «Отмена».")


@router.message(AdminAction.find_user)
async def admin_find_input(message: Message, session: AsyncSession, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    text = (message.text or "").strip()
    if text.startswith("/"):
        await state.clear()
        await message.answer("✖️ Отменено.")
        return
    text = text.lstrip("@")
    user = None
    if text.isdigit():
        user = await get_user(session, int(text))
    elif text:
        res = await session.execute(select(User).where(func.lower(User.username) == text.lower()))
        user = res.scalars().first()
    if user is None:
        await message.answer("❌ Пользователь не найден. Отправьте Telegram ID или @username.")
        return
    await state.clear()
    await message.answer(await user_info_text(session, user), parse_mode="HTML", reply_markup=user_card_kb(user))


# ─── Recent purchases ─────────────────────────────────────────────────────────

@router.callback_query(F.data == "admin:recent")
async def admin_recent(call: CallbackQuery, session: AsyncSession):
    if not is_admin(call.from_user.id):
        return await call.answer()
    rows = await get_recent_purchases(session, limit=15)
    lines = ["🧾 <b>Последние покупки</b>\n"]
    for r in rows:
        when = str(r.created_at)[5:16]
        if isinstance(r, Activation):
            what = f"📱 {html.escape(r.service_name or '')} {html.escape(r.phone_number or '')}"
        elif isinstance(r, Rental):
            what = f"🏠 аренда {html.escape(r.service_name or '')} {html.escape(r.phone_number or '')}"
        elif isinstance(r, EmailOrder):
            what = f"✉️ {html.escape(r.email or '')}"
        else:
            continue
        lines.append(f"{when} • <code>{r.user_id}</code> • {what} • {r.price} руб. • {r.status}")
    if len(lines) == 1:
        lines.append("Покупок пока нет.")
    await call.message.edit_text("\n".join(lines)[:4096], reply_markup=back_to_menu_kb(), parse_mode="HTML")
    await call.answer()


# ─── Broadcast ────────────────────────────────────────────────────────────────

@router.message(AdminAction.broadcast)
async def admin_broadcast_input(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("✖️ Отменено.")
        return
    await state.update_data(from_chat=message.chat.id, message_id=message.message_id)
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить всем", callback_data="admin:bc:send")
    builder.button(text="✖️ Отмена", callback_data="admin:menu")
    builder.adjust(1)
    await message.answer("👆 Так сообщение увидят пользователи. Отправить?", reply_markup=builder.as_markup())


@router.callback_query(F.data == "admin:bc:send")
async def admin_broadcast_send(call: CallbackQuery, session: AsyncSession, state: FSMContext):
    if not is_admin(call.from_user.id):
        return await call.answer()
    data = await state.get_data()
    await state.clear()
    if not data.get("message_id"):
        await call.answer("Нечего отправлять", show_alert=True)
        return
    user_ids = [u.id for u in await get_all_users(session)]
    await call.message.edit_text(f"📨 Отправляю {len(user_ids)} пользователям...")
    await call.answer()
    task = asyncio.create_task(run_broadcast(call.bot, user_ids, data["from_chat"], data["message_id"],
                                             report_chat=call.message.chat.id))
    _broadcasts.add(task)
    task.add_done_callback(_broadcasts.discard)


async def run_broadcast(bot, user_ids: list[int], from_chat: int, message_id: int, report_chat: int) -> tuple[int, int]:
    """Copy the admin's message to every user. Blocked/deleted users are counted, not fatal."""
    sent = failed = 0
    for uid in user_ids:
        try:
            await bot.copy_message(chat_id=uid, from_chat_id=from_chat, message_id=message_id)
            sent += 1
        except Exception as e:
            failed += 1
            logger.debug(f"Broadcast to {uid} failed: {e!r}")
        await asyncio.sleep(BROADCAST_DELAY)
    try:
        await bot.send_message(report_chat, f"✅ Рассылка завершена: отправлено {sent}, ошибок {failed}.")
    except Exception as e:
        logger.warning(f"Broadcast report failed: {e!r}")
    return sent, failed
