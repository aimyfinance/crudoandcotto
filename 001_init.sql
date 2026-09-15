-- 001: початкова схема. Гроші зберігаються як TEXT (Decimal), вага — INTEGER (грами).
-- Дати/часи — ISO 8601 у UTC (колонки *_at), календарні дати — 'YYYY-MM-DD'.

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    name        TEXT NOT NULL DEFAULT '',
    role        TEXT NOT NULL CHECK (role IN ('admin','manager','seller')),
    active      INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT NOT NULL UNIQUE,
    category     TEXT NOT NULL CHECK (category IN ('cheese','meat','pasta')),
    sku          TEXT UNIQUE,
    sale_mode    TEXT NOT NULL CHECK (sale_mode IN ('weight','piece')),
    piece_grams  INTEGER,                 -- вага однієї упаковки для штучних товарів
    retail_price TEXT NOT NULL,           -- €/кг (weight) або €/шт (piece), брутто
    active       INTEGER NOT NULL DEFAULT 1,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS suppliers (
    id   INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS purchases (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_date      TEXT NOT NULL,
    supplier_id   INTEGER REFERENCES suppliers(id),
    status        TEXT NOT NULL CHECK (status IN ('draft','received','cancelled')),
    extra_costs   TEXT NOT NULL DEFAULT '0',   -- транспорт та інші закупівельні витрати, розподіляються на партії пропорційно вазі
    comment       TEXT,
    created_by    INTEGER NOT NULL,
    created_at    TEXT NOT NULL,
    received_at   TEXT,
    cancelled_at  TEXT,
    cancelled_by  INTEGER,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS purchase_lines (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id  INTEGER NOT NULL REFERENCES purchases(id),
    product_id   INTEGER NOT NULL REFERENCES products(id),
    batch_code   TEXT,
    grams        INTEGER NOT NULL CHECK (grams > 0),
    price_per_kg TEXT NOT NULL,
    amount       TEXT NOT NULL,           -- grams/1000 * price, округлено до центів
    expiry_date  TEXT,
    comment      TEXT
);

CREATE TABLE IF NOT EXISTS batches (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id          INTEGER NOT NULL REFERENCES products(id),
    purchase_id         INTEGER REFERENCES purchases(id),
    purchase_line_id    INTEGER REFERENCES purchase_lines(id),
    batch_code          TEXT,
    source              TEXT NOT NULL CHECK (source IN ('purchase','opening','adjustment')),
    grams_in            INTEGER NOT NULL,
    grams_left          INTEGER NOT NULL CHECK (grams_left >= 0),
    price_per_kg        TEXT NOT NULL,     -- ціна постачальника
    landed_price_per_kg TEXT NOT NULL,     -- ціна постачальника + частка додаткових витрат (собівартість)
    expiry_date         TEXT,
    received_at         TEXT NOT NULL,     -- для FIFO
    comment             TEXT
);
CREATE INDEX IF NOT EXISTS idx_batches_fifo ON batches(product_id, received_at, id);

CREATE TABLE IF NOT EXISTS sales (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    sold_at        TEXT NOT NULL,
    sale_date      TEXT NOT NULL,          -- дата за Europe/Vienna
    payment_method TEXT NOT NULL CHECK (payment_method IN ('cash','card','other')),
    status         TEXT NOT NULL CHECK (status IN ('done','cancelled')),
    total          TEXT NOT NULL,
    cost_total     TEXT NOT NULL,
    comment        TEXT,
    created_by     INTEGER NOT NULL,
    created_at     TEXT NOT NULL,
    cancelled_at   TEXT,
    cancelled_by   INTEGER,
    cancel_reason  TEXT,
    client_key     TEXT UNIQUE             -- ключ ідемпотентності (захист від подвійного підтвердження)
);
CREATE INDEX IF NOT EXISTS idx_sales_date ON sales(sale_date);

CREATE TABLE IF NOT EXISTS sale_lines (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_id    INTEGER NOT NULL REFERENCES sales(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    grams      INTEGER NOT NULL CHECK (grams > 0),
    pieces     INTEGER,                   -- для штучних товарів
    price      TEXT NOT NULL,             -- застосована ціна (€/кг або €/шт)
    amount     TEXT NOT NULL,
    cost       TEXT NOT NULL              -- собівартість за списаними партіями
);

CREATE TABLE IF NOT EXISTS sale_line_batches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    sale_line_id INTEGER NOT NULL REFERENCES sale_lines(id),
    batch_id     INTEGER NOT NULL REFERENCES batches(id),
    grams        INTEGER NOT NULL,
    cost         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS writeoffs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT NOT NULL,
    op_date       TEXT NOT NULL,
    kind          TEXT NOT NULL CHECK (kind IN ('writeoff','adjustment')),
    product_id    INTEGER NOT NULL REFERENCES products(id),
    batch_id      INTEGER REFERENCES batches(id),
    grams_delta   INTEGER NOT NULL,        -- від'ємне = списання, додатне = дооприбуткування при інвентаризації
    cost_delta    TEXT NOT NULL,
    reason        TEXT NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('done','cancelled')),
    created_by    INTEGER NOT NULL,
    cancelled_at  TEXT,
    cancelled_by  INTEGER,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS writeoff_batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    writeoff_id INTEGER NOT NULL REFERENCES writeoffs(id),
    batch_id    INTEGER NOT NULL REFERENCES batches(id),
    grams_delta INTEGER NOT NULL,
    cost_delta  TEXT NOT NULL
);

-- Журнал руху залишків (незмінна історія; скасування — окремим записом зі знаком мінус).
CREATE TABLE IF NOT EXISTS stock_movements (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    product_id  INTEGER NOT NULL,
    batch_id    INTEGER NOT NULL,
    grams_delta INTEGER NOT NULL,
    cost_delta  TEXT NOT NULL,
    kind        TEXT NOT NULL,   -- purchase, opening, sale, sale_cancel, writeoff, writeoff_cancel, adjustment, adjustment_cancel, purchase_cancel
    ref_type    TEXT NOT NULL,
    ref_id      INTEGER NOT NULL,
    user_id     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mov_ts ON stock_movements(ts);

CREATE TABLE IF NOT EXISTS audit_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    action  TEXT NOT NULL,
    details TEXT
);

-- Ідемпотентність: оброблені callback/update ключі.
CREATE TABLE IF NOT EXISTS processed_updates (
    key TEXT PRIMARY KEY,
    ts  TEXT NOT NULL
);

-- Імпорт звітів касового апарата (денні підсумки) для звірки.
CREATE TABLE IF NOT EXISTS cash_register_days (
    day         TEXT PRIMARY KEY,
    cash        TEXT NOT NULL,
    card        TEXT NOT NULL,
    total       TEXT NOT NULL,
    source_file TEXT,
    imported_at TEXT NOT NULL,
    imported_by INTEGER NOT NULL
);
