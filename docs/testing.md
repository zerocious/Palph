# Требования к тестам

> **Doc sync:** 2026-09-05 · **802** тест в 52 файлах, все зелёные
> (`python -m pytest -q`, ~24 с).

## Запуск

```bash
pip install -r requirements-dev.txt
python -m pytest -q                        # весь набор
python -m pytest tests/test_sm2.py -v      # один файл
python -m pytest -k "leaderboard" -q       # по подстроке
python -m pytest --collect-only -q | tail -1   # сколько тестов сейчас
```

`pytest.ini`: `asyncio_mode = auto` (async-тесты не нужно помечать
декоратором), `testpaths = tests`, DeprecationWarning от aiogram/aiosqlite
подавлены.

`BOT_TOKEN` для тестов не нужен: `tests/conftest.py` подставляет фиктивный
через `os.environ.setdefault` — но только если реальный не задан.

## Обязательные правила

### 1. Время: только относительные привязки

**В тестах запрещены абсолютные календарные даты как якорь «сейчас».
Абсолютная дата допустима лишь тогда, когда она передаётся в код явным
параметром.**

Причина простая: код, читающий системные часы, в тесте всегда сравнивается
с реальным «сегодня». Тест, у которого данные записаны в жёстко заданную
неделю, зелёный ровно одну неделю в году, а потом молча краснеет — и
разработчик тратит вечер на отладку несуществующего бага.

**Так нельзя** — код под тестом считает текущую неделю сам, а данные
записаны в неделю из прошлого:

```python
NOW = datetime(2026, 5, 18, 14, 30)   # ❌ абсолютный якорь
WEEK = "2026-W21"                     # ❌ протухнет
await lb_repo.grant_task_pts(1, now_local=NOW)
text = await lb_service.render_leaderboard(1)   # смотрит на real now → пусто
```

**Так можно** — якорь относительный, ключи выводятся из него:

```python
_ANCHOR_TZ = pytz.timezone("Europe/Moscow")
NOW = datetime.now(_ANCHOR_TZ).replace(hour=14, minute=30, second=0, microsecond=0)
TODAY, WEEK = user_calendar_keys(NOW)   # ✅ всегда «текущая» неделя
```

**И так тоже можно** — абсолютная дата уходит в функцию параметром,
системные часы не участвуют:

```python
assert user_calendar_keys(datetime(2024, 1, 1))[1] == "2024-W01"   # ✅
assert _compute_ended_week_iso(datetime(2026, 5, 19)) == "2026-W20" # ✅
await lb_repo.consume_freeze_if_active(uid, "2026-05-19")           # ✅
```

Практическая формулировка: **если в цепочке вызовов есть хоть один
`datetime.now()` внутри продакшн-кода — якорь теста обязан быть
относительным**. Если весь тракт принимает время параметром — литерал
уместен и даже полезен (проверка границ ISO-недель, високосных дат,
переходов года).

Отсюда следует требование к продакшн-коду: **любая функция, зависящая от
времени, должна принимать его параметром** с дефолтом `None` →
`datetime.now(...)`. Так уже сделано в `LeaderboardRepository.grant_*`
(`now_local=None`), `render_pet(now_local=..., time_period=...)`,
`derive_emotion(now_local=...)`, `StreakService._process(tz=...)`,
`_compute_ended_week_iso(now_utc)`. Новый код обязан следовать этому же
шаблону — иначе он просто не будет детерминированно тестируемым.

Относительные смещения (`datetime.now() - timedelta(days=30)` для
«состарить» пользователя) — нормальная практика, они не привязаны к
календарю.

### 2. Изоляция

Каждый тест получает **свежую SQLite-БД во временном файле** (фикстура
`db` в `conftest.py`); файл и его `-wal`/`-shm` удаляются после. Тесты не
делят состояние, порядок выполнения не важен, `pytest -k` и параллельный
прогон безопасны.

### 3. Никакой сети

Telegram не вызывается по-настоящему: `unittest.mock.AsyncMock` вместо
объекта `Bot`. Тест, которому нужен реальный HTTP, писать не нужно —
пограничные случаи API покрываются проверкой того, какие исключения
код ловит (`tests/test_telegram_resilience.py`).

### 4. Документация — часть прогона

`tests/test_docs_consistency.py` гоняет проверки из
`scripts/check_docs.py` внутри pytest, поэтому расхождение документации с
кодом краснеет обычным `pytest -q`, а не только когда кто-то вспомнит про
отдельный скрипт. Внутрь теста не включена сверка счётчиков тестов — она
поднимает `pytest --collect-only` подпроцессом; её делает сам скрипт.

Отдельный тест `test_every_check_actually_asserts_something` следит, что
ни одна проверка не превратилась в пустышку, которая ничего не проверяет.

### 5. Каталоги — из продакшн-файлов

Фикстура `achievements_catalog` читает настоящий `achievements.json`, а не
копию: тест должен краснеть, когда каталог разъезжается с кодом.
Тот же принцип у `tests/test_tips_content.py`,
`tests/test_math_bernoulli_tasks.py`, `tests/test_i18n.py` — они валидируют
реальный контент репозитория.

### 6. Чистые функции тестируются без БД

Формулы (`sm2_update`, `piecewise_time_pts`, `streak_multiplier`,
`freeze_cost`, `derive_emotion`, `parse_friend_query`,
`task_answer_matches`, весь `plan_service`) вынесены в чистые функции
именно для того, чтобы их тесты не поднимали ни соединение, ни бота.
Новую формулу пишите чистой функцией — это требование к дизайну, а не
пожелание.

## Что должно быть покрыто

| Изменение | Минимум тестов |
|-----------|----------------|
| Новая формула / расчёт | Юнит-тест чистой функции + граничные значения |
| Новый метод репозитория | Happy path + пустой результат + идемпотентность (если применимо) |
| Новая таблица с `user_id` | Расширить `tests/test_delete_user_completely.py` |
| Новое событие аналитики | Проверка, что `properties` содержат ожидаемые ключи |
| Новый ключ локали | `tests/test_i18n.py` (паритет ru/en) |
| Новый контент | Валидатор структуры (см. `test_math_bernoulli_tasks.py`) |
| Багфикс | Регрессионный тест, падающий **до** фикса |

## Карта тестов

| Область | Файлы | ≈ тестов |
|---------|-------|---------:|
| Аналитика | `test_analytics_service.py`, `test_event_repository.py`, `test_log_parser.py`, `test_pa_verify_export.py` | 110 |
| Лидерборд и социальное | `test_leaderboard_service.py`, `test_leaderboard_repository.py`, `test_leaderboard_helpers.py`, `test_friend_repository.py`, `test_friend_invite_tokens.py`, `test_username_search.py`, `test_username_sync_middleware.py`, `test_integration_flows.py` | 204 |
| Таймер и сессии | `test_timer_edge_cases.py`, `test_task_session.py`, `test_level_up_notification.py` | 61 |
| Питомец | `test_pet_repository.py`, `test_render_pet.py`, `test_derive_emotion.py`, `test_pet_time_period.py`, `test_pet_picker_helpers.py` | 92 |
| Учебные режимы | `test_sm2.py`, `test_user_flashcards.py`, `test_user_tasks.py`, `test_flashcard_source.py`, `test_progress_repos.py`, `test_task_answer_match.py`, `test_math_bernoulli_tasks.py`, `test_prep_subjects.py` | 89 |
| Планировщики и стрики | `test_streak_service.py`, `test_reminder_service.py`, `test_morning_tip_reminder.py`, `test_backup_service.py` | 34 |
| Безопасность | `test_file_upload_security.py`, `test_command_input_security.py`, `test_rate_limiter.py`, `test_admin_message_rate_limit.py`, `test_telegram_resilience.py` | 56 |
| Приватность и данные | `test_delete_user_completely.py`, `test_db_paths.py` | 29 |
| Советы | `test_tips_gamification.py`, `test_tips_medium_features.py`, `test_tips_content.py`, `test_productivity_tips_files.py` | 31 |
| UX и настройки | `test_main_menu_ux.py`, `test_profile_ux.py`, `test_settings_fixes.py`, `test_tz_presets.py`, `test_i18n.py` | 39 |
| Спринт-план (UI выключен) | `test_plan_service.py`, `test_plan_repository.py` | 34 |
| Ачивки | `test_achievement_service.py` | 14 |
| Документация | `test_docs_consistency.py` | 9 |

Точные цифры на текущий момент: `pytest --collect-only -q | tail -1`.

## Дополнительные проверки

```bash
python scripts/check_docs.py          # документация против кода
python -m py_compile bot.py services.py repository.py db.py   # синтаксис
python -c "import bot"                # ловит name-errors в декораторах
python scripts/audit_i18n_keys.py     # ключи t() против locales/*.json
python scripts/check_plan_issues.py   # контент-гейт спринт-плана
python scripts/pa_verify_export.py    # аналитика и экспорт живы
```

## Чего в тестах нет

- **CI-прогона.** GitHub Actions запускает только `pip-audit`. Тесты и
  `check_docs.py` — ответственность разработчика перед коммитом. Оба
  возвращают ненулевой код выхода, так что при желании заводятся в CI
  одним шагом.
- **Замера покрытия.** `coverage` не подключён; ориентир — карта выше.
- **E2E через реальный Telegram.** Верхний уровень — интеграционные тесты
  на связках repo → service → render.
