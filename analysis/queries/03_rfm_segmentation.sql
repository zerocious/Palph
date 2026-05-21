-- Purpose: RFM segmentation over the coin economy.
--   R (Recency)   — days since the user's last study session.
--   F (Frequency) — total_sessions accumulated.
--   M (Monetary)  — total_coins (proxy for value: longer = more earned).
-- Parameters: none. Quintile boundaries are data-driven (NTILE inside CTEs).
-- Output shape: user_id, last_session_days_ago, sessions, coins,
--               r_score (1-5), f_score (1-5), m_score (1-5), rfm_segment.
-- Notes:
--   r_score 5 = most recent (last_session_days_ago in bottom quintile).
--   Classic RFM labelling — 'Champions' = 5/5/5, 'At Risk' = low R+F+M.
--   Skips users with total_sessions=0 (never_started bucket lives elsewhere).

WITH active_users AS (
    SELECT
        user_id,
        total_sessions  AS sessions,
        total_coins     AS coins,
        CASE
            WHEN last_session IS NULL THEN 99999
            ELSE CAST(julianday('now') - julianday(last_session) AS INTEGER)
        END AS last_session_days_ago
    FROM users
    WHERE total_sessions > 0
),
scored AS (
    SELECT
        user_id, sessions, coins, last_session_days_ago,
        -- NTILE 5 maps small-recency-days → high score (5 = best).
        6 - NTILE(5) OVER (ORDER BY last_session_days_ago ASC) AS r_score,
        NTILE(5) OVER (ORDER BY sessions ASC) AS f_score,
        NTILE(5) OVER (ORDER BY coins    ASC) AS m_score
    FROM active_users
)
SELECT
    user_id,
    last_session_days_ago,
    sessions,
    coins,
    r_score, f_score, m_score,
    CASE
        WHEN r_score >= 4 AND f_score >= 4 AND m_score >= 4 THEN 'Champions'
        WHEN r_score >= 4 AND f_score >= 3                    THEN 'Loyal'
        WHEN r_score >= 4 AND f_score <= 2                    THEN 'New / Promising'
        WHEN r_score <= 2 AND f_score >= 4                    THEN 'At Risk'
        WHEN r_score <= 2 AND f_score <= 2                    THEN 'Hibernating'
        ELSE 'Needs Attention'
    END AS rfm_segment
FROM scored
ORDER BY r_score DESC, f_score DESC, m_score DESC;
