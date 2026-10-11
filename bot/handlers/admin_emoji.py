"""/admin -> 🎨 Живые эмодзи: forward messages with premium emoji, the bot remembers their IDs."""

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy.ext.asyncio import AsyncSession

from bot import emoji
from bot.handlers import admin

router = Router()


class AdminEmoji(StatesGroup):
    collect = State()
    remove = State()


def screen_text() -> str:
    current = emoji.mapping()
    lines = [
        "🎨 <b>Живые эмодзи</b>",
        "",
        "Обычные эмодзи в текстах и на кнопках бота автоматически заменяются на живые.",
        "Работает, если у владельца бота (в @BotFather) есть Telegram Premium; иначе бот "
        "сам покажет обычные эмодзи.",
        "",
        f"Сохранено: <b>{len(current)}</b>",
    ]
    if current:
        lines.append(" ".join(current))
    return "\n".join(lines)


def screen_kb() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить (переслать сообщения)", callback_data="admin:emoji:add")
    if emoji.mapping():
        builder.button(text="➖ Удалить один", callback_data="admin:emoji:del")
        builder.button(text="🗑 Удалить все", callback_data="admin:emoji:clr")
    builder.button(text="🔙 Назад", callback_data="admin:menu")
    builder.adjust(1)
    return builder.as_markup()


def done_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="✅ Готово", callback_data="admin:emoji")]])


async def _show(call: CallbackQuery, text: str, kb: InlineKeyboardMarkup):
    try:
        await call.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except TelegramBadRequest as e:
        if "message is not modified" not in str(e):
            raise


@router.callback_query(F.data == "admin:emoji")
async def emoji_screen(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.clear()
    await _show(call, screen_text(), screen_kb())
    await call.answer()


@router.callback_query(F.data == "admin:emoji:add")
async def emoji_add(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminEmoji.collect)
    await _show(call, (
        "➕ Перешлите сюда сообщения с живыми эмодзи (например, из оригинального бота) "
        "или отправьте их сами с Premium-аккаунта.\n\n"
        "Можно несколько сообщений подряд. Когда закончите — нажмите «Готово»."
    ), done_kb())
    await call.answer()


@router.message(AdminEmoji.collect)
async def emoji_collect(message: Message, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(message.from_user.id):
        return
    if (message.text or "").startswith("/"):
        await state.clear()
        await message.answer("✖️ Готово.")
        return
    found = emoji.extract(message)
    if not found:
        await message.answer("В этом сообщении нет живых эмодзи. Перешлите другое или нажмите «Готово».",
                             reply_markup=done_kb())
        return
    await emoji.save(session, found)
    await message.answer(f"✅ Сохранено {len(found)}: {' '.join(found)}\nВсего: {len(emoji.mapping())}",
                         reply_markup=done_kb())


@router.callback_query(F.data == "admin:emoji:del")
async def emoji_del_prompt(call: CallbackQuery, state: FSMContext):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await state.set_state(AdminEmoji.remove)
    await _show(call, "➖ Отправьте эмодзи, который больше не нужно заменять.", done_kb())
    await call.answer()


@router.message(AdminEmoji.remove)
async def emoji_del(message: Message, session: AsyncSession, state: FSMContext):
    if not admin.is_admin(message.from_user.id):
        return
    await state.clear()
    key = emoji.normalize((message.text or "").strip())
    if key not in emoji.mapping():
        await message.answer("Такого эмодзи нет в списке.", reply_markup=screen_kb())
        return
    await emoji.remove(session, key)
    await message.answer(f"Удалено.\n\n{screen_text()}", reply_markup=screen_kb(), parse_mode="HTML")


@router.callback_query(F.data == "admin:emoji:clr")
async def emoji_clear_confirm(call: CallbackQuery):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    builder = InlineKeyboardBuilder()
    builder.button(text="🗑 Да, удалить все", callback_data="admin:emoji:clr!")
    builder.button(text="✖️ Отмена", callback_data="admin:emoji")
    builder.adjust(1)
    await _show(call, "Удалить все живые эмодзи? Бот вернётся к обычным.", builder.as_markup())
    await call.answer()


@router.callback_query(F.data == "admin:emoji:clr!")
async def emoji_clear(call: CallbackQuery, session: AsyncSession):
    if not admin.is_admin(call.from_user.id):
        return await call.answer()
    await emoji.remove(session)
    await _show(call, screen_text(), screen_kb())
    await call.answer("Удалено")
