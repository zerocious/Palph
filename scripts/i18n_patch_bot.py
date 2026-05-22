"""One-off patches for bot.py i18n wiring."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "bot.py"
t = p.read_text(encoding="utf-8")

replacements = [
    ("await get_subject_keyboard(user_id)", "await get_subject_keyboard(user_id, await loc(user_id))"),
    (
        "get_mode_keyboard_for_subject(subject_id, user_id)",
        "get_mode_keyboard_for_subject(subject_id, user_id, await loc(user_id))",
    ),
    ("await send_rating_prompt(chat_id, session_id)", "await send_rating_prompt(chat_id, session_id, user_id)"),
    (
        "await send_rating_prompt(message.chat.id, session_id)",
        "await send_rating_prompt(message.chat.id, session_id, message.from_user.id)",
    ),
    (
        "async def handle_subject_back_to_study(message: Message, state: FSMContext):\n    await state.clear()",
        "async def handle_subject_back_to_study(message: Message, state: FSMContext):\n    user_id = message.from_user.id\n    locale = await loc(user_id)\n    await state.clear()",
    ),
    (
        'reply_markup=get_study_keyboard(await loc(user_id)))\n\n\n@router.message(QuizStates.choosing_subject',
        'reply_markup=get_study_keyboard(locale))\n\n\n@router.message(QuizStates.choosing_subject',
    ),
    (
        "async def handle_quiz_exit(message: Message, state: FSMContext):\n    await state.clear()",
        "async def handle_quiz_exit(message: Message, state: FSMContext):\n    user_id = message.from_user.id\n    locale = await loc(user_id)\n    await state.clear()",
    ),
    ("F.text.in_([label for label, _ in QUIZ_SECTIONS])", "kb_in('kb.finish_quiz'), F.text.in_(_quiz_section_labels())"),
    ("section_map = {label: key for label, key in QUIZ_SECTIONS}", "section_map = _quiz_section_map(await loc(message.from_user.id))"),
]

for old, new in replacements:
    if old not in t:
        print("MISSING:", old[:70])
    else:
        t = t.replace(old, new)
        print("OK:", old[:50])

p.write_text(t, encoding="utf-8")
print("written")
