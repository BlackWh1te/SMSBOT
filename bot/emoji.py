"""Live (custom) emoji: plain emoji in outgoing texts and button labels are swapped for premium ones."""

import json
import re
from pathlib import Path

from aiogram.client.default import Default
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, Message
from loguru import logger
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BotSetting, CustomEmoji

VS16 = "️"
_map: dict[str, str] = {}
_pattern: re.Pattern | None = None
_TAG = re.compile(r"(<[^>]*>)")
_SKIP_TAGS = {"code", "pre", "tg-emoji", "a"}


def normalize(emoji: str) -> str:
    return emoji.replace(VS16, "")


def set_mapping(mapping: dict[str, str]) -> None:
    global _map, _pattern
    _map = {normalize(k): v for k, v in mapping.items() if normalize(k)}
    if not _map:
        _pattern = None
        return
    parts = [f"{VS16}?".join(re.escape(c) for c in key) + f"{VS16}?"
             for key in sorted(_map, key=len, reverse=True)]
    _pattern = re.compile("|".join(parts))


def mapping() -> dict[str, str]:
    return dict(_map)


def _sub(match: re.Match) -> str:
    return f'<tg-emoji emoji-id="{_map[normalize(match.group(0))]}">{match.group(0)}</tg-emoji>'


def _tag_name(tag: str) -> str:
    inner = tag[1:-1].strip().lstrip("/").split()
    return inner[0].lower() if inner else ""


def apply_html(text: str) -> str:
    """Wrap mapped emoji in <tg-emoji>, leaving tags, code blocks and links untouched."""
    if not _pattern or not text:
        return text
    out, skip = [], 0
    for part in _TAG.split(text):
        if part.startswith("<") and part.endswith(">"):
            if _tag_name(part) in _SKIP_TAGS:
                skip = max(skip + (-1 if part.startswith("</") else 1), 0)
            out.append(part)
        else:
            out.append(part if skip else _pattern.sub(_sub, part))
    return "".join(out)


def apply_button(btn: InlineKeyboardButton) -> InlineKeyboardButton:
    """'✅ Оплатить' -> icon=<live ✅>, text='Оплатить'."""
    if not _pattern or getattr(btn, "icon_custom_emoji_id", None):
        return btn
    m = _pattern.match(btn.text)
    rest = btn.text[m.end():].lstrip() if m else ""
    if not rest:
        return btn
    data = btn.model_dump(exclude_none=True)
    data.update(text=rest, icon_custom_emoji_id=_map[normalize(m.group(0))])
    return InlineKeyboardButton(**data)


def apply_markup(kb: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[apply_button(b) for b in row] for row in kb.inline_keyboard])


def extract(message: Message) -> dict[str, str]:
    """Custom emoji in a (forwarded) message -> {plain emoji: custom_emoji_id}."""
    found = {}
    for text, entities in ((message.text, message.entities), (message.caption, message.caption_entities)):
        for ent in entities or []:
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                key = normalize(ent.extract_from(text)).strip()
                if key:
                    found[key] = ent.custom_emoji_id
    return found


# ─── Storage ──────────────────────────────────────────────────────────────────

DEFAULTS_FILE = Path(__file__).with_name("emoji_defaults.json")
SEEDED_KEY = "emoji_defaults_seeded"


async def seed_defaults(session: AsyncSession) -> int:
    """First start only: store the bundled emoji set. Later admin edits (incl. "delete all") are kept."""
    if await session.get(BotSetting, SEEDED_KEY) or not DEFAULTS_FILE.exists():
        return 0
    defaults = json.loads(DEFAULTS_FILE.read_text(encoding="utf-8"))
    for e, eid in defaults.items():
        await session.merge(CustomEmoji(emoji=normalize(e), emoji_id=str(eid)))
    session.add(BotSetting(key=SEEDED_KEY, value="1"))
    await session.commit()
    return len(defaults)


async def load(session: AsyncSession) -> None:
    rows = (await session.execute(select(CustomEmoji))).scalars()
    set_mapping({r.emoji: r.emoji_id for r in rows})


async def save(session: AsyncSession, found: dict[str, str]) -> None:
    for emoji, eid in found.items():
        await session.merge(CustomEmoji(emoji=emoji, emoji_id=eid))
    await session.commit()
    await load(session)


async def remove(session: AsyncSession, emoji: str | None = None) -> None:
    stmt = delete(CustomEmoji)
    if emoji is not None:
        stmt = stmt.where(CustomEmoji.emoji == normalize(emoji))
    await session.execute(stmt)
    await session.commit()
    await load(session)


# ─── Outgoing requests ────────────────────────────────────────────────────────

def transform(bot, method):
    """Return a copy of the API call with live emoji, or None if nothing changes."""
    if not _pattern:
        return None
    update = {}
    parse_mode = getattr(method, "parse_mode", None)
    if isinstance(parse_mode, Default):
        parse_mode = bot.default[parse_mode.name] if bot.default else None
    if str(parse_mode).lower().endswith("html"):
        for field in ("text", "caption"):
            value = getattr(method, field, None)
            if isinstance(value, str):
                new = apply_html(value)
                if new != value:
                    update[field] = new
    kb = getattr(method, "reply_markup", None)
    if isinstance(kb, InlineKeyboardMarkup):
        new_kb = apply_markup(kb)
        if new_kb != kb:
            update["reply_markup"] = new_kb
    return method.model_copy(update=update) if update else None


# Errors that have nothing to do with live emoji - resending without them would fail the same way
NOT_EMOJI_ERRORS = ("not modified", "no caption", "message to edit not found", "can't be edited",
                    "query is too old")


class LiveEmojiMiddleware(BaseRequestMiddleware):
    """If Telegram rejects the live emoji (e.g. owner has no Premium), the original is resent."""

    async def __call__(self, make_request, bot, method):
        changed = transform(bot, method)
        if changed is None:
            return await make_request(bot, method)
        try:
            return await make_request(bot, changed)
        except TelegramBadRequest as e:
            if any(s in str(e).lower() for s in NOT_EMOJI_ERRORS):
                raise
            logger.warning(f"Live emoji rejected ({e.message}); sending plain emoji")
            return await make_request(bot, method)
