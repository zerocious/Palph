Сырые идеи без оценки → [BACKLOG.md](BACKLOG.md). Сюда (TODO.md) попадают
только размеченные задачи. История shipped'нутого живёт в
[session_notes.md](session_notes.md) — туда же и заглядывать для context'а
«что мы уже сделали».

Example:

[Тип] Краткий заголовок
Ценность: зачем это пользователю / продукту
Критерий готовности: как понять, что сделано
Приоритет: Must / Should / Could / Won't (на сейчас)

---

## Pre-release polish (до публичного v0.8-анонса)

Code-side бот feature-complete от original brief (см. session_notes
2026-05-20). Остались задачи без code-changes:

[Арт] Реальная графика питомца
Ценность: текущие placeholder PNG в `assets/pet/` функциональны, но
programmer-art ugly. Финальный visual polish.
Готовность: дроп replacement-файлов (125 PNG + 5 GIF, naming-схема
`<emotion>_<color>_<accessory>.png` и `<emotion>.gif`) поверх существующих
в `assets/pet/`. Без code-changes — render_pet уже работает.
Приоритет: Should (до публичного запуска).

[Контент] Разделы II–IV для «Основы производственного менеджмента»
Ценность: пользователь сейчас может пройти только Раздел I; брифу нужны
все четыре, иначе квиз ощущается как заглушка.
Готовность: файлы
`study_materials/industrial-management/situational/section-{ii,iii,iv}.txt`
содержат ≥10 терминов каждый в формате
«термин || определение || ключевые слова || ситуация»; кнопки разделов
автоматически появятся (data-driven).
Приоритет: Should (до публичного запуска).

---

## Бэклог (deferred, не в текущем спринте)

[Алгоритм] SM-2 для **ситуационных** квизов
Для флэш-карт ✅ сделано в v0.7 (см. session_notes 2026-05-17). Для
ситуационных квизов оставлено отложенным.
Ценность: бриф упоминает SM-2; сейчас фиксированные интервалы [1,2,4,7]
не адаптируются под сложность термина для конкретного пользователя.
Готовность: в `quiz_progress` добавляются `ease_factor`, `interval_days`,
`repetitions`; переиспользуется чистая функция `services.sm2_update()`
(уже написана).
**Блокер:** keyword-matching grader даёт почти бинарный сигнал
(2+ ключевых слов или нет), а SM-2 требует градиент 0–5. Сначала нужен
лучший grader (semantic similarity или промежуточный «частично знал»),
иначе SM-2 деградирует до того же бинарного поведения.
Приоритет: Won't (на сейчас) — фиксированных интервалов хватает до
появления более качественного grader.

[Фича] Daily tasks
Ценность: дополнительный игровой механизм — задание дня с бонусом;
в проектном контексте отмечено как «stub exists», но в коде следов нет.
Готовность: новая таблица `daily_tasks`, кнопка в главном меню, генерация
задачи в начале дня, проверка выполнения, начисление бонуса.
Приоритет: Won't (на сейчас) — не входит в MVP-scope из брифа.

---

## PA-аналитика roadmap (formalized 2026-05-21)

Контекст: проект используется как booster резюме для роли product analyst
intern/junior. Цель — превратить бот в источник данных для реалистичного
PA-анализа в Jupyter.

Что уже доступно (админ-команды + `/export all` ZIP + 8 reference SQL +
A/B framework + 133 PA-теста) — см. [README.md](README.md) и
[session_notes.md](session_notes.md).

Tier'ы по signal-per-effort. Пункты #1 (A/B framework) и #3 (reference SQL)
ship'нуты в PR #6. Пункт #2 (user properties via /start onboarding)
намеренно опущен по решению пользователя 2026-05-21. Ниже — что осталось.

**Tier 2 — data hygiene, «mature data person»**

4) [Аналитика] Event schema documentation
Ценность: bot self-documenting для будущего аналитика (включая future you
через 6 месяцев). Маркер «mature data person» в резюме.
Готовность: `docs/events_schema.md` — таблица: event_name | when_fires |
required_properties | optional_properties | example. Покрывает все 14
hook'ов из `bot.py` + любые добавленные с тех пор. Опционально drift-test,
парсящий `EventRepository.log` вызовы и сверяющий с .md.
Приоритет: Should (низкий effort, высокий signal — ~30 минут).

5) [Аналитика] Stable CSV schema contract
Ценность: downstream notebooks знают, что ожидать от `/export all` across
версий. Предотвращает silent breakage notebooks при schema-changes.
Готовность: `analysis/schema_v1.yaml` с column types (`int`, `text`,
`iso8601`, `json`) и meaning для каждой из 11 экспортируемых таблиц.
`metadata.json` из `/export all` уже содержит `schema_version` — здесь её
формализуем. При несовместимых изменениях bump v1 → v2; старая версия
yaml остаётся для legacy notebooks.
Приоритет: Could.

6) [Аналитика] Deploy/version markers в events table
Ценность: correlate metric changes с релизами («D7 dropped 5pp after
2026-05-15 — what shipped?»). Anti-correlation = portfolio gold.
Готовность: на bot startup лог `system.deploy` event в events table с
properties `{version: "<short_hash>", started_at: "<iso>"}`. Hash из
`git rev-parse --short HEAD` (через env переменную или `__version__`
константу). Появляется в `/event_timeline` как маркер деплоя. Notebook
`release_impact.ipynb` (опционально) — overlay deploy markers на
retention-кривую.
Приоритет: Should.

**Tier 3 — статистический / аналитический polish**

7) [Аналитика] Confidence intervals на /cohort_stats
Ценность: с <100 users point estimates lie. Wilson interval для binomial
retention. Показывает statistical literacy.
Готовность: `/cohort_stats` output расширен: D1/D7/D30 ± 95% CI (Wilson).
~1 час, `scipy.stats.binom.interval` или ручной Wilson. В тестах
фиксируется boundary case (n=0, n=1, p=0, p=1).
Приоритет: Could.

8) [Аналитика] Time-to-event метрики (survival analysis)
Ценность: beyond «did they retain D7» — survival analysis это правильный
фрейминг для understanding engagement timing.
Готовность:
— Метрики в `/dau` или новой команде `/timing`:
  · Time to second session (median + p25/p75)
  · Time from signup to first 25-min session (real engagement signal)
  · Median time-between-sessions per user
— Kaplan-Meier curve в notebook (`survival.ipynb`) с `lifelines` library —
  один-import.
— Тесты на корректность time-delta расчёта (включая timezone edge cases).
Приоритет: Could.

9) [Аналитика] Anonymization helper для shared notebooks
Ценность: можно публиковать notebooks в GitHub-портфолио без утечки real
Telegram user_id'ов (которые linkable к публичным профилям).
Готовность: `analysis/anonymize.py` — функция принимает CSV или DataFrame,
хэширует user_id'ы консистентно (HMAC-SHA256 с `secret_salt` из `.env`,
не коммитится). Notebook'и опционально включают `anonymize_df(df)` step
перед сохранением outputs. README.md в `analysis/` фиксирует правило:
«никаких raw user_id в коммите».
Приоритет: Must (как только хоть один notebook публикуется в GitHub) /
Could (пока notebooks только локально).

**Tier 4 — qualitative signal**

10) [Аналитика/Качественные данные] /feedback команда
Ценность: один Telegram-message → запись в БД. Раз в неделю grep по
терминам («сложно», «не понимаю», «баг») + pair с analytics («users who
say 'сложно' churn at 2× rate» = real insight, отлично читается в резюме).
Готовность: команда `/feedback <text>` принимает свободный текст → запись
в `user_feedback(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL
REFERENCES users(user_id), text TEXT NOT NULL, sentiment TEXT, created_at
TEXT NOT NULL DEFAULT (datetime('now')))`. `sentiment` nullable
(опционально enrich позже — rules-based scorer или LLM). Админ-команда
`/feedback_dump` для CSV-экспорта. Анти-абуз: rate-limit 1 message в час
на user_id.
Приоритет: Could.

11) [Аналитика/Качественные данные] In-bot NPS survey
Ценность: NPS universally recognized метрика в резюме. Конкретное число
для slide deck'а.
Готовность: после 7 active days (`has_studied_today` true 7 раз, не
подряд) fire 1-shot prompt «Насколько вероятно, что ты порекомендуешь
Palph другу? (0–10)». Inline-keyboard 0..10. Запись в `user_nps(user_id
INTEGER PRIMARY KEY REFERENCES users(user_id), score INTEGER NOT NULL
CHECK(score BETWEEN 0 AND 10), asked_at TEXT NOT NULL, answered_at TEXT
NOT NULL DEFAULT (datetime('now')))`. Score 9–10 = promoter, 7–8 =
passive, 0–6 = detractor. Команда `/nps` (admin) — текущий NPS +
breakdown по сегментам. Один опрос на пользователя в v1.
Приоритет: Could.

**Follow-up к PR #6 (A/B framework + reference SQL)**

#1 framework + #3 reference queries ship'нуты в PR #6 (см. session_notes
2026-05-21, секция «PA-roadmap kickoff»). Что осталось end-to-end-вырубить:
— Sprinkle `get_variant(...)` на конкретной decision-точке (например,
  `pet_level_in_profile_v1` в `pet_menu` handler). Требует продуктового
  решения «какой эксперимент первым».
— Запись `variant` в `events.properties` для последующих событий
  пользователя (нужен middleware / decorator pattern; tradeoff на overhead
  при каждом event-log).
— Notebook `analysis/experiments.ipynb` с retention-curves + significance
  test per variant. Имеет смысл только когда decision-сайт сработал и
  набралось >50 user'ов per variant.

---

## 🎯 Главный портфолио-asset (внешняя аналитика)

После сбора 30+ дней реальных данных:

- Папка `analysis/` в репозитории с Jupyter notebooks
- 4 notebook'а:
  - `01_cohort_retention.ipynb` — matplotlib heatmap retention table + key findings
  - `02_activation_funnel.ipynb` — waterfall chart + drop-off insights
  - `03_feature_adoption.ipynb` — adoption per mode + корреляция с retention
  - `04_session_patterns.ipynb` — heatmap часов × дней недели + рекомендации по timing
- `analysis/README.md` с executive summary + recommendations

Это будет выглядеть в резюме как реальная PA-работа, не как «вот мой Telegram-бот».
