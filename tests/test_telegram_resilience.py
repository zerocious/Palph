import asyncio

import pytest
from aiogram.exceptions import TelegramRetryAfter

from services import (
    TelegramSendBreaker,
    TelegramSendBreakerOpen,
    _send_with_retry_after,
    _telegram_send_breaker,
)


@pytest.fixture(autouse=True)
def reset_breaker():
    _telegram_send_breaker._failures = 0
    _telegram_send_breaker._open_until = 0.0
    yield
    _telegram_send_breaker._failures = 0
    _telegram_send_breaker._open_until = 0.0


@pytest.mark.asyncio
async def test_send_retries_transient_errors(monkeypatch):
    calls = {"n": 0}

    async def flaky_send():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("temporary")
        return "ok"

    async def _noop_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    result = await _send_with_retry_after(flaky_send, label="test", uid=1)
    assert result == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_send_retries_telegram_retry_after(monkeypatch):
    calls = {"n": 0}

    class FakeRetryAfter(TelegramRetryAfter):
        def __init__(self):
            self.method = "sendMessage"
            self.message = "retry"
            self.retry_after = 0

    async def rate_limited_send():
        calls["n"] += 1
        if calls["n"] == 1:
            raise FakeRetryAfter()
        return "sent"

    async def _noop_sleep(*_args, **_kwargs):
        return None

    monkeypatch.setattr(asyncio, "sleep", _noop_sleep)
    result = await _send_with_retry_after(rate_limited_send, label="test", uid=2)
    assert result == "sent"
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_breaker_opens_after_threshold():
    breaker = TelegramSendBreaker(failure_threshold=2, cooldown_seconds=60)
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_failure()
    assert breaker.allow() is False


@pytest.mark.asyncio
async def test_send_skips_when_breaker_open(monkeypatch):
    _telegram_send_breaker._open_until = float("inf")
    with pytest.raises(TelegramSendBreakerOpen):
        await _send_with_retry_after(lambda: asyncio.sleep(0), label="test", uid=3)
