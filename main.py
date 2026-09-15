from __future__ import annotations

import asyncio
import datetime as dt
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, FSInputFile

from . import services as S
from .config import TZ, settings
from .db import get_db
from .handlers import adjustments, catalog, common, purchases, reports, sales

log = logging.getLogger("crudo")


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(common.DedupMiddleware())
    dp.message.outer_middleware(common.AuthMiddleware())
    dp.callback_query.outer_middleware(common.AuthMiddleware())
    # порядок важливий: спочатку сценарії зі станами, common (скасування) — першим
    dp.include_routers(common.router, sales.router, purchases.router, catalog.router, adjustments.router, reports.router)
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


async def health_server() -> None:
    """Для хостингів, що вимагають відкритий порт (Render/Koyeb): відповідає 200 OK на /."""
    if not settings.health_port:
        return
    from aiohttp import web

    app = web.Application()
    app.router.add_get("/", lambda r: web.Response(text="ok"))
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", settings.health_port).start()
    log.info("health endpoint on :%s", settings.health_port)


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings.validate()
    db = get_db()
    S.ensure_admins(db, settings.admin_ids)
    bot = Bot(settings.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = build_dispatcher()
    await bot.set_my_commands([BotCommand(command="menu", description="Головне меню"),
                               BotCommand(command="id", description="Мій Telegram ID"),
                               BotCommand(command="help", description="Довідка"),
                               BotCommand(command="backup", description="Резервна копія (адмін)")])
    await health_server()
    asyncio.create_task(daily_backup(bot))
    log.info("bot started, db=%s", settings.db_path)
    await bot.delete_webhook(drop_pending_updates=False)
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
