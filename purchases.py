"""Закупівля: дата → постачальник → [товар → вага → ціна/кг → термін → партія]* → дод. витрати → підтвердження."""
from __future__ import annotations

from decimal import Decimal

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import services as S
from ..db import get_db, today_local
from ..keyboards import BACK, M_PURCHASE, SKIP, TODAY, inline, main_menu, nav_kb, product_picker
from ..money import ParseError, fmt_grams, fmt_money, fmt_price, line_amount, parse_money, parse_weight_grams
from .common import Flow, has_role, parse_date, ua_date

router = Router(name="purchases")


class Pur(StatesGroup):
    date = State()
    supplier = State()
    product = State()
    weight = State()
    price = State()
    expiry = State()
    batch = State()
    summary = State()
    extra = State()
    comment = State()


async def p_date(msg, state):
    await msg.answer("📦 Дата закупівлі (наприклад 15.09.2026):", reply_markup=nav_kb(TODAY, back=False))


async def p_supplier(msg, state):
    sups = S.list_suppliers(get_db())
    kb = inline([[(s["name"], f"pur:sup:{s['id']}")] for s in sups[:12]]) if sups else None
    await msg.answer("Постачальник — введіть назву або оберіть:", reply_markup=nav_kb())
    if kb:
        await msg.answer("Останні постачальники:", reply_markup=kb)


async def p_product(msg, state):
    data = await state.get_data()
    n = len(data.get("lines", []))
    await msg.answer(f"Товар №{n + 1} — оберіть:" if n else "Оберіть товар:", reply_markup=nav_kb())
    await msg.answer("Категорія / товар:", reply_markup=product_picker(get_db(), "pur", data.get("cat"), show_price=False))


async def p_weight(msg, state):
    data = await state.get_data()
    await msg.answer(f"⚖️ <b>{data['cur']['name']}</b>\nВага закупівлі: <code>10,5</code> (кг) або <code>10500</code> (г):",
                     reply_markup=nav_kb())


async def p_price(msg, state):
    data = await state.get_data()
    await msg.answer(f"💶 Ціна постачальника за кг, € ({fmt_grams(data['cur']['grams'])}):", reply_markup=nav_kb())


async def p_expiry(msg, state):
    await msg.answer("📅 Термін придатності (дата) або пропустіть:", reply_markup=nav_kb(SKIP))


async def p_batch(msg, state):
    await msg.answer("🏷 Номер партії / лот (за етикеткою) або пропустіть:", reply_markup=nav_kb(SKIP))


async def p_summary(msg, state):
    data = await state.get_data()
    await msg.answer(summary_text(data), reply_markup=nav_kb())
    await msg.answer("Дія:", reply_markup=inline([
        [("➕ Ще товар", "pur:more"), ("🚚 Дод. витрати", "pur:extra")],
        [("💬 Коментар", "pur:comment")],
        [("✅ Підтвердити надходження", "pur:confirm")],
    ]))


async def p_extra(msg, state):
    data = await state.get_data()
    await msg.answer(
        f"🚚 Додаткові витрати закупівлі (транспорт, дорога тощо), € — зараз {fmt_money(data.get('extra', '0'))}.\n"
        "Розподіляться на всі позиції пропорційно вазі і ввійдуть у собівартість. Введіть суму (0 — немає):",
        reply_markup=nav_kb())


async def p_comment(msg, state):
    await msg.answer("💬 Коментар до закупівлі:", reply_markup=nav_kb(SKIP))


flow = Flow({str(getattr(Pur, n)): fn for n, fn in [
    ("date", p_date), ("supplier", p_supplier), ("product", p_product), ("weight", p_weight), ("price", p_price),
    ("expiry", p_expiry), ("batch", p_batch), ("summary", p_summary), ("extra", p_extra), ("comment", p_comment)]})


def summary_text(data) -> str:
    out = [f"📦 <b>Закупівля</b> {ua_date(data['date'])} · {data.get('supplier') or '—'}"]
    total_g, total = 0, Decimal(0)
    for i, l in enumerate(data.get("lines", []), 1):
        amt = line_amount(l["grams"], Decimal(l["price"]))
        total += amt
        total_g += l["grams"]
        exp = f", до {ua_date(l['expiry'])}" if l.get("expiry") else ""
        bc = f", лот {l['batch']}" if l.get("batch") else ""
        out.append(f"{i}. {l['name']} — {fmt_grams(l['grams'])} × {fmt_price(l['price'])} €/кг = {fmt_money(amt)}{exp}{bc}")
    extra = Decimal(data.get("extra", "0"))
    out.append(f"\nТовар: <b>{fmt_money(total)}</b> ({fmt_grams(total_g)})")
    if extra:
        out.append(f"Дод. витрати: {fmt_money(extra)} (+{fmt_price((extra * 1000 / total_g).quantize(Decimal('0.0001')))} €/кг)")
        out.append(f"Разом із витратами: <b>{fmt_money(total + extra)}</b>")
    if data.get("comment"):
        out.append(f"Коментар: {data['comment']}")
    return "\n".join(out)


@router.message(F.text == M_PURCHASE)
async def start(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await msg.answer("Закупівлі доступні менеджеру й адміністратору.")
    await state.clear()
    await state.update_data(lines=[], extra="0", cat=None)
    await flow.goto(msg, state, Pur.date, push=False)


@router.message(StateFilter(Pur), F.text == BACK)
async def back(msg: Message, state: FSMContext, user):
    await flow.back(msg, state, user)


@router.message(StateFilter(Pur.date), F.text)
async def got_date(msg: Message, state: FSMContext):
    d_ = today_local() if msg.text == TODAY else parse_date(msg.text)
    if not d_:
        return await msg.answer("⚠️ Введіть дату як 15.09.2026")
    await state.update_data(date=d_)
    await flow.goto(msg, state, Pur.supplier)


@router.callback_query(StateFilter(Pur.supplier), F.data.startswith("pur:sup:"))
async def pick_supplier(cb: CallbackQuery, state: FSMContext, db):
    s = db.one("SELECT name FROM suppliers WHERE id=?", (int(cb.data.split(":")[2]),))
    await state.update_data(supplier=s["name"])
    await cb.answer()
    await flow.goto(cb.message, state, Pur.product)


@router.message(StateFilter(Pur.supplier), F.text)
async def got_supplier(msg: Message, state: FSMContext):
    await state.update_data(supplier=msg.text.strip()[:80])
    await flow.goto(msg, state, Pur.product)


@router.callback_query(StateFilter(Pur.product), F.data.startswith("pp:pur:"))
async def pick_product(cb: CallbackQuery, state: FSMContext, db):
    parts = cb.data.split(":")
    if parts[2] == "cat":
        await state.update_data(cat=parts[3])
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "pur", parts[3], show_price=False))
        return await cb.answer()
    if parts[2] == "pg":
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "pur", parts[4] or None, int(parts[3]), show_price=False))
        return await cb.answer()
    p = S.get_product(db, int(parts[3]))
    await state.update_data(cur={"id": p["id"], "name": p["name"]})
    await cb.answer()
    await flow.goto(cb.message, state, Pur.weight)


@router.message(StateFilter(Pur.product), F.text)
async def search(msg: Message, state: FSMContext, db):
    found = S.find_products(db, msg.text)
    if not found:
        return await msg.answer("Не знайдено. Новий товар створюйте в меню «Товари».")
    await msg.answer("Знайдено:", reply_markup=inline([[(p["name"][:60], f"pp:pur:id:{p['id']}")] for p in found[:10]]))


@router.message(StateFilter(Pur.weight), F.text)
async def got_weight(msg: Message, state: FSMContext):
    try:
        g = parse_weight_grams(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    data["cur"]["grams"] = g
    await state.update_data(cur=data["cur"])
    await flow.goto(msg, state, Pur.price)


@router.message(StateFilter(Pur.price), F.text)
async def got_price(msg: Message, state: FSMContext):
    try:
        p = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    data["cur"]["price"] = str(p)
    await state.update_data(cur=data["cur"])
    await flow.goto(msg, state, Pur.expiry)


@router.message(StateFilter(Pur.expiry), F.text)
async def got_expiry(msg: Message, state: FSMContext):
    data = await state.get_data()
    if msg.text == SKIP:
        data["cur"]["expiry"] = None
    else:
        d_ = parse_date(msg.text)
        if not d_:
            return await msg.answer("⚠️ Дата як 30.11.2026 або «Пропустити»")
        data["cur"]["expiry"] = d_
    await state.update_data(cur=data["cur"])
    await flow.goto(msg, state, Pur.batch)


@router.message(StateFilter(Pur.batch), F.text)
async def got_batch(msg: Message, state: FSMContext):
    data = await state.get_data()
    cur = data["cur"]
    cur["batch"] = None if msg.text == SKIP else msg.text.strip()[:40]
    lines = data.get("lines", [])
    lines.append(cur)
    await state.update_data(lines=lines, cur=None, _stack=[])
    await flow.goto(msg, state, Pur.summary, push=False)


@router.callback_query(StateFilter(Pur.summary), F.data == "pur:more")
async def more(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await flow.goto(cb.message, state, Pur.product)


@router.callback_query(StateFilter(Pur.summary), F.data == "pur:extra")
async def extra(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await flow.goto(cb.message, state, Pur.extra)


@router.message(StateFilter(Pur.extra), F.text)
async def got_extra(msg: Message, state: FSMContext):
    try:
        v = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.update_data(extra=str(v), _stack=[])
    await flow.goto(msg, state, Pur.summary, push=False)


@router.callback_query(StateFilter(Pur.summary), F.data == "pur:comment")
async def comment(cb: CallbackQuery, state: FSMContext):
    await cb.answer()
    await flow.goto(cb.message, state, Pur.comment)


@router.message(StateFilter(Pur.comment), F.text)
async def got_comment(msg: Message, state: FSMContext):
    await state.update_data(comment=None if msg.text == SKIP else msg.text.strip()[:200], _stack=[])
    await flow.goto(msg, state, Pur.summary, push=False)


@router.callback_query(StateFilter(Pur.summary), F.data == "pur:confirm")
async def confirm(cb: CallbackQuery, state: FSMContext, user, db):
    data = await state.get_data()
    if not data.get("lines"):
        return await cb.answer("Немає позицій", show_alert=True)
    lines = [S.PurchaseLine(l["id"], l["grams"], Decimal(l["price"]), l.get("expiry"), l.get("batch")) for l in data["lines"]]
    pid = S.create_purchase(db, user["telegram_id"], data["date"], data.get("supplier", ""), lines,
                            Decimal(data.get("extra", "0")), data.get("comment"), receive=True)
    await state.clear()
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass
    await cb.message.answer(f"✅ Закупівлю №{pid} проведено, залишки збільшено ({len(lines)} партій).",
                            reply_markup=main_menu(user["role"]))
    await cb.answer()
