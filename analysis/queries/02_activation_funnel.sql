-- Purpose: 5-step activation funnel from registration to early retention.
-- Parameters: none.
-- Output shape: step (TEXT), users (INT), pct_of_total (REAL).
-- Notes:
--   Mirrors AnalyticsService.compute_funnel. Each step is a strict superset
--   of the next: % drop-off between steps reveals where users stall.

WITH base AS (
    SELECT COUNT(*) AS total FROM users
),
counts AS (
    SELECT
        (SELECT COUNT(*) FROM users)                                AS registered,
        (SELECT COUNT(*) FROM users WHERE total_sessions >= 1)      AS started_first,
        (SELECT COUNT(*) FROM users WHERE total_sessions >= 5)      AS five_plus,
        (SELECT COUNT(*) FROM users WHERE total_sessions >= 10)     AS ten_plus,
        (SELECT COUNT(*) FROM users WHERE current_streak >= 3)      AS streak_3,
        (SELECT COUNT(*) FROM users WHERE current_streak >= 7)      AS streak_7
)
SELECT step, users,
       ROUND(100.0 * users / NULLIF((SELECT total FROM base), 0), 1) AS pct_of_total
FROM (
    SELECT 1 AS ord, '1. Registered'        AS step, registered    AS users FROM counts
    UNION ALL
    SELECT 2,        '2. Started 1+ session', started_first         FROM counts
    UNION ALL
    SELECT 3,        '3. 5+ sessions',        five_plus             FROM counts
    UNION ALL
    SELECT 4,        '4. 10+ sessions',       ten_plus              FROM counts
    UNION ALL
    SELECT 5,        '5. 3-day streak',       streak_3              FROM counts
    UNION ALL
    SELECT 6,        '6. 7-day streak',       streak_7              FROM counts
)
ORDER BY ord;
