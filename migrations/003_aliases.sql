-- 003: прив'язка назв товарів із касового апарата до товарів бота
CREATE TABLE IF NOT EXISTS product_aliases (
    alias      TEXT PRIMARY KEY,          -- нормалізована назва з каси
    alias_raw  TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
