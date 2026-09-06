"""
End-to-end integration tests across the full leaderboard stack.

Unit tests cover individual repo/service methods. These tests exercise
the FULL call chain: schema → grant_ → render → rollover → notification,
so cross-component contract bugs surface (e.g., if multiplier got
applied twice, or if a hidden user's score leaked into the rendered
public top).

Each test is one user-story scope, written as a self-contained
mini-scenario. Not exhaustive — focused on the most impactful flows
shipped in PR #3.
"""
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
import pytz

from repository import FriendRepository, LeaderboardRepository
from services import LeaderboardService, StreakService, user_calendar_keys


# Тесты не привязываются к абсолютной календарной дате (docs/testing.md §Время).
# `render_leaderboard` считает текущую ISO-неделю по TZ пользователя
# (дефолт — Europe/Moscow), поэтому якорь берётся от «сейчас» в этом TZ,
# а в код передаётся явным параметром `now_local=` каждого grant-вызова.
_ANCHOR_TZ = pytz.timezone("Europe/Moscow")
NOW = datetime.now(_ANCHOR_TZ).replace(hour=14, minute=30, second=0, microsecond=0)
TODAY, WEEK = user_calendar_keys(NOW)


@pytest_asyncio.fixture
async def lb_repo(db):
    return LeaderboardRepository(db)


@pytest_asyncio.fixture
async def friend_repo(db):
    return FriendRepository(db)


@pytest_asyncio.fixture
async def lb_service(user_repo, lb_repo, friend_repo):
    return LeaderboardService(user_repo, lb_repo, friend_repo=friend_repo)


async def _setup_user(user_repo, db, uid, age_days=30, streak=0, hidden=False, username=None):
    """Helper: создать user'а с заданным возрастом / streak / privacy / username."""
    await user_repo.create_user(uid, username=username)
    created_at = (datetime.now() - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    await db.execute(
        "UPDATE users SET created_at=?, current_streak=?, hidden_from_leaderboards=? "
        "WHERE user_id=?",
        (created_at, streak, 1 if hidden else 0, uid),
    )
    await db.commit()


# ============================================================
# 1. Full weekly-leaderboard journey: study → score → rollover → badge
# ============================================================
class TestFullLeaderboardJourney:
    async def test_one_user_full_week_to_badge(
        self, lb_repo, lb_service, user_repo, db
    ):
        """
        Один user main-сегмента, симулируем активность за неделю,
        проверяем точные scores, запускаем rollover, проверяем что top_1
        был выдан. End-to-end через grant_ → store → render_leaderboard
        → run_rollover → award_badge.
        """
        await _setup_user(user_repo, db, 1, age_days=30, streak=10)

        # День — 60 мин учёбы (tier 1, все 60 минут × 1.0 = 60 pts)
        pts_time = await lb_repo.grant_time_pts(1, 60, now_local=NOW)
        assert pts_time == 60.0

        # 3 math task'а правильно → 3 × 40 = 120 pts
        for _ in range(3):
            assert await lb_repo.grant_task_pts(1, now_local=NOW) is True

        # 5 quiz correct: 5 × 5 = 25 pts + bonus на 3-м = +15 = 40 quiz pts
        bonus_count = 0
        for _ in range(5):
            pts, bonus = await lb_repo.grant_quiz_pts_correct(1, now_local=NOW)
            if bonus:
                bonus_count += 1
        assert bonus_count == 1  # ровно на 3-м

        # 8 cards (3 new + 5 review) → 3×3 + 5×5 = 9 + 25 = 34
        for _ in range(3):
            await lb_repo.grant_card_pts(1, is_new=True, now_local=NOW)
        for _ in range(5):
            await lb_repo.grant_card_pts(1, is_new=False, now_local=NOW)

        # Проверка точных компонентов weekly_scores
        ws = await lb_repo.get_weekly_score(1, WEEK)
        assert ws["time_pts"] == 60.0
        assert ws["task_pts"] == 120
        assert ws["quiz_pts"] == 40
        assert ws["card_pts"] == 34

        # Render должен показать user'а
        text = await lb_service.render_leaderboard(1)
        assert "id=1" in text
        # multiplier 10-day streak = 1.10 → total_final = 254 × 1.10 = 279.4 → display "279"
        # (наш render использует :.0f)
        assert "279" in text

        # Rollover → top_1 badge (1 user в сегменте = top-1 место для main)
        stats = await lb_service.run_rollover(WEEK)
        assert stats["badges_awarded"] == 1  # только top_1, top-10% нужен ≥10

        badges = await lb_repo.get_active_badges(1)
        badge_ids = [b["badge_id"] for b in badges]
        assert "top_1" in badge_ids


# ============================================================
# 2. Friends full lifecycle: send → accept → both sides → render → remove
# ============================================================
class TestFriendsFullLifecycle:
    async def test_send_accept_friendship_render_remove(
        self, friend_repo, user_repo, lb_repo, lb_service, db
    ):
        await _setup_user(user_repo, db, 1, age_days=30)
        await _setup_user(user_repo, db, 2, age_days=30)

        # Alice (1) отправляет request → Bob (2)
        assert await friend_repo.send_request(1, 2) == "sent"
        assert await friend_repo.are_friends(1, 2) is False

        # Pending request виден Bob'у
        pending = await friend_repo.get_pending_received(2)
        assert len(pending) == 1
        assert pending[0]["from_user_id"] == 1

        # Bob принимает
        assert await friend_repo.accept_request(1, 2) is True

        # Дружба обоюдная
        assert await friend_repo.are_friends(1, 2) is True
        assert await friend_repo.are_friends(2, 1) is True  # symmetric
        assert await friend_repo.get_friends(1) == [2]
        assert await friend_repo.get_friends(2) == [1]
        # Request больше нет в pending
        assert await friend_repo.get_pending_received(2) == []

        # Дадим обоим очки и проверим render_friends_tab
        await lb_repo.grant_task_pts(1, now_local=NOW)
        await lb_repo.grant_task_pts(1, now_local=NOW)  # 80 pts
        await lb_repo.grant_task_pts(2, now_local=NOW)  # 40 pts
        text = await lb_service.render_friends_tab(1)
        # Alice впереди, sorted DESC
        pos_1 = text.find("id=1")
        pos_2 = text.find("id=2")
        assert 0 <= pos_1 < pos_2
        # Маркер собственной строки Alice
        assert "(Вы)" in text

        # Bob удаляет дружбу
        assert await friend_repo.remove_friend(2, 1) is True
        assert await friend_repo.are_friends(1, 2) is False
        assert await friend_repo.get_friends(1) == []
        assert await friend_repo.get_friends(2) == []


# ============================================================
# 3. Streak freeze full cycle: buy → consume on miss → reset after exhaustion
# ============================================================
class TestStreakFreezeFullCycle:
    async def test_buy_consume_then_reset(
        self, lb_repo, user_repo, db
    ):
        await _setup_user(user_repo, db, 1, age_days=30, streak=5)
        await user_repo.add_coins(1, 1000)

        bot = AsyncMock()
        bot.send_message = AsyncMock()
        ss = StreakService(user_repo, bot=bot, leaderboard_repo=lb_repo)

        # 1. Покупаем freeze (cost 500 для streak=5)
        assert await lb_repo.purchase_freeze(1, current_streak=5) == "purchased"
        assert (await user_repo.get_user(1))["total_coins"] == 500
        assert await lb_repo.has_active_freeze(1) is True

        # 2. Day 1: missed day → freeze consumed, streak preserved
        # (process_users_in_timezone использует today_local внутри)
        await ss.process_users_in_timezone("Europe/Moscow")
        u = await user_repo.get_user(1)
        assert u["current_streak"] == 5  # сохранён
        assert await lb_repo.has_active_freeze(1) is False  # потреблён
        # Уведомление пользователю отправлено
        bot.send_message.assert_called()

        # 3. Day 2: again missed → no freeze, streak resets
        await user_repo.db.execute(
            "UPDATE users SET last_streak_check_date = '2000-01-01' WHERE user_id = ?",
            (1,),
        )
        await user_repo.db.commit()
        await ss.process_users_in_timezone("Europe/Moscow")
        u = await user_repo.get_user(1)
        assert u["current_streak"] == 0  # сброшен

    async def test_freeze_cooldown_blocks_immediate_repurchase(
        self, lb_repo, user_repo, db
    ):
        await _setup_user(user_repo, db, 1, age_days=30, streak=5)
        await user_repo.add_coins(1, 2000)

        assert await lb_repo.purchase_freeze(1, 5) == "purchased"
        # Сразу же ещё одна попытка — cooldown
        assert await lb_repo.purchase_freeze(1, 5) == "cooldown_active"
        # Только 500 списано, не 1000
        assert (await user_repo.get_user(1))["total_coins"] == 1500


# ============================================================
# 4. Privacy: hidden user invisible to others, visible to self with marker
# ============================================================
class TestPrivacyEndToEnd:
    async def test_hidden_user_excluded_from_public_top_but_self_sees_marker(
        self, lb_repo, lb_service, user_repo, db
    ):
        # User 1 — visible top scorer; User 2 — hidden top scorer
        await _setup_user(user_repo, db, 1, age_days=30, hidden=False)
        await _setup_user(user_repo, db, 2, age_days=30, hidden=True)

        # User 2 имеет больший score
        await lb_repo.grant_task_pts(1, now_local=NOW)  # 40
        for _ in range(3):
            await lb_repo.grant_task_pts(2, now_local=NOW)  # 120

        # Public render для User 1 — User 2 не показывается, User 1 топ
        text_for_1 = await lb_service.render_leaderboard(1)
        assert "id=1" in text_for_1
        assert "id=2" not in text_for_1

        # Render для User 2 (он сам) — показывает свой rank с маркером "Вы скрыты"
        text_for_2 = await lb_service.render_leaderboard(2)
        assert "Вы скрыты" in text_for_2
        # User 2 видит себя в собственном render'е (как rank-info)
        assert "id=2" in text_for_2 or "Ваш ранг" in text_for_2

    async def test_hidden_user_still_earns_rollover_rewards(
        self, lb_repo, lb_service, user_repo, db
    ):
        """Privacy НЕ запрещает rewards — hidden user всё равно top-1 если заслужил."""
        await _setup_user(user_repo, db, 1, age_days=30, hidden=True)
        await _setup_user(user_repo, db, 2, age_days=30, hidden=False)
        await lb_repo.grant_task_pts(1, now_local=NOW)
        await lb_repo.grant_task_pts(1, now_local=NOW)
        await lb_repo.grant_task_pts(2, now_local=NOW)

        await lb_service.run_rollover(WEEK)

        # User 1 (hidden) получил top_1 badge
        badges_1 = await lb_repo.get_active_badges(1)
        assert any(b["badge_id"] == "top_1" for b in badges_1)
        # User 2 получил top_2
        badges_2 = await lb_repo.get_active_badges(2)
        assert any(b["badge_id"] == "top_2" for b in badges_2)


# ============================================================
# 5. Username search end-to-end: cached via create_user → findable
# ============================================================
class TestUsernameSearchEndToEnd:
    async def test_user_findable_by_handle_immediately_after_creation(
        self, user_repo, friend_repo, db
    ):
        """
        Имитация first-message gap fix: user создан с username,
        find_user_id_by_username сразу находит его. Это контракт,
        обеспеченный create_user(uid, username=...).
        """
        await user_repo.create_user(42, username="alice")
        assert await user_repo.find_user_id_by_username("alice") == 42

    async def test_friend_add_by_username_end_to_end(
        self, user_repo, friend_repo, db
    ):
        """
        Полный flow: alice создана с username, bob ищет её по @handle,
        отправляет request, alice принимает. После — оба видят друг друга.
        """
        from services import parse_friend_query

        # Setup: оба пользователя есть, у alice есть @handle
        await user_repo.create_user(1, username="alice")  # Alice
        await user_repo.create_user(2, username="bob")    # Bob

        # Bob набирает "@alice" → парсер → lookup → user_id
        username, target_id = parse_friend_query("@alice")
        assert username == "alice"
        assert target_id is None
        target_id = await user_repo.find_user_id_by_username(username)
        assert target_id == 1

        # Bob отправляет request
        assert await friend_repo.send_request(2, target_id) == "sent"

        # Alice принимает
        assert await friend_repo.accept_request(2, 1) is True
        assert await friend_repo.are_friends(1, 2) is True


# ============================================================
# 6. Multi-user leaderboard: multiplier-driven reordering
# ============================================================
class TestMultiUserMultiplierReordering:
    async def test_streak_multiplier_can_flip_top_3(
        self, lb_repo, lb_service, user_repo, db
    ):
        """
        Три юзера: high base + 0 streak, mid base + 14 streak, low base + 0.
        ORDER BY total_base дал бы A > B > C, но total_final flip'ит до
        B > A > C. Тест проверяет что render и rollover оба видят правильный
        порядок (read-time multiplier applied).
        """
        # User A: base 1000, streak 0 → final 1000
        # User B: base 900, streak 14 → final 1080
        # User C: base 500, streak 0 → final 500
        await _setup_user(user_repo, db, 1, age_days=30, streak=0)
        await _setup_user(user_repo, db, 2, age_days=30, streak=14)
        await _setup_user(user_repo, db, 3, age_days=30, streak=0)

        # Direct write для точных чисел
        await lb_repo._ensure_rows(1, TODAY, WEEK)
        await lb_repo._ensure_rows(2, TODAY, WEEK)
        await lb_repo._ensure_rows(3, TODAY, WEEK)
        await db.execute(
            "UPDATE weekly_scores SET task_pts=? WHERE user_id=? AND week_iso=?",
            (1000, 1, WEEK),
        )
        await db.execute(
            "UPDATE weekly_scores SET task_pts=? WHERE user_id=? AND week_iso=?",
            (900, 2, WEEK),
        )
        await db.execute(
            "UPDATE weekly_scores SET task_pts=? WHERE user_id=? AND week_iso=?",
            (500, 3, WEEK),
        )
        await db.commit()

        # Render: B должен быть выше A
        text = await lb_service.render_leaderboard(1)
        pos_2 = text.find("id=2")
        pos_1 = text.find("id=1")
        pos_3 = text.find("id=3")
        assert 0 <= pos_2 < pos_1 < pos_3, (
            f"Order wrong: 2@{pos_2}, 1@{pos_1}, 3@{pos_3}"
        )

        # Rollover: B получает top_1, A → top_2, C → top_3
        await lb_service.run_rollover(WEEK)
        async with db.execute(
            "SELECT user_id, badge_id FROM weekly_badges "
            "WHERE awarded_for_week=? ORDER BY badge_id",
            (WEEK,),
        ) as c:
            rows = await c.fetchall()
        result = [(r["user_id"], r["badge_id"]) for r in rows]
        # Sorted alphabetically by badge_id: top_1, top_2, top_3
        assert result == [(2, "top_1"), (1, "top_2"), (3, "top_3")]
