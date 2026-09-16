-- 002: витрати (операційні / оплата товару / податки / інвестиції)
CREATE TABLE IF NOT EXISTS expenses (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    op_date    TEXT NOT NULL,
    exp_type   TEXT NOT NULL CHECK (exp_type IN ('operating','goods','tax','investment')),
    category   TEXT NOT NULL,
    amount     TEXT NOT NULL,
    comment    TEXT,
    status     TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('done','cancelled')),
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses(op_date);
