# Current execution instructions

- Work directly on `main`, per the user's explicit instruction. Do not create branches or worktrees. Do not force-push or discard other contributors' work.
- Read `docs/mvp/README.md` and your assigned A/B/C plan. Older branch/PR instructions are superseded by this file and the user's request.
- A owns contracts, planning, API, storage, shared fixtures, dependencies/lock, integration, CI and root docs. B owns data/forecast and their tests. C owns buyer UI and its tests/demo assets.
- Keep changes scoped. Stage only the files you own. Before pushing, fetch and integrate newer main commits without overwriting colleagues' files. If a shared contract needs adjustment, coordinate it with A and preserve compatible consumer payloads.
- Synthetic and assumed data stay visibly labelled. Never commit corporate raw files, local databases or secrets. Do not send orders to real suppliers or ERP.
- Run meaningful affected tests before publishing. Forecast accuracy or business benefit must not be claimed from synthetic data as observed company results.
