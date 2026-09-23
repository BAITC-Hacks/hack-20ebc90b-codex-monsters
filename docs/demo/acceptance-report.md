# Приёмка части C

Область: Streamlit UI, HTTP-клиент, синтетический mock и документация. Контрольная исходная ревизия планов: `3f0f23b530c0ca3ab279fdfb39cebd69f51bcc1f`. Проверенная реализация части C: commit `5fab3ef59c437f1e3777b0ca32d6ccf2ef95e6d5`, ветка `codex/ui-mvp`. Проверены только принадлежащие C каталоги.

`MOCK` проверяет клиентское поведение на подготовленных синтетических ответах. `HTTP STUB` проверяет маршруты, JSON, ошибки и bytes на локальном сервере стандартной библиотеки. `LIVE` требует backend A/B с расчётами, хранением и enforcement. Эти уровни не взаимозаменяемы.

## Автоматические проверки

Фактически выполненная команда в изолированном тестовом окружении:

```bash
/tmp/codex-ui-mvp-venv/bin/python -m unittest discover -s tests/ui -v
```

Окружение: Python **3.12.14**, Streamlit **1.64.0**. Финальный запуск 2026-09-23: **53 теста, 2.951 с, OK**, без пропусков. Полный вывод сохранён в [ui-test-results.txt](ui-test-results.txt). Предупреждение Streamlit `missing ScriptRunContext` относится к запуску AppTest в тестовом процессе; исключений приложения нет. Для собственного `.venv` команда из [runbook](runbook.md) эквивалентна. Целевой Python 3.11 и общий lock-файл должны быть проверены при интеграции A.

| Проверка | Уровень | Статус | Доказательство |
|---|---|---|---|
| Base URL `/v1`, безопасные ID/query, decimal-строки и точные тела изменяющих запросов | HTTP STUB | PASS | 18 тестов [test_http_client.py](../../tests/ui/test_http_client.py), включая transport errors и export ниже |
| 403/409/422, timeout, неоднозначный 500, некорректный JSON/NaN, запрет redirects и отсутствие fallback | HTTP STUB | PASS | Ошибки сохраняют код; запросы не повторяются автоматически; невалидное число не отправляется |
| CSV приходит как bytes ответа; повтор использует прежний idempotency key; JSON не выдаётся за CSV | HTTP STUB | PASS | Проверены bytes, filename и точное тело обоих запросов export |
| Draft нельзя экспортировать; approval конкретной версии; изменение снимает approval | MOCK | PASS | 14 тестов [test_mock_workflow.py](../../tests/ui/test_mock_workflow.py); старый export отвергается после изменения |
| Ledger golden108 и явная ручная дельта; выбор строки по ID после сортировки/фильтра | MOCK/UI | PASS | Ledger сходится по Decimal; recommendation сохранена; PATCH адресует `line-tools` |
| Scenario не меняет baseline; неподдержанный budget отклонён | MOCK | PASS | Сверка proposal до/после, повтор idempotency key возвращает тот же job |
| Три вкладки, маркировка, edit → approve → refresh → CSV → edit | UI | PASS | 9 тестов [test_orders_app.py](../../tests/ui/test_orders_app.py); новая версия удаляет старую download-кнопку/bytes и approval |
| 403/409/422 сохраняют пользовательский ввод; timeout export сохраняет ключ попытки | UI | PASS | Инъекция ошибок в AppTest; утверждённый статус не подставляется локально |
| Встроенное скачивание таблиц Streamlit не обходит approval | UI | PASS | `client.disableDataExport=True`; отдельный утверждённый CSV остаётся доступен только через workflow |
| Токен окружения привязан к настроенному адресу API | UI CONFIG | PASS | 2 теста CredentialScopeTests: чужой адрес отклонён до создания клиента; настроенный адрес получает token |
| Scenarios/import/jobs, retry, очистка устаревшего результата, pagination и выбор контекста | UI | PASS | 10 тестов [test_secondary_views.py](../../tests/ui/test_secondary_views.py); failed run не показывает старую классификацию |

Также выполнен визуальный просмотр локального Streamlit в браузере по адресу `127.0.0.1:8501` в synthetic mock-режиме. Это проверка отображения UI, а не живой API интеграции. AppTest refresh проверяет новый GET в том же mock-клиенте; сохранение после новой браузерной сессии и перезапуска сервера этим не доказано.

## Зависимости, которые нельзя принять без backend

В этом checkout нет реализации API, worker, persistence, ingestion или forecasting. Поэтому ниже стоит BLOCKED независимо от результата локальных тестов.

| Требование из общей приёмки | Статус | Причина и требуемая проверка |
|---|---|---|
| AT-01, AT-16: чистый полный запуск, детерминизм и reset всего MVP | BLOCKED | Нет backend A/B и общей поставки окружения; нужен запуск из чистого checkout. |
| AT-02…AT-11: ingestion, detector, lost demand, netting и расчёт политики | BLOCKED | Mock содержит иллюстративные ответы; нужны независимые тесты настоящих вычислений. |
| AT-12: ledger всех вычисленных строк после ручной правки | BLOCKED | Локальная сверка fixtures не доказывает backend ledger. |
| AT-13: стоимость, единицы и бюджет на реальных расчётах | BLOCKED | Нужен backend с полными сопоставимыми ценами и budget enforcement. |
| AT-14: серверные 403/409/422, audit, точная версия, persistence approval/export | BLOCKED | HTTP stub проверяет передачу ошибок, но не серверную бизнес-логику или хранение. |
| AT-15: полный путь через живой API | BLOCKED | Нужны доступный backend URL, согласованные responses и прохождение UI без mock. |

## Протокол будущего live-прохода

Зафиксировать backend/frontend commit SHA, версии Python/Streamlit, seed, snapshot/run IDs и `as_of`. Пройти [пятиминутный сценарий](five-minute-demo.md), отдельно проверить export до approval, stale PATCH/approval, missing role, invalid quantity, mutation timeout и reload после approval. Сохранить только синтетические артефакты: обезличенный CSV с watermark и безопасные скриншоты/логи без token. После исправления повторять затронутые проверки; не переносить PASS mock в LIVE.
