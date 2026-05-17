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
    return db

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
            achievements_enabled INTEGER NOT NULL DEFAULT 1
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

    