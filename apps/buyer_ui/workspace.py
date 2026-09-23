"""Buyer-facing saved work and a source-to-order workflow using server operations."""
from __future__ import annotations

from datetime import datetime
from typing import Any

import streamlit as st

from .client import ApiError
from .formatting import format_date, show_api_error, show_quality
from .presentation import section_heading
from .secondary_views import _request_key, _select_run, _select_snapshot, _show_job, _snapshot_from_ref


def activate_run(run: dict[str, Any]) -> None:
    """Change context together; never carry an approval or draft to another plan."""
    _select_snapshot(run["snapshot_id"])
    _select_run(run["id"])
    st.session_state["base_seed"] = run.get("seed", 42)
    st.session_state["data_mode"] = run.get("mode", "synthetic_demo")
    st.session_state.setdefault("_secondary_run_contexts", {})[run["id"]] = {
        "snapshot_id": run["snapshot_id"], "policy": run.get("policy", {}),
        "review_overrides": run.get("review_overrides", []),
    }


def load_workspace(client: Any, *, refresh: bool = False) -> dict[str, Any] | None:
    if refresh or "buyer_workspace" not in st.session_state:
        try:
            st.session_state["buyer_workspace"] = client.workspace()
        except ApiError as error:
            show_api_error(error)
            st.caption("Не удалось загрузить сохранённые расчёты. Проверьте подключение и повторите обновление.")
            return None
    return st.session_state["buyer_workspace"]


def restore_workspace(client: Any) -> None:
    if st.session_state.get("buyer_workspace_restored"):
        return
    workspace = load_workspace(client)
    if workspace is None:
        return
    if not st.session_state.get("run_id"):
        relevant = [r for r in workspace.get("runs", [])
                    if r.get("status") in ("queued", "running", "succeeded") and r.get("snapshot_id")]
        completed = [r for r in relevant if r.get("status") == "succeeded"]
        # Workspace lists newest requests first. Resume the buyer's latest work,
        # observing its server job rather than submitting another calculation.
        if relevant and relevant[0].get("status") in ("queued", "running"):
            pending = relevant[0]
            _select_snapshot(pending["snapshot_id"])
            st.session_state["buyer_flow"] = {"stage": "run", "run_id": pending["id"],
                                             "snapshot_id": pending["snapshot_id"], "policy": pending.get("policy", {})}
            st.session_state["pending_page"] = "Данные"
        elif completed:
            preferred = next((r for r in completed if r["id"] == workspace.get("latest_run_id")), completed[0])
            latest_snapshot = workspace.get("latest_snapshot_id")
            if latest_snapshot and latest_snapshot != preferred["snapshot_id"]:
                _select_snapshot(latest_snapshot)
                st.session_state["pending_page"] = "Данные"
            else:
                activate_run(preferred)
        elif workspace.get("latest_snapshot_id"):
            _select_snapshot(workspace["latest_snapshot_id"])
            st.session_state["pending_page"] = "Данные"
    st.session_state["buyer_workspace_restored"] = True


def run_label(run: dict[str, Any], position: int = 1) -> str:
    kind = "Демонстрация" if run.get("mode") == "synthetic_demo" else "Данные компании"
    date = format_date(run.get("as_of") or run.get("created_at"))
    return f"{kind} · данные на {date} · расчёт {position}"


def render_saved_work(client: Any) -> None:
    workspace = load_workspace(client)
    if workspace is None:
        if st.button("Повторить подключение", key="workspace_retry"):
            st.session_state.pop("buyer_workspace", None)
            st.rerun()
        return
    runs = [r for r in workspace.get("runs", []) if r.get("status") == "succeeded" and r.get("snapshot_id")]
    if not runs:
        return
    with st.expander("Сохранённые планы закупки", expanded=False):
        ids = [r["id"] for r in runs]
        labels = {r["id"]: run_label(r, len(runs) - i) for i, r in enumerate(runs)}
        selected = st.selectbox("Выберите план", ids,
                                index=ids.index(st.session_state.get("run_id")) if st.session_state.get("run_id") in ids else 0,
                                format_func=labels.get, key="saved_run_picker")
        st.caption("Заказы, правки и утверждения хранятся на сервере. При смене плана несохранённый ввод будет очищен.")
        left, right = st.columns(2)
        if left.button("Открыть выбранный план", key="open_saved_run"):
            try:
                activate_run(client.get_planning_run(selected))
                st.session_state["pending_page"] = "Заказы"
                st.rerun()
            except ApiError as error:
                show_api_error(error)
        if right.button("Обновить список", key="refresh_saved_runs"):
            st.session_state.pop("buyer_workspace", None)
            st.rerun()


def source_groups(sources: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for source in sources:
        if source.get("available_for_import") is False:
            continue
        if source.get("mode") == "synthetic_demo":
            groups.setdefault("Демонстрационный набор", []).append(source)
        elif source.get("mode") == "real_preview":
            label = "Реальные данные · " + (source.get("supplier_name") or "компании")
            groups.setdefault(label, []).append(source)
    return groups


def snapshot_request(sources: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Use registered metadata; buyers never need to type IDs or mapping versions."""
    if not sources:
        return None
    modes = {source.get("mode") for source in sources}
    mappings = {source.get("mapping_version") for source in sources}
    dates = [source.get("default_as_of") or source.get("as_of") for source in sources]
    if len(modes) != 1 or len(mappings) != 1 or None in mappings or not all(dates):
        return None
    try:
        parsed = [datetime.fromisoformat(value.replace("Z", "+00:00")) for value in dates]
        if any(value.tzinfo is None for value in parsed):
            return None
    except (ValueError, TypeError):
        return None
    return {"source_ids": sorted(source["source_id"] for source in sources),
            "mode": next(iter(modes)), "mapping_version": next(iter(mappings)),
            "as_of": dates[parsed.index(max(parsed))]}


def start_plan(client: Any, snapshot_id: str, policy: dict[str, Any], *,
               review_overrides: list[dict[str, Any]] | None = None) -> str:
    payload = {"snapshot_id": snapshot_id, "policy": policy}
    if review_overrides:
        payload["review_overrides"] = review_overrides
    payload["idempotency_key"] = _request_key("planning", payload)
    accepted = client.create_planning_run(payload)
    if not accepted.get("run_id"):
        raise ApiError(None, "INVALID_RESPONSE", "Сервер не подтвердил запуск плана. Повторите действие с теми же условиями.", ambiguous=True)
    st.session_state["buyer_flow"] = {"stage": "run", "run_id": accepted["run_id"], "snapshot_id": snapshot_id,
                                     "policy": policy, "review_overrides": review_overrides or []}
    return accepted["run_id"]


@st.fragment(run_every=1)
def render_progress(client: Any) -> None:
    flow = st.session_state.get("buyer_flow")
    if not flow:
        return
    try:
        if flow["stage"] == "snapshot":
            job = client.get_job(flow["job_id"])
            _show_job(job, "Подготовка данных")
            if job.get("status") == "succeeded":
                snapshot_id = job.get("snapshot_id") or _snapshot_from_ref(job.get("result_ref"))
                if not snapshot_id:
                    st.error("Подготовка завершена, но сервер не вернул данные. Обратитесь к ответственному за систему.")
                    return
                snapshot = client.get_snapshot(snapshot_id)
                if not snapshot.get("quality", {}).get("capabilities", {}).get("can_plan"):
                    _select_snapshot(snapshot_id)
                    st.session_state["buyer_pending_policy"] = flow["policy"]
                    st.session_state.pop("buyer_flow", None)
                    st.session_state.pop("buyer_workspace", None)
                    st.session_state["pending_page"] = "Данные"
                    st.rerun()
                start_plan(client, snapshot_id, flow["policy"])
                st.rerun()
        else:
            job = client.get_planning_run(flow["run_id"])
            _show_job(job, "Расчёт закупки")
            if job.get("status") == "succeeded":
                activate_run(job)
                st.session_state.pop("buyer_flow", None)
                st.session_state.pop("buyer_workspace", None)
                st.session_state["pending_page"] = "Заказы"
                st.session_state["order_flash"] = ("success", "План готов. Проверьте товары и утвердите заказ каждому поставщику.")
                st.rerun()
        if job.get("status") in ("failed", "succeeded"):
            if st.button("Вернуться к выбору данных", key="buyer_flow_reset"):
                st.session_state.pop("buyer_flow", None)
                st.rerun()
        else:
            st.caption("Статус обновляется автоматически. Ваши прежние планы сохранены.")
    except ApiError as error:
        show_api_error(error)
        if st.button("Повторить проверку состояния", key="buyer_flow_retry"):
            st.rerun()


def render_new_plan(client: Any) -> None:
    section_heading(1, "Подготовьте план закупки", "Выберите данные и требуемый уровень наличия. Мы проверим источники, рассчитаем спрос и сгруппируем товары по поставщикам.")
    if st.session_state.get("buyer_flow"):
        render_progress(client)
        return
    try:
        sources = client.list_sources().get("items", [])
    except ApiError as error:
        show_api_error(error)
        return
    groups = source_groups(sources)
    if not groups:
        st.info("Источники данных пока не подключены. Ответственному за систему нужно подключить продажи и остатки компании.")
        return
    choice = st.selectbox("Источник для расчёта", list(groups), key="buyer_source_group")
    with st.form("buyer_new_plan", border=False):
        request = snapshot_request(groups[choice])
        if choice == "Демонстрационный набор":
            st.info("Синтетические продажи и условия закупки для проверки всего процесса. Выгрузка будет помечена как демонстрация.")
        else:
            st.info("Загрузим продажи компании. Если данных об остатках или условиях поставки не хватает, вы сможете заполнить их здесь и продолжить расчёт.")
            descriptions = dict.fromkeys(source.get("description") for source in groups[choice] if source.get("description"))
            for description in descriptions:
                st.caption(description)
        if request:
            st.caption(f"Данные на {format_date(request['as_of'])} · источников: {len(groups[choice])}")
        else:
            st.warning("Для источников не настроена дата расчёта или сопоставление полей. Обратитесь к ответственному за данные.")
        if request and request["mode"] == "real_preview":
            request["sku_limit"] = st.selectbox("Охват ассортимента", [20, 100, 10000],
                                               format_func=lambda value: "Все поддерживаемые товары" if value == 10000 else f"Первые {value} товаров",
                                               key="buyer_sku_limit")
            st.caption("Полный ассортимент может потребовать больше времени на подготовку и проверку условий.")
        target = st.selectbox("Цель: цикл без дефицита", [0.95, 0.99], format_func=lambda value: f"{value:.0%}", key="buyer_plan_target",
                              help="Целевая вероятность пройти цикл поставки без нехватки. Это параметр расчёта, а не гарантия.")
        submitted = st.form_submit_button("Подготовить план закупки", type="primary", disabled=request is None)
    if submitted and request:
        request["idempotency_key"] = _request_key("snapshot", request)
        try:
            accepted = client.create_snapshot(request)
            if not accepted.get("job_id"):
                raise ApiError(None, "INVALID_RESPONSE", "Подготовка не подтверждена сервером. Повторите действие с теми же условиями.", ambiguous=True)
            st.session_state["buyer_flow"] = {"stage": "snapshot", "job_id": accepted["job_id"],
                "policy": {"service_metric": "cycle_service", "service_target": target, "lead_time_delay_days": 0, "policy_version": "mvp-v1"}}
            st.rerun()
        except ApiError as error:
            show_api_error(error)


def render_buyer_data(client: Any) -> None:
    snapshot_id = st.session_state.get("snapshot_id")
    if snapshot_id:
        try:
            snapshot = client.get_snapshot(snapshot_id)
            with st.container(border=True):
                st.subheader("Данные текущего плана")
                st.caption(f"{'Синтетические данные' if snapshot.get('mode') == 'synthetic_demo' else 'Реальные данные компании'} · на {format_date(snapshot.get('as_of'))}")
                show_quality(snapshot.get("quality", {}))
            if not st.session_state.get("buyer_flow"):
                if snapshot.get("quality", {}).get("capabilities", {}).get("can_plan"):
                    with st.form("buyer_recalculate_current", border=False):
                        target = st.selectbox("Цель наличия для нового расчёта", [0.95, 0.99],
                                              format_func=lambda value: f"{value:.0%}", key="buyer_current_target")
                        recalculate = st.form_submit_button("Рассчитать заказ по этим данным", type="primary")
                    if recalculate:
                        start_plan(client, snapshot_id, {"service_metric": "cycle_service", "service_target": target,
                                                        "lead_time_delay_days": 0, "policy_version": "mvp-v1"})
                        st.rerun()
                from .procurement_inputs import render_procurement_inputs
                render_procurement_inputs(client, snapshot)
        except ApiError as error:
            show_api_error(error)
    with st.expander("Подготовить новый план", expanded=not snapshot_id or bool(st.session_state.get("buyer_flow"))):
        render_new_plan(client)
    try:
        sources = client.list_sources().get("items", [])
        with st.expander("Подключённые источники и охват данных"):
            st.dataframe([{"Источник": source.get("name", "Источник"),
                           "Поставщик": source.get("supplier_name") or "Не указан",
                           "Доступность": "Можно проверить и загрузить" if source.get("available_for_import", True) else "Зарегистрирован; импорт ещё не подключён",
                           "Описание": source.get("description") or "Описание отсутствует"}
                          for source in sources], hide_index=True, width="stretch")
    except ApiError as error:
        show_api_error(error)
