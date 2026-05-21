-- Purpose: Math subject ("Математика") funnel — mission-critical metric.
--   Project goal: increase the share of users who pass the math exam at
--   the home university. This query measures depth-of-engagement with
--   the math subject specifically.
-- Parameters: subject_id = 'Математика' (Russian "Mathematics"); change to
--   reproduce the same funnel for any subject_id in user_subject_stats.
-- Output: step (TEXT), users (INT), pct_of_visited (REAL).
-- Notes:
--   "Visited" = at least one row in user_subject_stats for subject_id.
--   "Answered MCQ" / "Completed task" / "Answered situational" use the
--   existing per-item progress tables — they don't carry subject_id
--   directly, so we approximate via question_hash / task_id / term_hash
--   not being NULL for the user, combined with their having visited the
--   subject. Strict subject attribution requires either (a) a separate
--   `subject_id` column on progress tables or (b) joining via content
--   manifest. For now, "visited the subject + has activity" is the
--   pragmatic proxy.

WITH math_visitors AS (
    SELECT DISTINCT user_id FROM user_subject_stats
    WHERE subject_id = 'Математика'
),
funnel AS (
    SELECT
        (SELECT COUNT(*) FROM math_visitors) AS visited_math,
        (SELECT COUNT(DISTINCT mcq.user_id) FROM mcq_progress mcq
            JOIN math_visitors mv ON mv.user_id = mcq.user_id) AS answered_mcq,
        (SELECT COUNT(DISTINCT mcq.user_id) FROM mcq_progress mcq
            JOIN math_visitors mv ON mv.user_id = mcq.user_id
            WHERE mcq.correct_count >= 1) AS mcq_correct,
        (SELECT COUNT(DISTINCT tp.user_id) FROM task_progress tp
            JOIN math_visitors mv ON mv.user_id = tp.user_id) AS attempted_task,
        (SELECT COUNT(DISTINCT tp.user_id) FROM task_progress tp
            JOIN math_visitors mv ON mv.user_id = tp.user_id
            WHERE tp.succeeded = 1) AS task_succeeded,
        (SELECT COUNT(DISTINCT fp.user_id) FROM flashcard_progress fp
            JOIN math_visitors mv ON mv.user_id = fp.user_id) AS reviewed_cards
)
SELECT step, users,
       ROUND(100.0 * users / NULLIF((SELECT visited_math FROM funnel), 0), 1)
           AS pct_of_visited
FROM (
    SELECT 1 AS ord, '1. Visited Math'              AS step, visited_math   AS users FROM funnel
    UNION ALL
    SELECT 2,        '2. Answered any MCQ',         answered_mcq            FROM funnel
    UNION ALL
    SELECT 3,        '3. MCQ correct ≥ 1',          mcq_correct             FROM funnel
    UNION ALL
    SELECT 4,        '4. Attempted any task',       attempted_task          FROM funnel
    UNION ALL
    SELECT 5,        '5. Task succeeded ≥ 1',       task_succeeded          FROM funnel
    UNION ALL
    SELECT 6,        '6. Reviewed any flashcard',   reviewed_cards          FROM funnel
)
ORDER BY ord;
