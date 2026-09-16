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
from ..export import build_csv_movements, build_excel
from ..keyboards import CANCEL, M_REPORTS, M_SETTINGS, inline, main_menu, nav_kb, product_picker
from ..money import fmt_grams, fmt_money
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


def period_kb():
    return inline([[("Сьогодні", "rep:today"), ("Вчора", "rep:yday")],
                   [("Цей тиждень", "rep:week"), ("Цей місяць", "rep:month"), ("Минулий місяць", "rep:pmonth")],
                   [("📆 Ввести період", "rep:custom")]])


def export_kb(d1: str, d2: str):
    return inline([[("📊 Excel (структура звіту)", f"rep:xlsx:{d1}:{d2}"), ("📄 CSV операцій", f"rep:csv:{d1}:{d2}")],
                   [("🧾 Звірка з касою", f"rep:rec:{d1}:{d2}"), ("💸 Витрати за період", f"rep:exp:{d1}:{d2}")]])


@router.message(F.text == M_REPORTS)
async def reports(msg: Message, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await msg.answer("Звіти доступні менеджеру й адміністратору.")
    await state.clear()
    await msg.answer("📈 <b>Звіти</b> — оберіть період:", reply_markup=main_menu(user["role"]))
    await msg.answer("Період:", reply_markup=period_kb())
    await msg.answer("Витрати:", reply_markup=inline([[("➕ Додати витрату", "exp:add"), ("🗑 Останні витрати", "exp:list")]]))


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
    await state.clear()
    await msg.answer(f"✅ Витрату №{eid} записано: {S.EXP_TYPES[data['exp_type']]} · {data['category']} · {fmt_money(data['amount'])} · {ua_date(data['op_date'])}",
                     reply_markup=main_menu(user["role"]))


@router.callback_query(F.data == "exp:list")
async def exp_list(cb: CallbackQuery, db, user):
    rows = S.recent_expenses(db, 10)
    if not rows:
        await cb.message.answer("Витрат ще немає.")
    else:
        await cb.message.answer("Останні витрати (натисніть, щоб видалити помилкову):", reply_markup=inline(
            [[(f"{ua_date(r['op_date'])} {r['category'][:22]} {fmt_money(r['amount'])}", f"exp:del:{r['id']}")] for r in rows]))
    await cb.answer()


@router.callback_query(F.data.startswith("exp:del:"))
async def exp_del(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    S.cancel_expense(db, int(cb.data.split(":")[2]), user["telegram_id"])
    await cb.answer("Витрату скасовано", show_alert=True)


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
    rows = [[("👥 Користувачі", "set:users")]]
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
        st = import_octobox(receipts, user["telegram_id"], since=since)
    except Exception as e:
        return await cb.message.answer(f"⚠️ Помилка імпорту: {e}")
    await state.clear()
    txt = [f"✅ Створено продажів: {st['created']}", f"Пропущено як уже імпортовані: {st['skipped_dup']}"]
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
