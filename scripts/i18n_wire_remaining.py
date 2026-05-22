"""Wire remaining user-facing strings to t() in bot.py."""
from pathlib import Path

p = Path(__file__).resolve().parent.parent / "bot.py"
t = p.read_text(encoding="utf-8")

replacements = [
    # rating
    ('await callback.answer("Не удалось сохранить оценку", show_alert=True)',
     'await callback.answer(t("rating.save_failed", await loc(user_id)), show_alert=True)'),
    ('await callback.message.edit_text(f"✅ Спасибо за оценку! {emoji}")',
     'await callback.message.edit_text(t("rating.thanks", await loc(user_id), emoji=emoji))'),
    # reconcile
    ("""                    msg = (
                        f"🎉 Таймер на {duration} мин завершился, пока бот был офлайн.\\n"
                        f"🪙 Получено: {duration} монет"
                    )
                    if bonus > 0:
                        msg += f"\\n✨ Бонус за достижения: +{bonus} монет"
                    msg += f"\\n📊 Всего монет: {user['total_coins']}"
                    await bot.send_message(chat_id, msg, reply_markup=get_main_keyboard(await loc(user_id)))""",
     """                    locale = await loc(user_id)
                    msg = t("timer.reconcile_finished", locale, duration=duration)
                    if bonus > 0:
                        msg += t("timer.bonus", locale, bonus=bonus)
                    msg += t("timer.total_coins", locale, total_coins=user["total_coins"])
                    await bot.send_message(chat_id, msg, reply_markup=get_main_keyboard(locale))"""),
    # mcq callback
    ('await callback.answer("Сессия завершена", show_alert=False)',
     'await callback.answer(t("mcq.session_ended", await loc(callback.from_user.id)), show_alert=False)'),
    ('await callback.answer("Состояние повреждено", show_alert=True)',
     'await callback.answer(t("mcq.state_broken", await loc(callback.from_user.id)), show_alert=True)'),
    ('feedback = "✅ Верно! +1 🪙"',
     'feedback = t("mcq.correct", await loc(user_id))'),
    ('feedback = f"❌ Неверно.\\nПравильный ответ: {correct_text}"',
     'feedback = t("mcq.wrong", await loc(user_id), answer=correct_text)'),
    # common callbacks - batch replace
    ('await callback.answer("Это не твои настройки", show_alert=True)',
     'await callback.answer(t("common.not_yours_settings", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Это не твои карточки", show_alert=True)',
     'await callback.answer(t("common.not_yours_cards", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Это не твоя сессия", show_alert=True)',
     'await callback.answer(t("common.not_yours_session", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Это не твои задачи", show_alert=True)',
     'await callback.answer(t("common.not_yours_tasks", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Это не твой профиль", show_alert=True)',
     'await callback.answer(t("common.not_yours_profile", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Это не твои достижения", show_alert=True)',
     'await callback.answer(t("common.not_yours_achievements", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Неизвестный часовой пояс", show_alert=True)',
     'await callback.answer(t("common.unknown_tz", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Карточка не найдена", show_alert=True)',
     'await callback.answer(t("common.card_not_found", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Сломанный callback", show_alert=True)',
     'await callback.answer(t("common.broken_callback", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Только для админов", show_alert=True)',
     'await callback.answer(t("common.admin_only", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Неизвестная таблица", show_alert=True)',
     'await callback.answer(t("common.unknown_table", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Запрос уже не активен.", show_alert=True)',
     'await callback.answer(t("common.request_inactive", await loc(callback.from_user.id)), show_alert=True)'),
    ('await callback.answer("Удалено" if deleted else "Карточка не найдена")',
     'locale = await loc(callback.from_user.id); await callback.answer(t("fc.deleted_ok", locale) if deleted else t("fc.deleted_fail", locale))'),
    ('await callback.answer("Удалено" if deleted else "Задача не найдена")',
     'locale = await loc(callback.from_user.id); await callback.answer(t("common.deleted", locale) if deleted else t("common.task_not_found", locale))'),
    # user tasks import
    ('await message.answer("Нужен файл с расширением .txt. См. инструкцию выше.")',
     'await message.answer(t("user_tasks.need_txt", await loc(message.from_user.id)))'),
    ('await message.answer("Не удалось прочитать файл. Сохрани его в кодировке UTF-8.")',
     'await message.answer(t("user_tasks.read_error", await loc(message.from_user.id)))'),
    ('await message.answer("Не удалось скачать файл. Попробуй ещё раз.")',
     'await message.answer(t("user_tasks.download_error", await loc(message.from_user.id)))'),
    ('await message.answer("Файл пустой или содержит только комментарии.")',
     'await message.answer(t("user_tasks.empty_file", await loc(message.from_user.id)))'),
]

for old, new in replacements:
    if old not in t:
        print("MISS:", old[:55].replace("\n", " "))
    else:
        t = t.replace(old, new)
        print("OK")

p.write_text(t, encoding="utf-8")
print("written")
