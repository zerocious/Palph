# Week 3 Checklist

> Palph v0.8 · doc sync 2026-05-25 — Portfolio

## День 15
- [ ] Consolidate Week 1 + Week 2 reports
- [ ] Final `pa_weekly_snapshot.py --week 3`
- [ ] Export all charts from notebooks → `analysis/outputs/`

## Дни 15–18 — Analysis
- [ ] Run 01_cohort_retention.ipynb
- [ ] Run 02_activation_funnel.ipynb
- [ ] Run 03_feature_adoption.ipynb
- [ ] Run 04_session_patterns.ipynb
- [ ] Screenshot admin dashboards for appendix

## День 19 — Writing
- [ ] Fill [case_study_template.md](case_study_template.md)
- [ ] Complete [product_decision_log.md](product_decision_log.md) (≥5 entries)
- [ ] Pick best [resume_bullets.md](resume_bullets.md)

## День 21 — Review
- [ ] Peer review case study (friend/mentor)
- [ ] Anonymize any user data in charts
- [ ] Git: commit analysis docs (not raw exports)

## Quality bar

- [ ] Case study readable in < 10 min
- [ ] Every insight backed by a number
- [ ] Every recommendation links to data
- [ ] At least 1 «what I'd do next» experiment

## Outputs folder

```
analysis/outputs/
  retention_heatmap.png
  activation_funnel.png
  feature_adoption.png
  session_heatmap.png
```

Create with notebooks or:
```python
fig.savefig("analysis/outputs/retention_heatmap.png", dpi=150, bbox_inches="tight")
```
