from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import ErrorEvent
from .fsm_storage import SQLiteStorage
from aiogram.types import BotCommand, FSInputFile, MenuButtonWebApp, WebAppInfo

from . import services as S
from .config import TZ, settings
from .db import get_db
from .handlers import adjustments, catalog, common, octobox, purchases, reports, sales, tasks

log = logging.getLogger("crudo")


async def on_error(event: ErrorEvent) -> None:
    """Будь-яка помилка в обробнику — у лог і коротко користувачеві, замість тиші."""
    log.exception("handler error: %s", event.exception)
    upd = event.update
    msg = upd.message or (upd.callback_query.message if upd.callback_query else None)
    if upd.callback_query:
        try:
            await upd.callback_query.answer("Сталася помилка", show_alert=False)
        except Exception:
            pass
    if msg:
        try:
            await msg.answer(f"⚠️ Помилка: {type(event.exception).__name__}: {str(event.exception)[:300]}\n"
                             "Спробуйте ще раз або натисніть /start. Якщо повторюється — напишіть адміністратору.")
        except Exception:
            pass


def build_dispatcher(persistent: bool = True) -> Dispatcher:
    dp = Dispatcher(storage=SQLiteStorage() if persistent else MemoryStorage())
    dp.errors.register(on_error)
    dp.update.outer_middleware(common.DedupMiddleware())
    dp.message.outer_middleware(common.AuthMiddleware())
    dp.callback_query.outer_middleware(common.AuthMiddleware())
    # порядок важливий: спочатку сценарії зі станами, common (скасування) — першим
    dp.include_routers(common.router, sales.router, purchases.router, catalog.router, adjustments.router, reports.router, tasks.router, octobox.router)
    return dp


async def daily_backup(bot: Bot) -> None:
    """Щодня о BACKUP_HOUR (Відень) робить копію бази і надсилає її у BACKUP_CHAT_ID (або першому адміну)."""
    db = get_db()
    chat_id = settings.backup_chat_id or settings.admin_ids[0]
    while True:
        now = dt.datetime.now(TZ)
        target = now.replace(hour=settings.backup_hour, minute=0, second=0, microsecond=0)
        if target <= now:
            target += dt.timedelta(days=1)
        await asyncio.sleep((target - now).total_seconds())
        try:
            path = db.make_backup()
            S.purge_old_keys(db)
            await bot.send_document(chat_id, FSInputFile(path), caption=f"💾 Щоденна резервна копія {dt.datetime.now(TZ):%d.%m.%Y}")
        except Exception:
            log.exception("backup failed")


async def minute_loop(bot: Bot) -> None:
    """Щохвилини: нагадування про касу за графіком і прострочені завдання."""
    while True:
        try:
            await tasks.shift_watchdog(bot)
        except Exception:
            log.exception("watchdog failed")
        await asyncio.sleep(60 - dt.datetime.now().second)


async def web_server() -> None:
    """HTTP-сервер: /health для хостингу + Mini App (сторінка і API), якщо задано PORT."""
    if not settings.health_port:
        return
    from aiohttp import web
    from .webapp import build_app

    runner = web.AppRunner(build_app())
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.health_port).start()
    log.info("web server on :%s (mini app %s)", settings.health_port, settings.webapp_url or "вимкнено — задайте WEBAPP_URL")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.validate()
    db = get_db()
    S.ensure_admins(db, settings.admin_ids)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    from . import webapp as _webapp
    _webapp.request_bot["bot"] = bot
    await bot.set_my_commands([BotCommand(command="menu", description="Головне меню"),
                               BotCommand(command="id", description="Мій Telegram ID"),
                               BotCommand(command="help", description="Довідка"),
                               BotCommand(command="backup", description="Резервна копія (адмін)")])
    await web_server()
    if settings.webapp_url:
        try:
            await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Каса", web_app=WebAppInfo(url=settings.webapp_url + "/app")))
        except Exception as e:  # некоректний WEBAPP_URL не має зупиняти бота
            log.warning("не вдалося встановити кнопку меню Mini App (%s): %s", settings.webapp_url, e)
    asyncio.create_task(daily_backup(bot))
    asyncio.create_task(minute_loop(bot))
    asyncio.create_task(octobox.sync_loop(bot))
    log.info("bot started, db=%s", settings.db_path)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
