"""
ETL: bot.log (structured log lines) → CSV для анализа в pandas/Jupyter.

Парсит структурированные log-строки формата
    YYYY-MM-DD HH:MM:SS - logger - LEVEL - event.tag key=value key=value ...
в строки с {timestamp, level, event_name, user_id, properties (JSON), raw_text}.

Используется:
1. Как CLI:    python parse_logs.py bot.log -o events_from_logs.csv
2. Как библиотека: from parse_logs import parse_log_file
3. Из бота через /parse_logs admin-команду

Поддерживает rotated log-файлы (bot.log.1, bot.log.2, ...) — передай несколько
путей или скрипт сам подхватит дефолтные.

Edge cases которые обрабатываются:
- multi-word values (next=2026-05-18 14:35:01 — две лексемы с пробелом)
- legacy unstructured строки (без event.tag формата) → event_name="unstructured"
- non-utf8 байты (errors="replace")
- malformed строки → пропускаются (None из parse_log_line)
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from pathlib import Path


# Грубая структура каждой log-строки: timestamp, logger, level, payload
LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) - "
    r"(?P<logger>\S+) - "
    r"(?P<level>\w+) - "
    r"(?P<payload>.+)$"
)

# Event-payload начинается с event.tag (точечно-разделённый идентификатор).
# Trailing key=value часть опциональна: события вроде `app.shutdown` без
# дополнительных полей валидны.
EVENT_HEAD_RE = re.compile(r"^(?P<event>[\w.]+\.[\w.]+)(?:\s+(?P<rest>.*))?$")

# Lazy-quantifier с lookahead: matches key=value где value может содержать
# пробелы (например next=2026-05-18 14:35:01). Останавливается перед следующим
# `\s+\w+=` или концом строки.
KV_RE = re.compile(r"(\w+)=(.+?)(?=\s+\w+=|$)")


def parse_log_line(line: str) -> dict | None:
    """
    Парсит одну строку лога. Возвращает dict или None для malformed/empty.

    Result schema:
        {
            "timestamp": "YYYY-MM-DD HH:MM:SS",
            "level": "INFO" | "WARNING" | "ERROR" | "DEBUG",
            "event_name": "session.complete" | "flash.rated" | ... | "unstructured",
            "user_id": int | None,        # extracted from key=value pairs
            "properties": "{...}" (JSON), # все остальные key=values
            "raw_text": "..."             # only set для unstructured строк
        }
    """
    line = line.rstrip("\r\n")
    m = LOG_LINE_RE.match(line)
    if not m:
        return None

    ts = m["ts"]
    level = m["level"]
    payload = m["payload"]

    event_m = EVENT_HEAD_RE.match(payload)
    if event_m:
        event_name = event_m["event"]
        kv_payload = event_m["rest"] or ""  # rest опциональный — может быть None
        kvs = KV_RE.findall(kv_payload)
        properties = dict(kvs)
        user_id_raw = properties.pop("user_id", None)
        user_id = None
        if user_id_raw is not None:
            try:
                user_id = int(user_id_raw)
            except (ValueError, TypeError):
                user_id = None
        return {
            "timestamp": ts,
            "level": level,
            "event_name": event_name,
            "user_id": user_id,
            "properties": json.dumps(properties, ensure_ascii=False),
            "raw_text": "",
        }

    # Unstructured: legacy log lines без event-tag (например "✅ StudyBuddy запущен")
    return {
        "timestamp": ts,
        "level": level,
        "event_name": "unstructured",
        "user_id": None,
        "properties": "{}",
        "raw_text": payload,
    }


def parse_log_file(path: str | Path) -> list[dict]:
    """Парсит весь файл; пропускает malformed/empty строки."""
    rows = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parsed = parse_log_line(line)
            if parsed is not None:
                rows.append(parsed)
    return rows


CSV_COLUMNS = ["timestamp", "level", "event_name", "user_id", "properties", "raw_text"]


def write_csv(rows: list[dict], output_path: str | Path) -> None:
    """Пишет в CSV-файл (UTF-8, RFC 4180 quoting)."""
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def to_csv_bytes(rows: list[dict]) -> bytes:
    """Возвращает CSV как bytes для in-memory отправки (Telegram document)."""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _default_log_paths() -> list[Path]:
    """bot.log + bot.log.1..9 если существуют — дефолт для CLI."""
    paths = []
    base = Path("bot.log")
    if base.exists():
        paths.append(base)
    for i in range(1, 10):
        p = Path(f"bot.log.{i}")
        if p.exists():
            paths.append(p)
    return paths


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Parse bot.log → CSV for pandas/Jupyter analysis",
    )
    parser.add_argument(
        "logs", nargs="*", type=Path,
        help="Log files to parse (default: bot.log + bot.log.1..9 если есть)",
    )
    parser.add_argument(
        "-o", "--output", type=Path, default=Path("events_from_logs.csv"),
        help="Output CSV path (default: events_from_logs.csv)",
    )
    parser.add_argument(
        "--quiet", action="store_true",
        help="Suppress progress output",
    )
    args = parser.parse_args(argv)

    log_files = args.logs or _default_log_paths()
    if not log_files:
        print("⚠️  No log files found (and none specified)", file=sys.stderr)
        return 1

    all_rows: list[dict] = []
    for path in log_files:
        if not path.exists():
            if not args.quiet:
                print(f"⚠️  skipping missing {path}", file=sys.stderr)
            continue
        rows = parse_log_file(path)
        all_rows.extend(rows)
        if not args.quiet:
            print(f"  {path}: {len(rows)} rows", file=sys.stderr)

    # Сортируем chronologically (multi-file → can have intermixed timestamps)
    all_rows.sort(key=lambda r: r["timestamp"])

    write_csv(all_rows, args.output)
    if not args.quiet:
        print(f"✅ {len(all_rows)} events → {args.output}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
