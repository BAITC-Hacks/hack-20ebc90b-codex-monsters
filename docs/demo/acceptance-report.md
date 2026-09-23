# Приёмка части C

## Актуальное дополнение: упрощение UI, 23.09.2026

На базе актуального main `2b7dce6`, включающего модуль B, проверен новый путь «Заказы / Что, если… / Данные». **235 passed, 45 subtests passed**, 17.51 s; Ruff apps/buyer_ui и tests/ui — PASS. Настоящий HTTP-проход включает создание данных и расчёта, правку, совмещённое утверждение с подготовкой CSV, сравнение и восстановление состояния. Подробности, критерии и ручная проверка — [упрощение интерфейса](ui-simplification.md).

Дальнейшие разделы — исторический протокол предыдущей версии. Его замечания о неподключённом B относятся к той базе и сохранённым fixture-v1 runs, а не к новым расчётам обновлённого main.

## Историческая проверка первоначального MVP

Проверенная реализация UI: `c0007e38253b102d8432bb02d4195aca44079292`. Последняя проверенная база A: `2c72a42706d3a6e399a367e9ea3da4054bd06ae1`; объединённый код `9bcbf7b7a4215e7aa7a182415422b4ab4125e8e3`. Работа продолжается в main по актуальному AGENTS.md. C меняет только свои UI/tests/demo каталоги.

## Выполненные проверки

Python **3.12.14**, Streamlit **1.64.0**, 23 сентября 2026 года:

```bash
/tmp/codex-ui-mvp-venv/bin/python -m ruff check apps/buyer_ui tests/ui
/tmp/codex-ui-mvp-venv/bin/python -m pytest -q
```

Ruff: **All checks passed**, включая src/apps/tests/scripts после получения новой main. Общий прогон: **123 passed, 30 subtests passed, 12.05 s**, без пропусков. Вывод: [full-test-results.txt](full-test-results.txt). Одно предупреждение — deprecation httpx в FastAPI TestClient. До интеграции отдельно прошли 53 проверки C: [исходный протокол](ui-test-results.txt).

| Проверка | Уровень | Статус | Доказательство |
|---|---|---|---|
| Routes, decimal-строки, 403/409/422, timeout/500, malformed JSON, точные CSV bytes | HTTP STUB | PASS | 18 test_http_client, отсутствие fallback и автоматических повторов mutations |
| Golden108, ручная дельта, nullable money, изоляция сценария, invalid/stale export | MOCK | PASS | 14 test_mock_workflow; mock не назван вычислительным backend |
| Три вкладки, стабильный line_id, черновики, версии и request key | UI | PASS | 9 AppTest заказов и 13 AppTest данных/сценариев |
| Токен нельзя отправить на изменённый пользователем адрес | CONFIG | PASS | 2 CredentialScopeTests: чужой URL отклонён до создания клиента |
| Snapshot → run → proposal → edit → approval → CSV | LIVE | PASS | test_live_backend запускает настоящий uvicorn и HttpClient |
| Draft/invalid/stale операции отклоняются backend | LIVE | PASS | Невалидное количество, старые version/hash, export до approval и после новой правки |
| Approval и идентичный CSV переживают перезапуск backend | LIVE | PASS | Новый клиент и процесс с тем же temp data_dir возвращают сохранённую версию и CSV bytes |
| Пустая UI-сессия создаёт snapshot/run; новая сессия восстанавливает approval | LIVE UI | PASS | AppTest с настоящим HTTP, без MockClient и подмены forecast_provider |
| Живой сценарий пересчитывает policy и сохраняет базовые предложения | LIVE | PASS | Суммы/количества на экране сверены с payload; forecast assumptions видимы |
| Встроенное скачивание таблицы отсутствует с первого отображения | BROWSER/UI | PASS | Новый сервер 8503: 0 Download as CSV; approved CSV доступен после workflow. Bootstrap-rerun проверен AppTest |

Три LIVE-теста используют временные каталоги и завершают свои процессы. Остальные 64 проверки общего прогона принадлежат backend A (contracts/planning/storage/API/fixtures).

## Проверка в браузере

Backend `127.0.0.1:18000`, UI `127.0.0.1:8503`. Snapshot `demo-20260923-v1`, seed 42, as_of `2026-09-23T00:00:00Z`, run `run-b29d6c2644ee4e3ba31bdc11e68ef45f`. IEK-DEMO и SYSTEME-DEMO: 15 пригодных строк, DEMO-016 явно исключён из-за неизвестных условий.

В proposal `53b81303-a126-594c-b17b-6e2f49bf7d86` строка DEMO-009 изменена 270 → 275 pcs с причиной «Проверка живого API на синтетических данных». Backend вернул draft v2 с ручной дельтой +5. Затем UI утвердил v2, получил серверный CSV и показал кнопку скачивания. Данные и учётная запись демонстрационные; поставщикам ничего не отправлялось.

## Оставшиеся границы

- Backend A использует `fixture-v1`: ingestion/forecast, классификация проектов и восстановление спроса B пока не подключены. Предупреждение `FORECAST_PROVIDER_NOT_CONNECTED` видно. Demand-events LIVE возвращает пустой список с model_version; UI не создаёт фиктивные события. Таблица классификации проверена на synthetic mock.
- AT-02…AT-06 и чувствительность к реальным данным не приняты этим отчётом. Fallback не доказывает качество модели, достигнутый сервис или экономию.
- SS/ROP отсутствуют в scenario payload: UI отмечает отсутствие SS и показывает возвращённые количества/стоимость. A может добавить nullable поля.
- Проверен Python 3.12.14; Python 3.11 отдельно не запускался. Общий uv.lock уже содержит Streamlit 1.64.0; файлы зависимостей A не менялись.
- Полный релиз A/B/C требует интеграции B и общей приёмки. C имеет проверенный живой buyer workflow и показывает оставшиеся ограничения.
