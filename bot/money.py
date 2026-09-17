"""Точна арифметика для грошей і ваги.

Правило округлення (єдине для всієї системи):
  * усі проміжні обчислення — Decimal без округлення;
  * ОСТАТОЧНІ суми в євро (сума позиції, сума продажу, собівартість
    позиції, сума списання) округлюються до центів за правилом
    ROUND_HALF_UP (0.005 -> 0.01);
  * підсумки за документ/період = сума вже округлених позицій
    (тому підсумок завжди збігається з сумою рядків у звіті);
  * вага зберігається як ціле число грамів, ціна за кг — Decimal
    з точністю до 4 знаків (для «приведеної» ціни партії з розподіленими
    витратами).
"""
from __future__ import annotations

import re
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation

CENT = Decimal("0.01")
PRICE_PREC = Decimal("0.0001")
ZERO = Decimal("0")

_num_re = re.compile(r"^\s*([+-]?\d+(?:[.,]\d+)?)\s*$")


class ParseError(ValueError):
    pass


def parse_decimal(text: str) -> Decimal:
    """'12,5' або '12.5' -> Decimal('12.5')."""
    m = _num_re.match(text or "")
    if not m:
        raise ParseError("Введіть число, наприклад 12,5 або 12.5")
    try:
        return Decimal(m.group(1).replace(",", "."))
    except InvalidOperation as e:  # pragma: no cover
        raise ParseError("Некоректне число") from e


def parse_weight_grams(text: str) -> int:
    """Приймає '250', '250 г', '250g', '0,25', '0.25 кг', '1,2кг'.

    Правило: якщо явно вказано кг — множимо на 1000; якщо вказано г — як є;
    без одиниці: число < 20 трактується як кг (0,25 -> 250 г), інакше як грами.
    Результат — ціле число грамів (округлення до 1 г).
    """
    t = (text or "").strip().lower().replace(" ", "")
    unit = None
    for suf in ("кг", "kg", "k", "к"):
        if t.endswith(suf):
            unit, t = "kg", t[: -len(suf)]
            break
    if unit is None:
        for suf in ("гр", "г", "g"):
            if t.endswith(suf):
                unit, t = "g", t[: -len(suf)]
                break
    val = parse_decimal(t)
    if val <= 0:
        raise ParseError("Вага має бути більшою за 0")
    if unit == "kg" or (unit is None and val < 20):
        grams = val * 1000
    else:
        grams = val
    g = int(grams.quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if g <= 0:
        raise ParseError("Вага має бути не менше 1 г")
    return g


def parse_money(text: str) -> Decimal:
    v = parse_decimal(text)
    if v < 0:
        raise ParseError("Сума не може бути від'ємною")
    return v.quantize(PRICE_PREC)


def round_cents(v: Decimal) -> Decimal:
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def line_amount(grams: int, price_per_kg: Decimal) -> Decimal:
    """250 г × 28 €/кг = 7,00 €."""
    return round_cents(Decimal(grams) * price_per_kg / Decimal(1000))


def piece_amount(pieces: int, price_per_piece: Decimal) -> Decimal:
    return round_cents(Decimal(pieces) * price_per_piece)


def fmt_money(v: Decimal | str | None) -> str:
    if v is None:
        return "—"
    v = Decimal(v)
    s = f"{round_cents(v):,.2f}".replace(",", " ").replace(".", ",")
    return f"{s} €"


def fmt_price(v: Decimal | str | None) -> str:
    """Ціна: завжди 2 знаки (14,60), якщо є копійки дрібніші за цент — 4 знаки (14,3315)."""
    if v is None:
        return "—"
    v = Decimal(v)
    q2 = v.quantize(Decimal("0.01"))
    s = f"{q2:.2f}" if q2 == v else f"{v:.4f}".rstrip("0")
    return s.replace(".", ",")


def fmt_kg(grams: int | None) -> str:
    if grams is None:
        return "—"
    kg = Decimal(grams) / 1000
    return f"{kg:.3f}".replace(".", ",") + " кг"


def fmt_grams(grams: int) -> str:
    if grams >= 1000:
        return fmt_kg(grams)
    return f"{grams} г"


def d(v) -> Decimal:
    """Безпечне перетворення значення з БД (TEXT) у Decimal."""
    if v is None:
        return ZERO
    return Decimal(str(v))
