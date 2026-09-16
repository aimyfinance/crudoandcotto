"""Залишки, Товари (картки), Партії (+ початкові залишки)."""
from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
import datetime as dt

from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message, WebAppInfo

from .. import services as S
from ..db import get_db, today_local
from ..export import build_csv_stock
from ..keyboards import BACK, CAT_SHORT, M_BATCHES, M_EXPIRY, M_OPENING, M_PRODUCTS, M_STOCK, SKIP, inline, main_menu, nav_kb, product_picker
from ..config import settings
from ..money import ParseError, d, fmt_grams, fmt_money, fmt_price, parse_money, parse_weight_grams, round_cents
from .common import Flow, has_role, parse_date, ua_date

router = Router(name="catalog")


# ======================= Залишки =======================

def _short(name: str, n: int = 18) -> str:
    return name if len(name) <= n else name[: n - 1] + "…"


def stock_text(db) -> str:
    """Компактна таблиця: назва · кг(шт) · € — моноширинним шрифтом, по категоріях, за вагою."""
    rows = S.stock_summary(db)
    if not rows:
        return "Залишків немає."
    out = []
    total = Decimal(0)
    by_cat: dict[str, list] = {}
    for r in rows:
        by_cat.setdefault(r["product"]["category"], []).append(r)
        total += r["cost_value"]
    for cat in ("meat", "cheese", "pasta"):
        items = sorted(by_cat.get(cat, []), key=lambda r: -r["grams"])
        if not items:
            continue
        cat_total = sum((r["cost_value"] for r in items), Decimal(0))
        out.append(f"<b>{S.CATEGORIES[cat]}</b> · {len(items)} поз. · {fmt_money(cat_total)}")
        lines = []
        for r in items:
            p = r["product"]
            if p["sale_mode"] == "piece" and p["piece_grams"] and r["grams"] >= p["piece_grams"]:
                qty = f"{r['grams'] // p['piece_grams']:>3} шт"
            else:
                qty = f"{r['grams'] / 1000:>5.2f}".replace(".", ",") + " кг" if r["grams"] >= 1000 else f"{r['grams']:>5} г "
            flag = "⏰" if r["nearest_expiry"] and r["nearest_expiry"] <= (dt.date.today() + dt.timedelta(days=7)).isoformat() else " "
            lines.append(f"{_short(p['name']):<18} {qty:>8} {float(r['cost_value']):>7.0f}€{flag}")
        out.append("<pre>" + "\n".join(lines) + "</pre>")
    out.append(f"Разом: <b>{fmt_grams(sum(r['grams'] for r in rows))}</b> на <b>{fmt_money(total)}</b> (закупівельна)")
    return "\n".join(out)


@router.message(F.text == M_STOCK)
async def stock(msg: Message, db, user):
    await msg.answer("📊 <b>Залишки</b>\n" + stock_text(db))
    rows = [[("⚠️ Мало на складі", "stock:low"), ("⏰ Спливає термін", "stock:exp")],
            [("🔎 По товару (партії)", "stock:byprod"), ("📄 CSV", "stock:csv")]]
    kb = inline(rows)
    if settings.webapp_url:
        kb.inline_keyboard.append([InlineKeyboardButton(text="📱 Зручніше — у застосунку", web_app=WebAppInfo(url=settings.webapp_url + "/app"))])
    await msg.answer("Деталі:", reply_markup=kb)


@router.message(StateFilter(None), F.text == M_EXPIRY)
async def stock_expiry_msg(msg: Message, db):
    rows = S.batches_expiring(db, 14)
    if not rows:
        return await msg.answer("Найближчі 14 днів терміни не спливають.")
    await msg.answer("⏰ <b>Терміни придатності (14 днів)</b>\n" + "\n".join(
        f"• {ua_date(r['expiry_date'])} — {r['product_name']}: {fmt_grams(r['grams_left'])}" for r in rows))


@router.callback_query(F.data == "stock:low")
async def stock_low(cb: CallbackQuery, db):
    rows = [r for r in S.stock_summary(db, include_zero=True)
            if (r["product"]["sale_mode"] == "piece" and r["product"]["piece_grams"] and r["grams"] < 3 * r["product"]["piece_grams"])
            or (r["product"]["sale_mode"] == "weight" and r["grams"] < 500)]
    txt = "⚠️ <b>Мало або немає</b> (менше 500 г / 3 шт)\n" + ("\n".join(
        f"• {r['product']['name']}: {fmt_grams(r['grams']) if r['grams'] else 'немає'}" for r in rows) if rows else "усього достатньо")
    await cb.message.answer(txt)
    await cb.answer()


@router.message(StateFilter(None), F.text == M_OPENING)
async def opening_start_msg(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await msg.answer("Недостатньо прав")
    await state.clear()
    await oflow.goto(msg, state, Opening.product, push=False)


@router.callback_query(F.data == "stock:exp")
async def stock_exp(cb: CallbackQuery, db):
    rows = S.batches_expiring(db, 7)
    if not rows:
        await cb.message.answer("Найближчі 7 днів терміни не спливають.")
    else:
        await cb.message.answer("⏰ <b>Спливає термін</b>\n" + "\n".join(
            f"• {r['product_name']} — {fmt_grams(r['grams_left'])}, до {ua_date(r['expiry_date'])} (партія {r['batch_code'] or '#' + str(r['id'])})"
            for r in rows))
    await cb.answer()


@router.callback_query(F.data == "stock:csv")
async def stock_csv(cb: CallbackQuery, db):
    await cb.message.answer_document(BufferedInputFile(build_csv_stock(db), filename=f"stock_{today_local()}.csv"))
    await cb.answer()


@router.callback_query(F.data == "stock:byprod")
async def stock_byprod(cb: CallbackQuery, db):
    await cb.message.answer("Оберіть товар:", reply_markup=product_picker(db, "bt", show_price=False))
    await cb.answer()


# ======================= Партії =======================

def batches_text(db, product_id: int) -> str:
    p = S.get_product(db, product_id)
    rows = S.batches_of_product(db, product_id)
    if not rows:
        return f"<b>{p['name']}</b>: відкритих партій немає."
    out = [f"🏷 <b>{p['name']}</b> — партії (порядок FIFO):"]
    for i, b in enumerate(rows, 1):
        val = round_cents(Decimal(b["grams_left"]) * d(b["landed_price_per_kg"]) / 1000)
        exp = f", до {ua_date(b['expiry_date'])}" if b["expiry_date"] else ""
        extra = "" if d(b["landed_price_per_kg"]) == d(b["price_per_kg"]) else f" (собів. {fmt_price(b['landed_price_per_kg'])})"
        out.append(f"{i}. #{b['id']} {b['batch_code'] or ''} від {ua_date(b['received_at'])}: <b>{fmt_grams(b['grams_left'])}</b> "
                   f"з {fmt_grams(b['grams_in'])} · {fmt_price(b['price_per_kg'])} €/кг{extra} · {fmt_money(val)}{exp}")
    return "\n".join(out)


@router.message(F.text == M_BATCHES)
async def batches(msg: Message, db, user, state: FSMContext):
    await state.clear()
    await msg.answer("🏷 Партії — оберіть товар:", reply_markup=main_menu(user["role"]))
    kb = product_picker(db, "bt", show_price=False)
    if has_role(user, "manager"):
        kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Початковий залишок", callback_data="bt:opening")])
    await msg.answer("Товар:", reply_markup=kb)


@router.callback_query(F.data.startswith("pp:bt:"))
async def batches_pick(cb: CallbackQuery, db):
    parts = cb.data.split(":")
    if parts[2] == "cat":
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "bt", parts[3], show_price=False))
        return await cb.answer()
    if parts[2] == "pg":
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "bt", parts[4] or None, int(parts[3]), show_price=False))
        return await cb.answer()
    await cb.message.answer(batches_text(db, int(parts[3])))
    await cb.answer()


class Opening(StatesGroup):
    product = State()
    weight = State()
    price = State()
    expiry = State()


async def o_product(msg, state):
    await msg.answer("Початковий залишок — оберіть товар:", reply_markup=nav_kb(back=False))
    await msg.answer("Товар:", reply_markup=product_picker(get_db(), "op", show_price=False))


async def o_weight(msg, state):
    await msg.answer("Вага залишку (кг або г):", reply_markup=nav_kb())


async def o_price(msg, state):
    await msg.answer("Закупівельна ціна за кг, € (собівартість цього залишку):", reply_markup=nav_kb())


async def o_expiry(msg, state):
    await msg.answer("Термін придатності або пропустіть:", reply_markup=nav_kb(SKIP))


oflow = Flow({str(Opening.product): o_product, str(Opening.weight): o_weight, str(Opening.price): o_price,
              str(Opening.expiry): o_expiry})


@router.callback_query(F.data == "bt:opening")
async def opening_start(cb: CallbackQuery, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await cb.answer()
    await oflow.goto(cb.message, state, Opening.product, push=False)


@router.message(StateFilter(Opening), F.text == BACK)
async def o_back(msg: Message, state: FSMContext, user):
    await oflow.back(msg, state, user)


@router.callback_query(StateFilter(Opening.product), F.data.startswith("pp:op:"))
async def o_pick(cb: CallbackQuery, state: FSMContext, db):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "op", cat, page, show_price=False))
        return await cb.answer()
    p = S.get_product(db, int(parts[3]))
    await state.update_data(pid=p["id"], pname=p["name"])
    await cb.answer()
    await oflow.goto(cb.message, state, Opening.weight)


@router.message(StateFilter(Opening.weight), F.text)
async def o_w(msg: Message, state: FSMContext):
    try:
        g = parse_weight_grams(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.update_data(grams=g)
    await oflow.goto(msg, state, Opening.price)


@router.message(StateFilter(Opening.price), F.text)
async def o_p(msg: Message, state: FSMContext):
    try:
        p = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.update_data(price=str(p))
    await oflow.goto(msg, state, Opening.expiry)


@router.message(StateFilter(Opening.expiry), F.text)
async def o_e(msg: Message, state: FSMContext, user, db):
    exp = None
    if msg.text != SKIP:
        exp = parse_date(msg.text)
        if not exp:
            return await msg.answer("⚠️ Дата як 30.11.2026 або «Пропустити»")
    data = await state.get_data()
    bid = S.add_opening_stock(db, user["telegram_id"], data["pid"], data["grams"], Decimal(data["price"]), exp)
    await state.clear()
    await msg.answer(f"✅ Партія #{bid}: {data['pname']} {fmt_grams(data['grams'])} по {fmt_price(data['price'])} €/кг додана.",
                     reply_markup=main_menu(user["role"]))


# ======================= Товари =======================

MODE_UA = {"weight": "на вагу", "piece": "поштучно"}


def product_card(p) -> str:
    unit = "шт" if p["sale_mode"] == "piece" else "кг"
    pg = f" · упаковка {p['piece_grams']} г" if p["sale_mode"] == "piece" else ""
    st = "" if p["active"] else " · <i>архів</i>"
    sku = f" · арт. {p['sku']}" if p["sku"] else ""
    return f"<b>{p['name']}</b>{st}\n{S.CATEGORIES[p['category']]} · {MODE_UA[p['sale_mode']]}{pg}{sku}\nРоздрібна ціна: <b>{fmt_price(p['retail_price'])} €/{unit}</b>"


def product_kb(p):
    rows = [[("💶 Ціна", f"prod:price:{p['id']}"), ("✏️ Назва", f"prod:name:{p['id']}")],
            [("📦 Упаковка, г", f"prod:pg:{p['id']}"), ("🔢 Артикул", f"prod:sku:{p['id']}")],
            [("🗄 В архів" if p["active"] else "♻️ Відновити", f"prod:toggle:{p['id']}")]]
    return inline(rows)


@router.message(F.text == M_PRODUCTS)
async def products(msg: Message, db, user, state: FSMContext):
    await state.clear()
    kb = product_picker(db, "pr", show_price=True)
    if has_role(user, "manager"):
        kb.inline_keyboard.append([InlineKeyboardButton(text="➕ Новий товар", callback_data="prod:new"),
                                   InlineKeyboardButton(text="🗄 Архів", callback_data="prod:archive")])
    await msg.answer("🧀 <b>Товари</b> — оберіть для перегляду/редагування:", reply_markup=main_menu(user["role"]))
    await msg.answer("Товар:", reply_markup=kb)


@router.callback_query(F.data.startswith("pp:pr:"))
async def products_pick(cb: CallbackQuery, db, user):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "pr", cat, page))
        return await cb.answer()
    p = S.get_product(db, int(parts[3]))
    await cb.message.answer(product_card(p), reply_markup=product_kb(p) if has_role(user, "manager") else None)
    await cb.answer()


@router.callback_query(F.data == "prod:archive")
async def products_archive(cb: CallbackQuery, db):
    rows = [p for p in S.list_products(db, active_only=False) if not p["active"]]
    if not rows:
        await cb.message.answer("Архів порожній.")
    else:
        await cb.message.answer("Архівні товари:", reply_markup=inline([[(p["name"][:60], f"pp:pr:id:{p['id']}")] for p in rows]))
    await cb.answer()


class Prod(StatesGroup):
    name = State()
    category = State()
    mode = State()
    piece_grams = State()
    price = State()
    sku = State()
    edit_value = State()


async def n_name(msg, state):
    await msg.answer("➕ Назва нового товару:", reply_markup=nav_kb(back=False))


async def n_cat(msg, state):
    await msg.answer("Категорія:", reply_markup=nav_kb())
    await msg.answer("Оберіть:", reply_markup=inline([[(t, f"prod:cat:{c}")] for c, t in CAT_SHORT.items()]))


async def n_mode(msg, state):
    await msg.answer("Спосіб продажу:", reply_markup=nav_kb())
    await msg.answer("Оберіть:", reply_markup=inline([[("⚖️ На вагу (€/кг)", "prod:mode:weight"), ("📦 Поштучно (€/шт)", "prod:mode:piece")]]))


async def n_pg(msg, state):
    await msg.answer("Вага однієї упаковки, г (наприклад 300):", reply_markup=nav_kb())


async def n_price(msg, state):
    data = await state.get_data()
    unit = "шт" if data.get("mode") == "piece" else "кг"
    await msg.answer(f"Роздрібна ціна, €/{unit} (брутто):", reply_markup=nav_kb())


async def n_sku(msg, state):
    await msg.answer("Артикул (або пропустіть):", reply_markup=nav_kb(SKIP))


pflow = Flow({str(Prod.name): n_name, str(Prod.category): n_cat, str(Prod.mode): n_mode, str(Prod.piece_grams): n_pg,
              str(Prod.price): n_price, str(Prod.sku): n_sku})


@router.callback_query(F.data == "prod:new")
async def prod_new(cb: CallbackQuery, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await cb.answer()
    await pflow.goto(cb.message, state, Prod.name, push=False)


@router.message(StateFilter(Prod), F.text == BACK)
async def p_back(msg: Message, state: FSMContext, user):
    await pflow.back(msg, state, user)


@router.message(StateFilter(Prod.name), F.text)
async def p_name(msg: Message, state: FSMContext, db):
    name = msg.text.strip()[:80]
    if db.one("SELECT 1 FROM products WHERE lower(name)=lower(?)", (name,)):
        return await msg.answer("⚠️ Товар із такою назвою вже є")
    await state.update_data(name=name)
    await pflow.goto(msg, state, Prod.category)


@router.callback_query(StateFilter(Prod.category), F.data.startswith("prod:cat:"))
async def p_cat(cb: CallbackQuery, state: FSMContext):
    await state.update_data(category=cb.data.split(":")[2])
    await cb.answer()
    await pflow.goto(cb.message, state, Prod.mode)


@router.callback_query(StateFilter(Prod.mode), F.data.startswith("prod:mode:"))
async def p_mode(cb: CallbackQuery, state: FSMContext):
    mode = cb.data.split(":")[2]
    await state.update_data(mode=mode)
    await cb.answer()
    await pflow.goto(cb.message, state, Prod.piece_grams if mode == "piece" else Prod.price)


@router.message(StateFilter(Prod.piece_grams), F.text)
async def p_pg(msg: Message, state: FSMContext):
    if not msg.text.strip().isdigit() or int(msg.text) <= 0:
        return await msg.answer("⚠️ Введіть ціле число грамів")
    await state.update_data(piece_grams=int(msg.text))
    await pflow.goto(msg, state, Prod.price)


@router.message(StateFilter(Prod.price), F.text)
async def p_price(msg: Message, state: FSMContext):
    try:
        p = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.update_data(price=str(p))
    await pflow.goto(msg, state, Prod.sku)


@router.message(StateFilter(Prod.sku), F.text)
async def p_sku(msg: Message, state: FSMContext, db, user):
    data = await state.get_data()
    sku = None if msg.text == SKIP else msg.text.strip()[:40]
    pid = S.create_product(db, data["name"], data["category"], data["mode"], Decimal(data["price"]),
                           data.get("piece_grams"), sku)
    await state.clear()
    await msg.answer("✅ Товар створено.\n" + product_card(S.get_product(db, pid)), reply_markup=main_menu(user["role"]))


# --- редагування полів картки ---

EDIT_PROMPTS = {"price": "Нова роздрібна ціна, €:", "name": "Нова назва:", "pg": "Вага упаковки, г:", "sku": "Артикул:"}


@router.callback_query(F.data.startswith("prod:"))
async def prod_edit(cb: CallbackQuery, state: FSMContext, db, user):
    parts = cb.data.split(":")
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    if len(parts) < 3 or parts[1] not in ("price", "name", "pg", "sku", "toggle"):
        return await cb.answer()
    pid = int(parts[2])
    if parts[1] == "toggle":
        p = S.get_product(db, pid)
        S.update_product(db, pid, active=0 if p["active"] else 1)
        p = S.get_product(db, pid)
        await cb.message.edit_text(product_card(p), reply_markup=product_kb(p))
        return await cb.answer("Оновлено")
    await state.set_state(Prod.edit_value)
    await state.update_data(edit_field=parts[1], edit_pid=pid)
    await cb.message.answer(EDIT_PROMPTS[parts[1]], reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Prod.edit_value), F.text)
async def prod_edit_value(msg: Message, state: FSMContext, db, user):
    data = await state.get_data()
    f, pid = data["edit_field"], data["edit_pid"]
    try:
        if f == "price":
            S.update_product(db, pid, retail_price=parse_money(msg.text))
        elif f == "name":
            S.update_product(db, pid, name=msg.text.strip()[:80])
        elif f == "pg":
            if not msg.text.strip().isdigit():
                raise ParseError("Ціле число грамів")
            S.update_product(db, pid, piece_grams=int(msg.text), sale_mode="piece")
        elif f == "sku":
            S.update_product(db, pid, sku=msg.text.strip()[:40] or None)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося зберегти: {e}")
    await state.clear()
    await msg.answer("✅ Збережено.\n" + product_card(S.get_product(db, pid)), reply_markup=main_menu(user["role"]))
