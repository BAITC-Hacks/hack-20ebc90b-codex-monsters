# Передача части C

UI `c0007e38253b102d8432bb02d4195aca44079292` проверен с последней базой A `2c72a42`; объединённый код `9bcbf7b`. Работа перенесена в main по новым AGENTS.md. Общие контракты, зависимости и файлы A/B не правились частью C.

Сданы: три вкладки, supplier proposals/ledger, редактирование line_id с reason/expected_version, свежая проверка version/hash перед approval/export, серверный CSV, реальные сценарные ответы и snapshot/job/run workflow. HTTP не переключается на mock; неоднозначные запросы не объявляются успешными.

Проверено: **123 теста** общего проекта, включая **3 настоящих HTTP/Streamlit integration tests**. Approval и идентичный CSV сохраняются после нового клиента, новой UI-сессии и перезапуска backend. [Отчёт](acceptance-report.md), [команды запуска](runbook.md).

UI согласован с фактическим payload A: `baseline_total_cost`, `baseline_base_qty/scenario_base_qty/delta_base_qty`, ключ SKU/supplier/warehouse вместо отсутствующего line_id. Единицы берутся из соответствующего proposal; неопределённые SS/ROP не подставляются. База сценария — исходный run/v1, ручные правки не объявляются базой what-if.

## Оставшиеся зависимости

1. B: подключить ingestion, forecast и demand-events. Server fallback `fixture-v1` помечен, LIVE project table пустая. PASS mock-классификации не подтверждает реальный detector.
2. A уже добавил seed/policy/as_of в run response. UI использует их, если они возвращены; для ранее сохранённых runs с null сохраняется явно помеченный fallback request seed. Snapshot дополнительно сверяется через proposals.
3. A: при необходимости добавить SS/ROP в scenario changed_lines. Сейчас показаны только возвращённые количества и стоимость.
4. Команда: проверить Python 3.11 и frozen-lock запуск на другом checkout. Тесты выполнены на Python 3.12.14; uv.lock содержит Streamlit 1.64.0 для отключения встроенного экспорта таблиц.

Mock reset очищает только имитацию текущей сессии. Для нового LIVE-демо можно остановить собственный backend и запустить его с новым выделенным EKT_DATA_DIR; UI не удаляет базы или файлы.
