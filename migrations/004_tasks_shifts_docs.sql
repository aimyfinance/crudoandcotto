-- 004: завдання, зміни каси, документи, налаштування
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT NOT NULL,
    assignee_id INTEGER,                 -- NULL = усім
    created_by  INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    due_date    TEXT,
    status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','done','cancelled')),
    done_at     TEXT,
    done_by     INTEGER
);
CREATE TABLE IF NOT EXISTS shifts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_date TEXT NOT NULL,
    opened_at  TEXT NOT NULL,
    opened_by  INTEGER NOT NULL,
    cash_start TEXT,
    closed_at  TEXT,
    closed_by  INTEGER,
    cash_end   TEXT,
    note       TEXT
);
CREATE TABLE IF NOT EXISTS documents (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT NOT NULL CHECK (kind IN ('purchase','expense','other')),
    ref_id      INTEGER,
    file_name   TEXT NOT NULL,
    path        TEXT NOT NULL,
    uploaded_by INTEGER NOT NULL,
    uploaded_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS app_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
