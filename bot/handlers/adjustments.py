"""Списання з причиною, інвентаризаційне коригування, скасування операцій, історія."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import services as S
from ..db import get_db, local_dt_str, today_local
from ..keyboards import BACK, M_HISTORY, M_WRITEOFF, inline, main_menu, nav_kb, product_picker
from ..money import ParseError, fmt_grams, fmt_money, fmt_price, parse_weight_grams, d
from .common import Flow, has_role, ua_date

router = Router(name="adjustments")

REASONS = ["Зіпсувалось / термін", "Дегустація / проба", "Усушка / обрізки", "Власне споживання", "Інше"]


class WO(StatesGroup):
    product = State()
    weight = State()
    reason = State()
    reason_text = State()
    confirm = State()


class Inv(StatesGroup):
    product = State()
    batch = State()
    actual = State()
    reason = State()


class Cancel(StatesGroup):
    pick = State()
    reason = State()


# ---------------- меню ----------------

@router.message(F.text == M_WRITEOFF)
async def menu(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await msg.answer("Списання і коригування доступні менеджеру й адміністратору.")
    await state.clear()
    await msg.answer("✂️ <b>Списання / коригування</b>", reply_markup=main_menu(user["role"]))
    await msg.answer("Оберіть дію:", reply_markup=inline([
        [("✂️ Списати з причиною", "wo:start")],
        [("📋 Інвентаризація (факт. залишок партії)", "inv:start")],
        [("↩️ Скасувати помилкову операцію", "cx:start")],
    ]))


# ---------------- списання ----------------

async def w_product(msg, state):
    await msg.answer("Списання — оберіть товар:", reply_markup=nav_kb(back=False))
    await msg.answer("Товар:", reply_markup=product_picker(get_db(), "wo", show_price=False))


async def w_weight(msg, state):
    data = await state.get_data()
    have = S.stock_of_product(get_db(), data["pid"])
    await msg.answer(f"<b>{data['pname']}</b>, залишок {fmt_grams(have)}.\nВага списання (г або кг):", reply_markup=nav_kb())


async def w_reason(msg, state):
    await msg.answer("Причина:", reply_markup=nav_kb())
    await msg.answer("Оберіть:", reply_markup=inline([[(r, f"wo:reason:{i}")] for i, r in enumerate(REASONS)]))


async def w_reason_text(msg, state):
    await msg.answer("Опишіть причину:", reply_markup=nav_kb())


async def w_confirm(msg, state):
    data = await state.get_data()
    await msg.answer(f"Списати <b>{data['pname']}</b> — {fmt_grams(data['grams'])}\nПричина: {data['reason']}\n(партія — FIFO)",
                     reply_markup=nav_kb())
    await msg.answer("Підтвердити?", reply_markup=inline([[("✅ Списати", "wo:confirm")]]))


wflow = Flow({str(WO.product): w_product, str(WO.weight): w_weight, str(WO.reason): w_reason,
              str(WO.reason_text): w_reason_text, str(WO.confirm): w_confirm})


@router.callback_query(F.data == "wo:start")
async def wo_start(cb: CallbackQuery, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await cb.answer()
    await wflow.goto(cb.message, state, WO.product, push=False)


@router.message(StateFilter(WO), F.text == BACK)
async def wo_back(msg: Message, state: FSMContext, user):
    await wflow.back(msg, state, user)


@router.callback_query(StateFilter(WO.product), F.data.startswith("pp:wo:"))
async def wo_pick(cb: CallbackQuery, state: FSMContext, db):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "wo", cat, page, show_price=False))
        return await cb.answer()
    p = S.get_product(db, int(parts[3]))
    await state.update_data(pid=p["id"], pname=p["name"])
    await cb.answer()
    await wflow.goto(cb.message, state, WO.weight)


@router.message(StateFilter(WO.weight), F.text)
async def wo_weight(msg: Message, state: FSMContext, db):
    try:
        g = parse_weight_grams(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    have = S.stock_of_product(db, data["pid"])
    if g > have:
        return await msg.answer(f"⚠️ Залишок лише {fmt_grams(have)}")
    await state.update_data(grams=g)
    await wflow.goto(msg, state, WO.reason)


@router.callback_query(StateFilter(WO.reason), F.data.startswith("wo:reason:"))
async def wo_reason(cb: CallbackQuery, state: FSMContext):
    r = REASONS[int(cb.data.split(":")[2])]
    await cb.answer()
    if r == "Інше":
        return await wflow.goto(cb.message, state, WO.reason_text)
    await state.update_data(reason=r)
    await wflow.goto(cb.message, state, WO.confirm)


@router.message(StateFilter(WO.reason_text), F.text)
async def wo_reason_text(msg: Message, state: FSMContext):
    await state.update_data(reason=msg.text.strip()[:120])
    await wflow.goto(msg, state, WO.confirm)


@router.callback_query(StateFilter(WO.confirm), F.data == "wo:confirm")
async def wo_confirm(cb: CallbackQuery, state: FSMContext, db, user):
    data = await state.get_data()
    try:
        wid = S.write_off(db, user["telegram_id"], data["pid"], data["grams"], data["reason"])
    except S.StockError as e:
        return await cb.answer(str(e), show_alert=True)
    await state.clear()
    await cb.message.answer(f"✅ Списання №{wid}: {data['pname']} {fmt_grams(data['grams'])} — {data['reason']}",
                            reply_markup=main_menu(user["role"]))
    await cb.answer()


# ---------------- інвентаризація ----------------

async def i_product(msg, state):
    await msg.answer("Інвентаризація — оберіть товар:", reply_markup=nav_kb(back=False))
    await msg.answer("Товар:", reply_markup=product_picker(get_db(), "inv", show_price=False))


async def i_batch(msg, state):
    data = await state.get_data()
    rows = S.batches_of_product(get_db(), data["pid"])
    if not rows:
        await msg.answer("Відкритих партій немає. Для дооприбуткування скористайтесь «Партії → Початковий залишок».",
                         reply_markup=nav_kb())
        return
    await msg.answer("Оберіть партію:", reply_markup=nav_kb())
    await msg.answer("Партія:", reply_markup=inline([
        [(f"#{b['id']} {b['batch_code'] or ''} від {ua_date(b['received_at'])} — {fmt_grams(b['grams_left'])}", f"inv:b:{b['id']}")]
        for b in rows]))


async def i_actual(msg, state):
    data = await state.get_data()
    await msg.answer(f"Обліковий залишок партії #{data['bid']}: {fmt_grams(data['left'])}.\nВведіть <b>фактичний</b> залишок (г або кг):",
                     reply_markup=nav_kb())


async def i_reason(msg, state):
    data = await state.get_data()
    delta = data["actual"] - data["left"]
    sign = "+" if delta > 0 else ""
    await msg.answer(f"Коригування {sign}{fmt_grams(abs(delta)) if delta < 0 else fmt_grams(delta)} ({'надлишок' if delta > 0 else 'нестача'}).\nПричина / коментар:",
                     reply_markup=nav_kb("Інвентаризація"))


iflow = Flow({str(Inv.product): i_product, str(Inv.batch): i_batch, str(Inv.actual): i_actual, str(Inv.reason): i_reason})


@router.callback_query(F.data == "inv:start")
async def inv_start(cb: CallbackQuery, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await cb.answer()
    await iflow.goto(cb.message, state, Inv.product, push=False)


@router.message(StateFilter(Inv), F.text == BACK)
async def inv_back(msg: Message, state: FSMContext, user):
    await iflow.back(msg, state, user)


@router.callback_query(StateFilter(Inv.product), F.data.startswith("pp:inv:"))
async def inv_pick(cb: CallbackQuery, state: FSMContext, db):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "inv", cat, page, show_price=False))
        return await cb.answer()
    p = S.get_product(db, int(parts[3]))
    await state.update_data(pid=p["id"], pname=p["name"])
    await cb.answer()
    await iflow.goto(cb.message, state, Inv.batch)


@router.callback_query(StateFilter(Inv.batch), F.data.startswith("inv:b:"))
async def inv_batch(cb: CallbackQuery, state: FSMContext, db):
    b = db.one("SELECT * FROM batches WHERE id=?", (int(cb.data.split(":")[2]),))
    await state.update_data(bid=b["id"], left=b["grams_left"])
    await cb.answer()
    await iflow.goto(cb.message, state, Inv.actual)


@router.message(StateFilter(Inv.actual), F.text)
async def inv_actual(msg: Message, state: FSMContext):
    t = msg.text.strip()
    try:
        g = 0 if t in ("0", "0,0", "0.0") else parse_weight_grams(t)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    if g == data["left"]:
        return await msg.answer("Фактичний залишок дорівнює обліковому — коригування не потрібне.")
    await state.update_data(actual=g)
    await iflow.goto(msg, state, Inv.reason)


@router.message(StateFilter(Inv.reason), F.text)
async def inv_reason(msg: Message, state: FSMContext, db, user):
    data = await state.get_data()
    try:
        wid = S.inventory_adjust(db, user["telegram_id"], data["bid"], data["actual"], msg.text.strip()[:120])
    except (S.StockError, ValueError) as e:
        return await msg.answer(f"⚠️ {e}")
    await state.clear()
    await msg.answer(f"✅ Коригування №{wid} проведено: партія #{data['bid']} → {fmt_grams(data['actual'])}.",
                     reply_markup=main_menu(user["role"]))


# ---------------- скасування операцій ----------------

def _ops_kb(db, only_user: int | None = None):
    rows = []
    for s in S.recent_sales(db, 8):
        if s["status"] != "done" or (only_user and s["created_by"] != only_user):
            continue
        rows.append([(f"🛒 Продаж №{s['id']} {local_dt_str(s['sold_at'])} {fmt_money(s['total'])}", f"cx:sale:{s['id']}")])
    if not only_user:
        for w in S.recent_writeoffs(db, 5):
            if w["status"] == "done":
                kind = "Списання" if w["kind"] == "writeoff" else "Коригування"
                rows.append([(f"✂️ {kind} №{w['id']} {w['product_name']} {w['grams_delta']:+d} г", f"cx:wo:{w['id']}")])
        for p in S.recent_purchases(db, 5):
            if p["status"] == "received":
                rows.append([(f"📦 Закупівля №{p['id']} {ua_date(p['doc_date'])} {p['supplier_name'] or ''}", f"cx:pur:{p['id']}")])
    return inline(rows) if rows else None


@router.callback_query(F.data == "cx:start")
async def cx_start(cb: CallbackQuery, state: FSMContext, db, user):
    await state.clear()
    kb = _ops_kb(db, None if has_role(user, "manager") else user["telegram_id"])
    if not kb:
        await cb.message.answer("Немає операцій для скасування.")
        return await cb.answer()
    await state.set_state(Cancel.pick)
    await cb.message.answer("↩️ Оберіть операцію для скасування (залишки буде відновлено, історія збережеться):",
                            reply_markup=nav_kb(back=False))
    await cb.message.answer("Операція:", reply_markup=kb)
    await cb.answer()


@router.callback_query(F.data.startswith("cx:sale:") | F.data.startswith("cx:wo:") | F.data.startswith("cx:pur:"))
async def cx_pick(cb: CallbackQuery, state: FSMContext, db, user):
    _, kind, oid = cb.data.split(":")
    if kind == "sale":
        s, _ = S.get_sale(db, int(oid))
        if not s or (not has_role(user, "manager") and s["created_by"] != user["telegram_id"]):
            return await cb.answer("Недоступно", show_alert=True)
    elif not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.set_state(Cancel.reason)
    await state.update_data(cx_kind=kind, cx_id=int(oid))
    await cb.message.answer("Причина скасування:", reply_markup=nav_kb("Помилка введення", back=False))
    await cb.answer()


@router.message(StateFilter(Cancel.reason), F.text)
async def cx_reason(msg: Message, state: FSMContext, db, user):
    data = await state.get_data()
    reason = msg.text.strip()[:120]
    try:
        if data["cx_kind"] == "sale":
            S.cancel_sale(db, data["cx_id"], user["telegram_id"], reason)
        elif data["cx_kind"] == "wo":
            S.cancel_writeoff(db, data["cx_id"], user["telegram_id"], reason)
        else:
            S.cancel_purchase(db, data["cx_id"], user["telegram_id"], reason)
    except (S.StockError, ValueError) as e:
        await state.clear()
        return await msg.answer(f"⚠️ {e}", reply_markup=main_menu(user["role"]))
    await state.clear()
    await msg.answer(f"✅ Операцію №{data['cx_id']} скасовано, залишки відновлено.", reply_markup=main_menu(user["role"]))


# ---------------- історія ----------------

def sale_line_text(s) -> str:
    st = " ❌" if s["status"] == "cancelled" else ""
    return f"№{s['id']} {local_dt_str(s['sold_at'])} — {fmt_money(s['total'])} {S.PAYMENTS[s['payment_method']]}{st}"


@router.message(F.text == M_HISTORY)
async def history(msg: Message, db, user, state: FSMContext):
    await state.clear()
    sales = S.visible_sales(db, user, 15)
    txt = "🕘 <b>Останні продажі</b>" + (" (ваші)" if user["role"] == "seller" else "") + "\n" + ("\n".join(sale_line_text(s) for s in sales) if sales else "— немає")
    if has_role(user, "manager"):
        wos = S.recent_writeoffs(db, 5)
        if wos:
            txt += "\n\n<b>Списання / коригування</b>\n" + "\n".join(
                f"№{w['id']} {ua_date(w['op_date'])} {w['product_name']} {w['grams_delta']:+d} г — {w['reason']}"
                + (" ❌" if w["status"] == "cancelled" else "") for w in wos)
        purs = S.recent_purchases(db, 5)
        if purs:
            txt += "\n\n<b>Закупівлі</b>\n" + "\n".join(
                f"№{p['id']} {ua_date(p['doc_date'])} {p['supplier_name'] or ''} — {p['status']}" for p in purs)
    await msg.answer(txt, reply_markup=main_menu(user["role"]))
    if sales:
        await msg.answer("Деталі продажу:", reply_markup=inline(
            [[(f"№{s['id']} {fmt_money(s['total'])}", f"hist:sale:{s['id']}") for s in sales[i:i + 3]] for i in range(0, min(len(sales), 9), 3)]
            + [[("↩️ Скасувати операцію", "cx:start")]]))


@router.callback_query(F.data.startswith("hist:sale:"))
async def hist_sale(cb: CallbackQuery, db):
    s, lines = S.get_sale(db, int(cb.data.split(":")[2]))
    if not s:
        return await cb.answer("Не знайдено", show_alert=True)
    u = db.one("SELECT name FROM users WHERE telegram_id=?", (s["created_by"],))
    txt = [f"🧾 <b>Продаж №{s['id']}</b> · {local_dt_str(s['sold_at'])} · {S.PAYMENTS[s['payment_method']]} · {u['name'] if u else s['created_by']}"]
    for l in lines:
        qty = f"{l['pieces']} шт" if l["pieces"] else fmt_grams(l["grams"])
        txt.append(f"• {l['product_name']} — {qty} × {fmt_price(l['price'])} = {fmt_money(l['amount'])} (собів. {fmt_money(l['cost'])})")
    txt.append(f"Разом: <b>{fmt_money(s['total'])}</b>, валовий прибуток {fmt_money(d(s['total']) - d(s['cost_total']))}")
    if s["status"] == "cancelled":
        txt.append(f"❌ Скасовано: {s['cancel_reason']}")
    await cb.message.answer("\n".join(txt))
    await cb.answer()
