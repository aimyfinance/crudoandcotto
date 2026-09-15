"""Службові команди: python -m bot.tools <команда>

  seed-products products.csv   — масово створити товари з CSV (name;category;sale_mode;piece_grams;retail_price;sku)
  backup                       — зробити резервну копію у BACKUP_DIR
  restore file.db              — відновити базу з файлу
  add-admin <telegram_id>      — додати адміністратора
"""
from __future__ import annotations

import csv
import sys
from decimal import Decimal

from . import services as S
from .db import get_db


def seed_products(path: str) -> None:
    db = get_db()
    n = 0
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f, delimiter=";"):
            name = row["name"].strip()
            if db.one("SELECT 1 FROM products WHERE lower(name)=lower(?)", (name,)):
                print("пропущено (вже є):", name)
                continue
            pg = int(row["piece_grams"]) if row.get("piece_grams") else None
            S.create_product(db, name, row["category"].strip(), row["sale_mode"].strip(),
                             Decimal(row["retail_price"].replace(",", ".") or "0"), pg, row.get("sku") or None)
            n += 1
    print(f"створено товарів: {n}")


def main(argv: list[str]) -> None:
    if not argv:
        print(__doc__)
        return
    cmd, *args = argv
    if cmd == "seed-products":
        seed_products(args[0])
    elif cmd == "backup":
        print("копія:", get_db().make_backup())
    elif cmd == "restore":
        get_db().restore_from(args[0])
        print("відновлено з", args[0])
    elif cmd == "add-admin":
        S.upsert_user(get_db(), int(args[0]), "admin")
        print("адміністратора додано")
    else:
        print(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
