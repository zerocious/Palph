# Math — высшая математика (схема Бернулли)

**Doc sync:** 2026-05-23 · 71 text-only tasks in Palph v0.8 (`task-08` removed — duplicate of `task-06`); **36 etalon tasks** with pedagogical `hint` from `source/top3_tasks_etalon_1.md`.

## Contents

| File / folder | Description |
|---------------|-------------|
| `tasks/task-01.json` … `task-72.json` (no `task-08`) | 71 text-only exam tasks (`text_only: true`) |
| `groups.json` | UI groups `exam-task-1` … `exam-task-6` (билет №1–6) |
| `topics.json` | Planner topic order: six exam-task groups |
| `source/top3_tasks_etalon.md` | Source curation list (not loaded by bot) |
| `source/top3_tasks_etalon_1.md` | Etalon top-3 tasks + 💡 hints (parsed by apply script) |
| `diagnostic/default.json` | 7 self-check prompts → task refs for sprint plan |
| `source/pz-6.pdf` | Source problem set (not loaded by bot) |

## Regenerating tasks

```bash
python scripts/generate_math_bernoulli_tasks.py
python scripts/generate_math_etalon_top3_tasks.py
python scripts/apply_math_task_hints.py
```

Bernoulli script writes `task-01`…`task-41` and diagnostic. Etalon script appends `task-42`…`task-72` and updates `groups.json` / `topics.json` (does not overwrite diagnostic). Hint script reads `source/top3_tasks_etalon_1.md` and writes `"hint"` into `task-16`…`18`, `20`, `25`, `42`…`72` (LaTeX → plain text for Telegram).

## Task JSON fields

| Field | Required | Notes |
|-------|----------|-------|
| `text_only` | yes | `true` — no PNG required |
| `problem` | yes | Full statement in chat |
| `accepted` | yes | Fraction + decimal variants |
| `solution_text` | yes | Shown after 2 wrong attempts |
| `hint` | optional | Pedagogical nudge on 1st wrong attempt (before full answer on 2nd) |
| `group`, `subtitle`, `topics` | yes | UI + planner |

## Answers

Each task lists `accepted` as exact fraction plus decimal variants (`.` and `,`). Matching logic: `task_answer_match.py`.
