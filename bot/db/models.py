from datetime import datetime, timezone
from sqlalchemy import (
    BigInteger, Boolean, Column, DateTime,
    Float, Integer, String, Text, UniqueConstraint, func
)
from sqlalchemy.orm import DeclarativeBase


def utcnow() -> datetime:
    """Naive UTC 'now' - same values as the deprecated datetime.utcnow(), so stored data is unchanged."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id = Column(BigInteger, primary_key=True)          # Telegram user_id
    username = Column(String(64), nullable=True)
    full_name = Column(String(128), nullable=True)
    language = Column(String(8), default="ru")
    currency = Column(String(3), nullable=True)          # display currency (USD/EUR); None = by language
    balance = Column(Float, default=0.0)                 # always RUB internally
    cashback = Column(Float, default=0.0)
    selected_country_id = Column(Integer, nullable=True)
    selected_country_name = Column(String(64), nullable=True)
    selected_operator = Column(String(32), default="any")
    status = Column(String(32), default="Новичок")
    total_bought = Column(Integer, default=0)
    total_rented = Column(Integer, default=0)
    total_activated = Column(Integer, default=0)
    total_rented_activated = Column(Integer, default=0)
    tos_accepted = Column(Boolean, default=False)
    is_banned = Column(Boolean, default=False)
    api_key = Column(String(64), nullable=True)
    referral_id = Column(BigInteger, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    last_seen = Column(DateTime, default=utcnow, onupdate=utcnow)


class Activation(Base):
    __tablename__ = "activations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    order_id = Column(Integer, nullable=False)           # GetSMS order id
    service_id = Column(Integer, nullable=False)
    service_name = Column(String(64), nullable=False)
    phone_number = Column(String(32), nullable=False)
    price = Column(Float, nullable=False)
    status = Column(String(32), default="WAITING")      # WAITING/RECEIVED/FINISHED/CANCELLED
    sms_code = Column(String(32), nullable=True)
    sms_text = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)
    finished_at = Column(DateTime, nullable=True)


class Rental(Base):
    __tablename__ = "rentals"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    order_id = Column(Integer, nullable=False)
    service_id = Column(Integer, nullable=False)
    service_name = Column(String(64), nullable=False)
    phone_number = Column(String(32), nullable=False)
    price = Column(Float, nullable=False)
    status = Column(String(32), default="WAITING")
    expire_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Favorite(Base):
    """A service the user starred (per country - service ids are country-specific)."""
    __tablename__ = "favorites"
    __table_args__ = (UniqueConstraint("user_id", "service_id", name="uq_favorite_user_service"),)

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    service_id = Column(Integer, nullable=False)
    service_name = Column(String(64), nullable=False)
    country_id = Column(Integer, nullable=False)
    country_name = Column(String(64), nullable=False)
    created_at = Column(DateTime, default=utcnow)


class EmailOrder(Base):
    """E-mail activation bought by a user (GetSMS /emails)."""
    __tablename__ = "email_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    email_id = Column(String(64), nullable=False, index=True)   # GetSMS order id
    email = Column(String(128), nullable=False)
    site = Column(String(64), nullable=False)
    domain = Column(String(64), nullable=False)
    price = Column(Float, nullable=False)                       # last amount charged (buy / reorder)
    status = Column(String(32), default="WAIT")                 # WAIT / RECEIVED / CANCELLED
    code = Column(String(64), nullable=True)
    created_at = Column(DateTime, default=utcnow)


class Payment(Base):
    __tablename__ = "payments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(BigInteger, nullable=False, index=True)
    payment_id = Column(String(64), nullable=True)       # NowPayments payment_id
    amount_usd = Column(Float, nullable=False)
    amount_rub = Column(Float, nullable=False)
    currency = Column(String(16), nullable=True)
    status = Column(String(32), default="waiting")       # waiting/confirming/confirmed/failed
    created_at = Column(DateTime, default=utcnow)
    confirmed_at = Column(DateTime, nullable=True)


class BannerCache(Base):
    __tablename__ = "banner_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(String(256), nullable=False)
    updated_at = Column(DateTime, default=utcnow)


class BotSetting(Base):
    __tablename__ = "bot_settings"

    key = Column(String(64), primary_key=True)
    value = Column(String(256), nullable=False)


class CustomEmoji(Base):
    __tablename__ = "custom_emoji"

    emoji = Column(String(32), primary_key=True)          # plain emoji without U+FE0F
    emoji_id = Column(String(32), nullable=False)


class PaymentSetting(Base):
    """Per-method payment config, edited by the owner via /admin."""
    __tablename__ = "payment_settings"

    method = Column(String(32), primary_key=True)        # code from bot.payments.METHODS
    enabled = Column(Boolean, default=False)
    title = Column(String(64), nullable=True)            # button label override
    min_amount = Column(Float, nullable=True)            # RUB
    credentials = Column(Text, nullable=True)            # Fernet-encrypted JSON {field: value}
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)
