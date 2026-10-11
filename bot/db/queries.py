import math
import secrets
from datetime import datetime, timedelta
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from bot.db.models import Activation, BannerCache, EmailOrder, Favorite, Payment, Rental, User, utcnow


# ─── USER ─────────────────────────────────────────────────────────────────────

async def get_or_create_user(session: AsyncSession, user_id: int, **kwargs) -> User:
    result = await session.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(id=user_id, api_key=secrets.token_hex(16), **kwargs)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            # Two first updates from a new user arrived at once and the other one won the insert
            await session.rollback()
            result = await session.execute(select(User).where(User.id == user_id))
            return result.scalar_one()
        await session.refresh(user)
    return user


async def get_user(session: AsyncSession, user_id: int) -> User | None:
    result = await session.execute(select(User).where(User.id == user_id))
    return result.scalar_one_or_none()


async def update_user(session: AsyncSession, user_id: int, **kwargs):
    await session.execute(update(User).where(User.id == user_id).values(**kwargs))
    await session.commit()


def valid_amount(amount) -> bool:
    return isinstance(amount, (int, float)) and math.isfinite(amount) and amount > 0


async def add_balance(session: AsyncSession, user_id: int, amount: float) -> bool:
    """Atomic credit. Returns False for unknown users and non-positive / nan / inf amounts."""
    if not valid_amount(amount):
        return False
    res = await session.execute(
        update(User).where(User.id == user_id)
        .values(balance=func.round(User.balance + amount, 2))
    )
    await session.commit()
    return res.rowcount == 1


async def deduct_balance(session: AsyncSession, user_id: int, amount: float) -> bool:
    """
    Atomic debit: one conditional UPDATE, so two concurrent purchases can never
    both spend the same money. Returns False if the balance is insufficient.
    """
    if not valid_amount(amount):
        return False
    res = await session.execute(
        update(User).where(User.id == user_id, User.balance >= amount)
        .values(balance=func.round(User.balance - amount, 2))
    )
    await session.commit()
    return res.rowcount == 1


async def settle_charge(session: AsyncSession, user_id: int, reserved: float, actual) -> bool:
    """
    After reserving `reserved` and the provider charging `actual`: refund the
    difference, or debit the extra. Returns False only when the user cannot cover
    a higher actual price - the caller must then cancel the order and refund.
    """
    try:
        actual = float(actual)
    except (TypeError, ValueError):
        return True                    # provider didn't report a price: keep the reserved amount
    diff = round(actual - reserved, 2)
    if diff > 0:
        return await deduct_balance(session, user_id, diff)
    if diff < 0:
        await add_balance(session, user_id, -diff)
    return True


async def transfer_balance(session: AsyncSession, from_id: int, to_id: int, amount: float) -> bool:
    """
    Move money between users in ONE transaction: the debit is conditional
    (balance >= amount), and the credit only happens if the debit did.
    """
    if not valid_amount(amount) or from_id == to_id:
        return False
    if await session.get(User, to_id) is None:     # check the recipient before any money moves
        return False
    debit = await session.execute(
        update(User).where(User.id == from_id, User.balance >= amount)
        .values(balance=func.round(User.balance - amount, 2))
    )
    if debit.rowcount != 1:
        await session.commit()               # nothing changed - just end the transaction
        return False
    credit = await session.execute(
        update(User).where(User.id == to_id).values(balance=func.round(User.balance + amount, 2))
    )
    if credit.rowcount != 1:
        await session.rollback()             # recipient vanished - undo the debit
        return False
    await session.commit()
    return True


LAST_SEEN_EVERY_SECONDS = 300


async def touch_last_seen(session: AsyncSession, user_id: int):
    """Update last_seen at most once per 5 minutes per user (one cheap conditional UPDATE)."""
    now = utcnow()
    threshold = now - timedelta(seconds=LAST_SEEN_EVERY_SECONDS)
    await session.execute(
        update(User).where(User.id == user_id, (User.last_seen.is_(None)) | (User.last_seen < threshold))
        .values(last_seen=now)
    )
    await session.commit()


async def increment_user_counter(session: AsyncSession, user_id: int, column: str, by: int = 1):
    col = getattr(User, column)
    await session.execute(update(User).where(User.id == user_id).values({column: col + by}))
    await session.commit()


async def get_all_users(session: AsyncSession) -> list[User]:
    result = await session.execute(select(User))
    return result.scalars().all()


# ─── ACTIVATION ───────────────────────────────────────────────────────────────

async def create_activation(session: AsyncSession, **kwargs) -> Activation:
    act = Activation(**kwargs)
    session.add(act)
    await session.commit()
    await session.refresh(act)
    return act


async def get_activation(session: AsyncSession, order_id: int) -> Activation | None:
    result = await session.execute(select(Activation).where(Activation.order_id == order_id))
    return result.scalar_one_or_none()


async def update_activation(session: AsyncSession, order_id: int, **kwargs):
    if "status" in kwargs and kwargs["status"] in ("FINISHED", "CANCELLED", "RECEIVED"):
        kwargs.setdefault("finished_at", utcnow())
    await session.execute(update(Activation).where(Activation.order_id == order_id).values(**kwargs))
    await session.commit()


ACTIVE_ORDER_STATUSES = ("WAITING", "RECEIVED")


async def transition_activation(session: AsyncSession, order_id: int, to_status: str,
                                from_statuses=ACTIVE_ORDER_STATUSES, **extra) -> bool:
    """
    Atomically move an activation to `to_status` only if it is still in one of
    `from_statuses`. Returns True for exactly one caller - use it to guard refunds.
    """
    values = {"status": to_status, **extra}
    if to_status in ("FINISHED", "CANCELLED"):
        values.setdefault("finished_at", utcnow())
    res = await session.execute(
        update(Activation)
        .where(Activation.order_id == order_id, Activation.status.in_(from_statuses))
        .values(**values)
    )
    await session.commit()
    return res.rowcount == 1


async def count_active_activations(session: AsyncSession, user_id: int) -> int:
    res = await session.execute(
        select(func.count(Activation.id)).where(Activation.user_id == user_id,
                                                Activation.status.in_(ACTIVE_ORDER_STATUSES)))
    return int(res.scalar() or 0)


async def get_active_activations(session: AsyncSession, since: datetime) -> list[Activation]:
    result = await session.execute(
        select(Activation).where(Activation.status.in_(ACTIVE_ORDER_STATUSES), Activation.created_at >= since)
    )
    return list(result.scalars())


async def get_user_activations(session: AsyncSession, user_id: int, limit: int = 20) -> list[Activation]:
    result = await session.execute(
        select(Activation).where(Activation.user_id == user_id)
        .order_by(Activation.created_at.desc()).limit(limit)
    )
    return result.scalars().all()


async def get_last_activation(session: AsyncSession, user_id: int) -> Activation | None:
    result = await session.execute(
        select(Activation).where(Activation.user_id == user_id)
        .order_by(Activation.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


# ─── RENTAL ───────────────────────────────────────────────────────────────────

async def create_rental(session: AsyncSession, **kwargs) -> Rental:
    r = Rental(**kwargs)
    session.add(r)
    await session.commit()
    await session.refresh(r)
    return r


async def get_last_rental(session: AsyncSession, user_id: int) -> Rental | None:
    result = await session.execute(
        select(Rental).where(Rental.user_id == user_id)
        .order_by(Rental.created_at.desc()).limit(1)
    )
    return result.scalar_one_or_none()


async def get_rental(session: AsyncSession, order_id: int) -> Rental | None:
    result = await session.execute(select(Rental).where(Rental.order_id == order_id))
    return result.scalars().first()


async def transition_rental(session: AsyncSession, order_id: int, to_status: str,
                            from_statuses=ACTIVE_ORDER_STATUSES) -> bool:
    res = await session.execute(
        update(Rental).where(Rental.order_id == order_id, Rental.status.in_(from_statuses))
        .values(status=to_status)
    )
    await session.commit()
    return res.rowcount == 1


# ─── EMAIL ────────────────────────────────────────────────────────────────────

async def create_email_order(session: AsyncSession, **kwargs) -> EmailOrder:
    e = EmailOrder(**kwargs)
    session.add(e)
    await session.commit()
    await session.refresh(e)
    return e


async def get_email_order(session: AsyncSession, email_id: str) -> EmailOrder | None:
    result = await session.execute(select(EmailOrder).where(EmailOrder.email_id == email_id))
    return result.scalars().first()


async def update_email_order(session: AsyncSession, email_id: str, **values):
    await session.execute(update(EmailOrder).where(EmailOrder.email_id == email_id).values(**values))
    await session.commit()


async def get_user_email_orders(session: AsyncSession, user_id: int, limit: int = 10) -> list[EmailOrder]:
    result = await session.execute(
        select(EmailOrder).where(EmailOrder.user_id == user_id)
        .order_by(EmailOrder.created_at.desc(), EmailOrder.id.desc()).limit(limit)
    )
    return list(result.scalars())


async def transition_email_order(session: AsyncSession, email_id: str, to_status: str,
                                 from_statuses=("WAIT", "RECEIVED"), **extra) -> bool:
    res = await session.execute(
        update(EmailOrder).where(EmailOrder.email_id == email_id, EmailOrder.status.in_(from_statuses))
        .values(status=to_status, **extra)
    )
    await session.commit()
    return res.rowcount == 1


# ─── ADMIN VIEWS ──────────────────────────────────────────────────────────────

async def get_recent_purchases(session: AsyncSession, limit: int = 15) -> list[Activation | Rental | EmailOrder]:
    rows = []
    for model in (Activation, Rental, EmailOrder):
        result = await session.execute(select(model).order_by(model.created_at.desc()).limit(limit))
        rows.extend(result.scalars())
    rows.sort(key=lambda r: r.created_at or datetime.min, reverse=True)
    return rows[:limit]


async def toggle_favorite(session: AsyncSession, user_id: int, service_id: int, service_name: str,
                          country_id: int, country_name: str) -> bool:
    """Star / unstar a service. Returns True if it is now a favorite."""
    result = await session.execute(
        select(Favorite).where(Favorite.user_id == user_id, Favorite.service_id == service_id))
    existing = result.scalars().first()
    if existing:
        await session.delete(existing)
        await session.commit()
        return False
    session.add(Favorite(user_id=user_id, service_id=service_id, service_name=service_name[:64],
                         country_id=country_id, country_name=country_name[:64]))
    try:
        await session.commit()
    except IntegrityError:          # double tap
        await session.rollback()
    return True


async def get_favorites(session: AsyncSession, user_id: int, limit: int = 20) -> list[Favorite]:
    result = await session.execute(
        select(Favorite).where(Favorite.user_id == user_id).order_by(Favorite.created_at.desc()).limit(limit))
    return list(result.scalars())


async def get_favorite(session: AsyncSession, fav_id: int) -> Favorite | None:
    return await session.get(Favorite, fav_id)


async def count_referrals(session: AsyncSession, user_id: int) -> int:
    result = await session.execute(select(func.count(User.id)).where(User.referral_id == user_id))
    return result.scalar() or 0


# ─── PAYMENT ──────────────────────────────────────────────────────────────────

async def create_payment(session: AsyncSession, **kwargs) -> Payment:
    p = Payment(**kwargs)
    session.add(p)
    await session.commit()
    await session.refresh(p)
    return p


async def get_payment(session: AsyncSession, payment_id: str) -> Payment | None:
    result = await session.execute(select(Payment).where(Payment.payment_id == payment_id))
    return result.scalar_one_or_none()


async def update_payment(session: AsyncSession, payment_id: str, **kwargs):
    await session.execute(update(Payment).where(Payment.payment_id == payment_id).values(**kwargs))
    await session.commit()


# ─── BANNER CACHE ─────────────────────────────────────────────────────────────

async def get_banner_file_id(session: AsyncSession) -> str | None:
    result = await session.execute(select(BannerCache).limit(1))
    row = result.scalar_one_or_none()
    return row.file_id if row else None


async def set_banner_file_id(session: AsyncSession, file_id: str):
    result = await session.execute(select(BannerCache).limit(1))
    row = result.scalar_one_or_none()
    if row:
        row.file_id = file_id
        row.updated_at = utcnow()
    else:
        session.add(BannerCache(file_id=file_id))
    await session.commit()
