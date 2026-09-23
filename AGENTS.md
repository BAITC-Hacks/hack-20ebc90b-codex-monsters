# Current execution instructions

- Work directly on `main`, per the user's explicit instruction. Do not create branches or worktrees. Do not force-push or discard other contributors' work.
- Read `docs/mvp/README.md` and your assigned A/B/C plan. Older branch/PR instructions are superseded by this file and the user's request.
- A owns contracts, planning, API, storage, shared fixtures, dependencies/lock, integration, CI and root docs. B owns data/forecast and their tests. C owns buyer UI and its tests/demo assets.
- Keep changes scoped. Stage only the files you own. Before pushing, fetch and integrate newer main commits without overwriting colleagues' files. If a shared contract needs adjustment, coordinate it with A and preserve compatible consumer payloads.
- Synthetic and assumed data stay visibly labelled. Never commit corporate raw files, local databases or secrets. Do not send orders to real suppliers or ERP.
- Run meaningful affected tests before publishing. Forecast accuracy or business benefit must not be claimed from synthetic data as observed company results.

## Общие критерии хакатона и отчёт об изменениях

- Перед планированием и после получения обновлений прочитай `docs/mvp/hackathon-criteria.md`. Все роли A/B/C учитывают одни критерии: ценность 25, результат и качество 20, инновационность 15, развитие и масштабирование 20, презентация/демо/ответы 20; всего 100.
- Для существенного изменения назови пользовательскую пользу, затронутые критерии и проверяемое доказательство. Проверяй целостный пользовательский сценарий; число тестов не заменяет демонстрацию результата. Отделяй работающие возможности, синтетические примеры и измеренные результаты от гипотез и дорожной карты.
- Единая информация хранится в этом репозитории и `origin/main`, с отдельным clone у каждого участника. Следуй `docs/mvp/git-workflow.md`, сохраняй чужие и незакоммиченные изменения. После синхронизации перечитывай обновлённые общие инструкции и свой план.
- После каждого завершённого пакета изменений **всегда сообщай пользователю**: что изменено, зачем/какие критерии усилены, что проверено и с каким результатом, commit SHA/ссылка и статус публикации, что требуется коллегам A/B/C. Не объявляй результат опубликованным до успешного push/проверенного обновления main.
- Существенные изменения запуска, контрактов, поведения и ограничений также отражай в handoff своей роли или профильной документации. Не отправляй сообщения людям во внешних сервисах без отдельного поручения.
