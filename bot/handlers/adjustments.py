"""Списання з причиною, інвентаризаційне коригування, скасування операцій, історія."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message

from .. import services as S
import datetime as dt

from ..db import get_db, local_dt_str, today_local
from ..keyboards import menu_for, BACK, M_HISTORY, M_LOG, M_WRITEOFF, inline, main_menu, nav_kb, product_picker
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
    await msg.answer("✂️ <b>Списання / коригування</b>", reply_markup=menu_for(user))
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
    db = get_db()
    have = S.stock_of_product(db, data["pid"])
    rows = S.batches_of_product(db, data["pid"])
    txt = f"<b>{data['pname']}</b>, залишок {fmt_grams(have)}\n"
    txt += batches_table(rows)
    txt += "\nВведіть вагу списання (г або кг) або оберіть партію, щоб списати її цілком:"
    await msg.answer(txt, reply_markup=nav_kb())
    if rows:
        await msg.answer("Списати цілу партію:", reply_markup=inline(
            [[(f"✂️ #{b['id']} {fmt_grams(b['grams_left'])}" + (f" · до {ua_date(b['expiry_date'])}" if b["expiry_date"] else ""), f"wo:batch:{b['id']}")] for b in rows[:8]]))


def batches_table(rows) -> str:
    """Партії у вигляді моноширинної таблиці: # · дата · залишок · €/кг · термін."""
    if not rows:
        return "<i>відкритих партій немає</i>"
    lines = ["  #   дата      залишок    €/кг  термін"]
    for b in rows:
        exp = ua_date(b["expiry_date"])[:5] if b["expiry_date"] else "  —  "
        warn = "⏰" if b["expiry_date"] and b["expiry_date"] <= (dt.date.today() + dt.timedelta(days=7)).isoformat() else " "
        lines.append(f"{b['id']:>4} {ua_date(b['received_at'])[:5]}  {fmt_grams(b['grams_left']):>9}  {str(f"{float(b['landed_price_per_kg']):.2f}").replace('.', ',')}  {exp}{warn}")
    return "<pre>" + "\n".join(lines) + "</pre>"


async def w_reason(msg, state):
    await msg.answer("Причина:", reply_markup=nav_kb())
    await msg.answer("Оберіть:", reply_markup=inline([[(r, f"wo:reason:{i}")] for i, r in enumerate(REASONS)]))


async def w_reason_text(msg, state):
    await msg.answer("Опишіть причину:", reply_markup=nav_kb())


async def w_confirm(msg, state):
    data = await state.get_data()
    src = f"партія #{data['batch_id']}" if data.get("batch_id") else "партія — FIFO"
    await msg.answer(f"Списати <b>{data['pname']}</b> — {fmt_grams(data['grams'])}\nПричина: {data['reason']}\n({src})",
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


@router.callback_query(StateFilter(WO.weight), F.data.startswith("wo:batch:"))
async def wo_batch_whole(cb: CallbackQuery, state: FSMContext, db):
    b = db.one("SELECT * FROM batches WHERE id=?", (int(cb.data.split(":")[2]),))
    if not b or b["grams_left"] <= 0:
        return await cb.answer("Партія порожня", show_alert=True)
    await state.update_data(grams=b["grams_left"], batch_id=b["id"])
    await cb.answer()
    await wflow.goto(cb.message, state, WO.reason)


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
    await state.update_data(grams=g, batch_id=None)
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
        wid = S.write_off(db, user["telegram_id"], data["pid"], data["grams"], data["reason"], batch_id=data.get("batch_id"))
    except S.StockError as e:
        return await cb.answer(str(e), show_alert=True)
    await state.clear()
    await cb.message.answer(f"✅ Списання №{wid}: {data['pname']} {fmt_grams(data['grams'])} — {data['reason']}",
                            reply_markup=menu_for(user))
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
    await msg.answer("Оберіть партію:\n" + batches_table(rows), reply_markup=nav_kb())
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
                     reply_markup=menu_for(user))


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
        return await msg.answer(f"⚠️ {e}", reply_markup=menu_for(user))
    await state.clear()
    await msg.answer(f"✅ Операцію №{data['cx_id']} скасовано, залишки відновлено.", reply_markup=menu_for(user))


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
                f"№{p['id']} {ua_date(p['doc_date'])} {p['supplier_name'] or ''} — {S.STATUS_UA.get(p['status'], p['status'])}" for p in purs)
    await msg.answer(txt, reply_markup=menu_for(user))
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


# ---------------- журнал дій ----------------

@router.message(StateFilter(None), F.text == M_LOG)
async def action_log(msg: Message, db, user):
    if not has_role(user, "manager"):
        return
    rows = S.audit_recent(db, 15, None if user["role"] == "admin" else None)
    if user["role"] == "manager":   # менеджер не бачить дій адміністратора
        admins = {u["telegram_id"] for u in S.list_users(db) if u["role"] == "admin"}
        rows = [r for r in rows if r["user_id"] not in admins]
    if not rows:
        return await msg.answer("Журнал порожній.")
    txt = ["🕘 <b>Журнал дій</b> (останні)"]
    kb = []
    for r in rows:
        who = r["user_name"] or r["user_id"]
        txt.append(f"<b>#{r['id']}</b> {local_dt_str(r['ts'])} · {who}\n     {S.audit_describe(db, r)}")
        if r["action"] in S.UNDOABLE:
            kb.append((f"↩️ #{r['id']}", f"undo:ask:{r['id']}"))
    await msg.answer("\n".join(txt))
    if kb:
        await msg.answer("Повернути дію (буде запитано підтвердження):", reply_markup=inline([kb[i:i + 4] for i in range(0, len(kb), 4)]))


@router.callback_query(F.data.startswith("undo:ask:"))
async def undo_ask(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    aid = int(cb.data.split(":")[2])
    r = db.one("SELECT * FROM audit_log WHERE id=?", (aid,))
    if not r:
        return await cb.answer("Не знайдено", show_alert=True)
    await cb.message.answer(f"Повернути дію #{aid}?\n{S.audit_describe(db, r)}", reply_markup=inline([[("↩️ Так, повернути", f"undo:yes:{aid}"), ("Ні", "noop")]]))
    await cb.answer()


@router.callback_query(F.data.startswith("undo:yes:"))
async def undo_yes(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    try:
        res = S.undo_action(db, int(cb.data.split(":")[2]), user["telegram_id"])
    except (S.StockError, S.DuplicateOperation, ValueError) as e:
        return await cb.answer(str(e), show_alert=True)
    await cb.message.answer(f"✅ {res}")
    await cb.answer()
