"""Scenario and data screens; all business results come from the API."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from uuid import uuid4

import streamlit as st

from .client import ApiError
from .formatting import format_decimal, format_money, show_api_error, show_issues, show_quality


def _fingerprint(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _request_key(operation: str, payload: dict[str, Any]) -> str:
    """Retain a key across reruns and ambiguous retries of the same request."""
    keys = st.session_state.setdefault("_secondary_request_keys", {})
    signature = f"{operation}:{_fingerprint(payload)}"
    if signature not in keys:
        keys[signature] = str(uuid4())
    return keys[signature]


def _clear_scenario() -> None:
    st.session_state.pop("_secondary_scenario", None)
    st.session_state.pop("_secondary_scenario_context", None)


def _select_snapshot(snapshot_id: str) -> None:
    if st.session_state.get("snapshot_id") != snapshot_id:
        st.session_state["snapshot_id"] = snapshot_id
        st.session_state["run_id"] = None
        st.session_state["proposal_id"] = None
        _clear_scenario()


def _select_run(run_id: str) -> None:
    if st.session_state.get("run_id") != run_id:
        st.session_state["run_id"] = run_id
        st.session_state["proposal_id"] = None
        _clear_scenario()


def _items(response: Any, alternate: str = "items") -> list[dict[str, Any]]:
    if isinstance(response, list):
        return [item for item in response if isinstance(item, dict)]
    if isinstance(response, dict):
        entries = response.get("items", response.get(alternate, []))
        return [item for item in entries if isinstance(item, dict)] if isinstance(entries, list) else []
    return []


def _show_mode(record: dict[str, Any]) -> None:
    if record.get("mode") == "synthetic_demo":
        st.warning("DEMO · синтетические данные. Результат предназначен для демонстрации.")
    elif record.get("mode") == "real_preview":
        st.info("Предпросмотр реальных данных; ограничения готовности указаны сервером.")
    if record.get("as_of"):
        st.caption(f"Данные на {record['as_of']}; контекст воспроизведения, а не текущие остатки.")


def _show_job(record: dict[str, Any], title: str) -> None:
    labels = {"queued": "в очереди", "running": "выполняется", "succeeded": "завершён", "failed": "ошибка"}
    status = record.get("status")
    st.write(f"**{title}: {labels.get(status, status or 'статус не передан')}**")
    details = {key: record[key] for key in ("id", "stage", "created_at", "updated_at") if record.get(key) is not None}
    if details:
        st.caption(" · ".join(f"{key}: {value}" for key, value in details.items()))
    progress = record.get("progress")
    if isinstance(progress, (float, int)) and 0 <= progress <= 1:
        st.progress(float(progress))
    if status == "failed":
        error = record.get("error")
        if isinstance(error, dict):
            st.error(f"{error.get('code', 'JOB_FAILED')}: {error.get('message', 'Сервер не вернул описание ошибки')}")
            if error.get("details"):
                st.json(error["details"])
        else:
            st.error("Задание завершилось ошибкой. Подробности не переданы сервером.")


def _snapshot_from_ref(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    value = value.strip()
    if "/snapshots/" in value:
        return value.split("/snapshots/", 1)[1].split("?", 1)[0].rstrip("/") or None
    # An opaque ID is permitted; other artifact references are not snapshot IDs.
    return value if "/" not in value and ":" not in value else None


def _positive_money(value: str) -> bool:
    try:
        parsed = Decimal(value)
        return parsed.is_finite() and parsed > 0
    except (InvalidOperation, ValueError):
        return False


def _base_proposals(client: Any, run: dict[str, Any], run_id: str) -> list[dict[str, Any]]:
    ids = run.get("proposal_ids")
    if not isinstance(ids, list):
        listing = client.list_proposals(run_id=run_id, limit=200)
        if isinstance(listing, dict) and listing.get("next_cursor"):
            st.warning("Показаны не все предложения базы. Бюджетное сравнение недоступно.")
            return []
        ids = [item["proposal_id"] for item in _items(listing) if item.get("proposal_id")]
    return [client.get_proposal(proposal_id) for proposal_id in ids]


def _budget_currency(proposals: list[dict[str, Any]]) -> str | None:
    """Gate financial controls using authoritative top-level capabilities."""
    if not proposals or any(not p.get("capabilities", {}).get("budget_available", False) for p in proposals):
        return None
    currencies = {p.get("currency") for p in proposals}
    if len(currencies) != 1 or not next(iter(currencies)):
        return None
    if any(p.get("total_cost") is None for p in proposals):
        return None
    return next(iter(currencies))


def _show_scenario_comparison(scenario: dict[str, Any], currency: str | None) -> None:
    """Render optional server comparison fields, without manufacturing deltas."""
    summary = scenario.get("summary")
    changed = scenario.get("changed_lines")
    count = summary.get("changed_line_count") if isinstance(summary, dict) else None
    if count is None and isinstance(changed, int):
        count = changed
    if count is not None:
        st.metric("Изменённых строк", str(count))
    else:
        st.caption("Количество изменённых строк не передано API.")
    if isinstance(summary, dict):
        rows = []
        for title, base_key, scenario_key in (
            ("Целевой cycle service", "base_service_target", "scenario_service_target"),
            ("Задержка поставщика, дней", "base_lead_time_delay_days", "scenario_lead_time_delay_days"),
        ):
            if base_key in summary or scenario_key in summary:
                rows.append({"Показатель": title, "База": format_decimal(summary.get(base_key)), "Сценарий": format_decimal(summary.get(scenario_key))})
        if "base_total_cost" in summary or "scenario_total_cost" in summary:
            cost_currency = currency if summary.get("currency") == currency else None
            rows.append({"Показатель": "Закупочная стоимость", "База": format_money(summary.get("base_total_cost"), cost_currency), "Сценарий": format_money(summary.get("scenario_total_cost"), cost_currency)})
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
    if isinstance(changed, list):
        lines = {line["line_id"]: line for line in changed if isinstance(line, dict) and line.get("line_id")}
        if lines:
            selected_id = st.selectbox("Строка сравнения", list(lines), format_func=lambda value: f"{lines[value].get('sku_id', value)} · {lines[value].get('name', value)}", key=f"secondary_scenario_line_{scenario.get('id', 'result')}")
            line = lines[selected_id]
            rows = []
            for title, base_key, scenario_key, uom in (
                ("Заказ", "base_purchase_qty", "scenario_purchase_qty", line.get("purchase_uom")),
                ("Страховой запас", "base_safety_stock", "scenario_safety_stock", line.get("base_uom")),
                ("Потребность до ограничений", "base_raw_need", "scenario_raw_need", line.get("base_uom")),
            ):
                if base_key in line or scenario_key in line:
                    rows.append({"Показатель": title, "База": format_decimal(line.get(base_key)), "Сценарий": format_decimal(line.get(scenario_key)), "Единица": uom or "Не передана"})
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
    if summary is None:
        st.info("Сервер не вернул сводку сравнения.")
    with st.expander("Полный результат сравнения из API"):
        st.json({"changed_lines": changed, "summary": summary})


def render_scenarios(client: Any) -> None:
    st.subheader("Сценарии")
    st.caption("Сценарий использует snapshot и scope завершённого расчёта. Базовые предложения сохраняются.")
    snapshot_id = st.session_state.get("snapshot_id")
    run_id = st.session_state.get("run_id")
    context = (st.session_state.get("client_mode"), snapshot_id, run_id, st.session_state.get("proposal_id"))
    if st.session_state.get("_secondary_scenario_context") != context:
        _clear_scenario()
        st.session_state["_secondary_scenario_context"] = context
    if not snapshot_id or not run_id:
        st.info("Выберите snapshot и завершите базовый расчёт во вкладке «Данные/проекты».")
        return
    try:
        base_run = client.get_planning_run(run_id)
        if base_run.get("status") != "succeeded":
            _clear_scenario()
            _show_job(base_run, "Базовый расчёт")
            st.info("Сравнение станет доступно после успешного завершения базового расчёта.")
            st.button("Обновить базовый расчёт", key="secondary_base_refresh")
            return
        proposals = _base_proposals(client, base_run, run_id)
    except ApiError as error:
        show_api_error(error)
        return
    if any(p.get("run_id") != run_id or p.get("snapshot_id") != snapshot_id for p in proposals):
        _clear_scenario()
        st.error("Базовые предложения относятся к другому snapshot или run. Выберите согласованный контекст.")
        return
    submitted_context = st.session_state.get("_secondary_run_contexts", {}).get(run_id, {})
    verified_snapshot = base_run.get("snapshot_id") or (proposals[0].get("snapshot_id") if proposals else submitted_context.get("snapshot_id"))
    if verified_snapshot != snapshot_id:
        _clear_scenario()
        st.error("API не подтвердил принадлежность завершённого базового расчёта выбранному snapshot.")
        return
    versions = tuple((p.get("proposal_id"), p.get("version"), p.get("content_hash")) for p in proposals)
    saved = st.session_state.get("_secondary_scenario")
    if saved and saved.get("base_versions") != versions:
        st.session_state.pop("_secondary_scenario", None)
        st.info("Версия базового предложения изменилась. Запустите новое сравнение.")
    seed = base_run.get("seed", submitted_context.get("seed", st.session_state.get("base_seed", 42)))
    saved = st.session_state.get("_secondary_scenario")
    if saved and saved.get("request", {}).get("seed") != seed:
        st.session_state.pop("_secondary_scenario", None)
        st.info("Seed базового расчёта изменился. Прежнее сравнение скрыто.")
    st.caption(f"Snapshot: {snapshot_id} · базовый run: {run_id} · seed: {seed}")
    if "seed" not in base_run:
        st.caption("Seed взят из конфигурации базового расчёта: API не возвращает его отдельным полем.")
    if proposals:
        _show_mode(proposals[0])
    currency = _budget_currency(proposals)
    policy = base_run.get("policy") or submitted_context.get("policy")
    with st.expander("Базовые параметры и ограничения"):
        if policy:
            st.json(policy)
        else:
            st.info("Параметры базовой политики не переданы API.")
        for proposal in proposals:
            st.write(f"Поставщик: {proposal.get('supplier_id', '—')} · склад: {proposal.get('warehouse_id', '—')}")
            st.write(f"Стоимость базы: {format_money(proposal.get('total_cost'), proposal.get('currency'))}")
            show_issues(proposal.get("warnings", []))
            for reason in proposal.get("capabilities", {}).get("reasons", []):
                st.caption(str(reason))
    with st.form("secondary_scenario_form"):
        target = st.selectbox("Цель: цикл без дефицита", [0.95, 0.99], index=1, format_func=lambda v: f"{v:.0%}", key="secondary_target")
        st.caption("Целевой cycle service; это не измеренный уровень сервиса и не fill rate.")
        delay = st.selectbox("Задержка поставщика, календарных дней", [0, 7, 14], key="secondary_delay")
        budget_enabled = st.checkbox("Ограничить бюджет", disabled=not currency, key="secondary_budget_enabled")
        budget = st.text_input("Бюджет (decimal-строка)", disabled=not currency, key="secondary_budget")
        if currency:
            st.caption(f"Валюта бюджета: {currency}; сопоставимость стоимости подтверждена capabilities сервера.")
        else:
            st.caption("Бюджет недоступен: нужны budget_available для всех предложений, полная стоимость и общая валюта.")
        submitted = st.form_submit_button("Рассчитать сценарий")
    if submitted:
        st.session_state.pop("_secondary_scenario", None)
        overrides: dict[str, Any] = {"service_target": target, "lead_time_delay_days": delay}
        if budget_enabled and currency:
            if not _positive_money(budget.strip()):
                st.error("Укажите положительный конечный бюджет, например 100000.00. Ввод сохранён.")
                return
            overrides["budget_cap"] = budget.strip()
        payload = {"base_run_id": run_id, "overrides": overrides, "seed": seed}
        payload["idempotency_key"] = _request_key("scenario", payload)
        try:
            result = client.create_scenario(payload)
            if not result.get("scenario_id"):
                st.error("API не вернул scenario_id. Результат неизвестен; повтор использует тот же request key.")
                return
            st.session_state["_secondary_scenario"] = {"id": result["scenario_id"], "request": payload, "base_versions": versions}
        except ApiError as error:
            show_api_error(error)
            if error.ambiguous:
                st.warning("Результат отправки неизвестен. Повтор тех же параметров использует прежний idempotency key.")
            return
    selected = st.session_state.get("_secondary_scenario")
    if not selected:
        return
    st.button("Обновить статус сценария", key="secondary_scenario_refresh")
    try:
        scenario = client.get_scenario(selected["id"])
    except ApiError as error:
        show_api_error(error)
        return
    wrong_base = scenario.get("base_run_id") not in (None, run_id)
    missing_base = scenario.get("status") == "succeeded" and scenario.get("base_run_id") != run_id
    wrong_snapshot = scenario.get("snapshot_id") not in (None, snapshot_id)
    wrong_seed = scenario.get("seed") not in (None, seed)
    if wrong_base or missing_base or wrong_snapshot or wrong_seed:
        _clear_scenario()
        st.error("Сервер не подтвердил ожидаемую базу, snapshot или seed сценария; сравнение скрыто.")
        return
    _show_job(scenario, "Сценарий")
    st.caption(f"Scenario ID: {selected['id']}")
    if scenario.get("status") != "succeeded":
        return
    st.write("**База / Сценарий**")
    st.json({"base_policy": policy, "scenario_overrides": selected["request"]["overrides"]})
    _show_scenario_comparison(scenario, currency)
    st.caption("Риск дефицита, потерянная маржа, экономия и достигнутый сервис не оценены интерфейсом. Оценка доступна только при наличии соответствующего поля в серверной сводке выше.")
    if scenario.get("assumptions"):
        with st.expander("Допущения сценария"):
            st.json(scenario["assumptions"])
    show_issues(scenario.get("warnings", []))
    st.info("Для применения политики создайте новый обычный расчёт и пройдите проверку и утверждение его предложения.")


def _render_snapshot_job(client: Any) -> None:
    with st.expander("Продолжить наблюдение за импортом по job ID"):
        with st.form("secondary_resume_job"):
            job_id = st.text_input("Job ID импорта", key="secondary_resume_job_id")
            if st.form_submit_button("Наблюдать за заданием") and job_id.strip():
                st.session_state["_secondary_snapshot_job"] = job_id.strip()
    job_id = st.session_state.get("_secondary_snapshot_job")
    if not job_id:
        return
    st.button("Обновить статус импорта", key="secondary_snapshot_refresh")
    try:
        job = client.get_job(job_id)
        _show_job(job, "Создание snapshot")
        if job.get("status") != "succeeded":
            return
        snapshot_id = _snapshot_from_ref(job.get("result_ref"))
        if not snapshot_id:
            st.info("Задание завершено. API не вернул распознаваемую ссылку на snapshot; выберите его по ID ниже.")
            if job.get("result_ref"):
                st.code(str(job["result_ref"]))
            return
        snapshot = client.get_snapshot(snapshot_id)
        _show_mode(snapshot)
        show_quality(snapshot.get("quality", {}))
        if st.button("Использовать готовый snapshot", key="secondary_use_created_snapshot"):
            _select_snapshot(snapshot_id)
            st.rerun()
    except ApiError as error:
        show_api_error(error)


def _render_sources_and_import(client: Any) -> None:
    st.write("**Зарегистрированные источники**")
    try:
        response = client.list_sources()
    except ApiError as error:
        show_api_error(error)
        return
    sources = _items(response, "sources")
    if sources:
        st.dataframe(sources, hide_index=True, width="stretch")
    else:
        st.info("API не вернул список зарегистрированных source IDs. Можно выбрать готовый snapshot по ID.")
    with st.expander("Метаданные источников из API"):
        st.json(response)
    source_ids = [s["source_id"] for s in sources if isinstance(s.get("source_id"), str)]
    with st.form("secondary_create_snapshot"):
        chosen = st.multiselect("Источники для нового snapshot", source_ids, key="secondary_sources")
        mapping = st.text_input("Версия mapping", value=st.session_state.get("mapping_version", "1.0"), key="secondary_mapping")
        mode = st.selectbox("Режим данных", ["synthetic_demo", "real_preview"], index=0 if st.session_state.get("data_mode", "synthetic_demo") == "synthetic_demo" else 1, key="secondary_import_mode")
        as_of = st.text_input("Момент воспроизведения as_of (с часовым поясом)", value=st.session_state.get("as_of", "2026-09-01T00:00:00+00:00"), key="secondary_as_of")
        create = st.form_submit_button("Создать snapshot", disabled=not source_ids)
    if create:
        if not chosen or not mapping.strip():
            st.error("Выберите хотя бы один источник и укажите версию mapping.")
        else:
            try:
                parsed = datetime.fromisoformat(as_of.strip().replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("timezone missing")
            except ValueError:
                st.error("as_of должен содержать дату, время и часовой пояс, например 2026-09-01T00:00:00+00:00.")
            else:
                payload = {"source_ids": sorted(chosen), "mapping_version": mapping.strip(), "mode": mode, "as_of": as_of.strip()}
                signature = _fingerprint(payload)
                submissions = st.session_state.setdefault("_secondary_snapshot_submissions", {})
                prior = submissions.get(signature)
                if prior and prior.get("job_id"):
                    st.session_state["_secondary_snapshot_job"] = prior["job_id"]
                    st.info("Этот запрос уже принят. Продолжаем наблюдение за его заданием.")
                elif prior and prior.get("ambiguous"):
                    st.warning("Результат предыдущей отправки неизвестен. POST /snapshots не имеет согласованного idempotency key; повтор не отправлен. Укажите подтверждённый job ID или snapshot ID.")
                else:
                    try:
                        result = client.create_snapshot(payload)
                        if result.get("job_id"):
                            submissions[signature] = result
                            st.session_state["_secondary_snapshot_job"] = result["job_id"]
                        else:
                            submissions[signature] = {"ambiguous": True}
                            st.error("API не вернул job ID. Результат неизвестен; повторная отправка остановлена.")
                    except ApiError as error:
                        if error.ambiguous:
                            submissions[signature] = {"ambiguous": True}
                        show_api_error(error)
    _render_snapshot_job(client)


def _render_planning(client: Any, snapshot: dict[str, Any]) -> None:
    st.write("**Обычный расчёт предложений**")
    quality = snapshot.get("quality", {})
    can_plan = quality.get("capabilities", {}).get("can_plan", False)
    if not can_plan:
        st.warning("Расчёт недоступен: snapshot не получил capability can_plan. Причины показаны в качестве данных.")
    with st.form("secondary_planning_form"):
        target = st.selectbox("Цель базового расчёта: цикл без дефицита", [0.95, 0.99], format_func=lambda v: f"{v:.0%}", key="secondary_base_target")
        delay = st.selectbox("Задержка базового расчёта, календарных дней", [0, 7, 14], key="secondary_base_delay")
        policy_version = st.text_input("Версия политики", value=st.session_state.get("policy_version", "1.0"), key="secondary_policy_version")
        start = st.form_submit_button("Запустить базовый расчёт", disabled=not can_plan)
    if start:
        if not policy_version.strip():
            st.error("Укажите версию политики.")
        else:
            policy = {"service_metric": "cycle_service", "service_target": target, "lead_time_delay_days": delay, "policy_version": policy_version.strip()}
            payload = {"snapshot_id": snapshot["snapshot_id"], "policy": policy}
            payload["idempotency_key"] = _request_key("planning", payload)
            try:
                result = client.create_planning_run(payload)
                if not result.get("run_id"):
                    st.error("API не вернул run ID. Повтор с прежними параметрами использует тот же request key.")
                else:
                    _select_run(result["run_id"])
                    st.session_state.setdefault("_secondary_run_contexts", {})[result["run_id"]] = {"snapshot_id": snapshot["snapshot_id"], "policy": policy, "seed": st.session_state.get("base_seed", 42)}
                    st.rerun()
            except ApiError as error:
                show_api_error(error)
                if error.ambiguous:
                    st.warning("Результат отправки неизвестен. Повтор тех же параметров использует прежний idempotency key.")
    run_id = st.session_state.get("run_id")
    if not run_id:
        return
    st.button("Обновить статус расчёта", key="secondary_run_refresh")
    try:
        run = client.get_planning_run(run_id)
        known_snapshot = run.get("snapshot_id") or st.session_state.get("_secondary_run_contexts", {}).get(run_id, {}).get("snapshot_id")
        if known_snapshot and known_snapshot != snapshot.get("snapshot_id"):
            st.error("Текущий run относится к другому snapshot. Запустите расчёт выбранного snapshot.")
            return
        _show_job(run, "Базовый расчёт")
        st.caption(f"Run ID: {run_id}")
        if run.get("quality"):
            show_quality(run["quality"])
        if run.get("status") == "succeeded":
            listing = client.list_proposals(run_id=run_id, limit=200)
            proposals = _items(listing)
            if not proposals:
                st.info("Расчёт завершён. Сервер не создал предложений; проверьте качество и причины исключения.")
            else:
                st.dataframe(proposals, hide_index=True, width="stretch")
                ids = [p["proposal_id"] for p in proposals if p.get("proposal_id")]
                if ids:
                    chosen = st.selectbox("Предложение для вкладки «Заказы»", ids, key=f"secondary_proposal_{run_id}")
                    if st.button("Выбрать предложение", key="secondary_choose_proposal"):
                        st.session_state["proposal_id"] = chosen
                        st.session_state["pending_proposal_selection"] = chosen
                        _clear_scenario()
                        st.rerun()
                if isinstance(listing, dict) and listing.get("next_cursor"):
                    st.caption("Показаны первые 200 предложений. Остальные доступны через фильтры вкладки «Заказы».")
    except ApiError as error:
        show_api_error(error)


def _render_projects(client: Any) -> None:
    st.write("**Проектные и регулярные продажи · только чтение**")
    st.caption("Всплеск сохраняется в истории. Исторический проект не создаёт будущую закупку без подтверждённого обязательства. Номер документа не является идентификатором клиента.")
    run_id = st.session_state.get("run_id")
    if not run_id:
        st.info("Для просмотра классификации выберите базовый расчёт.")
        return
    try:
        run = client.get_planning_run(run_id)
    except ApiError as error:
        show_api_error(error)
        return
    if run.get("status") != "succeeded":
        st.info("Классификация будет доступна после успешного завершения выбранного расчёта.")
        return
    known_snapshot = run.get("snapshot_id") or st.session_state.get("_secondary_run_contexts", {}).get(run_id, {}).get("snapshot_id")
    if known_snapshot and known_snapshot != st.session_state.get("snapshot_id"):
        st.error("Классификация относится к другому snapshot; данные скрыты.")
        return
    label = st.selectbox("Метка события", [None, "regular", "project", "suspected_project", "uncertain"], format_func=lambda v: "Все метки" if v is None else v, key="secondary_project_label")
    page_size = st.selectbox("Событий на странице", [25, 50, 100, 200], index=1, key="secondary_project_limit")
    context = (st.session_state.get("client_mode"), st.session_state.get("snapshot_id"), run_id, label, page_size)
    if st.session_state.get("_secondary_events_context") != context:
        st.session_state["_secondary_events_context"] = context
        st.session_state["_secondary_events_cursors"] = [None]
    cursors = st.session_state["_secondary_events_cursors"]
    try:
        response = client.list_demand_events(run_id, label=label, cursor=cursors[-1], limit=page_size)
    except ApiError as error:
        show_api_error(error)
        return
    events = _items(response, "events")
    if not events:
        st.info("В выбранном расчёте нет событий с этой меткой.")
    else:
        columns = ("event_id", "sku_id", "warehouse_id", "observed_qty", "regular_qty", "project_qty", "uncertain_qty", "label", "reason_codes", "reason", "confidence", "review_status", "event_at", "doc_id", "customer_token")
        rows = []
        for event in events:
            row = {key: event.get(key) for key in columns if any(key in item for item in events)}
            for key in ("observed_qty", "regular_qty", "project_qty", "uncertain_qty"):
                if key in row:
                    row[key] = format_decimal(row[key])
            if isinstance(row.get("reason_codes"), list):
                row["reason_codes"] = ", ".join(map(str, row["reason_codes"]))
            rows.append(row)
        st.dataframe(rows, hide_index=True, width="stretch")
        if not any(event.get("customer_token") for event in events):
            st.caption("Классификация по событиям/документам; клиентские идентификаторы в ответе отсутствуют.")
    next_cursor = response.get("next_cursor") if isinstance(response, dict) else None
    left, right = st.columns(2)
    if left.button("Предыдущая страница событий", disabled=len(cursors) <= 1, key="secondary_events_prev"):
        cursors.pop()
        st.rerun()
    if right.button("Следующая страница событий", disabled=not next_cursor or next_cursor in cursors, key="secondary_events_next"):
        cursors.append(next_cursor)
        st.rerun()
    if isinstance(response, dict):
        diagnostics = {key: value for key, value in response.items() if key not in ("items", "events", "next_cursor")}
        if diagnostics:
            with st.expander("Диагностика классификации из API"):
                st.json(diagnostics)


def render_data(client: Any) -> None:
    st.subheader("Данные/проекты")
    st.caption("Источники регистрируются на backend. Импорт и расчёты выполняются через API.")
    _render_sources_and_import(client)
    st.divider()
    with st.form("secondary_select_snapshot"):
        selected_id = st.text_input("Готовый snapshot ID", value=st.session_state.get("snapshot_id") or "", key="secondary_snapshot_input")
        select = st.form_submit_button("Проверить и выбрать snapshot")
    if select:
        if not selected_id.strip():
            st.error("Укажите snapshot ID.")
        else:
            try:
                snapshot = client.get_snapshot(selected_id.strip())
                if snapshot.get("snapshot_id") != selected_id.strip():
                    st.error("API вернул snapshot с другим ID; выбор не изменён.")
                else:
                    _select_snapshot(selected_id.strip())
                    st.rerun()
            except ApiError as error:
                show_api_error(error)
    snapshot_id = st.session_state.get("snapshot_id")
    if snapshot_id:
        try:
            snapshot = client.get_snapshot(snapshot_id)
            st.write(f"**Выбранный snapshot: {snapshot_id}**")
            _show_mode(snapshot)
            show_quality(snapshot.get("quality", {}))
            with st.expander("Метаданные и допущения snapshot"):
                st.json(snapshot)
            _render_planning(client, snapshot)
        except ApiError as error:
            show_api_error(error)
    else:
        st.info("Создайте snapshot или явно выберите готовый snapshot по ID.")
    st.divider()
    _render_projects(client)
