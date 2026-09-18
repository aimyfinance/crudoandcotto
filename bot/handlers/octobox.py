"""Octobox: /octobox_test (діагностика), /octobox_sync (ручна синхронізація), фонова синхронізація."""
from __future__ import annotations

import datetime as dt
import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from .. import services as S
from ..config import TZ
from ..db import get_db, today_local
from ..octobox_api import OdooClient, OdooError, config, diagnose, diagnose_text, fetch_receipts
from ..money import fmt_money

log = logging.getLogger("crudo.octobox")
router = Router(name="octobox")


def _client() -> OdooClient | None:
    c = config()
    if not (c["url"] and c["login"] and c["password"]):
        return None
    return OdooClient(c["url"], c["login"], c["password"], c["db"])


@router.message(Command("octobox_test"))
async def octobox_test(msg: Message, user):
    if user["role"] != "admin":
        return
    cl = _client()
    if not cl:
        return await msg.answer("Не задано OCTOBOX_URL / OCTOBOX_LOGIN / OCTOBOX_PASSWORD у змінних середовища.")
    await msg.answer("🔌 Підключаюсь до бек-офісу Octobox (лише читання)…")
    try:
        async with cl as c:
            out = await diagnose(c)
    except OdooError as e:
        return await msg.answer(f"❌ Odoo: {e}")
    except Exception as e:
        return await msg.answer(f"❌ Не вдалося підключитись: {type(e).__name__}: {e}")
    await msg.answer(diagnose_text(out)[:4000])
    if out.get("pos.order") and out.get("pos.order.line"):
        await msg.answer("Виглядає робочим. Наступний крок: задайте OCTOBOX_SYNC_MINUTES=10 (і OCTOBOX_WEIGHT_FIELD, якщо кандидатів кілька) — "
                         "бот сам забиратиме чеки. Або /octobox_sync — разова синхронізація зараз.")


async def run_sync(bot: Bot | None, user_id: int, since_days: int = 2, notify: bool = True,
                   since_date: str | None = None, force: bool = False) -> dict | None:
    """Забирає чеки за останні N днів (або з since_date) і проводить їх (ідемпотентно). Автоматично відкриває/закриває зміну."""
    from ..tools import import_octobox
    cl = _client()
    if not cl:
        return None
    db = get_db()
    if S.setting_get(db, "sync_paused") == "1" and not force:
        return {"paused": True, "created": 0, "skipped_dup": 0, "completed": 0, "unmatched": {}, "shortfalls": [], "refunds": 0, "skipped_old": 0}
    cfg = config()
    last = S.setting_get(db, "octobox_last_sync")
    since = dt.datetime.now(TZ) - dt.timedelta(days=since_days)
    if since_date:
        since = dt.datetime.fromisoformat(since_date).replace(tzinfo=TZ)
    elif last:
        since = min(since, dt.datetime.fromisoformat(last).astimezone(TZ) - dt.timedelta(hours=6))
    async with cl as c:
        await c.authenticate()
        receipts = await fetch_receipts(c, since, cfg["weight_field"], cfg["pos_config"], TZ)
    st = import_octobox(receipts, user_id, since=None) if receipts else {"created": 0, "skipped_dup": 0, "completed": 0, "unmatched": {}, "shortfalls": [], "refunds": 0}
    S.setting_set(db, "octobox_last_sync", dt.datetime.now(TZ).isoformat())
    # авто-зміна: перший чек сьогодні → відкрити (і знову відкрити, якщо чеки йдуть після закриття).
    # Закриття — лише за графіком (через 30 хв після кінця) або кнопкою; див. tasks.shift_watchdog.
    today = today_local()
    todays = [r for r in receipts if r["dt"].date().isoformat() == today]
    if todays and not S.current_shift(db):
        last_close = db.one("SELECT closed_at FROM shifts WHERE shift_date=? AND closed_at IS NOT NULL ORDER BY id DESC LIMIT 1", (today,))
        newest = max(r["dt"] for r in todays)
        reopen = False
        if last_close:
            closed_local = dt.datetime.fromisoformat(last_close["closed_at"]).astimezone(TZ).replace(tzinfo=None)
            reopen = newest > closed_local
        if not last_close or reopen:
            S.open_shift(db, user_id)
            S.setting_set(db, "shift_auto", today)
            if bot:
                await _notify(bot, db, ("🔁 Каса знову відкрита — надійшов чек Octobox о " if reopen else "▶️ Каса відкрита (перший чек Octobox о ")
                              + f"{(newest if reopen else min(r['dt'] for r in todays)):%H:%M})")
    # неприв'язані назви / нестачі — збираємо, повідомимо у звіті дня, а не протягом дня
    if st["unmatched"]:
        S.setting_set(db, "day_unmatched", ", ".join(sorted(set((S.setting_get(db, "day_unmatched") + "," + ",".join(st["unmatched"])).strip(",").split(",")))))
    if st["shortfalls"]:
        S.setting_set(db, "day_shortfalls", "1")
    return st


async def _notify(bot: Bot, db, text: str, admins_only: bool = False) -> None:
    for uid in S.notify_targets(db, "admin" if admins_only else "manager"):
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


@router.message(Command("octobox_sync"))
async def octobox_sync(msg: Message, user):
    """/octobox_sync — за 2 дні; /octobox_sync 01.09.2026 — з дати. Знімає паузу синхронізації."""
    if user["role"] != "admin":
        return
    if not _client():
        return await msg.answer("Octobox не налаштовано (OCTOBOX_URL/LOGIN/PASSWORD).")
    from .common import parse_date
    parts = (msg.text or "").split(maxsplit=1)
    since_date = parse_date(parts[1]) if len(parts) > 1 else None
    if len(parts) > 1 and not since_date:
        return await msg.answer("⚠️ Дата як 01.09.2026")
    db = get_db()
    S.setting_set(db, "sync_paused", "0")
    await msg.answer(f"🔄 Синхронізую чеки {'з ' + parts[1] if since_date else 'за останні 2 дні'}… (пауза синхронізації знята)")
    try:
        st = await run_sync(msg.bot, user["telegram_id"], notify=False, since_date=since_date, force=True)
    except Exception as e:
        return await msg.answer(f"❌ {type(e).__name__}: {e}")
    await msg.answer(f"✅ Нових чеків: {st['created']} · вже були: {st['skipped_dup']} · доповнено: {st.get('completed', 0)}"
                     + (f"\n⚠️ Без прив'язки: {', '.join(st['unmatched'])}" if st["unmatched"] else "")
                     + ("\n⚠️ Продано без залишку в боті — внесіть закупівлі й повторіть /octobox_sync" if st["shortfalls"] else ""))


@router.message(Command("octobox_pause"))
async def octobox_pause(msg: Message, user):
    if user["role"] != "admin":
        return
    S.setting_set(get_db(), "sync_paused", "1")
    await msg.answer("⏸ Синхронізацію з касою поставлено на паузу. Відновити: /octobox_sync")


async def sync_loop(bot: Bot) -> None:
    from ..config import settings
    while True:
        cfg = config()
        minutes = cfg["minutes"]
        if minutes <= 0 or not _client():
            return
        try:
            await run_sync(bot, settings.admin_ids[0])
        except Exception:
            log.exception("octobox sync failed")
        import asyncio
        await asyncio.sleep(minutes * 60)


async def refresh_register_days(user_id: int, date_from: str, date_to: str) -> int:
    """Підтягує підсумки каси по днях з бек-офісу у cash_register_days (джерело octobox-api). -> кількість днів."""
    from ..octobox_api import fetch_day_totals
    cl = _client()
    if not cl:
        return 0
    cfg = config()
    async with cl as c:
        await c.authenticate()
        days = await fetch_day_totals(c, date_from, date_to, cfg["pos_config"], TZ)
    db = get_db()
    for day, v in days.items():
        S.save_cash_day(db, user_id, day, v["cash"], v["card"], "octobox-api")
    return len(days)
