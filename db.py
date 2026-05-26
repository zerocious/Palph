import asyncio
import aiosqlite
import os
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.getenv("DB_PATH", "studybuddy.db")

async def get_db(db_path: str = DB_PATH) -> aiosqlite.Connection:
    """
    Создаёт и возвращает асинхронное подключение к SQLite.
    Настраивает WAL-режим и проверку внешних ключей.
    К соединению прикреплён asyncio.Lock — используйте его
    для read-modify-write операций (например, complete_session,
    переключение настроек), чтобы избежать гонок.
    """
    db = await aiosqlite.connect(db_path)
    db.row_factory = aiosqlite.Row
    db.lock = asyncio.Lock()
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    return db


async def execute_with_db_retry(coro_factory, *, retries: int = 3, base_delay: float = 0.05):
    """Retry aiosqlite coroutine on transient 'database is locked' errors."""
    for attempt in range(retries):
        try:
            return await coro_factory()
        except aiosqlite.OperationalError as e:
            if "locked" not in str(e).lower() or attempt == retries - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** attempt))

async def init_db(db: aiosqlite.Connection):
    """
    Инициализирует все таблицы и индексы, если они ещё не существуют.
    """
    await db.executescript("""
        -- Пользователи
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            timezone TEXT NOT NULL DEFAULT 'Europe/Moscow',
            total_sessions INTEGER NOT NULL DEFAULT 0,
            total_coins INTEGER NOT NULL DEFAULT 0,
            current_streak INTEGER NOT NULL DEFAULT 0,
            last_session TEXT,
            has_studied_today INTEGER NOT NULL DEFAULT 0,  -- булев флаг
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Настройки уведомлений
        CREATE TABLE IF NOT EXISTS notification_settings (
            user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            morning_enabled INTEGER NOT NULL DEFAULT 1,
            morning_time TEXT NOT NULL DEFAULT '09:00',
            evening_enabled INTEGER NOT NULL DEFAULT 1,
            evening_time TEXT NOT NULL DEFAULT '21:00',
            streak_enabled INTEGER NOT NULL DEFAULT 1,
            achievements_enabled INTEGER NOT NULL DEFAULT 1,
            flashcard_source TEXT NOT NULL DEFAULT 'mix'
        );

        -- Учебные сессии
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            duration_minutes INTEGER NOT NULL,
            coins_earned INTEGER NOT NULL,
            bonus_coins INTEGER NOT NULL DEFAULT 0,
            score INTEGER,  -- 1..4 эмодзи-оценка от пользователя; NULL = не оценено
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON study_sessions(user_id);
        CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON study_sessions(created_at);

        -- Достижения пользователей (прогресс)
        CREATE TABLE IF NOT EXISTS user_achievements (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            achievement_id TEXT NOT NULL,
            progress INTEGER NOT NULL DEFAULT 0,
            target INTEGER NOT NULL DEFAULT 0,
            completed INTEGER NOT NULL DEFAULT 0,  -- 0 или 1
            PRIMARY KEY (user_id, achievement_id)
        );

        -- Прогресс по квизам (интервальное повторение терминов)
        CREATE TABLE IF NOT EXISTS quiz_progress (
            user_id INTEGER NOT NULL,
            term_hash TEXT NOT NULL,
            last_attempt TEXT,
            is_correct INTEGER NOT NULL DEFAULT 0,
            streak INTEGER NOT NULL DEFAULT 0,
            next_review TEXT,
            PRIMARY KEY (user_id, term_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_quiz_user_id ON quiz_progress(user_id);

        -- SM-2 прогресс по флэш-картам (v0.7 #15).
        -- Используется только в режиме flashcards; ситуационные квизы
        -- остаются в quiz_progress на фиксированных интервалах.
        CREATE TABLE IF NOT EXISTS flashcard_progress (
            user_id INTEGER NOT NULL,
            card_hash TEXT NOT NULL,
            ease_factor REAL NOT NULL DEFAULT 2.5,
            interval_days INTEGER NOT NULL DEFAULT 0,
            repetitions INTEGER NOT NULL DEFAULT 0,
            last_review TEXT,
            next_review TEXT,
            PRIMARY KEY (user_id, card_hash)
        );
        CREATE INDEX IF NOT EXISTS idx_flashcard_user_next
            ON flashcard_progress(user_id, next_review);

        -- Per-question прогресс MCQ (v0.7 progress tracking).
        -- correct_count — сколько раз пользователь ответил правильно;
        -- total_count — сколько раз вопрос был показан. "Mastered" = correct_count ≥ 1.
        CREATE TABLE IF NOT EXISTS mcq_progress (
            user_id INTEGER NOT NULL,
            question_hash TEXT NOT NULL,
            correct_count INTEGER NOT NULL DEFAULT 0,
            total_count INTEGER NOT NULL DEFAULT 0,
            last_attempt TEXT,
            PRIMARY KEY (user_id, question_hash)
        );

        -- Per-task прогресс (v0.7 progress tracking).
        -- attempts_used — сколько попыток заняло (1..3); succeeded — bool (0/1).
        -- "Mastered" = succeeded=1 при любом attempts_used.
        CREATE TABLE IF NOT EXISTS task_progress (
            user_id INTEGER NOT NULL,
            task_id TEXT NOT NULL,
            attempts_used INTEGER NOT NULL DEFAULT 0,
            succeeded INTEGER NOT NULL DEFAULT 0,
            last_attempt TEXT,
            PRIMARY KEY (user_id, task_id)
        );

        -- Aggregate-статистика per (user, subject): visits + last_activity.
        -- Bump'ается из 4 точек старта учебных режимов (start_*_session +
        -- handle_quiz_section). Используется в экране прогресса как
        -- «Заходов» и «Последняя активность».
        CREATE TABLE IF NOT EXISTS user_subject_stats (
            user_id INTEGER NOT NULL,
            subject_id TEXT NOT NULL,
            visits INTEGER NOT NULL DEFAULT 0,
            last_activity TEXT,
            PRIMARY KEY (user_id, subject_id)
        );

        -- Append-only event log для PA-аналитики.
        -- Одна строка на каждое значимое действие пользователя; properties
        -- — JSON-словарь со event-specific полями. Используется внешними
        -- инструментами (Jupyter/pandas через /export events) для funnel,
        -- cohort, path, time-to-action анализа.
        -- user_id nullable: некоторые события могут быть system-level
        -- (хотя сейчас всё логируется per-user).
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            event_name TEXT NOT NULL,
            properties TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_events_user_time
            ON events(user_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_events_name_time
            ON events(event_name, created_at);

        -- Администраторы бота
        CREATE TABLE IF NOT EXISTS admins (
            user_id INTEGER PRIMARY KEY
        );

        -- Постоянное хранилище FSM для aiogram (см. fsm_storage.SQLiteStorage).
        -- key = "{bot_id}:{chat_id}:{user_id}:{thread_id}"; data — JSON.
        CREATE TABLE IF NOT EXISTS fsm_storage (
            key TEXT PRIMARY KEY,
            state TEXT,
            data TEXT NOT NULL DEFAULT '{}'
        );

        -- Цифровой питомец пользователя (v0.7 TODO #16).
        -- Один питомец на user, один общий дизайн — поэтому species не хранится.
        -- Эмоция (studying/excited/sad/sleepy/happy) выводится из состояния
        -- пользователя в момент рендера через services.derive_emotion() —
        -- здесь её НЕ храним.
        -- color/accessory всегда NOT NULL; sentinel-значение "none" для аксессуара
        -- (вместо nullable) — упрощает рендер и инвентарь.
        -- last_excited_at — timestamp последнего level-up или достижения;
        -- используется derive_emotion для приоритета "excited" в окне 5 минут.
        CREATE TABLE IF NOT EXISTS user_pet (
            user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            name TEXT NOT NULL DEFAULT 'Питомец',
            color TEXT NOT NULL DEFAULT 'orange',
            accessory TEXT NOT NULL DEFAULT 'none',
            level INTEGER NOT NULL DEFAULT 1,
            xp INTEGER NOT NULL DEFAULT 0,
            last_excited_at TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );

        -- Инвентарь купленных предметов (color/accessory). Composite PK
        -- гарантирует идемпотентность INSERT OR IGNORE при покупке.
        -- На создании питомца сидится двумя бесплатными дефолтами:
        -- (color, orange) и (accessory, none).
        CREATE TABLE IF NOT EXISTS user_pet_inventory (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            item_type TEXT NOT NULL,  -- 'color' | 'accessory'
            item_value TEXT NOT NULL, -- 'orange', 'hat', 'none', ...
            purchased_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, item_type, item_value)
        );

        -- Daily-cap счётчики per (user, local_date в TZ пользователя).
        -- Хранят intra-day состояние для score-инкрементов:
        --   time_minutes / time_pts — для piecewise time pts
        --   task_count — для daily cap 5 на math tasks
        --   quiz_count + quiz_series_running — для cap 25 + series bonus
        --   cards_count — для cap 8 на successful card reviews
        -- Series counter resets на следующий день естественно (новая PK row).
        -- См. LEADERBOARD.md §3, §4.
        CREATE TABLE IF NOT EXISTS daily_score_counters (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            local_date TEXT NOT NULL,                       -- 'YYYY-MM-DD' в TZ user'а
            time_minutes INTEGER NOT NULL DEFAULT 0,
            time_pts REAL NOT NULL DEFAULT 0,
            task_count INTEGER NOT NULL DEFAULT 0,
            quiz_count INTEGER NOT NULL DEFAULT 0,
            quiz_series_running INTEGER NOT NULL DEFAULT 0,
            cards_count INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, local_date)
        );

        -- Per-user weekly totals (что читает /leaderboard).
        -- week_iso = 'YYYY-Www' (ISO неделя в TZ пользователя). PK на (user_id, week_iso)
        -- партиционирует естественно: past-week строки никогда не обновляются после
        -- rollover в новую неделю. Multiplier НЕ хранится — вычисляется на read-time
        -- из users.current_streak (см. services.streak_multiplier).
        CREATE TABLE IF NOT EXISTS weekly_scores (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            week_iso TEXT NOT NULL,
            time_pts REAL NOT NULL DEFAULT 0,
            task_pts INTEGER NOT NULL DEFAULT 0,
            quiz_pts INTEGER NOT NULL DEFAULT 0,
            card_pts INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, week_iso)
        );
        CREATE INDEX IF NOT EXISTS idx_weekly_scores_week
            ON weekly_scores(week_iso);

        -- Streak freeze events: история купленных заморозок стрика.
        -- Cooldown enforced через MAX(granted_at) < now - 7 days.
        -- consumed_for_date = 'YYYY-MM-DD' пропущенного дня (если использована),
        -- иначе NULL (ещё не активна или активна и ждёт пропуска).
        CREATE TABLE IF NOT EXISTS streak_freezes (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            granted_at TEXT NOT NULL,                       -- datetime('now')
            streak_at_grant INTEGER NOT NULL,               -- audit: длина стрика при покупке
            cost_paid INTEGER NOT NULL,                     -- 500 / 750 / 1000
            consumed_for_date TEXT,                         -- 'YYYY-MM-DD' пропущенного дня
            PRIMARY KEY (user_id, granted_at)
        );
        -- Index для быстрого поиска активной (неиспользованной) заморозки user'а.
        CREATE INDEX IF NOT EXISTS idx_freezes_user_unused
            ON streak_freezes(user_id, consumed_for_date)
            WHERE consumed_for_date IS NULL;

        -- Weekly leaderboard-награды с expiration.
        -- badge_id ∈ {'top_1', 'top_2', 'top_3', 'breakthrough'}.
        -- awarded_for_week — 'YYYY-Www' (на какую неделю выдан).
        -- expires_at — обычно awarded_at + 7 days (1-week badges по спеке).
        CREATE TABLE IF NOT EXISTS weekly_badges (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            badge_id TEXT NOT NULL,
            awarded_for_week TEXT NOT NULL,
            awarded_at TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            PRIMARY KEY (user_id, badge_id, awarded_for_week)
        );

        -- Pending friend requests (Phase 4, LEADERBOARD.md §Segments → Friends).
        -- Хранятся только pending: на accept строка удаляется и появляется
        -- friendship; на reject/cancel — просто удаляется. PK (from, to)
        -- предотвращает дубль-отправку. CHECK исключает self-request.
        CREATE TABLE IF NOT EXISTS friend_requests (
            from_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            to_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (from_user_id, to_user_id),
            CHECK (from_user_id != to_user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_friend_requests_to
            ON friend_requests(to_user_id);

        -- Подтверждённые дружбы (Phase 4). Нормализованное хранение:
        -- ВСЕГДА user_a < user_b. Это гарантирует одну строку на дружбу
        -- (а не две a→b и b→a), упрощает уникальность и поиск
        -- "are A and B friends?".
        CREATE TABLE IF NOT EXISTS friendships (
            user_a INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            user_b INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_a, user_b),
            CHECK (user_a < user_b)
        );
        CREATE INDEX IF NOT EXISTS idx_friendships_user_b
            ON friendships(user_b);

        -- Friend invite tokens — Telegram deep-link «t.me/Bot?start=friend_<token>».
        -- Создаются через /share_friend, multiuse (один токен → много друзей),
        -- expires_at = created_at + 3 days. При клике на ссылку invitee
        -- автоматически становится другом creator'а (skip pending state),
        -- т.к. ссылка = consent от creator, click = consent от invitee.
        -- BACKLOG → ship 2026-05-19.
        CREATE TABLE IF NOT EXISTS friend_invite_tokens (
            token TEXT PRIMARY KEY,
            from_user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            expires_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_friend_invite_tokens_from
            ON friend_invite_tokens(from_user_id);

        -- Пользовательские флэш-карточки (per user + subject).
        CREATE TABLE IF NOT EXISTS user_flashcards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL,
            term TEXT NOT NULL,
            definition TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE (user_id, subject_id, term)
        );
        CREATE INDEX IF NOT EXISTS idx_user_flashcards_lookup
            ON user_flashcards(user_id, subject_id);

        -- Пользовательские задачи (текст, без картинки; импорт из .txt).
        CREATE TABLE IF NOT EXISTS user_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL,
            problem TEXT NOT NULL,
            accepted TEXT NOT NULL,
            hint TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_user_tasks_lookup
            ON user_tasks(user_id, subject_id);

        -- Статистика просмотров советов по продуктивности (геймификация).
        CREATE TABLE IF NOT EXISTS user_tips_stats (
            user_id INTEGER PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
            total_views INTEGER NOT NULL DEFAULT 0,
            last_coin_date TEXT,
            tip_of_day_id TEXT,
            tip_of_day_date TEXT
        );

        -- Какие советы уже показывали пользователю (для cooldown N дней).
        CREATE TABLE IF NOT EXISTS user_tips_seen (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            tip_id TEXT NOT NULL,
            seen_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, tip_id)
        );
        CREATE INDEX IF NOT EXISTS idx_user_tips_seen_user_time
            ON user_tips_seen(user_id, seen_at);

        -- Sprint exam plan (v0.9): binary skill per topic per subject.
        CREATE TABLE IF NOT EXISTS user_skill_map (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            skill INTEGER NOT NULL DEFAULT 0,
            last_updated TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, subject_id, topic)
        );

        -- Pre-computed 14-day sprint plan (JSON) + daily time budget.
        CREATE TABLE IF NOT EXISTS user_active_plan (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL,
            plan_json TEXT NOT NULL,
            day_minutes INTEGER NOT NULL DEFAULT 60,
            logical_day INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            PRIMARY KEY (user_id, subject_id)
        );

        -- UX flags for plan onboarding per subject.
        CREATE TABLE IF NOT EXISTS user_plan_meta (
            user_id INTEGER NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
            subject_id TEXT NOT NULL,
            diagnostic_done INTEGER NOT NULL DEFAULT 0,
            first_prompt_shown INTEGER NOT NULL DEFAULT 0,
            skip_plan_prompt INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (user_id, subject_id)
        );
    """)
    await db.commit()

    # Миграция: добавляем колонку `score` в существующие БД без неё.
    # SQLite не поддерживает IF NOT EXISTS для ADD COLUMN, поэтому ловим
    # ошибку "duplicate column" — в свежей БД её не будет, в старой будет.
    try:
        await db.execute("ALTER TABLE study_sessions ADD COLUMN score INTEGER")
        await db.commit()
    except Exception:
        pass  # колонка уже есть

    # Миграция: privacy opt-out для лидербордов (LEADERBOARD.md §Privacy).
    # 0 (default) = виден на публичных лидербордах; 1 = скрыт.
    # Score-аккумуляция и право на rewards не зависят от этого флага,
    # только публичная видимость.
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN "
            "hidden_from_leaderboards INTEGER NOT NULL DEFAULT 0"
        )
        await db.commit()
    except Exception:
        pass  # колонка уже есть

    # Миграция: Telegram @username для friends-search (BACKLOG → ship).
    # Nullable: пользователи без публичного @handle имеют NULL.
    # Обновляется UsernameSyncMiddleware на каждый Message/CallbackQuery,
    # т.к. Telegram может менять username в любой момент.
    try:
        await db.execute("ALTER TABLE users ADD COLUMN username TEXT")
        await db.commit()
    except Exception:
        pass  # колонка уже есть

    # Миграция: источник флэш-карт при учёбе (mix / official / own).
    try:
        await db.execute(
            "ALTER TABLE notification_settings ADD COLUMN "
            "flashcard_source TEXT NOT NULL DEFAULT 'mix'"
        )
        await db.commit()
    except Exception:
        pass  # колонка уже есть

    # Миграция: idempotency marker для nightly streak processing.
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN last_streak_check_date TEXT"
        )
        await db.commit()
    except Exception:
        pass  # колонка уже есть

    # UI locale: ru | en (пусто = ещё не выбран при первом /start).
    try:
        await db.execute(
            "ALTER TABLE users ADD COLUMN locale TEXT NOT NULL DEFAULT ''"
        )
        await db.commit()
    except Exception:
        pass  # колонка уже есть

    for col_sql in (
        "ALTER TABLE user_tips_stats ADD COLUMN tip_of_day_id TEXT",
        "ALTER TABLE user_tips_stats ADD COLUMN tip_of_day_date TEXT",
    ):
        try:
            await db.execute(col_sql)
            await db.commit()
        except Exception:
            pass

    # Миграция: индексируемые поля в events для PA-SQL (subject/mode/tip).
    for col_sql in (
        "ALTER TABLE events ADD COLUMN subject_id TEXT",
        "ALTER TABLE events ADD COLUMN mode TEXT",
        "ALTER TABLE events ADD COLUMN tip_id TEXT",
    ):
        try:
            await db.execute(col_sql)
            await db.commit()
        except Exception:
            pass

    