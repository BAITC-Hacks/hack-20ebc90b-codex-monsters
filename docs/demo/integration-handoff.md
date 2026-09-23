# Передача части C исполнителю A

Реализованы C-01, клиентская часть C-02, C-03 и локальные проверки C-04. Живой проход C-02/C-04 остаётся зависимостью backend. Ветка `codex/ui-mvp`, код `5fab3ef59c437f1e3777b0ca32d6ccf2ef95e6d5`. Контракты v1 не изменялись; изменения только в `apps/buyer_ui`, `tests/ui`, `assets/demo`, `docs/demo`.

Работает: три вкладки, supplier proposals/ledger, правка по line_id с reason и expected_version, сверка версии/hash перед approval/export, скачивание неизменённых CSV bytes из HTTP, сценарии и явный snapshot/job/run workflow. HTTP не переходит на mock. Повторы export/scenario/planning сохраняют request key; неоднозначный PATCH требует сверки, а повтор POST snapshot без согласованной idempotency блокируется.

Проверка: 53 теста PASS на Python 3.12.14 + Streamlit 1.64.0, включая локальный HTTP stub и AppTest. [Полный отчёт](acceptance-report.md), [команды запуска](runbook.md). Mock использует подготовленные ответы и не заменяет расчёт, хранение или авторизацию A/B.

## Что требуется для интеграции

1. Добавить Streamlit 1.64.0 в общие зависимости/lock и проверить Python 3.11. `client.disableDataExport` используется для отключения скачивания произвольной таблицы без approval; общий dependency-файл принадлежит A.
2. Предоставить URL API, synthetic snapshot/run IDs, seed и as_of. Настройки UI: `BUYER_UI_MODE=http`, `BUYER_API_URL`, `BUYER_SNAPSHOT_ID`, `BUYER_RUN_ID`, `BUYER_SEED`. Необязательный `BUYER_API_TOKEN` передаётся только на адрес `BUYER_API_URL`.
3. Сверить реальные JSON samples: envelope sources и demand-events, ProposalSummary, completed run, scenario summary/changed_lines. Эти части prose-контракта не полностью материализованы. UI принимает коллекции `items`, а для sources/demand-events также `sources`/`events`; неизвестные дополнительные поля сводки показывает как метаданные.
4. Подтвердить seed завершённого run. UI использует возвращённый `seed`, иначе явно помеченный `BUYER_SEED`; текущий POST planning-runs не содержит поля seed. Для строгого сравнения серверу нужно подтвердить одинаковый seed базы и сценария. Snapshot/scope сверяются через proposal details.
5. Выполнить LIVE-проверку edit → draft новой версии → approve → reload новой сессии → CSV, отрицательные 403/409/422 и timeout. Затем сценарий 99%/+7 дней на той же базе. Сервер должен выполнять расчёты и сохранять audit; это нельзя подтвердить локальным mock.

Reset UI в mock очищает только текущую имитацию в памяти. Полный reset backend должен быть предоставлен A и ограничен demo-состоянием. Команды запуска backend в документации C не выдуманы.
