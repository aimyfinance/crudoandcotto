from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from . import services as S
from .config import settings
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
M_REPORTS = "📊 Усі звіти та Excel"
M_SETTINGS = "⚙️ Налаштування"
M_TASKS = "📋 Завдання"
M_SHIFT_OPEN = "▶️ Почати зміну"
M_SHIFT_CLOSE = "⏹ Завершити зміну"

# групи головного меню
G_CASH = "🧾 Каса"
G_PURCH = "📦 Закупівлі"
G_STOCK = "📊 Склад"
G_EXP = "💸 Витрати"
G_REP = "📈 Звіти"
G_HOME = "🏠 Головне меню"
# додаткові дії всередині груп
M_INVOICE = "📄 Закупівля з інвойсу"
M_PURCH_MANUAL = "✍️ Закупівля вручну"
M_DOCS_PURCH = "🗂 Інвойси"
M_EXPIRY = "⏰ Терміни придатності"
M_OPENING = "➕ Початковий залишок"
M_EXP_ADD = "➕ Додати витрату"
M_EXP_LIST = "🗑 Останні витрати"
M_DOCS_EXP = "🗂 Чеки витрат"
M_EXP_SUMMARY = "📊 Резюме витрат"
M_REP_TODAY = "📅 Звіт за сьогодні"
M_REP_MONTH = "📆 Звіт за місяць"
M_REP_PERIOD = "🔎 Період"
GROUPS = {G_CASH, G_PURCH, G_STOCK, G_EXP, G_REP, G_HOME}

ROLE_MENUS = {
    "admin": [[M_TASKS, G_CASH], [G_PURCH, G_STOCK], [G_EXP, G_REP], [M_SETTINGS]],
    "manager": [[M_TASKS, G_CASH], [G_PURCH, G_STOCK], [G_EXP, G_REP], [M_SETTINGS]],
    "seller": [[M_TASKS, G_CASH], [G_STOCK]],
}
SUBMENUS = {
    G_CASH: {"seller": [[M_SALE, M_HISTORY]], "manager": [[M_SALE, M_HISTORY]]},
    G_PURCH: {"manager": [[M_INVOICE, M_PURCH_MANUAL], [M_BATCHES, M_DOCS_PURCH], [M_PURCHASE + " (останні)"]]},
    G_STOCK: {"seller": [[M_STOCK, M_EXPIRY], [M_BATCHES]],
              "manager": [[M_STOCK, M_EXPIRY], [M_WRITEOFF, M_PRODUCTS], [M_BATCHES, M_OPENING]]},
    G_EXP: {"manager": [[M_EXP_SUMMARY], [M_EXP_ADD, M_EXP_LIST], [M_DOCS_EXP]]},
    G_REP: {"manager": [[M_REP_TODAY, M_REP_MONTH], [M_REP_PERIOD, M_REPORTS]]},
}
MENU_BUTTONS = {M_SALE, M_PURCHASE, M_STOCK, M_PRODUCTS, M_BATCHES, M_WRITEOFF, M_HISTORY, M_REPORTS, M_SETTINGS, M_TASKS}


M_APP = "📱 Застосунок каси"


def _shift_btn() -> str:
    try:
        from .db import get_db
        return M_SHIFT_CLOSE if S.current_shift(get_db()) else M_SHIFT_OPEN
    except Exception:
        return M_SHIFT_OPEN


def main_menu(role: str) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=t) for t in r] for r in ROLE_MENUS.get(role, ROLE_MENUS["seller"])]
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def submenu(group: str, role: str) -> ReplyKeyboardMarkup | None:
    spec = SUBMENUS.get(group)
    if not spec:
        return None
    lvl = "seller" if role == "seller" else "manager"
    rows_spec = spec.get(lvl)
    if rows_spec is None:
        return None
    rows = []
    if group == G_CASH:
        first = [KeyboardButton(text=_shift_btn())]
        if settings.webapp_url:
            first.insert(0, KeyboardButton(text=M_APP, web_app=WebAppInfo(url=settings.webapp_url + "/app")))
        rows.append(first)
    rows += [[KeyboardButton(text=t) for t in r] for r in rows_spec]
    rows.append([KeyboardButton(text=G_HOME)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


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
