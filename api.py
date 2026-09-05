# api.py
"""
HTTP API для desktop-клиента Palph (Windows-приложение).

Живёт в том же asyncio-процессе, что и бот: те же репозитории/сервисы,
то же соединение с SQLite (один writer + общий db.lock). Поэтому нет ни
дублирования бизнес-логики, ни гонки двух процессов за один файл БД.

Транспорт — aiohttp.web: он уже в дереве зависимостей (его тянет aiogram),
так что API не добавляет ни одной новой зависимости в production-образ.

Авторизация: Bearer-токен устройства (см. repository.DeviceRepository),
навешивается декоратором @require_auth на каждый /api/*-эндпоинт. Токен
выдаётся в обмен на одноразовый код из бота (/link_app) — паролей и
отдельной регистрации нет, единственный источник идентичности остаётся
Telegram.

Эндпоинты:
    GET  /health              — liveness, без авторизации
    POST /auth/link           — код привязки → токен устройства
    GET  /api/me              — профиль: монеты, стрик, сессии, питомец
    GET  /api/pet             — питомец + инвентарь
    GET  /api/pet/image       — PNG-арт питомца (тот же asset, что в боте)
    GET  /api/achievements    — каталог достижений + прогресс пользователя
    GET  /api/devices         — привязанные устройства
    POST /api/logout          — отзыв текущего токена

По умолчанию выключен: bot.main() поднимает сервер через
start_api_server() только при API_ENABLED=1 в окружении.
"""
from __future__ import annotations

import functools
import json
import logging
import os
from collections import defaultdict, deque
from datetime import datetime, timedelta
from time import monotonic
from typing import NamedTuple

import pytz
from aiohttp import web

from services import derive_emotion, render_pet

logger = logging.getLogger("studybuddy_bot")

DEVICE_NAME_MAX_LEN = 64
MAX_BODY_BYTES = 4 * 1024


class AuthContext(NamedTuple):
    """Кто и с какого устройства пришёл — результат @require_auth."""

    user_id: int
    token: str


class LinkAttemptLimiter:
    """
    Anti-brute-force для POST /auth/link: sliding-window по IP.

    Код привязки живёт 10 минут и берётся из 30-символьного алфавита
    (~6.5e11 вариантов), так что перебор и без лимита нереалистичен —
    лимитер защищает от «шумного» клиента и от попыток перебирать
    короткие коды, если TTL/длина когда-нибудь изменятся.

    Считаются только НЕУДАЧНЫЕ попытки: за reverse-proxy (или NAT) все
    пользователи приходят с одного IP, и общий счётчик, куда попадали бы
    и успешные привязки, выдавал бы 429 людям, которые всё делают верно.

    Не персистится: после рестарта окна пустые (как UserRateLimiter).
    """

    def __init__(self, max_attempts: int = 10, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._buckets: dict[str, deque] = defaultdict(deque)

    def _prune(self, ip: str) -> deque:
        bucket = self._buckets[ip]
        cutoff = monotonic() - self.window_seconds
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        return bucket

    def is_blocked(self, ip: str) -> bool:
        """Исчерпан ли лимит неудач для этого IP (без регистрации попытки)."""
        return len(self._prune(ip)) >= self.max_attempts

    def register_failure(self, ip: str) -> None:
        """Отмечает неверный код — только такие попытки приближают блок."""
        self._prune(ip).append(monotonic())


# Ключи app-словаря: зависимости живут в самом Application, чтобы handler'ы
# не тянули глобалы из bot.py и поднимались в тестах отдельно от Telegram.
# web.AppKey (а не голая строка) — типизированный доступ без NotAppKeyWarning;
# есть во всех aiohttp, которые допускает aiogram (>= 3.9).
APP_DEPS = web.AppKey("palph_deps", dict)
APP_LIMITER = web.AppKey("palph_link_limiter", LinkAttemptLimiter)


# ------------------------------------------------------------
# Хелперы ответов
# ------------------------------------------------------------
def _json(payload: dict, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _error(message: str, status: int) -> web.Response:
    return _json({"error": message}, status=status)


async def _read_json(request: web.Request) -> dict:
    """
    Читает JSON-тело с ограничением размера. Пустое тело → {} (caller сам
    решает, какие поля обязательны); слишком большое или не разбираемое
    как JSON-объект → ValueError, который handler превращает в 400.
    """
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise ValueError("body too large")
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("malformed json")
    if not isinstance(data, dict):
        raise ValueError("json object expected")
    return data


def _client_ip(request: web.Request) -> str:
    """
    IP клиента с учётом reverse-proxy. X-Forwarded-For доверяем только
    если API стоит за прокси (API_TRUST_PROXY=1) — иначе заголовок
    подделывается клиентом и обходит rate-limit.
    """
    if os.getenv("API_TRUST_PROXY", "0") == "1":
        forwarded = request.headers.get("X-Forwarded-For", "")
        if forwarded:
            return forwarded.split(",")[0].strip()
    peername = request.remote
    return peername or "unknown"


def _bearer_token(request: web.Request) -> str:
    header = request.headers.get("Authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header[7:].strip()


# ------------------------------------------------------------
# Middlewares
# ------------------------------------------------------------
@web.middleware
async def cors_middleware(request: web.Request, handler):
    """
    CORS для WebView Tauri (origin вида tauri://localhost) и dev-сервера
    фронтенда (http://localhost:5173).

    Разрешаем '*' по умолчанию: авторизация здесь bearer-токеном, не
    cookie, поэтому браузер не может «случайно» отправить чужие
    креденшелы с другого сайта. Список сужается через API_CORS_ORIGINS.
    """
    allowed = os.getenv("API_CORS_ORIGINS", "*")
    origin = request.headers.get("Origin", "")
    if allowed == "*":
        allow_origin = "*"
    else:
        permitted = {o.strip() for o in allowed.split(",") if o.strip()}
        allow_origin = origin if origin in permitted else ""

    if request.method == "OPTIONS":
        response = web.Response(status=204)
    else:
        response = await handler(request)

    if allow_origin:
        response.headers["Access-Control-Allow-Origin"] = allow_origin
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
        response.headers["Access-Control-Max-Age"] = "600"
        if allow_origin != "*":
            response.headers["Vary"] = "Origin"
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    """
    Единый JSON-формат ошибок. Внутренние исключения логируются с
    traceback'ом, но наружу уходит только «internal error» — клиенту
    незачем видеть структуру БД.
    """
    try:
        return await handler(request)
    except web.HTTPException as e:
        if e.status >= 500:
            logger.warning("api.http_error path=%s status=%s", request.path, e.status)
        return _error(e.reason or "error", e.status)
    except Exception as e:
        logger.exception(
            "api.unhandled path=%s method=%s reason=%s",
            request.path, request.method, type(e).__name__,
        )
        return _error("internal error", 500)


def require_auth(handler):
    """
    Bearer-авторизация для одного эндпоинта.

    Обёрнутый handler получает вторым аргументом AuthContext, поэтому
    забыть авторизацию и «случайно» прочитать чужой user_id нельзя:
    без декоратора у handler'а просто нет откуда взять user_id.
    Тест test_all_api_routes_require_auth сторожит, что декоратор стоит
    на всех /api/*-маршрутах.
    """
    @functools.wraps(handler)
    async def wrapper(request: web.Request) -> web.Response:
        token = _bearer_token(request)
        if not token:
            return _error("authorization required", 401)
        deps = request.app[APP_DEPS]
        user_id = await deps["device_repo"].resolve_token(token)
        if user_id is None:
            return _error("invalid or revoked token", 401)
        return await handler(request, AuthContext(user_id=user_id, token=token))

    return wrapper


# ------------------------------------------------------------
# Handlers
# ------------------------------------------------------------
async def handle_health(request: web.Request) -> web.Response:
    """Liveness-probe: поднят ли API (без обращения к БД)."""
    return _json({"status": "ok", "service": "palph-api"})


async def handle_auth_link(request: web.Request) -> web.Response:
    """
    Обмен одноразового кода из /link_app на долгоживущий токен устройства.

    Body: {"code": "ABCD-EFGH", "device_name": "Мой ноутбук"}
    200:  {"token": "...", "user_id": 123}
    401:  код неверный, протух или уже использован
    429:  слишком много неверных кодов с этого IP
    """
    ip = _client_ip(request)
    limiter = request.app[APP_LIMITER]
    if limiter.is_blocked(ip):
        logger.warning("api.link_rate_limited ip=%s", ip)
        return _error("too many attempts, try again later", 429)

    try:
        body = await _read_json(request)
    except ValueError as e:
        return _error(str(e), 400)

    code = body.get("code")
    if not isinstance(code, str) or not code.strip():
        return _error("field 'code' is required", 400)
    device_name = body.get("device_name")
    if not isinstance(device_name, str):
        device_name = "Desktop"

    deps = request.app[APP_DEPS]
    link = await deps["device_repo"].exchange_code(
        code, device_name=device_name[:DEVICE_NAME_MAX_LEN],
    )
    if link is None:
        limiter.register_failure(ip)
        logger.info("api.link_failed ip=%s", ip)
        return _error("invalid or expired code", 401)

    return _json({"token": link.token, "user_id": link.user_id})


@require_auth
async def handle_me(request: web.Request, auth: AuthContext) -> web.Response:
    """Профиль для главного экрана приложения: монеты, стрик, сессии, питомец."""
    deps = request.app[APP_DEPS]
    user_id = auth.user_id

    user = await deps["user_repo"].get_user(user_id)
    if user is None:
        # Токен валиден, но пользователя уже нет (удалил аккаунт в боте).
        return _error("user not found", 404)

    total_minutes = await deps["session_repo"].get_total_minutes(user_id)
    pet = await deps["pet_repo"].get_pet(user_id)
    emotion, now_local = _pet_emotion(user, pet)

    return _json({
        "user_id": user_id,
        "coins": user["total_coins"],
        "streak": user["current_streak"],
        "total_sessions": user["total_sessions"],
        "total_minutes": total_minutes,
        "has_studied_today": bool(user["has_studied_today"]),
        "timezone": user["timezone"],
        "locale": user["locale"] or "ru",
        "last_session": user["last_session"],
        "local_time": now_local.isoformat(),
        "pet": _pet_payload(pet, emotion),
    })


@require_auth
async def handle_pet(request: web.Request, auth: AuthContext) -> web.Response:
    """Питомец + купленный инвентарь (для экрана кастомизации)."""
    deps = request.app[APP_DEPS]
    user_id = auth.user_id

    user = await deps["user_repo"].get_user(user_id)
    pet = await deps["pet_repo"].get_pet(user_id)
    emotion, _ = _pet_emotion(user, pet)
    inventory = await deps["pet_repo"].get_inventory(user_id)

    return _json({
        "pet": _pet_payload(pet, emotion),
        "inventory": [dict(item) for item in inventory],
    })


@require_auth
async def handle_pet_image(request: web.Request, auth: AuthContext) -> web.Response:
    """
    PNG-арт питомца — тот же asset, что бот шлёт в Telegram, чтобы
    приложение и чат показывали одного и того же питомца.
    """
    deps = request.app[APP_DEPS]
    user_id = auth.user_id

    user = await deps["user_repo"].get_user(user_id)
    pet = await deps["pet_repo"].get_pet(user_id)
    emotion, now_local = _pet_emotion(user, pet)
    try:
        path = render_pet(pet, emotion, now_local=now_local)
    except FileNotFoundError:
        return _error("pet asset not found", 404)
    return web.FileResponse(path)


@require_auth
async def handle_achievements(request: web.Request, auth: AuthContext) -> web.Response:
    """
    Каталог достижений с прогрессом пользователя. Достижения, к которым
    пользователь не приступал, отдаются с progress=0 и completed=false —
    клиенту не нужно знать про «строки нет в таблице».
    """
    deps = request.app[APP_DEPS]
    user_id = auth.user_id

    progress = await deps["ach_service"].list_progress(user_id)
    items = []
    for ach_id, definition in deps["achievements"].items():
        state = progress.get(ach_id, {})
        items.append({
            "id": ach_id,
            "name": definition.get("name", ach_id),
            "description": definition.get("description", ""),
            "icon": definition.get("icon", "🏆"),
            "reward": definition.get("reward", 0),
            "completed": bool(state.get("completed", False)),
            "progress": state.get("progress", 0),
            "target": state.get("target", 0),
        })
    return _json({"achievements": items})


@require_auth
async def handle_devices(request: web.Request, auth: AuthContext) -> web.Response:
    """
    Список привязанных устройств. token_hash наружу не отдаём —
    только короткий id для UI («это устройство»).
    """
    deps = request.app[APP_DEPS]
    current_hash = deps["device_repo"].hash_token(auth.token)
    devices = await deps["device_repo"].list_devices(auth.user_id)
    return _json({
        "devices": [
            {
                "id": d["token_hash"][:12],
                "name": d["device_name"],
                "created_at": d["created_at"],
                "last_seen_at": d["last_seen_at"],
                "current": d["token_hash"] == current_hash,
            }
            for d in devices
        ],
    })


@require_auth
async def handle_logout(request: web.Request, auth: AuthContext) -> web.Response:
    """Отзыв текущего токена — «выйти» в самом приложении."""
    deps = request.app[APP_DEPS]
    revoked = await deps["device_repo"].revoke_token(auth.token)
    logger.info("api.logout user_id=%s revoked=%s", auth.user_id, revoked)
    return _json({"revoked": revoked})


# ------------------------------------------------------------
# Общие хелперы домена
# ------------------------------------------------------------
def _pet_emotion(user, pet) -> tuple[str, datetime]:
    """
    (emotion, now_local) по той же логике, что _compute_pet_emotion_for_user
    в боте: is_studying=False (таймер живёт в FSM бота и API про него не
    знает), recently_excited — окно 5 минут от last_excited_at.
    """
    recently_excited = False
    if pet and pet.get("last_excited_at"):
        try:
            last = datetime.strptime(pet["last_excited_at"], "%Y-%m-%d %H:%M:%S")
            recently_excited = (datetime.now() - last) < timedelta(minutes=5)
        except (ValueError, TypeError):
            pass

    tz_name = (user or {}).get("timezone") or "Europe/Moscow"
    try:
        now_local = datetime.now(pytz.timezone(tz_name))
    except Exception:
        now_local = datetime.now()

    emotion = derive_emotion(
        is_studying=False,
        recently_excited=recently_excited,
        has_studied_today=bool(user["has_studied_today"]) if user else False,
        now_local=now_local,
    )
    return emotion, now_local


def _pet_payload(pet, emotion: str) -> dict:
    """Питомец в JSON. pet=None (ещё не создан) → дефолты, как в render_pet."""
    if not pet:
        return {
            "name": "Питомец",
            "color": "orange",
            "accessory": "none",
            "level": 1,
            "xp": 0,
            "emotion": emotion,
        }
    return {
        "name": pet["name"],
        "color": pet["color"],
        "accessory": pet["accessory"],
        "level": pet["level"],
        "xp": pet["xp"],
        "emotion": emotion,
    }


# ------------------------------------------------------------
# Сборка и запуск
# ------------------------------------------------------------
def create_app(
    *,
    user_repo,
    session_repo,
    pet_repo,
    device_repo,
    ach_service,
    achievements: dict,
) -> web.Application:
    """
    Собирает aiohttp-приложение с уже созданными репозиториями/сервисами
    бота. Зависимости передаются явно (не импортируются из bot.py), чтобы
    API поднимался в тестах без Telegram-токена и polling'а.
    """
    app = web.Application(
        middlewares=[cors_middleware, error_middleware],
    )
    app[APP_DEPS] = {
        "user_repo": user_repo,
        "session_repo": session_repo,
        "pet_repo": pet_repo,
        "device_repo": device_repo,
        "ach_service": ach_service,
        "achievements": achievements,
    }
    app[APP_LIMITER] = LinkAttemptLimiter()
    app.add_routes([
        web.get("/health", handle_health),
        web.post("/auth/link", handle_auth_link),
        web.get("/api/me", handle_me),
        web.get("/api/pet", handle_pet),
        web.get("/api/pet/image", handle_pet_image),
        web.get("/api/achievements", handle_achievements),
        web.get("/api/devices", handle_devices),
        web.post("/api/logout", handle_logout),
    ])
    return app


def api_enabled() -> bool:
    """API поднимается только при API_ENABLED=1 — по умолчанию выключен."""
    return os.getenv("API_ENABLED", "0") == "1"


async def start_api_server(app: web.Application) -> web.AppRunner:
    """
    Поднимает HTTP-сервер в текущем event loop и возвращает runner —
    caller обязан вызвать runner.cleanup() при остановке приложения.

    Слушает API_HOST:API_PORT (по умолчанию 0.0.0.0:8080). HTTPS
    терминируется снаружи (reverse-proxy на хосте/панель bothost) —
    сертификатами процесс бота не занимается.
    """
    host = os.getenv("API_HOST", "0.0.0.0")
    port = int(os.getenv("API_PORT", "8080"))
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port)
    await site.start()
    logger.info("api.started host=%s port=%s", host, port)
    return runner
