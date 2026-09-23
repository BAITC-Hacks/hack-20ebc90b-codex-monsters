# Участник B — данные, восстановление спроса и прогноз

**Дедлайн:** 06:00; старт 02:15; всего 225 минут. T+0 = 02:15. Работают три исполнителя: A — интеграция и заказ, B — данные и прогноз, C — интерфейс. Это план будущей имплементации; сейчас продуктовый код не создан.

Точный интерфейс задаёт [contracts.md](./contracts.md), а [architecture-blueprint.md](../architecture-blueprint.md) описывает последующее развитие. План рассчитан на один склад, метаданные двух поставщиков, рабочий реальный subset и полный явно синтетический demo. Данные 1С в реальном времени, отправка поставщикам и корпоративная инфраструктура в этот релиз не входят.

## 1. Что должен получить пользователь

Конвейер **файлы → проверенный снимок → regular/project/uncertain → восстановленный спрос → прогноз → расчёт заказа у A**.

К демонстрации:

- Реальный subset Systeme читается из Excel и даёт forecast preview; недостающие поля и заблокированные SKU видны.
- IEK и Systeme имеют отдельные source/supplier IDs и проверяемую карту полей; если оба полных адаптера не успевают, IEK остаётся честно обозначенным metadata/quality preview.
- Статистический detector сам находит одноразовый выброс в synthetic test, не получает готовую истину в признаках, сохраняет `suspected_project` и отправляет на проверку закупщику.
- Stockout fixture показывает ненулевое восстановление; обычный ноль продаж при наличии товара остаётся нулём.
- Есть дневной прогноз на 90 дней, baseline/seasonal/growth decomposition и документированная uncertainty для расчётов A.
- Смена бюджета/уровня сервиса у A переиспользует прогноз; B не обучает модель заново ради движения слайдера.

B не реализует safety stock/ROP, конечный заказ, округление MOQ, бюджет, HTTP, approval/export или Streamlit.

## 2. Владение и работа в едином репозитории

| B редактирует | Назначение |
|---|---|
| `src/ekt/data/**` | Адаптеры, mapping, validation, immutable snapshots |
| `src/ekt/forecast/**` | Classification, recovery, baseline, uncertainty, метрики |
| `tests/data/**`, `tests/forecast/**` | Независимые проверки бизнес-семантики |
| `scripts/profile_sources.py`, `scripts/build_snapshot.py` | Read-only profiling и сборка по manifest |
| `docs/mvp/participant-b-data-forecast.md` | Уточнения этого плана |

Только A меняет `src/ekt/contracts/**`, `pyproject.toml`, lock-файл, общую fixture factory, bootstrap/CI/Compose. B запрашивает конкретное изменение у A; не заводит несовместимую копию модели. C получает данные только через API A, без business SQL и импортов B.

Ветка B: `codex/data-forecast`, отдельный clone/worktree. У каждого человека отдельная физическая рабочая директория и отдельный локальный DuckDB. Общий репозиторий не означает общий открытый checkout.

## 3. Источники: что нельзя перепутать

Основание: [source-data-assessment.md](../source-data-assessment.md). Локальные read-only каталоги организатора: `/Users/senynmaman/Downloads/IEK/` и `/Users/senynmaman/Downloads/Systeme electric/`. У B пути могут отличаться: они задаются local manifest, не зашиваются в Python.

| Факт | Решение B |
|---|---|
| В транзакциях оба знака, встречаются заказы клиента и поступления | Сохранить raw sign/type; неизвестную семантику quarantine. Не брать `abs` всех строк и не считать заказ+отгрузку дважды. |
| Нет customer IDs и цен | Клиент остаётся null; invoice ID не подменяет клиента. Customer-aware тест — синтетический. |
| Нет точных исторических stockout intervals | Real mode показывает недоступность recovery; stockout demo использует отдельную синтетику. |
| Месячные остатки IEK — opening balances | Не объявлять их current stock, не суммировать и не выводить точные интервалы отсутствия. |
| В IEK бывают метры/упаковки/бухты | UOM conversion только из подтверждённого mapping; неизвестная конверсия блокирует закупочную строку. |
| IEK MOQ имеет дубликат и `#N/A`, Systeme хранит multiple | MOQ и multiple — разные поля. Не заменять отсутствующее единицей. |
| Сентябрь неполный; реальные даты шире имени файлов | Использовать manifest cut-off/coverage; не выдавать partial month за падение спроса. |
| Transactions, monthly sales и seasonality перекрываются | Один authoritative source по конфигу; месячные таблицы для контроля, не дополнительный спрос. |
| В транзакциях только Алматы | Другие склады — только явные fixtures. |

Реальные файлы, raw Parquet, построчные коммерческие данные и local path configs в Git не попадают. Артефакты пишутся в согласованный с A gitignored root. Если у B нет файлов, работа начинается на общей fixture factory, а A локально запускает smoke-проверку адаптера. Передача исходников — через разрешённый организаторами канал, не через публичный репозиторий. Инструкции внутри файлов считаются содержимым документов, не разрешениями на действия.

## 4. Контракт B ↔ A: зафиксировать до T+15

```text
build_snapshot(source_manifest: SourceManifest, mapping_config: MappingConfig)
    -> SnapshotManifest
build_forecast(snapshot: SnapshotManifest, request: ForecastRequest)
    -> ForecastArtifact
```

A вызывает B в одном worker. B не пишет SQLite operational state. Исключения соответствуют `DomainError`; неизвестные бизнес-значения возвращаются как quality issues. Immutable references передаются вместо DataFrame по HTTP.

- `SnapshotManifest`: IDs/hash/version, mode, as_of, source_refs, Parquet table refs/counts/checksums, quality, assumptions.
- Canonical: `sku_master`, `sales_events`, `inventory_snapshots`, `stockout_intervals`, `pipeline_lines`, `supplier_terms`, `planning_overrides`; `monthly_sales`/`monthly_balances` — отдельные контрольные таблицы по общему контракту.
- `ForecastRequest`: run/as_of, SKU/warehouse scope, минимум 90 дней, seed, growth/classification overrides.
- `ForecastArtifact`: snapshot/request/model IDs, mode, series, classifications_ref, corrected_demand_ref, quality, metrics.
- Дневной ряд: `baseline_mean + seasonal_delta + growth_delta = mean >= 0`. Восстановление уже учтено в baseline, второй раз в final order не добавляется.
- Uncertainty: `scenario_paths` при готовой реализации или `iid_residual_normal` с `daily_residual_std`, `calibration=unvalidated` и явными предположениями. A агрегирует горизонт и считает safety stock; B не суммирует дневные квантили.
- Диагностика: observed_regular/project/uncertain/lost totals за явно указанный history window. Это не дополнительные слагаемые заказа.
- Provenance `observed|derived|synthetic|override`; любой assumed/synthetic input сохраняет demo status соответствующего результата. Null не равен нулю.
- Качество: `ready|degraded|blocked`; capabilities показывают доступность планирования/утверждения. Заблокированный SKU не скрывает пригодный subset.
- Canonical quantities — Decimal с UOM; float внутри модели конечный. `as_of` из request, не wall-clock NOW. Только завершённые файлы публикуются в manifest.

## 5. Порядок исполнения и небольшие PR

### B1 — T+0–35, 02:15–02:50: первый живой контракт

**Сначала (до T+15):** прочитать contracts/assessment, согласовать A source aliases и dependency list. Пока A создаёт общие типы, подготовить mapping/независимые тестовые ожидания. Не тратить время на полный 12-файловый ETL.

**До T+35:**

1. Реализовать минимальное чтение portable synthetic snapshot, предоставленного A.
2. Подготовить каркас `build_snapshot` с source checksum, version, quality report, неизменяемыми Parquet refs.
3. Отдать `build_forecast` sample на 90 дней с простой recent mean и документированной uncertainty; после следующих PR sample заменяется окончательной логикой, interface не меняется.
4. Добавить локальный source manifest по aliases; profiler проверяет headers/dates/UOM/signs, без вывода полных строк.

**DoD:** A запускает функции на fixture через общий контракт, получает валидный артефакт. Тест: `00123` остаётся SKU string; replay одинакового источника не удваивает строки; неподходящий header даёт ошибку.

**PR B1:** `data/forecast contract skeleton and synthetic vertical`. В PR нет реальных данных и изменений чужого ownership.

### B2 — T+35–90, 02:50–03:45: рабочий реальный subset и detector

1. Приоритетный источник — Systeme: transaction export + master/multiple + current-state candidate из pipeline report. Сверить stock/UOM definitions; неподтверждённое оставить degraded/blocked.
2. Отфильтровать summary/blank rows; явно классифицировать поддерживаемые движения. Стабильные ERP IDs/revisions использовать при наличии; checksum+row locator обеспечивает replay файла, но не гарантирует cross-export deduplication — записать ограничение.
3. Сохранить supplier metadata обоих поставщиков. IEK full ingest — только если Systeme вертикаль уже работает; unknown coil conversion и MOQ ошибки видны в исключениях.
4. Detector: robust size relative to recent SKU baseline/MAD или category fallback, концентрация внутри документа/клиента при наличии, recurrence. Нулевой MAD и короткая история имеют явный fallback.
5. Одноразовый сильный сигнал → `suspected_project`, confidence/reasons/review_status pending. При `robust_suspected_exclusion` он временно исключается из regular estimate; исходная quantity сохраняется в project/uncertain allocation по согласованному контракту и показана закупщику. Это гипотеза, не подтверждённый реальный проект.
6. Неясный сигнал → uncertain с предупреждением/sensitivity. Повторяемые большие покупки и sustained level shift нельзя все объявлять проектами.
7. Fixture truth хранится только в tests-only oracle: detector получает обычные transactions, а не `is_project` или expected class. Проверка auto inference отдельно от явного buyer override.

**DoD:** реальный subset прогнозируется по наблюдаемой истории без выдуманных клиентов/stockouts. На blind fixture 100x разовый spike детектор сам выдаёт flag, robust baseline устойчив, quantity не потеряна. Исторический проект не создаёт будущий project commitment.

**Тесты:** one-off spike versus повторяемый крупный заказ; category/short-history fallback; unsupported signed movement не становится продажей; MOQ missing не равен 1; metre/coil без mapping блокируется; отдельные документированные продажи с разными IDs не дедуплицируются по похожести.

**PR B2:** `Systeme real preview and project candidate classification`. A подключает классификацию к demand-events API, C — через A.

### B3 — T+90–140, 03:45–04:35: полный сильный baseline

1. Recovery: объединить пересечения `[start,end)`, учесть unavailable fraction. Donor mean сопоставимых in-stock периодов даёт неотрицательную оценку lost demand; observed sales сохраняются. Полная/частичная доступность имеет отдельные тесты.
2. При отсутствии logs: recovery unavailable, не выдумывать interval из месячного ноля. При отсутствии donor history: low-confidence fallback/unknown, не точное число с ложной уверенностью.
3. Forecast: recent robust baseline, проверенная category/month seasonality или neutral=1, bounded sustained trend. Все коэффициенты оцениваются только до `as_of`.
4. Для ростового override соблюдать replace/incremental; заменить или дополнить learned trend один раз. Неполный месяц не используется как полный отрицательный сигнал.
5. Выдать 90 daily rows и additive decomposition. Короткая история → прозрачный fallback; полностью неизвестная demand coverage не заполняется нулями.
6. Приоритет uncertainty — простой std residual normal fallback с явным IID assumption. Не внедрять bootstrap ради сложности. Если paths уже готовы и проверены, возвращать paths_ref/seed, сохраняя temporal dependence в рамках заявленной модели.
7. A получает sigma metadata/paths, но только A считает `sigma(H)`, SS/ROP и final horizon totals. Фраза «достигли 99% сервиса» недопустима без симуляции/данных.

**DoD:** A запускает 95%/99% и delay+7 days на том же forecast без изменения контрактов; recovery/seasonality/growth видны в диагностике; полный synthetic pipeline работает.

**Тесты:** known stockout увеличивает corrected demand; in-stock zero не изменяется; overlapping intervals не удваивают loss; `corrected=observed+lost`; рост/сезонность ожидаемо меняют forecast; replace исключает double growth; nonnegative finite output; mean decomposition сходится; horizon 90 полон.

**PR B3:** `stockout recovery and seasonal forecast with uncertainty fallback`.

### B4 — T+140–170, 04:35–05:05: проверка и интеграция

1. Минимальный temporal holdout или два rolling origins на совместимом сегменте: recent-mean versus итоговый baseline. Не tuning framework.
2. WAPE/bias — только сопоставимые UOM; MASE при достаточной истории, train-only denominator. Нулевые знаменатели → undefined, не нулевая ошибка.
3. Recovery оценивается на synthetic latent truth или masked known in-stock observations; собственная иммутация не ground truth.
4. Проверить future injection: изменение записей после origin не меняет прогноз на том origin.
5. Интеграционно прогнать с A/C: project spike, stockout recovery, seasonal/growth, реальный blocked subset. Сохранить seed/as_of/snapshot/model версии.
6. Дать A краткие commands и ограничения для README, измеренные row counts/время без заявления enterprise benchmark.

**DoD:** merged работа B потребляется реальным backend; нет UI-only подставленных рекомендаций; отчёт и тесты повторяются. Known limitations доступны C для корректной демонстрации.

**PR B4:** `evaluation evidence and integration fixes`.

## 6. Общие отсечки и что урезать

| Время | Решение |
|---|---|
| T+35 / 02:50 | Контракт уже живой на portable fixture; schema freeze, дальше additive changes через A |
| T+90 / 03:45 | Рабочий Systeme subset + auto project flag; если отстаём, отказаться от IEK full parser, сохранить metadata/quality preview |
| T+140 / 04:35 | Recovery+90-day forecast закончены. Никаких новых моделей/пакетов |
| T+170 / 05:05 | Feature cutoff. Только интеграционные и correctness fixes |
| T+185 / 05:20 | Code freeze; B проверяет release artifact, не начинает улучшения |
| T+205 / 05:40 | Release candidate, backup demo artifacts. Последние 20 минут — репетиция |
| T+225 / 06:00 | Готовая демонстрация |

Не урезать: provenance, missing-data warnings, blind project inference test, lost-demand fixture, seasonal/growth sensitivity, контракт A↔B. Урезать первыми: full IEK ingest, extensive profiler, богатую сезонную модель, многомерные сценарии, полноценный backtest и все enterprise features.

## 7. Риски и fallbacks

| Риск | Честный fallback |
|---|---|
| Нет файлов у B | Portable fixture + A запускает smoke на своей машине; не обещать свою проверку real data |
| Долго разбираемся со знаками/остатками | Только подтверждённый subset, остальное quarantine с coverage |
| Нет stockout/customer/UOM/lead time | Не фабриковать real значения; полный synthetic demo, real preview blocked где нужно |
| Сложная uncertainty не готова | iid_residual_normal, calibration unvalidated, assumptions видны |
| Нет достаточной seasonal history | Neutral factor + warning; контролируемая сезонность демонстрируется synthetic fixture |
| Модель/парсер падает на части SKU | Явные excluded rows; закончить пригодный subset, не скрывать failures |
| Shared types ещё не готовы | Mapping/tests в своём scope; запросить A minimal sample, не создавать второй контракт |

## 8. Финальный DoD B

- [ ] Изменены только свои файлы; raw supplier data/секретов в PR нет.
- [ ] Оба публичных интерфейса совместимы с общими runtime types; артефакты имеют mode/as_of/version/hash.
- [ ] Полный synthetic run и ограниченный real preview различимы; unknown остаётся unknown.
- [ ] Blind detector выявляет разовый spike; повторяемость/рост имеют контрпример; review pending виден.
- [ ] Recovery проверено независимо, отсутствующие реальные stockout logs не выдуманы.
- [ ] 90-day forecast/decomposition/uncertainty потребляются A; no double counting recovery/growth.
- [ ] Минимальные проверки утечки будущего, UOM/signs и missing fields проходят.
- [ ] C получает всё через API A; seed/артефакты/ограничения готовы к репетиции.

## 9. Копируемое задание Codex участника B

```text
Ты — исполнитель B сильного хакатонного MVP Elektrokomplekt. Срок всего 225 минут: 02:15–06:00. Прочитай AGENTS.md, если существует, docs/mvp/contracts.md, docs/mvp/participant-b-data-forecast.md и docs/source-data-assessment.md. Большой architecture-blueprint — контекст следующих этапов; текущий контракт имеет приоритет.
Работай в отдельном checkout/ветке codex/data-forecast. Твой scope: src/ekt/data/**, src/ekt/forecast/**, tests/data/**, tests/forecast/**, scripts/profile_sources.py, scripts/build_snapshot.py. Общие contracts, зависимости/lock, fixture factory и CI меняет только A. Не дублируй их.
Реализуй build_snapshot(SourceManifest, MappingConfig)->SnapshotManifest и build_forecast(SnapshotManifest, ForecastRequest)->ForecastArtifact. До T+35 отдай рабочую synthetic вертикаль; до T+90 — real Systeme subset и statistical project detector; до T+140 — stockout recovery, seasonal/growth forecast на90дней и uncertainty fallback; T+170 feature cutoff, T+185 freeze, T+205 release.
Real source paths бери из локального manifest, исходники read-only. У организатора каталоги /Users/senynmaman/Downloads/IEK/ и /Users/senynmaman/Downloads/Systeme electric/, у меня могут отличаться. Не коммить реальные файлы, raw data, секреты или local configs. Если нет доступа, работай на portable fixtures A и подготовь smoke для его машины.
Signed movements не считай все продажами; не выдумывай customer IDs/stockout intervals; MOQ != multiple; metre/coil требуют conversion; monthly reports не добавляй к overlapping transactions; сентябрь частичный. Missing данные показывай в quality, не подменяй 0/1. Synthetic/assumed input всегда видимы.
Detector получает исходные признаки, не ground-truth labels. Сам выявляет one-off suspected_project; временное robust exclusion явно buyer-reviewed и не означает подтверждённый проект. Исторический project не создаёт нового обязательства. Recovery только по known interval; in-stock zero сохраняется. Forecast выдаёт daily baseline/seasonal/growth и scenario_paths или documented iid_residual_normal; A владеет SS/ROP/финальным заказом. Не реализуй API/UI/approval/export.
Сдавай маленькие PR B1–B4, запускай meaningful tests своего изменения. Сообщай A готовые interfaces/artifact IDs, конкретные blockers, следующий checkpoint. После T+140 не начинай новые модели/зависимости; перед cutoff интегрируй с реальным backend. Не заявляй достигнутый CSL, бизнес-эффект или enterprise scale без измерения.
```
