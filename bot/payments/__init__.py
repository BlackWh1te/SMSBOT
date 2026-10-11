"""
Payment methods: registry + owner-managed settings.

The owner configures everything from Telegram (/admin -> Payment methods):
API keys, on/off, button title, minimum amount. Nothing goes into .env.
Keys are stored Fernet-encrypted in the `payment_settings` table.

A method is shown to users only when it is *active* = enabled AND every
required key is filled in.
"""

import base64
import hashlib
import json
import math
from dataclasses import dataclass, field

from cryptography.fernet import Fernet, InvalidToken
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot import config
from bot.db.models import PaymentSetting


# ─── Registry ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Field:
    key: str
    label: str
    secret: bool = True
    required: bool = True
    number: bool = False        # value must be a positive number


@dataclass(frozen=True)
class MethodSpec:
    code: str
    titles: dict                # default button title per language
    fields: tuple = ()
    hint: str = ""              # shown to the owner in the admin card

    def default_title(self, lang: str = "ru") -> str:
        return self.titles.get(lang) or self.titles["ru"]


_AGGREGATOR_FIELDS = (
    Field("shop_id", "ID магазина", secret=False),
    Field("api_key", "API-ключ"),
    Field("secret_key", "Секретный ключ", required=False),
)

METHODS: dict[str, MethodSpec] = {m.code: m for m in [
    MethodSpec("binance_pay", {"ru": "Binance Pay"},
               (Field("api_key", "API-ключ"), Field("secret_key", "Секретный ключ")),
               "Binance Merchant → API Management"),
    MethodSpec("sbp", {"ru": "СБП", "en": "SBP (Russian fast payments)", "de": "SBP (Russland)"},
               _AGGREGATOR_FIELDS, "Платёжный агрегатор с приёмом СБП"),
    MethodSpec("card_ru", {"ru": "Карта РФ", "en": "Russian bank card", "de": "Russische Bankkarte"},
               _AGGREGATOR_FIELDS, "Платёжный агрегатор с приёмом карт РФ"),
    MethodSpec("cryptobot", {"ru": "CryptoBot"},
               (Field("api_token", "Токен Crypto Pay API"),),
               "@CryptoBot → Crypto Pay → Create App → API Token. Счёт выставляется в рублях, пользователь "
               "платит USDT/TON/BTC и др. прямо в Telegram. Минимум — сколько разрешит CryptoBot (копейки)."),
    MethodSpec("xrocket", {"ru": "XRocket"},
               (Field("api_token", "API-ключ Rocket Pay"),),
               "@xRocket → Rocket Pay → Create App → API key. Счёт в USDT по курсу из «💱 Курсы валют», "
               "минимум 0.0001 USDT."),
    MethodSpec("nowpayments", {"ru": "Крипта (много монет)", "en": "Crypto (many coins)", "de": "Krypto (viele Coins)"},
               (Field("api_key", "API-ключ"),
                Field("ipn_secret", "IPN-секрет", required=False)),
               "nowpayments.io → Store Settings → API keys. Монеты для оплаты = монеты, включённые в "
               "NOWPayments «Coins settings». Счёт выставляется в USD по курсу из «💱 Курсы валют». "
               "Оплата проверяется каждые 30 с, вебхук не нужен."),
    MethodSpec("stars", {"ru": "Telegram Stars", "en": "Telegram Stars", "de": "Telegram Stars"},
               (Field("rate", "Рублей за 1 звезду", secret=False, number=True),),
               "Ключи не нужны — только курс"),
]}


# ─── Encryption ───────────────────────────────────────────────────────────────

def _fernet() -> Fernet:
    secret = config.SETTINGS_SECRET or config.BOT_TOKEN
    digest = hashlib.sha256(b"martov-payment-settings:" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_credentials(creds: dict) -> str:
    return _fernet().encrypt(json.dumps(creds).encode()).decode()


def decrypt_credentials(token: str | None) -> dict:
    """Raises InvalidToken if the encryption key changed."""
    if not token:
        return {}
    return json.loads(_fernet().decrypt(token.encode()))


def mask(value: str) -> str:
    if not value:
        return "—"
    if len(value) <= 8:
        return "••••"
    return f"{value[:3]}••••{value[-4:]}"


# ─── State ────────────────────────────────────────────────────────────────────

@dataclass
class MethodState:
    spec: MethodSpec
    enabled: bool = False
    title: str | None = None
    min_amount: float | None = None
    creds: dict = field(default_factory=dict)
    keys_unreadable: bool = False   # stored keys can't be decrypted (encryption key changed)

    @property
    def code(self) -> str:
        return self.spec.code

    def display_title(self, lang: str = "ru") -> str:
        return self.title or self.spec.default_title(lang)

    @property
    def missing_fields(self) -> list[Field]:
        return [f for f in self.spec.fields if f.required and not self.creds.get(f.key)]

    @property
    def is_configured(self) -> bool:
        return not self.keys_unreadable and not self.missing_fields

    @property
    def is_active(self) -> bool:
        return self.enabled and self.is_configured


def _state_from_row(spec: MethodSpec, row: PaymentSetting | None) -> MethodState:
    if row is None:
        return MethodState(spec)
    state = MethodState(spec, enabled=bool(row.enabled), title=row.title, min_amount=row.min_amount)
    try:
        state.creds = decrypt_credentials(row.credentials)
    except (InvalidToken, ValueError):
        logger.warning(f"Payment method {spec.code}: stored keys can't be decrypted, re-enter them")
        state.keys_unreadable = True
    return state


async def _get_row(session: AsyncSession, code: str, create: bool = False) -> PaymentSetting | None:
    if code not in METHODS:
        raise KeyError(f"unknown payment method: {code}")
    row = await session.get(PaymentSetting, code)
    if row is None and create:
        row = PaymentSetting(method=code, enabled=False)
        session.add(row)
    return row


async def get_state(session: AsyncSession, code: str) -> MethodState:
    return _state_from_row(METHODS[code], await _get_row(session, code))


async def all_states(session: AsyncSession) -> list[MethodState]:
    rows = {r.method: r for r in (await session.execute(select(PaymentSetting))).scalars()}
    return [_state_from_row(spec, rows.get(code)) for code, spec in METHODS.items()]


async def active_methods(session: AsyncSession) -> list[MethodState]:
    return [s for s in await all_states(session) if s.is_active]


# ─── Mutations (owner only - callers must check admin rights) ─────────────────

def parse_number(text: str) -> float:
    """'1,5' / '1.5' -> 1.5; anything that is not a finite number -> ValueError (shown to the owner)."""
    try:
        num = float(str(text).strip().replace(",", "."))
    except ValueError:
        raise ValueError("нужно число")
    if not math.isfinite(num):
        raise ValueError("нужно число")
    return num


def validate_field_value(spec: MethodSpec, key: str, value: str) -> str:
    """Return the cleaned value or raise ValueError with a human-readable reason."""
    fld = next((f for f in spec.fields if f.key == key), None)
    if fld is None:
        raise ValueError(f"неизвестное поле {key}")
    value = value.strip()
    if not value:
        raise ValueError("пустое значение")
    if len(value) > 512:
        raise ValueError("слишком длинное значение")
    if fld.number:
        num = parse_number(value)
        if num <= 0:
            raise ValueError("нужно положительное число")
        value = str(num)
    return value


async def set_credential(session: AsyncSession, code: str, key: str, value: str) -> None:
    value = validate_field_value(METHODS[code], key, value)
    row = await _get_row(session, code, create=True)
    try:
        creds = decrypt_credentials(row.credentials)
    except (InvalidToken, ValueError):
        creds = {}   # old keys unreadable - start over
    creds[key] = value
    row.credentials = encrypt_credentials(creds)
    await session.commit()


async def clear_credentials(session: AsyncSession, code: str) -> None:
    row = await _get_row(session, code, create=True)
    row.credentials = None
    row.enabled = False
    await session.commit()


async def set_enabled(session: AsyncSession, code: str, enabled: bool) -> bool:
    """Returns False (and changes nothing) when enabling a method that isn't fully configured."""
    if enabled and not (await get_state(session, code)).is_configured:
        return False
    row = await _get_row(session, code, create=True)
    row.enabled = enabled
    await session.commit()
    return True


async def set_title(session: AsyncSession, code: str, title: str | None) -> None:
    title = (title or "").strip()
    if len(title) > 64:
        raise ValueError("слишком длинное название (макс. 64)")
    row = await _get_row(session, code, create=True)
    row.title = title or None
    await session.commit()


async def set_min_amount(session: AsyncSession, code: str, amount: float | None) -> None:
    if amount is not None and (not math.isfinite(amount) or amount < 0):
        raise ValueError("нужно положительное число")
    row = await _get_row(session, code, create=True)
    row.min_amount = amount or None
    await session.commit()
