# Case Study — Palph Product Analytics

> Palph v0.8 · doc sync 2026-05-25

**Title:** Improving Activation and Retention in a Telegram Study Bot  
**Author:** ___  
**Period:** YYYY-MM-DD → YYYY-MM-DD (3 weeks post-launch)  
**Role:** Product Analyst

---

## 1. Context

**Product:** Palph — Telegram-бот для регулярной учёбы студентов через Pomodoro, 4 учебных режима (квизы, карточки SM-2, MCQ, задачи), геймификацию (монеты, ачивки, питомец) и советы по продуктивности.

**Audience:** Студенты перед зачётом/экзаменом (ОПМ, математика).

**Problem:** Студенты теряют время на выбор «что учить» и не формируют регулярную привычку. Нужно понять, какие механики бота реально помогают начать и продолжить учёбу.

**My scope:** Event tracking design review, launch analytics, funnel/retention analysis, feature adoption, user segmentation, product recommendations.

---

## 2. Goal & Metrics

| Type | Metric | Target | Actual |
|------|--------|--------|--------|
| North Star | Activated users returning D7 | measure | |
| Acquisition | Registrations | 30–50 | |
| Activation | % session in 24h | baseline | |
| Retention | D7 | ≥15% | |
| Engagement | Stickiness DAU/MAU | ≥15% | |

**Hypotheses tested:** H1–H6 from [product_framework.md](../product_framework.md)

---

## 3. Data & Methods

**Sources:**
- SQLite `events` table (append-only, v0.8 hooks)
- Admin analytics: `/funnel`, `/cohort_stats`, `/product_metrics`, `/feature_usage`
- Weekly exports: `analysis/exports/week-*/export_*.zip`
- Qualitative: user feedback, planning poll (n=___)

**Tools:** Python, pandas, matplotlib, Jupyter, Palph `/export all`

**Cohort definition:** ISO week of `/start` (signup date)

**Activity definition:** `activity_progress` (study_sessions + progress tables) for retention; `activity_events` for heatmap

---

## 4. Analysis

### 4.1 Activation Funnel

_Insert funnel chart: `analysis/outputs/activation_funnel.png`_

**Key finding:** ___

| Step | Users | % | Drop-off |
|------|-------|---|----------|
| Registered | | 100% | |
| session_started | | | |
| subject_picked | | | |
| mode_picked | | | |
| First learning action | | | |

### 4.2 Time-to-Value

Median hours from signup to first `session_started`: ___

% activated within 24h: ___

### 4.3 Retention

_Insert: `analysis/outputs/retention_heatmap.png`_

| Cohort | D1 | D7 | Comment |
|--------|----|----|---------|
| Week 1 | | | |
| Week 2 | | | |

### 4.4 Feature Adoption

_Insert: `analysis/outputs/feature_adoption.png`_

Top features: ___
Features correlated with D7: ___

### 4.5 Session Patterns

_Insert: `analysis/outputs/session_heatmap.png`_

Peak study hours: ___
Recommendation for reminder timing: ___

---

## 5. Insights (5–7)

1. 
2. 
3. 
4. 
5. 

---

## 6. Recommendations

| Priority | Recommendation | Expected impact | Effort |
|----------|----------------|-----------------|--------|
| P0 | | | |
| P1 | | | |
| P2 | | | |

---

## 7. Next Experiment

**Hypothesis:** ___

**Test:** ___

**Success metric:** ___

**Example:** If planning poll ≥40% «14-day sprint» → ship diagnostic + 14-day plan MVP for ОПМ.

---

## Appendix

- [product_framework.md](../product_framework.md)
- [analytics_logbook.md](../analytics_logbook.md)
- [product_decision_log.md](product_decision_log.md)
- Admin screenshots: `/analytics`, `/funnel`, `/cohort_stats`
