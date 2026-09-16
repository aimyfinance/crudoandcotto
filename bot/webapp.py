"""Telegram Mini App: HTTP-сервер (aiohttp) з JSON API поверх тієї ж бази.

Безпека: кожен запит несе initData від Telegram; підпис перевіряється HMAC-SHA256
за офіційною схемою (secret = HMAC("WebAppData", BOT_TOKEN)), потім користувач
шукається в таблиці users — ті самі ролі, що і в боті.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.parse
import uuid
from decimal import Decimal
from pathlib import Path

from aiohttp import web

from . import services as S
from .config import settings
from .db import get_db, local_dt_str, today_local
from .money import d, line_amount, piece_amount, round_cents

STATIC = Path(__file__).resolve().parent / "static"


# ---------------- авторизація ----------------

def validate_init_data(init_data: str, bot_token: str, max_age: int = 86400) -> dict:
    """Повертає словник полів initData або кидає PermissionError."""
    if not init_data:
        raise PermissionError("Немає initData")
    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise PermissionError("Немає підпису")
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise PermissionError("Невірний підпис")
    if time.time() - int(pairs.get("auth_date", "0")) > max_age:
        raise PermissionError("Сесія застаріла — відкрийте застосунок заново")
    pairs["user"] = json.loads(pairs.get("user", "{}"))
    return pairs


def _auth(request_body: dict):
    data = validate_init_data(request_body.get("initData", ""), settings.bot_token)
    tg_id = int(data["user"]["id"])
    user = S.get_user(get_db(), tg_id)
    if not user:
        raise PermissionError(f"Доступ заборонено. Ваш ID: {tg_id}")
    return user


def _json(payload, status=200):
    return web.json_response(payload, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))


def api(handler):
    async def wrapper(request: web.Request):
        try:
            body = await request.json()
        except Exception:
            return _json({"error": "Некоректний запит"}, 400)
        try:
            user = _auth(body)
        except PermissionError as e:
            return _json({"error": str(e)}, 403)
        try:
            return _json(await handler(body, user))
        except (S.StockError, ValueError) as e:
            return _json({"error": str(e)}, 409)
    return wrapper


# ---------------- дані ----------------

def _products_with_stock(db):
    stock = {r["product"]["id"]: r for r in S.stock_summary(db, include_zero=True)}
    out = []
    for p in S.list_products(db):
        st = stock.get(p["id"])
        out.append({
            "id": p["id"], "name": p["name"], "category": p["category"], "sale_mode": p["sale_mode"],
            "piece_grams": p["piece_grams"], "price": str(d(p["retail_price"])),
            "grams": st["grams"] if st else 0, "expiry": st["nearest_expiry"] if st else None,
        })
    return out


def _today(db):
    t = today_local()
    rep = S.report_period(db, t, t)
    sales = [{"id": s["id"], "time": local_dt_str(s["sold_at"])[-5:], "total": str(d(s["total"])),
              "payment": s["payment_method"], "status": s["status"]} for s in S.recent_sales(db, 30, date=t)]
    return {
        "date": t, "revenue": str(rep["revenue"]), "gross_profit": str(rep["gross_profit"]),
        "sales_count": rep["sales_count"], "sold_grams": rep["sold_grams"],
        "by_payment": {k: str(v) for k, v in rep["by_payment"].items()},
        "top": [{"name": e["name"], "amount": str(e["amount"]), "grams": e["grams"], "margin": float(e["margin_pct"])}
                for e in rep["by_product"][:8]],
        "sales": sales,
        "stock_value": str(rep["stock_value"]), "stock_grams": rep["stock_grams"],
    }


@api
async def bootstrap(body, user):
    db = get_db()
    return {"user": {"id": user["telegram_id"], "name": user["name"], "role": user["role"]},
            "company": settings.company_name, "products": _products_with_stock(db), "today": _today(db),
            "client_key": uuid.uuid4().hex}


@api
async def create_sale(body, user):
    db = get_db()
    lines = []
    for l in body.get("lines", []):
        pieces = int(l["pieces"]) if l.get("pieces") else None
        lines.append(S.SaleLine(int(l["product_id"]), int(l["grams"]), Decimal(str(l["price"])), pieces))
    payment = body.get("payment", "cash")
    if payment not in S.PAYMENTS:
        raise ValueError("Невідомий спосіб оплати")
    key = body.get("client_key")
    try:
        res = S.create_sale(db, user["telegram_id"], lines, payment, client_key=f"app:{key}" if key else None)
    except S.DuplicateOperation as e:
        return {"duplicate": True, "message": str(e), "today": _today(db), "products": _products_with_stock(db)}
    return {"sale_id": res.sale_id, "total": str(res.total), "today": _today(db), "products": _products_with_stock(db),
            "client_key": uuid.uuid4().hex}


@api
async def cancel_sale(body, user):
    db = get_db()
    sid = int(body["sale_id"])
    s, _ = S.get_sale(db, sid)
    if not s:
        raise ValueError("Продаж не знайдено")
    if user["role"] == "seller" and (s["created_by"] != user["telegram_id"] or s["sale_date"] != today_local()):
        raise ValueError("Продавець може скасувати лише свій сьогоднішній продаж")
    S.cancel_sale(db, sid, user["telegram_id"], body.get("reason") or "скасовано в застосунку")
    return {"ok": True, "today": _today(db), "products": _products_with_stock(db)}


@api
async def stock(body, user):
    db = get_db()
    out = []
    for r in S.stock_summary(db):
        p = r["product"]
        batches = [{"id": b["id"], "code": b["batch_code"], "received": b["received_at"][:10], "expiry": b["expiry_date"],
                    "grams": b["grams_left"], "price": str(d(b["landed_price_per_kg"]))}
                   for b in S.batches_of_product(db, p["id"])]
        out.append({"id": p["id"], "name": p["name"], "category": p["category"], "sale_mode": p["sale_mode"],
                    "piece_grams": p["piece_grams"], "grams": r["grams"], "value": str(r["cost_value"]),
                    "expiry": r["nearest_expiry"], "batches": batches})
    return {"items": out, "total_value": str(sum((d(i["value"]) for i in out), Decimal(0)))}


@api
async def sale_detail(body, user):
    db = get_db()
    s, lines = S.get_sale(db, int(body["sale_id"]))
    if not s:
        raise ValueError("Продаж не знайдено")
    return {"id": s["id"], "time": local_dt_str(s["sold_at"]), "payment": s["payment_method"], "status": s["status"],
            "total": str(d(s["total"])), "lines": [{"name": l["product_name"], "grams": l["grams"], "pieces": l["pieces"],
                                                   "price": str(d(l["price"])), "amount": str(d(l["amount"]))} for l in lines]}


@api
async def today(body, user):
    return {"today": _today(get_db()), "products": _products_with_stock(get_db())}


async def index(request: web.Request):
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def health(request):
    return web.Response(text="ok")


def build_app() -> web.Application:
    app = web.Application(client_max_size=1024 * 1024)
    app.router.add_get("/", index)
    app.router.add_get("/app", index)
    app.router.add_get("/health", health)
    app.router.add_post("/api/bootstrap", bootstrap)
    app.router.add_post("/api/sale", create_sale)
    app.router.add_post("/api/sale/cancel", cancel_sale)
    app.router.add_post("/api/sale/detail", sale_detail)
    app.router.add_post("/api/stock", stock)
    app.router.add_post("/api/today", today)
    return app
