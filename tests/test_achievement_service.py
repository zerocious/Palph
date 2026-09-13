"""
Тесты AchievementService — выдача достижений на основе sessions/streak/minutes.

Каталог достижений берётся из реального achievements.json — не моков. Это
гарантирует что тесты сломаются если кто-то меняет каталог без оглядки на
сервисную логику.
"""
import pytest
import pytest_asyncio

from services import AchievementService


@pytest_asyncio.fixture
async def ach_service(user_repo, achievements_catalog):
    return AchievementService(user_repo, achievements_catalog)


class TestFirstSession:
    async def test_awarded_on_session_1(self, ach_service, created_user):
        new_ids, bonus = await ach_service.check_and_award(
            user_id=created_user, sessions=1, streak=0, total_minutes=10
        )
        assert "first_session" in new_ids
        assert bonus > 0

    async def test_not_re_awarded(self, ach_service, created_user):
        await ach_service.check_and_award(created_user, sessions=1, streak=0, total_minutes=10)
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=2, streak=0, total_minutes=20)
        assert "first_session" not in new_ids


class TestStreakAchievements:
    async def test_3_day_streak_not_at_2(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=2, streak=2, total_minutes=20)
        assert "3_day_streak" not in new_ids

    async def test_3_day_streak_at_3(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=3, streak=3, total_minutes=30)
        assert "3_day_streak" in new_ids

    async def test_7_day_streak_at_7(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=7, streak=7, total_minutes=70)
        assert "7_day_streak" in new_ids

    async def test_14_day_streak_at_14(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=14, streak=14, total_minutes=140)
        assert "14_day_streak" in new_ids


class TestSessionCountAchievements:
    async def test_10_sessions(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=10, streak=0, total_minutes=10)
        assert "10_sessions" in new_ids

    async def test_30_sessions(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=30, streak=0, total_minutes=30)
        assert "30_sessions" in new_ids

    async def test_9_sessions_no_10_session_ach(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=9, streak=0, total_minutes=9)
        assert "10_sessions" not in new_ids


class TestMinutesAchievements:
    async def test_100_minutes(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=4, streak=0, total_minutes=100)
        assert "100_minutes" in new_ids

    async def test_300_minutes(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=10, streak=0, total_minutes=300)
        assert "300_minutes" in new_ids

    async def test_99_minutes_no_100_ach(self, ach_service, created_user):
        new_ids, _ = await ach_service.check_and_award(created_user, sessions=4, streak=0, total_minutes=99)
        assert "100_minutes" not in new_ids


class TestMultipleAwardsInOneCall:
    async def test_all_thresholds_at_once(self, ach_service, created_user, achievements_catalog):
        """Когда пользователь скачком сразу пробивает несколько порогов."""
        new_ids, bonus = await ach_service.check_and_award(
            user_id=created_user, sessions=10, streak=7, total_minutes=100
        )
        # Ожидаем: first_session, 3_day_streak, 7_day_streak, 10_sessions, 100_minutes
        expected = {"first_session", "3_day_streak", "7_day_streak", "10_sessions", "100_minutes"}
        assert expected.issubset(set(new_ids))
        # Сумма наград = сумма по каталогу
        expected_bonus = sum(achievements_catalog[a]["reward"] for a in expected)
        assert bonus == expected_bonus


class TestNoAchievements:
    async def test_below_all_thresholds(self, ach_service, created_user):
        """sessions=0, streak=0, minutes=0 — никаких ачивок."""
        new_ids, bonus = await ach_service.check_and_award(
            user_id=created_user, sessions=0, streak=0, total_minutes=0
        )
        assert new_ids == []
        assert bonus == 0


# ============================================================
# Прогресс достижений — то, что рисует экран «🏆 Достижения»
# ============================================================
async def _progress_row(user_repo, uid, ach_id):
    async with user_repo.db.execute(
        "SELECT progress, target, completed FROM user_achievements "
        "WHERE user_id=? AND achievement_id=?",
        (uid, ach_id),
    ) as c:
        return await c.fetchone()


class TestProgressTracking:
    async def test_session_achievements_show_progress(
        self, ach_service, user_repo, created_user
    ):
        """
        У «сессионных» ачивок не было ветки обновления прогресса, строка в
        user_achievements не создавалась до самой выдачи — и экран
        достижений показывал «🔒 ЗАБЛОКИРОВАНО» человеку с 9 сессиями из
        10, тогда как минутные и стриковые честно показывали «45/100».
        """
        await ach_service.check_and_award(
            user_id=created_user, sessions=9, streak=1, total_minutes=45
        )
        for ach_id, target in (("10_sessions", 10), ("30_sessions", 30)):
            row = await _progress_row(user_repo, created_user, ach_id)
            assert row is not None, f"{ach_id}: строки прогресса нет"
            assert row["progress"] == 9
            assert row["target"] == target
            assert not row["completed"]

    async def test_every_unfinished_achievement_has_a_row(
        self, ach_service, user_repo, created_user, achievements_catalog
    ):
        """
        Ни одна ачивка из правил не должна остаться без прогресса —
        иначе на экране она молча выглядит недоступной.
        """
        await ach_service.check_and_award(
            user_id=created_user, sessions=2, streak=1, total_minutes=20
        )
        async with user_repo.db.execute(
            "SELECT achievement_id FROM user_achievements WHERE user_id=?",
            (created_user,),
        ) as c:
            have = {r["achievement_id"] for r in await c.fetchall()}
        expected = {
            ach_id
            for ach_id, _metric, _target in AchievementService.ACHIEVEMENT_RULES
            if ach_id in achievements_catalog
        }
        assert expected <= have, f"без строки прогресса: {expected - have}"

    async def test_progress_matches_its_metric(
        self, ach_service, user_repo, created_user
    ):
        """Каждому правилу — своя метрика, а не чужая."""
        await ach_service.check_and_award(
            user_id=created_user, sessions=4, streak=2, total_minutes=70
        )
        assert (await _progress_row(user_repo, created_user, "10_sessions"))["progress"] == 4
        assert (await _progress_row(user_repo, created_user, "3_day_streak"))["progress"] == 2
        assert (await _progress_row(user_repo, created_user, "100_minutes"))["progress"] == 70

    async def test_completed_row_is_not_downgraded(
        self, ach_service, user_repo, created_user
    ):
        """Выданную ачивку повторный проход не сбрасывает в прогресс."""
        await ach_service.check_and_award(
            user_id=created_user, sessions=10, streak=0, total_minutes=10
        )
        assert (await _progress_row(user_repo, created_user, "10_sessions"))["completed"]
        await ach_service.check_and_award(
            user_id=created_user, sessions=1, streak=0, total_minutes=10
        )
        row = await _progress_row(user_repo, created_user, "10_sessions")
        assert row["completed"], "выданная ачивка откатилась в незавершённую"

    async def test_unknown_achievement_id_is_skipped(self, user_repo, created_user):
        """
        Ачивка, которой нет в каталоге, пропускается. Раньше такой id
        сначала записывался в user_achievements, а потом ронял сессию на
        KeyError в _get_reward.
        """
        service = AchievementService(user_repo, {})  # пустой каталог
        new_ids, bonus = await service.check_and_award(
            user_id=created_user, sessions=100, streak=100, total_minutes=10000
        )
        assert new_ids == [] and bonus == 0

    async def test_award_order_follows_rules_table(self, ach_service, created_user):
        """Порядок выдачи = порядок таблицы: он попадает в уведомление."""
        new_ids, _ = await ach_service.check_and_award(
            user_id=created_user, sessions=30, streak=14, total_minutes=750
        )
        order = [a for a, _m, _t in AchievementService.ACHIEVEMENT_RULES]
        assert new_ids == [a for a in order if a in new_ids]

