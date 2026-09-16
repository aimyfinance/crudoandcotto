"""Octobox (бек-офіс на Odoo): JSON-RPC клієнт, діагностика та синхронізація чеків.

Змінні середовища:
  OCTOBOX_URL       https://cloud1.octobox.net
  OCTOBOX_LOGIN     логін бек-офісу (окремий користувач лише на читання — ідеально)
  OCTOBOX_PASSWORD  пароль
  OCTOBOX_DB        назва бази (необов'язково — спробуємо визначити)
  OCTOBOX_SYNC_MINUTES  інтервал синхронізації, 0 = вимкнено (за замовчуванням 0, вмикається після діагностики)
  OCTOBOX_POS_CONFIG    назва каси у бек-офісі (K01) — необов'язково, фільтр
  OCTOBOX_WEIGHT_FIELD  назва поля ваги в рядку чека, якщо автовизначення не спрацює

Читаємо лише: нічого в касі не змінюємо.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
from decimal import Decimal
from typing import Any

import aiohttp

log = logging.getLogger("crudo.octobox")


class OdooError(Exception):
    pass


class OdooClient:
    def __init__(self, url: str, login: str, password: str, db: str | None = None):
        self.url = url.rstrip("/")
        self.login = login
        self.password = password
        self.db = db
        self.uid: int | None = None
        self.session: aiohttp.ClientSession | None = None
        self.info: dict = {}

    async def __aenter__(self):
        self.session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        return self

    async def __aexit__(self, *a):
        if self.session:
            await self.session.close()

    async def _rpc(self, path: str, params: dict) -> Any:
        payload = {"jsonrpc": "2.0", "method": "call", "params": params, "id": 1}
        async with self.session.post(self.url + path, json=payload) as r:
            if r.status != 200:
                raise OdooError(f"HTTP {r.status} на {path}")
            data = await r.json(content_type=None)
        if "error" in data:
            err = data["error"]
            msg = (err.get("data") or {}).get("message") or err.get("message") or str(err)
            raise OdooError(msg)
        return data.get("result")

    async def databases(self) -> list[str]:
        try:
            return await self._rpc("/web/database/list", {}) or []
        except Exception:
            return []

    async def version(self) -> dict:
        try:
            return await self._rpc("/web/webclient/version_info", {}) or {}
        except Exception:
            return {}

    async def authenticate(self) -> dict:
        if not self.db:
            dbs = await self.databases()
            if len(dbs) == 1:
                self.db = dbs[0]
            elif dbs:
                raise OdooError(f"Кілька баз: {dbs} — вкажіть OCTOBOX_DB")
        res = await self._rpc("/web/session/authenticate", {"db": self.db, "login": self.login, "password": self.password})
        if not res or not res.get("uid"):
            raise OdooError("Авторизація не пройшла (перевірте логін/пароль/базу)")
        self.uid = res["uid"]
        self.info = {"uid": res["uid"], "name": res.get("name"), "db": self.db, "server_version": res.get("server_version")}
        return self.info

    async def call(self, model: str, method: str, args: list | None = None, kwargs: dict | None = None) -> Any:
        return await self._rpc("/web/dataset/call_kw", {"model": model, "method": method, "args": args or [], "kwargs": kwargs or {}})

    async def search_read(self, model: str, domain: list, fields: list[str], limit: int = 200, order: str | None = None) -> list[dict]:
        kw = {"domain": domain, "fields": fields, "limit": limit}
        if order:
            kw["order"] = order
        return await self.call(model, "search_read", [], kw) or []

    async def fields_get(self, model: str) -> dict:
        return await self.call(model, "fields_get", [], {"attributes": ["string", "type", "relation"]}) or {}

    async def model_exists(self, model: str) -> bool:
        try:
            await self.call(model, "search_count", [[]], {})
            return True
        except OdooError:
            return False


# ---------------- діагностика ----------------

WEIGHT_HINTS = ("weight", "gewicht", "peso", "kg", "gram")
LINE_FIELDS_BASE = ["order_id", "product_id", "full_product_name", "qty", "price_subtotal_incl", "price_subtotal", "price_unit", "discount", "refunded_orderline_id"]
ORDER_FIELDS_BASE = ["name", "pos_reference", "date_order", "state", "amount_total", "amount_paid", "config_id", "session_id", "payment_ids", "statement_ids", "lines"]


async def diagnose(client: OdooClient) -> dict:
    """Що доступно: версія, користувач, POS-моделі, поля ваги, приклад останніх чеків."""
    out: dict = {"steps": []}
    out["version"] = await client.version()
    out["auth"] = await client.authenticate()
    out["steps"].append(f"✅ Авторизація: {out['auth'].get('name')} (uid {out['auth']['uid']}), база {client.db}, версія {out['version'].get('server_version') or out['auth'].get('server_version') or '?'}")
    for model in ("pos.order", "pos.order.line", "pos.payment", "pos.config", "product.product"):
        ok = await client.model_exists(model)
        out[model] = ok
        out["steps"].append(f"{'✅' if ok else '❌'} модель {model}")
    if out.get("pos.order.line"):
        fields = await client.fields_get("pos.order.line")
        out["line_fields"] = {k: v.get("string") for k, v in fields.items()}
        weight = [k for k in fields if any(h in k.lower() or h in str(fields[k].get("string", "")).lower() for h in WEIGHT_HINTS)]
        out["weight_candidates"] = weight
        out["steps"].append("⚖️ кандидати поля ваги в рядку чека: " + (", ".join(f"{k} («{fields[k].get('string')}»)" for k in weight) or "не знайдено — вага може бути в qty"))
    SIMPLE = ("float", "integer", "monetary", "char", "text", "boolean", "date", "datetime", "selection")
    if out.get("pos.order.line"):
        try:
            all_fields = await client.fields_get("pos.order.line")
            simple = [k for k, v in all_fields.items() if v.get("type") in SIMPLE and not k.startswith("__")]
            out["line_field_list"] = sorted((k, str(v.get("string", "")), v.get("type")) for k, v in all_fields.items())
            out["steps"].append("🧩 прості поля рядка чека: " + ", ".join(f"{k} «{all_fields[k].get('string')}»" for k in sorted(simple)))
            rel = [k for k, v in all_fields.items() if v.get("type") not in SIMPLE]
            out["steps"].append("🔗 зв'язані поля рядка: " + ", ".join(sorted(rel)))
        except OdooError as e:
            out["steps"].append(f"⚠️ fields_get(pos.order.line): {e}")
            simple = []
        # читаємо останній рядок по частинах — поле, що дає помилку прав, просто пропускаємо
        rec = {}
        for chunk_start in range(0, len(simple), 10):
            chunk = simple[chunk_start:chunk_start + 10]
            try:
                last = await client.search_read("pos.order.line", [], chunk, limit=1, order="id desc")
                if last:
                    rec.update({k: v for k, v in last[0].items() if v not in (None, False, 0, 0.0, "", [])})
            except OdooError:
                for f in chunk:
                    try:
                        last = await client.search_read("pos.order.line", [], [f], limit=1, order="id desc")
                        if last and last[0].get(f) not in (None, False, 0, 0.0, "", []):
                            rec[f] = last[0][f]
                    except OdooError:
                        rec[f] = "⛔ немає прав"
        out["last_line_full"] = rec
        out["steps"].append("🔎 останній рядок (прості поля): " + "; ".join(f"{k}={v}" for k, v in rec.items()))
    if out.get("pos.order"):
        try:
            ofields_all = await client.fields_get("pos.order")
            pay_like = [k for k in ofields_all if any(h in k for h in ("statement", "payment", "journal"))]
            out["steps"].append("💳 поля оплати в чеку: " + (", ".join(pay_like) or "—"))
        except OdooError as e:
            out["steps"].append(f"⚠️ fields_get(pos.order): {e}")
        for m in ("account.bank.statement.line", "pos.payment.method", "account.journal"):
            out[m] = await client.model_exists(m)
            out["steps"].append(f"{'✅' if out[m] else '❌'} модель {m}")
    if out.get("pos.config"):
        try:
            cfgs = await client.search_read("pos.config", [], ["name"], limit=20)
            out["configs"] = [c["name"] for c in cfgs]
            out["steps"].append("🧾 каси: " + ", ".join(out["configs"]))
        except OdooError as e:
            out["steps"].append(f"⚠️ pos.config: {e}")
    if out.get("pos.order"):
      try:
        fields = await client.fields_get("pos.order")
        want = [f for f in ORDER_FIELDS_BASE if f in fields]
        orders = await client.search_read("pos.order", [], want, limit=3, order="date_order desc")
        out["sample_orders"] = orders
        out["steps"].append(f"🧾 останні чеки: " + "; ".join(f"{o.get('pos_reference') or o.get('name')} {o.get('date_order')} {o.get('amount_total')} € ({o.get('state')})" for o in orders))
        if orders and out.get("pos.order.line"):
            lf = await client.fields_get("pos.order.line")
            wantl = [f for f in LINE_FIELDS_BASE if f in lf] + [w for w in out.get("weight_candidates", []) if w in lf]
            wantl = list(dict.fromkeys(wantl + [f for f in ("price_unit", "discount", "pack_lot_ids", "product_id") if f in lf]))
            lines = await client.search_read("pos.order.line", [["order_id", "in", [o["id"] for o in orders]]], wantl, limit=30)
            out["sample_lines"] = lines
            lot_ids = [x for l in lines for x in (l.get("pack_lot_ids") or [])]
            lots = {}
            if lot_ids and await client.model_exists("pos.pack.operation.lot"):
                for lt in await client.search_read("pos.pack.operation.lot", [["id", "in", lot_ids]], ["lot_name", "pos_order_line_id"], limit=100):
                    lots.setdefault((lt.get("pos_order_line_id") or [None])[0], []).append(lt.get("lot_name"))
            prod_ids = list({(l.get("product_id") or [None])[0] for l in lines if l.get("product_id")})
            prices = {}
            if prod_ids:
                try:
                    for pr in await client.search_read("product.product", [["id", "in", prod_ids]], ["name", "lst_price", "uom_id"], limit=100):
                        prices[pr["id"]] = (pr.get("lst_price"), (pr.get("uom_id") or ["", ""])[1])
                except OdooError as e:
                    out["steps"].append(f"⚠️ product.product: {e}")
            out["steps"].append("📄 рядки останніх 3 чеків (товар · qty · сума · price_unit · знижка · лот · ціна товару/од.): " + "; ".join(
                f"{l.get('full_product_name') or (l.get('product_id') or ['', ''])[1]} · {l.get('qty')} · {l.get('price_subtotal_incl')} € · pu={l.get('price_unit')} · d={l.get('discount')}"
                f" · lot={lots.get(l['id'])} · list={prices.get((l.get('product_id') or [None])[0])}" for l in lines))
        if orders and out.get("pos.payment") and orders[0].get("payment_ids"):
            pays = await client.search_read("pos.payment", [["id", "in", orders[0]["payment_ids"]]], ["amount", "payment_method_id"], limit=10)
            out["sample_payments"] = pays
            out["steps"].append("💳 оплати останнього чека: " + "; ".join(f"{(p.get('payment_method_id') or ['', '?'])[1]} {p.get('amount')}" for p in pays))
        if orders and out.get("account.bank.statement.line") and orders[0].get("statement_ids"):
            sts = await client.search_read("account.bank.statement.line", [["id", "in", orders[0]["statement_ids"]]], ["amount", "journal_id"], limit=10)
            out["steps"].append("💳 оплати (виписка) останнього чека: " + "; ".join(f"{(p.get('journal_id') or ['', '?'])[1]} {p.get('amount')}" for p in sts))
      except OdooError as e:
        out["steps"].append(f"⚠️ читання чеків: {e}")
    return out


def diagnose_text(out: dict) -> str:
    return "🔌 <b>Octobox: діагностика</b>\n" + "\n".join(out["steps"])


# ---------------- синхронізація ----------------

def _receipt_from_order(o: dict, lines: list[dict], payments: list[dict], weight_field: str | None, tz) -> dict:
    d = dt.datetime.strptime(o["date_order"][:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=dt.timezone.utc).astimezone(tz).replace(tzinfo=None)
    pay = "cash"
    if payments:
        biggest = max(payments, key=lambda p: abs(float(p.get("amount") or 0)))
        name = str((biggest.get("payment_method_id") or ["", ""])[1]).lower()
        pay = "card" if any(w in name for w in ("kart", "card", "bankomat", "kredit", "sumup", "terminal")) else "cash"
    number = str(o.get("pos_reference") or o.get("name") or o["id"])
    # у Octobox номер чека виглядає як «1452» або «Order 00001-001-0001» — беремо цифри в кінці
    import re as _re
    groups = _re.findall(r"\d+", number)
    digits = groups[-1] if groups else str(o["id"])
    rlines = []
    for l in lines:
        amount = Decimal(str(l.get("price_subtotal_incl") if l.get("price_subtotal_incl") is not None else l.get("price_subtotal") or 0))
        qty = Decimal(str(l.get("qty") or 0))
        grams = 0
        if weight_field and l.get(weight_field) not in (None, False, 0):
            w = Decimal(str(l[weight_field]))
            grams = int(w if w >= 20 else w * 1000)   # >=20 → вже грами, інакше кг
        elif qty and qty != qty.to_integral():
            grams = int(qty * 1000)                   # дробова кількість = кг
        else:
            grams = _weight_from_lots(l.get("_lots") or [])
            if not grams:
                gross = amount
                disc = Decimal(str(l.get("discount") or 0))
                if disc:
                    gross = amount / (1 - disc / 100)   # сума до знижки — щоб ділити на ціну за кг
                per_kg = None
                pu = Decimal(str(l.get("price_unit") or 0))
                lp = Decimal(str(l.get("_list_price") or 0))
                if pu and pu > gross * 2:              # price_unit — це ціна за кг
                    per_kg = pu
                elif lp and lp > gross * 2:            # інакше — прайсова ціна товару за кг
                    per_kg = lp
                if per_kg and amount > 0:
                    grams = int((gross / per_kg * 1000).quantize(Decimal("1")))
        name = l.get("full_product_name") or (l.get("product_id") or ["", ""])[1] or "?"
        rlines.append({"name": str(name).strip(), "group": None, "grams": grams, "amount": amount})
    total = sum((x["amount"] for x in rlines), Decimal(0))
    return {"number": digits, "dt": d, "payment": pay, "refund": total < 0 or str(o.get("state")) == "cancel", "lines": rlines, "odoo_id": o["id"]}


async def fetch_receipts(client: OdooClient, since: dt.datetime, weight_field: str | None, pos_config: str | None, tz) -> list[dict]:
    domain = [["date_order", ">=", since.astimezone(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")],
              ["state", "in", ["paid", "done", "invoiced"]]]
    # каса — за внутрішнім ID (назва «K01» спільна для всіх клієнтів Octobox на одному сервері)
    cfgs = await client.search_read("pos.config", [["name", "=", pos_config]] if pos_config else [], ["name"], limit=10)
    if cfgs:
        domain.append(["config_id", "in", [c["id"] for c in cfgs]])
    ofields = await client.fields_get("pos.order")
    want = [f for f in ORDER_FIELDS_BASE if f in ofields]
    orders = await client.search_read("pos.order", domain, want, limit=2000, order="date_order asc")
    if not orders:
        return []
    lf = await client.fields_get("pos.order.line")
    wantl = list(dict.fromkeys([f for f in LINE_FIELDS_BASE if f in lf] + [f for f in ("pack_lot_ids",) if f in lf]
                               + ([weight_field] if weight_field and weight_field in lf else [])))
    all_lines = await client.search_read("pos.order.line", [["order_id", "in", [o["id"] for o in orders]]], wantl, limit=20000)
    # лоти (касові ваги нерідко пишуть туди грами) і ціни товарів (€/кг) — для обчислення ваги
    lot_ids = [x for l in all_lines for x in (l.get("pack_lot_ids") or [])]
    if lot_ids and await client.model_exists("pos.pack.operation.lot"):
        lots: dict[int, list] = {}
        for lt in await client.search_read("pos.pack.operation.lot", [["id", "in", lot_ids]], ["lot_name", "pos_order_line_id"], limit=20000):
            lots.setdefault((lt.get("pos_order_line_id") or [None])[0], []).append(lt.get("lot_name"))
        for l in all_lines:
            l["_lots"] = lots.get(l["id"], [])
    prod_ids = list({(l.get("product_id") or [None])[0] for l in all_lines if l.get("product_id")})
    if prod_ids:
        try:
            prices = {pr["id"]: pr.get("lst_price") for pr in await client.search_read("product.product", [["id", "in", prod_ids]], ["lst_price"], limit=5000)}
            for l in all_lines:
                l["_list_price"] = prices.get((l.get("product_id") or [None])[0])
        except OdooError:
            pass
    by_order: dict[int, list] = {}
    for l in all_lines:
        by_order.setdefault(l["order_id"][0], []).append(l)
    pay_by_order: dict[int, list] = {}
    pay_ids = [pid for o in orders for pid in (o.get("payment_ids") or [])]
    if pay_ids:
        pays = await client.search_read("pos.payment", [["id", "in", pay_ids]], ["amount", "payment_method_id", "pos_order_id"], limit=20000)
        for p in pays:
            pay_by_order.setdefault(p["pos_order_id"][0], []).append(p)
    else:
        # Odoo 10–12: оплати — рядки банківської виписки чека, спосіб = журнал (Bar / Karte)
        st_ids = [sid for o in orders for sid in (o.get("statement_ids") or [])]
        if st_ids:
            sts = await client.search_read("account.bank.statement.line", [["id", "in", st_ids]], ["amount", "journal_id", "pos_statement_id"], limit=20000)
            for p in sts:
                oid = (p.get("pos_statement_id") or [None])[0]
                if oid:
                    pay_by_order.setdefault(oid, []).append({"amount": p["amount"], "payment_method_id": p.get("journal_id")})
    return [_receipt_from_order(o, by_order.get(o["id"], []), pay_by_order.get(o["id"], []), weight_field, tz) for o in orders]


def _weight_from_lots(lots: list) -> int:
    """Лот виду '110', '0.110', '110 g', '0,11 kg' → грами."""
    import re as _re
    for name in lots:
        m = _re.search(r"(\d+(?:[.,]\d+)?)\s*(kg|g|гр|г)?", str(name or ""), _re.I)
        if not m:
            continue
        v = Decimal(m.group(1).replace(",", "."))
        unit = (m.group(2) or "").lower()
        if unit == "kg" or (not unit and v < 20):
            v *= 1000
        if 0 < v < 100000:
            return int(v)
    return 0


def config() -> dict:
    return {
        "url": os.getenv("OCTOBOX_URL", "").strip(), "login": os.getenv("OCTOBOX_LOGIN", "").strip(),
        "password": os.getenv("OCTOBOX_PASSWORD", ""), "db": os.getenv("OCTOBOX_DB", "").strip() or None,
        "minutes": int(os.getenv("OCTOBOX_SYNC_MINUTES", "0") or 0), "pos_config": os.getenv("OCTOBOX_POS_CONFIG", "").strip() or None,
        "weight_field": os.getenv("OCTOBOX_WEIGHT_FIELD", "").strip() or None,
    }
