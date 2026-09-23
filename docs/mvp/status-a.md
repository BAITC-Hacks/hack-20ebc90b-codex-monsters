# Статус A: backend и интеграция

Область A реализуется непосредственно в `main` по указанию пользователя. Этот документ описывает имеющиеся модули и подключение B/C; окончательную готовность общего продукта подтверждает совместная приёмка на release commit.

## Готовые интерфейсы и модули

| Компонент | Реализация / назначение |
|---|---|
| Контракты | `src/ekt/contracts`: Pydantic v2, Decimal как JSON strings, даты с timezone, provenance, capabilities, quantity/XAI/cost invariants |
| Parquet IO | `ekt.contracts.io.write_table` и `load_table`; ссылки, checksum и проверка количества строк |
| Planner | `ekt.planning.build_proposals(snapshot, forecast, policy, run_id)` → supplier proposals; alias `plan` |
| Ограничения количества | `ekt.planning.revalidate_line_quantity(line, purchase_qty)` → количество в базовой единице |
| Operational state | SQLite: jobs, snapshots, forecasts, proposal versions, audit/approval/export, idempotency |
| API | `ekt.api.app:app`; health, sources, snapshots, runs, proposals, edit, approve, CSV, scenarios, demand-events |
| Исполнение | Один локальный worker, сохраняемый lifecycle задач; ошибки показываются явно |
| Synthetic fixture | `ekt.demo.create_demo_snapshot`; фиксированный `as_of=2026-09-23T00:00:00Z`, 16 SKU, два поставщика |

Planner учитывает горизонты L и L+R, суммарные scenario paths либо явное IID-normal допущение; free stock уже очищен от резервов и blocked stock. Поставки учитываются по консервативному `eta_end`; поздняя поставка не скрывает ранний дефицит. Неизвестные MOQ/UOM/lead time блокируют затронутую строку и остаются видны в excluded lines. Явный MOQ=0 означает отсутствие минимума, `null` — неизвестное условие.

Округление соблюдает покупную упаковку и точность базовой единицы; нулевая потребность остаётся нулевой. Бюджет общий для всех поставщиков, доступен только при полном сопоставимом покрытии costs/currency. Алгоритм допустимый и детерминированный, без заявления оптимальности. XAI ledger сходится к каждой выбранной единице.

Edit создаёт новую draft-версию; approval связан с точными version/hash; экспорт — только CSV после утверждения. What-if использует тот же snapshot/forecast и не изменяет базовое предложение.

## Что требуется от B

Экспортировать из пакетов две функции с общими runtime-типами:

```python
# ekt.data
build_snapshot(source_manifest: SourceManifest, mapping_config: MappingConfig) -> SnapshotManifest

# ekt.forecast
build_forecast(snapshot: SnapshotManifest, request: ForecastRequest) -> ForecastArtifact
```

- Пакеты и имена функций сохранить точно: backend подключает их по этим imports.
- Канонические таблицы писать через общие модели/Parquet references; `MappingConfig.options["output_root"]` содержит каталог выходных артефактов.
- Forecast выдать ежедневно от `as_of + 1` до `as_of + 90`, без пропусков и дублирования. `mean = baseline_mean + seasonal_delta + growth_delta`.
- Не добавлять lost demand в финальный заказ повторно: он уже включён в baseline прогноза.
- Для uncertainty: `scenario_paths` с матрицей `scenario_id,date,quantity` либо `iid_residual_normal` с `daily_residual_std` и честным assumption note.
- `classifications_ref` нужен для `demand-events`; `corrected_demand_ref` сохраняет происхождение восстановления.
- Source paths регистрируются локально; публичный HTTP не принимает произвольный путь пользователя.

**До подключения B:** только синтетический run может использовать `fixture-v1`. Это временный вход для проверки planner/API; он не является готовой ML-моделью, detector или восстановлением потерянного спроса. Real preview без B выдаёт ошибку подключения, а не скрытую замену синтетикой.

## Что требуется от C

Запустить backend по корневому README и использовать `http://127.0.0.1:8000`:

- `/docs`, `/openapi.json` — точные схемы и методы.
- Все бизнес-маршруты начинаются `/v1/`.
- Snapshot/run/scenario возвращают `202` и `status_url`; читать job до terminal state.
- Proposals получать через API; не читать SQLite и не считать MOQ/SS/ROP в UI.
- Показывать `mode`, replay `as_of`, warnings, excluded lines и `fixture-v1`, если используется fallback.
- Edit отправляет покупное количество, reason и `expected_version`; approve — version/hash; CSV export — версия/idempotency key.
- Ошибки `409`, `422`, `403`, pending и недоступность API не превращать в локальный success.
- Полноценная готовность Streamlit и demo-прохода определяется работой C; этот статус её не заявляет.

## Проверка и границы релиза

Численные проверки planner находятся в `tests/planning`, контрактные — в `tests/contracts`, storage — в `tests/storage`, API — в `tests/api`. Команды приведены в корневом README. **Проверенный checkpoint A:** commit `d619ac2` опубликован в `main`; полный suite на этом этапе — **64 passed**, Ruff — без ошибок (результат подтверждён интегратором A). Это проверка текущего backend, не свидетельство готовности модулей B/C. Финальные результаты общей приёмки и release SHA фиксируются после их подключения.

На checkpoint `49db849` повторно прошли **64 теста**, Ruff и `scripts/smoke_api.py` через запущенный uvicorn. HTTP-проход проверил два предложения поставщикам, запрет экспорта черновика, правку с версией 2, approval/CSV и изолированный сценарий 95% → 99%. Синтетический baseline: 2 483 125 KZT; сценарий: 2 656 250 KZT. Это демонстрационные числа, не финансовый эффект компании.

CI настроен в `.github/workflows/checks.yml`. [Запуск GitHub Actions](https://github.com/BAITC-Hacks/hack-20ebc90b-codex-monsters/actions/runs/35844817924) не начал ни одного шага: аннотация GitHub — `The job was not started because your account is locked due to a billing issue.` Проверка на Linux runner пока не подтверждена; локальные проверки выше прошли. Для возобновления облачного CI владелец аккаунта должен решить вопрос биллинга.

Остаются совместные этапы: подключить B, подключить UI C, пройти live snapshot → run → explanation → scenario → edit → approve → CSV, зафиксировать ограничения данных и провести репетицию.

В MVP нет реальной передачи в ERP/поставщикам, SSO, нескольких workers, распределённого исполнения или доказанного достигнутого CSL. Demo identity задаётся серверным окружением. Целевой сервис и синтетические финансовые расчёты не выдаются за измеренный бизнес-эффект.
