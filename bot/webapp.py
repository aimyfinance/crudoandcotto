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
    sh = S.current_shift(db)
    return {
        "shift": {"open": bool(sh), "opened_at": local_dt_str(sh["opened_at"])[-5:] if sh else None, "by": sh["opened_name"] if sh else None},
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


@api
async def tasks_list(body, user):
    db = get_db()
    rows = S.tasks_for(db, user["telegram_id"], user["role"])
    done = S.done_tasks(db, 14, None if user["role"] in ("admin", "manager") else user["telegram_id"])
    return {"done": [{"id": t["id"], "text": t["text"], "by": t["done_name"] or str(t["done_by"]), "at": local_dt_str(t["done_at"]),
                      "status": t["status"]} for t in done],
            "tasks": [{"id": t["id"], "text": t["text"], "due": t["due_date"], "assignee": t["assignee_name"] or ("усім" if t["assignee_id"] is None else str(t["assignee_id"])),
                      "overdue": bool(t["due_date"] and t["due_date"] < today_local())} for t in rows]}


@api
async def task_done(body, user):
    db = get_db()
    t = S.task_done(db, int(body["task_id"]), user["telegram_id"])
    if t and t["created_by"] != user["telegram_id"]:
        bot = request_bot.get("bot")
        if bot:
            try:
                await bot.send_message(t["created_by"], f"✅ {user['name'] or user['telegram_id']} виконав(ла) завдання №{t['id']}: {t['text']}")
            except Exception:
                pass
    rows = S.tasks_for(db, user["telegram_id"], user["role"])
    return {"ok": bool(t), "tasks": [{"id": x["id"], "text": x["text"], "due": x["due_date"], "assignee": x["assignee_name"] or "усім",
                                      "overdue": bool(x["due_date"] and x["due_date"] < today_local())} for x in rows]}


@api
async def shift_toggle(body, user):
    db = get_db()
    import datetime as _dt
    from .config import TZ as _TZ
    now = _dt.datetime.now(_TZ).strftime("%H:%M")
    if body.get("action") == "open":
        sid = S.open_shift(db, user["telegram_id"])
        msg = f"▶️ Каса відкрита о {now} — {user['name'] or user['telegram_id']} (Mini App)" if sid else None
    else:
        s = S.close_shift(db, user["telegram_id"])
        rep = S.report_period(db, today_local(), today_local())
        msg = (f"⏹ Каса закрита о {now} — {user['name'] or user['telegram_id']} (Mini App)\n"
               f"Продажів у боті: {rep['sales_count']} · виручка {rep['revenue']} €") if s else None
    if msg:
        bot = request_bot.get("bot")
        if bot:
            for uid in S.notify_targets(db, "manager"):
                if uid != user["telegram_id"]:
                    try:
                        await bot.send_message(uid, msg)
                    except Exception:
                        pass
    return {"today": _today(db)}


request_bot: dict = {}   # головний модуль кладе сюди екземпляр Bot для сповіщень


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
    app.router.add_post("/api/shift", shift_toggle)
    app.router.add_post("/api/tasks", tasks_list)
    app.router.add_post("/api/tasks/done", task_done)
    return app
