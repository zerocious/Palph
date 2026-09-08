"""
Тесты LeaderboardService + новых read-методов LeaderboardRepository,
а также privacy-флага в UserRepository.

Покрывает Phase 2a (render + privacy + ranked-segment + badges) и
Phase 2b (run_rollover + ended-week computation):
- render_leaderboard (формат текста, auto-routing сегмента, скрытые юзеры)
- get_ranked_segment (фильтр сегмента, multiplier, hidden)
- get_user_rank
- award_badge (идемпотентность)
- get_active_badges (expiration)
- is_hidden_from_leaderboards / set_hidden_from_leaderboards
- run_rollover (top-3 main + breakthrough newbie + top-10% bonus,
  идемпотентность повторного запуска, hidden остаётся eligible)
- _compute_ended_week_iso (UTC anchor)
"""
from datetime import datetime, timedelta

import pytest
import pytest_asyncio

from repository import LeaderboardRepository
from services import LeaderboardService, user_calendar_keys, week_end_utc


# Ранжируемая неделя — ПРОШЛАЯ завершившаяся, а не фиксированная дата.
# Причина: _make_user стареет пользователя относительно datetime.now(),
# а сегмент newbie/main теперь считается на конец ранжируемой недели.
# С захардкоженной 2026-W21 данные становились невозможными — юзер,
# созданный «30 дней назад», набирал очки за неделю, закончившуюся за
# несколько месяцев ДО его регистрации. Раньше это не всплывало только
# потому, что сегмент брался по 'now'.
_LAST_WEEK = datetime.now() - timedelta(days=datetime.now().isoweekday() + 6)
WEEK = _LAST_WEEK.strftime("%G-W%V")


@pytest_asyncio.fixture
async def lb_repo(db):
    return LeaderboardRepository(db)


@pytest_asyncio.fixture
async def lb_service(user_repo, lb_repo):
    return LeaderboardService(user_repo, lb_repo)


@pytest_asyncio.fixture
async def lb_service_with_friends(user_repo, lb_repo, db):
    """LeaderboardService с friend_repo для тестов Phase 4."""
    from repository import FriendRepository
    fr = FriendRepository(db)
    return LeaderboardService(user_repo, lb_repo, friend_repo=fr)


async def _make_user(user_repo, db, uid, age_days, *, streak=0, hidden=False, username=None):
    """Создаёт user и сразу 'старит' его на N дней через UPDATE created_at."""
    await user_repo.create_user(uid, username=username)
    created_at = (datetime.now() - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE users SET created_at=?, current_streak=?, hidden_from_leaderboards=? "
        "WHERE user_id=?",
        (created_at, streak, 1 if hidden else 0, uid),
    )
    await db.commit()


async def _grant(lb_repo, uid, *, time=0, task=0, quiz=0, card=0, week_iso=WEEK):
    """Прямой write в weekly_scores для теста; обходит cap-логику."""
    if week_iso is None:
        local_date, week_iso = user_calendar_keys(datetime.now())
    else:
        local_date = "2026-05-18"
    await lb_repo._ensure_rows(uid, local_date, week_iso)
    await lb_repo.db.execute(
        "UPDATE weekly_scores SET time_pts=?, task_pts=?, quiz_pts=?, card_pts=? "
        "WHERE user_id=? AND week_iso=?",
        (time, task, quiz, card, uid, week_iso),
    )
    await lb_repo.db.commit()


# ============================================================
# LeaderboardRepository — get_ranked_segment, get_user_rank
# ============================================================
class TestGetRankedSegment:
    async def test_empty_segment(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30)
        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert ranked == []

    async def test_segment_filter_separates_newbie_main(
        self, lb_repo, user_repo, db
    ):
        # Two users — one fresh (newbie), one old (main); both have scores.
        await _make_user(user_repo, db, 1, age_days=2)   # newbie
        await _make_user(user_repo, db, 2, age_days=30)  # main
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        newbies = await lb_repo.get_ranked_segment(WEEK, "newbie")
        mains = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in newbies] == [1]
        assert [r["user_id"] for r in mains] == [2]

    async def test_sorts_by_total_final_with_multiplier(
        self, lb_repo, user_repo, db
    ):
        # User A: base 1000, streak 0 → final 1000
        # User B: base 900, streak 14 → final 1080
        # B should rank above A even though base is lower.
        await _make_user(user_repo, db, 1, age_days=30, streak=0)
        await _make_user(user_repo, db, 2, age_days=30, streak=14)
        await _grant(lb_repo, 1, task=1000)
        await _grant(lb_repo, 2, task=900)

        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in ranked] == [2, 1]
        assert ranked[0]["multiplier"] == 1.20
        assert ranked[0]["total_final"] == pytest.approx(1080.0)

    async def test_excludes_hidden_by_default(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)  # top by score, но скрытый

        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in ranked] == [1]

    async def test_includes_hidden_when_requested(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        ranked = await lb_repo.get_ranked_segment(
            WEEK, "main", exclude_hidden=False
        )
        # Сортировка по total_final: 2 (200) > 1 (100)
        assert [r["user_id"] for r in ranked] == [2, 1]
        # Hidden-флаг сохраняется в record'е
        assert ranked[0]["hidden"] is True
        assert ranked[1]["hidden"] is False

    async def test_ties_broken_deterministically_by_user_id(
        self, lb_repo, user_repo, db
    ):
        """
        При равных total_final порядок обязан быть детерминированным:
        от него зависит, кому run_rollover выдаст top_1, а кому top_2.
        Голый sort(reverse=True) стабилен и сохранял произвольный порядок
        строк SQLite — призы при ничьей распределялись случайно.
        """
        for uid in (3, 1, 2):  # намеренно не по возрастанию
            await _make_user(user_repo, db, uid, age_days=30, streak=0)
            await _grant(lb_repo, uid, task=100)

        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in ranked] == [1, 2, 3]
        assert len({r["total_final"] for r in ranked}) == 1  # ничья реальная

        # Повторный вызов даёт тот же порядок.
        again = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in again] == [1, 2, 3]

    async def test_tie_break_does_not_outrank_higher_score(
        self, lb_repo, user_repo, db
    ):
        """Tie-break вторичен: больший total_final всегда выше меньшего user_id."""
        await _make_user(user_repo, db, 1, age_days=30, streak=0)
        await _make_user(user_repo, db, 2, age_days=30, streak=0)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        ranked = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in ranked] == [2, 1]

    async def test_unknown_segment_raises(self, lb_repo):
        with pytest.raises(ValueError):
            await lb_repo.get_ranked_segment(WEEK, "premium")


class TestGetUserRank:
    async def test_returns_rank_and_entry(self, lb_repo, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30)
        await _make_user(user_repo, db, 2, age_days=30)
        await _make_user(user_repo, db, 3, age_days=30)
        await _grant(lb_repo, 1, task=300)
        await _grant(lb_repo, 2, task=200)
        await _grant(lb_repo, 3, task=100)

        rank, entry = await lb_repo.get_user_rank(2, WEEK, "main")
        assert rank == 2
        assert entry["user_id"] == 2

    async def test_returns_none_for_user_without_score(
        self, lb_repo, user_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        rank, entry = await lb_repo.get_user_rank(1, WEEK, "main")
        assert rank is None
        assert entry is None

    async def test_hidden_user_still_gets_rank(self, lb_repo, user_repo, db):
        """Hidden user видит свой rank — exclude_hidden=False внутри."""
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=200)

        rank, entry = await lb_repo.get_user_rank(2, WEEK, "main")
        assert rank == 1   # hidden user реально первый по очкам
        assert entry["hidden"] is True


# ============================================================
# award_badge / get_active_badges
# ============================================================
class TestAwardBadge:
    async def test_first_award_returns_true(self, lb_repo, created_user):
        result = await lb_repo.award_badge(created_user, "top_1", WEEK)
        assert result is True

    async def test_duplicate_award_returns_false(self, lb_repo, created_user):
        first = await lb_repo.award_badge(created_user, "top_1", WEEK)
        second = await lb_repo.award_badge(created_user, "top_1", WEEK)
        assert first is True
        assert second is False

    async def test_different_weeks_independent(self, lb_repo, created_user):
        assert await lb_repo.award_badge(created_user, "top_1", WEEK)
        assert await lb_repo.award_badge(created_user, "top_1", "2026-W22")


class TestGetActiveBadges:
    async def test_returns_non_expired(self, lb_repo, created_user):
        await lb_repo.award_badge(created_user, "top_1", WEEK)
        badges = await lb_repo.get_active_badges(created_user)
        assert len(badges) == 1
        assert badges[0]["badge_id"] == "top_1"

    async def test_excludes_expired(self, lb_repo, created_user, db):
        # Награждаем + сразу руками двигаем expires_at в прошлое
        await lb_repo.award_badge(created_user, "top_1", WEEK)
        await db.execute(
            "UPDATE weekly_badges SET expires_at = datetime('now', '-1 day') "
            "WHERE user_id=? AND badge_id=?",
            (created_user, "top_1"),
        )
        await db.commit()
        badges = await lb_repo.get_active_badges(created_user)
        assert badges == []


# ============================================================
# UserRepository — privacy column
# ============================================================
class TestPrivacyFlag:
    async def test_default_visible(self, user_repo, created_user):
        assert await user_repo.is_hidden_from_leaderboards(created_user) is False

    async def test_toggle_hidden(self, user_repo, created_user):
        await user_repo.set_hidden_from_leaderboards(created_user, True)
        assert await user_repo.is_hidden_from_leaderboards(created_user) is True
        await user_repo.set_hidden_from_leaderboards(created_user, False)
        assert await user_repo.is_hidden_from_leaderboards(created_user) is False

    async def test_nonexistent_user_returns_false(self, user_repo):
        # Defensive: нет user'а = считаем как не-скрытый (рендерить нечего)
        assert await user_repo.is_hidden_from_leaderboards(99999) is False


# ============================================================
# LeaderboardService.render_leaderboard
# ============================================================
class TestRenderLeaderboard:
    async def test_newbie_segment_for_fresh_user(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=2)
        await _grant(lb_repo, 1, task=80)
        text = await lb_service.render_leaderboard(1)
        assert "Новички" in text

    async def test_main_segment_for_old_user(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        await _grant(lb_repo, 1, task=80)
        text = await lb_service.render_leaderboard(1)
        assert "Основной" in text

    async def test_empty_segment_message(self, lb_service, user_repo, db):
        await _make_user(user_repo, db, 1, age_days=30)
        text = await lb_service.render_leaderboard(1)
        assert "никто не набрал" in text.lower() or "пока без очков" in text.lower()

    async def test_user_marked_in_top(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        await _grant(lb_repo, 1, task=200, week_iso=None)
        text = await lb_service.render_leaderboard(1)
        # Маркер для собственной строки
        assert "👤" in text
        assert "id=1" in text

    async def test_shows_username_when_stored(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30, username="alice")
        await _grant(lb_repo, 1, task=200, week_iso=None)
        text = await lb_service.render_leaderboard(1)
        assert "@alice" in text
        assert "id=1" not in text

    async def test_hidden_user_sees_own_rank_marker(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=100, week_iso=None)
        text = await lb_service.render_leaderboard(1)
        # Hidden user НЕ в публичном топе, но видит свой ранг с пометкой
        assert "Вы скрыты" in text

    async def test_hidden_user_not_in_others_top(
        self, lb_service, user_repo, lb_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30, hidden=False)
        await _make_user(user_repo, db, 2, age_days=30, hidden=True)
        await _grant(lb_repo, 1, task=50, week_iso=None)
        await _grant(lb_repo, 2, task=500, week_iso=None)  # Топ-1 по очкам, но скрытый
        # User 1 рендерит лидерборд — должен видеть себя первым, без user 2
        text = await lb_service.render_leaderboard(1)
        assert "id=1" in text
        assert "id=2" not in text


# ============================================================
# run_rollover — Phase 2b
# ============================================================
async def _get_badge_count(db, user_id):
    """Сколько активных бэджей у юзера."""
    async with db.execute(
        "SELECT COUNT(*) AS n FROM weekly_badges WHERE user_id=?",
        (user_id,),
    ) as c:
        return (await c.fetchone())["n"]


async def _get_coins(db, user_id):
    async with db.execute(
        "SELECT total_coins FROM users WHERE user_id=?", (user_id,)
    ) as c:
        row = await c.fetchone()
    return row["total_coins"] if row else 0


class TestSegmentAnchor:
    """
    Сегмент newbie/main обязан считаться НА КОНЕЦ ранжируемой недели.
    Rollover идёт во вторник за неделю, закончившуюся в воскресенье, —
    по 'now' пользователь, бывший newbie всю неделю, к моменту раздачи
    успевал стать main: «Прорыв недели» не доставался никому, а он сам
    вклинивался в топ-3 основного сегмента.
    """

    @staticmethod
    async def _newbie_of_past_week(user_repo, lb_repo, db, uid, weeks_ago=3):
        """
        Пользователь, который был newbie в течение недели `weeks_ago`
        назад, но давно перерос порог к сегодняшнему дню.
        Возвращает (week_iso, week_end).
        """
        monday = datetime.now() - timedelta(days=datetime.now().isoweekday() - 1)
        target = monday - timedelta(weeks=weeks_ago)
        week_iso = target.strftime("%G-W%V")
        week_end = week_end_utc(week_iso)
        # Зарегистрирован за 5 дней до конца той недели → newbie тогда,
        # но «сейчас» ему уже больше трёх недель → main.
        created = datetime.strptime(week_end, "%Y-%m-%d %H:%M:%S") - timedelta(days=5)
        await user_repo.create_user(uid)
        await db.execute(
            "UPDATE users SET created_at=? WHERE user_id=?",
            (created.strftime("%Y-%m-%d %H:%M:%S"), uid),
        )
        await db.commit()
        await _grant(lb_repo, uid, task=100, week_iso=week_iso)
        return week_iso, week_end

    async def test_same_user_flips_segment_with_anchor(
        self, lb_repo, user_repo, db
    ):
        """Один и тот же пользователь: по концу недели — newbie, по 'now' — main."""
        week_iso, week_end = await self._newbie_of_past_week(
            user_repo, lb_repo, db, 1
        )

        as_week_end = await lb_repo.get_ranked_segment(
            week_iso, "newbie", exclude_hidden=False, as_of=week_end
        )
        assert [r["user_id"] for r in as_week_end] == [1]

        # Без якоря (поведение до фикса) он уже main — и в newbie не попадает.
        as_now = await lb_repo.get_ranked_segment(
            week_iso, "newbie", exclude_hidden=False
        )
        assert as_now == []
        assert [
            r["user_id"]
            for r in await lb_repo.get_ranked_segment(
                week_iso, "main", exclude_hidden=False
            )
        ] == [1]

    async def test_rollover_awards_breakthrough_by_week_end(
        self, lb_service, lb_repo, user_repo, db
    ):
        """
        Итог для пользователя: он всю неделю соревновался среди новичков
        и обязан получить «Прорыв недели», а не top_1 основного сегмента.
        """
        week_iso, _ = await self._newbie_of_past_week(user_repo, lb_repo, db, 1)

        await lb_service.run_rollover(week_iso)

        badges = {b["badge_id"] for b in await lb_repo.get_active_badges(1)}
        assert "breakthrough" in badges, "newbie-приз ушёл мимо"
        assert "top_1" not in badges, "попал в основной сегмент вместо newbie"

    async def test_live_view_still_uses_now(self, lb_repo, user_repo, db):
        """
        as_of=None — дефолт: живой /leaderboard по-прежнему показывает
        сегмент на текущий момент, поведение не изменилось.
        """
        await _make_user(user_repo, db, 1, age_days=2)    # newbie сейчас
        await _make_user(user_repo, db, 2, age_days=30)   # main сейчас
        await _grant(lb_repo, 1, task=100)
        await _grant(lb_repo, 2, task=100)

        newbies = await lb_repo.get_ranked_segment(WEEK, "newbie")
        mains = await lb_repo.get_ranked_segment(WEEK, "main")
        assert [r["user_id"] for r in newbies] == [1]
        assert [r["user_id"] for r in mains] == [2]

    async def test_threshold_lives_in_one_constant(self):
        """Порог не должен снова расползтись копиями по SQL и сервису."""
        assert LeaderboardRepository.NEWBIE_MAX_AGE_DAYS == 7


class TestWeekEndUtc:
    def test_returns_sunday_end_of_day(self):
        # 2026-W37 — неделя с понедельника 2026-09-07 по воскресенье 13-е.
        assert week_end_utc("2026-W37") == "2026-09-13 23:59:59"

    def test_week_one_handles_year_boundary(self):
        assert week_end_utc("2026-W01") == "2026-01-04 23:59:59"

    def test_malformed_input_returns_none(self):
        for bad in ("", "мусор", "2026-W99", "2026", None):
            assert week_end_utc(bad) is None

    def test_format_matches_created_at(self):
        """Строка обязана сравниваться с users.created_at через julianday()."""
        value = week_end_utc("2026-W37")
        datetime.strptime(value, "%Y-%m-%d %H:%M:%S")  # не бросит — формат тот же


class TestRunRollover:
    async def test_empty_world_no_op(self, lb_service):
        """Никаких юзеров — rollover не падает и считает нули."""
        stats = await lb_service.run_rollover(WEEK)
        assert stats == {
            "week": WEEK,
            "badges_awarded": 0,
            "coins_distributed": 0,
            "segments_processed": 0,
        }

    async def test_main_top3_awarded(self, lb_service, user_repo, lb_repo, db):
        # 5 main-юзеров с разными очками
        for uid, pts in [(1, 500), (2, 400), (3, 300), (4, 200), (5, 100)]:
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=pts)

        stats = await lb_service.run_rollover(WEEK)
        # 3 топ-бэджа за main; newbie сегмент пустой
        assert stats["badges_awarded"] == 3
        assert stats["segments_processed"] == 1
        # top-10% не выдан (5 юзеров < MIN_SEGMENT_FOR_TOP10_BONUS=10)
        assert stats["coins_distributed"] == 0

        # User 1 = top_1, User 2 = top_2, User 3 = top_3
        async with db.execute(
            "SELECT user_id, badge_id FROM weekly_badges "
            "WHERE awarded_for_week=? ORDER BY user_id",
            (WEEK,),
        ) as c:
            rows = await c.fetchall()
        result = [(r["user_id"], r["badge_id"]) for r in rows]
        assert result == [(1, "top_1"), (2, "top_2"), (3, "top_3")]

    async def test_newbie_breakthrough_only(
        self, lb_service, user_repo, lb_repo, db
    ):
        # 3 новичка
        for uid, pts in [(1, 100), (2, 80), (3, 60)]:
            await _make_user(user_repo, db, uid, age_days=2)
            await _grant(lb_repo, uid, task=pts)

        stats = await lb_service.run_rollover(WEEK)
        # Только top-1 newbie получает breakthrough
        assert stats["badges_awarded"] == 1

        async with db.execute(
            "SELECT user_id, badge_id FROM weekly_badges WHERE awarded_for_week=?",
            (WEEK,),
        ) as c:
            rows = await c.fetchall()
        assert len(rows) == 1
        assert rows[0]["user_id"] == 1
        assert rows[0]["badge_id"] == "breakthrough"

    async def test_top10_pct_skipped_below_threshold(
        self, lb_service, user_repo, lb_repo, db
    ):
        """С 5 юзерами 10% = 0.5, скип — иначе bonus превращается в 'всем подряд'."""
        for uid in range(1, 6):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(6 - uid) * 100)

        stats = await lb_service.run_rollover(WEEK)
        assert stats["coins_distributed"] == 0
        # Никаких top10_pct_bonus бэджей
        async with db.execute(
            "SELECT COUNT(*) AS n FROM weekly_badges "
            "WHERE badge_id='top10_pct_bonus'"
        ) as c:
            assert (await c.fetchone())["n"] == 0

    async def test_top10_pct_awarded_at_threshold(
        self, lb_service, user_repo, lb_repo, db
    ):
        """10 юзеров → 10% = 1 человек получает coin-бонус."""
        for uid in range(1, 11):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(11 - uid) * 100)

        stats = await lb_service.run_rollover(WEEK)
        # 3 топ-бэджа + 1 top-10%
        assert stats["badges_awarded"] == 4
        assert stats["coins_distributed"] == LeaderboardService.COIN_BONUS_TOP10_PCT
        # User 1 — top_1 + top10_pct_bonus
        assert await _get_badge_count(db, 1) == 2
        assert await _get_coins(db, 1) == LeaderboardService.COIN_BONUS_TOP10_PCT

    async def test_top10_pct_floor_at_20_users(
        self, lb_service, user_repo, lb_repo, db
    ):
        """20 юзеров → 10% = 2 человека."""
        for uid in range(1, 21):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(21 - uid) * 100)

        stats = await lb_service.run_rollover(WEEK)
        # 3 top + 2 top-10%
        assert stats["badges_awarded"] == 5
        assert (
            stats["coins_distributed"]
            == 2 * LeaderboardService.COIN_BONUS_TOP10_PCT
        )

    async def test_idempotent_no_duplicate_badges(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Второй run_rollover для той же недели — no-op."""
        for uid in range(1, 11):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(11 - uid) * 100)

        await lb_service.run_rollover(WEEK)
        # Снова, той же неделей
        stats_2 = await lb_service.run_rollover(WEEK)
        # Все badges INSERT OR IGNORE — повторно ничего не вставилось
        assert stats_2["badges_awarded"] == 0
        assert stats_2["coins_distributed"] == 0

    async def test_idempotent_no_double_coins(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Повторный rollover не дублирует coin-бонус (rowcount-gated)."""
        for uid in range(1, 11):
            await _make_user(user_repo, db, uid, age_days=30)
            await _grant(lb_repo, uid, task=(11 - uid) * 100)

        await lb_service.run_rollover(WEEK)
        coins_after_first = await _get_coins(db, 1)
        await lb_service.run_rollover(WEEK)
        coins_after_second = await _get_coins(db, 1)
        assert coins_after_first == coins_after_second
        assert coins_after_first == LeaderboardService.COIN_BONUS_TOP10_PCT

    async def test_hidden_user_still_gets_rewards(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Hidden user не отображается публично, но rewards получает —
        по спеке (LEADERBOARD.md §Privacy)."""
        # User 1 — hidden, top score; user 2 — visible, second
        await _make_user(user_repo, db, 1, age_days=30, hidden=True)
        await _make_user(user_repo, db, 2, age_days=30, hidden=False)
        await _grant(lb_repo, 1, task=500)
        await _grant(lb_repo, 2, task=300)

        await lb_service.run_rollover(WEEK)
        # User 1 (hidden) должен иметь top_1 badge
        async with db.execute(
            "SELECT badge_id FROM weekly_badges WHERE user_id=?",
            (1,),
        ) as c:
            badges = [r["badge_id"] for r in await c.fetchall()]
        assert "top_1" in badges

    async def test_different_weeks_independent(
        self, lb_service, user_repo, lb_repo, db
    ):
        """Rollover для разных недель — независимое awarding."""
        await _make_user(user_repo, db, 1, age_days=30)
        await _grant(lb_repo, 1, task=100)

        await lb_service.run_rollover(WEEK)
        # Для следующей недели — другой week_iso, но weekly_scores не имеет
        # данных. Должно быть no-op.
        stats = await lb_service.run_rollover("2026-W22")
        assert stats["badges_awarded"] == 0
        # Прошлая неделя по-прежнему имеет ровно 1 badge (top_1)
        async with db.execute(
            "SELECT COUNT(*) AS n FROM weekly_badges WHERE awarded_for_week=?",
            (WEEK,),
        ) as c:
            assert (await c.fetchone())["n"] == 1


# ============================================================
# tasks._compute_ended_week_iso — UTC Tuesday anchor
# ============================================================
class TestComputeEndedWeekIso:
    def test_tuesday_anchor_returns_previous_week(self):
        from tasks import _compute_ended_week_iso
        # Tue 2026-05-19 00:00 UTC → ended week was 2026-W20 (Mon 11 - Sun 17)
        now = datetime(2026, 5, 19, 0, 0)
        assert _compute_ended_week_iso(now) == "2026-W20"

    def test_january_year_boundary(self):
        from tasks import _compute_ended_week_iso
        # Tue 2026-01-06 00:00 UTC → ended week = Mon 2025-12-29 - Sun 2026-01-04
        # ISO year of that week = 2026, week 1.
        now = datetime(2026, 1, 6, 0, 0)
        assert _compute_ended_week_iso(now) == "2026-W01"

    def test_late_december_iso_week_52(self):
        from tasks import _compute_ended_week_iso
        # Tue 2024-12-31 00:00 UTC → ended week = Mon 2024-12-23 - Sun 2024-12-29
        # ISO week 52 of 2024.
        now = datetime(2024, 12, 31, 0, 0)
        assert _compute_ended_week_iso(now) == "2024-W52"


# ============================================================
# Phase 3 — streak freeze repo methods
# ============================================================
class TestPurchaseFreeze:
    async def test_happy_path(self, lb_repo, user_repo, created_user, db):
        await user_repo.add_coins(created_user, 1000)
        result = await lb_repo.purchase_freeze(created_user, current_streak=5)
        assert result == "purchased"
        # Balance reduced by freeze_cost(5) = 500
        coins = (await user_repo.get_user(created_user))["total_coins"]
        assert coins == 500
        # Row in streak_freezes exists, unconsumed
        async with db.execute(
            "SELECT streak_at_grant, cost_paid, consumed_for_date "
            "FROM streak_freezes WHERE user_id=?",
            (created_user,),
        ) as c:
            row = await c.fetchone()
        assert row["streak_at_grant"] == 5
        assert row["cost_paid"] == 500
        assert row["consumed_for_date"] is None

    async def test_insufficient_coins(self, lb_repo, user_repo, created_user, db):
        # 100 coins, 5-day streak → cost=500 → insufficient
        await user_repo.add_coins(created_user, 100)
        result = await lb_repo.purchase_freeze(created_user, current_streak=5)
        assert result == "insufficient_coins"
        # Никаких изменений
        coins = (await user_repo.get_user(created_user))["total_coins"]
        assert coins == 100
        async with db.execute(
            "SELECT COUNT(*) AS n FROM streak_freezes WHERE user_id=?",
            (created_user,),
        ) as c:
            assert (await c.fetchone())["n"] == 0

    async def test_balance_exact_to_cost(
        self, lb_repo, user_repo, created_user
    ):
        """Баланс ровно равен цене — должна пройти, оставив 0."""
        await user_repo.add_coins(created_user, 500)
        result = await lb_repo.purchase_freeze(created_user, current_streak=3)
        assert result == "purchased"
        assert (await user_repo.get_user(created_user))["total_coins"] == 0

    async def test_cooldown_blocks_second_purchase(
        self, lb_repo, user_repo, created_user
    ):
        await user_repo.add_coins(created_user, 2000)
        first = await lb_repo.purchase_freeze(created_user, 5)
        assert first == "purchased"
        # Immediately try again
        second = await lb_repo.purchase_freeze(created_user, 5)
        assert second == "cooldown_active"
        # Никаких лишних строк/списаний
        async with lb_repo.db.execute(
            "SELECT COUNT(*) AS n FROM streak_freezes WHERE user_id=?",
            (created_user,),
        ) as c:
            assert (await c.fetchone())["n"] == 1
        # 1000 (1 freeze, cost 500), не 2 × 500 = 1000 списано — после ОДНОЙ покупки
        coins = (await user_repo.get_user(created_user))["total_coins"]
        assert coins == 2000 - 500

    async def test_cost_scales_with_streak(
        self, lb_repo, user_repo, created_user
    ):
        """freeze_cost(0) = 500, freeze_cost(8) = 750, freeze_cost(21) = 1000."""
        # 8-day streak → cost 750
        await user_repo.add_coins(created_user, 2000)
        result = await lb_repo.purchase_freeze(created_user, current_streak=8)
        assert result == "purchased"
        async with lb_repo.db.execute(
            "SELECT cost_paid FROM streak_freezes WHERE user_id=?",
            (created_user,),
        ) as c:
            assert (await c.fetchone())["cost_paid"] == 750


class TestHasActiveFreeze:
    async def test_no_freeze(self, lb_repo, created_user):
        assert await lb_repo.has_active_freeze(created_user) is False

    async def test_after_purchase(self, lb_repo, user_repo, created_user):
        await user_repo.add_coins(created_user, 1000)
        await lb_repo.purchase_freeze(created_user, 5)
        assert await lb_repo.has_active_freeze(created_user) is True

    async def test_after_consume(self, lb_repo, user_repo, created_user):
        await user_repo.add_coins(created_user, 1000)
        await lb_repo.purchase_freeze(created_user, 5)
        await lb_repo.consume_freeze_if_active(created_user, "2026-05-19")
        assert await lb_repo.has_active_freeze(created_user) is False


class TestConsumeFreezeIfActive:
    async def test_consume_active_freeze(
        self, lb_repo, user_repo, created_user, db
    ):
        await user_repo.add_coins(created_user, 1000)
        await lb_repo.purchase_freeze(created_user, 5)
        consumed = await lb_repo.consume_freeze_if_active(
            created_user, "2026-05-19"
        )
        assert consumed is True
        # consumed_for_date теперь установлен
        async with db.execute(
            "SELECT consumed_for_date FROM streak_freezes WHERE user_id=?",
            (created_user,),
        ) as c:
            row = await c.fetchone()
        assert row["consumed_for_date"] == "2026-05-19"

    async def test_consume_when_no_freeze(self, lb_repo, created_user):
        consumed = await lb_repo.consume_freeze_if_active(
            created_user, "2026-05-19"
        )
        assert consumed is False

    async def test_consume_idempotent(self, lb_repo, user_repo, created_user):
        await user_repo.add_coins(created_user, 1000)
        await lb_repo.purchase_freeze(created_user, 5)
        first = await lb_repo.consume_freeze_if_active(created_user, "2026-05-19")
        second = await lb_repo.consume_freeze_if_active(created_user, "2026-05-20")
        assert first is True
        assert second is False


class TestFreezeCooldown:
    async def test_no_cooldown_initially(self, lb_repo, created_user):
        assert await lb_repo.get_freeze_cooldown_remaining_days(created_user) == 0

    async def test_cooldown_active_after_purchase(
        self, lb_repo, user_repo, created_user
    ):
        await user_repo.add_coins(created_user, 1000)
        await lb_repo.purchase_freeze(created_user, 5)
        remaining = await lb_repo.get_freeze_cooldown_remaining_days(created_user)
        # 6 or 7 days remaining depending on rounding at the moment of the query
        assert 5 <= remaining <= 7


# ============================================================
# Phase 4 — render_friends_tab
# ============================================================
async def _add_friend_pair(db, friend_repo, a, b):
    """Helper: создаёт normalized дружбу через send + accept."""
    await friend_repo.send_request(a, b)
    await friend_repo.accept_request(a, b)


class TestRenderFriendsTab:
    async def test_no_friends_shows_hint(
        self, lb_service_with_friends, user_repo, db
    ):
        await _make_user(user_repo, db, 1, age_days=30)
        text = await lb_service_with_friends.render_friends_tab(1)
        assert "нет добавленных друзей" in text
        assert "/friends" in text

    async def test_single_friend_with_self(
        self, lb_service_with_friends, user_repo, lb_repo, db
    ):
        # Создаём двух пользователей и дружбу между ними
        await _make_user(user_repo, db, 1, age_days=30)
        await _make_user(user_repo, db, 2, age_days=30)
        fr = lb_service_with_friends.friend_repo
        await _add_friend_pair(db, fr, 1, 2)
        # Дать счёт на текущей неделе
        await _grant(lb_repo, 1, task=100, week_iso=None)
        await _grant(lb_repo, 2, task=200, week_iso=None)

        text = await lb_service_with_friends.render_friends_tab(1)
        # Обе строки присутствуют
        assert "id=1" in text
        assert "id=2" in text
        # Маркер собственной строки
        assert "(Вы)" in text
        # Top-2 ранжирование: medal для первого, второй без emoji
        assert "🥇" in text
        assert "🥈" in text

    async def test_friends_sorted_by_total_final(
        self, lb_service_with_friends, user_repo, lb_repo, db
    ):
        """User 1 видит друзей в порядке total_final desc (с multiplier)."""
        # User 1: streak 0, task=100 → final 100
        # User 2: streak 14, task=100 → final 120
        # User 3: streak 0, task=200 → final 200
        await _make_user(user_repo, db, 1, age_days=30, streak=0, username="usr1")
        await _make_user(user_repo, db, 2, age_days=30, streak=14, username="usr2")
        await _make_user(user_repo, db, 3, age_days=30, streak=0, username="usr3")
        fr = lb_service_with_friends.friend_repo
        await _add_friend_pair(db, fr, 1, 2)
        await _add_friend_pair(db, fr, 1, 3)
        await _grant(lb_repo, 1, task=100, week_iso=None)
        await _grant(lb_repo, 2, task=100, week_iso=None)
        await _grant(lb_repo, 3, task=200, week_iso=None)

        text = await lb_service_with_friends.render_friends_tab(1)
        # Порядок в тексте: 3 → 2 → 1 (200, 120, 100)
        pos_3 = text.find("@usr3")
        pos_2 = text.find("@usr2")
        pos_1 = text.find("@usr1")
        assert pos_3 < pos_2 < pos_1, f"Order wrong; got 3@{pos_3}, 2@{pos_2}, 1@{pos_1}"

    async def test_ties_broken_deterministically_by_user_id(
        self, lb_service_with_friends, user_repo, lb_repo, db
    ):
        """
        Ничья в friends-tab (типовой случай: начало недели, у всех 0 pts)
        обязана давать стабильный порядок — иначе медали 🥇🥈🥉 прыгают
        между показами. all_ids = friend_ids + [self], и стабильная
        сортировка сохраняла бы именно этот произвольный порядок.
        """
        for uid in (1, 2, 3):
            await _make_user(user_repo, db, uid, age_days=30, streak=0,
                             username=f"usr{uid}")
        fr = lb_service_with_friends.friend_repo
        await _add_friend_pair(db, fr, 1, 2)
        await _add_friend_pair(db, fr, 1, 3)
        # Никаких очков — у всех троих ровно 0.

        text = await lb_service_with_friends.render_friends_tab(1)
        order = [text.find(f"@usr{u}") for u in (1, 2, 3)]
        assert all(p >= 0 for p in order), text
        assert order == sorted(order), f"ожидали 1→2→3 при ничьей, got {order}"
        # Повторный показ — тот же порядок.
        assert await lb_service_with_friends.render_friends_tab(1) == text

    async def test_no_friend_repo_returns_message(self, lb_service):
        """Если LeaderboardService построен без friend_repo — graceful message."""
        text = await lb_service.render_friends_tab(1)
        assert "не настроен" in text.lower()
