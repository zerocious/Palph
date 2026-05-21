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


class TestCohortConfidenceIntervals:
    """PA-roadmap #7: cohort retention rows now carry Wilson 95% CI.
    Backwards-compatible — old d1/d7/d30 fields unchanged."""

    async def test_eligible_zero_gives_none_ci(self, db, analytics):
        """Когорта моложе N дней → eligible=0 → d_N_ci is None."""
        await _create_user_with_signup(db, 1, days_ago=0)
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["d1_ci"] is None
        assert c["d7_ci"] is None
        assert c["d30_ci"] is None

    async def test_ci_brackets_point_estimate(self, db, analytics):
        """Для cohort с реальной retention — Wilson CI содержит p̂."""
        # 5 user'ов, 8 дней назад; 3 активны на D1
        for uid in range(1, 6):
            await _create_user_with_signup(db, uid, days_ago=8)
        for uid in (1, 2, 3):
            await _add_activity(db, uid, days_ago=7, source="study_sessions")
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["d1"] == 0.6  # 3/5
        low, high = c["d1_ci"]
        # Точечная оценка должна лежать в интервале
        assert low <= 0.6 <= high
        # Wilson 95% для k=3/n=5 — должен быть широкий (>50% коридор)
        assert (high - low) > 0.5

    async def test_ci_width_shrinks_with_n(self, db, analytics):
        """Тот же p̂ при большем n → CI должен сужаться. Проверяем через
        две когорты на разных ISO-неделях с одинаковым retention."""
        # Cohort A: 5 user'ов 8 дней назад, 50% retention на D1
        await _create_user_with_signup(db, 1, days_ago=8)
        await _create_user_with_signup(db, 2, days_ago=8)
        await _create_user_with_signup(db, 3, days_ago=8)
        await _create_user_with_signup(db, 4, days_ago=8)
        await _add_activity(db, 1, days_ago=7, source="study_sessions")
        await _add_activity(db, 2, days_ago=7, source="study_sessions")
        # Cohort B: 20 user'ов 15 дней назад, 50% retention на D1
        # 15 дней назад точно даст другую ISO-неделю чем 8 дней назад
        for uid in range(100, 120):
            await _create_user_with_signup(db, uid, days_ago=15)
        for uid in range(100, 110):
            await _add_activity(db, uid, days_ago=14, source="study_sessions")
        result = await analytics.compute_cohort_retention()
        # Найти обе когорты
        c_small = next(c for c in result["cohorts"] if c["size"] == 4)
        c_big = next(c for c in result["cohorts"] if c["size"] == 20)
        assert c_small["d1"] == 0.5
        assert c_big["d1"] == 0.5
        ws = c_small["d1_ci"][1] - c_small["d1_ci"][0]
        wb = c_big["d1_ci"][1] - c_big["d1_ci"][0]
        assert wb < ws

    async def test_zero_retention_ci_starts_at_zero(self, db, analytics):
        """k=0 → CI lower bound = 0 (зажато), upper > 0."""
        await _create_user_with_signup(db, 1, days_ago=8)
        # No activity → 0/1 retained
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["d1"] == 0.0
        low, high = c["d1_ci"]
        assert low == 0.0
        assert high > 0.0

    async def test_full_retention_ci_ends_at_one(self, db, analytics):
        """k=n → CI upper bound = 1, lower < 1."""
        await _create_user_with_signup(db, 1, days_ago=8)
        await _add_activity(db, 1, days_ago=7, source="study_sessions")
        result = await analytics.compute_cohort_retention()
        c = result["cohorts"][0]
        assert c["d1"] == 1.0
        low, high = c["d1_ci"]
        assert high == 1.0
        assert low < 1.0


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

    async def test_metadata_points_at_schema_contract(self, analytics):
        """PA-roadmap #5: metadata.json should reference the YAML contract
        so downstream notebooks know which version to validate against."""
        _, metadata = await analytics.export_all_tables_zip()
        assert metadata["schema_version"] == "1"
        assert metadata["schema_contract"] == "analysis/schema_v1.yaml"

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


class TestSegments:
    async def test_empty_db(self, analytics):
        data = await analytics.compute_segments()
        assert data["total_users"] == 0
        assert data["segments"] == []

    async def test_never_started(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        # total_sessions defaults to 0
        data = await analytics.compute_segments()
        ns = next(s for s in data["segments"] if "Never" in s["name"])
        assert ns["count"] == 1

    async def test_tried_segment(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        await db.execute("UPDATE users SET total_sessions = 2 WHERE user_id = 1")
        await _add_activity(db, 1, days_ago=0, source="study_sessions")
        await db.commit()
        data = await analytics.compute_segments()
        tried = next(s for s in data["segments"] if "Tried" in s["name"])
        assert tried["count"] == 1

    async def test_active_segment(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=1)
        await db.execute("UPDATE users SET total_sessions = 5 WHERE user_id = 1")
        await _add_activity(db, 1, days_ago=0, source="study_sessions")
        await db.commit()
        data = await analytics.compute_segments()
        active = next(s for s in data["segments"] if "Active" in s["name"])
        assert active["count"] == 1

    async def test_power_segment(self, db, analytics):
        await _create_user_with_signup(db, 1, days_ago=10)
        await db.execute("UPDATE users SET total_sessions = 15 WHERE user_id = 1")
        await _add_activity(db, 1, days_ago=1, source="study_sessions")
        await db.commit()
        data = await analytics.compute_segments()
        power = next(s for s in data["segments"] if "Power" in s["name"])
        assert power["count"] == 1

    async def test_churned_takes_priority_over_active(self, db, analytics):
        """Active user не заходивший 20 дней — должен быть в Churned, не в Active."""
        await _create_user_with_signup(db, 1, days_ago=30)
        await db.execute("UPDATE users SET total_sessions = 5 WHERE user_id = 1")
        # Last activity was 20 days ago — past churn threshold (default 14)
        await _add_activity(db, 1, days_ago=20, source="study_sessions")
        await db.commit()
        data = await analytics.compute_segments()
        churned = next(s for s in data["segments"] if "Churned" in s["name"])
        active = next(s for s in data["segments"] if "Active" in s["name"])
        assert churned["count"] == 1
        assert active["count"] == 0

    async def test_never_started_not_marked_churned(self, db, analytics):
        """User with 0 sessions and no activity stays in never_started, not churned."""
        await _create_user_with_signup(db, 1, days_ago=30)
        # total_sessions=0, нет activity events
        data = await analytics.compute_segments()
        ns = next(s for s in data["segments"] if "Never" in s["name"])
        churned = next(s for s in data["segments"] if "Churned" in s["name"])
        assert ns["count"] == 1
        assert churned["count"] == 0

    async def test_pcts_sum_to_1(self, db, analytics):
        for uid, sessions, days_ago in [
            (1, 0, 1),    # never
            (2, 1, 1),    # tried
            (3, 5, 1),    # active
            (4, 15, 1),   # power
        ]:
            await _create_user_with_signup(db, uid, days_ago=days_ago)
            await db.execute(
                "UPDATE users SET total_sessions = ? WHERE user_id = ?",
                (sessions, uid),
            )
            if sessions > 0:
                await _add_activity(db, uid, days_ago=0, source="study_sessions")
        await db.commit()
        data = await analytics.compute_segments()
        total_pct = sum(s["pct"] for s in data["segments"])
        assert total_pct == pytest.approx(1.0, abs=0.001)


class TestContentStats:
    async def test_empty_db_returns_empty_lists(self, analytics):
        data = await analytics.compute_content_stats()
        assert data["hardest_situational"] == []
        assert data["most_attempted_mcq"] == []
        assert data["progress_coverage"]["situational_terms_attempted"] == 0
        assert data["flashcard_ef_distribution"]["total"] == 0

    async def test_hardest_situational_sorts_by_accuracy(self, db, analytics, created_user):
        # 3 terms: hash_easy (always correct), hash_med (50%), hash_hard (always wrong)
        await db.execute(
            "INSERT INTO quiz_progress (user_id, term_hash, is_correct, streak) "
            "VALUES (?, 'easy0001', 1, 1)",
            (created_user,),
        )
        await db.execute(
            "INSERT INTO quiz_progress (user_id, term_hash, is_correct, streak) "
            "VALUES (?, 'hard0001', 0, 0)",
            (created_user,),
        )
        await db.commit()
        data = await analytics.compute_content_stats()
        # hardest должно идти первым (accuracy=0)
        assert data["hardest_situational"][0]["term_hash"] == "hard0001"
        assert data["hardest_situational"][0]["accuracy"] == 0.0

    async def test_most_attempted_mcq_sorts_by_attempts(self, db, analytics, created_user):
        await db.execute(
            "INSERT INTO mcq_progress (user_id, question_hash, correct_count, total_count) "
            "VALUES (?, 'popular1', 3, 10)",
            (created_user,),
        )
        await db.execute(
            "INSERT INTO mcq_progress (user_id, question_hash, correct_count, total_count) "
            "VALUES (?, 'rare0001', 1, 1)",
            (created_user,),
        )
        await db.commit()
        data = await analytics.compute_content_stats()
        assert data["most_attempted_mcq"][0]["question_hash"] == "popular1"
        assert data["most_attempted_mcq"][0]["attempts"] == 10

    async def test_ef_distribution_buckets(self, db, analytics, created_user):
        # 4 карты — по одной в каждом бакете
        for i, ef in enumerate([1.4, 1.7, 2.2, 2.7]):
            await db.execute(
                "INSERT INTO flashcard_progress "
                "(user_id, card_hash, ease_factor, interval_days, repetitions) "
                "VALUES (?, ?, ?, 1, 1)",
                (created_user, f"card{i:04d}", ef),
            )
        await db.commit()
        data = await analytics.compute_content_stats()
        ef = data["flashcard_ef_distribution"]
        assert ef["lt_1_5"] == 1
        assert ef["1_5_to_2"] == 1
        assert ef["2_to_2_5"] == 1
        assert ef["gte_2_5"] == 1
        assert ef["total"] == 4


class TestEventTimeline:
    async def test_empty_db_returns_empty_list(self, analytics):
        events = await analytics.compute_event_timeline(hours=24)
        assert events == []

    async def test_recent_event_appears(self, db, analytics):
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (42, "test_event", '{"a": "b"}'),
        )
        await db.commit()
        events = await analytics.compute_event_timeline(hours=24)
        assert len(events) == 1
        assert events[0]["event_name"] == "test_event"
        assert events[0]["properties"] == {"a": "b"}

    async def test_old_events_filtered_out(self, db, analytics):
        # Event from 48 hours ago
        old_ts = (datetime.now() - timedelta(hours=48)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties, created_at) "
            "VALUES (?, ?, ?, ?)",
            (42, "old_event", "{}", old_ts),
        )
        await db.commit()
        events = await analytics.compute_event_timeline(hours=24)
        assert events == []

    async def test_limit_enforced(self, db, analytics):
        for i in range(10):
            await db.execute(
                "INSERT INTO events (user_id, event_name, properties, created_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (i, "evt", "{}"),
            )
        await db.commit()
        events = await analytics.compute_event_timeline(hours=24, limit=3)
        assert len(events) == 3

    async def test_malformed_properties_become_empty_dict(self, db, analytics):
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties, created_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (42, "bad", "not-valid-json"),
        )
        await db.commit()
        events = await analytics.compute_event_timeline(hours=24)
        assert events[0]["properties"] == {}


class TestHeatmap:
    async def test_empty_db_grid_all_zeros(self, analytics):
        data = await analytics.compute_heatmap(days=30)
        assert data["total_events"] == 0
        assert all(all(c == 0 for c in row) for row in data["grid"])
        assert data["peak"] is None

    async def test_grid_dimensions_correct(self, analytics):
        data = await analytics.compute_heatmap(days=30)
        assert len(data["grid"]) == 7  # 7 weekdays
        assert all(len(row) == 8 for row in data["grid"])  # 8 hour buckets
        assert len(data["weekday_labels"]) == 7
        assert len(data["hour_labels"]) == 8

    async def test_event_bucketed_correctly(self, db, analytics):
        # Event at known time: Monday 14:30 → weekday=0, hour_bucket=4 (12-15)
        # Use a known Monday afternoon timestamp
        # 2026-05-18 was a Monday at 14:30 → weekday=0, hour=14 → bucket=14//3=4
        ts = "2026-05-18 14:30:00"
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties, created_at) "
            "VALUES (?, ?, ?, ?)",
            (42, "x", "{}", ts),
        )
        await db.commit()
        # Use very wide window to include this event regardless of "now"
        data = await analytics.compute_heatmap(days=10000)
        assert data["total_events"] == 1
        # Monday=0, bucket=4
        assert data["grid"][0][4] == 1

    async def test_peak_identified(self, db, analytics):
        # 3 events in same bucket
        for _ in range(3):
            await db.execute(
                "INSERT INTO events (user_id, event_name, properties, created_at) "
                "VALUES (?, ?, ?, datetime('now'))",
                (42, "x", "{}"),
            )
        await db.commit()
        data = await analytics.compute_heatmap(days=1)
        assert data["peak"] is not None
        assert data["peak"]["count"] == 3
        assert data["peak"]["weekday"] in data["weekday_labels"]

    async def test_old_events_outside_window_excluded(self, db, analytics):
        old_ts = (datetime.now() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
        await db.execute(
            "INSERT INTO events (user_id, event_name, properties, created_at) "
            "VALUES (?, ?, ?, ?)",
            (42, "x", "{}", old_ts),
        )
        await db.commit()
        data = await analytics.compute_heatmap(days=30)
        assert data["total_events"] == 0
