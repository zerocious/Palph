-- Purpose: Per-feature adoption rate within each signup ISO-week cohort.
--   Reveals whether newer cohorts onboard differently from older ones —
--   e.g., did a UI tweak in week W19 move flashcards adoption +10pp?
-- Parameters: none.
-- Output shape: cohort_week, cohort_size, then *_pct columns per feature.
-- Notes:
--   "Adopted feature X" = the user has at least one row in the relevant
--   progress / activity table.

WITH cohort AS (
    SELECT
        user_id,
        strftime('%Y-W%W', created_at) AS cohort_week
    FROM users
),
flags AS (
    SELECT
        c.user_id,
        c.cohort_week,
        CASE WHEN EXISTS (SELECT 1 FROM study_sessions s    WHERE s.user_id = c.user_id) THEN 1 ELSE 0 END AS used_pomodoro,
        CASE WHEN EXISTS (SELECT 1 FROM flashcard_progress f WHERE f.user_id = c.user_id) THEN 1 ELSE 0 END AS used_flashcards,
        CASE WHEN EXISTS (SELECT 1 FROM mcq_progress m       WHERE m.user_id = c.user_id) THEN 1 ELSE 0 END AS used_mcq,
        CASE WHEN EXISTS (SELECT 1 FROM task_progress t      WHERE t.user_id = c.user_id) THEN 1 ELSE 0 END AS used_tasks,
        CASE WHEN EXISTS (SELECT 1 FROM quiz_progress q      WHERE q.user_id = c.user_id) THEN 1 ELSE 0 END AS used_situational,
        CASE WHEN EXISTS (SELECT 1 FROM user_pet p           WHERE p.user_id = c.user_id) THEN 1 ELSE 0 END AS adopted_pet,
        CASE WHEN EXISTS (SELECT 1 FROM friendships fr
                          WHERE fr.user_a = c.user_id OR fr.user_b = c.user_id) THEN 1 ELSE 0 END AS has_friend
    FROM cohort c
)
SELECT
    cohort_week,
    COUNT(*) AS cohort_size,
    ROUND(100.0 * SUM(used_pomodoro)   / COUNT(*), 1) AS pomodoro_pct,
    ROUND(100.0 * SUM(used_flashcards) / COUNT(*), 1) AS flashcards_pct,
    ROUND(100.0 * SUM(used_mcq)        / COUNT(*), 1) AS mcq_pct,
    ROUND(100.0 * SUM(used_tasks)      / COUNT(*), 1) AS tasks_pct,
    ROUND(100.0 * SUM(used_situational)/ COUNT(*), 1) AS situational_pct,
    ROUND(100.0 * SUM(adopted_pet)     / COUNT(*), 1) AS pet_pct,
    ROUND(100.0 * SUM(has_friend)      / COUNT(*), 1) AS friend_pct
FROM flags
GROUP BY cohort_week
ORDER BY cohort_week DESC;
