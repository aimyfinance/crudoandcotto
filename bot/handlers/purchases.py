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
from ..keyboards import BACK, M_PURCHASE, M_PURCH_MANUAL, M_INVOICE, SKIP, TODAY, inline, main_menu, nav_kb, product_picker
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
    await msg.answer("Або завантажте документ:", reply_markup=inline([[("📄 Закупівля з інвойсу (PDF/фото)", "pur:invoice")]]))


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


@router.message(StateFilter(None), F.text == M_PURCHASE + " (останні)")
async def recent(msg: Message, db, user):
    rows = S.recent_purchases(db, 10)
    if not rows:
        return await msg.answer("Закупівель ще немає.")
    await msg.answer("📦 <b>Останні закупівлі</b>\n" + "\n".join(
        f"№{p['id']} {ua_date(p['doc_date'])} {p['supplier_name'] or ''} — {p['status']}" + (f" · {p['comment']}" if p["comment"] else "") for p in rows))


@router.message(F.text.in_({M_PURCHASE, M_PURCH_MANUAL}))
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


# ======================= закупівля з інвойсу (PDF/фото) =======================

import asyncio
from aiogram.types import BufferedInputFile


class Inv(StatesGroup):
    file = State()
    review = State()
    line_product = State()
    line_weight = State()
    line_price = State()
    transport = State()


MIME_BY_EXT = {".pdf": "application/pdf", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}


async def _inv_begin(target: Message, state: FSMContext, user) -> str | None:
    if not has_role(user, "manager"):
        return "Недостатньо прав"
    import os
    if not os.getenv("ANTHROPIC_API_KEY"):
        return "Не задано ANTHROPIC_API_KEY на сервері — розпізнавання вимкнено"
    await state.clear()
    await state.set_state(Inv.file)
    await target.answer("📄 Надішліть інвойс: PDF або фото (як документ чи як фото). Можна кілька сторінок — по одній.",
                        reply_markup=nav_kb(back=False))
    return None


@router.callback_query(F.data == "pur:invoice")  # доступна зі стану Pur.date і без стану
async def inv_start(cb: CallbackQuery, state: FSMContext, user):
    err = await _inv_begin(cb.message, state, user)
    await cb.answer(err, show_alert=True) if err else await cb.answer()


@router.message(StateFilter(None), F.text == M_INVOICE)
async def inv_start_msg(msg: Message, state: FSMContext, user):
    err = await _inv_begin(msg, state, user)
    if err:
        await msg.answer(f"⚠️ {err}")


@router.message(StateFilter(Inv.file), F.document | F.photo)
async def inv_file(msg: Message, state: FSMContext, db, user):
    from ..invoice import extract_invoice, build_draft
    import io, os
    buf = io.BytesIO()
    if msg.document:
        ext = os.path.splitext(msg.document.file_name or "")[1].lower()
        mime = MIME_BY_EXT.get(ext) or msg.document.mime_type or ""
        if mime not in MIME_BY_EXT.values():
            return await msg.answer("⚠️ Підтримуються PDF, JPG, PNG, WEBP")
        await msg.bot.download(msg.document, destination=buf)
    else:
        mime = "image/jpeg"
        await msg.bot.download(msg.photo[-1], destination=buf)
    wait = await msg.answer("🔎 Розпізнаю інвойс… (10–30 с)")
    try:
        parsed = await asyncio.to_thread(extract_invoice, buf.getvalue(), mime)
        draft = build_draft(parsed)
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося розпізнати: {e}")
    import base64
    await state.update_data(draft=draft, inv_b64=base64.b64encode(buf.getvalue()).decode(),
                            inv_name=(msg.document.file_name if msg.document else f"invoice_{today_local()}.jpg"))
    await state.set_state(Inv.review)
    await _inv_review(msg, state)


def _line_text(i: int, l: dict) -> str:
    mark = {"alias": "🔗", "exact": "✅", "auto": "🔸", "none": "❌"}[l["how"]]
    if l.get("skip"):
        mark = "⏭"
    w = fmt_grams(l["grams"]) if l["grams"] else "— кг"
    pr = f"{fmt_price(l['price'])} €/кг" if l["price"] else "— €/кг"
    amt = fmt_money(l["amount"]) if l["amount"] else "—"
    iss = f" ⚠️ {'; '.join(l['issues'])}" if l["issues"] and not l.get("skip") else ""
    return f"{mark} {i + 1}. {l['description'][:40]} → <b>{l['product_name'] or 'не знайдено'}</b>\n     {w} × {pr} = {amt}{iss}"


async def _inv_review(msg: Message, state: FSMContext):
    data = await state.get_data()
    d = data["draft"]
    total_g = sum(l["grams"] or 0 for l in d["lines"] if not l.get("skip"))
    txt = [f"📄 <b>{d['supplier'] or 'Постачальник?'}</b> · №{d.get('number') or '—'} · {ua_date(d.get('date')) if d.get('date') else 'дата?'}"]
    txt += [_line_text(i, l) for i, l in enumerate(d["lines"])]
    txt.append(f"\nТовар: <b>{fmt_money(d['sum_lines'])}</b> · {fmt_grams(total_g)} · транспорт/інше: {fmt_money(Decimal(d['transport']) + Decimal(d['other_costs']))}")
    for name, ok, detail in d["checks"]:
        txt.append(f"{'✅' if ok else '⚠️'} {name} ({detail})")
    txt.append("\n✅ точний збіг · 🔗 збережена прив'язка · 🔸 підібрано автоматично — перевірте · ❌ не знайдено")
    await msg.answer("\n".join(txt))
    kb = [[(f"✏️ {i + 1}", f"inv:line:{i}") for i in range(j, min(j + 5, len(d["lines"])))] for j in range(0, len(d["lines"]), 5)]
    kb.append([("🚚 Транспорт / інші витрати", "inv:transport")])
    ready = any(l["product_id"] and l["grams"] and l["price"] and not l.get("skip") for l in d["lines"])
    kb.append([("✅ Оприбуткувати", "inv:post")] if ready else [("(немає готових позицій)", "noop")])
    await msg.answer("Дія:", reply_markup=inline(kb))


@router.callback_query(StateFilter(Inv.review), F.data.startswith("inv:line:"))
async def inv_line(cb: CallbackQuery, state: FSMContext):
    i = int(cb.data.split(":")[2])
    await state.update_data(inv_i=i)
    d = (await state.get_data())["draft"]
    l = d["lines"][i]
    await cb.message.answer(_line_text(i, l), reply_markup=inline([
        [("🧀 Товар", f"inv:edit:product"), ("⚖️ Вага", f"inv:edit:weight"), ("💶 Ціна/кг", f"inv:edit:price")],
        [("▶️ Пропустити позицію" if not l.get("skip") else "↩️ Повернути позицію", "inv:edit:skip"), ("➕ Створити товар", "inv:edit:new")],
    ]))
    await cb.answer()


@router.callback_query(StateFilter(Inv.review), F.data.startswith("inv:edit:"))
async def inv_edit(cb: CallbackQuery, state: FSMContext, db, user):
    what = cb.data.split(":")[2]
    data = await state.get_data()
    d, i = data["draft"], data["inv_i"]
    if what == "product":
        await state.set_state(Inv.line_product)
        await cb.message.answer(f"Який товар бота відповідає «{d['lines'][i]['description']}»?",
                                reply_markup=product_picker(db, "iv", show_price=False))
    elif what == "weight":
        await state.set_state(Inv.line_weight)
        await cb.message.answer("Вага позиції (кг або г):", reply_markup=nav_kb(back=False))
    elif what == "price":
        await state.set_state(Inv.line_price)
        await cb.message.answer("Ціна за кг, €:", reply_markup=nav_kb(back=False))
    elif what == "skip":
        d["lines"][i]["skip"] = not d["lines"][i].get("skip")
        await state.update_data(draft=d)
        await _inv_review(cb.message, state)
    elif what == "new":
        l = d["lines"][i]
        cat = "meat"
        low = l["description"].lower()
        if any(k in low for k in ("taleggio", "asiago", "bosina", "tur", "chevre", "pecorino", "formagg", "brie", "mozzar", "burrat", "parmig", "provol", "raclette", "gorgon")):
            cat = "cheese"
        if any(k in low for k in ("gnocchi", "ravioli", "tortell", "pasta", "cappell")):
            cat = "pasta"
        pid = S.create_product(db, l["description"][:80].title(), cat, "weight", Decimal(0))
        l["product_id"], l["product_name"], l["how"] = pid, S.get_product(db, pid)["name"], "exact"
        await state.update_data(draft=d)
        await cb.message.answer(f"✅ Створено товар «{l['product_name']}» ({S.CATEGORIES[cat]}, на вагу, роздрібна ціна 0 — задайте в «Товари»).")
        await _inv_review(cb.message, state)
    await cb.answer()


@router.callback_query(StateFilter(Inv.line_product), F.data.startswith("pp:iv:"))
async def inv_pick(cb: CallbackQuery, state: FSMContext, db):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "iv", cat, page, show_price=False))
        return await cb.answer()
    data = await state.get_data()
    d, i = data["draft"], data["inv_i"]
    p = S.get_product(db, int(parts[3]))
    d["lines"][i].update(product_id=p["id"], product_name=p["name"], how="alias")
    await state.update_data(draft=d)
    await state.set_state(Inv.review)
    await cb.answer("Прив'язано")
    await _inv_review(cb.message, state)


@router.message(StateFilter(Inv.line_weight), F.text)
async def inv_weight(msg: Message, state: FSMContext):
    try:
        g = parse_weight_grams(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    d, i = data["draft"], data["inv_i"]
    l = d["lines"][i]
    l["grams"] = g
    l["issues"] = [x for x in l["issues"] if x != "немає ваги"]
    if l["price"]:
        l["amount"] = str(line_amount(g, Decimal(l["price"])))
    d["sum_lines"] = str(sum((Decimal(x["amount"]) for x in d["lines"] if x["amount"]), Decimal(0)))
    await state.update_data(draft=d)
    await state.set_state(Inv.review)
    await _inv_review(msg, state)


@router.message(StateFilter(Inv.line_price), F.text)
async def inv_price(msg: Message, state: FSMContext):
    try:
        p = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    d, i = data["draft"], data["inv_i"]
    l = d["lines"][i]
    l["price"] = str(p)
    l["issues"] = [x for x in l["issues"] if x != "немає ціни"]
    if l["grams"]:
        l["amount"] = str(line_amount(l["grams"], p))
    d["sum_lines"] = str(sum((Decimal(x["amount"]) for x in d["lines"] if x["amount"]), Decimal(0)))
    await state.update_data(draft=d)
    await state.set_state(Inv.review)
    await _inv_review(msg, state)


@router.callback_query(StateFilter(Inv.review), F.data == "inv:transport")
async def inv_transport(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Inv.transport)
    d = (await state.get_data())["draft"]
    await cb.message.answer(f"🚚 Транспорт та інші закупівельні витрати, € (зараз {fmt_money(Decimal(d['transport']) + Decimal(d['other_costs']))}). "
                            "Розподіляться на позиції пропорційно вазі:", reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Inv.transport), F.text)
async def inv_transport_val(msg: Message, state: FSMContext):
    try:
        v = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    data = await state.get_data()
    d = data["draft"]
    d["transport"], d["other_costs"] = str(v), "0"
    await state.update_data(draft=d)
    await state.set_state(Inv.review)
    await _inv_review(msg, state)


@router.callback_query(StateFilter(Inv.review), F.data == "inv:post")
async def inv_post(cb: CallbackQuery, state: FSMContext, db, user):
    from ..invoice import post_draft
    data = await state.get_data()
    d = data["draft"]
    try:
        pid = post_draft(d, user["telegram_id"])
    except ValueError as e:
        return await cb.answer(str(e), show_alert=True)
    if data.get("inv_b64"):
        import base64
        from .tasks import save_document
        try:
            save_document(db, user["telegram_id"], "purchase", pid, data.get("inv_name") or "invoice.pdf", base64.b64decode(data["inv_b64"]))
        except Exception:
            pass
    n = sum(1 for l in d["lines"] if l["product_id"] and l["grams"] and l["price"] and not l.get("skip"))
    skipped = len(d["lines"]) - n
    await state.clear()
    await cb.message.answer(f"✅ Закупівлю №{pid} оприбутковано: {n} партій" + (f", пропущено {skipped} поз." if skipped else "") +
                            ". Прив'язки назв постачальника збережено — наступний інвойс розпізнається без правок.",
                            reply_markup=main_menu(user["role"]))
    await cb.answer()
