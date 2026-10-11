from aiogram.types import FSInputFile
from sqlalchemy.ext.asyncio import AsyncSession
from bot.config import BANNER_FILE_PATH
from bot.db.queries import get_banner_file_id, set_banner_file_id
from loguru import logger

_banner_file_id: str | None = None


async def get_banner(session: AsyncSession) -> str | FSInputFile:
    """
    Returns cached file_id if available, otherwise returns FSInputFile.
    Always try to cache after first send.
    """
    global _banner_file_id

    # In-memory cache first
    if _banner_file_id:
        return _banner_file_id

    # DB cache
    cached = await get_banner_file_id(session)
    if cached:
        _banner_file_id = cached
        return cached

    # Fallback to local file
    return FSInputFile(BANNER_FILE_PATH)


async def cache_banner_file_id(session: AsyncSession, file_id: str):
    """Store banner file_id after first successful send."""
    global _banner_file_id
    _banner_file_id = file_id
    await set_banner_file_id(session, file_id)
    logger.info(f"Banner file_id cached: {file_id}")
