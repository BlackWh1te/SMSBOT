"""
Country names. GetSMS sends Russian names with a flag ("🇷🇺 Россия"); English and German
come from bundled CLDR data keyed by the flag's ISO code. Unknown names are shown as is.
"""

import json
import re
from pathlib import Path

_DATA: dict[str, dict[str, str]] = json.loads(
    (Path(__file__).parent / "data" / "countries.json").read_text(encoding="utf-8"))
_FLAG = re.compile(r"^([\U0001F1E6-\U0001F1FF]{2})\s*(.*)$")

# GetSMS entries that come without a flag
_NO_FLAG = {
    "Мьянма": {"en": "Myanmar", "de": "Myanmar"},
    "США (виртуальные)": {"en": "USA (virtual)", "de": "USA (virtuell)"},
    "Дем. Конго": {"en": "DR Congo", "de": "Demokratische Republik Kongo"},
    "ЦАР": {"en": "Central African Republic", "de": "Zentralafrikanische Republik"},
    "Сент-Винсент и Гренадин": {"en": "Saint Vincent and the Grenadines", "de": "St. Vincent und die Grenadinen"},
}


def _iso(flag: str) -> str:
    return "".join(chr(ord(ch) - 0x1F1E6 + ord("A")) for ch in flag)


def localize(name: str | None, lang: str) -> str | None:
    if not name or lang == "ru":
        return name
    m = _FLAG.match(name.strip())
    if m:
        translated = _DATA.get(_iso(m.group(1)), {}).get(lang)
        return f"{m.group(1)} {translated}" if translated else name
    return _NO_FLAG.get(name.strip(), {}).get(lang, name)


def matches(name: str, query: str) -> bool:
    """Search hit in the original (Russian) name or in any translation."""
    query = query.strip().lower()
    return bool(query) and any(query in (localize(name, lang) or "").lower() for lang in ("ru", "en", "de"))
