# Math — высшая математика (схема Бернулли)

**Doc sync:** 2026-05-27 · 25 text-only tasks shipped in Palph v0.8.

## Contents

| File / folder | Description |
|---------------|-------------|
| `tasks/task-01.json` … `task-25.json` | Text-only exam tasks (`text_only: true`) |
| `groups.json` | UI group `exam-task-1` — «Билет, задача 1 (формула Бернулли)» |
| `topics.json` | Planner topic order: `["exam-task-1"]` |
| `diagnostic/default.json` | 7 self-check prompts → task refs for sprint plan |
| `source/pz-6.pdf` | Source problem set (not loaded by bot) |

## Regenerating tasks

```bash
python scripts/generate_math_bernoulli_tasks.py
```

Writes all `tasks/*.json`, `groups.json`, `topics.json`, and `diagnostic/default.json`.

## Answers

Each task lists `accepted` as exact fraction plus decimal variants (`.` and `,`). Matching logic: `task_answer_match.py`.
