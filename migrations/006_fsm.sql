-- 006: стан покрокових сценаріїв (FSM) у базі — переживає перезапуски бота
CREATE TABLE IF NOT EXISTS fsm_state (
    key        TEXT PRIMARY KEY,   -- bot:chat:user
    state      TEXT,
    data       TEXT NOT NULL DEFAULT '{}',
    updated_at TEXT NOT NULL
);
