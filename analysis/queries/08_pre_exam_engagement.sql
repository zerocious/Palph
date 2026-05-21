-- Purpose: Engagement curve in the 14 days leading up to a user's exam date.
--   How does activity change as the exam approaches? Reveals whether the
--   bot is being used as a crunch-cram tool or a steady-prep tool.
--
-- Status: BLOCKED — requires PA-roadmap #2 (`users.exam_date` column).
--   That item is intentionally skipped from the current PA-roadmap by
--   user decision, so this file is a stub. Schema unblocked = uncomment
--   the body below; until then the SELECT returns one explanatory row.
--
-- Parameters (when unblocked): none.
-- Output (when unblocked): days_to_exam (-14..0), avg_minutes_studied,
--                          users_active, sessions_count.

SELECT
    'BLOCKED: needs users.exam_date column (PA-roadmap #2, deferred)'
        AS status,
    'Migrate the users table with an exam_date TEXT column, capture it '
        || 'during onboarding, then uncomment the WITH-block below.'
        AS instruction;

-- -- Uncomment after adding users.exam_date (DATE in 'YYYY-MM-DD'):
-- WITH activity AS (
--     SELECT user_id, date(created_at) AS d FROM study_sessions
--     UNION ALL
--     SELECT user_id, date(last_attempt) FROM mcq_progress
--         WHERE last_attempt IS NOT NULL
--     UNION ALL
--     SELECT user_id, date(last_attempt) FROM task_progress
--         WHERE last_attempt IS NOT NULL
--     UNION ALL
--     SELECT user_id, date(last_review) FROM flashcard_progress
--         WHERE last_review IS NOT NULL
-- ),
-- duration AS (
--     SELECT user_id, date(created_at) AS d, duration_minutes
--     FROM study_sessions
-- )
-- SELECT
--     CAST(julianday(a.d) - julianday(u.exam_date) AS INTEGER) AS days_to_exam,
--     COALESCE(AVG(d.duration_minutes), 0)        AS avg_minutes_studied,
--     COUNT(DISTINCT a.user_id)                   AS users_active,
--     COUNT(*)                                    AS sessions_count
-- FROM activity a
-- JOIN users u ON u.user_id = a.user_id
-- LEFT JOIN duration d ON d.user_id = a.user_id AND d.d = a.d
-- WHERE u.exam_date IS NOT NULL
--   AND julianday(a.d) - julianday(u.exam_date) BETWEEN -14 AND 0
-- GROUP BY days_to_exam
-- ORDER BY days_to_exam;
