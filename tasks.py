import asyncio
import logging
import pytz
from datetime import datetime, timedelta
from repository import UserRepository
from services import ReminderService, StreakService, BackupService, LeaderboardService


logger = logging.getLogger("studybuddy_bot")


def _compute_ended_week_iso(now_utc: datetime) -> str:
    """
    Возвращает week_iso только что закончившейся недели для rollover'а.
    Anchor — UTC Tuesday 00:00; в этот момент Sunday прошедшей недели
    был 2 дня назад. ISO week оттуда даёт корректный week_iso.

    Выделено в pure-функцию, чтобы тесты могли передать произвольный
    now_utc без mocking даты внутри scheduler-loop'а.
    """
    ended = now_utc - timedelta(days=2)
    return ended.strftime("%G-W%V")


async def leaderboard_scheduler(leaderboard_service: LeaderboardService):
    """
    Раз в минуту проверяет, наступил ли Вторник 00:00 UTC. Если да —
    запускает rollover недели для week_iso, закончившейся в прошедшее
    воскресенье.

    Почему UTC Tuesday 00:00 anchor (не per-TZ rollover):
    - К UTC Вт 00:00 все глобальные TZ уже пересекли Sun→Mon boundary
      в локальном времени (самый восточный UTC+14 → Tue 14:00 локально;
      самый западный UTC-12 → Mon 12:00 локально).
    - Значит, weekly_scores для прошедшей недели у ВСЕХ юзеров
      зафиксирована — никаких race-conditions с поздними write'ами.
    - Pro per-TZ rollover: мини-лидерборды на TZ → fragmented rewards,
      ranking зависит от того, в каком TZ ты живёшь. Не наш кейс.

    Идемпотентность:
    - award_badge внутри run_rollover — INSERT OR IGNORE по PK
      (user_id, badge_id, awarded_for_week). Повторный fire безвреден.
    - Дополнительно дедупим по дате (last_run_date), чтобы не молотить
      SQL в каждой минуте окна 00:00–00:59.
    """
    last_run_date: str | None = None  # 'YYYY-MM-DD' последнего успешного rollover
    logger.info("leaderboard_scheduler: started")
    while True:
        try:
            now_utc = datetime.now(pytz.UTC)
            today = now_utc.strftime("%Y-%m-%d")
            # weekday() == 1 → вторник (пн=0, вт=1, …)
            if (
                now_utc.weekday() == 1
                and now_utc.hour == 0
                and last_run_date != today
            ):
                ended_week_iso = _compute_ended_week_iso(now_utc)
                stats = await leaderboard_service.run_rollover(ended_week_iso)
                last_run_date = today
                logger.info(
                    "leaderboard_scheduler.rollover_done week=%s "
                    "segments=%s badges=%s coins=%s",
                    stats["week"], stats["segments_processed"],
                    stats["badges_awarded"], stats["coins_distributed"],
                )
        except Exception as e:
            logger.error(f"leaderboard_scheduler failed: {e}")
        await asyncio.sleep(60)


async def streak_scheduler(
    streak_service: StreakService,
    user_repo: UserRepository,
    backup_service: BackupService | None = None,
):
    """
    Раз в минуту проверяет, в каких часовых поясах сейчас 23:59,
    и запускает обработку стриков для пользователей этих TZ.
    Дедупликация: каждый TZ обрабатывается не чаще одного раза в сутки.

    После обработки стриков для TZ — пытается сделать ежедневный backup БД.
    BackupService сам дедуплицирует по server-local date, поэтому реальный
    backup создаётся только один раз за глобальный день (первый TZ, который
    хитает 23:59). backup_service опционален (None — пропускаем backup-шаг).
    """
    last_check_date: dict[str, str] = {}  # tz -> "YYYY-MM-DD" последней обработки

    while True:
        try:
            tzs = await user_repo.get_distinct_timezones()
            for tz_name in tzs:
                try:
                    tz = pytz.timezone(tz_name)
                except Exception:
                    logger.warning(f"streak_scheduler: unknown timezone {tz_name!r}, skip")
                    continue
                now_local = datetime.now(tz)
                if now_local.hour == 23 and now_local.minute == 59:
                    today = now_local.strftime("%Y-%m-%d")
                    if last_check_date.get(tz_name) != today:
                        await streak_service.process_users_in_timezone(tz_name)
                        last_check_date[tz_name] = today
                        # Backup БД после streak processing. Dedupe внутри сервиса
                        # — один файл на server-local день.
                        if backup_service is not None:
                            await backup_service.maybe_backup_for_today()
        except Exception as e:
            logger.error(f"streak_scheduler failed: {e}")
        await asyncio.sleep(60)


async def reminder_scheduler(reminder_service: ReminderService, user_repo: UserRepository):
    """
    Раз в минуту проходит по всем используемым TZ, считает локальное hh:mm
    для каждого и вызывает reminder_service.tick(tz, hhmm). Запоминает
    последнюю обработанную минуту для каждого TZ, чтобы не отправлять
    дважды в ту же минуту.
    """
    last_tick: dict[str, str] = {}  # tz -> "HH:MM" последней отправки
    heartbeat_counter = 0
    logger.info("reminder_scheduler: started")
    while True:
        try:
            tzs = await user_repo.get_distinct_timezones()
            for tz_name in tzs:
                try:
                    tz = pytz.timezone(tz_name)
                except Exception:
                    logger.warning(f"reminder_scheduler: unknown timezone {tz_name!r}, skip")
                    continue
                now_local = datetime.now(tz)
                hhmm = now_local.strftime("%H:%M")
                if last_tick.get(tz_name) != hhmm:
                    await reminder_service.tick(tz_name, hhmm)
                    last_tick[tz_name] = hhmm
            heartbeat_counter += 1
            # Тихий heartbeat раз в 10 минут — подтверждает, что цикл живой.
            if heartbeat_counter % 10 == 0:
                logger.info(
                    f"reminder_scheduler heartbeat: tzs={tzs}, last_tick={last_tick}"
                )
        except Exception as e:
            logger.error(f"reminder_scheduler tick failed: {e}")
        # Спим до начала следующей минуты, плюс полсекунды.
        # Секунды одинаковы во всех TZ (смещения кратны минутам).
        await asyncio.sleep(max(1.0, 60.5 - datetime.now().second))
