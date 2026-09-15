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
