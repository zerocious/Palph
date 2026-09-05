"""
HTTP API для desktop-клиента (api.py).

Поднимаем реальный aiohttp-сервер на временном порту поверх тестовой БД —
проверяем именно то, что увидит Windows-приложение: коды ошибок, форму
JSON, работу Bearer-авторизации и rate-limit на обмене кода.
"""
from __future__ import annotations

import pytest
import pytest_asyncio
from aiohttp.test_utils import TestClient, TestServer

from api import APP_LIMITER, LinkAttemptLimiter, create_app
from repository import (
    DesktopTimerRepository, DeviceRepository, PetRepository, SessionRepository,
    UserRepository,
)
from services import AchievementService, StudyService


@pytest_asyncio.fixture
async def api_env(db, achievements_catalog):
    """(client, device_repo, user_id) — сервер с одним заведённым пользователем."""
    user_repo = UserRepository(db)
    device_repo = DeviceRepository(db)
    pet_repo = PetRepository(db)
    session_repo = SessionRepository(db)
    ach_service = AchievementService(user_repo, achievements_catalog)
    await user_repo.create_user(77)

    app = create_app(
        user_repo=user_repo,
        session_repo=session_repo,
        pet_repo=pet_repo,
        device_repo=device_repo,
        timer_repo=DesktopTimerRepository(db),
        ach_service=ach_service,
        study_service=StudyService(user_repo, session_repo, ach_service, pet_repo),
        achievements=achievements_catalog,
    )
    client = TestClient(TestServer(app))
    await client.start_server()
    try:
        yield client, device_repo, 77
    finally:
        await client.close()


async def _link(client, device_repo, user_id, device_name="Тестовый ПК") -> str:
    """Проходит полный flow привязки и возвращает токен."""
    code = await device_repo.create_link_code(user_id)
    resp = await client.post(
        "/auth/link",
        json={"code": DeviceRepository.format_code(code), "device_name": device_name},
    )
    assert resp.status == 200
    return (await resp.json())["token"]


class TestHealthAndAuth:
    async def test_health_needs_no_token(self, api_env):
        client, _, _ = api_env
        resp = await client.get("/health")
        assert resp.status == 200
        assert (await resp.json())["status"] == "ok"

    async def test_all_api_routes_require_auth(self, api_env):
        """
        Сторож: новый /api/*-эндпоинт без @require_auth сразу валит тест.
        Обходим реальную таблицу маршрутов, а не список из головы.
        """
        client, _, _ = api_env
        checked = 0
        for route in client.app.router.routes():
            path = route.resource.canonical
            if not path.startswith("/api/") or route.method == "OPTIONS":
                continue
            resp = await client.request(route.method, path)
            assert resp.status == 401, f"{route.method} {path} отдал {resp.status}"
            checked += 1
        assert checked >= 5

    async def test_api_requires_token(self, api_env):
        client, _, _ = api_env
        resp = await client.get("/api/me")
        assert resp.status == 401
        assert "error" in await resp.json()

    async def test_invalid_token_rejected(self, api_env):
        client, _, _ = api_env
        resp = await client.get("/api/me", headers={"Authorization": "Bearer nope"})
        assert resp.status == 401

    async def test_non_bearer_scheme_rejected(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        resp = await client.get("/api/me", headers={"Authorization": f"Basic {token}"})
        assert resp.status == 401

    async def test_revoked_token_stops_working(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        assert (await client.get("/api/me", headers=headers)).status == 200

        await device_repo.revoke_all(user_id)
        assert (await client.get("/api/me", headers=headers)).status == 401


class TestLinkEndpoint:
    async def test_valid_code_returns_token_and_user(self, api_env):
        client, device_repo, user_id = api_env
        code = await device_repo.create_link_code(user_id)
        resp = await client.post("/auth/link", json={"code": code})
        assert resp.status == 200
        body = await resp.json()
        assert body["user_id"] == user_id
        assert await device_repo.resolve_token(body["token"]) == user_id

    async def test_wrong_code_is_401(self, api_env):
        client, device_repo, user_id = api_env
        await device_repo.create_link_code(user_id)
        resp = await client.post("/auth/link", json={"code": "ZZZZZZZZ"})
        assert resp.status == 401

    async def test_code_cannot_be_reused(self, api_env):
        client, device_repo, user_id = api_env
        code = await device_repo.create_link_code(user_id)
        assert (await client.post("/auth/link", json={"code": code})).status == 200
        assert (await client.post("/auth/link", json={"code": code})).status == 401

    @pytest.mark.parametrize("payload", [{}, {"code": ""}, {"code": 12345678}])
    async def test_bad_payloads_are_400(self, api_env, payload):
        client, _, _ = api_env
        resp = await client.post("/auth/link", json=payload)
        assert resp.status == 400

    async def test_malformed_json_is_400(self, api_env):
        client, _, _ = api_env
        resp = await client.post(
            "/auth/link",
            data="not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_device_name_reaches_devices_list(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id, device_name="Ноутбук Алисы")
        resp = await client.get("/api/devices", headers={"Authorization": f"Bearer {token}"})
        devices = (await resp.json())["devices"]
        assert [d["name"] for d in devices] == ["Ноутбук Алисы"]
        assert devices[0]["current"] is True
        # Наружу отдаём короткий id, а не полный hash токена.
        assert len(devices[0]["id"]) == 12

    async def test_brute_force_is_rate_limited(self, api_env):
        client, _, _ = api_env
        client.app[APP_LIMITER] = LinkAttemptLimiter(max_attempts=3, window_seconds=300)
        for _ in range(3):
            assert (await client.post("/auth/link", json={"code": "AAAAAAAA"})).status == 401
        assert (await client.post("/auth/link", json={"code": "AAAAAAAA"})).status == 429

    async def test_successful_links_do_not_consume_limit(self, api_env):
        """
        За reverse-proxy/NAT все пользователи приходят с одного IP —
        удачные привязки не должны приближать общий блок.
        """
        client, device_repo, user_id = api_env
        client.app[APP_LIMITER] = LinkAttemptLimiter(max_attempts=2, window_seconds=300)
        for _ in range(5):
            await _link(client, device_repo, user_id)
        # Бюджет неудач не тронут: первый неверный код всё ещё 401, а не 429.
        assert (await client.post("/auth/link", json={"code": "AAAAAAAA"})).status == 401


class TestProfileEndpoints:
    async def test_me_returns_profile_and_pet(self, api_env, db):
        client, device_repo, user_id = api_env
        await UserRepository(db).add_coins(user_id, 120)
        token = await _link(client, device_repo, user_id)

        resp = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert body["user_id"] == user_id
        assert body["coins"] == 120
        assert body["streak"] == 0
        assert body["total_minutes"] == 0
        # Питомца ещё нет (создаётся на первой сессии) — отдаём дефолт.
        assert body["pet"]["level"] == 1
        assert body["pet"]["emotion"] in {"joy", "sad", "neutral"}

    async def test_me_404_when_user_row_gone(self, api_env, db):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        # Обычно удаление аккаунта сносит токены по CASCADE; выключаем FK,
        # чтобы проверить defensive-ветку «токен жив, пользователя нет».
        await db.execute("PRAGMA foreign_keys=OFF")
        await db.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        await db.commit()
        await db.execute("PRAGMA foreign_keys=ON")

        resp = await client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 404

    async def test_pet_endpoint_returns_inventory(self, api_env, db):
        client, device_repo, user_id = api_env
        await PetRepository(db).create_pet_with_defaults(user_id)
        token = await _link(client, device_repo, user_id)

        resp = await client.get("/api/pet", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        assert body["pet"]["color"] == "orange"
        owned = {(i["item_type"], i["item_value"]) for i in body["inventory"]}
        assert ("color", "orange") in owned and ("accessory", "none") in owned

    async def test_achievements_include_untouched_ones(self, api_env, achievements_catalog):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)

        resp = await client.get(
            "/api/achievements", headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 200
        items = (await resp.json())["achievements"]
        assert len(items) == len(achievements_catalog)
        assert all(item["completed"] is False for item in items)
        assert all({"id", "name", "icon", "reward"} <= set(item) for item in items)

    async def test_logout_revokes_only_current_device(self, api_env):
        client, device_repo, user_id = api_env
        first = await _link(client, device_repo, user_id, device_name="ПК")
        second = await _link(client, device_repo, user_id, device_name="Ноутбук")

        resp = await client.post(
            "/api/logout", headers={"Authorization": f"Bearer {first}"},
        )
        assert resp.status == 200
        assert (await resp.json())["revoked"] is True
        assert await device_repo.resolve_token(first) is None
        assert await device_repo.resolve_token(second) == user_id


class TestCors:
    async def test_preflight_allows_tauri_origin(self, api_env):
        client, _, _ = api_env
        resp = await client.options(
            "/api/me",
            headers={"Origin": "tauri://localhost", "Access-Control-Request-Method": "GET"},
        )
        assert resp.status == 204
        assert resp.headers["Access-Control-Allow-Origin"] == "*"
        assert "Authorization" in resp.headers["Access-Control-Allow-Headers"]

    async def test_cors_headers_on_real_response(self, api_env):
        client, _, _ = api_env
        resp = await client.get("/health", headers={"Origin": "tauri://localhost"})
        assert resp.headers["Access-Control-Allow-Origin"] == "*"


class TestPomodoro:
    """
    Таймер desktop-приложения. Главное свойство: время считает сервер,
    поэтому клиент не может «завершить» длинную сессию мгновенно.
    """

    async def test_no_timer_initially(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        resp = await client.get("/api/pomodoro", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        assert (await resp.json())["timer"] is None

    async def test_start_returns_countdown(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        resp = await client.post(
            "/api/pomodoro/start",
            json={"minutes": 25},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 200
        timer = (await resp.json())["timer"]
        assert timer["duration_minutes"] == 25
        assert 1490 <= timer["remaining_seconds"] <= 1500

    async def test_duration_is_clamped(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        for requested, expected in ((1, 5), (9999, 120)):
            resp = await client.post(
                "/api/pomodoro/start", json={"minutes": requested}, headers=headers,
            )
            assert (await resp.json())["timer"]["duration_minutes"] == expected

    async def test_bad_minutes_is_400(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        for payload in ({"minutes": "25"}, {"minutes": True}, {"minutes": 12.5}):
            resp = await client.post("/api/pomodoro/start", json=payload, headers=headers)
            assert resp.status == 400

    async def test_finish_without_timer_is_409(self, api_env):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        resp = await client.post(
            "/api/pomodoro/finish", headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 409

    async def test_instant_finish_earns_nothing(self, api_env, db):
        """Старт и сразу финиш — прошло 0 минут, монет быть не должно."""
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        await client.post("/api/pomodoro/start", json={"minutes": 25}, headers=headers)

        resp = await client.post("/api/pomodoro/finish", headers=headers)
        body = await resp.json()
        assert body["counted"] is False
        assert body["coins_earned"] == 0
        assert (await UserRepository(db).get_user(user_id))["total_coins"] == 0
        # Таймер снят — повторный финиш уже 409.
        assert (await client.post("/api/pomodoro/finish", headers=headers)).status == 409

    async def test_elapsed_time_is_credited_like_in_the_bot(self, api_env, db):
        """
        Отматываем started_at на 30 минут назад при заявленных 25 —
        засчитаться должны 25 (не больше длительности), с монетами,
        сессией, XP питомца и флагом стрика, как у телеграмного таймера.
        """
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        await client.post("/api/pomodoro/start", json={"minutes": 25}, headers=headers)
        await db.execute(
            "UPDATE desktop_timers SET started_at = datetime('now', '-30 minutes')"
        )
        await db.commit()

        body = await (await client.post("/api/pomodoro/finish", headers=headers)).json()
        assert body["counted"] is True
        assert body["minutes"] == 25

        user = await UserRepository(db).get_user(user_id)
        assert user["total_coins"] == body["coins_earned"]
        assert user["total_sessions"] == 1
        assert bool(user["has_studied_today"]) is True
        assert (await PetRepository(db).get_pet(user_id))["xp"] == 25
        assert await SessionRepository(db).get_total_minutes(user_id) == 25

    async def test_partial_session_credits_only_elapsed(self, api_env, db):
        """Досрочная остановка засчитывает реально прошедшее время."""
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        await client.post("/api/pomodoro/start", json={"minutes": 60}, headers=headers)
        await db.execute(
            "UPDATE desktop_timers SET started_at = datetime('now', '-7 minutes')"
        )
        await db.commit()

        body = await (await client.post("/api/pomodoro/finish", headers=headers)).json()
        assert body["minutes"] == 7

    async def test_restart_resets_the_countdown(self, api_env, db):
        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        await client.post("/api/pomodoro/start", json={"minutes": 25}, headers=headers)
        await db.execute(
            "UPDATE desktop_timers SET started_at = datetime('now', '-20 minutes')"
        )
        await db.commit()

        await client.post("/api/pomodoro/start", json={"minutes": 25}, headers=headers)
        timer = (await (await client.get("/api/pomodoro", headers=headers)).json())["timer"]
        assert timer["elapsed_seconds"] <= 5

    async def test_timer_is_per_user(self, api_env, db):
        """Таймер одного пользователя не виден другому."""
        client, device_repo, user_id = api_env
        other = 88
        await UserRepository(db).create_user(other)
        token = await _link(client, device_repo, user_id)
        other_token = (await device_repo.exchange_code(
            await device_repo.create_link_code(other)
        )).token

        await client.post(
            "/api/pomodoro/start", json={"minutes": 25},
            headers={"Authorization": f"Bearer {token}"},
        )
        resp = await client.get(
            "/api/pomodoro", headers={"Authorization": f"Bearer {other_token}"},
        )
        assert (await resp.json())["timer"] is None

    async def test_concurrent_finish_credits_one_session(self, api_env, db):
        """
        Регрессия: двойной клик по «Завершить» (или ручное завершение
        одновременно с автоматическим по истечении времени) засчитывал
        одну сессию дважды — это и портило статистику, и позволяло
        накручивать монеты.
        """
        import asyncio

        client, device_repo, user_id = api_env
        token = await _link(client, device_repo, user_id)
        headers = {"Authorization": f"Bearer {token}"}
        await client.post("/api/pomodoro/start", json={"minutes": 25}, headers=headers)
        await db.execute(
            "UPDATE desktop_timers SET started_at = datetime('now', '-30 minutes')"
        )
        await db.commit()

        responses = await asyncio.gather(*[
            client.post("/api/pomodoro/finish", headers=headers) for _ in range(5)
        ])
        statuses = sorted(r.status for r in responses)
        assert statuses == [200, 409, 409, 409, 409]

        user = await UserRepository(db).get_user(user_id)
        assert user["total_sessions"] == 1
        assert await SessionRepository(db).get_total_minutes(user_id) == 25
