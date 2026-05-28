# Math — высшая математика (схема Бернулли)

**Doc sync:** 2026-05-28 · 71 text-only tasks shipped in Palph v0.8 (`task-08` removed — duplicate of `task-06`).

## Contents

| File / folder | Description |
|---------------|-------------|
| `tasks/task-01.json` … `task-72.json` (no `task-08`) | 71 text-only exam tasks (`text_only: true`) |
| `groups.json` | UI groups `exam-task-1` … `exam-task-6` (билет №1–6) |
| `topics.json` | Planner topic order: six exam-task groups |
| `source/top3_tasks_etalon.md` | Source curation list (not loaded by bot) |
| `diagnostic/default.json` | 7 self-check prompts → task refs for sprint plan |
| `source/pz-6.pdf` | Source problem set (not loaded by bot) |

## Regenerating tasks

```bash
python scripts/generate_math_bernoulli_tasks.py
python scripts/generate_math_etalon_top3_tasks.py
```

Bernoulli script writes `task-01`…`task-41` and diagnostic. Etalon script appends `task-42`…`task-72` and updates `groups.json` / `topics.json` (does not overwrite diagnostic).

## Answers

Each task lists `accepted` as exact fraction plus decimal variants (`.` and `,`). Matching logic: `task_answer_match.py`.
