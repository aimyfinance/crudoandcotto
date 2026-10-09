-- 007: назви з каси без прив'язки — бот сам запитує, до якого товару прив'язати
CREATE TABLE IF NOT EXISTS pos_unmapped (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL UNIQUE,      -- назва з каси (як є)
    count       INTEGER NOT NULL DEFAULT 0,
    amount      TEXT NOT NULL DEFAULT '0',
    first_seen  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open',   -- open | bound | ignored
    asked_at    TEXT
);
