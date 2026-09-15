"""SQLite: одне з'єднання, глобальний лок на запис, міграції, backup/restore.

Усі операції, що змінюють залишки, виконуються в транзакції BEGIN IMMEDIATE
під threading.RLock — тому одночасні продажі кількох користувачів
серіалізуються, а перевірка залишку і списання відбуваються атомарно.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import os
import sqlite3
import threading
from pathlib import Path

from .config import settings, TZ

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def today_local() -> str:
    return dt.datetime.now(TZ).date().isoformat()


def local_date_of(utc_iso: str) -> str:
    return dt.datetime.fromisoformat(utc_iso).astimezone(TZ).date().isoformat()


def local_dt_str(utc_iso: str) -> str:
    return dt.datetime.fromisoformat(utc_iso).astimezone(TZ).strftime("%d.%m.%Y %H:%M")


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.migrate()

    # ---------- міграції ----------
    def migrate(self) -> None:
        with self.lock:
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {r[0] for r in self.conn.execute("SELECT name FROM schema_migrations")}
            for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
                if f.name in applied:
                    continue
                sql = f.read_text(encoding="utf-8")
                # executescript сам керує транзакцією (COMMIT перед виконанням)
                self.conn.executescript("BEGIN;\n" + sql + "\nCOMMIT;")
                self.conn.execute(
                    "INSERT INTO schema_migrations(name, applied_at) VALUES (?,?)", (f.name, now_utc())
                )

    # ---------- транзакції ----------
    @contextlib.contextmanager
    def tx(self):
        """Атомарна транзакція на запис (BEGIN IMMEDIATE + процесний лок)."""
        with self.lock:
            self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield self.conn
                self.conn.execute("COMMIT")
            except Exception:
                self.conn.execute("ROLLBACK")
                raise

    def q(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, params).fetchall()

    def one(self, sql: str, params=()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, params).fetchone()

    # ---------- backup / restore ----------
    def backup_to(self, dest: Path | str) -> Path:
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with self.lock:
            out = sqlite3.connect(str(dest))
            try:
                self.conn.backup(out)
            finally:
                out.close()
        return dest

    def make_backup(self) -> Path:
        stamp = dt.datetime.now(TZ).strftime("%Y%m%d_%H%M%S")
        dest = settings.backup_dir / f"crudo_{stamp}.db"
        self.backup_to(dest)
        # тримаємо лише 30 останніх локальних копій
        files = sorted(settings.backup_dir.glob("crudo_*.db"))
        for old in files[:-30]:
            old.unlink(missing_ok=True)
        return dest

    def restore_from(self, src: Path | str) -> None:
        """Замінює поточну базу вмістом файлу src (після перевірки цілісності)."""
        src = Path(src)
        check = sqlite3.connect(str(src))
        try:
            res = check.execute("PRAGMA integrity_check").fetchone()[0]
            if res != "ok":
                raise ValueError(f"Файл бази пошкоджено: {res}")
            tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "sales" not in tables or "batches" not in tables:
                raise ValueError("Це не база Crudo-бота")
        finally:
            check.close()
        with self.lock:
            self.make_backup()  # страхувальна копія перед відновленням
            src_conn = sqlite3.connect(str(src))
            try:
                src_conn.backup(self.conn)
            finally:
                src_conn.close()
            self.migrate()


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database(settings.db_path)
    return _db


def set_db(db: Database) -> None:
    global _db
    _db = db
