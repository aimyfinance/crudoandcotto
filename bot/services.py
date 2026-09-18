"""Бізнес-логіка обліку. Усі функції синхронні і працюють через Database.tx().

Правила:
  * списання партій — FIFO за датою надходження (received_at, потім id);
  * собівартість = landed_price_per_kg партії (ціна постачальника +
    додаткові витрати закупівлі, розподілені пропорційно вазі);
  * не можна продати більше, ніж є в залишку (перевірка в тій же транзакції,
    що і списання);
  * скасування не видаляє документ — змінює статус і додає зворотні рухи.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal

from .db import Database, now_utc, today_local, local_date_of  # noqa: F401
from .money import ZERO, d, round_cents, line_amount, piece_amount, PRICE_PREC

CATEGORIES = {"cheese": "Сир", "meat": "М'ясні вироби", "pasta": "Паста / напівфабрикати"}
PAYMENTS = {"cash": "Готівка", "card": "Картка", "other": "Інше"}
ROLES = {"admin": "Адміністратор", "manager": "Менеджер", "seller": "Продавець"}
STATUS_UA = {"received": "проведено", "draft": "чернетка", "cancelled": "скасовано", "done": "проведено"}


class StockError(Exception):
    pass


class InsufficientStock(StockError):
    def __init__(self, product_name: str, need: int, have: int):
        super().__init__(f"Недостатньо «{product_name}»: потрібно {need} г, є {have} г")
        self.product_name, self.need, self.have = product_name, need, have


class DuplicateOperation(StockError):
    pass


# ======================= користувачі =======================

def ensure_admins(db: Database, admin_ids: list[int]) -> None:
    with db.tx() as c:
        for uid in admin_ids:
            c.execute(
                "INSERT INTO users(telegram_id, name, role, active, created_at) VALUES (?,?,?,1,?) "
                "ON CONFLICT(telegram_id) DO UPDATE SET role='admin', active=1",
                (uid, "", "admin", now_utc()),
            )


def get_user(db: Database, tg_id: int):
    return db.one("SELECT * FROM users WHERE telegram_id=? AND active=1", (tg_id,))


def list_users(db: Database):
    return db.q("SELECT * FROM users ORDER BY role, name")


def upsert_user(db: Database, tg_id: int, role: str, name: str = "") -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO users(telegram_id, name, role, active, created_at) VALUES (?,?,?,1,?) "
            "ON CONFLICT(telegram_id) DO UPDATE SET role=excluded.role, active=1, "
            "name=CASE WHEN excluded.name='' THEN users.name ELSE excluded.name END",
            (tg_id, name, role, now_utc()),
        )


def deactivate_user(db: Database, tg_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE users SET active=0 WHERE telegram_id=?", (tg_id,))


def touch_user_name(db: Database, tg_id: int, name: str) -> None:
    with db.tx() as c:
        c.execute("UPDATE users SET name=? WHERE telegram_id=? AND name=''", (name, tg_id))


def audit(c, user_id: int, action: str, details: dict | str | None = None) -> None:
    c.execute(
        "INSERT INTO audit_log(ts, user_id, action, details) VALUES (?,?,?,?)",
        (now_utc(), user_id, action, json.dumps(details, ensure_ascii=False) if isinstance(details, dict) else details),
    )


# ======================= ідемпотентність =======================

def claim_key(db: Database, key: str) -> bool:
    """True — ключ новий (обробляємо), False — вже оброблявся (ігноруємо)."""
    with db.tx() as c:
        try:
            c.execute("INSERT INTO processed_updates(key, ts) VALUES (?,?)", (key, now_utc()))
            return True
        except Exception:
            return False


def purge_old_keys(db: Database, keep: int = 20000) -> None:
    with db.tx() as c:
        c.execute(
            "DELETE FROM processed_updates WHERE key NOT IN (SELECT key FROM processed_updates ORDER BY ts DESC LIMIT ?)",
            (keep,),
        )


# ======================= товари =======================

def create_product(db: Database, name: str, category: str, sale_mode: str, retail_price: Decimal,
                   piece_grams: int | None = None, sku: str | None = None) -> int:
    if sale_mode == "piece" and not piece_grams:
        raise ValueError("Для штучного товару вкажіть вагу упаковки в грамах")
    ts = now_utc()
    with db.tx() as c:
        cur = c.execute(
            "INSERT INTO products(name, category, sku, sale_mode, piece_grams, retail_price, active, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,1,?,?)",
            (name.strip(), category, (sku or "").strip() or None, sale_mode, piece_grams, str(retail_price), ts, ts),
        )
        return cur.lastrowid


def update_product(db: Database, product_id: int, **fields) -> None:
    allowed = {"name", "category", "sku", "sale_mode", "piece_grams", "retail_price", "active", "register_price"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(k)
        sets.append(f"{k}=?")
        vals.append(str(v) if isinstance(v, Decimal) else v)
    sets.append("updated_at=?")
    vals.append(now_utc())
    vals.append(product_id)
    with db.tx() as c:
        c.execute(f"UPDATE products SET {', '.join(sets)} WHERE id=?", vals)


def list_products(db: Database, active_only: bool = True, category: str | None = None):
    sql = "SELECT * FROM products WHERE 1=1"
    p: list = []
    if active_only:
        sql += " AND active=1"
    if category:
        sql += " AND category=?"
        p.append(category)
    sql += " ORDER BY category, name"
    return db.q(sql, p)


def get_product(db: Database, product_id: int):
    return db.one("SELECT * FROM products WHERE id=?", (product_id,))


def find_products(db: Database, text: str):
    return db.q("SELECT * FROM products WHERE active=1 AND lower(name) LIKE ? ORDER BY name LIMIT 20",
                (f"%{text.lower()}%",))


# ======================= постачальники =======================

def get_or_create_supplier(db: Database, name: str) -> int:
    name = name.strip()
    row = db.one("SELECT id FROM suppliers WHERE lower(name)=lower(?)", (name,))
    if row:
        return row["id"]
    with db.tx() as c:
        return c.execute("INSERT INTO suppliers(name) VALUES (?)", (name,)).lastrowid


def list_suppliers(db: Database):
    return db.q("SELECT * FROM suppliers ORDER BY name")


# ======================= закупівля =======================

@dataclass
class PurchaseLine:
    product_id: int
    grams: int
    price_per_kg: Decimal
    expiry_date: str | None = None
    batch_code: str | None = None
    comment: str | None = None

    @property
    def amount(self) -> Decimal:
        return line_amount(self.grams, self.price_per_kg)


def create_purchase(db: Database, user_id: int, doc_date: str, supplier_name: str,
                    lines: list[PurchaseLine], extra_costs: Decimal = ZERO, comment: str | None = None,
                    receive: bool = True) -> int:
    """Створює закупівлю; якщо receive=True — одразу підтверджує надходження і створює партії."""
    if not lines:
        raise ValueError("Закупівля без позицій")
    supplier_id = get_or_create_supplier(db, supplier_name) if supplier_name else None
    ts = now_utc()
    with db.tx() as c:
        pid = c.execute(
            "INSERT INTO purchases(doc_date, supplier_id, status, extra_costs, comment, created_by, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (doc_date, supplier_id, "draft", str(extra_costs), comment, user_id, ts),
        ).lastrowid
        for ln in lines:
            c.execute(
                "INSERT INTO purchase_lines(purchase_id, product_id, batch_code, grams, price_per_kg, amount, expiry_date, comment) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (pid, ln.product_id, ln.batch_code, ln.grams, str(ln.price_per_kg), str(ln.amount), ln.expiry_date, ln.comment),
            )
        audit(c, user_id, "purchase.create", {"purchase_id": pid})
        if receive:
            _receive_purchase(c, pid, user_id)
    return pid


def receive_purchase(db: Database, purchase_id: int, user_id: int) -> None:
    with db.tx() as c:
        _receive_purchase(c, purchase_id, user_id)


def _receive_purchase(c, purchase_id: int, user_id: int) -> None:
    p = c.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
    if not p:
        raise ValueError("Закупівлю не знайдено")
    if p["status"] != "draft":
        raise DuplicateOperation("Ця закупівля вже підтверджена або скасована")
    lines = c.execute("SELECT * FROM purchase_lines WHERE purchase_id=? ORDER BY id", (purchase_id,)).fetchall()
    total_grams = sum(l["grams"] for l in lines)
    extra = d(p["extra_costs"])
    # розподіл додаткових витрат пропорційно вазі => однакова надбавка €/кг для всіх позицій
    extra_per_kg = (extra * 1000 / Decimal(total_grams)).quantize(PRICE_PREC) if total_grams and extra else ZERO
    ts = now_utc()
    received_at = f"{p['doc_date']}T00:00:00+00:00"  # FIFO за датою документа закупівлі
    for l in lines:
        landed = (d(l["price_per_kg"]) + extra_per_kg).quantize(PRICE_PREC)
        bid = c.execute(
            "INSERT INTO batches(product_id, purchase_id, purchase_line_id, batch_code, source, grams_in, grams_left, "
            "price_per_kg, landed_price_per_kg, expiry_date, received_at, comment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (l["product_id"], purchase_id, l["id"], l["batch_code"], "purchase", l["grams"], l["grams"],
             l["price_per_kg"], str(landed), l["expiry_date"], received_at, l["comment"]),
        ).lastrowid
        _movement(c, ts, l["product_id"], bid, l["grams"], round_cents(Decimal(l["grams"]) * landed / 1000),
                  "purchase", "purchase", purchase_id, user_id)
    c.execute("UPDATE purchases SET status='received', received_at=? WHERE id=?", (ts, purchase_id))
    audit(c, user_id, "purchase.receive", {"purchase_id": purchase_id})


def cancel_purchase(db: Database, purchase_id: int, user_id: int, reason: str) -> None:
    """Скасування можливе, лише якщо з жодної партії ще нічого не списано."""
    with db.tx() as c:
        p = c.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        if not p or p["status"] == "cancelled":
            raise DuplicateOperation("Закупівля вже скасована або не існує")
        if p["status"] == "received":
            used = c.execute(
                "SELECT COUNT(*) FROM batches WHERE purchase_id=? AND grams_left <> grams_in", (purchase_id,)
            ).fetchone()[0]
            if used:
                g = purchase_usage(db, purchase_id)
                raise StockError(f"З партій цієї закупівлі вже списано {g} г (продажі/списання) — спочатку скасуйте їх")
            ts = now_utc()
            for b in c.execute("SELECT * FROM batches WHERE purchase_id=?", (purchase_id,)):
                _movement(c, ts, b["product_id"], b["id"], -b["grams_in"],
                          -round_cents(Decimal(b["grams_in"]) * d(b["landed_price_per_kg"]) / 1000),
                          "purchase_cancel", "purchase", purchase_id, user_id)
                c.execute("UPDATE batches SET grams_left=0 WHERE id=?", (b["id"],))
        c.execute("UPDATE purchases SET status='cancelled', cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=?",
                  (now_utc(), user_id, reason, purchase_id))
        audit(c, user_id, "purchase.cancel", {"purchase_id": purchase_id, "reason": reason})


def add_opening_stock(db: Database, user_id: int, product_id: int, grams: int, price_per_kg: Decimal,
                      expiry_date: str | None = None, date: str | None = None, comment: str | None = None) -> int:
    date = date or today_local()
    ts = now_utc()
    with db.tx() as c:
        bid = c.execute(
            "INSERT INTO batches(product_id, purchase_id, purchase_line_id, batch_code, source, grams_in, grams_left, "
            "price_per_kg, landed_price_per_kg, expiry_date, received_at, comment) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (product_id, None, None, "початковий", "opening", grams, grams, str(price_per_kg), str(price_per_kg),
             expiry_date, f"{date}T00:00:00+00:00", comment),
        ).lastrowid
        _movement(c, ts, product_id, bid, grams, round_cents(Decimal(grams) * price_per_kg / 1000),
                  "opening", "batch", bid, user_id)
        audit(c, user_id, "stock.opening", {"batch_id": bid})
        return bid


def _movement(c, ts, product_id, batch_id, grams_delta, cost_delta: Decimal, kind, ref_type, ref_id, user_id):
    c.execute(
        "INSERT INTO stock_movements(ts, product_id, batch_id, grams_delta, cost_delta, kind, ref_type, ref_id, user_id) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (ts, product_id, batch_id, grams_delta, str(cost_delta), kind, ref_type, ref_id, user_id),
    )


# ======================= залишки =======================

def stock_of_product(c_or_db, product_id: int) -> int:
    ex = c_or_db.execute if hasattr(c_or_db, "execute") else c_or_db.conn.execute
    row = ex("SELECT COALESCE(SUM(grams_left),0) FROM batches WHERE product_id=? AND grams_left>0", (product_id,)).fetchone()
    return int(row[0])


def stock_summary(db: Database, include_zero: bool = False):
    """[{product, grams, cost_value, nearest_expiry}]"""
    rows = db.q(
        "SELECT p.id, p.name, p.category, p.sale_mode, p.piece_grams, p.retail_price, "
        "COALESCE(SUM(b.grams_left),0) AS grams, "
        "MIN(CASE WHEN b.grams_left>0 THEN b.expiry_date END) AS nearest_expiry "
        "FROM products p LEFT JOIN batches b ON b.product_id=p.id AND b.grams_left>0 "
        "WHERE p.active=1 GROUP BY p.id ORDER BY p.category, p.name"
    )
    out = []
    for r in rows:
        if not include_zero and r["grams"] == 0:
            continue
        val = ZERO
        for b in db.q("SELECT grams_left, landed_price_per_kg FROM batches WHERE product_id=? AND grams_left>0", (r["id"],)):
            val += round_cents(Decimal(b["grams_left"]) * d(b["landed_price_per_kg"]) / 1000)
        out.append({"product": r, "grams": int(r["grams"]), "cost_value": val, "nearest_expiry": r["nearest_expiry"]})
    return out


def batches_of_product(db: Database, product_id: int, only_open: bool = True):
    sql = "SELECT * FROM batches WHERE product_id=?"
    if only_open:
        sql += " AND grams_left>0"
    return db.q(sql + " ORDER BY received_at, id", (product_id,))


def batches_expiring(db: Database, days: int = 7):
    return db.q(
        "SELECT b.*, p.name AS product_name FROM batches b JOIN products p ON p.id=b.product_id "
        "WHERE b.grams_left>0 AND b.expiry_date IS NOT NULL AND b.expiry_date <= date(?, ?) "
        "ORDER BY b.expiry_date",
        (today_local(), f"+{days} days"),
    )


# ======================= продаж =======================

@dataclass
class SaleLine:
    product_id: int
    grams: int
    price: Decimal          # застосована ціна (€/кг або €/шт)
    pieces: int | None = None

    def amount(self) -> Decimal:
        if self.pieces:
            return piece_amount(self.pieces, self.price)
        return line_amount(self.grams, self.price)


@dataclass
class SaleResult:
    sale_id: int
    total: Decimal
    cost_total: Decimal
    lines: list = field(default_factory=list)


def _consume_fifo(c, product_id: int, grams: int, product_name: str) -> list[tuple[int, int, Decimal]]:
    """Списує grams з партій за FIFO. Повертає [(batch_id, grams, cost)]. Кидає InsufficientStock."""
    have = stock_of_product(c, product_id)
    if have < grams:
        raise InsufficientStock(product_name, grams, have)
    left = grams
    taken = []
    for b in c.execute("SELECT * FROM batches WHERE product_id=? AND grams_left>0 ORDER BY received_at, id", (product_id,)):
        if left <= 0:
            break
        take = min(left, b["grams_left"])
        cost = round_cents(Decimal(take) * d(b["landed_price_per_kg"]) / 1000)
        c.execute("UPDATE batches SET grams_left=grams_left-? WHERE id=? AND grams_left>=?", (take, b["id"], take))
        taken.append((b["id"], take, cost))
        left -= take
    if left != 0:  # pragma: no cover — захист від гонки
        raise InsufficientStock(product_name, grams, have)
    return taken


def create_sale(db: Database, user_id: int, lines: list[SaleLine], payment_method: str,
                client_key: str | None = None, comment: str | None = None, sold_at: str | None = None) -> SaleResult:
    """sold_at — лише для імпорту історії (ISO-дата 'YYYY-MM-DD'); інакше — зараз."""
    if not lines:
        raise ValueError("Продаж без позицій")
    ts = (f"{sold_at}T12:00:00+00:00" if len(sold_at) == 10 else sold_at) if sold_at else now_utc()
    with db.tx() as c:
        if client_key:
            dup = c.execute("SELECT id, total, cost_total FROM sales WHERE client_key=?", (client_key,)).fetchone()
            if dup:
                raise DuplicateOperation(f"Продаж №{dup['id']} уже проведено")
        total = ZERO
        cost_total = ZERO
        sid = c.execute(
            "INSERT INTO sales(sold_at, sale_date, payment_method, status, total, cost_total, comment, created_by, created_at, client_key) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, local_date_of(ts), payment_method, "done", "0", "0", comment, user_id, ts, client_key),
        ).lastrowid
        out_lines = []
        for ln in lines:
            prod = c.execute("SELECT * FROM products WHERE id=?", (ln.product_id,)).fetchone()
            if not prod or not prod["active"]:
                raise ValueError("Товар не знайдено або архівний")
            taken = _consume_fifo(c, ln.product_id, ln.grams, prod["name"])
            cost = sum((t[2] for t in taken), ZERO)
            amt = ln.amount()
            lid = c.execute(
                "INSERT INTO sale_lines(sale_id, product_id, grams, pieces, price, amount, cost) VALUES (?,?,?,?,?,?,?)",
                (sid, ln.product_id, ln.grams, ln.pieces, str(ln.price), str(amt), str(cost)),
            ).lastrowid
            for bid, g, cst in taken:
                c.execute("INSERT INTO sale_line_batches(sale_line_id, batch_id, grams, cost) VALUES (?,?,?,?)",
                          (lid, bid, g, str(cst)))
                _movement(c, ts, ln.product_id, bid, -g, -cst, "sale", "sale", sid, user_id)
            total += amt
            cost_total += cost
            out_lines.append({"product": prod["name"], "grams": ln.grams, "pieces": ln.pieces, "price": ln.price,
                              "amount": amt, "cost": cost})
        c.execute("UPDATE sales SET total=?, cost_total=? WHERE id=?", (str(total), str(cost_total), sid))
        audit(c, user_id, "sale.create", {"sale_id": sid, "total": str(total)})
    return SaleResult(sid, total, cost_total, out_lines)


def add_sale_lines(db: Database, sale_id: int, user_id: int, lines: list[SaleLine]) -> Decimal:
    """Додає позиції до вже проведеного продажу (для доповнення імпорту). Повертає суму доданого."""
    ts = now_utc()
    added = ZERO
    with db.tx() as c:
        s = c.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not s or s["status"] != "done":
            raise ValueError("Продаж не знайдено або скасовано")
        total, cost_total = d(s["total"]), d(s["cost_total"])
        for ln in lines:
            prod = c.execute("SELECT * FROM products WHERE id=?", (ln.product_id,)).fetchone()
            taken = _consume_fifo(c, ln.product_id, ln.grams, prod["name"])
            cost = sum((t[2] for t in taken), ZERO)
            amt = ln.amount()
            lid = c.execute(
                "INSERT INTO sale_lines(sale_id, product_id, grams, pieces, price, amount, cost) VALUES (?,?,?,?,?,?,?)",
                (sale_id, ln.product_id, ln.grams, ln.pieces, str(ln.price), str(amt), str(cost)),
            ).lastrowid
            for bid, g, cst in taken:
                c.execute("INSERT INTO sale_line_batches(sale_line_id, batch_id, grams, cost) VALUES (?,?,?,?)", (lid, bid, g, str(cst)))
                _movement(c, s["sold_at"], ln.product_id, bid, -g, -cst, "sale", "sale", sale_id, user_id)
            total += amt
            cost_total += cost
            added += amt
        c.execute("UPDATE sales SET total=?, cost_total=? WHERE id=?", (str(total), str(cost_total), sale_id))
        audit(c, user_id, "sale.add_lines", {"sale_id": sale_id, "added": str(added)})
    return added


def cancel_sale(db: Database, sale_id: int, user_id: int, reason: str) -> None:
    ts = now_utc()
    with db.tx() as c:
        s = c.execute("SELECT * FROM sales WHERE id=?", (sale_id,)).fetchone()
        if not s:
            raise ValueError("Продаж не знайдено")
        if s["status"] == "cancelled":
            raise DuplicateOperation("Продаж уже скасовано")
        for slb in c.execute(
            "SELECT slb.*, sl.product_id FROM sale_line_batches slb JOIN sale_lines sl ON sl.id=slb.sale_line_id "
            "WHERE sl.sale_id=?", (sale_id,)
        ).fetchall():
            c.execute("UPDATE batches SET grams_left=grams_left+? WHERE id=?", (slb["grams"], slb["batch_id"]))
            _movement(c, ts, slb["product_id"], slb["batch_id"], slb["grams"], d(slb["cost"]),
                      "sale_cancel", "sale", sale_id, user_id)
        c.execute("UPDATE sales SET status='cancelled', cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=?",
                  (ts, user_id, reason, sale_id))
        audit(c, user_id, "sale.cancel", {"sale_id": sale_id, "reason": reason})


def get_sale(db: Database, sale_id: int):
    s = db.one("SELECT * FROM sales WHERE id=?", (sale_id,))
    if not s:
        return None, []
    lines = db.q("SELECT sl.*, p.name AS product_name, p.sale_mode FROM sale_lines sl JOIN products p ON p.id=sl.product_id "
                 "WHERE sl.sale_id=? ORDER BY sl.id", (sale_id,))
    return s, lines


def recent_sales(db: Database, limit: int = 15, date: str | None = None):
    if date:
        return db.q("SELECT * FROM sales WHERE sale_date=? ORDER BY id DESC LIMIT ?", (date, limit))
    return db.q("SELECT * FROM sales ORDER BY id DESC LIMIT ?", (limit,))


# ======================= списання / коригування =======================

def write_off(db: Database, user_id: int, product_id: int, grams: int, reason: str,
              batch_id: int | None = None, op_date: str | None = None) -> int:
    """Списання з причиною. Якщо batch_id не вказано — FIFO. op_date — лише для імпорту історії."""
    ts = f"{op_date}T12:00:00+00:00" if op_date else now_utc()
    with db.tx() as c:
        prod = c.execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        if batch_id:
            b = c.execute("SELECT * FROM batches WHERE id=? AND product_id=?", (batch_id, product_id)).fetchone()
            if not b:
                raise ValueError("Партію не знайдено")
            if b["grams_left"] < grams:
                raise InsufficientStock(prod["name"], grams, b["grams_left"])
            cost = round_cents(Decimal(grams) * d(b["landed_price_per_kg"]) / 1000)
            c.execute("UPDATE batches SET grams_left=grams_left-? WHERE id=?", (grams, batch_id))
            taken = [(batch_id, grams, cost)]
        else:
            taken = _consume_fifo(c, product_id, grams, prod["name"])
        cost_total = sum((t[2] for t in taken), ZERO)
        wid = c.execute(
            "INSERT INTO writeoffs(ts, op_date, kind, product_id, batch_id, grams_delta, cost_delta, reason, status, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, local_date_of(ts), "writeoff", product_id, batch_id, -grams, str(-cost_total), reason, "done", user_id),
        ).lastrowid
        for bid, g, cst in taken:
            c.execute("INSERT INTO writeoff_batches(writeoff_id, batch_id, grams_delta, cost_delta) VALUES (?,?,?,?)",
                      (wid, bid, -g, str(-cst)))
            _movement(c, ts, product_id, bid, -g, -cst, "writeoff", "writeoff", wid, user_id)
        audit(c, user_id, "writeoff.create", {"writeoff_id": wid, "reason": reason})
        return wid


def inventory_adjust(db: Database, user_id: int, batch_id: int, actual_grams: int, reason: str,
                     op_date: str | None = None) -> int:
    """Інвентаризаційне коригування конкретної партії до фактичного залишку."""
    ts = f"{op_date}T12:00:00+00:00" if op_date else now_utc()
    with db.tx() as c:
        b = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise ValueError("Партію не знайдено")
        delta = actual_grams - b["grams_left"]
        if delta == 0:
            raise ValueError("Фактичний залишок збігається з обліковим — коригування не потрібне")
        cost_delta = round_cents(Decimal(delta) * d(b["landed_price_per_kg"]) / 1000)
        c.execute("UPDATE batches SET grams_left=? WHERE id=?", (actual_grams, batch_id))
        wid = c.execute(
            "INSERT INTO writeoffs(ts, op_date, kind, product_id, batch_id, grams_delta, cost_delta, reason, status, created_by) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (ts, local_date_of(ts), "adjustment", b["product_id"], batch_id, delta, str(cost_delta), reason, "done", user_id),
        ).lastrowid
        c.execute("INSERT INTO writeoff_batches(writeoff_id, batch_id, grams_delta, cost_delta) VALUES (?,?,?,?)",
                  (wid, batch_id, delta, str(cost_delta)))
        _movement(c, ts, b["product_id"], batch_id, delta, cost_delta, "adjustment", "writeoff", wid, user_id)
        audit(c, user_id, "adjustment.create", {"writeoff_id": wid, "delta": delta, "reason": reason})
        return wid


def reset_all_data(db: Database, user_id: int) -> None:
    """Повне очищення облікових даних (товари, партії, операції, витрати). Користувачі лишаються.
    Перед цим робиться резервна копія."""
    db.make_backup()
    # файли документів закупівель і витрат — теж прибираємо (записи про них нижче)
    from pathlib import Path as _P
    for r in db.q("SELECT path FROM documents WHERE kind IN ('purchase','expense')"):
        try:
            _P(r["path"]).unlink(missing_ok=True)
        except Exception:
            pass
    with db.tx() as c:
        for t in ("sale_line_batches", "sale_lines", "sales", "writeoff_batches", "writeoffs", "stock_movements",
                  "batches", "purchase_lines", "purchases", "suppliers", "product_aliases", "products", "expenses",
                  "cash_register_days", "processed_updates", "shifts", "fsm_state"):
            c.execute(f"DELETE FROM {t}")
        c.execute("DELETE FROM documents WHERE kind IN ('purchase','expense')")
        c.execute("DELETE FROM app_settings WHERE key IN ('octobox_last_sync','shift_auto','alert_open','alert_close') OR key LIKE 'grp:%'")
        c.execute("INSERT INTO app_settings(key, value) VALUES ('sync_paused','1') ON CONFLICT(key) DO UPDATE SET value='1'")
        c.execute("DELETE FROM sqlite_sequence")
        audit(c, user_id, "db.reset", None)


# ======================= прив'язки назв каси =======================

import re as _re
import difflib as _difflib

_STOP = {"di", "con", "del", "della", "de", "dop", "igp", "ca", "g", "gr", "kg", "la", "il", "frische", "pasta",
         "einzel", "ohne", "kopf", "dissosato", "nostrana", "classico", "piccante", "stagionato", "dolche", "dolce"}


def norm_name(s: str) -> str:
    return _re.sub(r"\s+", " ", (s or "").strip().lower())


def _tokens(s: str) -> list[str]:
    toks = _re.findall(r"[a-zà-ÿäöüß]+", norm_name(s))
    return [t for t in toks if t not in _STOP and len(t) > 2]


def get_alias(db: Database, raw: str):
    r = db.one("SELECT product_id FROM product_aliases WHERE alias=?", (norm_name(raw),))
    return r["product_id"] if r else None


def set_alias(db: Database, raw: str, product_id: int, user_id: int) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO product_aliases(alias, alias_raw, product_id, created_by, created_at) VALUES (?,?,?,?,?) "
                  "ON CONFLICT(alias) DO UPDATE SET product_id=excluded.product_id, alias_raw=excluded.alias_raw",
                  (norm_name(raw), raw.strip(), product_id, user_id, now_utc()))


def list_aliases(db: Database):
    return db.q("SELECT a.*, p.name AS product_name FROM product_aliases a JOIN products p ON p.id=a.product_id ORDER BY a.alias_raw")


def match_product(db: Database, raw: str) -> tuple[int | None, str]:
    """-> (product_id | None, спосіб: 'alias' | 'exact' | 'auto' | 'none')."""
    pid = get_alias(db, raw)
    if pid:
        return pid, "alias"
    n = norm_name(raw)
    row = db.one("SELECT id FROM products WHERE lower(trim(name))=?", (n,))
    if row:
        return row["id"], "exact"
    rt = _tokens(raw)
    if not rt:
        return None, "none"
    best: list[tuple[float, int, int, str]] = []
    for p in db.q("SELECT p.id, p.name, COALESCE((SELECT SUM(grams_left) FROM batches b WHERE b.product_id=p.id),0) AS st "
                  "FROM products p WHERE p.active=1"):
        pt = _tokens(p["name"])
        if not pt:
            continue
        hit = 0
        for t in rt:
            if any(_difflib.SequenceMatcher(None, t, q).ratio() >= 0.8 for q in pt):
                hit += 1
        score = hit / len(rt)
        # перше слово назви (Culatta, Mortadella, Taleggio…) — головна ознака товару
        if _difflib.SequenceMatcher(None, rt[0], pt[0]).ratio() >= 0.8:
            score += 0.25
        if score >= 0.5:
            best.append((score, 1 if p["st"] > 0 else 0, -len(p["name"]), p["name"], p["id"]))
    if not best:
        return None, "none"
    best.sort(reverse=True)
    if len(best) > 1 and best[0][0] == best[1][0] and best[0][1] == best[1][1] and best[0][0] < 1.0:
        return None, "none"   # неоднозначно — хай вирішує людина
    return best[0][4], "auto"


# ======================= витрати =======================

EXP_TYPES = {"operating": "Операційні", "goods": "Оплата товару", "tax": "Податки", "investment": "Інвестиції"}


def add_expense(db: Database, user_id: int, op_date: str, exp_type: str, category: str, amount: Decimal,
                comment: str | None = None) -> int:
    if exp_type not in EXP_TYPES:
        raise ValueError("Невідомий тип витрати")
    with db.tx() as c:
        eid = c.execute(
            "INSERT INTO expenses(op_date, exp_type, category, amount, comment, status, created_by, created_at) "
            "VALUES (?,?,?,?,?,'done',?,?)",
            (op_date, exp_type, category.strip(), str(round_cents(amount)), comment, user_id, now_utc()),
        ).lastrowid
        audit(c, user_id, "expense.create", {"expense_id": eid, "amount": str(amount)})
        return eid


def cancel_expense(db: Database, expense_id: int, user_id: int) -> int:
    """Скасовує витрату і видаляє прикріплені до неї чеки. Повертає кількість видалених документів."""
    with db.tx() as c:
        c.execute("UPDATE expenses SET status='cancelled' WHERE id=?", (expense_id,))
        audit(c, user_id, "expense.cancel", {"expense_id": expense_id})
    return delete_documents_for(db, "expense", expense_id)


def restore_expense(db: Database, expense_id: int, user_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE expenses SET status='done' WHERE id=?", (expense_id,))
        audit(c, user_id, "expense.restore", {"expense_id": expense_id})


def update_expense(db: Database, expense_id: int, user_id: int, **fields) -> None:
    allowed = {"op_date", "exp_type", "category", "amount", "comment"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            raise ValueError(k)
        sets.append(f"{k}=?")
        vals.append(str(round_cents(v)) if k == "amount" else v)
    vals.append(expense_id)
    with db.tx() as c:
        c.execute(f"UPDATE expenses SET {', '.join(sets)} WHERE id=?", vals)
        audit(c, user_id, "expense.update", {"expense_id": expense_id, "fields": list(fields)})


def get_expense(db: Database, expense_id: int):
    return db.one("SELECT e.*, u.name AS user_name FROM expenses e LEFT JOIN users u ON u.telegram_id=e.created_by WHERE e.id=?", (expense_id,))


def recent_cancelled_expenses(db: Database, limit: int = 10):
    return db.q("SELECT * FROM expenses WHERE status='cancelled' ORDER BY id DESC LIMIT ?", (limit,))


def expense_categories(db: Database, exp_type: str | None = None) -> list[str]:
    sql = "SELECT category, COUNT(*) n FROM expenses WHERE status='done'"
    p: list = []
    if exp_type:
        sql += " AND exp_type=?"
        p.append(exp_type)
    return [r["category"] for r in db.q(sql + " GROUP BY category ORDER BY n DESC, category", p)]


def expenses_period(db: Database, date_from: str, date_to: str) -> dict:
    rows = db.q("SELECT * FROM expenses WHERE status='done' AND op_date BETWEEN ? AND ? ORDER BY op_date, id",
                (date_from, date_to))
    by_type: dict[str, Decimal] = {k: ZERO for k in EXP_TYPES}
    by_cat: dict[tuple, Decimal] = {}
    for r in rows:
        by_type[r["exp_type"]] += d(r["amount"])
        key = (r["exp_type"], r["category"])
        by_cat[key] = by_cat.get(key, ZERO) + d(r["amount"])
    return {"rows": rows, "by_type": by_type, "by_category": by_cat}


def recent_expenses(db: Database, limit: int = 15):
    return db.q("SELECT * FROM expenses WHERE status='done' ORDER BY op_date DESC, id DESC LIMIT ?", (limit,))


def cancel_writeoff(db: Database, writeoff_id: int, user_id: int, reason: str) -> None:
    ts = now_utc()
    with db.tx() as c:
        w = c.execute("SELECT * FROM writeoffs WHERE id=?", (writeoff_id,)).fetchone()
        if not w:
            raise ValueError("Операцію не знайдено")
        if w["status"] == "cancelled":
            raise DuplicateOperation("Операцію вже скасовано")
        for wb in c.execute("SELECT * FROM writeoff_batches WHERE writeoff_id=?", (writeoff_id,)).fetchall():
            b = c.execute("SELECT grams_left FROM batches WHERE id=?", (wb["batch_id"],)).fetchone()
            new_left = b["grams_left"] - wb["grams_delta"]
            if new_left < 0:
                raise StockError("Неможливо скасувати: залишок партії вже витрачено")
            c.execute("UPDATE batches SET grams_left=? WHERE id=?", (new_left, wb["batch_id"]))
            _movement(c, ts, w["product_id"], wb["batch_id"], -wb["grams_delta"], -d(wb["cost_delta"]),
                      f"{w['kind']}_cancel", "writeoff", writeoff_id, user_id)
        c.execute("UPDATE writeoffs SET status='cancelled', cancelled_at=?, cancelled_by=?, cancel_reason=? WHERE id=?",
                  (ts, user_id, reason, writeoff_id))
        audit(c, user_id, "writeoff.cancel", {"writeoff_id": writeoff_id, "reason": reason})


def recent_writeoffs(db: Database, limit: int = 15):
    return db.q("SELECT w.*, p.name AS product_name FROM writeoffs w JOIN products p ON p.id=w.product_id "
                "ORDER BY w.id DESC LIMIT ?", (limit,))


def purchases_period(db: Database, date_from: str, date_to: str) -> dict:
    rows = db.q("SELECT pu.id, pu.doc_date, pu.extra_costs, pu.comment, s.name AS supplier, "
                "(SELECT SUM(grams) FROM purchase_lines pl WHERE pl.purchase_id=pu.id) AS grams, "
                "(SELECT SUM(CAST(amount AS REAL)) FROM purchase_lines pl WHERE pl.purchase_id=pu.id) AS amount "
                "FROM purchases pu LEFT JOIN suppliers s ON s.id=pu.supplier_id "
                "WHERE pu.status='received' AND pu.doc_date BETWEEN ? AND ? ORDER BY pu.doc_date, pu.id", (date_from, date_to))
    by_sup: dict[str, list] = {}
    for r in rows:
        e = by_sup.setdefault(r["supplier"] or "—", [0, ZERO, 0])
        e[0] += r["grams"] or 0
        e[1] += Decimal(str(r["amount"] or 0))
        e[2] += 1
    prods = db.q("SELECT p.name, SUM(pl.grams) g, SUM(CAST(pl.amount AS REAL)) a FROM purchase_lines pl "
                 "JOIN purchases pu ON pu.id=pl.purchase_id JOIN products p ON p.id=pl.product_id "
                 "WHERE pu.status='received' AND pu.doc_date BETWEEN ? AND ? GROUP BY p.id ORDER BY a DESC LIMIT 10", (date_from, date_to))
    return {"rows": rows, "count": len(rows), "grams": sum(r["grams"] or 0 for r in rows),
            "amount": sum((Decimal(str(r["amount"] or 0)) for r in rows), ZERO),
            "extra": sum((d(r["extra_costs"]) for r in rows), ZERO), "by_supplier": by_sup, "top": prods}


def sales_by_day(db: Database, date_from: str, date_to: str):
    return db.q("SELECT sale_date, COUNT(*) n, SUM(CAST(total AS REAL)) t FROM sales WHERE status='done' AND sale_date BETWEEN ? AND ? "
                "GROUP BY sale_date ORDER BY sale_date", (date_from, date_to))


def recent_purchases(db: Database, limit: int = 15):
    return db.q("SELECT pu.*, s.name AS supplier_name FROM purchases pu LEFT JOIN suppliers s ON s.id=pu.supplier_id "
                "ORDER BY pu.id DESC LIMIT ?", (limit,))


# ======================= звіти =======================

def report_period(db: Database, date_from: str, date_to: str) -> dict:
    """Звіт за період [date_from, date_to] (дати за Europe/Vienna, включно)."""
    sales = db.q("SELECT * FROM sales WHERE status='done' AND sale_date BETWEEN ? AND ?", (date_from, date_to))
    sale_ids = [s["id"] for s in sales]
    revenue = sum((d(s["total"]) for s in sales), ZERO)
    cogs = sum((d(s["cost_total"]) for s in sales), ZERO)
    by_payment = {}
    for s in sales:
        by_payment[s["payment_method"]] = by_payment.get(s["payment_method"], ZERO) + d(s["total"])

    by_product: dict[int, dict] = {}
    if sale_ids:
        qmarks = ",".join("?" * len(sale_ids))
        for l in db.q(f"SELECT sl.*, p.name, p.category FROM sale_lines sl JOIN products p ON p.id=sl.product_id "
                      f"WHERE sl.sale_id IN ({qmarks})", sale_ids):
            e = by_product.setdefault(l["product_id"], {"name": l["name"], "category": l["category"], "grams": 0,
                                                        "pieces": 0, "amount": ZERO, "cost": ZERO})
            e["grams"] += l["grams"]
            e["pieces"] += l["pieces"] or 0
            e["amount"] += d(l["amount"])
            e["cost"] += d(l["cost"])
    for e in by_product.values():
        e["gross_profit"] = e["amount"] - e["cost"]
        e["margin_pct"] = (e["gross_profit"] / e["amount"] * 100) if e["amount"] else ZERO

    purchases = db.q(
        "SELECT pl.grams, pl.amount, pu.extra_costs, pu.id AS pid FROM purchase_lines pl JOIN purchases pu ON pu.id=pl.purchase_id "
        "WHERE pu.status='received' AND pu.doc_date BETWEEN ? AND ?", (date_from, date_to))
    purchased_grams = sum(r["grams"] for r in purchases)
    purchased_amount = sum((d(r["amount"]) for r in purchases), ZERO)
    extra = sum((d(r["extra_costs"]) for r in db.q(
        "SELECT extra_costs FROM purchases WHERE status='received' AND doc_date BETWEEN ? AND ?", (date_from, date_to))), ZERO)

    wos = db.q("SELECT w.*, p.name AS product_name FROM writeoffs w JOIN products p ON p.id=w.product_id "
               "WHERE w.status='done' AND w.op_date BETWEEN ? AND ? ORDER BY w.ts", (date_from, date_to))
    writeoff_grams = sum(-w["grams_delta"] for w in wos if w["grams_delta"] < 0)
    writeoff_cost = sum((-d(w["cost_delta"]) for w in wos if w["grams_delta"] < 0), ZERO)
    adj_grams = sum(w["grams_delta"] for w in wos if w["kind"] == "adjustment")
    adj_cost = sum((d(w["cost_delta"]) for w in wos if w["kind"] == "adjustment"), ZERO)

    stock = stock_summary(db)
    exp = expenses_period(db, date_from, date_to)
    gross = revenue - cogs
    after_wo = gross - writeoff_cost
    op_result = after_wo - exp["by_type"]["operating"] - exp["by_type"]["tax"]
    return {
        "expenses": exp, "gross_after_writeoffs": after_wo, "operating_result": op_result,
        "date_from": date_from, "date_to": date_to,
        "sales_count": len(sales), "revenue": revenue, "cogs": cogs, "gross_profit": revenue - cogs,
        "by_payment": by_payment, "by_product": sorted(by_product.values(), key=lambda e: -e["amount"]),
        "sold_grams": sum(e["grams"] for e in by_product.values()),
        "purchased_grams": purchased_grams, "purchased_amount": purchased_amount, "purchase_extra_costs": extra,
        "writeoffs": wos, "writeoff_grams": writeoff_grams, "writeoff_cost": writeoff_cost,
        "adjustment_grams": adj_grams, "adjustment_cost": adj_cost,
        "stock": stock, "stock_grams": sum(s["grams"] for s in stock),
        "stock_value": sum((s["cost_value"] for s in stock), ZERO),
    }


def product_ledger(db: Database, date_from: str, date_to: str) -> list[dict]:
    """Рядки для аркуша «Товар» у структурі звіту менеджера: по кожній партії —
    залишок на початок, закупівля, продаж, прибуток, втрати, залишок на кінець."""
    start_ts = f"{date_from}T00:00:00+00:00"
    end_ts = f"{date_to}T23:59:59+00:00"
    out = []
    for p in db.q("SELECT * FROM products ORDER BY category, name"):
        for b in db.q("SELECT * FROM batches WHERE product_id=? ORDER BY received_at, id", (p["id"],)):
            movs = db.q("SELECT * FROM stock_movements WHERE batch_id=? ORDER BY ts, id", (b["id"],))
            opening_g, opening_c = 0, ZERO
            buy_g, buy_c = 0, ZERO
            sold_g, sold_rev, sold_cost = 0, ZERO, ZERO
            loss_g, loss_c = 0, ZERO
            for m in movs:
                # рухи з датою за Відень
                mdate = local_date_of(m["ts"]) if m["kind"] not in ("purchase", "opening", "purchase_cancel") else b["received_at"][:10]
                g, cst = m["grams_delta"], d(m["cost_delta"])
                if mdate < date_from:
                    opening_g += g
                    opening_c += cst
                    continue
                if mdate > date_to:
                    continue
                if m["kind"] in ("purchase", "opening", "purchase_cancel"):
                    buy_g += g
                    buy_c += cst
                elif m["kind"] in ("sale", "sale_cancel"):
                    sold_g -= g
                    sold_cost -= cst
                else:
                    loss_g -= g
                    loss_c -= cst
            # виручка по партії — з sale_line_batches (пропорційно вазі позиції)
            for r in db.q(
                "SELECT slb.grams, sl.grams AS line_grams, sl.amount FROM sale_line_batches slb "
                "JOIN sale_lines sl ON sl.id=slb.sale_line_id JOIN sales s ON s.id=sl.sale_id "
                "WHERE slb.batch_id=? AND s.status='done' AND s.sale_date BETWEEN ? AND ?", (b["id"], date_from, date_to)):
                sold_rev += d(r["amount"]) * Decimal(r["grams"]) / Decimal(r["line_grams"])
            sold_rev = round_cents(sold_rev)
            closing_g = opening_g + buy_g - sold_g - loss_g
            if not any((opening_g, buy_g, sold_g, loss_g, closing_g)):
                continue
            landed = d(b["landed_price_per_kg"])
            out.append({
                "product": p["name"], "category": CATEGORIES[p["category"]], "batch": b["batch_code"] or f"#{b['id']}",
                "batch_id": b["id"], "date": b["received_at"][:10], "expiry": b["expiry_date"],
                "opening_g": opening_g, "opening_c": opening_c,
                "buy_g": buy_g, "buy_price": d(b["price_per_kg"]), "landed_price": landed, "buy_c": buy_c,
                "sold_g": sold_g, "sold_price": (sold_rev * 1000 / sold_g) if sold_g else None, "sold_rev": sold_rev,
                "profit": sold_rev - sold_cost, "sold_cost": sold_cost,
                "loss_g": loss_g, "loss_c": loss_c,
                "closing_g": closing_g, "closing_c": round_cents(Decimal(closing_g) * landed / 1000),
            })
    return out


def movements_export(db: Database, date_from: str, date_to: str):
    return db.q(
        "SELECT m.*, p.name AS product_name, b.batch_code, b.landed_price_per_kg, u.name AS user_name "
        "FROM stock_movements m JOIN products p ON p.id=m.product_id JOIN batches b ON b.id=m.batch_id "
        "LEFT JOIN users u ON u.telegram_id=m.user_id "
        "WHERE m.ts BETWEEN ? AND ? ORDER BY m.ts, m.id",
        (f"{date_from}T00:00:00", f"{date_to}T23:59:59+99:99"),
    )


# ======================= каса: імпорт і звірка =======================

def save_cash_day(db: Database, user_id: int, day: str, cash: Decimal, card: Decimal, source_file: str | None) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO cash_register_days(day, cash, card, total, source_file, imported_at, imported_by) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET cash=excluded.cash, card=excluded.card, total=excluded.total, "
            "source_file=excluded.source_file, imported_at=excluded.imported_at, imported_by=excluded.imported_by",
            (day, str(cash), str(card), str(cash + card), source_file, now_utc(), user_id),
        )


def reconcile(db: Database, date_from: str, date_to: str) -> list[dict]:
    days = {}
    for s in db.q("SELECT sale_date, payment_method, total FROM sales WHERE status='done' AND sale_date BETWEEN ? AND ?",
                  (date_from, date_to)):
        e = days.setdefault(s["sale_date"], {"bot_cash": ZERO, "bot_card": ZERO, "reg_cash": None, "reg_card": None})
        if s["payment_method"] == "card":
            e["bot_card"] += d(s["total"])
        else:
            e["bot_cash"] += d(s["total"])
    for r in db.q("SELECT * FROM cash_register_days WHERE day BETWEEN ? AND ?", (date_from, date_to)):
        e = days.setdefault(r["day"], {"bot_cash": ZERO, "bot_card": ZERO, "reg_cash": None, "reg_card": None})
        e["reg_cash"], e["reg_card"] = d(r["cash"]), d(r["card"])
    out = []
    for day in sorted(days):
        e = days[day]
        bot_total = e["bot_cash"] + e["bot_card"]
        reg_total = (e["reg_cash"] + e["reg_card"]) if e["reg_cash"] is not None else None
        out.append({"day": day, **e, "bot_total": bot_total, "reg_total": reg_total,
                    "diff": (bot_total - reg_total) if reg_total is not None else None})
    return out


# ======================= налаштування (в базі) =======================

def setting_get(db: Database, key: str, default: str = "") -> str:
    r = db.one("SELECT value FROM app_settings WHERE key=?", (key,))
    return r["value"] if r else default


def setting_set(db: Database, key: str, value: str) -> None:
    with db.tx() as c:
        c.execute("INSERT INTO app_settings(key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# ======================= завдання =======================

def create_task(db: Database, user_id: int, text: str, assignee_id: int | None, due_date: str | None) -> int:
    with db.tx() as c:
        tid = c.execute("INSERT INTO tasks(text, assignee_id, created_by, created_at, due_date, status) VALUES (?,?,?,?,?,'open')",
                        (text.strip(), assignee_id, user_id, now_utc(), due_date)).lastrowid
        audit(c, user_id, "task.create", {"task_id": tid})
        return tid


def tasks_for(db: Database, user_id: int, role: str):
    """Відкриті завдання: свої + «усім»; менеджер/адмін бачить усі відкриті."""
    if role in ("admin", "manager"):
        return db.q("SELECT t.*, u.name AS assignee_name FROM tasks t LEFT JOIN users u ON u.telegram_id=t.assignee_id "
                    "WHERE t.status='open' ORDER BY COALESCE(t.due_date,'9999'), t.id")
    return db.q("SELECT t.*, u.name AS assignee_name FROM tasks t LEFT JOIN users u ON u.telegram_id=t.assignee_id "
                "WHERE t.status='open' AND (t.assignee_id=? OR t.assignee_id IS NULL) ORDER BY COALESCE(t.due_date,'9999'), t.id", (user_id,))


def task_done(db: Database, task_id: int, user_id: int) -> dict | None:
    with db.tx() as c:
        t = c.execute("SELECT * FROM tasks WHERE id=? AND status='open'", (task_id,)).fetchone()
        if not t:
            return None
        c.execute("UPDATE tasks SET status='done', done_at=?, done_by=? WHERE id=?", (now_utc(), user_id, task_id))
        audit(c, user_id, "task.done", {"task_id": task_id})
        return dict(t)


def task_cancel(db: Database, task_id: int, user_id: int) -> None:
    with db.tx() as c:
        c.execute("UPDATE tasks SET status='cancelled', done_at=?, done_by=? WHERE id=? AND status='open'", (now_utc(), user_id, task_id))


def done_tasks(db: Database, days: int = 30, user_id: int | None = None):
    """Виконані/скасовані за останні N днів (менеджер бачить усі, продавець — свої)."""
    since = (dt_date_today() - __import__("datetime").timedelta(days=days)).isoformat()
    sql = ("SELECT t.*, u.name AS assignee_name, d.name AS done_name FROM tasks t "
           "LEFT JOIN users u ON u.telegram_id=t.assignee_id LEFT JOIN users d ON d.telegram_id=t.done_by "
           "WHERE t.status IN ('done','cancelled') AND t.done_at >= ?")
    p: list = [since]
    if user_id is not None:
        sql += " AND (t.assignee_id=? OR t.assignee_id IS NULL OR t.created_by=?)"
        p += [user_id, user_id]
    return db.q(sql + " ORDER BY t.done_at DESC LIMIT 30", p)


def dt_date_today():
    import datetime as _dt
    return _dt.date.fromisoformat(today_local())


def overdue_tasks(db: Database, today: str):
    return db.q("SELECT t.*, u.name AS assignee_name FROM tasks t LEFT JOIN users u ON u.telegram_id=t.assignee_id "
                "WHERE t.status='open' AND t.due_date IS NOT NULL AND t.due_date<=? ORDER BY t.due_date", (today,))


# ======================= зміни каси =======================

def open_shift(db: Database, user_id: int, cash_start: Decimal | None = None) -> int | None:
    """Повертає id нової зміни або None, якщо зміна вже відкрита."""
    with db.tx() as c:
        if c.execute("SELECT 1 FROM shifts WHERE closed_at IS NULL").fetchone():
            return None
        sid = c.execute("INSERT INTO shifts(shift_date, opened_at, opened_by, cash_start) VALUES (?,?,?,?)",
                        (today_local(), now_utc(), user_id, str(cash_start) if cash_start is not None else None)).lastrowid
        audit(c, user_id, "shift.open", {"shift_id": sid})
        return sid


def close_shift(db: Database, user_id: int, cash_end: Decimal | None = None, note: str | None = None):
    with db.tx() as c:
        s = c.execute("SELECT * FROM shifts WHERE closed_at IS NULL ORDER BY id DESC LIMIT 1").fetchone()
        if not s:
            return None
        c.execute("UPDATE shifts SET closed_at=?, closed_by=?, cash_end=?, note=? WHERE id=?",
                  (now_utc(), user_id, str(cash_end) if cash_end is not None else None, note, s["id"]))
        audit(c, user_id, "shift.close", {"shift_id": s["id"]})
        return dict(s)


def current_shift(db: Database):
    return db.one("SELECT s.*, u.name AS opened_name FROM shifts s LEFT JOIN users u ON u.telegram_id=s.opened_by WHERE s.closed_at IS NULL ORDER BY s.id DESC LIMIT 1")


def shifts_on(db: Database, day: str):
    return db.q("SELECT * FROM shifts WHERE shift_date=? ORDER BY id", (day,))


# ======================= документи =======================

def add_document(db: Database, user_id: int, kind: str, ref_id: int | None, file_name: str, path: str) -> int:
    with db.tx() as c:
        return c.execute("INSERT INTO documents(kind, ref_id, file_name, path, uploaded_by, uploaded_at) VALUES (?,?,?,?,?,?)",
                         (kind, ref_id, file_name, path, user_id, now_utc())).lastrowid


def list_documents(db: Database, kind: str, limit: int = 15):
    return db.q("SELECT * FROM documents WHERE kind=? ORDER BY id DESC LIMIT ?", (kind, limit))


def get_document(db: Database, doc_id: int):
    return db.one("SELECT * FROM documents WHERE id=?", (doc_id,))


def delete_documents_for(db: Database, kind: str, ref_id: int) -> int:
    """Видаляє файли й записи документів, прикріплених до запису. Повертає кількість."""
    from pathlib import Path as _P
    rows = db.q("SELECT * FROM documents WHERE kind=? AND ref_id=?", (kind, ref_id))
    for r in rows:
        try:
            _P(r["path"]).unlink(missing_ok=True)
        except Exception:
            pass
    with db.tx() as c:
        c.execute("DELETE FROM documents WHERE kind=? AND ref_id=?", (kind, ref_id))
    return len(rows)


def remove_batch(db: Database, batch_id: int, user_id: int, reason: str) -> None:
    """Видаляє (обнуляє) одну партію, якщо з неї ще нічого не списано. Історія руху зберігається."""
    ts = now_utc()
    with db.tx() as c:
        b = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b:
            raise ValueError("Партію не знайдено")
        if b["grams_left"] == 0 and b["grams_in"] > 0:
            raise DuplicateOperation("Партія вже порожня")
        if b["grams_left"] != b["grams_in"]:
            raise StockError(f"З партії вже списано {b['grams_in'] - b['grams_left']} г — спершу скасуйте ті продажі/списання")
        _movement(c, ts, b["product_id"], batch_id, -b["grams_in"],
                  -round_cents(Decimal(b["grams_in"]) * d(b["landed_price_per_kg"]) / 1000), "purchase_cancel", "batch", batch_id, user_id)
        # обнуляємо і прихід, і залишок: партія «видалена», але рух в історії зберігається
        c.execute("UPDATE batches SET grams_left=0, grams_in=0, comment=COALESCE(comment,'') || ' [видалено: ' || ? || ']' WHERE id=?", (reason, batch_id))
        if b["purchase_line_id"]:
            c.execute("UPDATE batches SET purchase_line_id=NULL WHERE id=?", (batch_id,))
            c.execute("DELETE FROM purchase_lines WHERE id=?", (b["purchase_line_id"],))
        audit(c, user_id, "batch.remove", {"batch_id": batch_id, "reason": reason})


def purchase_usage(db: Database, purchase_id: int) -> int:
    """Скільки грамів уже списано з партій закупівлі."""
    r = db.one("SELECT COALESCE(SUM(grams_in - grams_left),0) FROM batches WHERE purchase_id=?", (purchase_id,))
    return int(r[0])


def notify_targets(db: Database, min_role: str = "manager", actor_id: int | None = None) -> list[int]:
    """Кого сповіщати. Про дії адміністратора дізнаються лише інші адміни."""
    roles = ("admin", "manager") if min_role == "manager" else ("admin",)
    if actor_id is not None:
        actor = get_user(db, actor_id)
        if actor and actor["role"] == "admin":
            roles = ("admin",)
    return [r["telegram_id"] for r in db.q(f"SELECT telegram_id FROM users WHERE active=1 AND role IN ({','.join('?' * len(roles))})", roles)]


def visible_sales(db: Database, user, limit: int = 15, date: str | None = None):
    """Продавець бачить лише свої продажі; менеджер — усі, крім адмінських; адмін — усі."""
    sql = "SELECT s.* FROM sales s LEFT JOIN users u ON u.telegram_id=s.created_by WHERE 1=1"
    p: list = []
    if user["role"] == "seller":
        sql += " AND s.created_by=?"
        p.append(user["telegram_id"])
    elif user["role"] == "manager":
        sql += " AND COALESCE(u.role,'') <> 'admin'"
    if date:
        sql += " AND s.sale_date=?"
        p.append(date)
    sql += " ORDER BY s.id DESC LIMIT ?"
    p.append(limit)
    return db.q(sql, p)


# ======================= журнал дій і відкат =======================

ACTION_UA = {
    "sale.create": "🛒 Продаж створено", "sale.cancel": "🛒 Продаж скасовано", "sale.add_lines": "🛒 Продаж доповнено",
    "purchase.create": "📦 Закупівлю створено", "purchase.receive": "📦 Закупівлю оприбутковано", "purchase.cancel": "📦 Закупівлю скасовано",
    "stock.opening": "🏷 Початковий залишок", "batch.remove": "🏷 Партію видалено",
    "writeoff.create": "✂️ Списання", "adjustment.create": "📋 Інвентаризація", "writeoff.cancel": "✂️ Списання скасовано",
    "expense.create": "💸 Витрату додано", "expense.cancel": "💸 Витрату видалено", "expense.restore": "💸 Витрату відновлено",
    "expense.update": "💸 Витрату змінено",
    "task.create": "📋 Завдання створено", "task.done": "📋 Завдання виконано",
    "shift.open": "▶️ Зміну відкрито", "shift.close": "⏹ Зміну закрито", "db.reset": "🧹 Базу очищено",
}
UNDOABLE = {"sale.create", "sale.cancel", "purchase.receive", "purchase.cancel", "batch.remove", "writeoff.create",
            "adjustment.create", "expense.create", "expense.cancel", "task.done", "task.create"}


def audit_recent(db: Database, limit: int = 20, user_id: int | None = None):
    sql = "SELECT a.*, u.name AS user_name FROM audit_log a LEFT JOIN users u ON u.telegram_id=a.user_id"
    p: list = []
    if user_id is not None:
        sql += " WHERE a.user_id=?"
        p.append(user_id)
    return db.q(sql + " ORDER BY a.id DESC LIMIT ?", p + [limit])


def audit_describe(db: Database, row) -> str:
    """Людський опис запису журналу з назвами об'єктів."""
    det = json.loads(row["details"]) if row["details"] else {}
    a = row["action"]
    label = ACTION_UA.get(a, a)
    extra = ""
    try:
        if a.startswith("sale"):
            s = db.one("SELECT total, sale_date FROM sales WHERE id=?", (det.get("sale_id"),))
            extra = f" №{det.get('sale_id')} на {s['total']} €" if s else f" №{det.get('sale_id')}"
        elif a.startswith("purchase"):
            p = db.one("SELECT pu.doc_date, s.name FROM purchases pu LEFT JOIN suppliers s ON s.id=pu.supplier_id WHERE pu.id=?", (det.get("purchase_id"),))
            extra = f" №{det.get('purchase_id')} {p['name'] or ''} {p['doc_date']}" if p else f" №{det.get('purchase_id')}"
        elif a.startswith("expense"):
            e = db.one("SELECT category, amount, op_date FROM expenses WHERE id=?", (det.get("expense_id"),))
            extra = f" №{det.get('expense_id')}: {e['category']} {e['amount']} € ({e['op_date']})" if e else f" №{det.get('expense_id')}"
        elif a in ("writeoff.create", "adjustment.create", "writeoff.cancel"):
            w = db.one("SELECT w.grams_delta, w.reason, p.name FROM writeoffs w JOIN products p ON p.id=w.product_id WHERE w.id=?", (det.get("writeoff_id"),))
            extra = f" №{det.get('writeoff_id')}: {w['name']} {w['grams_delta']:+d} г ({w['reason']})" if w else ""
        elif a == "batch.remove" or a == "stock.opening":
            b = db.one("SELECT b.grams_in, p.name FROM batches b JOIN products p ON p.id=b.product_id WHERE b.id=?", (det.get("batch_id"),))
            extra = f" #{det.get('batch_id')}: {b['name']}" if b else f" #{det.get('batch_id')}"
        elif a.startswith("task"):
            t = db.one("SELECT text FROM tasks WHERE id=?", (det.get("task_id"),))
            extra = f" №{det.get('task_id')}: {t['text'][:40]}" if t else ""
    except Exception:
        pass
    return label + extra


def undo_action(db: Database, audit_id: int, user_id: int) -> str:
    """Відкат дії з журналу. Повертає повідомлення. Кидає StockError/ValueError, якщо неможливо."""
    row = db.one("SELECT * FROM audit_log WHERE id=?", (audit_id,))
    if not row:
        raise ValueError("Запис журналу не знайдено")
    det = json.loads(row["details"]) if row["details"] else {}
    a = row["action"]
    if a not in UNDOABLE:
        raise ValueError("Цю дію не можна повернути автоматично")
    if a == "sale.create":
        cancel_sale(db, det["sale_id"], user_id, f"відкат дії №{audit_id}")
        return f"Продаж №{det['sale_id']} скасовано, залишки відновлено"
    if a == "sale.cancel":
        new_id = restore_sale(db, det["sale_id"], user_id)
        return f"Продаж відновлено як №{new_id}"
    if a == "purchase.receive":
        cancel_purchase(db, det["purchase_id"], user_id, f"відкат дії №{audit_id}")
        delete_documents_for(db, "purchase", det["purchase_id"])
        return f"Закупівлю №{det['purchase_id']} скасовано"
    if a == "purchase.cancel":
        restore_purchase(db, det["purchase_id"], user_id)
        return f"Закупівлю №{det['purchase_id']} відновлено, партії повернуто на склад"
    if a == "batch.remove":
        restore_batch(db, det["batch_id"], user_id)
        return f"Партію #{det['batch_id']} повернуто на склад"
    if a in ("writeoff.create", "adjustment.create"):
        cancel_writeoff(db, det["writeoff_id"], user_id, f"відкат дії №{audit_id}")
        return f"Операцію №{det['writeoff_id']} скасовано"
    if a == "expense.create":
        cancel_expense(db, det["expense_id"], user_id)
        return f"Витрату №{det['expense_id']} видалено"
    if a == "expense.cancel":
        restore_expense(db, det["expense_id"], user_id)
        return f"Витрату №{det['expense_id']} відновлено"
    if a == "task.done":
        with db.tx() as c:
            c.execute("UPDATE tasks SET status='open', done_at=NULL, done_by=NULL WHERE id=?", (det["task_id"],))
        return f"Завдання №{det['task_id']} знову відкрите"
    if a == "task.create":
        task_cancel(db, det["task_id"], user_id)
        return f"Завдання №{det['task_id']} скасовано"
    raise ValueError("Невідома дія")


def restore_sale(db: Database, sale_id: int, user_id: int) -> int:
    """Повторно проводить скасований продаж (новим документом, тим самим часом і позиціями)."""
    s, lines = get_sale(db, sale_id)
    if not s or s["status"] != "cancelled":
        raise ValueError("Продаж не скасований")
    new_lines = [SaleLine(l["product_id"], l["grams"], d(l["price"]), l["pieces"]) for l in lines]
    res = create_sale(db, user_id, new_lines, s["payment_method"], comment=f"відновлено з №{sale_id}", sold_at=s["sold_at"])
    return res.sale_id


def restore_purchase(db: Database, purchase_id: int, user_id: int) -> None:
    ts = now_utc()
    with db.tx() as c:
        p = c.execute("SELECT * FROM purchases WHERE id=?", (purchase_id,)).fetchone()
        if not p or p["status"] != "cancelled":
            raise ValueError("Закупівля не скасована")
        for b in c.execute("SELECT * FROM batches WHERE purchase_id=?", (purchase_id,)).fetchall():
            if b["grams_left"] != 0:
                raise StockError("Партії закупівлі вже мають залишок — відновлення неможливе")
            c.execute("UPDATE batches SET grams_left=grams_in WHERE id=?", (b["id"],))
            _movement(c, ts, b["product_id"], b["id"], b["grams_in"], round_cents(Decimal(b["grams_in"]) * d(b["landed_price_per_kg"]) / 1000),
                      "purchase", "purchase", purchase_id, user_id)
        c.execute("UPDATE purchases SET status='received', cancelled_at=NULL, cancelled_by=NULL, cancel_reason=NULL WHERE id=?", (purchase_id,))
        audit(c, user_id, "purchase.receive", {"purchase_id": purchase_id, "restored": True})


def restore_batch(db: Database, batch_id: int, user_id: int) -> None:
    ts = now_utc()
    with db.tx() as c:
        b = c.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
        if not b or b["grams_in"] != 0:
            raise ValueError("Партія не видалена")
        m = c.execute("SELECT grams_delta FROM stock_movements WHERE batch_id=? AND kind IN ('purchase','opening') ORDER BY id LIMIT 1", (batch_id,)).fetchone()
        if not m:
            raise ValueError("Немає даних про прихід партії")
        g = m["grams_delta"]
        c.execute("UPDATE batches SET grams_in=?, grams_left=? WHERE id=?", (g, g, batch_id))
        _movement(c, ts, b["product_id"], batch_id, g, round_cents(Decimal(g) * d(b["landed_price_per_kg"]) / 1000), "purchase", "batch", batch_id, user_id)
        audit(c, user_id, "stock.opening", {"batch_id": batch_id, "restored": True})


# ======================= висновки / підказки =======================

def stale_products(db: Database, days: int = 14):
    """Товари із залишком, що не продавались N днів (або взагалі)."""
    since = (dt_date_today() - __import__("datetime").timedelta(days=days)).isoformat()
    rows = db.q(
        "SELECT p.id, p.name, COALESCE(SUM(b.grams_left),0) AS grams, "
        "(SELECT MAX(s.sale_date) FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE sl.product_id=p.id AND s.status='done') AS last_sale "
        "FROM products p JOIN batches b ON b.product_id=p.id AND b.grams_left>0 WHERE p.active=1 GROUP BY p.id")
    return [r for r in rows if r["grams"] > 0 and (r["last_sale"] is None or r["last_sale"] < since)]


def low_stock(db: Database):
    """Мало або немає — лише товари, що є в обороті (залишок > 0 або продавались за 30 днів)."""
    since = (dt_date_today() - __import__("datetime").timedelta(days=30)).isoformat()
    active = {r["product_id"] for r in db.q(
        "SELECT DISTINCT sl.product_id FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE s.status='done' AND s.sale_date>=?", (since,))}
    out = []
    for r in stock_summary(db, include_zero=True):
        p = r["product"]
        if r["grams"] <= 0 and p["id"] not in active:
            continue
        if p["sale_mode"] == "piece" and p["piece_grams"]:
            if r["grams"] < 3 * p["piece_grams"]:
                out.append(r)
        elif r["grams"] < 500:
            out.append(r)
    return out


def previous_batch_price(db: Database, product_id: int, before_purchase_id: int):
    r = db.one("SELECT price_per_kg, received_at FROM batches WHERE product_id=? AND source='purchase' AND purchase_id<>? "
               "AND grams_in>0 ORDER BY received_at DESC, id DESC LIMIT 1", (product_id, before_purchase_id))
    return r


def product_sales_30d(db: Database, product_id: int) -> dict:
    since = (dt_date_today() - __import__("datetime").timedelta(days=30)).isoformat()
    r = db.one("SELECT COALESCE(SUM(sl.grams),0) g, COALESCE(SUM(CAST(sl.amount AS REAL)),0) a, COALESCE(SUM(CAST(sl.cost AS REAL)),0) c, COUNT(DISTINCT s.id) n "
               "FROM sale_lines sl JOIN sales s ON s.id=sl.sale_id WHERE sl.product_id=? AND s.status='done' AND s.sale_date>=?", (product_id, since))
    return {"grams": int(r["g"]), "amount": Decimal(str(round(r["a"], 2))), "cost": Decimal(str(round(r["c"], 2))), "checks": r["n"]}


def conclusions(db: Database, rep: dict) -> list[str]:
    """3–4 висновки словами до звіту."""
    out = []
    bp = rep["by_product"]
    if bp:
        top = bp[0]
        out.append(f"🏆 Найбільше виручки дав {top['name']}: {top['amount']:.0f} € ({top['grams'] / 1000:.1f} кг).")
        sold_enough = [e for e in bp if e["amount"] >= 20]
        if sold_enough:
            worst = min(sold_enough, key=lambda e: e["margin_pct"])
            if worst["margin_pct"] < 45:
                out.append(f"📉 Найнижча маржа — {worst['name']}: {worst['margin_pct']:.0f} %. Перевірте ціну або закупівлю.")
    stale = stale_products(db, 14)
    if stale:
        names = ", ".join(f"{r['name']} ({r['grams'] / 1000:.1f} кг)" for r in sorted(stale, key=lambda r: -r["grams"])[:4])
        out.append(f"🧊 Не продавались понад 14 днів: {names}.")
    low = low_stock(db)
    if low:
        out.append("📦 Закінчується: " + ", ".join(r["product"]["name"] for r in low[:5]) + ".")
    exp = batches_expiring(db, 3)
    if exp:
        out.append("⏰ Спливає за 3 дні: " + ", ".join(f"{r['product_name']} до {r['expiry_date'][8:10]}.{r['expiry_date'][5:7]}" for r in exp[:4]) + ".")
    return out


def reconcile_grouped(db: Database, date_from: str, date_to: str) -> list[dict]:
    """Звірка: дні до першого чека з каси (історія місячними блоками) — по місяцях, далі — по днях."""
    first = db.one("SELECT MIN(sale_date) d FROM sales WHERE client_key LIKE 'octobox:%'")
    cut = first["d"] if first and first["d"] else "9999-12-31"
    rows = reconcile(db, date_from, date_to)
    out: dict[str, dict] = {}
    for r in rows:
        key = r["day"] if r["day"] >= cut else r["day"][:7]
        e = out.setdefault(key, {"day": key, "bot_total": ZERO, "reg_total": None, "monthly": len(key) == 7})
        e["bot_total"] += r["bot_total"]
        if r["reg_total"] is not None:
            e["reg_total"] = (e["reg_total"] or ZERO) + r["reg_total"]
    for e in out.values():
        e["diff"] = (e["bot_total"] - e["reg_total"]) if e["reg_total"] is not None else None
    return [out[k] for k in sorted(out)]
