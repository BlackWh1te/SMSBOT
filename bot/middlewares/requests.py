"""Outgoing-request fixes shared by every screen."""

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageCaption, EditMessageText


class CaptionFallbackMiddleware(BaseRequestMiddleware):
    """
    Screens edit the banner's caption. When the screen sits on a plain text message
    (search results, /fill, ...), Telegram refuses with "no caption" - edit the text instead.
    Register it before LiveEmojiMiddleware so the retried text still gets live emoji.
    """

    async def __call__(self, make_request, bot, method):
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as e:
            if not isinstance(method, EditMessageCaption) or not method.caption \
                    or "no caption" not in str(e).lower():
                raise
            return await make_request(bot, EditMessageText(
                chat_id=method.chat_id, message_id=method.message_id,
                inline_message_id=method.inline_message_id, text=method.caption,
                parse_mode=method.parse_mode, reply_markup=method.reply_markup))
