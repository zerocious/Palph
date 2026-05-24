"""
Sprint exam plan Telegram handlers (v0.9).
Registered from bot.main() after globals are initialized.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from typing import Any, Callable, Awaitable

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from i18n import t, subject_label, quiz_section_label
from plan_service import (
    MIN_PLAN_ITEMS,
    build_content_catalog,
    build_progress_snapshot,
    catalog_has_minimum,
    compute_skill_from_diagnostic,
    count_today_pending,
    generate_sprint_plan,
    get_today_items,
    get_today_pool,
    is_today_complete,
    load_diagnostic,
    mark_item_done,
    plan_progress_summary,
    resolve_diagnostic_question,
)

logger = logging.getLogger("studybuddy_bot")

# Flip to True when re-enabling the exam sprint plan in Telegram UI.
PLAN_UI_ENABLED = False

PLAN_MINUTE_OPTIONS = (60, 120, 180, 240)


class PlanStates(StatesGroup):
    picking_minutes = State()
    diagnostic = State()


def _badge_label(locale: str, badge: str) -> str:
    key = f"plan.badge_{badge}"
    text = t(key, locale)
    return text if text != key else badge


def _mode_icon(mode: str) -> str:
    return {
        "flashcards": "🃏",
        "mcq": "☑️",
        "tasks": "📝",
        "situational": "💬",
    }.get(mode, "📌")


async def _user_progress_snapshot(user_id: int):
    b = _bmod()
    flash_rows = await b.flashcard_repo.list_progress(user_id)
    mcq_rows = await b.mcq_repo.list_progress(user_id)
    task_rows = await b.task_repo.list_progress(user_id)
    quiz_rows: list[dict] = []
    async with b.db.execute(
        "SELECT term_hash, next_review FROM quiz_progress WHERE user_id = ?",
        (user_id,),
    ) as cursor:
        quiz_rows = [dict(row) for row in await cursor.fetchall()]
    return build_progress_snapshot(
        flashcard_rows=flash_rows,
        quiz_rows=quiz_rows,
        mcq_rows=mcq_rows,
        task_rows=task_rows,
    )


async def plan_available(subject_id: str) -> tuple[bool, str]:
    catalog = build_content_catalog(subject_id)
    if not catalog_has_minimum(catalog, MIN_PLAN_ITEMS):
        return False, "no_content"
    if not load_diagnostic(subject_id):
        return False, "no_diagnostic"
    return True, ""


async def build_plan_subject_keyboard(user_id: int, subject_id: str, locale: str):
    kb = InlineKeyboardBuilder()
    active = await _plan_repo().get_active_plan(user_id, subject_id)
    kb.button(
        text=t("plan.btn_today", locale),
        callback_data=f"plan:today:{user_id}:{subject_id}",
    )
    if active:
        kb.button(
            text=t("plan.btn_calendar", locale),
            callback_data=f"plan:cal:{user_id}:{subject_id}",
        )
        kb.button(
            text=t("plan.btn_change", locale),
            callback_data=f"plan:change:{user_id}:{subject_id}",
        )
    else:
        kb.button(
            text=t("plan.btn_start", locale),
            callback_data=f"plan:start:{user_id}:{subject_id}",
        )
    kb.adjust(1)
    return kb.as_markup()


async def maybe_offer_first_plan_prompt(
    message: Message,
    state: FSMContext,
    user_id: int,
    subject_id: str,
    locale: str,
) -> None:
    ok, _ = await plan_available(subject_id)
    if not ok:
        return
    meta = await _plan_repo().get_meta(user_id, subject_id)
    active = await _plan_repo().get_active_plan(user_id, subject_id)
    if active or meta.get("skip_plan_prompt"):
        return
    if meta.get("first_prompt_shown"):
        return
    await _plan_repo().upsert_meta(user_id, subject_id, first_prompt_shown=1)
    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("plan.prompt_start", locale),
        callback_data=f"plan:start:{user_id}:{subject_id}",
    )
    kb.button(
        text=t("plan.prompt_skip", locale),
        callback_data=f"plan:skip_prompt:{user_id}:{subject_id}",
    )
    kb.button(
        text=t("plan.prompt_never", locale),
        callback_data=f"plan:never_prompt:{user_id}:{subject_id}",
    )
    kb.adjust(1)
    await message.answer(
        t("plan.first_visit", locale, subject=subject_label(subject_id, locale)),
        reply_markup=kb.as_markup(),
    )


async def render_today_plan(
    chat_id: int,
    user_id: int,
    subject_id: str,
    locale: str,
    *,
    edit_message_id: int | None = None,
) -> None:
    b = _bmod()
    row = await _plan_repo().get_active_plan(user_id, subject_id)
    if not row:
        text = t("plan.no_plan", locale)
        kb = InlineKeyboardBuilder()
        kb.button(
            text=t("plan.btn_start", locale),
            callback_data=f"plan:start:{user_id}:{subject_id}",
        )
        markup = kb.as_markup()
        await _safe_edit_or_send(
            chat_id, text, edit_message_id=edit_message_id, reply_markup=markup,
        )
        return

    plan = row["plan_json"]
    logical_day = row["logical_day"]
    day_minutes = row["day_minutes"]
    done, total = count_today_pending(plan, logical_day, day_minutes)
    pool = get_today_pool(plan, logical_day, day_minutes)

    lines = [
        t("plan.today_header", locale, day=logical_day, done=done, total=total),
        "",
    ]
    kb = InlineKeyboardBuilder()
    for n, (day, idx, item) in enumerate(pool, start=1):
        if item.get("status") == "done":
            lines.append(f"✅ {n}. {_mode_icon(item['mode'])} {_badge_label(locale, item.get('badge', 'new'))}")
        else:
            lines.append(
                f"{n}. {_mode_icon(item['mode'])} {_badge_label(locale, item.get('badge', 'new'))}"
            )
            kb.button(
                text=f"▶️ {n}",
                callback_data=f"plan:item:{user_id}:{subject_id}:{day}:{idx}",
            )
    if not pool:
        lines.append(t("plan.today_empty", locale))

    kb.button(
        text=t("plan.btn_calendar", locale),
        callback_data=f"plan:cal:{user_id}:{subject_id}",
    )
    kb.adjust(2)
    text = "\n".join(lines)
    await _safe_edit_or_send(
        chat_id,
        text,
        edit_message_id=edit_message_id,
        reply_markup=kb.as_markup(),
        parse_mode="HTML",
    )


async def render_calendar(
    chat_id: int,
    user_id: int,
    subject_id: str,
    locale: str,
    *,
    edit_message_id: int | None = None,
) -> None:
    b = _bmod()
    row = await _plan_repo().get_active_plan(user_id, subject_id)
    if not row:
        await render_today_plan(chat_id, user_id, subject_id, locale, edit_message_id=edit_message_id)
        return
    plan = row["plan_json"]
    logical_day = row["logical_day"]
    done_all, total_all = plan_progress_summary(plan)
    lines = [
        t("plan.calendar_header", locale, done=done_all, total=total_all),
        "",
    ]
    for day_block in plan.get("days") or []:
        day_num = day_block.get("day", 0)
        items = day_block.get("items") or []
        d_done = sum(1 for i in items if i.get("status") == "done")
        marker = "👉" if day_num == logical_day else "  "
        lines.append(f"{marker} {t('plan.day_line', locale, day=day_num, done=d_done, total=len(items))}")

    kb = InlineKeyboardBuilder()
    kb.button(
        text=t("plan.btn_today", locale),
        callback_data=f"plan:today:{user_id}:{subject_id}",
    )
    if logical_day < 14:
        kb.button(
            text=t("plan.btn_next_day", locale),
            callback_data=f"plan:nextday:{user_id}:{subject_id}",
        )
    kb.adjust(1)
    text = "\n".join(lines)
    await _safe_edit_or_send(
        chat_id, text, edit_message_id=edit_message_id, reply_markup=kb.as_markup(),
    )


async def _finalize_plan_item(
    user_id: int,
    state: FSMContext,
    *,
    success: bool = True,
) -> bool:
    """Mark plan item done if in plan mode. Returns True if plan item was handled."""
    b = _bmod()
    data = await state.get_data()
    if not data.get("plan_mode"):
        return False
    if not success:
        return True
    subject_id = data.get("plan_subject_id")
    day = data.get("plan_day")
    idx = data.get("plan_idx")
    chat_id = data.get("plan_chat_id") or user_id
    if subject_id is None or day is None or idx is None:
        return True
    row = await _plan_repo().get_active_plan(user_id, subject_id)
    if not row:
        await state.update_data(plan_mode=False)
        return True
    plan = mark_item_done(row["plan_json"], int(day), int(idx))
    await _plan_repo().update_plan_json(user_id, subject_id, plan)
    await b.event_repo.log(
        user_id,
        "plan_item_completed",
        {"subject_id": subject_id, "day": day, "idx": idx},
        subject_id=subject_id,
    )
    locale = await b.loc(user_id)
    day_complete = is_today_complete(plan, row["logical_day"], row["day_minutes"])
    if day_complete:
        await b.event_repo.log(
            user_id,
            "plan_day_completed",
            {"subject_id": subject_id, "logical_day": row["logical_day"]},
            subject_id=subject_id,
        )
        await _tg_bot().send_message(chat_id, t("plan.day_complete", locale))
    await state.clear()
    await render_today_plan(chat_id, user_id, subject_id, locale)
    return True


async def _send_minute_picker(chat_id: int, user_id: int, subject_id: str, locale: str) -> None:
    kb = InlineKeyboardBuilder()
    for minutes in PLAN_MINUTE_OPTIONS:
        kb.button(
            text=t(f"plan.minutes_{minutes}", locale),
            callback_data=f"plan:minutes:{user_id}:{subject_id}:{minutes}",
        )
    kb.adjust(2)
    await _tg_bot().send_message(chat_id, t("plan.pick_minutes", locale), reply_markup=kb.as_markup())


async def _start_diagnostic(
    chat_id: int,
    user_id: int,
    subject_id: str,
    day_minutes: int,
    state: FSMContext,
    locale: str,
) -> None:
    b = _bmod()
    questions = load_diagnostic(subject_id)
    if not questions:
        await _tg_bot().send_message(chat_id, t("plan.no_diagnostic", locale))
        return
    await state.set_state(PlanStates.diagnostic)
    await state.update_data(
        plan_subject_id=subject_id,
        plan_day_minutes=day_minutes,
        plan_diag_questions=questions,
        plan_diag_index=0,
        plan_diag_answers=[],
    )
    await _send_diagnostic_question(chat_id, user_id, state, locale)


async def _send_diagnostic_question(
    chat_id: int,
    user_id: int,
    state: FSMContext,
    locale: str,
) -> None:
    b = _bmod()
    data = await state.get_data()
    questions = data.get("plan_diag_questions") or []
    idx = data.get("plan_diag_index", 0)
    if idx >= len(questions):
        await _finish_diagnostic(chat_id, user_id, state, locale)
        return
    q = questions[idx]
    kb = InlineKeyboardBuilder()
    if q.get("mode") == "mcq" and q.get("options"):
        for i, opt in enumerate(q["options"]):
            kb.button(text=opt, callback_data=f"plan:diag:{user_id}:{i}")
        kb.adjust(1)
    else:
        kb.button(text=t("plan.diag_know", locale), callback_data=f"plan:diag:{user_id}:1")
        kb.button(text=t("plan.diag_unknown", locale), callback_data=f"plan:diag:{user_id}:0")
        kb.adjust(2)
    prompt = q.get("prompt") or q.get("question") or t("plan.diag_default_q", locale)
    await _tg_bot().send_message(
        chat_id,
        t("plan.diag_progress", locale, current=idx + 1, total=len(questions))
        + "\n\n"
        + prompt,
        reply_markup=kb.as_markup(),
    )


async def _finish_diagnostic(
    chat_id: int,
    user_id: int,
    state: FSMContext,
    locale: str,
) -> None:
    b = _bmod()
    data = await state.get_data()
    subject_id = data.get("plan_subject_id")
    day_minutes = data.get("plan_day_minutes", 60)
    questions = data.get("plan_diag_questions") or []
    answers = data.get("plan_diag_answers") or []
    skills = compute_skill_from_diagnostic(questions, answers)
    await _plan_repo().bulk_upsert_skills(user_id, subject_id, skills)
    await _plan_repo().upsert_meta(user_id, subject_id, diagnostic_done=1)

    progress = await _user_progress_snapshot(user_id)
    skill_map = await _plan_repo().get_skill_map(user_id, subject_id)
    catalog = build_content_catalog(subject_id)
    plan = generate_sprint_plan(catalog, skill_map, progress, day_minutes, subject_id)
    await _plan_repo().save_plan(user_id, subject_id, plan, day_minutes, logical_day=1)
    await b.event_repo.log(
        user_id,
        "plan_started",
        {"subject_id": subject_id, "day_minutes": day_minutes, "items_count": sum(
            len(d.get("items") or []) for d in plan.get("days") or []
        )},
        subject_id=subject_id,
    )
    await state.clear()
    await _tg_bot().send_message(chat_id, t("plan.created", locale, days=14))
    await render_today_plan(chat_id, user_id, subject_id, locale)


async def _launch_plan_item(
    callback: CallbackQuery,
    state: FSMContext,
    user_id: int,
    subject_id: str,
    day: int,
    idx: int,
    locale: str,
) -> None:
    b = _bmod()
    row = await _plan_repo().get_active_plan(user_id, subject_id)
    if not row:
        await callback.answer(t("plan.no_plan", locale), show_alert=True)
        return
    plan = row["plan_json"]
    item = None
    for day_block in plan.get("days") or []:
        if day_block.get("day") == day:
            items = day_block.get("items") or []
            if 0 <= idx < len(items):
                item = items[idx]
            break
    if not item or item.get("status") == "done":
        await callback.answer(t("plan.item_done", locale), show_alert=False)
        return

    mode = item["mode"]
    ref = item["ref"]
    section = item.get("section")
    subject_lbl = subject_label(subject_id, locale)

    await state.update_data(
        subject_id=subject_id,
        subject_label=subject_lbl,
        plan_mode=True,
        plan_subject_id=subject_id,
        plan_day=day,
        plan_idx=idx,
        plan_chat_id=callback.message.chat.id,
    )
    await callback.answer()

    if mode == "mcq":
        questions = b.load_mcq(subject_id)
        target = next((q for q in questions if b._mcq_hash(q["question"]) == ref), None)
        if not target:
            await callback.message.answer(t("plan.item_missing", locale))
            return
        await state.update_data(
            mcq_questions=[target],
            mcq_index=0,
            mcq_correct_count=0,
            mcq_user_id=user_id,
            plan_single=True,
        )
        await state.set_state(b.QuizStates.answering_mcq)
        await callback.message.answer(t("plan.item_start", locale))
        await b._send_next_mcq_question(callback.message.chat.id, state)
        return

    if mode == "flashcards":
        cards = b.load_flashcards(subject_id)
        card = next((c for c in cards if c["hash"] == ref), None)
        if not card:
            await callback.message.answer(t("plan.item_missing", locale))
            return
        cards_by_hash = {card["hash"]: card}
        await state.update_data(
            flash_cards_by_hash=cards_by_hash,
            flash_candidate_hashes=[ref],
            flash_reviewed_count=0,
            flash_coins_earned=0,
            flash_user_id=user_id,
            flash_subject_id=subject_id,
            flash_subject_label=subject_lbl,
            flash_current_hash=ref,
            plan_single=True,
        )
        await state.set_state(b.QuizStates.answering_flash)
        await b._send_flashcard(callback.message.chat.id, state, ref)
        return

    if mode == "tasks":
        tasks = b.load_tasks(subject_id)
        task = next((tk for tk in tasks if tk["id"] == ref), None)
        if not task:
            await callback.message.answer(t("plan.item_missing", locale))
            return
        await state.update_data(
            task_questions=[task],
            task_index=0,
            task_attempts=0,
            task_correct_count=0,
            task_coins_earned=0,
            task_user_id=user_id,
            task_subject_id=subject_id,
            task_subject_label=subject_lbl,
            plan_single=True,
        )
        await state.set_state(b.QuizStates.answering_task)
        await callback.message.answer(t("plan.item_start", locale))
        await b._send_next_task(callback.message.chat.id, state)
        return

    if mode == "situational":
        if not section:
            await callback.message.answer(t("plan.item_missing", locale))
            return
        terms = b.load_quiz_section(section, subject_id)
        term = next((tm for tm in terms if tm.hash == ref), None)
        if not term:
            await callback.message.answer(t("plan.item_missing", locale))
            return
        section_name = quiz_section_label(section, locale)
        await state.update_data(
            current_term=term.to_dict(),
            section=section,
            section_name=section_name,
            plan_mode=True,
            plan_single=True,
        )
        await state.set_state(b.QuizStates.answering)
        await callback.message.answer(
            t("quiz.answer_prompt", locale, section=section_name, term=term.term),
            reply_markup=b.get_quiz_answer_keyboard(locale),
        )
        return


_plan_repo_ref = None
_bot_ref = None
_bmod_ref = None


def _plan_repo():
    if _plan_repo_ref is None:
        raise RuntimeError("plan repo not initialized")
    return _plan_repo_ref


def _tg_bot():
    if _bot_ref is None:
        raise RuntimeError("telegram bot not initialized")
    return _bot_ref


def _bmod():
    """Running module (__main__ when started via python bot.py)."""
    if _bmod_ref is None:
        raise RuntimeError("bot module not initialized")
    return _bmod_ref


async def _safe_edit_or_send(
    chat_id: int,
    text: str,
    *,
    edit_message_id: int | None = None,
    reply_markup=None,
    parse_mode: str | None = None,
) -> None:
    """Edit inline plan UI; fall back to a new message if edit fails."""
    tg = _tg_bot()
    kwargs = {"reply_markup": reply_markup}
    if parse_mode:
        kwargs["parse_mode"] = parse_mode
    if edit_message_id:
        try:
            await tg.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=edit_message_id,
                **kwargs,
            )
            return
        except TelegramBadRequest:
            pass
    await tg.send_message(chat_id, text, **kwargs)


def register_plan_handlers(
    router: Router,
    *,
    plan_repo,
    loc_fn: Callable[[int], Awaitable[str]],
    bot_instance,
    bot_module,
) -> None:
    global _plan_repo_ref, _bot_ref, _bmod_ref
    _plan_repo_ref = plan_repo
    _bot_ref = bot_instance
    _bmod_ref = bot_module

    @router.callback_query(F.data.startswith("plan:"))
    async def handle_plan_callback(callback: CallbackQuery, state: FSMContext):
        b = _bmod()
        parts = callback.data.split(":")
        if len(parts) < 3:
            await callback.answer()
            return
        action = parts[1]
        try:
            user_id = int(parts[2])
        except ValueError:
            await callback.answer()
            return
        if callback.from_user.id != user_id:
            await callback.answer(t("common.not_yours_session", await loc_fn(user_id)), show_alert=True)
            return
        locale = await loc_fn(user_id)

        if action == "today":
            subject_id = parts[3]
            await render_today_plan(
                callback.message.chat.id,
                user_id,
                subject_id,
                locale,
                edit_message_id=callback.message.message_id,
            )
            await callback.answer()
            return

        if action == "cal":
            subject_id = parts[3]
            await render_calendar(
                callback.message.chat.id,
                user_id,
                subject_id,
                locale,
                edit_message_id=callback.message.message_id,
            )
            await callback.answer()
            return

        if action == "start" or action == "change":
            subject_id = parts[3]
            ok, reason = await plan_available(subject_id)
            if not ok:
                msg = t("plan.no_content", locale) if reason == "no_content" else t("plan.no_diagnostic", locale)
                await callback.answer(msg, show_alert=True)
                return
            if action == "change":
                await _plan_repo().delete_plan(user_id, subject_id)
                await b.event_repo.log(
                    user_id,
                    "plan_regenerated",
                    {"subject_id": subject_id},
                    subject_id=subject_id,
                )
            await _send_minute_picker(callback.message.chat.id, user_id, subject_id, locale)
            await callback.answer()
            return

        if action == "minutes":
            if len(parts) < 5:
                await callback.answer()
                return
            subject_id = parts[3]
            try:
                minutes = int(parts[4])
            except ValueError:
                await callback.answer()
                return
            if minutes not in PLAN_MINUTE_OPTIONS:
                await callback.answer()
                return
            await _start_diagnostic(
                callback.message.chat.id,
                user_id,
                subject_id,
                minutes,
                state,
                locale,
            )
            await callback.answer()
            return

        if action == "skip_prompt":
            subject_id = parts[3]
            await _plan_repo().upsert_meta(user_id, subject_id, first_prompt_shown=1)
            await callback.answer(t("plan.prompt_skipped", locale))
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        if action == "never_prompt":
            subject_id = parts[3]
            await _plan_repo().upsert_meta(
                user_id, subject_id, first_prompt_shown=1, skip_plan_prompt=1,
            )
            await callback.answer()
            await callback.message.edit_reply_markup(reply_markup=None)
            return

        if action == "nextday":
            subject_id = parts[3]
            row = await _plan_repo().get_active_plan(user_id, subject_id)
            if row and row["logical_day"] < 14:
                await _plan_repo().set_logical_day(
                    user_id, subject_id, row["logical_day"] + 1,
                )
            await render_calendar(
                callback.message.chat.id,
                user_id,
                subject_id,
                locale,
                edit_message_id=callback.message.message_id,
            )
            await callback.answer()
            return

        if action == "item":
            subject_id = parts[3]
            day = int(parts[4])
            idx = int(parts[5])
            await _launch_plan_item(callback, state, user_id, subject_id, day, idx, locale)
            return

        if action == "diag":
            data = await state.get_data()
            if await state.get_state() != PlanStates.diagnostic.state:
                await callback.answer()
                return
            questions = data.get("plan_diag_questions") or []
            q_idx = data.get("plan_diag_index", 0)
            if q_idx >= len(questions):
                await callback.answer()
                return
            q = questions[q_idx]
            try:
                choice = int(parts[3])
            except ValueError:
                await callback.answer()
                return
            if q.get("mode") == "mcq" and q.get("options"):
                correct_idx = q.get("correct_index", 0)
                is_correct = choice == correct_idx
            else:
                is_correct = bool(choice)
            answers = list(data.get("plan_diag_answers") or [])
            answers.append(is_correct)
            await state.update_data(
                plan_diag_answers=answers,
                plan_diag_index=q_idx + 1,
            )
            try:
                await callback.message.edit_reply_markup(reply_markup=None)
            except Exception:
                pass
            await callback.answer(t("plan.diag_saved", locale), show_alert=False)
            await asyncio.sleep(0.3)
            await _send_diagnostic_question(
                callback.message.chat.id, user_id, state, locale,
            )
            return

        await callback.answer()


async def on_plan_activity_complete(user_id: int, state: FSMContext, *, success: bool = True) -> bool:
    """Call from study handlers when a plan-linked item finishes."""
    return await _finalize_plan_item(user_id, state, success=success)


async def return_to_plan_without_complete(
    chat_id: int,
    user_id: int,
    state: FSMContext,
    locale: str,
    *,
    message: str | None = None,
) -> None:
    """Exit a plan-linked study mode without marking the item done."""
    b = _bmod()
    data = await state.get_data()
    subject_id = data.get("plan_subject_id") or data.get("subject_id")
    await state.clear()
    if message:
        await _tg_bot().send_message(chat_id, message)
    if subject_id:
        await render_today_plan(chat_id, user_id, subject_id, locale)
