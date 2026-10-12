"""
Стрик держит не только Pomodoro, но и учебная активность за день.

Раньше users.has_studied_today ставился только в increment_sessions, то
есть только таймером. Час карточек и задач давал очки в лидерборд, но
стрик горел, а вечером приходило «питомец грустит, ты сегодня не
учился». Теперь достаточно STREAK_ACTIVITY_THRESHOLD засчитанных действий
за день (верные ответы квиз/MCQ, решённые задачи, успешные карточки).
Хук стоит в успешной ветке grant_*, поэтому покрывает все точки
начисления сразу.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio

from repository import LeaderboardRepository
from services import StreakService
from tests.conftest import user_now

NOW = user_now().replace(hour=14, minute=30, second=0, microsecond=0)
THRESHOLD = LeaderboardRepository.STREAK_ACTIVITY_THRESHOLD


@pytest_asyncio.fixture
async def lb_repo(db):
    return LeaderboardRepository(db)


async def _studied(user_repo, uid) -> bool:
    return bool((await user_repo.get_user(uid))["has_studied_today"])


async def _quiz(lb_repo, uid, n, now=NOW):
    for _ in range(n):
        await lb_repo.grant_quiz_pts_correct(uid, now_local=now)


async def _cards(lb_repo, uid, n, now=NOW):
    for _ in range(n):
        await lb_repo.grant_card_pts(uid, is_new=False, now_local=now)


async def _tasks(lb_repo, uid, n, now=NOW):
    for _ in range(n):
        await lb_repo.grant_task_pts(uid, now_local=now)


class TestThreshold:
    async def test_below_threshold_does_not_count(self, lb_repo, user_repo, created_user):
        await _quiz(lb_repo, created_user, THRESHOLD - 1)
        assert not await _studied(user_repo, created_user)

    async def test_reaching_threshold_counts(self, lb_repo, user_repo, created_user):
        await _quiz(lb_repo, created_user, THRESHOLD)
        assert await _studied(user_repo, created_user)

    async def test_flips_exactly_on_the_threshold_action(
        self, lb_repo, user_repo, created_user
    ):
        for i in range(1, THRESHOLD + 1):
            await lb_repo.grant_quiz_pts_correct(created_user, now_local=NOW)
            assert await _studied(user_repo, created_user) == (i >= THRESHOLD)

    async def test_threshold_is_reachable_by_every_single_kind(self):
        """
        Если дневной кап вида активности ниже порога, этим видом одним
        стрик не удержать — это был бы тихий пробел. Капы: задачи 5,
        карточки 8, квизы 25.
        """
        assert THRESHOLD <= 5, "порог выше капа задач — задачами одними не набрать"


class TestEveryKindOfActivity:
    async def test_quiz_alone(self, lb_repo, user_repo, created_user):
        await _quiz(lb_repo, created_user, THRESHOLD)
        assert await _studied(user_repo, created_user)

    async def test_cards_alone(self, lb_repo, user_repo, created_user):
        await _cards(lb_repo, created_user, THRESHOLD)
        assert await _studied(user_repo, created_user)

    async def test_tasks_alone(self, lb_repo, user_repo, created_user):
        await _tasks(lb_repo, created_user, THRESHOLD)
        assert await _studied(user_repo, created_user)

    async def test_kinds_add_up(self, lb_repo, user_repo, created_user):
        """Три задачи + две карточки — учебный день, хотя ни один вид порога не достиг."""
        await _tasks(lb_repo, created_user, 3)
        await _cards(lb_repo, created_user, THRESHOLD - 3)
        assert await _studied(user_repo, created_user)


class TestWhatDoesNotCount:
    async def test_wrong_answers_do_not_count(self, lb_repo, user_repo, created_user):
        """Неверные ответы счётчик не двигают — иначе стрик держал бы тап наугад."""
        for _ in range(THRESHOLD * 3):
            await lb_repo.reset_quiz_series(created_user, now_local=NOW)
        assert not await _studied(user_repo, created_user)

    async def test_yesterdays_activity_does_not_count_today(
        self, lb_repo, user_repo, created_user
    ):
        yesterday = NOW - timedelta(days=1)
        await _quiz(lb_repo, created_user, THRESHOLD - 1, now=yesterday)
        await _quiz(lb_repo, created_user, 1, now=NOW)  # сегодня всего одно
        assert not await _studied(user_repo, created_user)

    async def test_other_users_activity_does_not_leak(
        self, lb_repo, user_repo, created_user
    ):
        await user_repo.create_user(999)
        await _quiz(lb_repo, 999, THRESHOLD * 2)
        assert await _studied(user_repo, 999)
        assert not await _studied(user_repo, created_user)


class TestIdempotentAndSafe:
    async def test_already_studied_stays_studied(self, lb_repo, user_repo, created_user):
        await user_repo.set_has_studied_today(created_user, True)
        await _quiz(lb_repo, created_user, 1)
        assert await _studied(user_repo, created_user)

    async def test_activity_is_committed_with_points(
        self, lb_repo, user_repo, created_user, db
    ):
        """Флаг и очки пишутся одной транзакцией: не бывает флага без очков."""
        await _quiz(lb_repo, created_user, THRESHOLD)
        async with db.execute(
            "SELECT quiz_pts FROM weekly_scores WHERE user_id=?", (created_user,)
        ) as c:
            row = await c.fetchone()
        assert row["quiz_pts"] > 0 and await _studied(user_repo, created_user)

    async def test_capped_actions_do_not_error(self, lb_repo, user_repo, created_user):
        """За капом (задачи: 5/день) вызовы остаются безвредными."""
        await _tasks(lb_repo, created_user, 9)
        assert await _studied(user_repo, created_user)


class TestStreakAndReminders:
    async def test_activity_only_day_extends_streak(
        self, lb_repo, user_repo, created_user
    ):
        """Главный сценарий: ни одной Pomodoro-сессии, но стрик живёт."""
        svc = StreakService(user_repo, bot=AsyncMock())
        await user_repo.set_streak(created_user, 4)
        await _cards(lb_repo, created_user, THRESHOLD)
        await svc.process_all_users()
        assert (await user_repo.get_user(created_user))["current_streak"] == 5

    async def test_without_activity_streak_still_resets(
        self, lb_repo, user_repo, created_user
    ):
        """Регрессия наоборот: порог не превратил стрик в вечный."""
        svc = StreakService(user_repo, bot=AsyncMock())
        await user_repo.set_streak(created_user, 4)
        await _cards(lb_repo, created_user, THRESHOLD - 1)
        await svc.process_all_users()
        assert (await user_repo.get_user(created_user))["current_streak"] == 0

    async def test_evening_reminder_skips_active_user(
        self, lb_repo, user_repo, created_user
    ):
        """Кто уже позанимался карточками, вечернее «ты не учился» не получает."""
        tz = (await user_repo.get_user(created_user))["timezone"]
        await user_repo.db.execute(
            "UPDATE notification_settings SET evening_enabled=1, evening_time='21:00' "
            "WHERE user_id=?", (created_user,),
        )
        await user_repo.db.commit()
        before = await user_repo.get_users_due_for_evening(tz, "21:00")
        assert created_user in [u["user_id"] for u in before]

        await _cards(lb_repo, created_user, THRESHOLD)
        after = await user_repo.get_users_due_for_evening(tz, "21:00")
        assert created_user not in [u["user_id"] for u in after]


class TestUserFacingTextMatchesRule:
    """Порог «5» живёт и в коде, и в текстах — смена одного не должна
    молча оставлять устаревшим другое."""

    @pytest.mark.parametrize("locale", ["ru", "en"])
    def test_faq_states_the_real_threshold(self, locale):
        from i18n import t
        body = t("faq.earn_coins.body", locale)
        assert str(THRESHOLD) in body, (
            f"FAQ ({locale}) не называет порог {THRESHOLD}"
        )

    @pytest.mark.parametrize("locale", ["ru", "en"])
    @pytest.mark.parametrize("key", ["reminders.evening_sad", "reminders.evening_fallback"])
    def test_evening_reminders_mention_both_ways(self, locale, key):
        from i18n import t
        text = t(key, locale)
        # и таймер, и «ответы/задания/карточки» — иначе напоминание
        # снова обещает, что стрик держит только таймер.
        timer = "таймер" if locale == "ru" else "timer"
        other = ("карточ", "заданий") if locale == "ru" else ("cards", "tasks")
        assert timer in text.lower()
        assert any(w in text.lower() for w in other)
