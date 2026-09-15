"""Продаж: товар → вага/кількість → (ціна) → кошик → спосіб оплати = підтвердження."""
from __future__ import annotations

import uuid
from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import services as S
from ..db import get_db
from ..keyboards import BACK, M_SALE, inline, main_menu, nav_kb, product_picker
from ..money import ParseError, fmt_grams, fmt_money, fmt_price, line_amount, parse_money, parse_weight_grams, piece_amount, d
from .common import Flow, cancel_to_menu

router = Router(name="sales")


class Sale(StatesGroup):
    product = State()
    weight = State()
    pieces = State()
    price = State()
    cart = State()


async def p_product(msg: Message, state: FSMContext):
    data = await state.get_data()
    n = len(data.get("cart", []))
    txt = "🛒 Оберіть товар:" if not n else f"🛒 У кошику {n} поз. на {fmt_money(_cart_total(data))}. Додайте ще товар:"
    await msg.answer(txt, reply_markup=nav_kb(back=n > 0))
    await msg.answer("Категорія / товар:", reply_markup=product_picker(get_db(), "sale", data.get("cat")))


async def p_weight(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = data["cur"]
    have = S.stock_of_product(get_db(), p["id"])
    await msg.answer(
        f"⚖️ <b>{p['name']}</b> — {fmt_price(p['price'])} €/кг\nЗалишок: {fmt_grams(have)}\n"
        "Введіть вагу: <code>250</code> (г) або <code>0,25</code> (кг)",
        reply_markup=nav_kb("100", "200", "300", "500"),
    )


async def p_pieces(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = data["cur"]
    have = S.stock_of_product(get_db(), p["id"])
    pcs = have // p["piece_grams"] if p["piece_grams"] else 0
    await msg.answer(
        f"🔢 <b>{p['name']}</b> — {fmt_price(p['price'])} €/шт ({p['piece_grams']} г)\nЗалишок: {pcs} шт\nСкільки штук?",
        reply_markup=nav_kb("1", "2", "3", "4"),
    )


async def p_price(msg: Message, state: FSMContext):
    data = await state.get_data()
    p = data["cur"]
    unit = "шт" if p["mode"] == "piece" else "кг"
    await msg.answer(f"✏️ Нова ціна для цієї позиції, €/{unit} (зараз {fmt_price(p['price'])}):", reply_markup=nav_kb())


async def p_cart(msg: Message, state: FSMContext):
    data = await state.get_data()
    await msg.answer(cart_text(data), reply_markup=nav_kb())
    await msg.answer("Дія:", reply_markup=cart_kb(data))


flow = Flow({
    str(Sale.product): p_product, str(Sale.weight): p_weight, str(Sale.pieces): p_pieces,
    str(Sale.price): p_price, str(Sale.cart): p_cart,
})


def _cart_total(data) -> Decimal:
    return sum((Decimal(l["amount"]) for l in data.get("cart", [])), Decimal(0))


def cart_text(data) -> str:
    lines = data.get("cart", [])
    if not lines:
        return "Кошик порожній."
    out = ["🧾 <b>Покупка</b>"]
    for i, l in enumerate(lines, 1):
        qty = f"{l['pieces']} шт" if l.get("pieces") else fmt_grams(l["grams"])
        unit = "шт" if l.get("pieces") else "кг"
        out.append(f"{i}. {l['name']} — {qty} × {fmt_price(l['price'])} €/{unit} = <b>{fmt_money(l['amount'])}</b>")
    out.append(f"\nРазом: <b>{fmt_money(_cart_total(data))}</b>")
    return "\n".join(out)


def cart_kb(data):
    key = data["key"]
    rows = [[("💵 Готівка — підтвердити", f"sale:pay:cash:{key}"), ("💳 Картка — підтвердити", f"sale:pay:card:{key}")],
            [("➕ Ще товар", "sale:more")]]
    if data.get("cart"):
        rows.append([(f"🗑 Видалити поз. {i}", f"sale:del:{i}") for i in range(1, len(data["cart"]) + 1)][:4])
    return inline(rows)


def line_preview_kb():
    return inline([[("✅ Додати в кошик", "sale:add"), ("✏️ Інша ціна", "sale:chprice")]])


# ---------------- вхід ----------------

@router.message(F.text == M_SALE)
async def start_sale(msg: Message, state: FSMContext, user):
    await state.clear()
    await state.update_data(cart=[], key=uuid.uuid4().hex, cat=None)
    await flow.goto(msg, state, Sale.product, push=False)


@router.message(StateFilter(Sale), F.text == BACK)
async def back(msg: Message, state: FSMContext, user):
    await flow.back(msg, state, user)


# ---------------- вибір товару ----------------

@router.callback_query(StateFilter(Sale.product), F.data.startswith("pp:sale:"))
async def pick_product(cb: CallbackQuery, state: FSMContext, db):
    parts = cb.data.split(":")
    if parts[2] == "cat":
        await state.update_data(cat=parts[3])
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "sale", parts[3]))
        return await cb.answer()
    if parts[2] == "pg":
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "sale", parts[4] or None, int(parts[3])))
        return await cb.answer()
    p = S.get_product(db, int(parts[3]))
    if not p:
        return await cb.answer("Товар не знайдено", show_alert=True)
    await state.update_data(cur={"id": p["id"], "name": p["name"], "mode": p["sale_mode"],
                                 "piece_grams": p["piece_grams"], "price": str(d(p["retail_price"])),
                                 "grams": None, "pieces": None})
    await cb.answer()
    await flow.goto(cb.message, state, Sale.pieces if p["sale_mode"] == "piece" else Sale.weight)


@router.message(StateFilter(Sale.product), F.text)
async def search_product(msg: Message, state: FSMContext, db):
    found = S.find_products(db, msg.text)
    if not found:
        return await msg.answer("Нічого не знайдено. Оберіть із списку або уточніть назву.")
    await msg.answer("Знайдено:", reply_markup=inline([[(p["name"][:60], f"pp:sale:id:{p['id']}")] for p in found[:10]]))


# ---------------- вага / штуки ----------------

async def _show_preview(msg: Message, state: FSMContext):
    data = await state.get_data()
    cur = data["cur"]
    price = Decimal(cur["price"])
    if cur["mode"] == "piece":
        amt = piece_amount(cur["pieces"], price)
        qty = f"{cur['pieces']} шт ({fmt_grams(cur['grams'])})"
        unit = "шт"
    else:
        amt = line_amount(cur["grams"], price)
        qty = fmt_grams(cur["grams"])
        unit = "кг"
    await msg.answer(f"{cur['name']}: {qty} × {fmt_price(price)} €/{unit} = <b>{fmt_money(amt)}</b>",
                     reply_markup=line_preview_kb())


@router.message(StateFilter(Sale.weight), F.text)
async def got_weight(msg: Message, state: FSMContext, db):
    try:
        g = parse_weight_grams(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    cur = data["cur"]
    have = S.stock_of_product(db, cur["id"]) - _in_cart(data, cur["id"])
    if g > have:
        return await msg.answer(f"⚠️ Недостатньо залишку: доступно {fmt_grams(max(have, 0))}. Введіть меншу вагу.")
    cur["grams"] = g
    await state.update_data(cur=cur)
    await _show_preview(msg, state)


@router.message(StateFilter(Sale.pieces), F.text)
async def got_pieces(msg: Message, state: FSMContext, db):
    t = msg.text.strip()
    if not t.isdigit() or int(t) <= 0:
        return await msg.answer("⚠️ Введіть ціле число штук")
    n = int(t)
    data = await state.get_data()
    cur = data["cur"]
    g = n * cur["piece_grams"]
    have = S.stock_of_product(db, cur["id"]) - _in_cart(data, cur["id"])
    if g > have:
        return await msg.answer(f"⚠️ Доступно лише {max(have, 0) // cur['piece_grams']} шт.")
    cur["grams"], cur["pieces"] = g, n
    await state.update_data(cur=cur)
    await _show_preview(msg, state)


def _in_cart(data, product_id: int) -> int:
    return sum(l["grams"] for l in data.get("cart", []) if l["product_id"] == product_id)


@router.callback_query(StateFilter(Sale.weight, Sale.pieces, Sale.price), F.data == "sale:chprice")
async def change_price(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await flow.goto(cb.message, state, Sale.price)


@router.message(StateFilter(Sale.price), F.text)
async def got_price(msg: Message, state: FSMContext):
    try:
        price = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    if price <= 0:
        return await msg.answer("⚠️ Ціна має бути більшою за 0")
    data = await state.get_data()
    cur = data["cur"]
    cur["price"] = str(price)
    await state.update_data(cur=cur)
    await _show_preview(msg, state)


@router.callback_query(StateFilter(Sale.weight, Sale.pieces, Sale.price), F.data == "sale:add")
async def add_line(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    cur = data["cur"]
    if not cur.get("grams"):
        return await cb.answer("Спочатку введіть вагу/кількість", show_alert=True)
    price = Decimal(cur["price"])
    amt = piece_amount(cur["pieces"], price) if cur["mode"] == "piece" else line_amount(cur["grams"], price)
    cart = data.get("cart", [])
    cart.append({"product_id": cur["id"], "name": cur["name"], "grams": cur["grams"], "pieces": cur["pieces"],
                 "price": str(price), "amount": str(amt)})
    await state.update_data(cart=cart, cur=None, _stack=[])
    await cb.answer("Додано")
    await flow.goto(cb.message, state, Sale.cart, push=False)


# ---------------- кошик ----------------

@router.callback_query(StateFilter(Sale.cart), F.data == "sale:more")
async def more(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await flow.goto(cb.message, state, Sale.product)


@router.callback_query(StateFilter(Sale.cart), F.data.startswith("sale:del:"))
async def del_line(cb: CallbackQuery, state: FSMContext):
    i = int(cb.data.split(":")[2]) - 1
    data = await state.get_data()
    cart = data.get("cart", [])
    if 0 <= i < len(cart):
        cart.pop(i)
    await state.update_data(cart=cart)
    await cb.answer("Видалено")
    if not cart:
        return await flow.goto(cb.message, state, Sale.product, push=False)
    await cb.message.edit_text(cart_text(await state.get_data()))
    await cb.message.answer("Дія:", reply_markup=cart_kb(await state.get_data()))


@router.callback_query(F.data.startswith("sale:pay:"))
async def confirm_sale(cb: CallbackQuery, state: FSMContext, user, db):
    _, _, method, key = cb.data.split(":")
    data = await state.get_data()
    if data.get("key") != key or not data.get("cart"):
        # повторне натискання після завершення або стара кнопка
        return await cb.answer("Цей продаж уже проведено або скасовано", show_alert=True)
    lines = [S.SaleLine(l["product_id"], l["grams"], Decimal(l["price"]), l.get("pieces")) for l in data["cart"]]
    try:
        res = S.create_sale(db, user["telegram_id"], lines, method, client_key=f"sale:{key}")
    except S.DuplicateOperation as e:
        await state.clear()
        await cb.message.answer(f"ℹ️ {e}", reply_markup=main_menu(user["role"]))
        return await cb.answer()
    except S.InsufficientStock as e:
        return await cb.answer(str(e), show_alert=True)
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(
        f"✅ Продаж №{res.sale_id} проведено — <b>{fmt_money(res.total)}</b> ({S.PAYMENTS[method]})\n"
        "Це внутрішній запис, не фіскальний чек.",
        reply_markup=main_menu(user["role"]),
    )
    await cb.answer()
