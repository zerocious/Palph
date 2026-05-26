# Study materials layout

Palph v0.8 — data-driven discovery in `bot.py` (`available_subjects`, `available_modes`).

Each subject lives in `study_materials/<subject_id>/`. The bot discovers subjects from folder names (see `SUBJECTS` in `bot.py`).

**Doc sync:** 2026-05-25.

## Required for sprint exam plan

| Path | Purpose |
|------|---------|
| `flashcards.txt` | Official flashcards (`term \|\| definition [\|\| topics]`) |
| `mcq.txt` | MCQ questions (`question \|\| correct \|\| wrong×3 [\|\| topics]`) |
| `tasks/task-NN.json` + `task-NN.png` | Photo tasks (optional `"topics": ["section-i"]`) |
| `situational/section-*.txt` | Situational terms (topic = section id) |
| `diagnostic/default.json` | Entry test for plan generation |
| `topics.json` (optional) | `{"order": ["section-i", "general", ...]}` for planner progression |

## Text-only tasks (no PNG)

For subjects where the problem is fully described in text (e.g. math Bernoulli ticket), set in `task-NN.json`:

```json
{
  "text_only": true,
  "problem": "Full problem statement…",
  "accepted": ["8/27", "0.2963", "0,2963"],
  "solution_text": "Step-by-step solution shown after 3 wrong attempts.",
  "group": "exam-task-1",
  "subtitle": "Пример 2",
  "topics": ["exam-task-1"]
}
```

- `text_only: true` — `task-NN.png` is optional; the bot sends the problem as a text message.
- `solution_text` — shown instead of `task-NN-solution.png` when the user exhausts attempts.
- Answers are checked with `task_answer_match.task_answer_matches` (fractions, comma/dot decimals, normalized text).

`plan_service.build_content_catalog` includes text-only tasks without a PNG (same rule as `load_tasks` in `bot.py`).

## Task groups (`groups.json`)

Optional `study_materials/<subject>/groups.json` — map of group id → metadata:

```json
{
  "exam-task-1": {
    "title": "Билет — задача 1 (формула Бернулли)",
    "description": "Short hint for authors"
  }
}
```

When present, the **tasks** mode shows an inline picker (group title + task count) before the session. Tasks reference a group via `"group": "exam-task-1"` in JSON.

## Math (`study_materials/math/`)

Shipped content: 15 Bernoulli-scheme tasks (PZ-6), one group `exam-task-1`, diagnostic + `topics.json`. See [math/README.md](math/README.md). Regenerate from source script: `python scripts/generate_math_bernoulli_tasks.py`.

## Accounting (`study_materials/accounting/`)

Folder for future **бухучёт** content. `source/` holds raw PDFs/DOCs (not read by the bot). See [accounting/README.md](accounting/README.md). Subject is **not** wired in `SUBJECTS` yet.

## Diagnostic JSON

```json
{
  "questions": [
    {
      "mode": "mcq",
      "ref": "<question_hash>",
      "topic": "general",
      "prompt": "...",
      "options": ["...", "..."],
      "correct_index": 0
    },
    {
      "mode": "situational",
      "ref": "<term_hash>",
      "section": "section-i",
      "topic": "section-i",
      "prompt": "Situation text..."
    },
    {
      "mode": "tasks",
      "ref": "task-01",
      "topic": "exam-task-1",
      "prompt": "Can you solve…?"
    }
  ]
}
```

`ref` must match catalog hashes (MD5 prefix for MCQ/situational; task id for `mode: tasks`). Untagged flashcards/MCQ default to topic `general`.

## Minimum content

Sprint plan requires at least **10** catalog items and a non-empty `diagnostic/default.json`.
