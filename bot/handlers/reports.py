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
from aiogram.types import BufferedInputFile, CallbackQuery, FSInputFile, Message

from .. import services as S
from ..cash_import import parse_cash_report
from ..config import TZ, settings
from ..db import today_local
from ..export import build_csv_movements, build_excel
from ..keyboards import CANCEL, M_REPORTS, M_SETTINGS, inline, main_menu, nav_kb
from ..money import fmt_grams, fmt_money
from .common import has_role, parse_period, ua_date

router = Router(name="reports")


class Rep(StatesGroup):
    period = State()


class Sett(StatesGroup):
    add_user = State()
    cash_file = State()
    db_file = State()
    cash_confirm = State()


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
    return today.isoformat(), today.isoformat()


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
    out.append(f"Поточні залишки: {fmt_grams(rep['stock_grams'])} на {fmt_money(rep['stock_value'])}")
    if rep["by_product"]:
        out.append("\n<b>Продажі за товарами</b> (виручка · вал. прибуток · маржа)")
        for e in rep["by_product"][:15]:
            out.append(f"• {e['name']}: {fmt_grams(e['grams'])} · {fmt_money(e['amount'])} · {fmt_money(e['gross_profit'])} · {e['margin_pct']:.0f} %")
    out.append("\n<i>Валовий прибуток = виручка − собівартість проданого (з транспортом). Операційні витрати не враховано.</i>")
    return "\n".join(out)


def period_kb():
    return inline([[("Сьогодні", "rep:today"), ("Вчора", "rep:yday")],
                   [("Цей тиждень", "rep:week"), ("Цей місяць", "rep:month"), ("Минулий місяць", "rep:pmonth")],
                   [("📆 Ввести період", "rep:custom")]])


def export_kb(d1: str, d2: str):
    return inline([[("📊 Excel (структура звіту)", f"rep:xlsx:{d1}:{d2}"), ("📄 CSV операцій", f"rep:csv:{d1}:{d2}")],
                   [("🧾 Звірка з касою", f"rep:rec:{d1}:{d2}")]])


@router.message(F.text == M_REPORTS)
async def reports(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await msg.answer("Звіти доступні менеджеру й адміністратору.")
    await state.clear()
    await msg.answer("📈 <b>Звіти</b> — оберіть період:", reply_markup=main_menu(user["role"]))
    await msg.answer("Період:", reply_markup=period_kb())


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
    await state.clear()
    await _send_report(msg, db, user, *pr)


async def _send_report(msg: Message, db, user, d1: str, d2: str):
    rep = S.report_period(db, d1, d2)
    await msg.answer(report_text(rep), reply_markup=main_menu(user["role"]))
    await msg.answer("Експорт:", reply_markup=export_kb(d1, d2))


@router.callback_query(F.data.startswith("rep:"))
async def rep_cb(cb: CallbackQuery, db, user):
    parts = cb.data.split(":")
    code = parts[1]
    if code in ("today", "yday", "week", "month", "pmonth"):
        await cb.answer()
        return await _send_report(cb.message, db, user, *_period(code))
    d1, d2 = parts[2], parts[3]
    if code == "xlsx":
        await cb.answer("Готую Excel…")
        path = build_excel(db, d1, d2, Path("data/exports") / f"crudo_{d1}_{d2}.xlsx")
        await cb.message.answer_document(FSInputFile(path), caption=f"Звіт {ua_date(d1)} — {ua_date(d2)}: аркуші «Товар», «Зведення», «Оплати», «Продажі за товарами», «Залишки», «Операції»")
    elif code == "csv":
        await cb.answer()
        await cb.message.answer_document(BufferedInputFile(build_csv_movements(db, d1, d2), filename=f"operations_{d1}_{d2}.csv"))
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


# ---------------- налаштування ----------------

def settings_kb(user):
    rows = [[("👥 Користувачі", "set:users")]]
    if user["role"] == "admin":
        rows += [[("➕ Додати користувача", "set:adduser")],
                 [("💾 Резервна копія зараз", "set:backup"), ("♻️ Відновити з файлу", "set:restore")],
                 [("🧾 Імпорт звіту каси", "set:cashimport")]]
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
        kb = inline([[(f"🚫 {u['name'] or u['telegram_id']}", f"set:deluser:{u['telegram_id']}")]
                     for u in rows if u["active"] and u["telegram_id"] != user["telegram_id"]])
    await cb.message.answer(txt + ("\n\nВимкнути доступ:" if kb and kb.inline_keyboard else ""), reply_markup=kb if kb and kb.inline_keyboard else None)
    await cb.answer()


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
