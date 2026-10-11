import json
import os
from functools import lru_cache
from bot.config import BOT_USERNAME, SUPPORT_EMAIL, CHANNEL_LINK, SUPPORT_LINK

LOCALES_DIR = os.path.join(os.path.dirname(__file__))
SUPPORTED_LANGS = ["ru", "en", "de"]

@lru_cache(maxsize=None)
def _load(lang: str) -> dict:
    path = os.path.join(LOCALES_DIR, f"{lang}.json")
    with open(path, encoding="utf-8-sig") as f:
        return json.load(f)

def operator_label(operator: str | None, lang: str) -> str:
    """GetSMS uses "any" for "no preferred operator"."""
    return operator if operator and operator != "any" else t("no_operator", lang)


def status_label(status: str | None, lang: str) -> str:
    """Order status code (WAITING, FINISHED, ...) -> translated label; unknown codes are shown as is."""
    key = f"order_status_{status}"
    text = t(key, lang)
    return (status or "—") if text == key else text


def t(key: str, lang: str = "ru", **kwargs) -> str:
    """Translate a key for given lang, fallback to ru."""
    if lang not in SUPPORTED_LANGS:
        lang = "ru"
    strings = _load(lang)
    text = strings.get(key, _load("ru").get(key, key))
    # Built-in substitutions
    kwargs.setdefault("bot_username", BOT_USERNAME)
    kwargs.setdefault("support_email", SUPPORT_EMAIL)
    kwargs.setdefault("channel_link", CHANNEL_LINK)
    kwargs.setdefault("support_link", SUPPORT_LINK)
    if kwargs:
        try:
            text = text.format(**kwargs)
        except KeyError:
            pass
    return text
