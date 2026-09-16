from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Vienna"))


def _ids(s: str) -> list[int]:
    out = []
    for part in (s or "").replace(";", ",").split(","):
        part = part.strip()
        if part:
            out.append(int(part))
    return out


@dataclass
class Settings:
    bot_token: str = os.getenv("BOT_TOKEN", "")
    admin_ids: list[int] = field(default_factory=lambda: _ids(os.getenv("ADMIN_IDS", "")))
    db_path: Path = Path(os.getenv("DB_PATH", "data/crudo.db"))
    backup_dir: Path = Path(os.getenv("BACKUP_DIR", "data/backups"))
    backup_chat_id: int | None = int(os.getenv("BACKUP_CHAT_ID")) if os.getenv("BACKUP_CHAT_ID") else None
    backup_hour: int = int(os.getenv("BACKUP_HOUR", "23"))
    health_port: int | None = int(os.getenv("PORT")) if os.getenv("PORT") else None
    company_name: str = os.getenv("COMPANY_NAME", "Crudo & Cotto Delikatessen")
    webapp_url: str = os.getenv("WEBAPP_URL", "").strip().rstrip("/")   # публічна https-адреса сервера для Mini App

    def __post_init__(self):
        if self.webapp_url and not self.webapp_url.startswith("http"):
            self.webapp_url = "https://" + self.webapp_url

    def validate(self) -> None:
        if not self.bot_token:
            raise SystemExit("BOT_TOKEN не задано (див. .env.example)")
        if not self.admin_ids:
            raise SystemExit("ADMIN_IDS не задано — вкажіть хоча б один Telegram ID адміністратора")


settings = Settings()
