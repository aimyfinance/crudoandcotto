"""Розпізнавання інвойсів постачальника (PDF/фото) через Claude API і підготовка чернетки закупівлі.

Модель повертає JSON; далі все детерміноване: перевірка сум, зіставлення назв із товарами бота
(з урахуванням збережених прив'язок «постачальник:код» та назв), розрахунок транспорту.
"""
from __future__ import annotations

import base64
import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP

from . import services as S
from .db import get_db

MODEL = os.getenv("INVOICE_MODEL", "claude-sonnet-4-6")

PROMPT = """Ти витягуєш дані з інвойсу/проформи постачальника продуктів (сири, м'ясні вироби, паста) для обліку закупівлі.
Поверни ЛИШЕ JSON без пояснень і без markdown, за схемою:
{
 "supplier": "назва постачальника",
 "number": "номер документа",
 "date": "YYYY-MM-DD",
 "currency": "EUR",
 "lines": [
   {"code": "артикул або null", "description": "опис як у документі", "pieces": число або null,
    "weight_kg": число (вага в кг, десятковий роздільник крапка) або null,
    "price_per_kg": число або null, "amount": число (сума рядка нетто) або null,
    "lot": "лот/партія або null", "expiry": "YYYY-MM-DD або null"}
 ],
 "transport": число (транспорт/доставка/spese trasporto, 0 якщо немає),
 "other_costs": число (інші нетоварні витрати: упаковка, збори; 0 якщо немає),
 "total_goods": число (сума товарних рядків, як у документі, або null),
 "total_document": число (підсумок до сплати, або null)
}
Правила: транспорт і збори НЕ включай у lines; коми в числах (8,250) — це десятковий роздільник;
якщо ціна вказана за штуку, а не за кг — price_per_kg = amount / weight_kg; якщо ваги немає — weight_kg null.
Не вигадуй значень, яких немає в документі."""


def _client():
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY не задано — додайте ключ у змінні середовища")
    return anthropic.Anthropic(api_key=key)


def extract_invoice(data: bytes, mime: str) -> dict:
    """Надсилає документ моделі, повертає розібраний JSON."""
    client = _client()
    b64 = base64.b64encode(data).decode()
    if mime == "application/pdf":
        block = {"type": "document", "source": {"type": "base64", "media_type": mime, "data": b64}}
    else:
        block = {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}}
    resp = client.messages.create(
        model=MODEL, max_tokens=4000,
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT}]}],
    )
    text = "".join(getattr(p, "text", "") for p in resp.content)
    return parse_model_json(text)


def parse_model_json(text: str) -> dict:
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("Модель не повернула JSON")
    return json.loads(t[start:end + 1])


def _d(v) -> Decimal | None:
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return Decimal(str(v))
    s = str(v).replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return None


def alias_key(supplier: str, code: str | None, description: str) -> str:
    """Ключ прив'язки: код постачальника, якщо є, інакше опис."""
    sup = S.norm_name(supplier or "")[:30]
    return f"inv:{sup}:{code.strip()}" if code else f"inv:{sup}:{S.norm_name(description)}"


def build_draft(parsed: dict) -> dict:
    """Перетворює JSON моделі на чернетку закупівлі з перевірками і зіставленням товарів."""
    db = get_db()
    supplier = (parsed.get("supplier") or "").strip()
    lines = []
    sum_lines = Decimal(0)
    for i, l in enumerate(parsed.get("lines") or []):
        desc = (l.get("description") or "").strip()
        code = (l.get("code") or None)
        kg = _d(l.get("weight_kg"))
        price = _d(l.get("price_per_kg"))
        amount = _d(l.get("amount"))
        if kg and not price and amount:
            price = (amount / kg).quantize(Decimal("0.0001"))
        if kg and price and amount is None:
            amount = (kg * price).quantize(Decimal("0.01"), ROUND_HALF_UP)
        pid = S.get_alias(db, alias_key(supplier, code, desc))
        how = "alias" if pid else None
        if not pid:
            pid, how = S.match_product(db, desc)
        prod = S.get_product(db, pid) if pid else None
        grams = int((kg * 1000).quantize(Decimal("1"))) if kg else None
        issues = []
        if not grams:
            issues.append("немає ваги")
        if not price or price <= 0:
            issues.append("немає ціни")
        if amount is not None and kg and price and abs(amount - (kg * price)) > Decimal("0.05"):
            issues.append(f"сума {amount} ≠ {kg}×{price}")
        lines.append({"i": i, "code": code, "description": desc, "pieces": l.get("pieces"), "grams": grams,
                      "price": str(price) if price else None, "amount": str(amount) if amount is not None else None,
                      "lot": l.get("lot"), "expiry": l.get("expiry"),
                      "product_id": pid, "product_name": prod["name"] if prod else None, "how": how or "none",
                      "issues": issues})
        if amount is not None:
            sum_lines += amount
    transport = _d(parsed.get("transport")) or Decimal(0)
    other = _d(parsed.get("other_costs")) or Decimal(0)
    total_goods = _d(parsed.get("total_goods"))
    total_doc = _d(parsed.get("total_document"))
    checks = []
    if total_goods is not None:
        diff = sum_lines - total_goods
        checks.append(("Сума рядків = «товари» в документі", abs(diff) <= Decimal("0.05"), f"{sum_lines} vs {total_goods}"))
    if total_doc is not None:
        diff = sum_lines + transport + other - total_doc
        checks.append(("Рядки + транспорт = підсумок документа", abs(diff) <= Decimal("0.05"),
                       f"{sum_lines + transport + other} vs {total_doc}"))
    return {"supplier": supplier, "number": parsed.get("number"), "date": parsed.get("date"), "lines": lines,
            "transport": str(transport), "other_costs": str(other), "sum_lines": str(sum_lines),
            "total_goods": str(total_goods) if total_goods is not None else None,
            "total_document": str(total_doc) if total_doc is not None else None, "checks": checks}


def post_draft(draft: dict, user_id: int, doc_date: str | None = None) -> int:
    """Створює закупівлю з чернетки (лише рядки з товаром, вагою і ціною). Зберігає прив'язки."""
    db = get_db()
    plines = []
    for l in draft["lines"]:
        if l.get("skip") or not l["product_id"] or not l["grams"] or not l["price"]:
            continue
        plines.append(S.PurchaseLine(l["product_id"], int(l["grams"]), Decimal(l["price"]), l.get("expiry"),
                                     l.get("lot") or (draft.get("number") or None), l.get("description")))
        S.set_alias(db, alias_key(draft["supplier"], l.get("code"), l["description"]), l["product_id"], user_id)
    if not plines:
        raise ValueError("Немає жодної позиції з товаром, вагою і ціною")
    extra = Decimal(draft["transport"]) + Decimal(draft["other_costs"])
    comment = f"інвойс {draft.get('number') or ''} від {draft.get('date') or ''}".strip()
    return S.create_purchase(db, user_id, doc_date or draft.get("date") or S.today_local(), draft["supplier"] or "постачальник",
                             plines, extra, comment, receive=True)
