from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from bot import countries as country_names, currency, pricing
from bot.i18n import t


def lang_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="\U0001f1f7\U0001f1fa \u0420\u0443\u0441\u0441\u043a\u0438\u0439", callback_data="lang:ru")
    builder.button(text="\U0001f1fa\U0001f1f8 English", callback_data="lang:en")
    builder.button(text="\U0001f1e9\U0001f1ea Deutsch", callback_data="lang:de")
    builder.adjust(3)
    return builder.as_markup()


def tos_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("tos_accept_btn", lang), callback_data="tos:accept")
    return builder.as_markup()


GREEN = "success"      # Bot API button style, like the accent buttons of the original bot

REPLY_MENU = (("btn_buy_number", "btn_topup"), ("btn_profile", "btn_menu"))


def reply_menu_keyboard(lang: str) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t(key, lang)) for key in row] for row in REPLY_MENU],
        resize_keyboard=True, is_persistent=True,
    )


def reply_menu_actions() -> dict[str, str]:
    """Reply-keyboard label in any language -> action, so an old keyboard keeps working after a language change."""
    from bot.i18n import SUPPORTED_LANGS
    return {t(key, lang): key for row in REPLY_MENU for key in row for lang in SUPPORTED_LANGS}


def close_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t("btn_close", lang), callback_data="close")]])


def main_menu_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_anon", lang), callback_data="menu:anon")
    builder.adjust(1)
    builder.button(text=t("btn_buy_number", lang), callback_data="menu:buy", style=GREEN)
    builder.button(text=t("btn_topup", lang), callback_data="menu:topup", style=GREEN)
    builder.button(text=t("btn_rent", lang), callback_data="menu:rent")
    builder.button(text=t("btn_email", lang), callback_data="menu:email")
    builder.button(text=t("btn_history", lang), callback_data="menu:history")
    builder.button(text=t("btn_country", lang), callback_data="menu:country")
    builder.button(text=t("btn_last_activation", lang), callback_data="menu:last_act")
    builder.button(text=t("btn_last_rent", lang), callback_data="menu:last_rent")
    builder.adjust(1, 2, 2, 2, 2)
    builder.button(text=t("btn_profile", lang), callback_data="menu:profile")
    builder.adjust(1, 2, 2, 2, 2, 1)
    return builder.as_markup()


def profile_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_language", lang), callback_data="profile:language")
    builder.button(text=t("btn_api", lang), callback_data="profile:api")
    builder.button(text=t("btn_transfer", lang), callback_data="profile:transfer", style=GREEN)
    builder.button(text=t("btn_cashback", lang), callback_data="profile:cashback")
    builder.button(text=t("btn_referrals", lang), callback_data="profile:referrals")
    builder.button(text=t("btn_info", lang), callback_data="profile:info")
    builder.button(text=t("btn_disable_ads", lang), callback_data="profile:disable_ads", style=GREEN)
    builder.button(text=t("btn_currency", lang), callback_data="profile:currency")
    builder.button(text=t("btn_back", lang), callback_data="menu:main")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def currency_keyboard(lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for cur in currency.CURRENCIES:
        builder.button(text=t(f"btn_cur_{cur}", lang), callback_data=f"cur:{cur}")
    builder.button(text=t("btn_back", lang), callback_data="menu:profile")
    builder.adjust(len(currency.CURRENCIES), 1)
    return builder.as_markup()


def topup_keyboard(lang: str, methods: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    """methods: [(code, button title)] of the currently active payment methods."""
    builder = InlineKeyboardBuilder()
    for code, title in methods:
        builder.button(text=title, callback_data=f"topup:m:{code}")
    # 2 per row; with an odd count the first one gets a full-width row
    rows = ([1] if len(methods) % 2 else []) + [2] * (len(methods) // 2)
    if rows:
        builder.adjust(*rows)
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:main"))
    return builder.as_markup()


def countries_keyboard(countries: list[dict], page: int, total_pages: int, lang: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for c in countries:
        builder.button(text=country_names.localize(c["name"], lang), callback_data=f"country:{c['id']}")
    # 3 per row
    rows = [3] * (len(countries) // 3)
    if len(countries) % 3:
        rows.append(len(countries) % 3)
    builder.adjust(*rows)

    # Operator + Search row
    builder.row(
        InlineKeyboardButton(text=t("select_operator", lang), callback_data="operator:select"),
        InlineKeyboardButton(text=t("btn_search", lang), callback_data="search:country"),
    )
    # Pagination row with LIVE button
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text=t("btn_prev", lang), callback_data=f"countries_page:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    nav.append(InlineKeyboardButton(text="LIVE \u25b6", callback_data="live:countries"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text=t("btn_next", lang), callback_data=f"countries_page:{page+1}"))
    builder.row(*nav)
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:main"))
    return builder.as_markup()


def services_keyboard(services: list[dict], page: int, total_pages: int, lang: str, is_rent: bool = False) -> InlineKeyboardMarkup:
    prefix = "rent_service" if is_rent else "service"
    page_prefix = "rent_services_page" if is_rent else "services_page"
    builder = InlineKeyboardBuilder()
    for s in services:
        builder.button(text=s["name"], callback_data=f"{prefix}:{s['id']}")
    rows = [3] * (len(services) // 3)
    if len(services) % 3:
        rows.append(len(services) % 3)
    builder.adjust(*rows)
    # Pagination with LIVE
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton(text=t("btn_prev", lang), callback_data=f"{page_prefix}:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page}/{total_pages}", callback_data="noop"))
    nav.append(InlineKeyboardButton(text="LIVE \u25b6", callback_data=f"live:services"))
    if page < total_pages:
        nav.append(InlineKeyboardButton(text=t("btn_next", lang), callback_data=f"{page_prefix}:{page+1}"))
    builder.row(*nav)
    # Email + Search row (only for activation services, not rent)
    if not is_rent:
        builder.row(
            InlineKeyboardButton(text=t("btn_email", lang), callback_data="menu:email"),
            InlineKeyboardButton(text=t("btn_search", lang), callback_data="search:service"),
        )
        builder.row(InlineKeyboardButton(text=t("btn_favorites", lang), callback_data="favs"))
    builder.row(InlineKeyboardButton(text=t("btn_back", lang), callback_data="menu:main"))
    return builder.as_markup()


MAX_PRICE_TIERS = 8


def service_card_keyboard(service_id: int, price: float, count: int, lang: str,
                          tiers: list[dict] | None = None, cur: str = "USD") -> InlineKeyboardMarkup:
    """
    One buy button per price tier (cheap and expensive numbers, like the original bot):
    if cheap numbers don't work, the user can try a more expensive provider.
    Without tier data: a single button with the default price.
    """
    builder = InlineKeyboardBuilder()
    if tiers:
        for tier in tiers[:MAX_PRICE_TIERS]:
            kopecks = round(tier["price"] * 100)           # provider cost; the markup is added at purchase
            builder.button(text=t("btn_price_tier", lang, price=currency.fmt(pricing.sell(tier["price"]), cur, up=True), count=tier["count"]),
                           callback_data=f"order:create:{service_id}:{kopecks}")
        buy_rows = [1] * min(len(tiers), MAX_PRICE_TIERS)
    else:
        builder.button(text=t("btn_price_tier", lang, price=currency.fmt(pricing.sell(price), cur, up=True), count=count),
                       callback_data=f"order:create:{service_id}")
        buy_rows = [1]
    builder.button(text=t("btn_top10", lang), callback_data=f"top10:{service_id}")
    # The GetSMS API has no prefix exclusion or bulk ordering - say so instead of doing nothing
    builder.button(text=t("btn_exclude_prefix", lang), callback_data="na:prefix")
    builder.button(text=t("btn_multi_order", lang), callback_data="na:multi")
    builder.button(text=t("btn_add_fav", lang), callback_data=f"fav:{service_id}")
    builder.button(text=t("btn_other_countries", lang), callback_data="menu:country")
    builder.button(text=t("btn_back", lang), callback_data="menu:buy")
    builder.button(text=t("btn_menu", lang), callback_data="menu:main")
    builder.adjust(*buy_rows, 1, 1, 1, 2, 2)
    return builder.as_markup()


def order_active_keyboard(order_id: int, lang: str, can_repeat: bool = True) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if can_repeat:
        builder.button(text=t("btn_request_another", lang), callback_data=f"order:repeat:{order_id}")
    builder.button(text=t("btn_finish_order", lang), callback_data=f"order:finish:{order_id}")
    builder.button(text=t("btn_back", lang), callback_data="menu:main")
    builder.button(text=t("btn_menu", lang), callback_data="menu:main")
    builder.adjust(1, 1, 2) if can_repeat else builder.adjust(1, 2)
    return builder.as_markup()


def error_keyboard(lang: str, retry_cb: str = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if retry_cb:
        builder.button(text=t("btn_retry", lang), callback_data=retry_cb)
    builder.button(text=t("btn_back", lang), callback_data="menu:main")
    builder.button(text=t("btn_menu", lang), callback_data="menu:main")
    builder.adjust(1, 2)
    return builder.as_markup()


def back_keyboard(lang: str, back_cb: str = "menu:main") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=t("btn_back", lang), callback_data=back_cb)
    builder.button(text=t("btn_menu", lang), callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()
