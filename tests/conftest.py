"""
Shared fixtures for the MARTOV SMS Bot test suite.

Everything here is offline and deterministic:
  * each test gets its own fresh in-memory SQLite database
  * the GetSMS client is replaced by AsyncMocks (no network, no real money)
  * Telegram CallbackQuery / Message objects are lightweight mocks

Live tests against the real GetSMS / Telegram APIs live in test_live.py and
only run with `pytest --live`.
"""

import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

# Must happen before any `bot.*` import: load_dotenv() never overrides
# variables that are already set, so the production bot.db is never touched.
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from bot.db.models import Base

TEST_USER_ID = 111_000_111
OTHER_USER_ID = 222_000_222
ADMIN_ID = 999_000_999


# ─── CLI: opt-in live tests ──────────────────────────────────────────────────

def pytest_addoption(parser):
    parser.addoption(
        "--live", action="store_true", default=False,
        help="run tests that call the real GetSMS / Telegram APIs",
    )


def pytest_configure(config):
    config.addinivalue_line("markers", "live: calls real external APIs (needs --live)")


def pytest_collection_modifyitems(config, items):
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="live API test - run with --live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


# ─── Database ────────────────────────────────────────────────────────────────

@pytest.fixture
async def session_factory(monkeypatch):
    """Fresh shared in-memory DB per test. StaticPool lets every session
    (including the ones poll_sms / middleware open themselves) see the same DB."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    import bot.db
    import bot.middlewares.db
    monkeypatch.setattr(bot.db, "AsyncSessionLocal", factory)
    monkeypatch.setattr(bot.middlewares.db, "AsyncSessionLocal", factory)

    yield factory
    await engine.dispose()


@pytest.fixture
async def session(session_factory):
    async with session_factory() as s:
        yield s


@pytest.fixture
async def make_user(session):
    """Create a user row: await make_user(balance=100, selected_country_id=1, ...)."""
    from bot.db.queries import get_or_create_user, update_user

    async def _make(user_id: int = TEST_USER_ID, **fields):
        await get_or_create_user(session, user_id)
        defaults = {"tos_accepted": True}
        defaults.update(fields)
        await update_user(session, user_id, **defaults)
        return await _fresh_user(session, user_id)

    return _make


async def _fresh_user(session, user_id):
    from bot.db.models import User
    return await session.get(User, user_id, populate_existing=True)


@pytest.fixture
def reload_user(session):
    """Re-read a user from the DB, bypassing the identity map."""
    async def _reload(user_id: int = TEST_USER_ID):
        return await _fresh_user(session, user_id)
    return _reload


# ─── Global state resets ─────────────────────────────────────────────────────

class FakeInvoiceClient:
    """Stands in for CryptoBot / xRocket: records created invoices, returns the statuses a test sets."""

    def __init__(self):
        self.created, self.invoices, self.refuse = [], {}, False

    async def create(self, amount, payload, description):
        if self.refuse:
            return None
        self.created.append((amount, payload))
        invoice_id = str(100 + len(self.created))
        return {"id": invoice_id, "url": f"https://t.me/pay?start=IV{invoice_id}"}

    async def statuses(self, ids):
        return {i: self.invoices[i] for i in ids if i in self.invoices}


@pytest.fixture(autouse=True)
def fake_invoices(monkeypatch):
    """Tests never reach the real payment providers."""
    from bot.payments import invoice_flow
    fake = FakeInvoiceClient()
    monkeypatch.setattr(invoice_flow, "client_for", lambda state: fake)
    return fake


@pytest.fixture(autouse=True)
def _reset_purchase_limits():
    import bot.antiabuse as antiabuse
    antiabuse.reset()
    yield
    antiabuse.reset()


@pytest.fixture(autouse=True)
def _flat_exchange_rates(monkeypatch):
    """1 RUB = 1 $ = 1 € in tests, so amounts keep their values; test_currency.py covers real rates."""
    import bot.currency as currency
    monkeypatch.setattr(currency, "_auto", {})
    monkeypatch.setattr(currency, "_manual", {})
    monkeypatch.setattr(currency, "FALLBACK", {"USD": 1.0, "EUR": 1.0})


@pytest.fixture(autouse=True)
def _reset_banner_cache(monkeypatch):
    import bot.utils.banner as banner
    monkeypatch.setattr(banner, "_banner_file_id", None)


# ─── Fake GetSMS API ─────────────────────────────────────────────────────────

COUNTRIES = [{"id": i, "name": f"Country{i}"} for i in range(1, 51)]
SERVICES = [{"id": 100 + i, "name": f"Service{i}"} for i in range(1, 31)]


@pytest.fixture
def fake_getsms(monkeypatch):
    """Patch every network method on the shared `getsms` singleton.
    All handlers import the same instance, so one patch covers them all.
    Override return values per test: fake_getsms.get_service_price.return_value = 50"""
    from bot.api.getsms import getsms

    defaults = {
        "get_balance": 1000.0,
        "get_countries": COUNTRIES,
        "get_services": SERVICES,
        "get_operators": ["mts", "beeline", "megafon"],
        "search_services": [],
        "get_service_price": 50.0,
        "get_service_count": 123,
        "get_offers": {"ok": True},
        "create_order": {"ok": False, "error_code": "NOT_CONFIGURED"},
        "get_order": {"ok": True, "data": {"status": "WAITING"}},
        "get_active_orders": [],
        "request_another_code": {"ok": True},
        "finish_order": {"ok": True, "refunded": False},
        "create_rental": {"ok": False, "error_code": "NOT_CONFIGURED"},
        "get_rental": {"ok": True},
        "finish_rental": {"ok": True},
        "get_rental_price": {"ok": True, "price": 200.0},
        # Email formats as documented in PROJECT.md and confirmed against the live API
        "get_email_domains": [{"domain": "gmail.com", "price": 3.39, "count": 617608},
                              {"domain": "mail.ru", "price": 5.0, "count": 100}],
        "create_email": {"ok": True, "order": {"id": "e1", "email": "x@gmail.com", "status": "WAIT", "price": 3.39}},
        "check_email": {"ok": True, "order": {"status": "WAIT"}},
        "reorder_email": {"ok": True, "order": {"status": "WAIT"}, "charged": 3.39},
        "cancel_email": {"ok": True, "refunded": True, "refund_amount": 3.39},
    }
    fake = SimpleNamespace()
    for name, value in defaults.items():
        mock = AsyncMock(return_value=value)
        monkeypatch.setattr(getsms, name, mock)
        setattr(fake, name, mock)
    return fake


def order_response(order_id=5001, price=50.0, phone="+79990001122", service="Telegram"):
    return {
        "ok": True,
        "order": {
            "id": order_id,
            "phone_number": phone,
            "price": price,
            "expire_date": "2026-10-09T12:34:56Z",
        },
        "service": {"name": service},
    }


# ─── FSM state ───────────────────────────────────────────────────────────────

@pytest.fixture
def make_state():
    """Real aiogram FSMContext on in-memory storage: state = make_state(user_id)."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    storage = MemoryStorage()

    def _make(user_id: int = TEST_USER_ID):
        return FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=user_id, user_id=user_id))
    return _make


# ─── Telegram object mocks ───────────────────────────────────────────────────

def _message_mock(user_id: int):
    msg = MagicMock(name="Message")
    msg.chat.id = user_id
    msg.from_user = SimpleNamespace(id=user_id, username="tester", full_name="Test User")
    sent = MagicMock(name="SentMessage")
    sent.animation.file_id = "NEW_TG_FILE_ID"
    msg.answer = AsyncMock(return_value=sent)
    msg.answer_animation = AsyncMock(return_value=sent)
    msg.edit_caption = AsyncMock()
    msg.edit_text = AsyncMock()
    msg.edit_reply_markup = AsyncMock()
    msg.delete = AsyncMock()
    return msg


@pytest.fixture
def make_call():
    """Build a fake CallbackQuery: call = make_call("menu:buy")."""
    def _make(data: str, user_id: int = TEST_USER_ID):
        call = MagicMock(name="CallbackQuery")
        call.data = data
        call.from_user = SimpleNamespace(id=user_id, username="tester", full_name="Test User")
        call.answer = AsyncMock()
        call.message = _message_mock(user_id)
        return call
    return _make


@pytest.fixture
def make_message():
    """Build a fake incoming Message: msg = make_message("/addbal 1 2")."""
    def _make(text: str = "", user_id: int = TEST_USER_ID):
        msg = _message_mock(user_id)
        msg.text = text
        return msg
    return _make


# ─── Assertion helpers ───────────────────────────────────────────────────────

def last_caption(call) -> str:
    """Caption text of the most recent edit_caption call."""
    return call.message.edit_caption.await_args.kwargs["caption"]


def last_markup(call):
    return call.message.edit_caption.await_args.kwargs["reply_markup"]


def callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def alert_text(call) -> str | None:
    """Text of the last call.answer(..., show_alert=True), else None."""
    for c in reversed(call.answer.await_args_list):
        if c.kwargs.get("show_alert"):
            return c.args[0] if c.args else c.kwargs.get("text")
    return None
