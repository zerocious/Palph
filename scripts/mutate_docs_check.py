#!/usr/bin/env python3
"""
Мутационная проверка `scripts/check_docs.py`.

Зачем: проверка, которую нельзя провалить, бесполезна, а выглядит она в
отчёте точно так же, как работающая. За время этой сессии одна и та же
ошибка ловилась трижды — узкий класс символов в регулярке (`[a-z_]+`)
делал переименованную сущность НЕВИДИМОЙ для выборки, и проверка проходила
на усечённом множестве. Ещё семь проверок сравнивали документ с числом,
записанным прямо в проверке, а не прочитанным из кода.

Скрипт вносит по одной поломке за раз — в документ или в код — и требует,
чтобы `check_docs.py` упал. Файлы восстанавливаются после каждой мутации.

    python scripts/mutate_docs_check.py            # все группы
    python scripts/mutate_docs_check.py --group constants

ВАЖНО: не запускать два экземпляра одновременно и не запускать параллельно
с чем-либо, что правит те же файлы, — восстановление одного затрёт
мутацию другого и оставит рабочее дерево грязным.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# (группа, описание, файл, что заменить, на что, ожидаемая подстрока в FAIL)
Mutation = tuple[str, str, str, str, str, str]

MUTATIONS: list[Mutation] = [
    # --- структура документации ---
    ("structure", "битая ссылка", "docs/README.md",
     "](architecture.md)", "](nope.md)", "ссылки"),
    ("structure", "новая таблица в схеме", "db.py",
     "CREATE TABLE IF NOT EXISTS admins (",
     "CREATE TABLE IF NOT EXISTS zzz_new (x INT);\n        CREATE TABLE IF NOT EXISTS admins (",
     "таблиц"),
    ("structure", "переименована таблица", "db.py",
     "CREATE TABLE IF NOT EXISTS admins (", "CREATE TABLE IF NOT EXISTS admins_v2 (",
     "таблица"),
    ("structure", "новый репозиторий", "repository.py",
     "\nclass AdminRepository:", "\nclass ExtraRepo:\n    pass\n\nclass AdminRepository:",
     "репозитори"),
    ("structure", "переименовано событие", "bot.py",
     '"leaderboard_privacy_toggled"', '"lb_privacy_toggled2"', "события"),
    ("structure", "событие исчезло из документа", "docs/analytics.md",
     "| `freeze_purchased` |", "| `freeze_purchasedX` |", "события описаны"),
    ("structure", "переименован алиас /export", "services.py",
     '"streak_freezes": "streak_freezes",', '"freezes2": "streak_freezes",', "алиас"),
    ("structure", "переименован вложенный ключ локали", "locales/ru.json",
     '"main_menu"', '"main_menu_x"', "полных путей"),
    ("structure", "ключ пропал в en", "locales/en.json",
     '"news_body"', '"news_body_x"', "полных путей"),
    ("structure", "перевёрнут фичефлаг", "plan_handlers.py",
     "PLAN_UI_ENABLED = False", "PLAN_UI_ENABLED = True", "PLAN_UI_ENABLED"),
    ("structure", "незакрытый блок кода", "docs/i18n.md",
     "## Локаль пользователя", "```python\n## Локаль пользователя", "разметка"),

    # --- команды ---
    ("commands", "удалён раздел админ-команды", "admin_commands.md",
     "### `/backup` 👑 главный админ", "### `/backupX` 👑 главный админ",
     "раздел в admin_commands"),
    ("commands", "команда пикера без хендлера", "locale_bot.py",
     'BotCommand(command="pet"', 'BotCommand(command="petz"', "пикера существуют"),

    # --- числа баланса (мутация в КОДЕ, документ остаётся старым) ---
    ("constants", "MAX_TASK_ATTEMPTS", "bot.py",
     "MAX_TASK_ATTEMPTS = 2", "MAX_TASK_ATTEMPTS = 3", "MAX_TASK_ATTEMPTS"),
    ("constants", "награды за задачу", "bot.py",
     "TASK_REWARDS_BY_ATTEMPT = [3, 2]", "TASK_REWARDS_BY_ATTEMPT = [5, 4]", "награда"),
    ("constants", "интервалы квизов", "bot.py",
     "QUIZ_INTERVALS = [1, 2, 4, 7]", "QUIZ_INTERVALS = [1, 3, 5, 9]", "QUIZ_INTERVALS"),
    ("constants", "дневной кап задач", "repository.py",
     "task_count < 5", "task_count < 7", "кап задач"),
    ("constants", "дневной кап квизов", "repository.py",
     "quiz_count < 25", "quiz_count < 30", "кап квизов"),
    ("constants", "дневной кап карточек", "repository.py",
     "cards_count < 8", "cards_count < 12", "кап карточек"),
    ("constants", "очки за задачу", "repository.py",
     "task_pts = task_pts + 40", "task_pts = task_pts + 60", "очки за задачу"),
    ("constants", "TOP_N_DISPLAY", "services.py",
     "TOP_N_DISPLAY = 20", "TOP_N_DISPLAY = 10", "TOP_N_DISPLAY"),
    ("constants", "COIN_BONUS_TOP10_PCT", "services.py",
     "COIN_BONUS_TOP10_PCT = 50", "COIN_BONUS_TOP10_PCT = 75", "COIN_BONUS"),
    ("constants", "MIN_SEGMENT_FOR_TOP10_BONUS", "services.py",
     "MIN_SEGMENT_FOR_TOP10_BONUS = 10", "MIN_SEGMENT_FOR_TOP10_BONUS = 20", "MIN_SEGMENT"),
    ("constants", "EF_FLOOR", "services.py", "EF_FLOOR = 1.3", "EF_FLOOR = 1.5", "EF_FLOOR"),
    ("constants", "cooldown советов", "bot.py",
     "TIPS_SEEN_COOLDOWN_DAYS = 7", "TIPS_SEEN_COOLDOWN_DAYS = 14", "TIPS_SEEN_COOLDOWN"),
    ("constants", "SPRINT_DAYS", "plan_service.py",
     "SPRINT_DAYS = 14", "SPRINT_DAYS = 21", "SPRINT_DAYS"),
    ("constants", "MIN_PLAN_ITEMS", "plan_service.py",
     "MIN_PLAN_ITEMS = 10", "MIN_PLAN_ITEMS = 15", "MIN_PLAN_ITEMS"),
]


def run_checker() -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "scripts/check_docs.py", "--quiet"],
        cwd=ROOT, capture_output=True, text=True,
    )
    return proc.returncode, proc.stdout


def main() -> int:
    ap = argparse.ArgumentParser(description="Мутационная проверка check_docs.py")
    ap.add_argument("--group", choices=("structure", "commands", "constants"),
                    help="прогнать только одну группу")
    args = ap.parse_args()

    code, _ = run_checker()
    if code != 0:
        print("check_docs.py падает ещё до мутаций — сначала почини документацию.")
        return 1

    missed, total = [], 0
    for group, desc, rel, old, new, expect in MUTATIONS:
        if args.group and group != args.group:
            continue
        total += 1
        path = ROOT / rel
        backup = path.with_suffix(path.suffix + ".mutbak")
        shutil.copy(path, backup)
        try:
            src = path.read_text(encoding="utf-8")
            if old not in src:
                print(f"[ЯКОРЬ УСТАРЕЛ] {group}/{desc}: не нашёл фрагмент в {rel}")
                missed.append(desc)
                continue
            path.write_text(src.replace(old, new, 1), encoding="utf-8")
            rc, out = run_checker()
            fails = [l.strip() for l in out.splitlines() if "FAIL" in l]
            caught = rc != 0 and any(expect in l for l in fails)
            if not caught:
                missed.append(desc)
            mark = "ПОЙМАНО" if caught else "ПРОПУЩЕНО"
            detail = fails[0][:80] if fails else "проверка не упала"
            print(f"[{mark:9}] {group:9} {desc:32} {detail}")
        finally:
            shutil.copy(backup, path)
            backup.unlink()

    rc, out = run_checker()
    print(f"\nпоймано {total - len(missed)}/{total}")
    if rc != 0:
        print("ВНИМАНИЕ: после восстановления check_docs.py всё ещё падает —\n"
              "проверь рабочее дерево (`git status`), мутация могла не откатиться.")
        return 1
    if missed:
        print("Не пойманы: " + ", ".join(missed))
        print("Проверку, которую нельзя провалить, надо усилить или удалить.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
