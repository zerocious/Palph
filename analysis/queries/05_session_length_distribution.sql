-- Purpose: Pomodoro session length distribution + key percentiles.
--   Surfaces whether the 25-min default dominates, whether free-duration
--   sessions cluster anywhere distinctive, and what the long tail looks like.
-- Parameters: none.
-- Output: two result sets — emit them as two separate executions if your
--   client doesn't show both. (`sqlite3` CLI handles back-to-back SELECTs.)
-- Notes:
--   PERCENT_RANK is widely supported in modern SQLite (>= 3.25, 2018).
--   If your build is older, drop the second SELECT.

-- 1) Bucketed histogram.
SELECT
    CASE
        WHEN duration_minutes < 5   THEN '00. <5 min'
        WHEN duration_minutes < 10  THEN '01. 5-9 min'
        WHEN duration_minutes < 15  THEN '02. 10-14 min'
        WHEN duration_minutes < 25  THEN '03. 15-24 min'
        WHEN duration_minutes = 25  THEN '04. 25 min (default)'
        WHEN duration_minutes < 30  THEN '05. 26-29 min'
        WHEN duration_minutes < 45  THEN '06. 30-44 min'
        WHEN duration_minutes < 60  THEN '07. 45-59 min'
        WHEN duration_minutes < 90  THEN '08. 60-89 min'
        ELSE                              '09. 90+ min'
    END AS bucket,
    COUNT(*) AS sessions,
    ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM study_sessions), 1) AS pct
FROM study_sessions
GROUP BY bucket
ORDER BY bucket;

-- 2) Quartiles via PERCENT_RANK.
WITH ranked AS (
    SELECT duration_minutes,
           PERCENT_RANK() OVER (ORDER BY duration_minutes) AS pr
    FROM study_sessions
)
SELECT
    (SELECT duration_minutes FROM ranked
        WHERE pr >= 0.25 ORDER BY pr ASC LIMIT 1) AS p25,
    (SELECT duration_minutes FROM ranked
        WHERE pr >= 0.50 ORDER BY pr ASC LIMIT 1) AS p50_median,
    (SELECT duration_minutes FROM ranked
        WHERE pr >= 0.75 ORDER BY pr ASC LIMIT 1) AS p75,
    (SELECT duration_minutes FROM ranked
        WHERE pr >= 0.95 ORDER BY pr ASC LIMIT 1) AS p95,
    (SELECT AVG(duration_minutes) FROM study_sessions) AS mean,
    (SELECT COUNT(*) FROM study_sessions)              AS n;
