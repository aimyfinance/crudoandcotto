"""Прив'язка назв каси до товарів бота у відповідь на запит бота (um:*)."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery

from .. import services as S
from ..keyboards import inline, product_picker
from .common import has_role

router = Router(name="unmapped")


class Unm(StatesGroup):
    pick = State()


async def _bind_and_resync(cb: CallbackQuery, db, user, row, pid: int) -> None:
    from .octobox import run_sync
    S.set_alias(db, row["name"], pid, user["telegram_id"])
    S.unmapped_set(db, row["id"], "bound")
    prod = S.get_product(db, pid)
    await cb.message.answer(f"🔗 «{row['name']}» → <b>{prod['name']}</b>. Дооблікую продажі за останні дні…")
    st = await run_sync(None, user["telegram_id"], since_days=14, notify=False, force=True)
    if st:
        done = st.get("completed_lines", 0)
        await cb.message.answer(f"✅ Додано {done} позицій у {st.get('completed', 0)} чеках." if done else "✅ Прив'язано; нових позицій для дообліку не знайдено.")
    else:
        await cb.message.answer("✅ Прив'язано. Продажі підтягнуться при наступній синхронізації.")


@router.callback_query(F.data.startswith("um:bind:"))
async def um_bind(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    _, _, uid, pid = cb.data.split(":")
    row = S.unmapped_get(db, int(uid))
    if not row or row["status"] != "open":
        return await cb.answer("Уже оброблено", show_alert=True)
    await cb.answer()
    await _bind_and_resync(cb, db, user, row, int(pid))


@router.callback_query(F.data.startswith("um:pick:"))
async def um_pick(cb: CallbackQuery, state: FSMContext, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    uid = int(cb.data.split(":")[2])
    await state.set_state(Unm.pick); await state.update_data(um_id=uid)
    await cb.message.answer(f"Який товар бота відповідає «{S.unmapped_get(db, uid)['name']}»?", reply_markup=product_picker(db, "um", show_price=False))
    await cb.answer()


@router.callback_query(StateFilter(Unm.pick), F.data.startswith("pp:um:"))
async def um_pick_product(cb: CallbackQuery, state: FSMContext, db, user):
    parts = cb.data.split(":")
    if parts[2] in ("cat", "pg"):
        cat = parts[3] if parts[2] == "cat" else (parts[4] or None)
        page = int(parts[3]) if parts[2] == "pg" else 0
        await cb.message.edit_reply_markup(reply_markup=product_picker(db, "um", cat, page, show_price=False))
        return await cb.answer()
    uid = (await state.get_data())["um_id"]; await state.clear()
    row = S.unmapped_get(db, uid)
    await cb.answer()
    await _bind_and_resync(cb, db, user, row, int(parts[3]))


@router.callback_query(F.data.startswith("um:new:"))
async def um_new(cb: CallbackQuery, state: FSMContext, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    from .catalog import Prod, pflow
    uid = int(cb.data.split(":")[2]); row = S.unmapped_get(db, uid)
    await state.clear()
    if db.one("SELECT 1 FROM products WHERE lower(name)=lower(?)", (row["name"],)):
        return await cb.answer("Товар із такою назвою вже є — оберіть його зі списку", show_alert=True)
    await state.update_data(name=row["name"], alias_for=uid)
    await cb.answer()
    await cb.message.answer(f"➕ Новий товар «{row['name']}». Далі — категорія:")
    await pflow.goto(cb.message, state, Prod.category, push=False)


@router.callback_query(F.data.startswith("um:ign:"))
async def um_ign(cb: CallbackQuery, db, user):
    if not has_role(user, "manager"):
        return await cb.answer("Недостатньо прав", show_alert=True)
    uid = int(cb.data.split(":")[2])
    S.unmapped_set(db, uid, "ignored")
    await cb.answer("Ігноруватиму")
    await cb.message.answer(f"🚫 «{S.unmapped_get(db, uid)['name']}» більше не питатиму. Передумаєте — Налаштування → 🔗 Прив'язки.")


@router.callback_query(F.data == "um:list")
async def um_list(cb: CallbackQuery, db, user):
    from .octobox import unmapped_prompt
    rows = S.unmapped_open(db)
    if not rows:
        await cb.answer("Усі назви каси прив'язано ✅", show_alert=True); return
    await cb.answer()
    for row in rows[:10]:
        text, kb = unmapped_prompt(db, row)
        await cb.message.answer(text, reply_markup=inline(kb))
