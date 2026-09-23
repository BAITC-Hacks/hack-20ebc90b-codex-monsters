# Исполнитель A: пользовательский Codex — backend, расчёт заказа, интеграция

Текущая реализация и interfaces: [status-a.md](status-a.md). Ниже исходный план задач, а не подтверждение полной приёмки.

## Миссия и границы

Собрать три независимые части в работающий продукт до6:00. Пользователь принимает продуктовые решения, Codex выполняет этот план. Отсчёт T0=2:15, весь срок225мин; не перезапускать отсчёт после прочтения. Источники правды: [master-plan.md](master-plan.md), [contracts.md](contracts.md), [acceptance-demo.md](acceptance-demo.md).

Владение: `src/ekt/contracts/**`, `src/ekt/planning/**`, `src/ekt/api/**`, `src/ekt/storage/**`, `tests/contracts/**`, `tests/planning/**`, `tests/api/**`, `tests/integration/**`, `tests/fixtures/**`, корневые deps/lock/config/Docker/CI/README. Не писать вместо B importer/forecast и вместо C Streamlit. Shared changes публиковать небольшими проверенными commits в main; A координирует интеграцию.

## A01. Foundation, до T+35

1. Проверить `main`, существующие изменения и актуальность общего плана. По указанию пользователя не создавать ветки, worktree или PR; сохранять чужую работу.
2. Создать Python package layout, pyproject и один lock; FastAPI/Streamlit runtime, DuckDB/Parquet, data-read libs, pytest. Не ставить Celery/Redis/Kubernetes.
3. Материализовать MVP Pydantic contracts и agreed enums, отдельно Date/Decimal serialization и domain errors. Определить один публичный snapshot reader/registry, чтобы B не угадывал storage.
4. Seeded fixture factory 12–20 SKU/2 suppliers/1warehouse, variants для acceptance. Raw oracle labels только в tests, не в detector payload.
5. Sample responses на реальные schema типы: ready/blocked snapshot, job status, proposal detail, scenario result, error. C получает их сразу; B — fixture schema.
6. Backend health и skeleton jobs/storage, простой demo actor из server config. `.env.example` без секретов; `.gitignore` исключает raw data/DB/cache/outputs/secrets.

DoD: другой участник устанавливает окружение по lock, импортирует schemas, validates samples. `tests/contracts` проверяет Quantity/Date/enums/invariants. Shared files в первом небольшом commit в main. Не ждать всей бизнес-логики, чтобы дать основу команде.

## A02. Policy и XAI, T+35–105

1. Обработать ForecastArtifact от B, сначала на fixture boundary. Суммировать daily baseline/seasonal/growth на L и L+R, проверять достаточный horizon.
2. SS/ROP: aggregate scenario paths если есть; documented IID-normal residual fallback если paths нет. Не суммировать daily quantiles, не называть target CSL фактически достигнутым сервисом.
3. Netting: on_hand-reserved-blocked согласно agreed accounting, либо verified free stock; не применять вычитания дважды. Dated pipeline консервативно по eta_end; unknown ETA не покрывает раннюю need. Project commitments не дублировать forecast.
4. Рассчитать projected shortage до прихода заказа и raw order-up-to need. Одна поздняя поставка может уменьшить позднюю потребность, но не скрывает раннюю urgency.
5. UOM conversion → MOQ/multiple → rounded candidate. Missing constraint блокирует line. Need0 → quantity0. Budget allocation только complete currency/cost cohort; разрешённые партии и cash cap никогда не нарушаются.
6. Explanation ledger: forecasting components + SS - usable stock - eligible pipeline + clipping/MOQ/pack/budget/manual deltas. Конечная сумма равна selected base quantity.

DoD/tests: golden108; invalid/zero MOQ; need0; decimal metre-to-coil conversion; candidate≤0; late pipeline warning; budget below min batch returns deferred/infeasible; all ledger deltas reconcile; growth/lost не удваиваются. Не нужна общая MILP-библиотека для P0.

## A03. Живой run/API/storage, первый путь до T+60

1. SQLite tables/репозитории: snapshots, jobs, runs, proposals, proposal_versions, approvals, exports. Локальный single coordinator writes; worker возвращает артефакты/results.
2. Register config sources, snapshot job, planning-run job. Реальный вызов `B.build_forecast`, затем `planning`. Публикация proposals только после полного validated result.
3. Implement `contracts.md` endpoints health/jobs/runs/proposals/detail; filters/pagination, stages, structured errors.
4. Run metadata включает mode/as_of/seed/policy/snapshot/model hashes, capabilities/reasons. Real blocked lines в excluded coverage. Duplicate idempotency key + same payload возвращает тот же run; different payload →409.
5. UI получает стабильный live endpoint до готовности всех матмодулей. При restart queued/running job не должен висеть вечно: mark failed/retryable и разрешить retry, атомарность результатов сохранить.

DoD: C запускает один actual fixture run через API, видит предложение и explanation. Никакого скрытого возврата canned JSON в production handler. Допустим fake forecast provider в unit tests, не в release execution.

## A04. Edit → approve → CSV, до T+105

1. PATCH по expected_version, допустимые purchase increments/constraints; новый version, draft. Original recommendation отдельно от buyer selected qty; reason обязателен.
2. Approval actor server-side. Exact version/hash; quality/capabilities и freshness в replay context. Atomic compare-and-set запрещает race двух edits/approve.
3. POST export проверяет approved exact version, сохраняет CSV/audit; repeated key не создаёт новое действие. CSV содержит mode/date/supplier/sku/uom/qty/reason/approval.
4. Demo CSV явно «ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ». Никаких отправок в ERP/email/vendor API. Demo role config — не SSO/security readiness.

DoD/tests: unapproved export403/422 по API policy; stale version409; edit invalidates; unsupported qty422; missing data blocked; approved export есть и содержит согласованные числа; retry idempotent. В тестах проверяется backend состояние, не frontend кнопка.

## A05. What-if, T+105–150

1. Scenario из immutable base_run; overrides CSL0.95/0.99 и LT delay0/7/14. Reuse forecast if horizon sufficient; иное даёт явную ошибку/новый forecast, не truncation.
2. Replan with same seed/snapshot и тот же sample path; compare line deltas/stockout timing/cash if known. Baseline versions/approvals не меняются.
3. Budget сценарий работает на fully priced synthetic cohort. Unknown prices/currencies → budget_available=false, UI получает reason.
4. No scenario approval/export route. При принятии параметров создаётся новый planning-run обычного workflow.

DoD/tests: unconstrained raw target nondecreasing95→99; final packs могут быть равны, особенно при rounding/caps; delayed receipts не делают их ранними; scenario не мутирует baseline; cost sum/cap корректны.

## A06. Сборка и release, T+150–205

- По мере готовности интегрировать небольшие commits B/C в main. Держать main запускаемым; не включать неготовые feature paths.
- Прогнать общую acceptance таблицу с независимыми fixtures, numerical testsB и buyer interactionsC.
- README: dependencies, local source-config, safe demo command, API/UI launch, tests, limitations, replay date. Пример будущих команд согласовать и реально проверить; не выдавать неподдерживаемые команды за готовые.
- Docker Compose если не отнимает время от runnable local path; одна проверенная portable последовательность обязательна, два launch способа не обязательны.
- CI на synthetic inputs: lint/import/schema/meaningful tests. Secrets/raw data не нужны. Dependency/install без сети на демонстрации заранее проверить.
- Feature freeze T+185 (5:20). Fresh-clone smoke или чистое окружение одного teammate. Release SHA и evidence T+205 (5:40), последние20мин только репетиция.

## Порядок checkpoints в main

1. `A01 foundation/contracts`: shared skeleton/types/fixtures; быстро согласовать с B/C.
2. `A02+A03 first vertical`: actual forecast→order→HTTP; интеграция C.
3. `A04 approval/export`: вся state machine одним reviewable change.
4. `A05 scenarios`: отдельная фича, не destabilize baseline.
5. `A06 integration/release`: только glue/tests/docs fixes.

B/C обновляют свои main после каждого общего checkpoint. Новые ветки/worktree/PR не создавать; не делать force-push. Конфликты shared files решает A после чтения обеих сторон.

## Передача результата

При передаче каждого commit: что работает, tests/команды и результат, data limitations, что требуется B/C, какая schema version. На checkpoint сообщать: commit SHA; runnable behavior; blockers с минимальным воспроизведением; next deliverable. Перед релизом пользователь должен увидеть живой walkthrough, а не список реализованных функций.

## Стартовый prompt для Codex A

```text
Ты исполнитель A этого репозитория. Реализуй docs/mvp/participant-a-platform-integration.md по docs/mvp/master-plan.md и contracts.md. Всего у команды было225мин с2:15 до6:00; уточни оставшееся время по текущему контексту, не перезапускай таймер. Начни с A01 и дай B/C shared types/samples, затем первый actual end-to-end run. Работай только в своих каталогах, не подменяй B forecasting и C UI. Работай прямо в main, без новых веток/worktree/PR. Делай небольшие проверенные commits/checkpoints; пользователь принимает scope, ты владеешь технической интеграцией. Реальный ERP/vendor send не делать; synthetic маркировать; не коммитить supplier raw data/секреты. Тестируй meaningful invariants и сообщай готовый runnable результат. Если первое живое соединение не работает, приоритет integration bug, а не новая фича. Внешнюю коммуникацию участникам выполняй только при отдельном поручении; готовь ссылки и handoff заметки в repo.
```
