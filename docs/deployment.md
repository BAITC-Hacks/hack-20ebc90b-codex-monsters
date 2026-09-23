# Локальный запуск и live demo

Один Python 3.12 runtime запускает FastAPI и Streamlit. UI вызывает API через
loopback; публичным остаётся только порт Streamlit. Launcher ждёт готовности
обоих процессов и завершает всё приложение, если один сервис упал. Один worker
API и одна реплика обязательны: очередь заданий находится в памяти процесса,
а состояние — в SQLite. Контракты A/B/C не меняются.

## Облачный экземпляр Railway

- Адрес: <https://buyer-app-production-6621.up.railway.app>.
- [Проект Railway](https://railway.com/project/b51995b8-f7cb-4dfd-b9b4-18b678608b9f).
- Service `buyer-app`, production; одна реплика, Dockerfile, healthcheck
  `/_stcore/health`, публичный порт 8501. API остаётся на loopback.
- Volume `buyer-app-volume` смонтирован в `/app/var`: SQLite и Parquet хранятся
  вместе. Для прав на Railway volume установлен `RAILWAY_RUN_UID=0`;
  локальный Docker по-прежнему запускается от UID 10001.
- Исходный runtime commit: `19596be559458e2a3c41709fb2511d8de5d7e3a7`.
  Загружен отдельный набор из 46 файлов кода и synthetic fixtures (~667 kB),
  без корпоративных ZIP, `.git`, `.env`, БД и локальных экспортов.

Проверено 23.09.2026: deployment `7a203141-8252-41a0-8e2e-68881fc13653`
получил `SUCCESS`, публичный health вернул HTTP 200. В браузере через HTTPS и
WebSocket создан снимок и рассчитаны два заказа. В облачном контейнере прошли
`scripts/check_health.py` и `scripts/smoke_api.py`: настоящий прогноз B,
запрет CSV черновика, правка, утверждение версии 2, демонстрационный CSV,
what-if 95% → 99% без изменения базового заказа. Smoke run:
`run-7eefc0c9755f4626b7d5c1cee5550dfd`, утверждённое предложение
`db992271-3f58-5b00-be70-a51e751b7eba`. Проверено наличие SQLite на `/app/var`,
отсутствие `EKT_SOURCE_CONFIG` и отключение внешней отправки.

Публикация выполняется CLI из подготовленного каталога. Автодеплой по push
из GitHub пока не подключён. GitHub Actions организации не стартует из-за
billing lock: [проверенный run](https://github.com/BAITC-Hacks/hack-20ebc90b-codex-monsters/actions/runs/35858767140).
Облачная сборка Railway от Actions не зависит.

На момент создания 23.09.2026 аккаунт Railway имел trial $5 / до 30 дней.
Платная подписка не подключалась. Доступность после расходования кредита или
окончания trial требует решения владельца аккаунта; это не бессрочный бесплатный
хостинг. Данные по-прежнему синтетические, общая demo identity и нет отправки
заказов поставщикам.

## Локально

```bash
uv sync --frozen --python 3.12
uv run python scripts/run_app.py
```

Открыть <http://localhost:8501>. Сначала выбрать «Данные», создать синтетический
снимок и рассчитать предложение, затем проверить «План закупки» и CSV.
Данные по умолчанию в `var/`; `EKT_DATA_DIR` меняет каталог. Команда запускается
из любого cwd и использует корень репозитория. `Ctrl+C` останавливает оба сервиса.
Переменные из `.env.example` нужно экспортировать в окружение; launcher не читает
`.env` автоматически. Альтернатива без настройки Python: `docker compose up --build -d`.

Локальная HTTP-проверка всей цепочки:

```bash
uv run python scripts/smoke_api.py
```

В Docker API остаётся приватным, поэтому выполнить ту же проверку внутри:

```bash
docker compose exec -T app python scripts/smoke_api.py
docker compose exec -T app python scripts/check_health.py
docker compose logs --tail=100 app
```

Проверяются расчёт прогнозом B, запрет CSV до утверждения, ручная правка,
утверждение версии, демонстрационный CSV, what-if и неизменность базового заказа.

## Быстрая HTTPS-ссылка через Cloudflare

```bash
docker compose --profile share up --build -d
docker compose logs tunnel
```

Скопировать `https://….trycloudflare.com` из логов. Снаружи проверить создание
снимка/расчёта и скачивание CSV — это также проверит соединение WebSocket.
Нельзя закрывать Docker, выключать компьютер или давать ему уснуть во время демо.
Команда `docker compose --profile share stop tunnel` отключает ссылку, оставляя
локальное приложение. Полная остановка: `docker compose --profile share down`.
Не добавлять `-v`, если нужны сохранённые расчёты.

[Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/trycloudflare/)
не требуют аккаунта/домена и выдают временный адрес; у сервиса нет гарантии
доступности для production. Перезапуск tunnel может изменить ссылку.

## Повторная публикация в Railway

Установить официальный [Railway CLI](https://docs.railway.com/cli) и выполнить
`railway login`. Из корня репозитория подготовить отдельный каталог:

```bash
python3 scripts/prepare_deploy.py --output var/railway-upload --ref HEAD
```

Скрипт берёт файлы только из указанного commit, записывает SHA в `DEPLOY_COMMIT`
и исключает Git-историю, корпоративные архивы, `.env`, локальные БД и незакоммиченные
изменения. Непустой каталог не перезаписывается: для следующей публикации указать
новый output. Проверить напечатанный SHA и состав каталога перед отправкой.

Для уже созданного экземпляра:

```bash
cd var/railway-upload
railway up --project b51995b8-f7cb-4dfd-b9b4-18b678608b9f \
  --service buyer-app --environment production --detach
railway deployment list --project b51995b8-f7cb-4dfd-b9b4-18b678608b9f \
  --service buyer-app --environment production --limit 1
```

`--detach` подтверждает отправку, а не готовность приложения. Дождаться `SUCCESS`,
затем открыть публичную ссылку и выполнить расчёт. Для проверки health:

```bash
curl --fail https://buyer-app-production-6621.up.railway.app/_stcore/health
```

Запускать `railway up` **только из подготовленного каталога**: `.dockerignore`
ограничивает Docker context, но не исходную загрузку CLI. Прямое подключение
текущего GitHub-репозитория передаст хостингу и уже отслеживаемые корпоративные ZIP;
для текущего демо выбран экспорт по allowlist.

## Настройка нового экземпляра Railway

1. Создать пустой проект и сервис. Публиковать подготовленный каталог командой
   выше, заменив project/service на свои. Корневой Dockerfile определяется
   автоматически; tunnel-сервис не нужен, HTTPS предоставляет Railway.
2. Убедиться, что установлены `PORT=8501`, `EKT_DATA_DIR=/app/var`,
   `BUYER_LOCK_CONNECTION=true`. Start command уже задан в Dockerfile.
   `BUYER_UI_MODE=http` и loopback API URL выставляет launcher.
3. Добавить volume с mount path `/app/var`, чтобы SQLite, снимки и утверждения
   сохранялись при redeploy. Установить **1 replica**. Для прав на Railway volume
   установить `RAILWAY_RUN_UID=0`; без volume сборка работает от пользователя `ekt`.
4. В Networking → Generate Domain указать target port `8501`.
   Healthcheck path: `/_stcore/health`.
5. Дождаться успешного deployment и пройти сценарий через выданную ссылку.
   Работающий деплой подтверждается открывающимся UI и расчётом, а не только
   успешной сборкой образа.

Источники: [Dockerfiles](https://docs.railway.com/builds/dockerfiles),
[CLI deploy](https://docs.railway.com/cli/deploying),
[persistent volumes](https://docs.railway.com/volumes).
Аккаунт хостинга и его тариф выбирает владелец. Без volume состояние контейнера
считать временным. Реальную стоимость и доступный тариф смотреть перед созданием.

## VPS

На сервере с Docker склонировать репозиторий и выполнить `docker compose up --build -d`.
Для стабильного HTTPS разместить reverse proxy перед `127.0.0.1:8501`, включить
поддержку WebSocket и указать свой домен. Не открывать API-порт 8000 наружу.
Backup должен включать весь volume с SQLite и связанными Parquet-артефактами.

## Границы live demo

Сборка по allowlist исключает корпоративные ZIP-архивы, локальные БД, `.env`,
выгрузки и историю Git. Публичное демо использует только встроенную синтетику.
Все посетители используют общую демонстрационную identity и общее состояние;
SSO и разделение организаций пока не реализованы. CSV не отправляется поставщику.
Для публичного экземпляра не задавать `EKT_SOURCE_CONFIG` с реальными файлами.

`BUYER_LOCK_CONNECTION=true` фиксирует HTTP-режим и API URL на сервере; посетитель
не может изменить назначение запросов в настройках UI. Это не заменяет
аутентификацию. Локальный контейнер использует отдельный непривилегированный
UID 10001; исключение для Railway volume описано выше.

Пользовательская польза: команда запускает весь продукт одной командой и может
показать живой путь закупщика по ссылке. Усиливаются критерии «результат и качество»,
«развитие и масштабирование» и «презентация/демо». Численная точность и бизнес-эффект
этим изменением не измеряются.
