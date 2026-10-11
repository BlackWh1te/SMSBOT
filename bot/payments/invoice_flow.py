"""
CryptoBot and xRocket top-ups (Telegram-native crypto, tiny minimums):

  amount in RUB -> invoice in the provider's bot (CryptoBot: priced in RUB; xRocket: in USDT)
  -> user pays inside Telegram -> watcher() polls every 20 s (or the user taps "check")
  -> on "paid" the provider's amount is checked against the stored one and the balance is
     credited exactly once (same atomic ledger as NOWPayments).

Rows live in `payments` with payment_id "<method>:<invoice id>" and status "active" until paid.
"""

import asyncio
import secrets
from datetime import timedelta

import aiohttp
from loguru import logger
from sqlalchemy import func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from bot import currency, payments
from bot.db.models import Payment, utcnow
from bot.payments.nowpayments_flow import credit_payment

CRYPTOBOT, XROCKET = "cryptobot", "xrocket"
METHODS = (CRYPTOBOT, XROCKET)
STATUS_ACTIVE, STATUS_EXPIRED = "active", "expired"
INVOICE_TTL = 3600              # seconds the invoice can be paid
MAX_OPEN_INVOICES = 3           # unpaid invoices per user
POLL_INTERVAL = 20
TIMEOUT = aiohttp.ClientTimeout(total=15)


class CryptoBotClient:
    """Crypto Pay API (@CryptoBot): https://pay.crypt.bot/api/<method>, header Crypto-Pay-API-Token."""
    BASE = "https://pay.crypt.bot/api"

    def __init__(self, token: str, base: str = BASE):
        self.token, self.base = token, base

    async def _call(self, method: str, params: dict) -> dict:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as http:
                async with http.get(f"{self.base}/{method}", params=params,
                                    headers={"Crypto-Pay-API-Token": self.token}) as r:
                    return await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"CryptoBot {method} failed: {e!r}")
            return {"ok": False, "error": "network"}

    async def create(self, amount_rub: float, payload: str, description: str) -> dict | None:
        data = await self._call("createInvoice", {
            "currency_type": "fiat", "fiat": "RUB", "amount": f"{amount_rub:.2f}",
            "description": description[:1024], "payload": payload, "expires_in": INVOICE_TTL,
            "allow_comments": "false",
        })
        inv = data.get("result") if data.get("ok") else None
        if not inv or not inv.get("invoice_id"):
            logger.warning(f"CryptoBot createInvoice refused: {data}")
            return None
        url = inv.get("bot_invoice_url") or inv.get("mini_app_invoice_url") or inv.get("pay_url")
        return {"id": str(inv["invoice_id"]), "url": url} if url else None

    async def statuses(self, ids: list[str]) -> dict[str, dict]:
        """{invoice id: invoice} for the ids the API returned."""
        data = await self._call("getInvoices", {"invoice_ids": ",".join(ids), "count": len(ids)})
        if not data.get("ok"):
            return {}
        return {str(i.get("invoice_id")): i for i in (data.get("result") or {}).get("items") or []}


class XRocketClient:
    """xRocket Pay API: https://pay.xrocket.exchange, header Rocket-Pay-Key."""
    BASE = "https://pay.xrocket.exchange"

    def __init__(self, token: str, base: str = BASE):
        self.token, self.base = token, base

    async def _request(self, method: str, path: str, json: dict | None = None) -> dict:
        try:
            async with aiohttp.ClientSession(timeout=TIMEOUT) as http:
                async with http.request(method, f"{self.base}{path}", json=json,
                                        headers={"Rocket-Pay-Key": self.token}) as r:
                    return await r.json(content_type=None)
        except Exception as e:
            logger.warning(f"xRocket {method} {path} failed: {e!r}")
            return {"success": False, "error": "network"}

    async def create(self, amount_usdt: float, payload: str, description: str) -> dict | None:
        data = await self._request("POST", "/tg-invoices", {
            "amount": amount_usdt, "numPayments": 1, "currency": "USDT",
            "description": description[:1000], "payload": payload, "expiredIn": INVOICE_TTL,
        })
        inv = data.get("data") if data.get("success") else None
        if not inv or not inv.get("id") or not inv.get("link"):
            logger.warning(f"xRocket create invoice refused: {data}")
            return None
        return {"id": str(inv["id"]), "url": inv["link"]}

    async def statuses(self, ids: list[str]) -> dict[str, dict]:
        out = {}
        for invoice_id in ids:
            data = await self._request("GET", f"/tg-invoices/{invoice_id}")
            if data.get("success") and data.get("data"):
                out[invoice_id] = data["data"]
        return out


def client_for(state: payments.MethodState):
    token = state.creds.get("api_token", "")
    return CryptoBotClient(token) if state.code == CRYPTOBOT else XRocketClient(token)


def split_id(payment_id: str) -> tuple[str, str]:
    method, _, invoice_id = payment_id.partition(":")
    return method, invoice_id


def amount_matches(method: str, pay: Payment, inv: dict) -> bool:
    """The paid invoice must be exactly the one we created - never credit on a lower amount."""
    try:
        if method == CRYPTOBOT:
            return inv.get("fiat") == "RUB" and abs(float(inv["amount"]) - pay.amount_rub) < 0.01
        return inv.get("currency") == "USDT" and abs(float(inv["amount"]) - pay.amount_usd) < 0.0001
    except (KeyError, TypeError, ValueError):
        return False


async def open_invoices(session: AsyncSession, user_id: int) -> int:
    since = utcnow() - timedelta(seconds=INVOICE_TTL)
    res = await session.execute(select(func.count(Payment.id)).where(
        Payment.user_id == user_id, Payment.status == STATUS_ACTIVE, Payment.created_at >= since))
    return int(res.scalar() or 0)


async def create_invoice(session: AsyncSession, user_id: int, state: payments.MethodState,
                         amount_rub: float) -> tuple[Payment | None, str | None]:
    """Returns (payment row, pay url); (None, None) when the provider refused."""
    amount_usd = round(amount_rub / currency.rate("USD"), 4)
    payload = f"tg{user_id}-{secrets.token_hex(4)}"
    description = f"Balance top-up {amount_rub:.2f} RUB"
    client = client_for(state)
    if state.code == CRYPTOBOT:
        inv = await client.create(amount_rub, payload, description)
    else:
        inv = await client.create(amount_usd, payload, description)
    if inv is None:
        return None, None
    row = Payment(user_id=user_id, payment_id=f"{state.code}:{inv['id']}", amount_usd=amount_usd,
                  amount_rub=amount_rub, currency="RUB" if state.code == CRYPTOBOT else "USDT",
                  status=STATUS_ACTIVE)
    session.add(row)
    await session.commit()
    return row, inv["url"]


async def refresh(session: AsyncSession, pays: list[Payment]) -> list[Payment]:
    """Check the given active invoices; returns the ones credited now."""
    credited = []
    by_method: dict[str, list[Payment]] = {}
    for pay in pays:
        by_method.setdefault(split_id(pay.payment_id)[0], []).append(pay)
    for method, group in by_method.items():
        if method not in METHODS:
            continue
        state = await payments.get_state(session, method)
        if not state.creds.get("api_token"):
            continue
        found = await client_for(state).statuses([split_id(p.payment_id)[1] for p in group])
        for pay in group:
            inv = found.get(split_id(pay.payment_id)[1])
            if not inv:
                continue
            status = inv.get("status")
            if status == "paid":
                if not amount_matches(method, pay, inv):
                    logger.error(f"Invoice {pay.payment_id}: paid amount {inv.get('amount')} does not match "
                                 f"{pay.amount_rub} RUB / {pay.amount_usd} USDT - not credited")
                    continue
                if await credit_payment(session, pay.payment_id) is not None:
                    credited.append(pay)
            elif status == "expired":
                await session.execute(update(Payment).where(
                    Payment.payment_id == pay.payment_id, Payment.status == STATUS_ACTIVE
                ).values(status=STATUS_EXPIRED))
                await session.commit()
    return credited


async def pending(session: AsyncSession) -> list[Payment]:
    since = utcnow() - timedelta(seconds=INVOICE_TTL + 600)
    res = await session.execute(select(Payment).where(
        Payment.status == STATUS_ACTIVE, Payment.created_at >= since,
        or_(*(Payment.payment_id.like(f"{m}:%") for m in METHODS))))
    return list(res.scalars())


async def watch_once(session_factory, notify) -> int:
    async with session_factory() as session:
        credited = await refresh(session, await pending(session))
    for pay in credited:
        try:
            await notify(pay.user_id, pay)
        except Exception as e:
            logger.warning(f"Could not notify user {pay.user_id} about top-up: {e!r}")
    return len(credited)


async def watcher(session_factory, notify, interval: int = POLL_INTERVAL):
    logger.info("CryptoBot/xRocket watcher started")
    while True:
        try:
            await watch_once(session_factory, notify)
        except Exception as e:
            logger.error(f"Invoice watcher pass failed: {e!r}")
        await asyncio.sleep(interval)
