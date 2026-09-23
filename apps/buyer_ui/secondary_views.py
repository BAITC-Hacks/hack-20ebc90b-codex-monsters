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
from .formatting import format_date, format_decimal, format_money, show_api_error, show_issues, show_quality


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
        st.caption("Демонстрация на синтетических данных.")
    elif record.get("mode") == "real_preview":
        st.info("Предпросмотр реальных данных; ограничения готовности указаны сервером.")
    if record.get("as_of"):
        st.caption(f"Данные на {format_date(record['as_of'])}.")


def _show_job(record: dict[str, Any], title: str) -> None:
    labels = {"queued": "в очереди", "running": "выполняется", "succeeded": "завершён", "failed": "ошибка"}
    status = record.get("status")
    st.write(f"**{title}: {labels.get(status, status or 'статус не передан')}**")
    details = {key: record[key] for key in ("id", "stage", "created_at", "updated_at") if record.get(key) is not None}
    if details:
        with st.expander("Сведения о выполнении"):
            st.json(details)
    progress = record.get("progress")
    if status in ("queued", "running") and isinstance(progress, (float, int)) and 0 <= progress <= 1:
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


def _show_scenario_comparison(
    scenario: dict[str, Any], currency: str | None, proposals: list[dict[str, Any]]
) -> None:
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
        if any(key in summary for key in ("baseline_total_cost", "base_total_cost", "scenario_total_cost")):
            cost_currency = currency if summary.get("currency") == currency else None
            baseline_cost = summary.get("baseline_total_cost", summary.get("base_total_cost"))
            rows.append({"Показатель": "Закупочная стоимость", "База": format_money(baseline_cost, cost_currency), "Сценарий": format_money(summary.get("scenario_total_cost"), cost_currency)})
        if rows:
            st.dataframe(rows, hide_index=True, width="stretch")
    if isinstance(changed, list):
        # API comparison rows use business identity, not generated proposal line IDs.
        lines = {
            line.get("line_id") or json.dumps([line.get("sku_id"), line.get("supplier_id"), line.get("warehouse_id")]): line
            for line in changed if isinstance(line, dict) and line.get("sku_id")
        }
        if lines:
            metadata = {
                (line.get("sku_id"), proposal.get("supplier_id"), proposal.get("warehouse_id")): line
                for proposal in proposals for line in proposal.get("lines", [])
            }
            selected_id = st.selectbox("Строка сравнения", list(lines), format_func=lambda value: " · ".join(str(lines[value][key]) for key in ("sku_id", "name", "supplier_id", "warehouse_id") if lines[value].get(key)), key=f"secondary_scenario_line_{scenario.get('id', 'result')}")
            line = lines[selected_id]
            detail = metadata.get((line.get("sku_id"), line.get("supplier_id"), line.get("warehouse_id")), {})
            rows = []
            for title, base_key, scenario_key, uom in (
                ("Заказ в базовой единице", "baseline_base_qty", "scenario_base_qty", line.get("base_uom") or detail.get("base_uom")),
                ("Заказ", "base_purchase_qty", "scenario_purchase_qty", line.get("purchase_uom")),
                ("Страховой запас", "base_safety_stock", "scenario_safety_stock", line.get("base_uom")),
                ("Потребность до ограничений", "base_raw_need", "scenario_raw_need", line.get("base_uom")),
            ):
                if base_key in line or scenario_key in line:
                    rows.append({"Показатель": title, "База": format_decimal(line.get(base_key)), "Сценарий": format_decimal(line.get(scenario_key)), "Единица": uom or "Не передана"})
            if rows:
                st.dataframe(rows, hide_index=True, width="stretch")
            if "delta_base_qty" in line:
                st.caption(f"Изменение заказа из API: {format_decimal(line['delta_base_qty'], signed=True)} {line.get('base_uom') or detail.get('base_uom') or '(единица не передана)'}")
            if not any(key in line for key in ("base_safety_stock", "scenario_safety_stock")):
                st.caption("Страховой запас сценария не передан API; сравнение SS недоступно.")
    if summary is None:
        st.info("Сервер не вернул сводку сравнения.")
    with st.expander("Полный результат сравнения из API"):
        st.json({"changed_lines": changed, "summary": summary})


def render_scenarios(client: Any) -> None:
    st.subheader("Что изменится в заказе?")
    st.caption("Сравните варианты на тех же данных. Текущий заказ останется без изменений.")
    snapshot_id = st.session_state.get("snapshot_id")
    run_id = st.session_state.get("run_id")
    context = (st.session_state.get("client_mode"), snapshot_id, run_id, st.session_state.get("proposal_id"))
    if st.session_state.get("_secondary_scenario_context") != context:
        _clear_scenario()
        st.session_state["_secondary_scenario_context"] = context
    if not snapshot_id or not run_id:
        st.info("Выберите snapshot и завершите базовый расчёт в разделе «Данные».")
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
    seed = base_run.get("seed")
    if seed is None:
        seed = st.session_state.get("base_seed", 42)
    saved = st.session_state.get("_secondary_scenario")
    if saved and saved.get("request", {}).get("seed") != seed:
        st.session_state.pop("_secondary_scenario", None)
        st.info("Seed базового расчёта изменился. Прежнее сравнение скрыто.")
    with st.expander("Параметры исходного расчёта"):
        st.json({"snapshot_id": snapshot_id, "run_id": run_id, "seed": seed,
                 "model_version": base_run.get("model_version")})
        if base_run.get("seed") is None:
            st.caption("API не сообщает seed базового прогноза; сервер использует сохранённый прогноз исходного расчёта.")
    if proposals:
        _show_mode(proposals[0])
    forecast_warnings = {
        issue.get("message", issue.get("code")): issue
        for proposal in proposals for line in proposal.get("lines", [])
        for issue in line.get("warnings", [])
        if isinstance(issue, dict) and issue.get("code") == "FORECAST_PROVIDER_NOT_CONNECTED"
    }
    show_issues(list(forecast_warnings.values()))
    currency = _budget_currency(proposals)
    policy = base_run.get("policy") or submitted_context.get("policy")
    with st.expander("Базовые параметры и ограничения"):
        if policy:
            st.json(policy)
        else:
            st.info("Параметры базовой политики не переданы API.")
        for proposal in proposals:
            st.write(f"Поставщик: {proposal.get('supplier_id', '—')} · склад: {proposal.get('warehouse_id', '—')}")
            st.write(f"Стоимость текущего предложения: {format_money(proposal.get('total_cost'), proposal.get('currency'))}")
            show_issues(proposal.get("warnings", []))
            for reason in proposal.get("capabilities", {}).get("reasons", []):
                st.caption(str(reason))
    with st.form("secondary_scenario_form"):
        protection, delivery = st.columns(2)
        target = protection.selectbox("Цель: цикл без дефицита", [0.95, 0.99], index=1,
                                      format_func=lambda v: f"{v:.0%}", key="secondary_target",
                                      help="Целевая вероятность пройти цикл поставки без дефицита. Это цель расчёта, а не гарантия.")
        delay = delivery.selectbox("Поставщик задержится на", [0, 7, 14],
                                   format_func=lambda v: "Без задержки" if v == 0 else f"{v} дней", key="secondary_delay")
        with st.expander("Ограничить бюджет"):
            budget_enabled = st.checkbox("Учитывать лимит", disabled=not currency, key="secondary_budget_enabled")
            budget = st.text_input("Лимит закупки", disabled=not currency, key="secondary_budget",
                                   help=f"Сумма в {currency}" if currency else "Нужны цены всех товаров в одной валюте.")
            if not currency:
                st.caption("Лимит недоступен: нужны цены всех товаров в одной валюте и поддержка сервера.")
        submitted = st.form_submit_button("Сравнить варианты", type="primary")
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
    if scenario.get("status") != "succeeded":
        st.button("Обновить результат", key="secondary_scenario_refresh")
        return
    with st.expander("Обновить результат сравнения"):
        st.button("Обновить результат", key="secondary_scenario_refresh")
    st.write("**База / Сценарий**")
    st.caption("База — исходный результат завершённого расчёта. Ручные правки предложений не входят в базу сценария.")
    with st.expander("Параметры сравнения"):
        st.json({"base_policy": policy, "scenario_overrides": selected["request"]["overrides"]})
    _show_scenario_comparison(scenario, currency, proposals)
    if scenario.get("assumptions"):
        with st.expander("Допущения сценария"):
            st.json(scenario["assumptions"])
    show_issues(scenario.get("warnings", []))
    st.info("Чтобы использовать эти параметры в заказе, запустите новый расчёт в разделе «Данные» и утвердите результат.")


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
        _show_job(job, "Подготовка данных")
        if job.get("status") != "succeeded":
            return
        snapshot_id = job.get("snapshot_id") or _snapshot_from_ref(job.get("result_ref"))
        if not snapshot_id:
            st.info("Задание завершено. API не вернул распознаваемую ссылку на snapshot; выберите его по ID ниже.")
            if job.get("result_ref"):
                st.code(str(job["result_ref"]))
            return
        snapshot = client.get_snapshot(snapshot_id)
        if snapshot.get("snapshot_id") != snapshot_id:
            st.error("API вернул snapshot с другим ID; результат импорта не выбран.")
            return
        _show_mode(snapshot)
        show_quality(snapshot.get("quality", {}))
        if st.button("Использовать эти данные", key="secondary_use_created_snapshot"):
            _select_snapshot(snapshot_id)
            st.rerun()
    except ApiError as error:
        show_api_error(error)


def _render_sources_and_import(client: Any) -> None:
    st.caption("Выберите источники, уже подключённые к серверу. Загрузка файлов здесь не предусмотрена.")
    try:
        response = client.list_sources()
    except ApiError as error:
        show_api_error(error)
        return
    sources = _items(response, "sources")
    if sources:
        with st.expander("Список источников"):
            st.dataframe(sources, hide_index=True, width="stretch")
    else:
        st.info("API не вернул список зарегистрированных source IDs. Можно выбрать готовый snapshot по ID.")
    with st.expander("Метаданные источников из API"):
        st.json(response)
    source_ids = [s["source_id"] for s in sources if isinstance(s.get("source_id"), str)]
    with st.form("secondary_create_snapshot"):
        chosen = st.multiselect("Источники данных", source_ids, key="secondary_sources")
        with st.expander("Параметры импорта"):
            mapping = st.text_input("Версия mapping", value=st.session_state.get("mapping_version", "1.0"), key="secondary_mapping")
            mode = st.selectbox("Режим данных", ["synthetic_demo", "real_preview"], index=0 if st.session_state.get("data_mode", "synthetic_demo") == "synthetic_demo" else 1, key="secondary_import_mode")
            as_of = st.text_input("Момент воспроизведения as_of (с часовым поясом)", value=st.session_state.get("as_of", "2026-09-01T00:00:00+00:00"), key="secondary_as_of")
        create = st.form_submit_button("Подготовить данные", disabled=not source_ids)
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
    st.write("**Рассчитать заказ**")
    quality = snapshot.get("quality", {})
    can_plan = quality.get("capabilities", {}).get("can_plan", False)
    if not can_plan:
        st.warning("Расчёт недоступен. Откройте качество данных и исправьте указанные ограничения.")
    with st.form("secondary_planning_form"):
        with st.expander("Изменить параметры расчёта"):
            target = st.selectbox("Цель: цикл без дефицита", [0.95, 0.99], format_func=lambda v: f"{v:.0%}", key="secondary_base_target",
                                  help="Цель расчёта, а не гарантия отсутствия дефицита.")
            delay = st.selectbox("Задержка поставщика", [0, 7, 14], format_func=lambda v: "Без задержки" if v == 0 else f"{v} дней", key="secondary_base_delay")
            policy_version = st.text_input("Версия политики", value=st.session_state.get("policy_version", "1.0"), key="secondary_policy_version")
        delay_label = "без задержки" if delay == 0 else f"задержка {delay} дней"
        st.caption(f"Цель: {target:.0%}, {delay_label}.")
        start = st.form_submit_button("Рассчитать заказ", disabled=not can_plan, type="primary")
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
                    st.session_state.setdefault("_secondary_run_contexts", {})[result["run_id"]] = {"snapshot_id": snapshot["snapshot_id"], "policy": policy}
                    st.rerun()
            except ApiError as error:
                show_api_error(error)
                if error.ambiguous:
                    st.warning("Результат отправки неизвестен. Повтор тех же параметров использует прежний idempotency key.")
    run_id = st.session_state.get("run_id")
    if not run_id:
        return
    try:
        run = client.get_planning_run(run_id)
        known_snapshot = run.get("snapshot_id") or st.session_state.get("_secondary_run_contexts", {}).get(run_id, {}).get("snapshot_id")
        if known_snapshot and known_snapshot != snapshot.get("snapshot_id"):
            st.error("Текущий run относится к другому snapshot. Запустите расчёт выбранного snapshot.")
            return
        _show_job(run, "Базовый расчёт")
        if run.get("status") != "succeeded":
            st.button("Обновить статус расчёта", key="secondary_run_refresh")
        else:
            with st.expander("Обновить состояние расчёта"):
                st.button("Обновить статус расчёта", key="secondary_run_refresh")
        if run.get("status") == "succeeded":
            listing = client.list_proposals(run_id=run_id, limit=200)
            proposals = _items(listing)
            if not proposals:
                st.info("Расчёт завершён. Сервер не создал предложений; проверьте качество и причины исключения.")
            else:
                st.success(f"Готово: заказов по поставщикам — {len(proposals)}.")
                ids = [p["proposal_id"] for p in proposals if p.get("proposal_id")]
                if ids and st.button("Открыть заказы", key="secondary_choose_proposal"):
                    chosen = st.session_state.get("proposal_id")
                    chosen = chosen if chosen in ids else ids[0]
                    st.session_state["proposal_id"] = chosen
                    st.session_state["pending_proposal_selection"] = chosen
                    st.session_state["pending_page"] = "Заказы"
                    _clear_scenario()
                    st.rerun()
                if isinstance(listing, dict) and listing.get("next_cursor"):
                    st.caption("Остальные предложения доступны в разделе «Заказы».")
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
    label = st.selectbox("Метка события", [None, "regular", "project", "suspected_project", "uncertain"], format_func=lambda v: {None: "Все", "regular": "Регулярные", "project": "Проектные", "suspected_project": "Возможно проектные", "uncertain": "Не определено"}[v], key="secondary_project_label")
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
    st.subheader("Данные для заказа")
    snapshot_id = st.session_state.get("snapshot_id")
    if snapshot_id:
        try:
            snapshot = client.get_snapshot(snapshot_id)
            _show_mode(snapshot)
            quality = snapshot.get("quality", {})
            label = {"ready": "Данные готовы к работе", "degraded": "В данных есть ограничения", "blocked": "Данные требуют исправления"}.get(quality.get("status"), "Проверьте качество данных")
            with st.expander(label, expanded=quality.get("status") == "blocked"):
                show_quality(quality)
                with st.expander("Технические сведения"):
                    st.json(snapshot)
            _render_planning(client, snapshot)
        except ApiError as error:
            show_api_error(error)
    else:
        st.write("Выберите подключённые источники, подготовьте данные и запустите первый расчёт.")
    with st.expander("Обновить исходные данные", expanded=not snapshot_id):
        _render_sources_and_import(client)
        with st.expander("Выбрать готовые данные по ID"):
            with st.form("secondary_select_snapshot"):
                selected_id = st.text_input("ID набора данных", value=snapshot_id or "", key="secondary_snapshot_input")
                select = st.form_submit_button("Выбрать данные")
            if select:
                if not selected_id.strip():
                    st.error("Укажите ID набора данных.")
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
    with st.expander("Проектные и регулярные продажи"):
        _render_projects(client)
