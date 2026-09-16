from __future__ import annotations

import datetime as dt
import re
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, F, Router
from aiogram.filters import Command, CommandStart, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, TelegramObject, Update

from .. import services as S
from ..config import settings
from ..db import get_db, today_local
from ..keyboards import BACK, CANCEL, G_CASH, G_EXP, G_HOME, G_PURCH, G_REP, G_STOCK, GROUPS, main_menu, submenu

router = Router(name="common")

ROLE_RANK = {"seller": 1, "manager": 2, "admin": 3}


def has_role(user, minimum: str) -> bool:
    return ROLE_RANK.get(user["role"], 0) >= ROLE_RANK[minimum]


# ---------------- middlewares ----------------

class AuthMiddleware(BaseMiddleware):
    """Пропускає лише користувачів із таблиці users. Іншим — повідомлення з їх ID."""

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        tg_user = data.get("event_from_user")
        if tg_user is None:
            return None
        db = get_db()
        user = S.get_user(db, tg_user.id)
        if not user:
            if isinstance(event, Message):
                await event.answer(
                    f"⛔️ Доступ заборонено.\nВаш Telegram ID: <code>{tg_user.id}</code>\n"
                    "Передайте його адміністратору, щоб вас додали."
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Доступ заборонено", show_alert=True)
            return None
        if not user["name"] and tg_user.full_name:
            S.touch_user_name(db, tg_user.id, tg_user.full_name)
        data["user"] = user
        data["db"] = db
        return await handler(event, data)


class DedupMiddleware(BaseMiddleware):
    """Повторна доставка того самого update (мережеві збої, перезапуск) ігнорується."""

    async def __call__(self, handler, event: Update, data):
        key = f"upd:{event.update_id}"
        if not S.claim_key(get_db(), key):
            return None
        return await handler(event, data)


# ---------------- Flow: покрокові сценарії з «Назад» ----------------

def _key(st) -> str:
    if hasattr(st, "state"):
        return st.state
    s = str(st)
    if s.startswith("<State '"):
        return s[8:-2]
    return s


class Flow:
    """Реєстр підказок для станів; підтримує стек для кнопки «Назад».

    prompts: {state_str: async fn(msg, state)} — функція, що показує запитання кроку.
    """

    def __init__(self, prompts: dict):
        # ключі нормалізуємо до рядка стану 'Group:name' (State або str)
        self.prompts = {_key(k): v for k, v in prompts.items()}

    async def goto(self, msg: Message, state: FSMContext, new_state, push: bool = True) -> None:
        cur = await state.get_state()
        data = await state.get_data()
        stack = list(data.get("_stack", []))
        if push and cur and cur != _key(new_state) and cur in self.prompts:
            stack.append(cur)
        await state.update_data(_stack=stack)
        await state.set_state(new_state)
        await self.prompts[_key(new_state)](msg, state)

    async def back(self, msg: Message, state: FSMContext, user) -> None:
        data = await state.get_data()
        stack = list(data.get("_stack", []))
        if not stack:
            await cancel_to_menu(msg, state, user)
            return
        prev = stack.pop()
        await state.update_data(_stack=stack)
        await state.set_state(prev)
        await self.prompts[prev](msg, state)


async def cancel_to_menu(msg: Message, state: FSMContext, user, text: str = "Скасовано.") -> None:
    await state.clear()
    await msg.answer(text, reply_markup=main_menu(user["role"]))


def role_menu_text(user) -> str:
    return f"Головне меню · {S.ROLES[user['role']]}"


# ---------------- дати ----------------

def parse_date(text: str) -> str | None:
    t = (text or "").strip()
    for fmt in ("%d.%m.%Y", "%d.%m.%y", "%Y-%m-%d", "%d/%m/%Y", "%d.%m"):
        try:
            v = dt.datetime.strptime(t, fmt).date()
            if fmt == "%d.%m":
                v = v.replace(year=dt.date.today().year)
            return v.isoformat()
        except ValueError:
            continue
    return None


def parse_period(text: str) -> tuple[str, str] | None:
    parts = re.split(r"\s*[-–—]\s*|\s+", (text or "").strip())
    if len(parts) == 1:
        d1 = parse_date(parts[0])
        return (d1, d1) if d1 else None
    if len(parts) >= 2:
        d1, d2 = parse_date(parts[0]), parse_date(parts[-1])
        if d1 and d2:
            return (min(d1, d2), max(d1, d2))
    return None


def ua_date(iso: str | None) -> str:
    if not iso:
        return "—"
    y, m, d_ = iso[:10].split("-")
    return f"{d_}.{m}.{y}"


# ---------------- базові команди ----------------

@router.message(CommandStart())
@router.message(Command("menu"))
async def cmd_start(msg: Message, state: FSMContext, user):
    await state.clear()
    await msg.answer(
        f"Вітаю, {user['name'] or 'колего'}! Це облік {settings.company_name}.\n{role_menu_text(user)}",
        reply_markup=main_menu(user["role"]),
    )


@router.message(Command("id"))
async def cmd_id(msg: Message):
    await msg.answer(f"Ваш Telegram ID: <code>{msg.from_user.id}</code>")


@router.message(Command("help"))
async def cmd_help(msg: Message, user):
    await msg.answer(
        "Команди: /menu — головне меню, /id — ваш Telegram ID, /backup — резервна копія (адмін).\n"
        "У кожному сценарії є кнопки «◀️ Назад» і «❌ Скасувати».\n"
        f"Дати вводьте як 15.09.2026, вагу — як 250 (г) або 0,25 / 1,2 кг. Сьогодні: {ua_date(today_local())}."
    )


@router.message(StateFilter("*"), F.text == CANCEL)
async def cancel_any(msg: Message, state: FSMContext, user):
    await cancel_to_menu(msg, state, user)


GROUP_TITLES = {G_CASH: "🧾 Каса", G_PURCH: "📦 Закупівлі", G_STOCK: "📊 Склад", G_EXP: "💸 Витрати", G_REP: "📈 Звіти"}


@router.message(F.text.in_(GROUPS))
async def open_group(msg: Message, state: FSMContext, user):
    await state.clear()
    if msg.text == G_HOME:
        return await msg.answer(role_menu_text(user), reply_markup=main_menu(user["role"]))
    kb = submenu(msg.text, user["role"])
    if kb is None:
        return await msg.answer("Цей розділ недоступний для вашої ролі.", reply_markup=main_menu(user["role"]))
    hint = ""
    if msg.text == G_CASH:
        from .. import services as _S
        from ..db import get_db as _g
        sh = _S.current_shift(_g())
        from ..db import local_dt_str as _l
        hint = f"\nЗміна відкрита о {_l(sh['opened_at'])[-5:]} · {sh['opened_name'] or ''}" if sh else "\nЗміна не відкрита"
    await msg.answer(GROUP_TITLES[msg.text] + hint, reply_markup=kb)


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "menu")
async def cb_menu(cb: CallbackQuery, state: FSMContext, user):
    await state.clear()
    await cb.message.answer(role_menu_text(user), reply_markup=main_menu(user["role"]))
    await cb.answer()
