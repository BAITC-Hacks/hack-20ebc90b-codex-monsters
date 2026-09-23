# Контракты сильного MVP — версия 1

Статус: спецификация MVP 1.0; runtime-модели реализованы в `src/ekt/contracts`. Проверяемые HTTP-схемы доступны в `/docs` и `/openapi.json`; состояние интеграции — [status-a.md](status-a.md). Владелец общих контрактов — исполнитель A. Срок команды: 225 минут, поэтому используем один Python-проект, FastAPI, Streamlit и один backend worker. Корпоративные контракты из architecture-blueprint.md остаются направлением развития; для этого релиза точный интерфейс задаёт этот документ.

## 1. Общие правила

- Python 3.11; Pydantic v2 для runtime-моделей. A фиксирует совместимые версии зависимостей в общем lock-файле. Новые зависимости B/C запрашивают у A.
- `ID` — непустая строка. SKU не преобразуются в числа. `Qty` и `Money` — Decimal, в JSON строки; деньги всегда с валютой. `Date` — YYYY-MM-DD, `Timestamp` — RFC3339 с timezone. Арифметика бюджета не использует binary float.
- Один базовый UOM на SKU. Покупная единица, коэффициент пересчёта, MOQ и multiple сохраняются отдельно. `null` означает неизвестно, не ноль.
- Все числа в JSON, отмеченные `float`, конечные; NaN/Infinity запрещены. Вероятности в [0,1], целевой CSL строго между 0 и 1.
- `mode`: `synthetic_demo` или `real_preview`. Сквозной сценарий synthetic_demo содержит заметную маркировку и разрешает только демонстрационный CSV. Реальные строки с блокирующими пробелами нельзя утверждать/экспортировать как заказ.
- `as_of` — явный момент воспроизведения данных, а не неявный NOW. Возраст вычисляется относительно выбранного контекста. Replay/demo не маркируется как текущая оперативная рекомендация.
- `provenance`: `observed|derived|synthetic|override`; сохраняется на уровне записей/полей, где смешаны основания. Любой synthetic/assumed input делает соответствующий результат демонстрационным, даже если остальная история реальная.
- Источники/артефакты передаются ссылками; DataFrame не передаётся по HTTP. Пустой результат отличается от ошибки и блокировки.

## 2. Владение и зависимости

```text
src/ekt/contracts/    A: Pydantic schemas, shared enums, OpenAPI
src/ekt/data/         B: source adapters, validation, immutable snapshots
src/ekt/forecast/     B: classification, recovery, baseline, uncertainty
src/ekt/planning/     A: SS/ROP, netting, rounding, budget, explanations
src/ekt/api/          A: routes, lifecycle, versions, approvals, exports
src/ekt/storage/      A: SQLite run/proposal state and artifact registry
apps/buyer_ui/        C: API client, Streamlit views, presentation
```

API зависит от planning и forecast; forecast зависит от canonical snapshot; UI зависит только от HTTP-контрактов. Import API из forecast/data, чтение SQLite из UI и дублирование расчётных формул в UI запрещены этой схемой владения. B может использовать DuckDB как свой reader, но не писать backend operational state. SQLite пишет единственный backend coordinator.

## 3. Граница A ↔ B

Публичные Python-интерфейсы, которые реализует B:

```text
build_snapshot(source_manifest: SourceManifest, mapping_config: MappingConfig)
    -> SnapshotManifest

build_forecast(snapshot: SnapshotManifest, request: ForecastRequest)
    -> ForecastArtifact
```

A вызывает их из одного worker-процесса. Чистые расчёты не знают FastAPI/Streamlit и не меняют proposal/approval. Исключения превращаются в `DomainError(code, message, affected_sku_ids, retryable)`; неизвестная обязательная бизнес-семантика отдаётся quality issue, а не guessed value.

### SnapshotManifest

| Поле | Тип / смысл |
|---|---|
| `schema_version` | literal `1.0` |
| `snapshot_id`, `mapping_version`, `manifest_hash` | ID |
| `mode`, `as_of`, `created_at` | enum, Timestamp, Timestamp |
| `source_refs` | list `{source_id:ID, checksum:ID, kind:string, local_ref:string}`; local paths остаются backend-side |
| `tables` | dict `table_name -> {uri:string, format:parquet, row_count:int, checksum:ID}` |
| `quality` | QualityReport |
| `assumptions` | list `{field:ID, value:string, provenance:enum, reason:string, scope_ids:ID[]}` |

Canonical tables:

| Таблица / ключ | Поля |
|---|---|
| `sku_master` / sku_id | sku_id, name, category_id?, base_uom, purchase_uom?, base_units_per_purchase_uom?, quantity_quantum, supplier_id, supplier_article?, provenance |
| `sales_events` / event_id | source_id, row_ref, doc_id, line_id?, revision?, event_at, sku_id, warehouse_id, event_type, quantity_base:Qty, demand_effect:increase/decrease/none, customer_token?, provenance |
| `inventory_snapshots` / snapshot+sku+warehouse | as_of, on_hand_base, reserved_base, blocked_base, free_base, accounting_definition_version, provenance |
| `stockout_intervals` / sku+warehouse+start | start_at, end_at?, unavailable_fraction:float, evidence:observed/inferred/synthetic, confidence:float |
| `pipeline_lines` / po_line_id | sku_id, supplier_id, warehouse_id, remaining_base_qty, eta_start?, eta_end?, eta_semantics:window/deadline/unknown, status, provenance |
| `supplier_terms` / supplier+sku+warehouse | moq_purchase:Qty?, pack_multiple_purchase:Qty?, lead_time_days:int?, review_days:int?, cost_per_base:Money?, currency?, valid_at, provenance |
| `planning_overrides` / scope+parameter+version | scope_id, parameter, value, valid_from, valid_to?, provenance, reason |
| `monthly_sales`, `monthly_balances` | отдельные контрольные таблицы; period_start, period_end, sku_id, measure, value?, source_id, coverage; не добавляются к overlapping transactions |

Поля без указанного типа повторно используют ID/Qty/Date/Timestamp из общих правил по смыслу. A материализует поля в Pydantic/Arrow и фиксирует sample JSON; этот документ не подменяет runtime validation. Отсутствующие бизнес-определения отражаются в quality, а не скрываются обязательностью схемы.

### QualityReport

`{status:ready|degraded|blocked, accepted_rows:int, rejected_rows:int, affected_skus:int, issues:QualityIssue[], capabilities:Capabilities}`.

`QualityIssue = {code:ID, severity:info|warning|blocking, scope_ids:ID[], source_ref:string?, message:string}`.

`Capabilities = {can_plan:bool, can_approve:bool, can_export:bool, budget_available:bool, customer_detection_available:bool, observed_stockouts_available:bool, reasons:string[]}`.

Aggregate quality показывает покрытие, но один заблокированный SKU не скрывает остальные. Proposal включает только однородные по mode/currency/условиям пригодные строки; excluded rows всегда видны в отдельном списке причин. Блокирующие поля для реальной строки: текущий stock/netting, UOM conversion, MOQ/multiple, lead time/review, supplier mapping. Если одна из этих величин synthetic/assumed, строка остаётся demo.

### ForecastRequest

`{run_id:ID, as_of:Timestamp, sku_ids:ID[], warehouse_ids:ID[], horizon_days:int, seed:int, growth_overrides:GrowthOverride[], review_overrides:ClassificationOverride[]}`.

`GrowthOverride = {category_id:ID, mode:replace|incremental, rate:float, valid_from:Date, valid_to:Date}`. Rate > -1. Модель не применяет один тренд дважды.

`ClassificationOverride = {event_ids:ID[], label:regular|project, reason:string}`. UI редактирование классификации — stretch, контракт резервируется; демо detector получает исходные признаки, не ground-truth labels тестов.

Horizon покрывает max(L+R+scenario_delay), минимум 90 дней для стандартного fixture; если больше доступного горизонта, явный `HORIZON_TOO_SHORT`. Дневные месячные паттерны могут быть приближением, источник и метод видны.

### ForecastArtifact

`{schema_version:"1.0", forecast_id:ID, snapshot_id:ID, request_hash:ID, model_version:ID, seed:int, as_of:Timestamp, mode:enum, series:ForecastSeries[], classifications_ref:string, corrected_demand_ref:string, quality:QualityReport, metrics:Metric[]}`.

`ForecastSeries`:

| Поле | Тип / ограничение |
|---|---|
| `sku_id`, `warehouse_id`, `base_uom` | ID |
| `daily` | list `{date:Date, baseline_mean:float, seasonal_delta:float, growth_delta:float, mean:float}` |
| `uncertainty` | `{method:scenario_paths|iid_residual_normal, daily_residual_std:float?, paths_ref:string?, path_count:int?, calibration:unvalidated|backtested, assumption_note:string}` |
| `observed_regular_total`, `estimated_lost_total`, `project_total`, `uncertain_total` | Qty; diagnostic history window, не дополнительные слагаемые final order |
| `classification_policy` | `review_confirmed|robust_suspected_exclusion|regular_only` |
| `warnings` | QualityIssue[] |

`mean = baseline_mean + seasonal_delta + growth_delta >= 0` в tolerance 1e-8. Lost demand включён в базовый прогноз один раз. Для mode scenario_paths B возвращает matrix `scenario_id,date,quantity`, одинаковые сценарии для разных policy runs. Для normal fallback A использует sigma(H)=std_daily*sqrt(H) только с явной IID/normal assumption; не заявляем измеренный CSL. Реальный uncertainty качество не завышается из-за импутации.

`classifications` records: event_id, sku_id, warehouse_id, observed_qty, regular_qty, project_qty, uncertain_qty, label, reason_codes, confidence, review_status. Large one-off может быть `suspected_project`, excluded by configured robust policy, но не объявляется подтверждённым проектом. Pending event показывается покупателю. В fixtures truth labels находятся в tests-only oracle.

## 4. Граница backend ↔ UI

Все пути начинаются `/v1`. Query pagination: `limit` default50/max200, `cursor?`. Timestamp/context и mode присутствуют во всех детальных экранах.

| Метод / route | Request | Response |
|---|---|---|
| GET `/health` | — | `{status:"ok",version:string}` |
| GET `/sources` | — | Metadata зарегистрированных локальных source IDs, import state, counts; никаких file contents |
| POST `/snapshots` | `{source_ids:ID[],mapping_version:ID,mode:enum,as_of:Timestamp}` | 202 `{job_id:ID,status_url:string}` |
| GET `/jobs/{id}` | — | JobStatus, `result_ref?` |
| GET `/snapshots/{id}` | — | Public SnapshotManifest без machine-specific raw paths |
| POST `/planning-runs` | `{snapshot_id:ID,policy:PlanningPolicy,idempotency_key:ID}` | 202 `{run_id:ID,status_url:string}` |
| GET `/planning-runs/{id}` | — | JobStatus + `proposal_ids:ID[]`, `quality`, `forecast_id?` |
| GET `/proposals` | run_id?, supplier_id?, cursor?, limit? | `{items:ProposalSummary[],next_cursor:string?}` |
| GET `/proposals/{id}` | version? | ProposalDetail; omitted version означает current |
| PATCH `/proposals/{id}` | `{expected_version:int,edits:[{line_id:ID,purchase_qty:Qty}],reason:string}` | ProposalDetail нового version, status draft |
| POST `/proposals/{id}/approve` | `{expected_version:int,content_hash:ID}` | `{approval_id:ID,proposal_id:ID,version:int,status:"approved"}` |
| POST `/proposals/{id}/export` | `{expected_version:int,idempotency_key:ID}` | 200 `text/csv; charset=utf-8`, download filename; backend создаёт/повторно отдаёт сохранённый CSV и audit event |
| POST `/scenarios` | `{base_run_id:ID,overrides:ScenarioOverrides,seed:int,idempotency_key:ID}` | 202 `{scenario_id:ID,status_url:string}` |
| GET `/scenarios/{id}` | — | JobStatus + `{base_run_id,changed_lines,summary,assumptions}` при успехе |
| GET `/planning-runs/{id}/demand-events` | label?, cursor?, limit? | Пагинированные classification records, aggregated diagnostics |

Список sources регистрируется из локального config/CLI A+B. Upload Excel через браузер — stretch: он не нужен для презентации зарегистрированных источников. Не принимать arbitrary user filesystem path по публичному HTTP. `POST /snapshots` можно заменить готовым fixture snapshot до конца первого checkpoint, но окончательный run не должен работать только на статическом JSON.

`JobStatus = {id:ID,status:queued|running|succeeded|failed,stage:string,progress:float,result_ref:string?,error:ApiError?,created_at:Timestamp,updated_at:Timestamp}`. Состояние persists в SQLite, результат публикуется atomically. Один worker; недоделанные при restart jobs получают failed с причиной и могут быть повторены.

`PlanningPolicy = {service_metric:"cycle_service",service_target:float,review_days:int?,budget_cap:Money?,currency:string?,max_cover_days:int?,lead_time_delay_days:int,policy_version:ID}`. service target95/99 обозначается 0.95/0.99; delay0/7/14 календарных дней для demo с явным календарём.

`ScenarioOverrides = {service_target:float?,budget_cap:Money?,lead_time_delay_days:int?}`. Budget доступен только если complete comparable costs/currency по всем eligible lines. Сценарий не меняет baseline snapshot, approvals или реальные предложения. Scenario approve/export отсутствует; если нужно принять сценарий — новый обычный planning-run и отдельное утверждение.

`ProposalDetail = {proposal_id:ID,version:int,status:draft|approved,content_hash:ID,run_id:ID,snapshot_id:ID,mode:enum,supplier_id:ID,warehouse_id:ID,as_of:Timestamp,currency:string?,total_cost:Money?,capabilities:Capabilities,warnings:QualityIssue[],lines:ProposalLine[],excluded_lines:ExcludedLine[]}`.

`ProposalLine = {line_id:ID,sku_id:ID,name:string,base_uom:ID,purchase_uom:ID,recommended_base_qty:Qty,recommended_purchase_qty:Qty,selected_purchase_qty:Qty,selected_base_qty:Qty,moq_purchase:Qty,pack_multiple_purchase:Qty,conversion:Qty,rop:Qty,safety_stock:Qty,raw_need:Qty,unit_cost:Money?,line_cost:Money?,urgency:critical|soon|routine,projected_stockout_date:Date?,explanation:ExplanationComponent[],warnings:QualityIssue[]}`.

`ExplanationComponent = {code:ID,label:string,delta_base_qty:Qty,source_refs:string[],note:string?}`. Ordered sum = displayed selected base quantity. После ручной правки появляется `manual_override_delta`; recommended_qty не переписывается. Сначала clipping raw need >=0, затем MOQ/pack, затем budget allocation; отрицательные/cap adjustments также входят в ledger. Нельзя скрывать manual override внутри baseline demand.

`ExcludedLine = {sku_id:ID,reasons:QualityIssue[]}`. Строки нельзя молча потерять.

API error `{code:ID,message:string,details:object,retryable:bool}`. 409 stale version/hash; 422 invalid quantity/missing terms/unsupported budget; 403 missing role; 404 missing ID; 500 internal with safe message. C сохраняет inputs пользователя при ошибке и не показывает success из локального session_state.

## 5. Approval и export

- Demo backend identity берётся из server config с ролью planner/approver; интерфейс всегда маркирует demo identity. Это не enterprise authentication. Настоящую авторизацию/SSO не заявляем.
- Approval сохраняет immutable audit record (actor, timestamp, version/hash). Atomic update + optimistic concurrency запрещают утвердить уже изменённую версию.
- PATCH создаёт новый version/status draft. Старые approval/export artifacts остаются для истории, но не утверждают новый version.
- Export требует approved exact version/hash, повторяет known quality/constraint checks, использует idempotency key. Vendor отправки/ERP endpoint в MVP нет.
- CSV содержит mode, as_of, supplier/SKU IDs, quantities/UOM, proposal/version, explanations, approval marker. Demo начинается с явной маркировки «ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ».
- Draft CSV preview — stretch и только с маркировкой draft; не смешивать с approved export.

## 6. Совместимость и freeze

До T+35 A публикует типы и sample responses. C начинает на mock responses с теми же типами, B — на маленьком synthetic snapshot. После T+35 изменения additive/nullable и по согласованию с A; breaking change одновременно обновляет B/C и tests. Не поддерживаем v2 или сложную schema registry в эти 225 минут.

Минимальные fixtures: regular+seasonal+growth, 100x project candidate, stockout, metre/coil, delayed pipeline, invalid MOQ, stale approval, missing costs. Seed и as_of фиксированы. Fixture rows не содержат исходных коммерческих данных. A владеет factory/common samples, B независимыми expected outcomes, C screenshots/demo narrative.
