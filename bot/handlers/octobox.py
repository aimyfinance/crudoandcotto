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


async def run_sync(bot: Bot | None, user_id: int, since_days: int = 2, notify: bool = True) -> dict | None:
    """Забирає чеки за останні N днів і проводить їх (ідемпотентно). Автоматично відкриває/закриває зміну."""
    from ..tools import import_octobox
    cl = _client()
    if not cl:
        return None
    cfg = config()
    db = get_db()
    last = S.setting_get(db, "octobox_last_sync")
    since = dt.datetime.now(TZ) - dt.timedelta(days=since_days)
    if last:
        since = min(since, dt.datetime.fromisoformat(last).astimezone(TZ) - dt.timedelta(hours=6))
    async with cl as c:
        await c.authenticate()
        receipts = await fetch_receipts(c, since, cfg["weight_field"], cfg["pos_config"], TZ)
    st = import_octobox(receipts, user_id, since=None) if receipts else {"created": 0, "skipped_dup": 0, "completed": 0, "unmatched": {}, "shortfalls": [], "refunds": 0}
    S.setting_set(db, "octobox_last_sync", dt.datetime.now(TZ).isoformat())
    # авто-зміна: перший чек сьогодні → відкрити; після закінчення графіка і 90 хв без чеків → закрити
    today = today_local()
    todays = [r for r in receipts if r["dt"].date().isoformat() == today]
    if todays and not S.current_shift(db) and not S.shifts_on(db, today):
        S.open_shift(db, user_id)
        S.setting_set(db, "shift_auto", today)
        if bot:
            await _notify(bot, db, f"▶️ Каса відкрита (перший чек Octobox о {min(r['dt'] for r in todays):%H:%M})")
    sh = S.current_shift(db)
    if sh and todays and S.setting_get(db, "shift_auto") == today:
        last_dt = max(r["dt"] for r in todays)
        if dt.datetime.now(TZ).replace(tzinfo=None) - last_dt > dt.timedelta(minutes=90):
            S.close_shift(db, user_id, note="авто за Octobox")
            from .tasks import day_summary_text
            if bot:
                await _notify(bot, db, f"⏹ Каса закрита (останній чек Octobox о {last_dt:%H:%M}).\n\n" + day_summary_text(db, today))
    if bot and notify and (st["created"] or st.get("completed") or st["unmatched"]):
        txt = f"🔄 Octobox: нових чеків {st['created']}"
        if st.get("completed"):
            txt += f", доповнено {st['completed']}"
        if st["unmatched"]:
            txt += "\n⚠️ Без прив'язки (пропущено): " + ", ".join(st["unmatched"])[:300] + "\nНалаштування → 🔗 Прив'язки або імпорт чеків файлом, щоб прив'язати."
        if st["shortfalls"]:
            txt += "\n⚠️ Продано без залишку в боті — внесіть закупівлі."
        await _notify(bot, db, txt, admins_only=True)
    return st


async def _notify(bot: Bot, db, text: str, admins_only: bool = False) -> None:
    for uid in S.notify_targets(db, "admin" if admins_only else "manager"):
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass


@router.message(Command("octobox_sync"))
async def octobox_sync(msg: Message, user):
    if user["role"] != "admin":
        return
    if not _client():
        return await msg.answer("Octobox не налаштовано (OCTOBOX_URL/LOGIN/PASSWORD).")
    await msg.answer("🔄 Синхронізую чеки за останні 2 дні…")
    try:
        st = await run_sync(msg.bot, user["telegram_id"], notify=False)
    except Exception as e:
        return await msg.answer(f"❌ {type(e).__name__}: {e}")
    await msg.answer(f"✅ Нових чеків: {st['created']} · вже були: {st['skipped_dup']} · доповнено: {st.get('completed', 0)}"
                     + (f"\n⚠️ Без прив'язки: {', '.join(st['unmatched'])}" if st["unmatched"] else ""))


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
