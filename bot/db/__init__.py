from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from bot.config import DATABASE_URL
from bot.db.models import Base

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


# Columns added after the first release: create_all() never alters existing tables.
NEW_COLUMNS = {"users": {"currency": "VARCHAR(3)", "is_banned": "BOOLEAN NOT NULL DEFAULT 0"}}


def _add_missing_columns(sync_conn):
    insp = inspect(sync_conn)
    for table, columns in NEW_COLUMNS.items():
        existing = {c["name"] for c in insp.get_columns(table)}
        for name, ddl in columns.items():
            if name not in existing:
                sync_conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}"))


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)


async def get_session() -> AsyncSession:
    async with AsyncSessionLocal() as session:
        yield session
