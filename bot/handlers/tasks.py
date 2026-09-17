"""Завдання, зміни каси (відкрити/закрити + графік), документи."""
from __future__ import annotations

import datetime as dt
import re
from decimal import Decimal
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, FSInputFile, Message

from .. import services as S
from ..config import TZ
from ..db import get_db, local_dt_str, today_local
from ..keyboards import menu_for, M_SHIFT_CLOSE, M_SHIFT_OPEN, M_TASKS, SKIP, inline, main_menu, nav_kb
from ..money import ParseError, fmt_grams, fmt_money, parse_money
from .common import has_role, parse_date, ua_date

router = Router(name="tasks")

DOCS_DIR = Path("data/docs")


# ======================= завдання =======================

class Task(StatesGroup):
    who = State()
    text = State()
    due = State()


def _task_line(t) -> str:
    who = t["assignee_name"] or ("усім" if t["assignee_id"] is None else str(t["assignee_id"]))
    due = f" · до {ua_date(t['due_date'])}" if t["due_date"] else ""
    late = " ⏰" if t["due_date"] and t["due_date"] < today_local() else ""
    return f"№{t['id']} {t['text']} — <i>{who}</i>{due}{late}"


@router.message(F.text == M_TASKS)
async def tasks_menu(msg: Message, state: FSMContext, db, user):
    await state.clear()
    rows = S.tasks_for(db, user["telegram_id"], user["role"])
    txt = "📋 <b>Завдання</b>\n" + ("\n".join(_task_line(t) for t in rows) if rows else "Відкритих завдань немає.")
    await msg.answer(txt, reply_markup=menu_for(user))
    kb = [[(f"✅ №{t['id']}", f"task:done:{t['id']}") for t in rows[i:i + 3]] for i in range(0, min(len(rows), 12), 3)]
    if has_role(user, "manager"):
        kb.append([("➕ Нове завдання", "task:new"), ("✔️ Виконані", "task:done_list")])
        if rows:
            kb.append([(f"🗑 №{t['id']}", f"task:cancel:{t['id']}") for t in rows[:4]])
    else:
        kb.append([("✔️ Виконані", "task:done_list")])
    await msg.answer("Дія:", reply_markup=inline(kb))


@router.callback_query(F.data == "task:done_list")
async def task_done_list(cb: CallbackQuery, db, user):
    rows = S.done_tasks(db, 30, None if has_role(user, "manager") else user["telegram_id"])
    if not rows:
        await cb.message.answer("За останні 30 днів виконаних завдань немає.")
    else:
        txt = ["✔️ <b>Виконані за 30 днів</b>"]
        for t in rows:
            st = "✅" if t["status"] == "done" else "🗑"
            txt.append(f"{st} №{t['id']} {t['text']} — {t['done_name'] or t['done_by']}, {local_dt_str(t['done_at'])}")
        await cb.message.answer("\n".join(txt))
    await cb.answer()


@router.callback_query(F.data.startswith("task:done:"))
async def task_done(cb: CallbackQuery, db, user):
    t = S.task_done(db, int(cb.data.split(":")[2]), user["telegram_id"])
    if not t:
        return await cb.answer("Завдання вже закрите", show_alert=True)
    await cb.answer("Виконано ✅")
    if t["created_by"] != user["telegram_id"]:
        try:
            await cb.bot.send_message(t["created_by"], f"✅ {user['name'] or user['telegram_id']} виконав(ла) завдання №{t['id']}: {t['text']}")
        except Exception:
            pass
    await cb.message.answer(f"✅ Завдання №{t['id']} виконано.")


@router.callback_query(F.data.startswith("task:cancel:"))
async def task_cancel(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    S.task_cancel(db, int(cb.data.split(":")[2]), user["telegram_id"])
    await cb.answer("Скасовано", show_alert=True)


@router.callback_query(F.data == "task:new")
async def task_new(cb: CallbackQuery, state: FSMContext, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await state.set_state(Task.who)
    users = [u for u in S.list_users(db) if u["active"]]
    kb = [[(f"{u['name'] or u['telegram_id']} ({S.ROLES[u['role']]})", f"task:who:{u['telegram_id']}")] for u in users]
    kb.append([("👥 Усім", "task:who:all")])
    await cb.message.answer("Кому:", reply_markup=nav_kb(back=False))
    await cb.message.answer("Виконавець:", reply_markup=inline(kb))
    await cb.answer()


@router.callback_query(StateFilter(Task.who), F.data.startswith("task:who:"))
async def task_who(cb: CallbackQuery, state: FSMContext):
    v = cb.data.split(":")[2]
    await state.update_data(task_who=None if v == "all" else int(v))
    await state.set_state(Task.text)
    await cb.message.answer("Текст завдання (або оберіть шаблон):", reply_markup=nav_kb("📦 Дозамовити…", "✂️ Списати прострочене", "🧾 Звірити касу", back=False))
    await cb.answer()


@router.message(StateFilter(Task.text), F.text)
async def task_text(msg: Message, state: FSMContext, db):
    text = msg.text.strip()
    if text.startswith("📦 Дозамовити"):
        low = S.low_stock(db)
        text = "Дозамовити: " + ("; ".join(f"{r['product']['name']} (є {fmt_grams(r['grams'])})" for r in low) if low else "(вкажіть товари)")
    elif text.startswith("✂️ Списати"):
        exp = S.batches_expiring(db, 3)
        text = "Списати прострочене: " + ("; ".join(f"{r['product_name']} {fmt_grams(r['grams_left'])} до {ua_date(r['expiry_date'])}" for r in exp) if exp else "перевірити терміни")
    elif text.startswith("🧾 Звірити"):
        text = "Звірити касу Octobox з ботом за вчора (Звіти → Звірка з касою)"
    await state.update_data(task_text=text[:500])
    await state.set_state(Task.due)
    await msg.answer("Термін (дата) або пропустіть:", reply_markup=nav_kb(SKIP, "📅 Сьогодні", "📅 Завтра"))


@router.message(StateFilter(Task.due), F.text)
async def task_due(msg: Message, state: FSMContext, db, user):
    t = msg.text
    if t == SKIP:
        due = None
    elif t.startswith("📅 Сьогодні"):
        due = today_local()
    elif t.startswith("📅 Завтра"):
        due = (dt.date.fromisoformat(today_local()) + dt.timedelta(days=1)).isoformat()
    else:
        due = parse_date(t)
        if not due:
            return await msg.answer("⚠️ Дата як 20.09.2026 або «Пропустити»")
    data = await state.get_data()
    tid = S.create_task(db, user["telegram_id"], data["task_text"], data["task_who"], due)
    await state.clear()
    await msg.answer(f"✅ Завдання №{tid} створено.", reply_markup=menu_for(user))
    targets = [data["task_who"]] if data["task_who"] else [u["telegram_id"] for u in S.list_users(db) if u["active"] and u["telegram_id"] != user["telegram_id"]]
    for uid in targets:
        try:
            await msg.bot.send_message(uid, f"📋 Нове завдання №{tid} від {user['name'] or 'керівника'}:\n<b>{data['task_text']}</b>"
                                       + (f"\nТермін: {ua_date(due)}" if due else ""),
                                       reply_markup=inline([[("✅ Виконано", f"task:done:{tid}")]]))
        except Exception:
            pass


# ======================= зміни каси =======================

class Shift(StatesGroup):
    cash_start = State()
    cash_end = State()
    schedule = State()


@router.message(F.text == M_SHIFT_OPEN)
async def shift_open(msg: Message, state: FSMContext, db, user):
    if S.current_shift(db):
        return await msg.answer("Каса вже відкрита.", reply_markup=menu_for(user))
    await state.set_state(Shift.cash_start)
    await msg.answer("▶️ Готівка в касі на старт, € (або пропустіть):", reply_markup=nav_kb(SKIP, back=False))


@router.message(StateFilter(Shift.cash_start), F.text)
async def shift_open_cash(msg: Message, state: FSMContext, db, user):
    cash = None
    if msg.text != SKIP:
        try:
            cash = parse_money(msg.text)
        except ParseError as e:
            return await msg.answer(f"⚠️ {e}")
    sid = S.open_shift(db, user["telegram_id"], cash)
    await state.clear()
    if not sid:
        return await msg.answer("Каса вже відкрита.", reply_markup=menu_for(user))
    t = dt.datetime.now(TZ).strftime("%H:%M")
    await msg.answer(f"✅ Касу відкрито о {t}. Гарного ярмарку!", reply_markup=menu_for(user))
    await _notify(msg.bot, db, f"▶️ Каса відкрита о {t} — {user['name'] or user['telegram_id']}"
                  + (f", готівка на старт {fmt_money(cash)}" if cash is not None else ""), exclude=user["telegram_id"])


@router.message(F.text == M_SHIFT_CLOSE)
async def shift_close(msg: Message, state: FSMContext, db, user):
    if not S.current_shift(db):
        return await msg.answer("Каса не відкрита.", reply_markup=menu_for(user))
    await state.set_state(Shift.cash_end)
    await msg.answer("⏹ Готівка в касі на кінець дня, € (або пропустіть):", reply_markup=nav_kb(SKIP, back=False))


@router.message(StateFilter(Shift.cash_end), F.text)
async def shift_close_cash(msg: Message, state: FSMContext, db, user):
    cash = None
    if msg.text != SKIP:
        try:
            cash = parse_money(msg.text)
        except ParseError as e:
            return await msg.answer(f"⚠️ {e}")
    s = S.close_shift(db, user["telegram_id"], cash)
    await state.clear()
    if not s:
        return await msg.answer("Каса не відкрита.", reply_markup=menu_for(user))
    t = dt.datetime.now(TZ).strftime("%H:%M")
    rep = S.report_period(db, today_local(), today_local())
    summary = (f"⏹ Каса закрита о {t} — {user['name'] or user['telegram_id']} (відкрита о {local_dt_str(s['opened_at'])[-5:]})\n"
               f"Продажів у боті: {rep['sales_count']} · {fmt_grams(rep['sold_grams'])} · виручка <b>{fmt_money(rep['revenue'])}</b>"
               + (" (" + ", ".join(f"{S.PAYMENTS[k].lower()} {fmt_money(v)}" for k, v in rep["by_payment"].items()) + ")" if rep["by_payment"] else ""))
    if cash is not None:
        summary += f"\nГотівка в касі: {fmt_money(cash)}"
        if s["cash_start"] is not None and rep["by_payment"].get("cash") is not None:
            expected = Decimal(s["cash_start"]) + rep["by_payment"]["cash"]
            summary += f" · очікувано {fmt_money(expected)} · різниця {fmt_money(cash - expected)}"
    await msg.answer("✅ " + summary, reply_markup=menu_for(user))
    await _notify(msg.bot, db, summary + "\n\n" + day_summary_text(db, today_local()), exclude=user["telegram_id"])


async def _notify(bot: Bot, db, text: str, exclude: int | None = None) -> None:
    for uid in S.notify_targets(db, "manager", actor_id=exclude):
        if uid == exclude:
            continue
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


# --- графік ярмарків ---

DAYS = {"пн": 0, "вт": 1, "ср": 2, "чт": 3, "пт": 4, "сб": 5, "нд": 6}
DAYS_UA = {v: k for k, v in DAYS.items()}


def parse_schedule(text: str) -> dict | None:
    """'пт, сб 08:00-14:00' -> {'days':[4,5], 'open':'08:00', 'close':'14:00'}"""
    t = text.lower().replace("—", "-").replace("–", "-")
    m = re.search(r"(\d{1,2}[:.]\d{2})\s*-\s*(\d{1,2}[:.]\d{2})", t)
    if not m:
        return None
    days = [DAYS[d] for d in re.findall(r"пн|вт|ср|чт|пт|сб|нд", t)]
    if not days:
        return None
    f = lambda s: s.replace(".", ":").zfill(5)
    return {"days": sorted(set(days)), "open": f(m.group(1)), "close": f(m.group(2))}


def schedule_text(db) -> str:
    raw = S.setting_get(db, "market_schedule")
    if not raw:
        return "не задано"
    sc = parse_schedule(raw)
    return f"{', '.join(DAYS_UA[d] for d in sc['days'])} {sc['open']}–{sc['close']}" if sc else raw


@router.callback_query(F.data == "set:schedule")
async def schedule_start(cb: CallbackQuery, state: FSMContext, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.set_state(Shift.schedule)
    await cb.message.answer(f"🕘 Графік ярмарків зараз: <b>{schedule_text(db)}</b>\nВведіть новий, наприклад: <code>пт, сб 08:00-14:00</code>\n"
                            "Бот нагадає, якщо касу не відкрито через 30 хв після початку або не закрито через 30 хв після кінця.",
                            reply_markup=nav_kb(back=False))
    await cb.answer()


@router.message(StateFilter(Shift.schedule), F.text)
async def schedule_set(msg: Message, state: FSMContext, db, user):
    if not parse_schedule(msg.text):
        return await msg.answer("⚠️ Формат: <code>пт, сб 08:00-14:00</code>")
    S.setting_set(db, "market_schedule", msg.text.strip())
    await state.clear()
    await msg.answer(f"✅ Графік: {schedule_text(db)}", reply_markup=menu_for(user))


def day_summary_text(db, day: str) -> str:
    """Вечірній підсумок для менеджера: виручка, топ-3, що закінчується, що спливає."""
    rep = S.report_period(db, day, day)
    out = [f"🌙 <b>Підсумок дня {ua_date(day)}</b>",
           f"Виручка <b>{fmt_money(rep['revenue'])}</b> · {rep['sales_count']} чеків · {fmt_grams(rep['sold_grams'])} · вал. прибуток {fmt_money(rep['gross_profit'])}"]
    if rep["by_payment"]:
        out.append("Оплата: " + ", ".join(f"{S.PAYMENTS[k].lower()} {fmt_money(v)}" for k, v in rep["by_payment"].items()))
    if rep["by_product"]:
        out.append("Топ-3: " + "; ".join(f"{e['name']} {fmt_money(e['amount'])}" for e in rep["by_product"][:3]))
    low = S.low_stock(db)
    if low:
        out.append("📦 Закінчується: " + ", ".join(r["product"]["name"] for r in low[:6]))
    exp = S.batches_expiring(db, 2)
    if exp:
        out.append("⏰ Спливає за 2 дні: " + ", ".join(f"{r['product_name']} ({fmt_grams(r['grams_left'])})" for r in exp[:6]))
    return "\n".join(out)


def week_start_text(db) -> str:
    st = S.stock_summary(db)
    out = [f"📅 <b>Початок тижня</b>: на складі {fmt_grams(sum(r['grams'] for r in st))} на {fmt_money(sum((r['cost_value'] for r in st), Decimal(0)))}"]
    stale = S.stale_products(db, 14)
    if stale:
        out.append("🧊 Не продавались 14+ днів: " + ", ".join(f"{r['name']} ({fmt_grams(r['grams'])})" for r in sorted(stale, key=lambda r: -r["grams"])[:8]))
    low = S.low_stock(db)
    if low:
        out.append("📦 Дозамовити: " + ", ".join(r["product"]["name"] for r in low[:8]))
    exp = S.batches_expiring(db, 7)
    if exp:
        out.append("⏰ Спливає цього тижня: " + ", ".join(f"{r['product_name']} до {ua_date(r['expiry_date'])[:5]}" for r in exp[:8]))
    return "\n".join(out)


async def shift_watchdog(bot: Bot) -> None:
    """Викликається щохвилини: нагадування про невідкриту/незакриту касу за графіком, прострочені завдання о 9:00."""
    db = get_db()
    now = dt.datetime.now(TZ)
    key_day = now.date().isoformat()
    sc = parse_schedule(S.setting_get(db, "market_schedule"))
    if sc and now.weekday() in sc["days"]:
        hm = now.strftime("%H:%M")
        open_alert = (dt.datetime.strptime(sc["open"], "%H:%M") + dt.timedelta(minutes=30)).strftime("%H:%M")
        close_alert = (dt.datetime.strptime(sc["close"], "%H:%M") + dt.timedelta(minutes=30)).strftime("%H:%M")
        if hm == open_alert and not S.shifts_on(db, key_day) and S.setting_get(db, "alert_open") != key_day:
            S.setting_set(db, "alert_open", key_day)
            await _notify(bot, db, f"⚠️ Ярмарковий день, {sc['open']} + 30 хв — каса ще не відкрита в боті.")
        if hm == close_alert and S.current_shift(db) and S.setting_get(db, "alert_close") != key_day:
            S.setting_set(db, "alert_close", key_day)
            await _notify(bot, db, f"⚠️ {sc['close']} + 30 хв — каса ще не закрита в боті.")
    if now.weekday() == 0 and now.strftime("%H:%M") == "08:00" and S.setting_get(db, "alert_week") != key_day:
        S.setting_set(db, "alert_week", key_day)
        await _notify(bot, db, week_start_text(db))
    if now.strftime("%H:%M") == "09:00" and S.setting_get(db, "alert_tasks") != key_day:
        S.setting_set(db, "alert_tasks", key_day)
        for t in S.overdue_tasks(db, key_day):
            targets = [t["assignee_id"]] if t["assignee_id"] else [u["telegram_id"] for u in S.list_users(db) if u["active"]]
            for uid in set(targets + [t["created_by"]]):
                try:
                    await bot.send_message(uid, f"⏰ Завдання №{t['id']} «{t['text']}» — термін {ua_date(t['due_date'])}",
                                           reply_markup=inline([[("✅ Виконано", f"task:done:{t['id']}")]]))
                except Exception:
                    pass


# ======================= документи =======================

def save_document(db, user_id: int, kind: str, ref_id: int | None, file_name: str, data: bytes) -> int:
    folder = DOCS_DIR / ("purchases" if kind == "purchase" else "expenses" if kind == "expense" else "other")
    folder.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^\w.\-]+", "_", file_name)[:80]
    path = folder / f"{today_local()}_{ref_id or 'x'}_{safe}"
    path.write_bytes(data)
    return S.add_document(db, user_id, kind, ref_id, file_name, str(path))


class Attach(StatesGroup):
    kind = State()
    target = State()
    file = State()


@router.callback_query(F.data == "set:docs")
async def docs_menu(cb: CallbackQuery, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await cb.message.answer("📁 <b>Документи</b>", reply_markup=inline([[("📦 Інвойси закупівель", "docs:purchase"), ("💸 Чеки витрат", "docs:expense")],
                                                                      [("🗃 Інше", "docs:other")],
                                                                      [("➕ Прикріпити документ", "docs:attach")]]))
    await cb.answer()


@router.callback_query(F.data == "docs:attach")
async def attach_start(cb: CallbackQuery, state: FSMContext, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    await state.clear()
    await state.set_state(Attach.kind)
    await cb.message.answer("До чого прикріпити?", reply_markup=nav_kb(back=False))
    await cb.message.answer("Оберіть:", reply_markup=inline([[("📦 До закупівлі", "att:kind:purchase"), ("💸 До витрати", "att:kind:expense")],
                                                             [("🗃 Просто в архів", "att:kind:other")]]))
    await cb.answer()


@router.callback_query(StateFilter(Attach.kind), F.data.startswith("att:kind:"))
async def attach_kind(cb: CallbackQuery, state: FSMContext, db):
    kind = cb.data.split(":")[2]
    await state.update_data(att_kind=kind)
    if kind == "other":
        await state.update_data(att_ref=None)
        await state.set_state(Attach.file)
        await cb.message.answer("📎 Надішліть файл або фото:")
        return await cb.answer()
    if kind == "purchase":
        rows = S.recent_purchases(db, 15)
        kb = [[(f"№{r['id']} {ua_date(r['doc_date'])} {r['supplier_name'] or ''} {(r['comment'] or '')[:20]}", f"att:ref:{r['id']}")] for r in rows]
    else:
        rows = S.recent_expenses(db, 15)
        kb = [[(f"№{r['id']} {ua_date(r['op_date'])} {r['category'][:22]} {fmt_money(r['amount'])}", f"att:ref:{r['id']}")] for r in rows]
    if not kb:
        await state.clear()
        await cb.message.answer("Записів ще немає.")
        return await cb.answer()
    await state.set_state(Attach.target)
    await cb.message.answer("До якого запису?", reply_markup=inline(kb))
    await cb.answer()


@router.callback_query(StateFilter(Attach.target), F.data.startswith("att:ref:"))
async def attach_target(cb: CallbackQuery, state: FSMContext):
    await state.update_data(att_ref=int(cb.data.split(":")[2]))
    await state.set_state(Attach.file)
    await cb.message.answer("📎 Надішліть файл або фото (можна кілька по черзі; «❌ Скасувати» — коли все):")
    await cb.answer()


@router.message(StateFilter(Attach.file), F.document | F.photo)
async def attach_file(msg: Message, state: FSMContext, db, user):
    import io
    data = await state.get_data()
    buf = io.BytesIO()
    if msg.document:
        await msg.bot.download(msg.document, destination=buf)
        name = msg.document.file_name or "document"
    else:
        await msg.bot.download(msg.photo[-1], destination=buf)
        name = f"photo_{dt.datetime.now(TZ):%H%M%S}.jpg"
    save_document(db, user["telegram_id"], data["att_kind"], data.get("att_ref"), name, buf.getvalue())
    ref = f" до запису №{data['att_ref']}" if data.get("att_ref") else " в архів"
    await msg.answer(f"📁 Збережено{ref}. Надішліть ще один файл або натисніть «❌ Скасувати».")


@router.callback_query(F.data.startswith("docs:"))
async def docs_list(cb: CallbackQuery, db, user):
    parts = cb.data.split(":")
    if parts[1] == "get":
        d = S.get_document(db, int(parts[2]))
        if not d or not Path(d["path"]).exists():
            return await cb.answer("Файл не знайдено", show_alert=True)
        await cb.message.answer_document(FSInputFile(d["path"], filename=d["file_name"]),
                                         caption=f"{'Закупівля' if d['kind'] == 'purchase' else 'Витрата'} №{d['ref_id'] or '—'} · {local_dt_str(d['uploaded_at'])}")
        return await cb.answer()
    rows = S.list_documents(db, parts[1])
    if not rows:
        await cb.message.answer("Документів ще немає. Прикріпити: 📁 Документи → ➕ Прикріпити документ.")
    else:
        await cb.message.answer("Останні документи (натисніть, щоб отримати файл):", reply_markup=inline(
            [[(f"№{d['ref_id'] or '—'} · {local_dt_str(d['uploaded_at'])[:10]} · {d['file_name'][:24]}", f"docs:get:{d['id']}")] for d in rows]))
    await cb.answer()


@router.message(F.text == "/backup_docs")
async def backup_docs(msg: Message, db, user):
    if user["role"] != "admin":
        return
    import shutil
    if not DOCS_DIR.exists():
        return await msg.answer("Документів ще немає.")
    out = Path("data") / f"docs_{today_local()}"
    shutil.make_archive(str(out), "zip", DOCS_DIR)
    await msg.answer_document(FSInputFile(str(out) + ".zip"), caption="📁 Архів документів (інвойси й чеки)")
