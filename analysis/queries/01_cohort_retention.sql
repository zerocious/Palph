-- Purpose: D1 / D7 / D30 retention per ISO-week signup cohort.
-- Parameters: none.
-- Output shape: cohort_week (TEXT), cohort_size (INT),
--               d1_retained (INT), d7_retained (INT), d30_retained (INT),
--               d1_pct (REAL), d7_pct (REAL), d30_pct (REAL).
-- Notes:
--   "Active on day N" = at least one row in any progress / session source
--   with date(signup_date + N days). We pull from study_sessions,
--   user_subject_stats, quiz_progress, flashcard_progress, mcq_progress,
--   task_progress — same UNION as services.AnalyticsService.

WITH all_activity AS (
    SELECT user_id, date(created_at) AS activity_date FROM study_sessions
    UNION ALL
    SELECT user_id, date(last_activity) FROM user_subject_stats
        WHERE last_activity IS NOT NULL
    UNION ALL
    SELECT user_id, date(last_attempt) FROM quiz_progress
        WHERE last_attempt IS NOT NULL
    UNION ALL
    SELECT user_id, date(last_review) FROM flashcard_progress
        WHERE last_review IS NOT NULL
    UNION ALL
    SELECT user_id, date(last_attempt) FROM mcq_progress
        WHERE last_attempt IS NOT NULL
    UNION ALL
    SELECT user_id, date(last_attempt) FROM task_progress
        WHERE last_attempt IS NOT NULL
),
user_signups AS (
    SELECT
        user_id,
        date(created_at) AS signup_date,
        strftime('%Y-W%W', created_at) AS cohort_week
    FROM users
),
retention AS (
    SELECT
        s.cohort_week,
        s.user_id,
        MAX(CASE WHEN a.activity_date = date(s.signup_date, '+1 day')
                 THEN 1 ELSE 0 END) AS active_d1,
        MAX(CASE WHEN a.activity_date = date(s.signup_date, '+7 day')
                 THEN 1 ELSE 0 END) AS active_d7,
        MAX(CASE WHEN a.activity_date = date(s.signup_date, '+30 day')
                 THEN 1 ELSE 0 END) AS active_d30
    FROM user_signups s
    LEFT JOIN all_activity a ON a.user_id = s.user_id
    GROUP BY s.cohort_week, s.user_id
)
SELECT
    cohort_week,
    COUNT(*) AS cohort_size,
    SUM(active_d1)  AS d1_retained,
    SUM(active_d7)  AS d7_retained,
    SUM(active_d30) AS d30_retained,
    ROUND(100.0 * SUM(active_d1)  / COUNT(*), 1) AS d1_pct,
    ROUND(100.0 * SUM(active_d7)  / COUNT(*), 1) AS d7_pct,
    ROUND(100.0 * SUM(active_d30) / COUNT(*), 1) AS d30_pct
FROM retention
GROUP BY cohort_week
ORDER BY cohort_week DESC;
