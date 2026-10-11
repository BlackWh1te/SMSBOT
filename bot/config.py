import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
GETSMS_API_KEY: str = os.getenv("GETSMS_API_KEY", "")
GETSMS_BASE_URL: str = os.getenv("GETSMS_BASE_URL", "https://userapi.getsms.shop")
NOWPAYMENTS_API_KEY: str = os.getenv("NOWPAYMENTS_API_KEY", "")
NOWPAYMENTS_IPN_SECRET: str = os.getenv("NOWPAYMENTS_IPN_SECRET", "")
NOWPAYMENTS_BASE_URL: str = os.getenv("NOWPAYMENTS_BASE_URL", "https://api.nowpayments.io/v1")
DATABASE_URL: str = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./bot.db")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "MartovBot")
SUPPORT_LINK: str = os.getenv("SUPPORT_LINK", "https://t.me/support")
CHANNEL_LINK: str = os.getenv("CHANNEL_LINK", "https://t.me/channel")
SUPPORT_EMAIL: str = os.getenv("SUPPORT_EMAIL", "support@example.com")
DEFAULT_LANGUAGE: str = os.getenv("DEFAULT_LANGUAGE", "ru")
BANNER_FILE_PATH: str = os.getenv("BANNER_FILE_PATH", "6695164095367458395.mp4")
BANNER_FILE_ID: str = os.getenv("BANNER_FILE_ID", "")
# Public URL of the banner GIF/MP4. When set, the TOS is sent as ONE message:
# full text + banner shown as a large link preview above it (captions are capped at 1024 chars).
BANNER_URL: str = os.getenv("BANNER_URL", "")
# Encrypts payment API keys stored in the DB. If empty, a key is derived from BOT_TOKEN
# (changing the token then makes saved keys unreadable - they must be re-entered).
SETTINGS_SECRET: str = os.getenv("SETTINGS_SECRET", "")
ADMIN_IDS: list[int] = [
    int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
]
