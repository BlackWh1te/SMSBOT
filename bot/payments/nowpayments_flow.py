"""
NOWPayments top-up flow (no webhook needed):

  amount in RUB -> USD at the bot's exchange rate -> POST /payment (user's chosen coin)
  -> user sends crypto to pay_address
  -> watcher() polls GET /payment/{id} every 30 s (or the user taps "check")
  -> on "finished" the balance is credited exactly once (atomic conditional UPDATE).

All rows in the `payments` table belong to this flow.
"""

import asyncio
import secrets
from datetime import timedelta

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import currency, payments
from bot.api.nowpayments import PENDING_STATUSES, STATUS_FINISHED, NowPaymentsClient
from bot.db.models import Payment, User, utcnow

METHOD = "nowpayments"
POLL_INTERVAL = 30           # seconds
WATCH_WINDOW = timedelta(hours=48)

# Offered in this order when enabled in the owner's NOWPayments "Coins settings"
PREFERRED_COINS = [
    ("btc", "BTC"), ("eth", "ETH"), ("usdttrc20", "USDT (TRC20)"), ("ltc", "LTC"),
    ("sol", "SOL"), ("ton", "TON"), ("trx", "TRX"), ("usdterc20", "USDT (ERC20)"),
]
COIN_LABELS = dict(PREFERRED_COINS)


def client_for(state: payments.MethodState) -> NowPaymentsClient:
    return NowPaymentsClient(api_key=state.creds.get("api_key", ""),
                             ipn_secret=state.creds.get("ipn_secret") or "")


def coin_label(code: str) -> str:
    return COIN_LABELS.get(code, code.upper())


async def available_coins(state: payments.MethodState) -> list[tuple[str, str]]:
    enabled = set(await client_for(state).get_merchant_coins())
    if not enabled:                     # API unreachable / nothing configured: offer the defaults
        return PREFERRED_COINS
    preferred = [c for c in PREFERRED_COINS if c[0] in enabled]
    return preferred or [(c, c.upper()) for c in sorted(enabled)[:8]]


async def create_invoice(session: AsyncSession, user_id: int, state: payments.MethodState,
                         amount_rub: float, coin: str) -> tuple[Payment | None, dict]:
    """Returns (payment_row, api_response). payment_row is None when NOWPayments refused."""
    amount_usd = currency.from_rub(amount_rub, "USD")
    order_id = f"tg{user_id}-{secrets.token_hex(4)}"
    resp = await client_for(state).create_payment(
        amount_usd, coin, order_id, order_description=f"Balance top-up {amount_usd:.2f} USD",
    )
    if resp.get("error") or not resp.get("payment_id") or not resp.get("pay_address"):
        logger.warning(f"NOWPayments create_payment refused for user {user_id}: {resp}")
        return None, resp
    row = Payment(user_id=user_id, payment_id=str(resp["payment_id"]), amount_usd=amount_usd,
                  amount_rub=amount_rub, currency=coin, status=resp.get("payment_status", "waiting"))
    session.add(row)
    await session.commit()
    return row, resp


async def credit_payment(session: AsyncSession, payment_id: str) -> Payment | None:
    """
    Mark the payment finished and add its RUB amount to the user's balance in ONE
    transaction. The conditional UPDATE makes this idempotent: if the watcher and
    the "check" button race, only one of them credits.
    """
    res = await session.execute(
        update(Payment)
        .where(Payment.payment_id == payment_id, Payment.status != STATUS_FINISHED)
        .values(status=STATUS_FINISHED, confirmed_at=utcnow())
    )
    if res.rowcount != 1:
        # Already credited by someone else. Nothing changed, so just end the transaction:
        # rollback() would expire the Payment objects the watcher is still iterating over.
        await session.commit()
        return None
    pay = (await session.execute(select(Payment).where(Payment.payment_id == payment_id))).scalar_one()
    await session.execute(
        update(User).where(User.id == pay.user_id).values(balance=User.balance + pay.amount_rub)
    )
    await session.commit()
    logger.info(f"Top-up credited: user {pay.user_id} +{pay.amount_rub} RUB (NOWPayments {payment_id})")
    return pay


async def refresh_payment(session: AsyncSession, pay: Payment) -> tuple[str, bool]:
    """Fetch the live status. Returns (status, credited_now)."""
    if pay.status == STATUS_FINISHED:
        return STATUS_FINISHED, False
    state = await payments.get_state(session, METHOD)
    if not state.creds.get("api_key"):
        return pay.status, False
    data = await client_for(state).get_payment_status(pay.payment_id)
    status = data.get("payment_status")
    if not status:                      # API error - keep the old status, try again later
        return pay.status, False
    if status == STATUS_FINISHED:
        return STATUS_FINISHED, (await credit_payment(session, pay.payment_id)) is not None
    if status != pay.status:
        await session.execute(
            update(Payment).where(Payment.payment_id == pay.payment_id, Payment.status != STATUS_FINISHED)
            .values(status=status)
        )
        await session.commit()
    return status, False


async def pending_payments(session: AsyncSession) -> list[Payment]:
    since = utcnow() - WATCH_WINDOW
    res = await session.execute(
        select(Payment).where(Payment.status.in_(PENDING_STATUSES), Payment.created_at >= since)
    )
    return list(res.scalars())


async def watch_once(session_factory, notify) -> int:
    """One polling pass. `notify(user_id, payment)` is awaited for every credited payment."""
    credited = 0
    async with session_factory() as session:
        for pay in await pending_payments(session):
            try:
                _, done = await refresh_payment(session, pay)
            except Exception as e:      # one bad payment must not stop the others
                logger.error(f"NOWPayments refresh {pay.payment_id} failed: {e!r}")
                continue
            if done:
                credited += 1
                try:
                    await notify(pay.user_id, pay)
                except Exception as e:
                    logger.warning(f"Could not notify user {pay.user_id} about top-up: {e!r}")
    return credited


async def watcher(session_factory, notify, interval: int = POLL_INTERVAL):
    """Runs forever next to polling; started from bot.main."""
    logger.info("NOWPayments watcher started")
    while True:
        try:
            await watch_once(session_factory, notify)
        except Exception as e:
            logger.error(f"NOWPayments watcher pass failed: {e!r}")
        await asyncio.sleep(interval)
