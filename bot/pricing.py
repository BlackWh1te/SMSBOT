"""Owner's markup on numbers: GetSMS cost × multiplier = price the user sees and pays."""

import math

from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BotSetting

KEY = "markup_numbers"
MIN_MARKUP, MAX_MARKUP = 1.0, 10.0
PRESETS = (1.0, 1.5, 2.0, 3.0)

_markup = 1.0


def markup() -> float:
    return _markup


def sell(cost) -> float | None:
    """Provider cost -> user price; None if the cost is missing or invalid."""
    try:
        cost = float(cost)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(cost) or cost <= 0:
        return None
    return round(cost * _markup, 2)


def parse_number(text: str) -> float:
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError:
        raise ValueError("нужно число, например 1.5")


def validate(value: float) -> float:
    if not math.isfinite(value) or not MIN_MARKUP <= value <= MAX_MARKUP:
        raise ValueError(f"нужно число от {MIN_MARKUP:g} до {MAX_MARKUP:g}")
    return round(value, 2)


async def load(session: AsyncSession) -> None:
    global _markup
    row = await session.get(BotSetting, KEY)
    try:
        _markup = validate(float(row.value)) if row else 1.0
    except ValueError:
        _markup = 1.0


async def set_markup(session: AsyncSession, value: float) -> None:
    global _markup
    value = validate(value)
    await session.merge(BotSetting(key=KEY, value=str(value)))
    await session.commit()
    _markup = value
