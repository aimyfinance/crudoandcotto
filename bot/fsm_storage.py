"""FSM-сховище aiogram поверх SQLite: стани та дані сценаріїв не губляться при перезапуску."""
from __future__ import annotations

import json
from typing import Any

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StateType, StorageKey

from .db import get_db, now_utc


def _k(key: StorageKey) -> str:
    return f"{key.bot_id}:{key.chat_id}:{key.user_id}"


class SQLiteStorage(BaseStorage):
    async def set_state(self, key: StorageKey, state: StateType = None) -> None:
        st = state.state if isinstance(state, State) else state
        db = get_db()
        with db.tx() as c:
            c.execute("INSERT INTO fsm_state(key, state, data, updated_at) VALUES (?,?,'{}',?) "
                      "ON CONFLICT(key) DO UPDATE SET state=excluded.state, updated_at=excluded.updated_at", (_k(key), st, now_utc()))
            if st is None:
                c.execute("UPDATE fsm_state SET data='{}' WHERE key=? AND state IS NULL AND data='{}'", (_k(key),))

    async def get_state(self, key: StorageKey) -> str | None:
        r = get_db().one("SELECT state FROM fsm_state WHERE key=?", (_k(key),))
        return r["state"] if r else None

    async def set_data(self, key: StorageKey, data: dict[str, Any]) -> None:
        db = get_db()
        with db.tx() as c:
            c.execute("INSERT INTO fsm_state(key, state, data, updated_at) VALUES (?,NULL,?,?) "
                      "ON CONFLICT(key) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                      (_k(key), json.dumps(data, ensure_ascii=False, default=str), now_utc()))

    async def get_data(self, key: StorageKey) -> dict[str, Any]:
        r = get_db().one("SELECT data FROM fsm_state WHERE key=?", (_k(key),))
        return json.loads(r["data"]) if r and r["data"] else {}

    async def close(self) -> None:
        pass
