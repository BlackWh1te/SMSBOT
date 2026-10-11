"""bot/db/queries.py - every test runs on its own empty database."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from bot.db import queries as q
from bot.db.models import Activation, BannerCache
from tests.conftest import OTHER_USER_ID, TEST_USER_ID


def _act(**overrides):
    data = dict(user_id=TEST_USER_ID, order_id=1, service_id=7, service_name="Telegram",
                phone_number="+70000000000", price=10.0, status="WAITING")
    data.update(overrides)
    return data


class TestUsers:
    async def test_create_user_defaults(self, session):
        u = await q.get_or_create_user(session, TEST_USER_ID, username="bob", full_name="Bob")
        assert (u.id, u.username, u.full_name) == (TEST_USER_ID, "bob", "Bob")
        assert u.balance == 0.0 and u.cashback == 0.0
        assert u.language == "ru"
        assert u.tos_accepted is False
        assert u.selected_operator == "any"
        assert u.total_bought == u.total_activated == u.total_rented == 0
        assert len(u.api_key) == 32 and int(u.api_key, 16) >= 0

    async def test_get_or_create_is_idempotent_and_keeps_data(self, session):
        await q.get_or_create_user(session, TEST_USER_ID, username="first")
        again = await q.get_or_create_user(session, TEST_USER_ID, username="second")
        assert again.username == "first"
        assert len(await q.get_all_users(session)) == 1

    async def test_api_keys_are_unique(self, session):
        a = await q.get_or_create_user(session, 1)
        b = await q.get_or_create_user(session, 2)
        assert a.api_key != b.api_key

    async def test_get_user_missing(self, session):
        assert await q.get_user(session, 404) is None

    async def test_update_user(self, session, reload_user):
        await q.get_or_create_user(session, TEST_USER_ID)
        await q.update_user(session, TEST_USER_ID, language="de", tos_accepted=True)
        u = await reload_user()
        assert u.language == "de" and u.tos_accepted is True


class TestBalance:
    async def test_add_balance(self, session, make_user, reload_user):
        await make_user(balance=10)
        await q.add_balance(session, TEST_USER_ID, 5.5)
        assert (await reload_user()).balance == 15.5

    async def test_add_balance_unknown_user_is_noop(self, session):
        await q.add_balance(session, 404, 100)
        assert await q.get_user(session, 404) is None

    async def test_float_rounding(self, session, make_user, reload_user):
        await make_user(balance=0)
        await q.add_balance(session, TEST_USER_ID, 0.1)
        await q.add_balance(session, TEST_USER_ID, 0.2)
        assert (await reload_user()).balance == 0.3

    async def test_deduct_success(self, session, make_user, reload_user):
        await make_user(balance=100)
        assert await q.deduct_balance(session, TEST_USER_ID, 30) is True
        assert (await reload_user()).balance == 70

    async def test_deduct_exact_balance(self, session, make_user, reload_user):
        await make_user(balance=42.5)
        assert await q.deduct_balance(session, TEST_USER_ID, 42.5) is True
        assert (await reload_user()).balance == 0

    async def test_deduct_insufficient_leaves_balance(self, session, make_user, reload_user):
        await make_user(balance=10)
        assert await q.deduct_balance(session, TEST_USER_ID, 10.01) is False
        assert (await reload_user()).balance == 10

    async def test_deduct_unknown_user(self, session):
        assert await q.deduct_balance(session, 404, 1) is False

    async def test_deduct_rejects_negative_amount(self, session, make_user, reload_user):
        await make_user(balance=10)
        assert await q.deduct_balance(session, TEST_USER_ID, -50) is False
        assert (await reload_user()).balance == 10


class TestActivations:
    async def test_create_and_get(self, session):
        act = await q.create_activation(session, **_act(order_id=55))
        assert act.id is not None and act.status == "WAITING"
        found = await q.get_activation(session, 55)
        assert found.service_name == "Telegram"
        assert await q.get_activation(session, 56) is None

    @pytest.mark.parametrize("status", ["FINISHED", "CANCELLED", "RECEIVED"])
    async def test_terminal_status_sets_finished_at(self, session, status):
        await q.create_activation(session, **_act())
        await q.update_activation(session, 1, status=status)
        act = await session.get(Activation, 1, populate_existing=True)
        assert act.status == status and act.finished_at is not None

    async def test_waiting_does_not_set_finished_at(self, session):
        await q.create_activation(session, **_act())
        await q.update_activation(session, 1, status="WAITING", sms_code="1")
        act = await session.get(Activation, 1, populate_existing=True)
        assert act.finished_at is None and act.sms_code == "1"

    async def test_explicit_finished_at_is_kept(self, session):
        when = datetime(2020, 1, 1)
        await q.create_activation(session, **_act())
        await q.update_activation(session, 1, status="FINISHED", finished_at=when)
        act = await session.get(Activation, 1, populate_existing=True)
        assert act.finished_at == when

    async def test_history_newest_first_limited_and_per_user(self, session):
        base = datetime(2026, 1, 1)
        for i in range(5):
            await q.create_activation(session, **_act(order_id=i, created_at=base + timedelta(minutes=i)))
        await q.create_activation(session, **_act(order_id=99, user_id=OTHER_USER_ID))
        hist = await q.get_user_activations(session, TEST_USER_ID, limit=3)
        assert [a.order_id for a in hist] == [4, 3, 2]
        last = await q.get_last_activation(session, TEST_USER_ID)
        assert last.order_id == 4

    async def test_last_activation_none(self, session):
        assert await q.get_last_activation(session, TEST_USER_ID) is None


class TestRentals:
    async def test_last_rental(self, session):
        assert await q.get_last_rental(session, TEST_USER_ID) is None
        base = datetime(2026, 1, 1)
        for i in range(3):
            await q.create_rental(session, user_id=TEST_USER_ID, order_id=i, service_id=1,
                                  service_name="s", phone_number="p", price=1.0,
                                  created_at=base + timedelta(hours=i))
        assert (await q.get_last_rental(session, TEST_USER_ID)).order_id == 2


class TestPayments:
    async def test_create_update_get(self, session):
        await q.create_payment(session, user_id=TEST_USER_ID, payment_id="np1",
                               amount_usd=5, amount_rub=450, currency="USDT")
        p = await q.get_payment(session, "np1")
        assert p.status == "waiting"
        await q.update_payment(session, "np1", status="confirmed", confirmed_at=datetime.utcnow())
        await session.refresh(p)
        assert p.status == "confirmed" and p.confirmed_at is not None
        assert await q.get_payment(session, "nope") is None


class TestBannerCacheTable:
    async def test_empty(self, session):
        assert await q.get_banner_file_id(session) is None

    async def test_set_then_update_keeps_single_row(self, session):
        await q.set_banner_file_id(session, "A")
        await q.set_banner_file_id(session, "B")
        assert await q.get_banner_file_id(session) == "B"
        count = (await session.execute(select(func.count(BannerCache.id)))).scalar()
        assert count == 1
