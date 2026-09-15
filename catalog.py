"""Залишки, Товари (картки), Партії (+ початкові залишки)."""
from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message

from .. import services as S
from ..db import get_db, today_local
from ..export import build_csv_stock
from ..keyboards import BACK, CAT_SHORT, M_BATCHES, M_PRODUCTS, M_STOCK, SKIP, inline, main_menu, nav_kb, product_picker
from ..money import ParseError, d, fmt_grams, fmt_money, fmt_price, parse_money, parse_weight_grams, round_cents
from .common import Flow, has_role, parse_date, ua_date

router = Router(name="catalog")


# ======================= Залишки =======================

def stock_text(db) -> str:
    rows = S.stock_summary(db)
    if not rows:
        return "Залишків немає."
    out = []
    cat = None
    total = Decimal(0)
    for r in rows:
        if r["product"]["category"] != cat:
            cat = r["product"]["category"]
            out.append(f"\n<b>{S.CATEGORIES[cat]}</b>")
        qty = fmt_grams(r["grams"])
        if r["product"]["sale_mode"] == "piece" and r["product"]["piece_grams"]:
            qty += f" ({r['grams'] // r['product']['piece_grams']} шт)"
        exp = f" ⏰{ua_date(r['nearest_expiry'])}" if r["nearest_expiry"] else ""
        out.append(f"• {r['product']['name']}: <b>{qty}</b> · {fmt_money(r['cost_value'])}{exp}")
        total += r["cost_value"]
    out.append(f"\nЗакупівельна вартість залишків: <b>{fmt_money(total)}</b>")
    return "\n".join(out).strip()


@router.message(F.text == M_STOCK)
async def stock(msg: Message, db, user):
    await msg.answer("📊 <b>Залишки</b>\n" + stock_text(db), reply_markup=main_menu(user["role"]))
    await msg.answer("Деталі:", reply_markup=inline([[("⏰ Спливає термін (7 днів)", "stock:exp"), ("📄 CSV залишків", "stock:csv")],
                                                    [("🔎 По товару (партії)", "stock:byprod")]]))


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
