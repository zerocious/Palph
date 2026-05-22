"""
Anti-spam for free-text messages forwarded to admins: 1 per user per 60s.
Uses the same UserRateLimiter instance config as bot.admin_message_limiter.
"""
import time

from bot import admin_message_limiter
from services import UserRateLimiter


class TestAdminMessageLimiterConfig:
    def test_matches_bot_instance(self):
        assert isinstance(admin_message_limiter, UserRateLimiter)
        assert admin_message_limiter.max_actions == 1
        assert admin_message_limiter.window_seconds == 60
        assert admin_message_limiter.warn_threshold == 1.0


class TestAdminMessageLimiterBehavior:
    def setup_method(self):
        admin_message_limiter.reset(user_id=42)

    def test_first_message_allowed(self):
        assert admin_message_limiter.check(user_id=42) == "ok"

    def test_second_within_window_blocked(self):
        assert admin_message_limiter.check(user_id=42) == "ok"
        assert admin_message_limiter.check(user_id=42) == "block"

    def test_after_window_expires_allowed_again(self):
        rl = UserRateLimiter(max_actions=1, window_seconds=1, warn_threshold=1.0)
        assert rl.check(user_id=99) == "ok"
        assert rl.check(user_id=99) == "block"
        time.sleep(1.1)
        assert rl.check(user_id=99) == "ok"

    def test_users_isolated(self):
        admin_message_limiter.reset(user_id=1)
        admin_message_limiter.reset(user_id=2)
        assert admin_message_limiter.check(user_id=1) == "ok"
        assert admin_message_limiter.check(user_id=1) == "block"
        assert admin_message_limiter.check(user_id=2) == "ok"
