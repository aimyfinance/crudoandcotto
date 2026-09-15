from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import services as S
from .money import fmt_price

BACK = "◀️ Назад"
CANCEL = "❌ Скасувати"
SKIP = "⏭ Пропустити"
TODAY = "📅 Сьогодні"

M_SALE = "🛒 Продаж"
M_PURCHASE = "📦 Закупівля"
M_STOCK = "📊 Залишки"
M_PRODUCTS = "🧀 Товари"
M_BATCHES = "🏷 Партії"
M_WRITEOFF = "✂️ Списання / коригування"
M_HISTORY = "🕘 Історія"
M_REPORTS = "📈 Звіти"
M_SETTINGS = "⚙️ Налаштування"

ROLE_MENUS = {
    "admin": [[M_SALE, M_PURCHASE], [M_STOCK, M_PRODUCTS], [M_BATCHES, M_WRITEOFF], [M_HISTORY, M_REPORTS], [M_SETTINGS]],
    "manager": [[M_SALE, M_PURCHASE], [M_STOCK, M_PRODUCTS], [M_BATCHES, M_WRITEOFF], [M_HISTORY, M_REPORTS]],
    "seller": [[M_SALE, M_STOCK], [M_HISTORY]],
}
MENU_BUTTONS = {M_SALE, M_PURCHASE, M_STOCK, M_PRODUCTS, M_BATCHES, M_WRITEOFF, M_HISTORY, M_REPORTS, M_SETTINGS}


def main_menu(role: str) -> ReplyKeyboardMarkup:
    rows = ROLE_MENUS.get(role, ROLE_MENUS["seller"])
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in r] for r in rows], resize_keyboard=True)


def nav_kb(*extra: str, back: bool = True) -> ReplyKeyboardMarkup:
    rows = []
    if extra:
        rows.append([KeyboardButton(text=e) for e in extra])
    rows.append([KeyboardButton(text=BACK), KeyboardButton(text=CANCEL)] if back else [KeyboardButton(text=CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def remove_kb() -> ReplyKeyboardRemove:
    return ReplyKeyboardRemove()


def inline(rows: list[list[tuple[str, str]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=t, callback_data=c) for t, c in r] for r in rows])


CAT_SHORT = {"cheese": "🧀 Сири", "meat": "🥩 М'ясо", "pasta": "🍝 Паста"}


def product_picker(db, ctx: str, category: str | None = None, page: int = 0, per_page: int = 8,
                   show_price: bool = True, active_only: bool = True) -> InlineKeyboardMarkup:
    """Вибір товару: фільтр за категорією + сторінки. callback: pp:{ctx}:id:{product_id}"""
    kb = InlineKeyboardBuilder()
    kb.row(*[InlineKeyboardButton(text=("• " if category == c else "") + t, callback_data=f"pp:{ctx}:cat:{c}")
             for c, t in CAT_SHORT.items()])
    prods = S.list_products(db, active_only=active_only, category=category)
    total = len(prods)
    chunk = prods[page * per_page:(page + 1) * per_page]
    for p in chunk:
        label = p["name"]
        if show_price:
            unit = "шт" if p["sale_mode"] == "piece" else "кг"
            label += f" — {fmt_price(p['retail_price'])} €/{unit}"
        kb.row(InlineKeyboardButton(text=label[:60], callback_data=f"pp:{ctx}:id:{p['id']}"))
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"pp:{ctx}:pg:{page - 1}:{category or ''}"))
    if (page + 1) * per_page < total:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"pp:{ctx}:pg:{page + 1}:{category or ''}"))
    if nav:
        kb.row(*nav)
    if total == 0:
        kb.row(InlineKeyboardButton(text="(немає товарів)", callback_data="noop"))
    return kb.as_markup()
