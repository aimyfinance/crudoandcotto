"""Звіти (текст + Excel/CSV), налаштування (користувачі, бекап, відновлення, імпорт каси)."""
from __future__ import annotations

import datetime as dt
import io
from decimal import Decimal
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, InlineKeyboardButton, Message

from .. import services as S
from ..cash_import import parse_cash_report
from ..config import TZ, settings
from ..export import build_csv_movements, build_excel
from ..keyboards import (CANCEL, M_DOCS_EXP, M_DOCS_PURCH, M_EXP_ADD, M_EXP_LIST, M_EXP_SUMMARY, M_PUR_SUMMARY, M_SALE_SUMMARY, M_REP_MONTH, M_REP_PERIOD, M_REP_TODAY,
                         M_REPORTS, M_SETTINGS, inline, main_menu, nav_kb, product_picker)
from ..money import fmt_grams, fmt_money, fmt_price
from ..db import today_local, get_db
from .common import Flow, has_role, parse_date, parse_period, ua_date

router = Router(name="reports")


class Rep(StatesGroup):
    period = State()


class Sett(StatesGroup):
    add_user = State()
    cash_file = State()
    db_file = State()
    cash_confirm = State()
    products_file = State()
    opening_file = State()
    history_file = State()
    expenses_file = State()
    reset_confirm = State()
    octobox_file = State()
    octobox_review = State()
    octobox_alias = State()
    octobox_date = State()


class Exp(StatesGroup):
    date = State()
    type = State()
    category = State()
    amount = State()
    comment = State()
    photo = State()


# ---------------- періоди ----------------

def _period(code: str) -> tuple[str, str]:
    today = dt.datetime.now(TZ).date()
    if code == "today":
        return today.isoformat(), today.isoformat()
    if code == "yday":
        y = today - dt.timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if code == "week":
        return (today - dt.timedelta(days=today.weekday())).isoformat(), today.isoformat()
    if code == "month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if code == "pmonth":
        first = today.replace(day=1)
        last_prev = first - dt.timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    if code == "year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    return today.isoformat(), today.isoformat()


PERIOD_CODES = ("today", "yday", "week", "month", "pmonth", "year")


def prev_period(d1: str, d2: str) -> tuple[str, str]:
    """Попередній період такої ж довжини (для місяця — попередній календарний місяць)."""
    a, b = dt.date.fromisoformat(d1), dt.date.fromisoformat(d2)
    if a.day == 1 and (b + dt.timedelta(days=1)).day == 1 and (b.year, b.month) == (a.year, a.month):
        last_prev = a - dt.timedelta(days=1)
        return last_prev.replace(day=1).isoformat(), last_prev.isoformat()
    n = (b - a).days + 1
    return (a - dt.timedelta(days=n)).isoformat(), (a - dt.timedelta(days=1)).isoformat()


def period_label(d1: str, d2: str) -> str:
    return ua_date(d1) if d1 == d2 else f"{ua_date(d1)} — {ua_date(d2)}"


def period_kb_for(prefix: str):
    """Універсальний вибір періоду; callback '<prefix>:<code>' або '<prefix>:custom'."""
    return inline([[("Сьогодні", f"{prefix}:today"), ("Вчора", f"{prefix}:yday"), ("Тиждень", f"{prefix}:week")],
                   [("Цей місяць", f"{prefix}:month"), ("Минулий місяць", f"{prefix}:pmonth"), ("Рік", f"{prefix}:year")],
                   [("📆 Свій період", f"{prefix}:custom")]])


def report_text(rep: dict) -> str:
    p = f"{ua_date(rep['date_from'])}" if rep["date_from"] == rep["date_to"] else f"{ua_date(rep['date_from'])} — {ua_date(rep['date_to'])}"
    out = [f"📈 <b>Звіт за {p}</b>",
           f"Продажів: {rep['sales_count']} · продано {fmt_grams(rep['sold_grams'])}",
           f"Виручка: <b>{fmt_money(rep['revenue'])}</b>" + (
               " (" + ", ".join(f"{S.PAYMENTS[k].lower()} {fmt_money(v)}" for k, v in rep["by_payment"].items()) + ")" if rep["by_payment"] else ""),
           f"Собівартість проданого: {fmt_money(rep['cogs'])}",
           f"Валовий прибуток: <b>{fmt_money(rep['gross_profit'])}</b>"
           + (f" ({rep['gross_profit'] / rep['revenue'] * 100:.1f} %)" if rep["revenue"] else ""),
           f"Закуплено: {fmt_grams(rep['purchased_grams'])} на {fmt_money(rep['purchased_amount'])}"
           + (f" + витрати {fmt_money(rep['purchase_extra_costs'])}" if rep["purchase_extra_costs"] else ""),
           f"Списано: {fmt_grams(rep['writeoff_grams'])} на {fmt_money(rep['writeoff_cost'])}"]
    if rep["adjustment_grams"]:
        out.append(f"Інвентаризаційні коригування: {rep['adjustment_grams']:+d} г ({fmt_money(rep['adjustment_cost'])})")
    out.append(f"Валовий прибуток після списань: {fmt_money(rep['gross_after_writeoffs'])}")
    ex = rep["expenses"]["by_type"]
    if any(ex.values()):
        out.append(f"Операційні витрати: {fmt_money(ex['operating'])} · податки: {fmt_money(ex['tax'])}")
        out.append(f"<b>Операційний результат</b> (ВП − списання − опер. витрати − податки): <b>{fmt_money(rep['operating_result'])}</b>")
        if ex["goods"] or ex["investment"]:
            out.append(f"<i>Довідково: оплата товару {fmt_money(ex['goods'])}, інвестиції {fmt_money(ex['investment'])}</i>")
    out.append(f"Поточні залишки: {fmt_grams(rep['stock_grams'])} на {fmt_money(rep['stock_value'])}")
    if rep["by_product"]:
        out.append("\n<b>Продажі за товарами</b> (виручка · вал. прибуток · маржа)")
        for e in rep["by_product"][:15]:
            out.append(f"• {e['name']}: {fmt_grams(e['grams'])} · {fmt_money(e['amount'])} · {fmt_money(e['gross_profit'])} · {e['margin_pct']:.0f} %")
    out.append("\n<i>Валовий прибуток = виручка − собівартість проданого (з транспортом). Це не чистий прибуток.</i>")
    return "\n".join(out)


def period_kb(kind: str = "rep"):
    return period_kb_for(kind)


def export_kb(d1: str, d2: str):
    return inline([[("📊 Excel (структура звіту)", f"rep:xlsx:{d1}:{d2}"), ("📄 CSV операцій", f"rep:csv:{d1}:{d2}")],
                   [("🧾 Звірка з касою", f"rep:rec:{d1}:{d2}"), ("💸 Витрати за період", f"rep:exp:{d1}:{d2}")]])


@router.message(F.text == M_REPORTS)
async def reports(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await msg.answer("Звіти доступні менеджеру й адміністратору.")
    await state.clear()
    await msg.answer("📈 <b>Звіти</b> — оберіть період:", reply_markup=main_menu(user["role"]))
    await msg.answer("Період:", reply_markup=period_kb("rep"))
    await msg.answer("Витрати:", reply_markup=inline([[("➕ Додати витрату", "exp:add"), ("🗑 Останні витрати", "exp:list")]]))


@router.message(StateFilter(None), F.text.in_({M_REP_TODAY, M_REP_MONTH}))
async def rep_quick(msg: Message, db, user):
    if not has_role(user, "manager"):
        return
    await _send_report(msg, db, user, *_period("today" if msg.text == M_REP_TODAY else "month"))


@router.message(StateFilter(None), F.text == M_REP_PERIOD)
async def rep_period_btn(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return
    await state.set_state(Rep.period)
    await msg.answer("Введіть період: <code>01.09.2026 - 15.09.2026</code> або одну дату:", reply_markup=nav_kb(back=False))


def expense_summary_text(db, d1: str, d2: str) -> str:
    p1, p2 = prev_period(d1, d2)
    cur, prev = S.expenses_period(db, d1, d2), S.expenses_period(db, p1, p2)
    rep_cur, rep_prev = S.report_period(db, d1, d2), S.report_period(db, p1, p2)
    out = [f"💸 <b>Витрати за {period_label(d1, d2)}</b>  · порівняно з {period_label(p1, p2)}"]
    tot_c = sum(cur["by_type"].values(), Decimal(0))
    tot_p = sum(prev["by_type"].values(), Decimal(0))
    for t, label in S.EXP_TYPES.items():
        c, p = cur["by_type"][t], prev["by_type"][t]
        if c or p:
            out.append(f"• {label}: <b>{fmt_money(c)}</b>  (попер. {fmt_money(p)})")
    if not tot_c and not tot_p:
        out.append("Витрат за період немає.")
    out.append(f"Разом: <b>{fmt_money(tot_c)}</b>  (попер. {fmt_money(tot_p)})")
    opex_c = cur["by_type"]["operating"] + cur["by_type"]["tax"]
    if rep_cur["revenue"]:
        out.append(f"Операційні + податки = {opex_c / rep_cur['revenue'] * 100:.0f} % виручки ({fmt_money(rep_cur['revenue'])})")
    out.append(f"Операційний результат: <b>{fmt_money(rep_cur['operating_result'])}</b>  (попер. {fmt_money(rep_prev['operating_result'])})")
    top = sorted(((c, v) for (t, c), v in cur["by_category"].items() if t in ("operating", "tax")), key=lambda x: -x[1])[:6]
    if top:
        out.append("\n<b>Найбільші операційні за період</b>")
        prev_cat = {c: v for (t, c), v in prev["by_category"].items()}
        for c, v in top:
            pv = prev_cat.get(c)
            out.append(f"• {c}: {fmt_money(v)}" + (f" (попер. {fmt_money(pv)})" if pv else ""))
    return "\n".join(out)


@router.message(StateFilter(None), F.text == M_EXP_SUMMARY)
async def exp_summary_msg(msg: Message, db, user):
    if not has_role(user, "manager"):
        return
    await msg.answer("📊 Резюме витрат — оберіть період:", reply_markup=period_kb_for("exps"))


async def _send_exp_summary(msg: Message, db, d1: str, d2: str):
    await msg.answer(expense_summary_text(db, d1, d2))
    await msg.answer("Детальніше:", reply_markup=inline([[("📋 Розбивка по категоріях", f"rep:exp:{d1}:{d2}"), ("📈 Повний звіт", f"rep:full:{d1}:{d2}")],
                                                         [("🔁 Інший період", "exps:menu")]]))


@router.callback_query(F.data.startswith("exps:"))
async def exp_summary_cb(cb: CallbackQuery, state: FSMContext, db, user):
    code = cb.data.split(":")[1]
    if code == "menu":
        await cb.message.answer("Період:", reply_markup=period_kb_for("exps"))
    elif code == "custom":
        await state.set_state(Rep.period)
        await state.update_data(period_target="exps")
        await cb.message.answer("Введіть період: <code>01.08.2026 - 31.08.2026</code> або одну дату:", reply_markup=nav_kb(back=False))
    elif code in PERIOD_CODES:
        await _send_exp_summary(cb.message, db, *_period(code))
    await cb.answer()


# ---------------- резюме закупівель і продажів (той самий вибір періоду) ----------------

def purchases_summary_text(db, d1: str, d2: str) -> str:
    pu = S.purchases_period(db, d1, d2)
    out = [f"📦 <b>Закупівлі за {period_label(d1, d2)}</b>"]
    if not pu["count"]:
        out.append("Закупівель за період немає.")
        return "\n".join(out)
    out.append(f"Документів: {pu['count']} · {fmt_grams(pu['grams'])} · товар <b>{fmt_money(pu['amount'])}</b>"
               + (f" + транспорт/інше {fmt_money(pu['extra'])}" if pu["extra"] else ""))
    if pu["grams"]:
        out.append(f"Середня ціна з витратами: {fmt_price(((pu['amount'] + pu['extra']) * 1000 / pu['grams']).quantize(Decimal('0.01')))} €/кг")
    out.append("\n<b>За постачальниками</b>")
    for sup, (g, a, n) in sorted(pu["by_supplier"].items(), key=lambda x: -x[1][1]):
        out.append(f"• {sup}: {n} док. · {fmt_grams(g)} · {fmt_money(a)}")
    out.append("\n<b>Найбільше закуплено (€)</b>")
    for r in pu["top"]:
        out.append(f"• {r['name']}: {fmt_grams(int(r['g']))} · {fmt_money(Decimal(str(r['a'])))}")
    if pu["count"] <= 12:
        out.append("\n<b>Документи</b>")
        for r in pu["rows"]:
            out.append(f"• №{r['id']} {ua_date(r['doc_date'])} {r['supplier'] or ''} — {fmt_money(Decimal(str(r['amount'] or 0)))}" + (f" · {r['comment']}" if r["comment"] else ""))
    return "\n".join(out)


def sales_summary_text(db, d1: str, d2: str) -> str:
    rep = S.report_period(db, d1, d2)
    p1, p2 = prev_period(d1, d2)
    prev = S.report_period(db, p1, p2)
    out = [f"🛒 <b>Продажі за {period_label(d1, d2)}</b>  · порівняно з {period_label(p1, p2)}"]
    if not rep["sales_count"]:
        out.append("Продажів за період немає.")
        return "\n".join(out)
    avg = rep["revenue"] / rep["sales_count"]
    out.append(f"Виручка: <b>{fmt_money(rep['revenue'])}</b>  (попер. {fmt_money(prev['revenue'])})")
    out.append(f"Чеків: {rep['sales_count']} · середній чек {fmt_money(avg)} · продано {fmt_grams(rep['sold_grams'])}")
    if rep["by_payment"]:
        out.append("Оплата: " + ", ".join(f"{S.PAYMENTS[k].lower()} {fmt_money(v)}" for k, v in rep["by_payment"].items()))
    out.append(f"Валовий прибуток: <b>{fmt_money(rep['gross_profit'])}</b> ({rep['gross_profit'] / rep['revenue'] * 100:.0f} %)")
    days = S.sales_by_day(db, d1, d2)
    if 1 < len(days) <= 31:
        out.append("\n<b>По днях</b>")
        for r in days:
            out.append(f"• {ua_date(r['sale_date'])}: {fmt_money(Decimal(str(r['t'])))} ({r['n']} чек.)")
    out.append("\n<b>Топ товарів</b> (виручка · маржа)")
    for e in rep["by_product"][:10]:
        out.append(f"• {e['name']}: {fmt_grams(e['grams'])} · {fmt_money(e['amount'])} · {e['margin_pct']:.0f} %")
    return "\n".join(out)


SUMMARY_KINDS = {"purs": ("📦 Резюме закупівель", purchases_summary_text), "sales": ("🛒 Резюме продажів", sales_summary_text)}


async def _send_kind_summary(msg: Message, db, kind: str, d1: str, d2: str):
    title, fn = SUMMARY_KINDS[kind]
    await msg.answer(fn(db, d1, d2))
    extra = [("📊 Excel", f"rep:xlsx:{d1}:{d2}"), ("🧾 Звірка з касою", f"rep:rec:{d1}:{d2}")] if kind == "sales" else [("📈 Повний звіт", f"rep:full:{d1}:{d2}")]
    await msg.answer("Детальніше:", reply_markup=inline([extra, [("🔁 Інший період", f"{kind}:menu")]]))


@router.message(StateFilter(None), F.text.in_({M_PUR_SUMMARY, M_SALE_SUMMARY}))
async def kind_summary_msg(msg: Message, user):
    if not has_role(user, "manager"):
        return
    kind = "purs" if msg.text == M_PUR_SUMMARY else "sales"
    await msg.answer(f"{SUMMARY_KINDS[kind][0]} — оберіть період:", reply_markup=period_kb_for(kind))


@router.callback_query(F.data.startswith("purs:") | F.data.startswith("sales:"))
async def kind_summary_cb(cb: CallbackQuery, state: FSMContext, db, user):
    kind, code = cb.data.split(":")[:2]
    if code == "menu":
        await cb.message.answer("Період:", reply_markup=period_kb_for(kind))
    elif code == "custom":
        await state.set_state(Rep.period)
        await state.update_data(period_target=kind)
        await cb.message.answer("Введіть період: <code>01.08.2026 - 31.08.2026</code> або одну дату:", reply_markup=nav_kb(back=False))
    elif code in PERIOD_CODES:
        await _send_kind_summary(cb.message, db, kind, *_period(code))
    await cb.answer()


@router.message(StateFilter(None), F.text == M_EXP_ADD)
async def exp_add_msg(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return
    await state.clear()
    await eflow.goto(msg, state, Exp.date, push=False)


def expense_card(e) -> str:
    docs = len(get_db().q("SELECT id FROM documents WHERE kind='expense' AND ref_id=?", (e["id"],)))
    return (f"💸 <b>Витрата №{e['id']}</b>" + (" · <i>скасована</i>" if e["status"] == "cancelled" else "") +
            f"\n{ua_date(e['op_date'])} · {S.EXP_TYPES[e['exp_type']]}\n<b>{e['category']}</b> — <b>{fmt_money(e['amount'])}</b>"
            + (f"\nКоментар: {e['comment']}" if e["comment"] else "") +
            f"\nВніс(ла): {e['user_name'] or e['created_by']}" + (f" · 📎 документів: {docs}" if docs else ""))


def expense_card_kb(e):
    if e["status"] == "cancelled":
        return inline([[("↩️ Відновити", f"exp:restore:{e['id']}")]])
    return inline([[("🔁 Повторити сьогодні", f"exp:repeat:{e['id']}"), ("✏️ Змінити", f"exp:edit:{e['id']}")],
                   [("🗑 Видалити", f"exp:delask:{e['id']}")]])


@router.message(StateFilter(None), F.text == M_EXP_LIST)
async def exp_list_msg(msg: Message, db, user):
    rows = S.recent_expenses(db, 12)
    if not rows:
        return await msg.answer("Витрат ще немає.")
    kb = [[(f"{ua_date(r['op_date'])} {r['category'][:22]} {fmt_money(r['amount'])}", f"exp:view:{r['id']}")] for r in rows]
    if S.recent_cancelled_expenses(db, 1):
        kb.append([("↩️ Скасовані (відновити)", "exp:cancelled")])
    await msg.answer("💸 <b>Останні витрати</b> — натисніть, щоб відкрити:", reply_markup=inline(kb))


@router.callback_query(F.data.startswith("exp:view:"))
async def exp_view(cb: CallbackQuery, db, user):
    e = S.get_expense(db, int(cb.data.split(":")[2]))
    if not e:
        return await cb.answer("Не знайдено", show_alert=True)
    await cb.message.answer(expense_card(e), reply_markup=expense_card_kb(e) if has_role(user, "manager") else None)
    await cb.answer()


@router.callback_query(F.data == "exp:cancelled")
async def exp_cancelled(cb: CallbackQuery, db):
    rows = S.recent_cancelled_expenses(db, 10)
    await cb.message.answer("Скасовані витрати — натисніть, щоб відновити:", reply_markup=inline(
        [[(f"{ua_date(r['op_date'])} {r['category'][:22]} {fmt_money(r['amount'])}", f"exp:restore:{r['id']}")] for r in rows]))
    await cb.answer()


@router.callback_query(F.data.startswith("exp:restore:"))
async def exp_restore(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    eid = int(cb.data.split(":")[2])
    S.restore_expense(db, eid, user["telegram_id"])
    await cb.answer("Відновлено", show_alert=True)
    await cb.message.answer(expense_card(S.get_expense(db, eid)), reply_markup=expense_card_kb(S.get_expense(db, eid)))


@router.callback_query(F.data.startswith("exp:repeat:"))
async def exp_repeat(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    e = S.get_expense(db, int(cb.data.split(":")[2]))
    nid = S.add_expense(db, user["telegram_id"], today_local(), e["exp_type"], e["category"], Decimal(e["amount"]), e["comment"])
    await cb.answer("Створено копію на сьогодні")
    await cb.message.answer(expense_card(S.get_expense(db, nid)), reply_markup=expense_card_kb(S.get_expense(db, nid)))


@router.callback_query(F.data.startswith("exp:delask:"))
async def exp_delask(cb: CallbackQuery, db, user):
    eid = int(cb.data.split(":")[2])
    e = S.get_expense(db, eid)
    await cb.message.answer(f"Точно видалити витрату №{eid} «{e['category']}» {fmt_money(e['amount'])}? Прикріплені чеки теж буде видалено.",
                            reply_markup=inline([[("🗑 Так, видалити", f"exp:del:{eid}"), ("Ні", "noop")]]))
    await cb.answer()


class ExpEdit(StatesGroup):
    value = State()


@router.callback_query(F.data.startswith("exp:edit:"))
async def exp_edit(cb: CallbackQuery, state: FSMContext, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    parts = cb.data.split(":")
    eid = int(parts[2])
    if len(parts) == 3:
        await cb.message.answer("Що змінити?", reply_markup=inline([
            [("💶 Суму", f"exp:edit:{eid}:amount"), ("🏷 Категорію", f"exp:edit:{eid}:category")],
            [("📅 Дату", f"exp:edit:{eid}:op_date"), ("💬 Коментар", f"exp:edit:{eid}:comment")],
            [("📂 Тип", f"exp:edit:{eid}:exp_type")]]))
        return await cb.answer()
    field = parts[3]
    if field == "exp_type":
        await cb.message.answer("Тип:", reply_markup=inline([[(v, f"exp:settype:{eid}:{k}")] for k, v in S.EXP_TYPES.items()]))
        return await cb.answer()
    await state.set_state(ExpEdit.value)
    await state.update_data(exp_edit_id=eid, exp_edit_field=field)
    prompts = {"amount": "Нова сума, €:", "category": "Нова категорія:", "op_date": "Нова дата (15.09.2026):", "comment": "Новий коментар:"}
    await cb.message.answer(prompts[field], reply_markup=nav_kb(back=False))
    await cb.answer()


@router.callback_query(F.data.startswith("exp:settype:"))
async def exp_settype(cb: CallbackQuery, db, user):
    _, _, eid, t = cb.data.split(":")
    S.update_expense(db, int(eid), user["telegram_id"], exp_type=t)
    await cb.answer("Змінено")
    e = S.get_expense(db, int(eid))
    await cb.message.answer(expense_card(e), reply_markup=expense_card_kb(e))


@router.message(StateFilter(ExpEdit.value), F.text)
async def exp_edit_value(msg: Message, state: FSMContext, db, user):
    from ..money import parse_money, ParseError
    data = await state.get_data()
    eid, field = data["exp_edit_id"], data["exp_edit_field"]
    try:
        if field == "amount":
            S.update_expense(db, eid, user["telegram_id"], amount=parse_money(msg.text))
        elif field == "op_date":
            d_ = parse_date(msg.text)
            if not d_:
                return await msg.answer("⚠️ Дата як 15.09.2026")
            S.update_expense(db, eid, user["telegram_id"], op_date=d_)
        elif field == "category":
            S.update_expense(db, eid, user["telegram_id"], category=msg.text.strip()[:60])
        else:
            S.update_expense(db, eid, user["telegram_id"], comment=msg.text.strip()[:120])
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.clear()
    e = S.get_expense(db, eid)
    await msg.answer("✅ Змінено.\n" + expense_card(e), reply_markup=expense_card_kb(e))
    await msg.answer("Готово.", reply_markup=main_menu(user["role"]))


@router.message(StateFilter(None), F.text.in_({M_DOCS_PURCH, M_DOCS_EXP}))
async def docs_msg(msg: Message, db, user):
    if not has_role(user, "manager"):
        return
    kind = "purchase" if msg.text == M_DOCS_PURCH else "expense"
    rows = S.list_documents(db, kind)
    if not rows:
        return await msg.answer("Документів ще немає.")
    from ..db import local_dt_str as _l
    await msg.answer("📁 Останні документи (натисніть, щоб отримати файл):", reply_markup=inline(
        [[(f"№{d['ref_id'] or '—'} · {_l(d['uploaded_at'])[:10]} · {d['file_name'][:24]}", f"docs:get:{d['id']}")] for d in rows]))


@router.callback_query(F.data == "rep:custom")
async def rep_custom(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Rep.period)
    await cb.message.answer("Введіть період: <code>01.09.2026 - 15.09.2026</code> або одну дату:", reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Rep.period), F.text)
async def rep_period(msg: Message, state: FSMContext, db, user):
    pr = parse_period(msg.text)
    if not pr:
        return await msg.answer("⚠️ Формат: 01.09.2026 - 15.09.2026")
    target = (await state.get_data()).get("period_target")
    await state.clear()
    if target == "exps":
        return await _send_exp_summary(msg, db, *pr)
    if target in SUMMARY_KINDS:
        return await _send_kind_summary(msg, db, target, *pr)
    await _send_report(msg, db, user, *pr)


async def _send_report(msg: Message, db, user, d1: str, d2: str):
    rep = S.report_period(db, d1, d2)
    await msg.answer(report_text(rep), reply_markup=main_menu(user["role"]))
    kb = export_kb(d1, d2)
    kb.inline_keyboard.append([InlineKeyboardButton(text="🔁 Інший період", callback_data="rep:menu")])
    await msg.answer("Експорт:", reply_markup=kb)


@router.callback_query(F.data.startswith("rep:"))
async def rep_cb(cb: CallbackQuery, state: FSMContext, db, user):
    parts = cb.data.split(":")
    code = parts[1]
    if code == "menu":
        await cb.answer()
        return await cb.message.answer("Період:", reply_markup=period_kb())
    if code in PERIOD_CODES:
        await cb.answer()
        return await _send_report(cb.message, db, user, *_period(code))
    d1, d2 = parts[2], parts[3]
    if code == "full":
        await cb.answer()
        return await _send_report(cb.message, db, user, d1, d2)
    if code == "xlsx":
        await cb.answer("Готую Excel…")
        path = build_excel(db, d1, d2, Path("data/exports") / f"crudo_{d1}_{d2}.xlsx")
        await cb.message.answer_document(FSInputFile(path), caption=f"Звіт {ua_date(d1)} — {ua_date(d2)}: аркуші «Товар», «Зведення», «Оплати», «Продажі за товарами», «Залишки», «Операції»")
    elif code == "csv":
        await cb.answer()
        await cb.message.answer_document(BufferedInputFile(build_csv_movements(db, d1, d2), filename=f"operations_{d1}_{d2}.csv"))
    elif code == "exp":
        ex = S.expenses_period(db, d1, d2)
        if not ex["rows"]:
            await cb.message.answer("Витрат за період немає.")
        else:
            txt = [f"💸 <b>Витрати {ua_date(d1)} — {ua_date(d2)}</b>"]
            for t, label in S.EXP_TYPES.items():
                cats = [(c, v) for (tt, c), v in ex["by_category"].items() if tt == t]
                if not cats:
                    continue
                txt.append(f"\n<b>{label}: {fmt_money(ex['by_type'][t])}</b>")
                for c, v in sorted(cats, key=lambda x: -x[1]):
                    txt.append(f"• {c}: {fmt_money(v)}")
            await cb.message.answer("\n".join(txt))
        await cb.answer()
    elif code == "rec":
        rows = S.reconcile(db, d1, d2)
        if not rows:
            await cb.message.answer("За період немає ні продажів, ні даних каси.")
        else:
            txt = ["🧾 <b>Звірка бот ↔ каса</b>"]
            for r in rows:
                if r["reg_total"] is None:
                    txt.append(f"{ua_date(r['day'])}: бот {fmt_money(r['bot_total'])} · каса — не імпортовано")
                else:
                    flag = "✅" if r["diff"] == 0 else "⚠️"
                    txt.append(f"{ua_date(r['day'])}: бот {fmt_money(r['bot_total'])} · каса {fmt_money(r['reg_total'])} · різниця {fmt_money(r['diff'])} {flag}")
            await cb.message.answer("\n".join(txt))
        await cb.answer()


# ---------------- витрати: ручне введення ----------------

async def e_date(msg, state):
    await msg.answer("💸 Дата витрати:", reply_markup=nav_kb("📅 Сьогодні", back=False))


async def e_type(msg, state):
    await msg.answer("Тип:", reply_markup=nav_kb())
    await msg.answer("Оберіть:", reply_markup=inline([[(v, f"exp:type:{k}")] for k, v in S.EXP_TYPES.items()]))


async def e_category(msg, state):
    data = await state.get_data()
    cats = S.expense_categories(get_db(), data["exp_type"])[:12]
    await msg.answer("Категорія — введіть назву або оберіть:", reply_markup=nav_kb())
    if cats:
        await msg.answer("Останні:", reply_markup=inline([[(c[:40], f"exp:cat:{i}")] for i, c in enumerate(cats)]))
    await state.update_data(cat_options=cats)


async def e_amount(msg, state):
    await msg.answer("Сума, €:", reply_markup=nav_kb())


async def e_comment(msg, state):
    await msg.answer("Коментар (або пропустіть):", reply_markup=nav_kb("⏭ Пропустити"))


eflow = Flow({Exp.date: e_date, Exp.type: e_type, Exp.category: e_category, Exp.amount: e_amount, Exp.comment: e_comment})


@router.callback_query(F.data == "exp:add")
async def exp_add(cb: CallbackQuery, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await cb.answer()
    await eflow.goto(cb.message, state, Exp.date, push=False)


@router.message(StateFilter(Exp), F.text == "◀️ Назад")
async def exp_back(msg: Message, state: FSMContext, user):
    await eflow.back(msg, state, user)


@router.message(StateFilter(Exp.date), F.text)
async def exp_date(msg: Message, state: FSMContext):
    d_ = today_local() if msg.text.startswith("📅") else parse_date(msg.text)
    if not d_:
        return await msg.answer("⚠️ Дата як 15.09.2026")
    await state.update_data(op_date=d_)
    await eflow.goto(msg, state, Exp.type)


@router.callback_query(StateFilter(Exp.type), F.data.startswith("exp:type:"))
async def exp_type(cb: CallbackQuery, state: FSMContext):
    await state.update_data(exp_type=cb.data.split(":")[2])
    await cb.answer()
    await eflow.goto(cb.message, state, Exp.category)


@router.callback_query(StateFilter(Exp.category), F.data.startswith("exp:cat:"))
async def exp_cat_pick(cb: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    await state.update_data(category=data["cat_options"][int(cb.data.split(":")[2])])
    await cb.answer()
    await eflow.goto(cb.message, state, Exp.amount)


@router.message(StateFilter(Exp.category), F.text)
async def exp_cat_text(msg: Message, state: FSMContext):
    await state.update_data(category=msg.text.strip()[:60])
    await eflow.goto(msg, state, Exp.amount)


@router.message(StateFilter(Exp.amount), F.text)
async def exp_amount(msg: Message, state: FSMContext):
    from ..money import parse_money, ParseError
    try:
        v = parse_money(msg.text)
    except ParseError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.update_data(amount=str(v))
    await eflow.goto(msg, state, Exp.comment)


@router.message(StateFilter(Exp.comment), F.text)
async def exp_comment(msg: Message, state: FSMContext, db, user):
    data = await state.get_data()
    comment = None if msg.text.startswith("⏭") else msg.text.strip()[:120]
    eid = S.add_expense(db, user["telegram_id"], data["op_date"], data["exp_type"], data["category"], Decimal(data["amount"]), comment)
    await state.update_data(exp_id=eid)
    await state.set_state(Exp.photo)
    await msg.answer(f"✅ Витрату №{eid} записано: {S.EXP_TYPES[data['exp_type']]} · {data['category']} · {fmt_money(data['amount'])} · {ua_date(data['op_date'])}\n"
                     "📎 Надішліть фото чека/рахунку для архіву або пропустіть:", reply_markup=nav_kb("⏭ Пропустити", back=False))


@router.message(StateFilter(Exp.photo), F.photo | F.document)
async def exp_photo(msg: Message, state: FSMContext, db, user):
    from .tasks import save_document
    data = await state.get_data()
    buf = io.BytesIO()
    if msg.document:
        await msg.bot.download(msg.document, destination=buf)
        name = msg.document.file_name or "receipt"
    else:
        await msg.bot.download(msg.photo[-1], destination=buf)
        name = "receipt.jpg"
    save_document(db, user["telegram_id"], "expense", data["exp_id"], name, buf.getvalue())
    await state.clear()
    await msg.answer("📁 Чек збережено в архів витрат.", reply_markup=main_menu(user["role"]))


@router.message(StateFilter(Exp.photo), F.text)
async def exp_photo_skip(msg: Message, state: FSMContext, user):
    await state.clear()
    await msg.answer("Гаразд, без документа.", reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "exp:list")
async def exp_list(cb: CallbackQuery, db, user):
    rows = S.recent_expenses(db, 10)
    if not rows:
        await cb.message.answer("Витрат ще немає.")
    else:
        await cb.message.answer("Останні витрати — натисніть, щоб відкрити:", reply_markup=inline(
            [[(f"{ua_date(r['op_date'])} {r['category'][:22]} {fmt_money(r['amount'])}", f"exp:view:{r['id']}")] for r in rows]))
    await cb.answer()


@router.callback_query(F.data.startswith("exp:del:"))
async def exp_del(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    n = S.cancel_expense(db, int(cb.data.split(":")[2]), user["telegram_id"])
    await cb.answer("Витрату скасовано" + (f", видалено чеків: {n}" if n else ""), show_alert=True)


# ---------------- очищення бази ----------------

@router.message(Command("reset_db"))
async def reset_db_start(msg: Message, state: FSMContext, user):
    if user["role"] != "admin":
        return
    await state.set_state(Sett.reset_confirm)
    await msg.answer("⚠️ Це видалить УСІ товари, партії, продажі, закупівлі, списання й витрати (користувачі лишаться). "
                     "Перед видаленням буде зроблено резервну копію.\nЩоб підтвердити, надішліть слово <b>ВИДАЛИТИ</b>.",
                     reply_markup=nav_kb(back=False))


@router.message(StateFilter(Sett.reset_confirm), F.text)
async def reset_db_confirm(msg: Message, state: FSMContext, db, user):
    await state.clear()
    if msg.text.strip() != "ВИДАЛИТИ":
        return await msg.answer("Скасовано, нічого не видалено.", reply_markup=main_menu(user["role"]))
    path = db.make_backup()
    await msg.answer_document(FSInputFile(path), caption="💾 Копія бази перед очищенням — збережіть.")
    S.reset_all_data(db, user["telegram_id"])
    await msg.answer("🧹 Базу очищено. Тепер можна імпортувати історію.", reply_markup=main_menu(user["role"]))


# ---------------- налаштування ----------------

def settings_kb(user):
    rows = [[("👥 Користувачі", "set:users"), ("🕘 Графік ярмарків", "set:schedule")], [("📁 Документи", "set:docs")]]
    if user["role"] == "admin":
        rows += [[("➕ Додати користувача", "set:adduser")],
                 [("💾 Резервна копія зараз", "set:backup"), ("♻️ Відновити з файлу", "set:restore")],
                 [("🧾 Імпорт звіту каси", "set:cashimport")],
                 [("📥 Імпорт товарів з файлу", "set:prodimport"), ("📥 Імпорт початкових залишків", "set:openimport")],
                 [("📥 Імпорт історії руху товарів", "set:histimport"), ("📥 Імпорт витрат", "set:expimport")],
                 [("🧾 Імпорт чеків Octobox (по товарах)", "set:octobox")], [("🔗 Прив'язки назв каси", "set:aliases")]]
    return inline(rows)


@router.message(F.text == M_SETTINGS)
async def settings_menu(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return
    await state.clear()
    await msg.answer(f"⚙️ <b>Налаштування</b>\nЧасовий пояс звітів: Europe/Vienna · Ваш ID: <code>{user['telegram_id']}</code>",
                     reply_markup=main_menu(user["role"]))
    await msg.answer("Оберіть:", reply_markup=settings_kb(user))


@router.callback_query(F.data == "set:users")
async def set_users(cb: CallbackQuery, db, user):
    rows = S.list_users(db)
    txt = "👥 <b>Користувачі</b>\n" + "\n".join(
        f"• {u['name'] or '—'} — {S.ROLES[u['role']]} (<code>{u['telegram_id']}</code>){'' if u['active'] else ' · вимкнено'}" for u in rows)
    kb = None
    if user["role"] == "admin":
        kb = inline([[(f"🔄 Роль: {u['name'] or u['telegram_id']}", f"set:role:{u['telegram_id']}"),
                      (f"🚫", f"set:deluser:{u['telegram_id']}")]
                     for u in rows if u["active"] and u["telegram_id"] != user["telegram_id"]])
    await cb.message.answer(txt + ("\n\n🔄 змінити роль · 🚫 вимкнути доступ:" if kb and kb.inline_keyboard else ""), reply_markup=kb if kb and kb.inline_keyboard else None)
    await cb.answer()


@router.callback_query(F.data.startswith("set:role:"))
async def set_role_pick(cb: CallbackQuery, db, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    tid = int(cb.data.split(":")[2])
    u = db.one("SELECT * FROM users WHERE telegram_id=?", (tid,))
    await cb.message.answer(f"Нова роль для {u['name'] or tid} (зараз {S.ROLES[u['role']]}):",
                            reply_markup=inline([[(v, f"set:setrole:{tid}:{k}")] for k, v in S.ROLES.items() if k != u["role"]]))
    await cb.answer()


@router.callback_query(F.data.startswith("set:setrole:"))
async def set_role_apply(cb: CallbackQuery, db, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    _, _, tid, role = cb.data.split(":")
    S.upsert_user(db, int(tid), role)
    await cb.answer(f"Роль змінено на {S.ROLES[role]}", show_alert=True)
    try:
        await cb.bot.send_message(int(tid), f"ℹ️ Вашу роль у боті змінено на «{S.ROLES[role]}». Натисніть /start, щоб оновити меню.")
    except Exception:
        pass


@router.callback_query(F.data.startswith("set:deluser:"))
async def set_deluser(cb: CallbackQuery, db, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    S.deactivate_user(db, int(cb.data.split(":")[2]))
    await cb.answer("Доступ вимкнено", show_alert=True)


@router.callback_query(F.data == "set:adduser")
async def set_adduser(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.add_user)
    await cb.message.answer("Надішліть: <code>ID роль Ім'я</code>\nролі: seller / manager / admin\nНаприклад: <code>123456789 seller Марія</code>\n"
                            "(ID користувач дізнається командою /id або з повідомлення «Доступ заборонено»)",
                            reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.add_user), F.text)
async def set_adduser_text(msg: Message, state: FSMContext, db, user):
    parts = msg.text.split(maxsplit=2)
    if len(parts) < 2 or not parts[0].isdigit() or parts[1] not in S.ROLES:
        return await msg.answer("⚠️ Формат: <code>123456789 seller Марія</code>")
    S.upsert_user(db, int(parts[0]), parts[1], parts[2] if len(parts) > 2 else "")
    await state.clear()
    await msg.answer(f"✅ Користувача {parts[0]} додано як {S.ROLES[parts[1]]}.", reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "set:backup")
@router.message(Command("backup"))
async def backup(event: Message | CallbackQuery, db, user):
    if user["role"] != "admin":
        return
    msg = event.message if isinstance(event, CallbackQuery) else event
    path = db.make_backup()
    await msg.answer_document(FSInputFile(path), caption=f"💾 Резервна копія бази · {dt.datetime.now(TZ):%d.%m.%Y %H:%M}\n"
                              "Збережіть файл. Для відновлення: Налаштування → Відновити з файлу.")
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "set:restore")
async def restore_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.db_file)
    await cb.message.answer("♻️ Надішліть файл бази (.db), отриманий як резервна копія. <b>Поточні дані буде замінено</b> "
                            "(перед цим створиться страхувальна копія).", reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.db_file), F.document)
async def restore_file(msg: Message, state: FSMContext, db, user):
    doc = msg.document
    if not doc.file_name.endswith(".db"):
        return await msg.answer("⚠️ Очікую файл .db")
    tmp = Path("data/restore_tmp.db")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    await msg.bot.download(doc, destination=tmp)
    try:
        db.restore_from(tmp)
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося відновити: {e}")
    finally:
        tmp.unlink(missing_ok=True)
    await state.clear()
    await msg.answer("✅ Базу відновлено з файлу.", reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "set:prodimport")
async def prod_import_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.products_file)
    await cb.message.answer(
        "📥 Надішліть файл товарів (CSV або Excel) з колонками:\n"
        "<code>name; category; sale_mode; piece_grams; retail_price; sku</code>\n"
        "category: cheese / meat / pasta (або сир / м'ясо / паста); sale_mode: weight / piece (або вага / шт).\n"
        "Товари з такою самою назвою пропускаються, тож файл можна надсилати повторно.",
        reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.products_file), F.document)
async def prod_import_file(msg: Message, state: FSMContext, user):
    from ..tools import import_products
    doc = msg.document
    buf = io.BytesIO()
    await msg.bot.download(doc, destination=buf)
    try:
        created, skipped, errors = import_products(doc.file_name, buf.getvalue())
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося прочитати файл: {e}")
    await state.clear()
    txt = f"✅ Створено товарів: {created}\nПропущено (вже були): {skipped}"
    if errors:
        txt += "\n\n⚠️ Помилки:\n" + "\n".join(errors[:15])
    await msg.answer(txt, reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "set:openimport")
async def open_import_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.opening_file)
    await cb.message.answer(
        "📥 Надішліть файл початкових залишків (CSV або Excel) з колонками:\n"
        "<code>name; kg; price_per_kg; expiry</code>\n"
        "name — точна назва товару як у боті; kg — фактичний залишок; price_per_kg — закупівельна ціна; "
        "expiry — термін придатності (необов'язково).\n⚠️ Кожне надсилання додає нові партії — надсилайте один раз.",
        reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.opening_file), F.document)
async def open_import_file(msg: Message, state: FSMContext, user):
    from ..tools import import_opening_stock
    doc = msg.document
    buf = io.BytesIO()
    await msg.bot.download(doc, destination=buf)
    try:
        created, errors = import_opening_stock(doc.file_name, buf.getvalue(), user["telegram_id"])
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося прочитати файл: {e}")
    await state.clear()
    txt = f"✅ Створено партій початкового залишку: {created}"
    if errors:
        txt += "\n\n⚠️ Помилки:\n" + "\n".join(errors[:15])
    await msg.answer(txt, reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "set:histimport")
async def hist_import_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.history_file)
    await cb.message.answer(
        "📥 Надішліть файл історії (Excel з аркушем «Рух товарів»). По кожному періоду і товару бот створить закупівлю, "
        "продаж (спосіб оплати «Інше»), списання і вирівняє залишок до колонки closing_kg. Товари, яких немає, буде створено.\n"
        "Повторне надсилання того ж файлу нічого не дублює. Це може тривати до хвилини.",
        reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.history_file), F.document)
async def hist_import_file(msg: Message, state: FSMContext, user):
    from ..tools import import_history
    doc = msg.document
    buf = io.BytesIO()
    await msg.bot.download(doc, destination=buf)
    await msg.answer("⏳ Імпортую…")
    try:
        st = import_history(doc.file_name, buf.getvalue(), user["telegram_id"])
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося прочитати файл: {e}")
    await state.clear()
    txt = (f"✅ Історію імпортовано.\nРядків (період × товар): {st['rows']}, пропущено як уже імпортовані: {st['skipped']}\n"
           f"Створено нових товарів: {st['products_created']}\nВирівнювань до звіту: {st['aligned']}")
    if st["errors"]:
        txt += "\n\n⚠️ Помилки:\n" + "\n".join(st["errors"][:15])
    await msg.answer(txt, reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "set:expimport")
async def exp_import_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.expenses_file)
    await cb.message.answer("📥 Надішліть файл витрат (Excel з аркушем «Витрати» або CSV): <code>date; type; category; amount; comment</code>. "
                            "Дублікати (та сама дата, тип, категорія, сума) пропускаються.", reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.expenses_file), F.document)
async def exp_import_file(msg: Message, state: FSMContext, user):
    from ..tools import import_expenses
    doc = msg.document
    buf = io.BytesIO()
    await msg.bot.download(doc, destination=buf)
    try:
        created, skipped, errors = import_expenses(doc.file_name, buf.getvalue(), user["telegram_id"])
    except Exception as e:
        return await msg.answer(f"⚠️ Не вдалося прочитати файл: {e}")
    await state.clear()
    txt = f"✅ Витрат додано: {created}, пропущено (дублікати): {skipped}"
    if errors:
        txt += "\n\n⚠️ Помилки:\n" + "\n".join(errors[:15])
    await msg.answer(txt, reply_markup=main_menu(user["role"]))


# ---------------- Octobox: імпорт чеків по позиціях ----------------

@router.callback_query(F.data == "set:octobox")
async def octo_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.clear()
    await state.set_state(Sett.octobox_file)
    await cb.message.answer(
        "🧾 Надішліть експорт Octobox <b>по позиціях</b> (Excel з колонками Produkt, Betrag, Gewicht (kg)). "
        "Бот створить продажі по чеках і спише залишки. Чеки, що вже імпортовані, пропускаються.\n"
        "⚠️ Спершу внесіть закупівлі за цей період — інакше бот дооприбуткує нестачу автоматично і позначить це.",
        reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.octobox_file), F.document)
async def octo_file(msg: Message, state: FSMContext, db, user):
    import base64
    from ..tools import parse_octobox_lines, octobox_summary, last_octobox_date
    buf = io.BytesIO()
    await msg.bot.download(msg.document, destination=buf)
    try:
        receipts = parse_octobox_lines(msg.document.file_name, buf.getvalue())
    except Exception as e:
        return await msg.answer(f"⚠️ {e}")
    if not receipts:
        return await msg.answer("⚠️ У файлі немає чеків.")
    last = last_octobox_date(db) or db.one("SELECT MAX(sale_date) d FROM sales WHERE status='done'")["d"]
    since = None
    if last:
        since = (dt.date.fromisoformat(last) + dt.timedelta(days=1)).isoformat()
    await state.update_data(octo_b64=base64.b64encode(buf.getvalue()).decode(), octo_name=msg.document.file_name, octo_since=since)
    await state.set_state(Sett.octobox_review)
    await _octo_review(msg, state, db)


async def _octo_review(msg: Message, state: FSMContext, db):
    import base64
    from ..tools import parse_octobox_lines, octobox_summary
    data = await state.get_data()
    receipts = parse_octobox_lines(data["octo_name"], base64.b64decode(data["octo_b64"]))
    sm = octobox_summary(receipts)
    lines = [f"🧾 Чеків: {sm['count']} · {ua_date(sm['date_from'])} — {ua_date(sm['date_to'])} · днів: {len(sm['days'])}",
             "\n<b>Прив'язка назв каси до товарів бота</b> (натисніть, щоб змінити):"]
    names = sorted(sm["matches"].items(), key=lambda kv: (kv[1]["how"] != "none", kv[1]["how"] != "auto", kv[0]))
    unmatched = [n for n, m in names if m["how"] == "none"]
    for n, m in names:
        mark = {"none": "❌", "auto": "🔸", "exact": "✅", "alias": "🔗"}[m["how"]]
        lines.append(f"{mark} {n} → {m['product_name'] or '<b>не знайдено</b>'} ({m['count']})")
    lines.append("\n✅ точний збіг · 🔗 збережена прив'язка · 🔸 підібрано автоматично — перевірте · ❌ не знайдено (буде пропущено)")
    await msg.answer("\n".join(lines))
    kb = [[(f"✏️ {n[:36]}", f"octo:alias:{i}")] for i, (n, m) in enumerate(names)]
    await state.update_data(octo_names=[n for n, _ in names])
    since = data.get("octo_since")
    kb.append([(f"▶️ Імпортувати з {ua_date(since)}" if since else "▶️ Імпортувати всі чеки", "octo:run:since")])
    if since:
        kb.append([("▶️ Імпортувати всі чеки з файлу", "octo:run:all")])
    kb.append([("📆 Інша дата початку", "octo:date")])
    await msg.answer("Дія:", reply_markup=inline(kb))


@router.callback_query(StateFilter(Sett.octobox_review), F.data.startswith("octo:alias:"))
async def octo_alias(cb: CallbackQuery, state: FSMContext, db):
    data = await state.get_data()
    name = data["octo_names"][int(cb.data.split(":")[2])]
    await state.update_data(octo_alias_name=name)
    await state.set_state(Sett.octobox_alias)
    await cb.message.answer(f"Який товар бота відповідає «{name}»?", reply_markup=product_picker(db, "al", show_price=False))
    await cb.answer()


@router.callback_query(StateFilter(Sett.octobox_alias), F.data.startswith("pp:al:"))
async def octo_alias_pick(cb: CallbackQuery, state: FSMContext, db, user):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "al", cat, page, show_price=False))
        return await cb.answer()
    data = await state.get_data()
    S.set_alias(db, data["octo_alias_name"], int(parts[3]), user["telegram_id"])
    await cb.answer("Прив'язано")
    await state.set_state(Sett.octobox_review)
    await _octo_review(cb.message, state, db)


@router.callback_query(StateFilter(Sett.octobox_review), F.data == "octo:date")
async def octo_date(cb: CallbackQuery, state: FSMContext):
    await state.set_state(Sett.octobox_date)
    await cb.message.answer("З якої дати імпортувати чеки? (наприклад 01.09.2026)", reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.octobox_date), F.text)
async def octo_date_text(msg: Message, state: FSMContext, db):
    d_ = parse_date(msg.text)
    if not d_:
        return await msg.answer("⚠️ Дата як 01.09.2026")
    await state.update_data(octo_since=d_)
    await state.set_state(Sett.octobox_review)
    await _octo_review(msg, state, db)


@router.callback_query(StateFilter(Sett.octobox_review), F.data.startswith("octo:run:"))
async def octo_run(cb: CallbackQuery, state: FSMContext, db, user):
    import base64
    from ..tools import parse_octobox_lines, import_octobox
    data = await state.get_data()
    since = data.get("octo_since") if cb.data.endswith(":since") else None
    await cb.answer()
    await cb.message.answer("⏳ Імпортую чеки…")
    receipts = parse_octobox_lines(data["octo_name"], base64.b64decode(data["octo_b64"]))
    try:
        from ..tools import learn_register_prices
        learned = learn_register_prices(receipts, user["telegram_id"])
        st = import_octobox(receipts, user["telegram_id"], since=since)
    except Exception as e:
        return await cb.message.answer(f"⚠️ Помилка імпорту: {e}")
    await state.clear()
    txt = [f"✅ Створено продажів: {st['created']}", f"Пропущено як уже імпортовані: {st['skipped_dup']}"]
    if learned:
        txt.append(f"💶 Ціни каси за кг оновлено для {len(learned)} товарів (використовуються для ваги при синхронізації)")
    if st.get("completed"):
        txt.append(f"Доповнено раніше імпортованих чеків: {st['completed']} (+{st['completed_lines']} поз.)")
    if st.get("complete_conflicts"):
        txt.append("⚠️ Чеки з розбіжністю сум (не доповнено): " + "; ".join(st["complete_conflicts"][:8]))
    if st["skipped_old"]:
        txt.append(f"Пропущено як старіші за дату початку: {st['skipped_old']}")
    if st["refunds"]:
        txt.append(f"Сторно опрацьовано: {st['refunds']}")
    if st["refunds_unmatched"]:
        txt.append("⚠️ Сторно без пари (перевірте вручну): " + "; ".join(st["refunds_unmatched"][:10]))
    if st["unmatched"]:
        txt.append("⚠️ Пропущено позиції без прив'язки: " + ", ".join(f"{k} ({v})" for k, v in st["unmatched"].items()))
    if st["shortfalls"]:
        from collections import Counter
        c = Counter(x.split(" +")[0] for x in st["shortfalls"])
        txt.append("⚠️ Продано більше, ніж було в залишку — нестачу дооприбутковано автоматично (внесіть закупівлі й перевірте партії): "
                   + ", ".join(f"{k} ({v} поз.)" for k, v in c.items()))
    await cb.message.answer("\n".join(txt), reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "set:aliases")
async def aliases_list(cb: CallbackQuery, db):
    rows = S.list_aliases(db)
    if not rows:
        await cb.message.answer("Збережених прив'язок ще немає — вони з'являються, коли ви вручну зіставляєте назви під час імпорту чеків.")
    else:
        await cb.message.answer("🔗 <b>Назви каси → товари бота</b>\n" + "\n".join(f"• {r['alias_raw']} → {r['product_name']}" for r in rows))
    await cb.answer()


@router.callback_query(F.data == "set:cashimport")
async def cash_start(cb: CallbackQuery, state: FSMContext, user):
    if user["role"] != "admin":
        return await cb.answer("Лише адміністратор", show_alert=True)
    await state.set_state(Sett.cash_file)
    await cb.message.answer("🧾 Надішліть звіт каси (CSV або XLSX) з колонками «Дата» + «Готівка»/«Картка», "
                            "або «Дата» + «Сума» + «Оплата». Дані використовуються лише для звірки.", reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Sett.cash_file), F.document)
async def cash_file(msg: Message, state: FSMContext):
    doc = msg.document
    buf = io.BytesIO()
    await msg.bot.download(doc, destination=buf)
    try:
        days = parse_cash_report(doc.file_name, buf.getvalue())
    except ValueError as e:
        return await msg.answer(f"⚠️ {e}")
    await state.update_data(cash_days={k: {"cash": str(v["cash"]), "card": str(v["card"])} for k, v in days.items()},
                            cash_name=doc.file_name)
    await state.set_state(Sett.cash_confirm)
    txt = "\n".join(f"{ua_date(k)}: готівка {fmt_money(v['cash'])}, картка {fmt_money(v['card'])}" for k, v in sorted(days.items()))
    await msg.answer(f"Розпізнано {len(days)} дн.:\n{txt}", reply_markup=inline([[("✅ Зберегти для звірки", "set:cashsave")]]))


@router.callback_query(StateFilter(Sett.cash_confirm), F.data == "set:cashsave")
async def cash_save(cb: CallbackQuery, state: FSMContext, db, user):
    data = await state.get_data()
    for day, v in data["cash_days"].items():
        S.save_cash_day(db, user["telegram_id"], day, Decimal(v["cash"]), Decimal(v["card"]), data.get("cash_name"))
    await state.clear()
    await cb.message.answer("✅ Дані каси збережено. Звірка: Звіти → період → «Звірка з касою».", reply_markup=main_menu(user["role"]))
    await cb.answer()
