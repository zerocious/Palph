-- Purpose: Per-user signal table for churn-correlation analysis.
--   Each row = one user, with first-week activity signals + a churn label
--   (active in last 14 days vs not). Feed into a notebook to compute
--   correlations / a simple logistic model.
-- Parameters: none. Define "first week" as the 7 days following signup.
-- Output: user_id, days_since_signup, week1_sessions, week1_coins,
--         tried_pomodoro, tried_flashcards, tried_mcq, tried_tasks,
--         had_friend_by_d7, has_pet_by_d7, is_retained_d30 (0/1).
-- Notes:
--   Users with days_since_signup < 30 are still inside the D30 window,
--   so their retention label is provisional — filter them out in the
--   notebook before fitting a model.

WITH d7 AS (
    SELECT
        u.user_id,
        u.created_at,
        date(u.created_at, '+7 day') AS d7_date,
        date(u.created_at, '+30 day') AS d30_date,
        CAST(julianday('now') - julianday(u.created_at) AS INTEGER)
            AS days_since_signup
    FROM users u
),
week1_sessions AS (
    SELECT s.user_id,
           COUNT(*) AS n_sessions,
           COALESCE(SUM(s.coins_earned + s.bonus_coins), 0) AS coins_earned
    FROM study_sessions s
    JOIN d7 ON d7.user_id = s.user_id
    WHERE s.created_at <= d7.d7_date
    GROUP BY s.user_id
),
recent_activity AS (
    SELECT user_id, MAX(activity_date) AS last_activity
    FROM (
        SELECT user_id, date(created_at)   AS activity_date FROM study_sessions
        UNION ALL
        SELECT user_id, date(last_activity)              FROM user_subject_stats WHERE last_activity IS NOT NULL
        UNION ALL
        SELECT user_id, date(last_review)                FROM flashcard_progress WHERE last_review IS NOT NULL
        UNION ALL
        SELECT user_id, date(last_attempt)               FROM mcq_progress       WHERE last_attempt IS NOT NULL
        UNION ALL
        SELECT user_id, date(last_attempt)               FROM task_progress      WHERE last_attempt IS NOT NULL
        UNION ALL
        SELECT user_id, date(last_attempt)               FROM quiz_progress      WHERE last_attempt IS NOT NULL
    )
    GROUP BY user_id
)
SELECT
    d7.user_id,
    d7.days_since_signup,
    COALESCE(ws.n_sessions, 0)    AS week1_sessions,
    COALESCE(ws.coins_earned, 0)  AS week1_coins,
    CASE WHEN COALESCE(ws.n_sessions, 0) >= 1 THEN 1 ELSE 0 END AS tried_pomodoro,
    CASE WHEN EXISTS (
        SELECT 1 FROM flashcard_progress fp
        WHERE fp.user_id = d7.user_id AND fp.last_review <= d7.d7_date
    ) THEN 1 ELSE 0 END AS tried_flashcards,
    CASE WHEN EXISTS (
        SELECT 1 FROM mcq_progress m
        WHERE m.user_id = d7.user_id AND m.last_attempt <= d7.d7_date
    ) THEN 1 ELSE 0 END AS tried_mcq,
    CASE WHEN EXISTS (
        SELECT 1 FROM task_progress t
        WHERE t.user_id = d7.user_id AND t.last_attempt <= d7.d7_date
    ) THEN 1 ELSE 0 END AS tried_tasks,
    CASE WHEN EXISTS (
        SELECT 1 FROM friendships fr
        WHERE (fr.user_a = d7.user_id OR fr.user_b = d7.user_id)
          AND fr.created_at <= d7.d7_date
    ) THEN 1 ELSE 0 END AS had_friend_by_d7,
    CASE WHEN EXISTS (
        SELECT 1 FROM user_pet p
        WHERE p.user_id = d7.user_id AND p.created_at <= d7.d7_date
    ) THEN 1 ELSE 0 END AS has_pet_by_d7,
    CASE WHEN ra.last_activity IS NOT NULL
              AND ra.last_activity >= date('now', '-14 day')
         THEN 1 ELSE 0 END AS is_retained_d30
FROM d7
LEFT JOIN week1_sessions ws ON ws.user_id = d7.user_id
LEFT JOIN recent_activity ra ON ra.user_id = d7.user_id
ORDER BY d7.days_since_signup DESC;
