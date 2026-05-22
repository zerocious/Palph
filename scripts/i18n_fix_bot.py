"""Apply i18n bugfixes to bot.py."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "bot.py"
t = p.read_text(encoding="utf-8")

replacements = [
    # cmd_start bilingual picker
    (
        't("lang.picker_title", "ru"),',
        't("lang.picker_title_bilingual", "ru"),',
    ),
    # critical user_id bugs
    (
        """@router.message(UserTaskImportStates.waiting_for_file, Command("cancel"))
async def handle_ut_import_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Импорт отменён.", reply_markup=get_main_keyboard(await loc(user_id)))""",
        """@router.message(UserTaskImportStates.waiting_for_file, Command("cancel"))
async def handle_ut_import_cancel(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("user_tasks.import_cancelled", locale),
        reply_markup=get_main_keyboard(locale),
    )""",
    ),
    (
        """@router.message(SettingsStates.waiting_for_time, Command("cancel"))
async def cancel_time_input(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=get_main_keyboard(await loc(user_id)))""",
        """@router.message(SettingsStates.waiting_for_time, Command("cancel"))
async def cancel_time_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    await state.clear()
    await message.answer(
        t("common.cancelled", locale),
        reply_markup=get_main_keyboard(locale),
    )""",
    ),
    (
        """@router.message(SettingsStates.waiting_for_time)
async def process_time_input(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    match = TIME_RE.match(text)
    if not match:
        await message.answer(
            "❌ Неверный формат. Введи время как ЧЧ:ММ, например 09:30.\\n"
            "Для отмены отправь /cancel."
        )
        return""",
        """@router.message(SettingsStates.waiting_for_time)
async def process_time_input(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    text = (message.text or "").strip()
    match = TIME_RE.match(text)
    if not match:
        await message.answer(t("settings.time_invalid", locale))
        return""",
    ),
    (
        """    if slot not in ("morning", "evening"):
        await state.clear()
        await message.answer("Ошибка состояния, попробуй ещё раз.", reply_markup=get_main_keyboard(await loc(user_id)))
        return

    user_id = message.from_user.id""",
        """    if slot not in ("morning", "evening"):
        await state.clear()
        await message.answer(
            t("errors.state_error", locale),
            reply_markup=get_main_keyboard(locale),
        )
        return""",
    ),
    (
        """    label = "утреннее" if slot == "morning" else "вечернее"
    await message.answer(
        f"✅ Новое {label} время сохранено: {normalized}",
        reply_markup=get_main_keyboard(await loc(user_id)),
    )""",
        """    slot_label = t(
        "settings.slot_morning" if slot == "morning" else "settings.slot_evening",
        locale,
    )
    await message.answer(
        t("settings.time_saved", locale, slot=slot_label, time=normalized),
        reply_markup=get_main_keyboard(locale),
    )""",
    ),
    # user task import instruction
    (
        """    await callback.message.answer(
        USER_TASK_TXT_INSTRUCTION,
        parse_mode="HTML",
    )""",
        """    locale = await loc(user_id)
    await callback.message.answer(
        t("user_tasks.instruction", locale),
        parse_mode="HTML",
    )""",
    ),
    # friends invite
    (
        'await message.answer(\n            "⏳ Ссылка-приглашение недействительна или истекла."\n        )',
        'locale = await loc(invitee_id)\n        await message.answer(t("friends.invite_invalid", locale))',
    ),
    (
        """        await message.answer(
            f"🎉 Ты добавлен в друзья к пользователю "
            f"<code>{creator_id}</code>!",
            parse_mode="HTML",
        )""",
        """        locale = await loc(invitee_id)
        await message.answer(
            t("friends.invite_accepted", locale, creator_id=creator_id),
            parse_mode="HTML",
        )""",
    ),
    (
        """        await message.answer("👥 Вы уже друзья.")""",
        """        await message.answer(t("friends.already_friends", await loc(invitee_id)))""",
    ),
    (
        """        await message.answer(
            "🙂 Это твоя собственная ссылка — отправь её другим пользователям."
        )""",
        """        await message.answer(t("friends.own_link", await loc(invitee_id)))""",
    ),
    # timer natural finish
    (
        """            user = await user_repo.get_user(user_id)
            response = (
                f"🎉 Таймер завершён!\\n"
                f"⏱️ Сессия: {duration} минут\\n"
                f"🪙 Получено: {duration} монет"
            )
            if bonus > 0:
                response += f"\\n✨ Бонус за достижения: +{bonus} монет"
            response += f"\\n📊 Всего монет: {user['total_coins']}"
            try:
                await bot.send_message(chat_id, response, reply_markup=get_main_keyboard(await loc(user_id)))""",
        """            user = await user_repo.get_user(user_id)
            locale = await loc(user_id)
            response = t("timer.finished", locale, duration=duration)
            if bonus > 0:
                response += t("timer.bonus", locale, bonus=bonus)
            response += t("timer.total_coins", locale, total_coins=user["total_coins"])
            try:
                await bot.send_message(chat_id, response, reply_markup=get_main_keyboard(locale))""",
    ),
    # mcq
    (
        """async def handle_mcq_stop(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = data.get("mcq_correct_count", 0)
    answered = data.get("mcq_index", 0)
    total = len(data.get("mcq_questions", []))
    logger.info(
        "mcq.session.stop user_id=%s subject=%s answered=%s/%s correct=%s",
        message.from_user.id, data.get("subject_id"), answered, total, correct,
    )
    await message.answer(
        f"⏹ MCQ остановлен.\\n"
        f"Отвечено: {answered}/{total} (правильных: {correct})\\n"
        f"🪙 Получено: {correct} монет",
        reply_markup=get_study_keyboard(await loc(user_id)),
    )""",
        """async def handle_mcq_stop(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    data = await state.get_data()
    correct = data.get("mcq_correct_count", 0)
    answered = data.get("mcq_index", 0)
    total = len(data.get("mcq_questions", []))
    logger.info(
        "mcq.session.stop user_id=%s subject=%s answered=%s/%s correct=%s",
        user_id, data.get("subject_id"), answered, total, correct,
    )
    await message.answer(
        t("mcq.stopped", locale, answered=answered, total=total, correct=correct),
        reply_markup=get_study_keyboard(locale),
    )""",
    ),
    # task finish/stop
    (
        """    await bot.send_message(
        chat_id,
        f"🎉 Готово! {subject_label}\\n"
        f"Решено: {correct} из {total}\\n"
        f"🪙 Заработано: {coins} монет",
        reply_markup=get_study_keyboard(await loc(user_id)),
    )""",
        """    uid = data.get("task_user_id", chat_id)
    locale = await loc(uid)
    await bot.send_message(
        chat_id,
        t("task.done", locale, subject_label=subject_label, correct=correct, total=total, coins=coins),
        reply_markup=get_study_keyboard(locale),
    )""",
    ),
    (
        """async def handle_task_stop(message: Message, state: FSMContext):
    data = await state.get_data()
    correct = data.get("task_correct_count", 0)
    coins = data.get("task_coins_earned", 0)
    idx = data.get("task_index", 0)
    total = len(data.get("task_questions", []))
    logger.info(
        "task.session.stop user_id=%s subject=%s answered=%s/%s correct=%s coins=%s",
        message.from_user.id, data.get("task_subject_id"),
        idx, total, correct, coins,
    )
    await message.answer(
        f"⏹ Задачи остановлены.\\n"
        f"Решено: {idx}/{total} (правильных: {correct})\\n"
        f"🪙 Получено: {coins} монет",
        reply_markup=get_study_keyboard(await loc(user_id)),
    )""",
        """async def handle_task_stop(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    data = await state.get_data()
    correct = data.get("task_correct_count", 0)
    coins = data.get("task_coins_earned", 0)
    idx = data.get("task_index", 0)
    total = len(data.get("task_questions", []))
    logger.info(
        "task.session.stop user_id=%s subject=%s answered=%s/%s correct=%s coins=%s",
        user_id, data.get("task_subject_id"),
        idx, total, correct, coins,
    )
    await message.answer(
        t("task.stopped", locale, idx=idx, total=total, correct=correct, coins=coins),
        reply_markup=get_study_keyboard(locale),
    )""",
    ),
    # flash finish/stop
    (
        """    if reviewed == 0:
        msg = "🎉 Все карточки уже проработаны. Возвращайся позже!"
    else:
        msg = (
            f"🎉 Сессия завершена!\\n{subject_label}\\n"
            f"Просмотрено карточек: {reviewed}\\n"
            f"🪙 Заработано: {coins} монет"
        )
    await bot.send_message(chat_id, msg, reply_markup=get_study_keyboard(await loc(user_id)))""",
        """    uid = data.get("flash_user_id", chat_id)
    locale = await loc(uid)
    if reviewed == 0:
        msg = t("flash.all_reviewed", locale)
    else:
        msg = t(
            "flash.session_done", locale,
            subject_label=subject_label, reviewed=reviewed, coins=coins,
        )
    await bot.send_message(chat_id, msg, reply_markup=get_study_keyboard(locale))""",
    ),
    (
        """async def handle_flashcard_stop(message: Message, state: FSMContext):
    data = await state.get_data()
    reviewed = data.get("flash_reviewed_count", 0)
    coins = data.get("flash_coins_earned", 0)
    logger.info(
        "flash.session.stop user_id=%s subject=%s reviewed=%s coins=%s",
        message.from_user.id, data.get("flash_subject_id"), reviewed, coins,
    )
    await message.answer(
        f"⏹ Сессия флэш-карт остановлена.\\n"
        f"Просмотрено: {reviewed}\\n"
        f"🪙 Получено: {coins} монет",
        reply_markup=get_study_keyboard(await loc(user_id)),
    )""",
        """async def handle_flashcard_stop(message: Message, state: FSMContext):
    user_id = message.from_user.id
    locale = await loc(user_id)
    data = await state.get_data()
    reviewed = data.get("flash_reviewed_count", 0)
    coins = data.get("flash_coins_earned", 0)
    logger.info(
        "flash.session.stop user_id=%s subject=%s reviewed=%s coins=%s",
        user_id, data.get("flash_subject_id"), reviewed, coins,
    )
    await message.answer(
        t("flash.stopped", locale, reviewed=reviewed, coins=coins),
        reply_markup=get_study_keyboard(locale),
    )""",
    ),
    # support message
    (
        """    await message.answer(
        "✅ Твое сообщение отправлено! Администратор ответит в ближайшее время.\\n\\n"
        "А пока можешь продолжить учиться — выбери Учеба в меню ниже 👇",
        reply_markup=get_main_keyboard(await loc(user_id))
    )""",
        """    locale = await loc(user_id)
    await message.answer(
        t("support.message_sent", locale),
        reply_markup=get_main_keyboard(locale),
    )""",
    ),
]

for old, new in replacements:
    if old not in t:
        print("MISSING:", old[:60].replace("\n", " "))
    else:
        t = t.replace(old, new, 1)
        print("OK")

p.write_text(t, encoding="utf-8")
print("done")
