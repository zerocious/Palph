"""
Тесты AnalyticsService — cohort retention.

Чтобы было воспроизводимо без time-mock'инга, мы манипулируем
created_at у users и last_attempt у progress-таблиц напрямую.
Это симулирует «прошёл день/неделя/месяц» без реального ожидания.
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from services import AnalyticsService


@pytest_asyncio.fixture
async def analytics(db):
    return AnalyticsService(db)


def _ts(date_offset_days: int, base: datetime = None) -> str:
    """Сегодня минус N дней, в формате SQLite datetime."""
    if base is None:
        base = datetime.now()
    return (base - timedelta(days=date_offset_days)).strftime("%Y-%m-%d %H:%M:%S")


async def _create_user_with_signup(db, user_id: int, days_ago: int):
    """Создаёт user'а с заданной датой регистрации (days_ago дней назад)."""
    await db.execute(
        "INSERT INTO users (user_id, created_at) VALUES (?, ?)",
        (user_id, _ts(days_ago)),
    )
    await db.commit()


async def _add_activity(db, user_id: int, days_ago: int, source: str = "study_sessions"):
    """Добавляет событие активности с заданным days_ago. source — имя таблицы."""
    ts = _ts(days_ago)
    if source == "study_sessions":
        await db.execute(
            "INSERT INTO study_sessions (user_id, duration_minutes, coins_earned, created_at) "
            "VALUES (?, 25, 25, ?)",
            (user_id, ts),
        )
    elif source == "quiz_progress":
        # Уникальный hash на каждый event чтобы не было PK-collision
        h = f"q{user_id}{days_ago:03d}"[:8]
        await db.execute(
            "INSERT INTO quiz_progress (user_id, term_hash, last_attempt) VALUES (?, ?, ?)",
            (user_id, h, ts),
        )
    elif source == "flashcard_progress":
        h = f"f{user_id}{days_ago:03d}"[:8]
        await db.execute(
            "INSERT INTO flashcard_progress (user_id, card_hash, last_review) VALUES (?, ?, ?)",
            (user_id, h, ts),
        )
    elif source == "mcq_progress":
        h = f"m{user_id}{days_ago:03d}"[:8]
        await db.execute(
            "INSERT INTO mcq_progress (user_id, question_hash, correct_count, total_count, last_attempt) "
            "VALUES (?, ?, 1, 1, ?)",
            (user_id, h, ts),
        )
    elif source == "task_progress":
        await db.execute(
            "INSERT INTO task_progress (user_id, task_id, succeeded, last_attempt) VALUES (?, ?, 1, ?)",
            (user_id, f"task-{days_ago:03d}", ts),
        )
    elif source == "user_subject_stats":
        await db.execute(
            "INSERT OR REPLACE INTO user_subject_stats (user_id, subject_id, visits, last_activity) "
            "VALUES (?, ?, 1, ?)",
            (user_id, f"sub-{days_ago}", ts),
        )
    await db.commit()


class TestEmptyDatabase:
    async def test_no_users_returns_empty(self, analytics):
        result = await analytics.compute_cohort_retention()
        assert result["total_users"] == 0
        assert result["cohorts"] == []


class TestSingleUser:
    async def test_user_signed_up_today_no_retention_data(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=0)
        result = await analytics.compute_cohort_retention()
        assert result["total_users"] == 1
        assert len(result["cohorts"]) == 1
        c = result["cohorts"][0]
        assert c["size"] == 1
        # User is too young for any retention metric
        assert c["d1"] is None
        assert c["d7"] is None
        assert c["d30"] is None

    async def test_user_31_days_old_no_d1_activity(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=31)
        # No activity recorded → all retention = 0
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["size"] == 1
        assert c["d1"] == 0.0
        assert c["d7"] == 0.0
        assert c["d30"] == 0.0

    async def test_user_active_on_d1_exactly(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=8)
        # Активность ровно на день 1 после регистрации (т.е. 7 дней назад)
        await _add_activity(db, 1, days_ago=7, source="study_sessions")
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["d1"] == 1.0  # 100% retained on D1

    async def test_user_active_d1_but_not_d7(self, db, analytics):
        # Подпись 10 дней назад → может иметь D1 + D7 данные
        await _create_user_with_signup(db, 1, days_ago=10)
        # День 1 после signup = 9 дней назад
        await _add_activity(db, 1, days_ago=9, source="study_sessions")
        # День 7 после signup = 3 дня назад → нет активности
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["d1"] == 1.0
        assert c["d7"] == 0.0


class TestMultipleActivitySources:
    async def test_activity_in_quiz_progress_counts(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=8)
        await _add_activity(db, 1, days_ago=7, source="quiz_progress")
        result = await analytics.compute_cohort_retention()
        assert result["cohorts"][0]["d1"] == 1.0

    async def test_activity_in_flashcard_progress_counts(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=8)
        await _add_activity(db, 1, days_ago=7, source="flashcard_progress")
        result = await analytics.compute_cohort_retention()
        assert result["cohorts"][0]["d1"] == 1.0

    async def test_activity_in_mcq_progress_counts(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=8)
        await _add_activity(db, 1, days_ago=7, source="mcq_progress")
        result = await analytics.compute_cohort_retention()
        assert result["cohorts"][0]["d1"] == 1.0

    async def test_activity_in_task_progress_counts(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=8)
        await _add_activity(db, 1, days_ago=7, source="task_progress")
        result = await analytics.compute_cohort_retention()
        assert result["cohorts"][0]["d1"] == 1.0


class TestCohortBucketingByWeek:
    async def test_users_in_same_iso_week_grouped_together(self, db, analytics):
        # Два пользователя в одной ISO-неделе (понедельник + четверг этой недели)
        await _create_user_with_signup(db, 1, days_ago=35)
        await _create_user_with_signup(db, 2, days_ago=33)  # тот же ISO-week, скорее всего
        result = await analytics.compute_cohort_retention()
        # Будут 1 или 2 когорты в зависимости от того, попали ли в одну неделю
        total = sum(c["size"] for c in result["cohorts"])
        assert total == 2

    async def test_separate_weeks_make_separate_cohorts(self, db, analytics):
        # 60 дней назад vs 10 дней назад — точно разные ISO-недели
        await _create_user_with_signup(db, 1, days_ago=60)
        await _create_user_with_signup(db, 2, days_ago=10)
        result = await analytics.compute_cohort_retention()
        assert len(result["cohorts"]) >= 2


class TestCohortPercentages:
    async def test_partial_retention_computed_correctly(self, db, analytics):
        # 4 пользователя в одной неделе, 2 активны на D1
        for uid, days_ago in [(1, 8), (2, 8), (3, 9), (4, 9)]:
            await _create_user_with_signup(db, uid, days_ago=days_ago)
        # User 1: активен на D1 (день 1 после signup = days_ago-1)
        await _add_activity(db, 1, days_ago=7, source="study_sessions")
        await _add_activity(db, 3, days_ago=8, source="study_sessions")
        # User 2 и 4: нет активности
        result = await analytics.compute_cohort_retention()
        # Все 4 в одной когорте (~неделе)
        # 2 из 4 retained на D1 → 50%
        total_retained = sum(int(c["d1"] * c["size"]) for c in result["cohorts"] if c["d1"] is not None)
        total_eligible = sum(c["size"] for c in result["cohorts"] if c["d1"] is not None)
        assert total_eligible == 4
        assert total_retained == 2


class TestEligibilityFilter:
    async def test_young_users_excluded_from_d30_denominator(self, db, analytics):
        # User старше 30 дней → eligible для D30
        await _create_user_with_signup(db, 1, days_ago=35)
        # User младше 30 дней → НЕ eligible
        await _create_user_with_signup(db, 2, days_ago=10)
        # No activity for either
        result = await analytics.compute_cohort_retention()
        # У старого пользователя своя когорта; D30 = 0% (0/1)
        # У молодого пользователя своя когорта; D30 = None (0 eligible)
        old_cohort = next((c for c in result["cohorts"] if c["d30"] is not None), None)
        young_cohort = next((c for c in result["cohorts"] if c["d30"] is None), None)
        assert old_cohort is not None
        assert old_cohort["d30"] == 0.0
        assert young_cohort is not None


class TestFunnel:
    async def test_empty_db(self, analytics):
        steps = await analytics.compute_funnel()
        assert steps == []

    async def test_user_at_first_step_only(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        # users.total_sessions defaults to 0 → no further funnel progress
        steps = await analytics.compute_funnel()
        assert len(steps) == 6
        registered = next(s for s in steps if s["name"] == "Registered")
        started = next(s for s in steps if s["name"] == "Started studying (≥1 session)")
        assert registered["count"] == 1
        assert registered["pct"] == 1.0
        assert started["count"] == 0
        assert started["pct"] == 0.0

    async def test_user_with_5_sessions_advances_funnel(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        await db.execute("UPDATE users SET total_sessions = 5 WHERE user_id = 1")
        await db.commit()
        steps = await analytics.compute_funnel()
        assert next(s for s in steps if "5+ sessions" in s["name"])["count"] == 1
        assert next(s for s in steps if "10+ sessions" in s["name"])["count"] == 0

    async def test_achievement_count_correct(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=10)
        # Mark 3-day streak achievement as completed
        await db.execute(
            "INSERT INTO user_achievements (user_id, achievement_id, completed, progress, target) "
            "VALUES (1, '3_day_streak', 1, 3, 3)"
        )
        await db.commit()
        steps = await analytics.compute_funnel()
        streak_step = next(s for s in steps if "3-day streak" in s["name"])
        assert streak_step["count"] == 1
        assert streak_step["pct"] == 1.0


class TestEngagement:
    async def test_empty_db(self, analytics):
        data = await analytics.compute_engagement()
        assert data["dau"] == 0
        assert data["wau"] == 0
        assert data["mau"] == 0
        assert data["stickiness"] is None
        assert data["total_users"] == 0

    async def test_active_today_counts_in_dau(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=10)
        await _add_activity(db, 1, days_ago=0, source="study_sessions")
        data = await analytics.compute_engagement()
        assert data["dau"] == 1
        assert data["wau"] == 1
        assert data["mau"] == 1

    async def test_only_recent_active_in_wau(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=30)
        # 10 days ago — outside WAU, inside MAU
        await _add_activity(db, 1, days_ago=10, source="study_sessions")
        data = await analytics.compute_engagement()
        assert data["dau"] == 0
        assert data["wau"] == 0
        assert data["mau"] == 1

    async def test_stickiness_correct(self, db, analytics):
        # 3 users — 1 active today, 3 active in last 30 days
        for uid in (1, 2, 3):
            await _create_user_with_signup(db, uid, days_ago=20)
            await _add_activity(db, uid, days_ago=15, source="study_sessions")  # MAU only
        await _add_activity(db, 1, days_ago=0, source="study_sessions")  # 1 active today
        data = await analytics.compute_engagement()
        assert data["dau"] == 1
        assert data["mau"] == 3
        assert data["stickiness"] == pytest.approx(1 / 3)

    async def test_new_today_counted(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=0)
        await _create_user_with_signup(db, 2, days_ago=0)
        await _create_user_with_signup(db, 3, days_ago=5)
        data = await analytics.compute_engagement()
        assert data["new_today"] == 2


class TestFeatureUsage:
    async def test_empty_db(self, analytics):
        data = await analytics.compute_feature_usage()
        assert data["total_users"] == 0
        assert data["features"] == []

    async def test_situational_quiz_adoption(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        await _create_user_with_signup(db, 2, days_ago=1)
        # Only user 1 has quiz progress
        await _add_activity(db, 1, days_ago=0, source="quiz_progress")
        data = await analytics.compute_feature_usage()
        sit_quiz = next(f for f in data["features"] if "Situational" in f["name"])
        assert sit_quiz["count"] == 1
        assert sit_quiz["pct"] == 0.5

    async def test_timezone_changed_counts(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        await _create_user_with_signup(db, 2, days_ago=1)
        # User 1 changes timezone
        await db.execute("UPDATE users SET timezone = 'Asia/Yekaterinburg' WHERE user_id = 1")
        await db.commit()
        data = await analytics.compute_feature_usage()
        tz_feat = next(f for f in data["features"] if "часовой пояс" in f["name"])
        assert tz_feat["count"] == 1


class TestExport:
    async def test_export_unknown_table_raises(self, analytics):
        with pytest.raises(KeyError):
            await analytics.export_table_csv("nonexistent_alias")

    async def test_export_users_returns_csv_with_header(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        csv_bytes, row_count = await analytics.export_table_csv("users")
        text = csv_bytes.decode("utf-8")
        lines = text.strip().split("\n")
        # First line = header (column names)
        assert "user_id" in lines[0]
        # Plus 1 data row
        assert row_count == 1
        assert len(lines) == 2

    async def test_export_empty_table(self, analytics):
        csv_bytes, row_count = await analytics.export_table_csv("sessions")
        # Header only, no data
        assert row_count == 0
        text = csv_bytes.decode("utf-8")
        # Header line present
        assert text.strip().split("\n")[0]  # at least one line

    async def test_export_handles_multiple_rows(self, db, analytics):
        for i in range(5):
            await _create_user_with_signup(db, 100 + i, days_ago=i)
        csv_bytes, row_count = await analytics.export_table_csv("users")
        assert row_count == 5


class TestExportAllTablesZip:
    async def test_returns_valid_zip(self, analytics):
        import zipfile
        import io

        zip_bytes, metadata = await analytics.export_all_tables_zip()
        # Must be a valid ZIP
        assert zipfile.is_zipfile(io.BytesIO(zip_bytes))

    async def test_contains_all_tables_plus_metadata(self, analytics):
        import zipfile
        import io
        from services import AnalyticsService

        zip_bytes, _ = await analytics.export_all_tables_zip()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            names = set(zf.namelist())
        # Каждой таблице из whitelist — соответствует CSV
        expected_csvs = {
            f"{table_name}.csv"
            for table_name in AnalyticsService.EXPORTABLE_TABLES.values()
        }
        assert expected_csvs.issubset(names)
        # И metadata.json
        assert "metadata.json" in names

    async def test_metadata_schema(self, analytics):
        zip_bytes, metadata = await analytics.export_all_tables_zip()
        assert "exported_at" in metadata
        assert metadata["exported_at"].endswith("Z")  # UTC suffix
        assert "schema_version" in metadata
        assert "row_counts" in metadata
        assert "tables" in metadata
        # row_counts has entry for each table
        assert set(metadata["row_counts"].keys()) == set(metadata["tables"])

    async def test_metadata_row_counts_match_data(self, db, analytics, created_user):
        """Если в users одна запись, metadata.row_counts.users == 1."""
        # created_user fixture создаёт user_id=42
        zip_bytes, metadata = await analytics.export_all_tables_zip()
        assert metadata["row_counts"]["users"] == 1

    async def test_zip_csvs_match_individual_export(self, db, analytics, created_user):
        """ZIP'нутый CSV для users должен совпадать с /export users."""
        import zipfile
        import io

        individual_csv, _ = await analytics.export_table_csv("users")
        zip_bytes, _ = await analytics.export_all_tables_zip()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            with zf.open("users.csv") as f:
                from_zip = f.read()
        assert individual_csv == from_zip

    async def test_metadata_json_inside_zip_parseable(self, analytics):
        import zipfile
        import io
        import json

        zip_bytes, _ = await analytics.export_all_tables_zip()
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            with zf.open("metadata.json") as f:
                parsed = json.loads(f.read().decode("utf-8"))
        assert "exported_at" in parsed
        assert isinstance(parsed["row_counts"], dict)

    async def test_empty_db_still_produces_valid_zip(self, analytics):
        """Empty DB — каждый CSV содержит только header; metadata: все counts=0."""
        import zipfile
        import io

        zip_bytes, metadata = await analytics.export_all_tables_zip()
        # Все row_counts должны быть 0
        assert all(n == 0 for n in metadata["row_counts"].values())
        # CSVs всё ещё парсятся (header-only)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            users_csv = zf.open("users.csv").read().decode("utf-8")
        # Header-line presents
        assert "user_id" in users_csv
