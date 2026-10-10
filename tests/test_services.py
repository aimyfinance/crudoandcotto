import threading
from decimal import Decimal

import pytest

from bot.db import Database, today_local
from bot import services as S
from bot.money import parse_weight_grams, parse_decimal, line_amount, ParseError


@pytest.fixture
def db(tmp_path):
    return Database(tmp_path / "t.db")


@pytest.fixture
def prod(db):
    return S.create_product(db, "Prosciutto di Parma", "meat", "weight", Decimal("36.32"))


def test_parsing():
    assert parse_weight_grams("250") == 250
    assert parse_weight_grams("250 г") == 250
    assert parse_weight_grams("0,25") == 250
    assert parse_weight_grams("0.25 кг") == 250
    assert parse_weight_grams("1,2кг") == 1200
    assert parse_weight_grams("1200") == 1200
    assert parse_decimal("12,5") == Decimal("12.5")
    with pytest.raises(ParseError):
        parse_weight_grams("abc")
    with pytest.raises(ParseError):
        parse_weight_grams("0")


def test_rounding_rule():
    assert line_amount(250, Decimal("28")) == Decimal("7.00")
    assert line_amount(333, Decimal("29.99")) == Decimal("9.99")   # 9.98667 -> 9.99
    assert line_amount(125, Decimal("36.32")) == Decimal("4.54")   # 4.54 exactly
    assert line_amount(1, Decimal("5")) == Decimal("0.01")         # 0.005 -> 0.01 (HALF_UP)


def test_purchase_landed_cost_and_partial_sale(db, prod):
    p2 = S.create_product(db, "Taleggio", "cheese", "weight", Decimal("28"))
    pid = S.create_purchase(
        db, 1, "2026-09-01", "Salumificio",
        [S.PurchaseLine(prod, 10000, Decimal("29.80"), "2026-12-01"),
         S.PurchaseLine(p2, 2000, Decimal("19.70"))],
        extra_costs=Decimal("60"),  # 60 € транспорту на 12 кг = 5 €/кг
    )
    b1 = S.batches_of_product(db, prod)[0]
    assert b1["landed_price_per_kg"] == "34.8000"
    assert b1["price_per_kg"] == "29.80"
    assert S.stock_of_product(db, prod) == 10000

    r = S.create_sale(db, 1, [S.SaleLine(prod, 250, Decimal("36.32"))], "cash", client_key="k1")
    assert r.total == Decimal("9.08")
    assert r.cost_total == Decimal("8.70")   # 0.25 * 34.8
    assert S.stock_of_product(db, prod) == 9750

    # покупка залишається у статусі received, скасувати не можна, бо є продаж
    with pytest.raises(S.StockError):
        S.cancel_purchase(db, pid, 1, "помилка")


def test_fifo_across_batches_with_different_prices(db, prod):
    S.create_purchase(db, 1, "2026-08-01", "A", [S.PurchaseLine(prod, 1000, Decimal("20"))])
    S.create_purchase(db, 1, "2026-09-01", "B", [S.PurchaseLine(prod, 1000, Decimal("30"))])
    r = S.create_sale(db, 1, [S.SaleLine(prod, 1500, Decimal("40"))], "card")
    assert r.total == Decimal("60.00")
    # 1000 г по 20 + 500 г по 30 = 20 + 15
    assert r.cost_total == Decimal("35.00")
    bs = S.batches_of_product(db, prod, only_open=False)
    assert [b["grams_left"] for b in bs] == [0, 500]
    # звіт
    rep = S.report_period(db, today_local(), today_local())
    assert rep["revenue"] == Decimal("60.00")
    assert rep["cogs"] == Decimal("35.00")
    assert rep["gross_profit"] == Decimal("25.00")
    assert rep["by_payment"]["card"] == Decimal("60.00")


def test_insufficient_stock(db, prod):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 500, Decimal("20"))])
    with pytest.raises(S.InsufficientStock):
        S.create_sale(db, 1, [S.SaleLine(prod, 501, Decimal("40"))], "cash")
    assert S.stock_of_product(db, prod) == 500  # транзакція відкочена
    assert db.one("SELECT COUNT(*) FROM sales")[0] == 0


def test_piece_product(db):
    pid = S.create_product(db, "Tortelloni Tartufo 300g", "pasta", "piece", Decimal("14.00"), piece_grams=300)
    S.create_purchase(db, 1, "2026-09-01", "Pasta", [S.PurchaseLine(pid, 3000, Decimal("15.30"))])
    r = S.create_sale(db, 1, [S.SaleLine(pid, 600, Decimal("14.00"), pieces=2)], "cash")
    assert r.total == Decimal("28.00")
    assert r.cost_total == Decimal("9.18")
    assert S.stock_of_product(db, pid) == 2400


def test_writeoff_adjust_cancel(db, prod):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 1000, Decimal("20"))])
    wid = S.write_off(db, 1, prod, 200, "зіпсувалось")
    assert S.stock_of_product(db, prod) == 800
    b = S.batches_of_product(db, prod)[0]
    aid = S.inventory_adjust(db, 1, b["id"], 850, "інвентаризація")
    assert S.stock_of_product(db, prod) == 850
    S.cancel_writeoff(db, aid, 1, "помилково")
    assert S.stock_of_product(db, prod) == 800
    S.cancel_writeoff(db, wid, 1, "помилково")
    assert S.stock_of_product(db, prod) == 1000
    with pytest.raises(S.DuplicateOperation):
        S.cancel_writeoff(db, wid, 1, "ще раз")


def test_sale_cancel_restores_batches(db, prod):
    S.create_purchase(db, 1, "2026-08-01", "A", [S.PurchaseLine(prod, 300, Decimal("20"))])
    S.create_purchase(db, 1, "2026-09-01", "B", [S.PurchaseLine(prod, 300, Decimal("30"))])
    r = S.create_sale(db, 1, [S.SaleLine(prod, 400, Decimal("40"))], "cash")
    assert [b["grams_left"] for b in S.batches_of_product(db, prod, False)] == [0, 200]
    S.cancel_sale(db, r.sale_id, 1, "покупець передумав")
    assert [b["grams_left"] for b in S.batches_of_product(db, prod, False)] == [300, 300]
    with pytest.raises(S.DuplicateOperation):
        S.cancel_sale(db, r.sale_id, 1, "ще раз")
    s, _ = S.get_sale(db, r.sale_id)
    assert s["status"] == "cancelled"
    assert db.one("SELECT COUNT(*) FROM stock_movements WHERE kind='sale_cancel'")[0] == 2


def test_duplicate_event(db, prod):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 1000, Decimal("20"))])
    S.create_sale(db, 1, [S.SaleLine(prod, 100, Decimal("40"))], "cash", client_key="sale-abc")
    with pytest.raises(S.DuplicateOperation):
        S.create_sale(db, 1, [S.SaleLine(prod, 100, Decimal("40"))], "cash", client_key="sale-abc")
    assert S.stock_of_product(db, prod) == 900
    assert S.claim_key(db, "cb:1") is True
    assert S.claim_key(db, "cb:1") is False


def test_concurrent_sales_never_oversell(db, prod):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 1000, Decimal("20"))])
    ok, fail = [], []
    barrier = threading.Barrier(20)

    def worker(i):
        barrier.wait()
        try:
            S.create_sale(db, i, [S.SaleLine(prod, 150, Decimal("40"))], "cash")
            ok.append(i)
        except S.InsufficientStock:
            fail.append(i)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ok) == 6          # 6 * 150 = 900 <= 1000, сьомий не проходить
    assert len(fail) == 14
    assert S.stock_of_product(db, prod) == 100
    total_moved = db.one("SELECT SUM(grams_delta) FROM stock_movements")[0]
    assert total_moved == 100


def test_product_ledger_matches_report_structure(db, prod):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 66950, Decimal("29.80"))])
    S.create_sale(db, 1, [S.SaleLine(prod, 31050, Decimal("36.32"))], "cash")
    S.write_off(db, 1, prod, 11500, "втрати")
    rows = S.product_ledger(db, "2026-01-01", "2030-12-31")
    assert len(rows) == 1
    r = rows[0]
    assert r["buy_g"] == 66950 and r["buy_c"] == Decimal("1995.11")
    assert r["sold_g"] == 31050 and r["sold_rev"] == Decimal("1127.74")
    assert r["loss_g"] == 11500 and r["loss_c"] == Decimal("342.70")
    assert r["closing_g"] == 24400 and r["closing_c"] == Decimal("727.12")
    # прибуток = виручка − собівартість проданого (925.29) = 202.45
    assert r["profit"] == Decimal("202.45")


def test_backup_restore(db, prod, tmp_path):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 1000, Decimal("20"))])
    bk = db.backup_to(tmp_path / "bk.db")
    S.create_sale(db, 1, [S.SaleLine(prod, 100, Decimal("40"))], "cash")
    assert S.stock_of_product(db, prod) == 900
    db.restore_from(bk)
    assert S.stock_of_product(db, prod) == 1000


def test_reconcile(db, prod):
    S.create_purchase(db, 1, "2026-09-01", "A", [S.PurchaseLine(prod, 1000, Decimal("20"))])
    S.create_sale(db, 1, [S.SaleLine(prod, 100, Decimal("40"))], "cash")
    S.create_sale(db, 1, [S.SaleLine(prod, 100, Decimal("40"))], "card")
    day = today_local()
    S.save_cash_day(db, 1, day, Decimal("4"), Decimal("4.5"), "z.csv")
    rec = S.reconcile(db, day, day)[0]
    assert rec["bot_total"] == Decimal("8.00")
    assert rec["diff"] == Decimal("-0.50")


def test_unmapped_suggest_and_upsert(db):
    from decimal import Decimal
    p1 = S.create_product(db, "Prosciutto Crudo Nazionale dissosato", "meat", "weight", Decimal("36"))
    S.create_product(db, "Taleggio DOP", "cheese", "weight", Decimal("28"))
    sug = S.suggest_products(db, "Prosciutto di Parma")
    assert sug and sug[0][0] == p1
    uid = S.unmapped_upsert(db, "Prosciutto di Parma", 25, Decimal("221.86"))
    S.unmapped_upsert(db, "Prosciutto di Parma", 2, Decimal("10"))
    row = S.unmapped_get(db, uid)
    assert row["count"] == 27 and abs(float(row["amount"]) - 231.86) < 0.01 and row["status"] == "open"
    S.unmapped_set(db, uid, "open", asked=True)
    assert S.unmapped_get(db, uid)["asked_at"]
    S.unmapped_set(db, uid, "ignored")
    assert not S.unmapped_open(db)


def test_purchase_absorbs_auto_batch(db):
    """Продали за касою до внесення закупівлі → тимчасова партія; закупівля має її поглинути."""
    import datetime as dt
    from decimal import Decimal
    from bot.tools import import_octobox
    from bot.db import set_db
    set_db(db); S.ensure_admins(db, [1])
    pid = S.create_product(db, "Taleggio", "cheese", "weight", Decimal("28.9"))
    day = dt.datetime.now()
    rc = {"number": "5001", "dt": day, "payment": "cash", "refund": False, "odoo_id": 1,
          "lines": [{"name": "Taleggio", "group": None, "grams": 1920, "amount": Decimal("55.46"), "discount": Decimal(0)}]}
    st = import_octobox([rc], 1)
    assert st["created"] == 1 and st["shortfalls"]
    assert S.stock_of_product(db, pid) == 0
    sale = db.one("SELECT * FROM sales WHERE client_key='octobox:5001'")
    assert Decimal(sale["cost_total"]) == 0          # тимчасова партія без ціни → маржа 100 %
    # менеджер вносить реальну поставку 6,69 кг по 8,70 €/кг (вже включає продані 1,92 кг)
    S.create_purchase(db, 1, day.date().isoformat(), "Rognoni", [S.PurchaseLine(pid, 6690, Decimal("8.70"))])
    assert S.stock_of_product(db, pid) == 6690 - 1920
    sale = db.one("SELECT * FROM sales WHERE client_key='octobox:5001'")
    assert Decimal(sale["cost_total"]) == Decimal("16.70")   # 1,92 кг × 8,70
    auto = db.one("SELECT * FROM batches WHERE comment LIKE 'продано за касою%'")
    assert auto["grams_left"] == 0 and "[зараховано" in auto["comment"]
    # повторна закупівля вже нічого не поглинає
    S.create_purchase(db, 1, day.date().isoformat(), "Rognoni", [S.PurchaseLine(pid, 1000, Decimal("8.70"))])
    assert S.stock_of_product(db, pid) == 6690 - 1920 + 1000


def test_unmatched_counted_on_reimport(db):
    import datetime as dt
    from decimal import Decimal
    from bot.tools import import_octobox
    from bot.db import set_db
    set_db(db); S.ensure_admins(db, [1])
    pid = S.create_product(db, "Golfetta", "meat", "weight", Decimal("45"))
    S.add_opening_stock(db, 1, pid, 5000, Decimal("20"), None, dt.date.today().isoformat(), "test")
    rc = {"number": "7001", "dt": dt.datetime.now(), "payment": "cash", "refund": False, "odoo_id": 1,
          "lines": [{"name": "Golfetta", "group": None, "grams": 500, "amount": Decimal("22.5"), "discount": Decimal(0)},
                    {"name": "Prosciutto di Parma", "group": None, "grams": 300, "amount": Decimal("12"), "discount": Decimal(0)}]}
    st1 = import_octobox([rc], 1)
    assert st1["created"] == 1 and st1["unmatched"] == {"Prosciutto di Parma": 1}
    st2 = import_octobox([rc], 1)          # повторний синк — чек уже є
    assert st2["skipped_dup"] == 1 and st2["unmatched"] == {"Prosciutto di Parma": 1}
    assert st2["unmatched_amount"]["Prosciutto di Parma"] == Decimal("12")


def test_hts_enrich_weights_and_payments():
    import asyncio
    from bot.octobox_api import _hts_enrich, _receipt_from_order
    from bot.config import TZ

    class FakeClient:
        async def model_exists(self, m): return True
        async def fields_get(self, m): return {k: {} for k in ("order_id", "payment_method_id", "scale_weight", "scale_weight_computed", "price_unit", "qty", "price_subtotal")}
        async def search_read(self, model, domain, fields, limit=200, order=None):
            return [{"order_id": [1, "K01/1874"], "payment_method_id": [3049, "Karte"], "scale_weight": 268.0, "price_unit": 10.18, "qty": 1.0},
                    {"order_id": [1, "K01/1874"], "payment_method_id": [3049, "Karte"], "scale_weight": 188.0, "price_unit": 8.46, "qty": 1.0},
                    {"order_id": [2, "K01/1875"], "payment_method_id": [3048, "Bar"], "scale_weight": 0.0, "scale_weight_computed": 0.5, "price_unit": 20.0, "qty": 1.0}]
    orders = [{"id": 1, "date_order": "2026-10-09 13:39:11", "name": "K01/1874"}, {"id": 2, "date_order": "2026-10-09 13:40:00", "name": "K01/1875"}]
    lines = {1: [{"order_id": [1], "price_unit": 10.18, "qty": 1.0, "price_subtotal_incl": 10.18, "product_id": [5, "Prosciutto Cotto"]},
                 {"order_id": [1], "price_unit": 8.46, "qty": 1.0, "price_subtotal_incl": 8.46, "product_id": [6, "Golfetta"]}],
             2: [{"order_id": [2], "price_unit": 20.0, "qty": 1.0, "price_subtotal_incl": 20.0, "product_id": [7, "Taleggio"]}]}
    pays = asyncio.run(_hts_enrich(FakeClient(), orders, lines))
    assert lines[1][0]["_scale_grams"] == 268 and lines[1][1]["_scale_grams"] == 188 and lines[2][0]["_scale_grams"] == 500
    r1 = _receipt_from_order(orders[0], lines[1], pays[1], None, TZ)
    r2 = _receipt_from_order(orders[1], lines[2], pays[2], None, TZ)
    assert r1["payment"] == "card" and r2["payment"] == "cash"
    assert [l["grams"] for l in r1["lines"]] == [268, 188]


def test_archived_product_not_matched(db):
    from decimal import Decimal
    p_short = S.create_product(db, "Taleggio", "cheese", "weight", Decimal("28"))
    p_dop = S.create_product(db, "Taleggio DOP VERO ROGNONI", "cheese", "weight", Decimal("28"))
    assert S.match_product(db, "Taleggio")[0] == p_short
    S.update_product(db, p_short, active=0)
    pid, how = S.match_product(db, "Taleggio")
    assert pid != p_short                      # архівний не ловить продажі
    S.set_alias(db, "Taleggio", p_dop, 1)
    assert S.match_product(db, "Taleggio") == (p_dop, "alias")
    S.delete_alias(db, "Taleggio")
    assert S.match_product(db, "Taleggio")[1] != "alias"
