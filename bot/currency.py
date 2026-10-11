"""
Display currency. Money is stored and charged in RUB (GetSMS sells in RUB); users see and
type amounts in USD or EUR. Rates = RUB per 1 unit: owner's manual rate, else the Central
Bank of Russia rate (refreshed hourly, last value persisted), else a built-in fallback.
"""

import asyncio
import math

import aiohttp
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import BotSetting

CURRENCIES = {"USD": "$", "EUR": "€"}
FALLBACK = {"USD": 85.0, "EUR": 95.0}       # only until the first successful fetch
CBR_URL = "https://www.cbr-xml-daily.ru/daily_json.js"
UPDATE_INTERVAL = 3600
MIN_RATE, MAX_RATE = 1.0, 10_000.0

_auto: dict[str, float] = {}
_manual: dict[str, float] = {}
_auto_date = ""


def user_currency(user) -> str:
    cur = getattr(user, "currency", None)
    if cur in CURRENCIES:
        return cur
    return "EUR" if getattr(user, "language", None) == "de" else "USD"


def label(cur: str) -> str:
    return f"{cur} ({CURRENCIES[cur]})"


def rate(cur: str) -> float:
    return _manual.get(cur) or _auto.get(cur) or FALLBACK[cur]


def rate_source(cur: str) -> str:
    return "manual" if cur in _manual else "auto" if cur in _auto else "fallback"


def auto_date() -> str:
    return _auto_date


def to_rub(amount: float, cur: str) -> float:
    return round(amount * rate(cur), 2)


def from_rub(rub: float, cur: str, up: bool = False) -> float:
    value = float(rub or 0) / rate(cur)
    return math.ceil(value * 100 - 1e-6) / 100 if up else round(value, 2)


def fmt(rub, cur: str, up: bool = False) -> str:
    """RUB amount -> '1.25 $'. up=True for prices, so a price is never shown lower than charged."""
    return f"{from_rub(rub, cur, up):.2f} {CURRENCIES[cur]}"


def fmt_amount(amount: float, cur: str) -> str:
    """An amount already in the user's currency."""
    return f"{amount:.2f} {CURRENCIES[cur]}"


# ─── Storage ──────────────────────────────────────────────────────────────────

def _key(kind: str, cur: str) -> str:
    return f"rate_{kind}_{cur}"


async def _get(session: AsyncSession, key: str) -> str | None:
    row = await session.get(BotSetting, key)
    return row.value if row else None


def _valid(value) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and MIN_RATE <= value <= MAX_RATE else None


async def load(session: AsyncSession) -> None:
    global _auto_date
    _auto.clear()
    _manual.clear()
    for cur in CURRENCIES:
        if (v := _valid(await _get(session, _key("auto", cur)))) is not None:
            _auto[cur] = v
        if (v := _valid(await _get(session, _key("manual", cur)))) is not None:
            _manual[cur] = v
    _auto_date = await _get(session, "rate_auto_date") or ""


async def set_manual(session: AsyncSession, cur: str, value: float | None) -> None:
    """value=None returns the currency to the automatic rate."""
    if cur not in CURRENCIES:
        raise ValueError("неизвестная валюта")
    key = _key("manual", cur)
    if value is None:
        if row := await session.get(BotSetting, key):
            await session.delete(row)
        await session.commit()
        _manual.pop(cur, None)
        return
    if _valid(value) is None:
        raise ValueError(f"нужен курс от {MIN_RATE:g} до {MAX_RATE:g} руб.")
    value = round(float(value), 4)
    await session.merge(BotSetting(key=key, value=str(value)))
    await session.commit()
    _manual[cur] = value


def parse_cbr(data: dict) -> tuple[dict[str, float], str]:
    """{"Date": "...", "Valute": {"USD": {"Value": 84.9, "Nominal": 1}, ...}} -> ({cur: rub}, date)."""
    rates = {}
    for cur in CURRENCIES:
        v = data["Valute"][cur]
        rates[cur] = _valid(float(v["Value"]) / float(v["Nominal"]))
    if None in rates.values():
        raise ValueError(f"bad rates {rates}")
    return rates, str(data.get("Date", ""))[:10]


async def fetch_cbr() -> tuple[dict[str, float], str] | None:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as http:
            async with http.get(CBR_URL) as r:
                return parse_cbr(await r.json(content_type=None))
    except Exception as e:
        logger.warning(f"Exchange rates fetch failed: {e!r}")
        return None


async def store_auto(session: AsyncSession, rates: dict[str, float], date: str) -> None:
    global _auto_date
    for cur, v in rates.items():
        await session.merge(BotSetting(key=_key("auto", cur), value=str(round(v, 4))))
    await session.merge(BotSetting(key="rate_auto_date", value=date))
    await session.commit()
    _auto.update({cur: round(v, 4) for cur, v in rates.items()})
    _auto_date = date


async def refresh(session_factory) -> bool:
    got = await fetch_cbr()
    if got is None:
        return False
    async with session_factory() as session:
        await store_auto(session, *got)
    return True


async def updater(session_factory, interval: int = UPDATE_INTERVAL):
    while True:
        if await refresh(session_factory):
            logger.info("Exchange rates: " + ", ".join(f"{c}={rate(c):g}" for c in CURRENCIES))
        await asyncio.sleep(interval)
