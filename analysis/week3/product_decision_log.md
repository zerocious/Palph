# Product Decision Log — Palph

> Doc sync 2026-05-25

> Записывай каждое продуктовое решение: что решили, на каких данных, какие альтернативы, что проверить дальше.

---

## Template

```markdown
### DEC-NNN: Title (YYYY-MM-DD)

**Decision:** What we decided  
**Status:** proposed / accepted / rejected / deferred  

**Context:** 1–2 sentences  

**Data:**
- Metric / observation that triggered this

**Alternatives considered:**
1. Option A — pros/cons
2. Option B — pros/cons

**Expected outcome:** What should improve  

**Follow-up metric:** How we'll know it worked  

**Owner:** ___
```

---

## Decisions

### DEC-001: Launch analytics baseline before public release (prelaunch)

**Decision:** Run `pa_verify_export.py --save-baseline` before first marketing post  
**Status:** accepted  

**Context:** PA portfolio requires reproducible data pipeline from day 0.  

**Data:**
- 20 exportable tables + 10 analytics commands shipped in v0.8
- Empty events table without test-user flow

**Alternatives:**
1. Start collecting after launch — risk missing early cohort behavior
2. External analytics (Amplitude) — overkill for MVP

**Expected outcome:** Clean baseline for week-over-week comparison  

**Follow-up:** Compare week-1 export row_counts vs prelaunch  

---

### DEC-002: ___ (Week 1)

**Decision:**  
**Status:** proposed  

**Context:**  

**Data:**
- 

**Alternatives:**
1. 
2. 

**Expected outcome:**  

**Follow-up metric:**  

---

### DEC-003: Planning feature priority (Week 2)

**Decision:** ⬜ Build / ⬜ Defer / ⬜ UX-only  
**Status:** proposed  

**Context:** Backlog has adaptive plan + 14-day sprint; need user validation.  

**Data:**
- Planning poll: n=___, top option=___
- Behavior: ___% users repeat subject→mode without completing session

**Alternatives:**
1. Ship 14-day sprint first (exam use-case)
2. Ship «Continue» button only (TODO #21 UX)
3. Full adaptive plan (higher cost)

**Expected outcome:**  

**Follow-up metric:** D7 retention of plan users vs control  

---

### DEC-004: ___ (Week 2)

---

### DEC-005: ___ (Week 3)

---

_Add at least 5 decisions by end of Week 3._
