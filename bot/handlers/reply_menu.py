"""Reply keyboard under the input field: Buy number / Top up / Profile / Menu."""

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.queries import get_or_create_user
from bot.handlers import activation, profile, start, topup
from bot.keyboards import lang_keyboard, reply_menu_actions
from bot.utils.banner import cache_banner_file_id, get_banner

router = Router()
ACTIONS = reply_menu_actions()


async def send_screen(message: Message, session: AsyncSession, text: str, kb: InlineKeyboardMarkup):
    """New banner message, so the inline navigation of the screen (edit_caption) keeps working."""
    banner = await get_banner(session)
    msg = await message.answer_animation(animation=banner, caption=text, reply_markup=kb, parse_mode="HTML")
    if msg.animation and not isinstance(banner, str):
        await cache_banner_file_id(session, msg.animation.file_id)


@router.message(F.text.in_(ACTIONS))
async def reply_menu(message: Message, session: AsyncSession, state: FSMContext):
    await open_section(message, session, state, ACTIONS[message.text])


@router.message(Command("buy"))
async def cmd_buy(message: Message, session: AsyncSession, state: FSMContext):
    await open_section(message, session, state, "btn_buy_number")


async def open_section(message: Message, session: AsyncSession, state: FSMContext, action: str):
    await state.clear()             # a reply button always wins over a pending text input
    user = await get_or_create_user(session, message.from_user.id, username=message.from_user.username,
                                    full_name=message.from_user.full_name)
    if not user.tos_accepted:
        await message.answer(start.LANGUAGE_PROMPT, reply_markup=lang_keyboard())
        return

    if action == "btn_menu":
        await start.show_main_menu(message, session, user)
        return
    if action == "btn_profile":
        text, kb = profile.profile_screen(user)
    elif action == "btn_topup":
        text, kb = await topup.topup_screen(session, user.language)
    elif user.selected_country_id:
        text, kb = await activation.services_screen(user, page=1)
    else:
        text, kb = await activation.countries_screen(user, page=1)
    await send_screen(message, session, text, kb)
