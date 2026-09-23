# Git, разделение файлов и handoff

Репозиторий: https://github.com/BAITC-Hacks/hack-20ebc90b-codex-monsters. На машине пользователя проверены gh authentication и WRITE-доступ; секреты для этого подключения не требуются. На каждом другом компьютере участник сам авторизуется своим аккаунтом и проверяет доступ. Пароли/токены не пересылать в чат или репозиторий.

## Старт

До merge планирующего PR общая база — `origin/codex/mvp-plan`. После merge — `origin/main`. Это планирующая ветка, приложение в ней ещё не реализовано. У каждого отдельный clone/worktree и своя feature branch, не общий открытый checkout.

Пример первого старта B, если ветка ещё не создана:

```bash
git fetch origin
git switch -c codex/data-forecast origin/codex/mvp-plan
```

Для A имя `codex/platform-integration`, для C `codex/buyer-ui`. После включения плана в main новые ветки начинать от `origin/main`. Перед переключением проверить `git status`, не терять незакоммиченные изменения. Не использовать reset --hard или force-push для синхронизации чужой работы.

## Владение

| Owner | Изменяет |
|---|---|
| A | contracts, planning, api, storage, shared fixtures, dependencies/lock, root configs, CI/Docker, README, integration tests |
| B | data, forecast, собственные tests, два data scripts |
| C | buyer_ui, UI tests, demo docs/assets |

Граница фиксируется в master-plan.md. Если изменение затрагивает чужую область, автор описывает требуемый interface/change в своём PR и связывает dependency; владельцу не приходится распутывать неожиданный rewrite. Изменения shared contract принимает A, сразу обновляет sample и уведомляет B/C согласованным командным каналом.

## Маленькие PR и интеграция

1. Первый PR A — skeleton/contracts/samples; B/C могут до него готовить свои модули по contracts.md.
2. B/C отправляют маленькие coherent PR на завершённых checkpoints, target `main`; в описании owner, задачаID, вход/выход, проверки, ограничения, зависимые PR.
3. A проверяет boundary tests и объединяет готовые PR последовательно. После каждого merge B/C подтягивают новую main в свои ветки. Если PR foundation ещё не слит, зависимость явно указана.
4. Никакого `git add .` без просмотра diff: stage только свою область. Raw Excel, DB, exports, secrets и large generated artifacts не включать.
5. Каждый PR сохраняет runnable совместимость; public types/routes не переименовывать без одновременной правки consumer.
6. Integration владелец A. Каждый сам запускает tests своего блока; C дополнительно проверяет реальную UI-взаимосвязь, B — числовую корректность.
7. Freeze5:20: новые features прекращаются. Release5:40: фиксируем commit SHA и demo environment. Main защищаем от непроверенных late rewrites.

Форма handoff для автора:

```text
Task: B03
Commit/PR: <ссылка>
Работает: <проверяемое действие>
Контракт: 1.0, без breaking changes / перечисление
Проверка: <команда + результат>
Ограничения: <конкретные данные или случаи>
Нужно от A/C: <точный interface или ничего>
Следующий checkpoint: <время/результат>
```

## Что передать людям

УчастникуB — ссылка на participant-b-data-forecast.md и contracts.md; участникуC — participant-c-ui-demo.md и contracts.md. В последних разделах уже есть копируемые prompts. Участники называют своим Codex роль явно. GitHub usernames нужны только для issue/PR assignees; работа по ролям A/B/C от них не зависит.

GitHub Issues не созданы и участникам не отправлены сообщения в рамках подготовки плана. Три role-документа уже являются готовыми заданиями. Если создавать issues позже, использовать тело соответствующего задания/ссылку и не копировать коммерческие данные.
