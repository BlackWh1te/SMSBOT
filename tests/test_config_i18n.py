"""Config parsing, secrets hygiene and i18n dictionaries."""

import glob
import importlib
import json
import os
import re
import string

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
I18N_DIR = os.path.join(ROOT, "bot", "i18n")
LANGS = ["ru", "en", "de"]
CAPTION_LIMIT = 1024   # Telegram media caption limit
MESSAGE_LIMIT = 4096   # Telegram text message limit


def _load(lang):
    with open(os.path.join(I18N_DIR, f"{lang}.json"), encoding="utf-8-sig") as f:
        return json.load(f)


DICTS = {lang: _load(lang) for lang in LANGS}
ALL_KEYS = sorted(DICTS["ru"])


def _placeholders(template: str) -> set[str]:
    return {field for _, field, _, _ in string.Formatter().parse(template) if field}


# ─── Config ──────────────────────────────────────────────────────────────────

class TestConfig:
    def test_admin_ids_parsing_skips_garbage(self, monkeypatch):
        import bot.config as cfg
        monkeypatch.setenv("ADMIN_IDS", " 1, 22 ,abc,, 333,-5 ")
        try:
            importlib.reload(cfg)
            assert cfg.ADMIN_IDS == [1, 22, 333]
        finally:
            monkeypatch.undo()
            importlib.reload(cfg)

    def test_admin_ids_empty(self, monkeypatch):
        import bot.config as cfg
        monkeypatch.setenv("ADMIN_IDS", "")
        try:
            importlib.reload(cfg)
            assert cfg.ADMIN_IDS == []
        finally:
            monkeypatch.undo()
            importlib.reload(cfg)

    def test_banner_file_exists_relative_to_project(self):
        from bot.config import BANNER_FILE_PATH
        path = BANNER_FILE_PATH if os.path.isabs(BANNER_FILE_PATH) else os.path.join(ROOT, BANNER_FILE_PATH)
        assert os.path.isfile(path), f"banner missing: {path}"
        assert os.path.getsize(path) > 10_000

    def test_env_example_has_no_real_secrets(self):
        with open(os.path.join(ROOT, ".env.example"), encoding="utf-8-sig") as f:
            values = dict(
                line.strip().split("=", 1) for line in f
                if "=" in line and not line.lstrip().startswith("#")
            )
        for key in ("BOT_TOKEN", "GETSMS_API_KEY", "NOWPAYMENTS_API_KEY", "NOWPAYMENTS_IPN_SECRET"):
            v = values.get(key, "")
            # bool() so a failure never echoes the secret itself into test output
            looks_real = bool(re.fullmatch(r"[0-9a-fA-F]{32,}", v) or re.fullmatch(r"\d{6,}:[\w-]{30,}", v))
            assert not looks_real, f"{key} in .env.example looks like a real credential"


# ─── i18n dictionaries ───────────────────────────────────────────────────────

class TestI18nDictionaries:
    @pytest.mark.parametrize("lang", LANGS)
    def test_same_keys_as_ru(self, lang):
        assert set(DICTS[lang]) == set(DICTS["ru"])

    @pytest.mark.parametrize("lang", LANGS)
    def test_no_empty_values(self, lang):
        empty = [k for k, v in DICTS[lang].items() if not str(v).strip()]
        assert not empty

    @pytest.mark.parametrize("key", ALL_KEYS)
    def test_placeholders_identical_across_languages(self, key):
        found = {lang: _placeholders(DICTS[lang][key]) for lang in LANGS}
        assert found["ru"] == found["en"] == found["de"], found

    @pytest.mark.parametrize("lang", LANGS)
    def test_no_encoding_corruption(self, lang):
        """Saving the JSON with a non-UTF-8 encoding turns Cyrillic, emoji and umlauts into '?'."""
        broken = {k: v for k, v in DICTS[lang].items()
                  if re.search(r"\?\?|\w\?\w|(^|\s)\?(\s|$)", v)}
        assert not broken, f"text replaced by '?': {sorted(broken)}"

    def test_russian_strings_contain_cyrillic(self):
        latin_only_ok = {"btn_cashback", "btn_api", "lang_en", "lang_de"}  # brand / language names
        no_cyrillic = [k for k, v in DICTS["ru"].items()
                       if re.search(r"[A-Za-z]{4,}", re.sub(r"<[^>]+>|\{\w+\}", "", v))
                       and not re.search(r"[а-яА-ЯёЁ]", v) and k not in latin_only_ok]
        assert not no_cyrillic, no_cyrillic

    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("key", ["info_text", "tos_text", "topup_crypto_text",
                                     "anon_text", "fill_text", "topup_text", "ads_disabled_text"])
    def test_screens_called_without_args_have_no_raw_placeholders(self, key, lang):
        """Handlers call t(key, lang) with no kwargs; built-in substitutions must cover everything."""
        from bot.i18n import t
        assert not _placeholders(t(key, lang)), f"{lang}:{key} shows raw {{placeholders}}"

    def test_every_key_used_in_code_exists(self):
        used = set()
        for path in glob.glob(os.path.join(ROOT, "bot", "**", "*.py"), recursive=True):
            with open(path, encoding="utf-8-sig") as f:
                used |= set(re.findall(r"""\bt\(\s*['"]([a-z0-9_]+)['"]""", f.read()))
        assert used, "regex found no t() calls - test is broken"
        assert used - set(DICTS["ru"]) == set()


class TestTranslate:
    def test_unknown_lang_falls_back_to_ru(self):
        from bot.i18n import t
        assert t("btn_back", "zz") == t("btn_back", "ru")
        assert t("btn_back", None) == t("btn_back", "ru")

    def test_missing_key_returns_key(self):
        from bot.i18n import t
        assert t("definitely_not_a_key", "en") == "definitely_not_a_key"

    def test_missing_kwarg_does_not_raise(self):
        from bot.i18n import t
        # main_menu_text needs balance/country/operator - must degrade, not crash
        assert isinstance(t("main_menu_text", "ru"), str)

    def test_substitution(self):
        from bot.i18n import t
        out = t("main_menu_text", "en", balance=999.5, country="Narnia", operator="any")
        assert "999.5" in out and "Narnia" in out

    def test_builtin_substitutions_in_tos(self):
        from bot.config import BOT_USERNAME, SUPPORT_EMAIL
        from bot.i18n import t
        for lang in LANGS:
            tos = t("tos_text", lang)
            assert BOT_USERNAME in tos
            assert SUPPORT_EMAIL in tos
            assert "{" not in tos, "unformatted placeholder left in TOS"

    @pytest.mark.parametrize("lang", LANGS)
    def test_tos_fits_in_one_text_message(self, lang):
        from bot.i18n import t
        full = f"<b>{t('tos_title', lang)}</b>\n\n{t('tos_text', lang)}"
        assert len(full) <= MESSAGE_LIMIT

    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("key", ALL_KEYS)
    def test_every_template_formats_with_dummy_values(self, key, lang):
        from bot.i18n import t
        fields = _placeholders(DICTS[lang][key])
        out = t(key, lang, **{f: "X" for f in fields})
        assert not _placeholders(out), f"leftover placeholders in {lang}:{key}"

    @pytest.mark.parametrize("lang", LANGS)
    @pytest.mark.parametrize("key", [
        "main_menu_text", "profile_text", "select_service_text", "service_card_text",
        "order_created", "sms_received", "rent_created", "api_info", "referrals_text",
        "topup_text", "topup_crypto_text", "info_text", "anon_text", "email_text",
    ])
    def test_caption_templates_fit_telegram_caption(self, key, lang):
        """These texts are sent as animation captions (max 1024 chars)."""
        from bot.i18n import t
        fields = _placeholders(DICTS[lang][key])
        realistic = {f: "W" * 40 for f in fields}  # generous 40-char values
        out = t(key, lang, **realistic)
        assert len(out) <= CAPTION_LIMIT, f"{lang}:{key} is {len(out)} chars"
