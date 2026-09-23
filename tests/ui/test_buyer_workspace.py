"""The normal buyer flow needs no opaque IDs, mappings or API configuration."""
import os
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest

from apps.buyer_ui.client import MockClient
from apps.buyer_ui.workspace import snapshot_request, source_groups

APP = Path(__file__).resolve().parents[2] / "apps/buyer_ui/app.py"


def clean(app):
    assert not app.exception, [item.message for item in app.exception]
    assert not app.code
    assert not app.json


def test_registered_metadata_drives_import_without_inventing_dates():
    sources = [dict(source_id="sales", mode="real_preview", mapping_version="systeme-preview-v1",
                    default_as_of="2026-09-22T23:59:59+05:00", available_for_import=True,
                    supplier_name="Systeme Electric"),
               dict(source_id="catalog-only", mode="real_preview", available_for_import=False)]
    groups = source_groups(sources)
    assert list(groups) == ["Реальные данные · Systeme Electric"]
    request = snapshot_request(next(iter(groups.values())))
    assert request["source_ids"] == ["sales"]
    assert request["as_of"] == sources[0]["default_as_of"]
    assert snapshot_request([dict(sources[0], default_as_of=None)]) is None


def test_normal_workspace_restores_saved_run_and_hides_technical_controls():
    with patch.dict(os.environ, {"BUYER_UI_MODE": "mock", "BUYER_DEVELOPER_MODE": "0",
                                "BUYER_RUN_ID": "", "BUYER_SNAPSHOT_ID": ""}):
        app = AppTest.from_file(str(APP), default_timeout=15).run()
        clean(app)
        assert app.session_state["run_id"] == "demo-run"
        assert app.session_state["snapshot_id"] == "demo-snapshot"
        assert all(item.label not in {"Адрес API", "ID поставщика", "Номер набора данных"} for item in app.text_input)
        app.radio(key="workspace_page").set_value("Данные").run()
        clean(app)
        assert not any(item.label in {"Данные на дату и время", "Номер набора данных"} for item in app.text_input)
        assert any(button.label == "Подготовить план закупки" for button in app.button)
        assert app.selectbox(key="buyer_source_group").options == ["Демонстрационный набор"]


def test_source_to_order_automatic_flow_uses_backend_and_restores_server_result():
    harness = '''
import streamlit as st
from apps.buyer_ui.workspace import render_new_plan
render_new_plan(st.session_state["client"])
'''
    app = AppTest.from_string(harness, default_timeout=15)
    client = MockClient()
    app.session_state["client"] = client
    app.run()
    clean(app)
    next(button for button in app.button if button.label == "Подготовить план закупки").click().run()
    clean(app)
    assert app.session_state["run_id"] == "demo-run"
    assert app.session_state["snapshot_id"] == "demo-snapshot"
    assert "buyer_flow" not in app.session_state
    assert len(client._requests) == 1


def test_apply_scenario_creates_separate_plan_with_exact_policy_and_no_approval():
    harness = '''
import streamlit as st
from apps.buyer_ui.secondary_views import render_scenarios
render_scenarios(st.session_state["client"])
'''
    client = MockClient()
    original = client.get_proposal("demo-proposal-tools")
    calls = []

    def accept_new_plan(payload):
        calls.append(payload)
        return {"run_id": "new-operational-plan"}

    client.create_planning_run = accept_new_plan
    app = AppTest.from_string(harness, default_timeout=15)
    for key, value in {"client": client, "snapshot_id": "demo-snapshot", "run_id": "demo-run", "client_mode": "mock"}.items():
        app.session_state[key] = value
    app.run()
    next(button for button in app.button if button.label == "Сравнить варианты").click().run()
    next(button for button in app.button if button.label == "Применить условия к новому плану").click().run()
    clean(app)
    assert len(calls) == 1
    assert calls[0]["snapshot_id"] == "demo-snapshot"
    assert calls[0]["policy"]["service_target"] == 0.99
    assert calls[0]["policy"]["lead_time_delay_days"] == 0
    assert app.session_state["buyer_flow"]["run_id"] == "new-operational-plan"
    assert app.session_state["run_id"] == "demo-run"
    assert client.get_proposal("demo-proposal-tools") == original


def test_reload_during_calculation_resumes_job_without_creating_duplicate():
    harness = '''
import streamlit as st
from apps.buyer_ui.workspace import restore_workspace, render_progress
restore_workspace(st.session_state["client"])
if st.session_state.get("buyer_flow"):
    render_progress(st.session_state["client"])
'''
    client = MockClient()
    client._fixture["run"].update(status="running", stage="forecast", progress=0.4)
    calls = []

    def unexpected_create(payload):
        calls.append(payload)
        raise AssertionError("Reload must not resubmit a calculation")

    client.create_planning_run = unexpected_create
    app = AppTest.from_string(harness, default_timeout=15)
    app.session_state["client"] = client
    app.run()
    clean(app)
    assert app.session_state["buyer_flow"]["run_id"] == "demo-run"
    assert app.session_state["pending_page"] == "Данные"
    assert any("автоматически" in item.value for item in app.caption)
    client._fixture["run"].update(status="succeeded", stage="complete", progress=1.0)
    app.run()
    clean(app)
    assert "buyer_flow" not in app.session_state
    assert app.session_state["run_id"] == "demo-run"
    assert app.session_state["pending_page"] == "Заказы"
    assert calls == []


def test_methodology_is_disclosed_without_internal_field_keys():
    harness = '''
import streamlit as st
from apps.buyer_ui.views import _ledger
_ledger(st.session_state["line"])
'''
    line = MockClient().get_proposal("demo-proposal-tools")["lines"][0]
    line["warnings"] = [
        {"code": "PLANNING_ASSUMPTION", "message": "all_inputs: Фиксированный учебный набор.", "severity": "warning"},
        {"code": "IID_NORMAL_ASSUMPTION", "message": "normal error model", "severity": "warning"},
        {"code": "PROJECTED_STOCKOUT", "message": "Stockout projected", "severity": "warning"},
    ]
    app = AppTest.from_string(harness, default_timeout=15)
    app.session_state["line"] = line
    app.run()
    clean(app)
    assumption_panel = next(item for item in app.expander if item.label == "Допущения расчёта")
    assert any("Фиксированный учебный набор" in item.value for item in assumption_panel.warning)
    assert not any("all_inputs" in item.value for item in app.warning)
    assert any("закончится" in item.value for item in app.warning)


def test_buyer_classification_requires_reason_and_creates_new_plan_only():
    harness = '''
import streamlit as st
from apps.buyer_ui.secondary_views import _render_event_review
_render_event_review(st.session_state["client"], st.session_state["run"], st.session_state["events"])
'''
    client = MockClient()
    original = client.get_proposal("demo-proposal-tools")
    calls = []

    def review(run_id, payload):
        calls.append((run_id, payload))
        return {"run_id": "reviewed-plan"}

    client.review_events = review
    app = AppTest.from_string(harness, default_timeout=15)
    app.session_state["client"] = client
    app.session_state["run"] = client.get_planning_run("demo-run")
    app.session_state["snapshot_id"] = "demo-snapshot"
    app.session_state["events"] = [
        {"event_id": "positive", "sku_id": "TOOL-001", "observed_qty": "1000", "review_status": "pending", "reason_codes": ["one_off_robust_size_outlier"]},
        {"event_id": "return", "sku_id": "TOOL-001", "observed_qty": "-5", "review_status": "pending", "reason_codes": ["DEMAND_DECREASE"]},
    ]
    app.run()
    clean(app)
    assert len(app.selectbox(key="review_event_selection").options) == 1
    next(button for button in app.button if button.label == "Подтвердить и пересчитать новый план").click().run()
    assert calls == []
    assert any("обоснование" in item.value for item in app.error)
    app.radio(key="review_event_label").set_value("project")
    app.text_area(key="review_event_reason").set_value("Подтверждён разовый строительный проект")
    next(button for button in app.button if button.label == "Подтвердить и пересчитать новый план").click().run()
    clean(app)
    assert len(calls) == 1
    assert calls[0][0] == "demo-run"
    assert calls[0][1]["event_ids"] == ["positive"]
    assert calls[0][1]["label"] == "project"
    assert calls[0][1]["idempotency_key"]
    assert app.session_state["buyer_flow"]["run_id"] == "reviewed-plan"
    assert client.get_proposal("demo-proposal-tools") == original


def test_reload_restores_confirmed_inputs_even_when_new_plan_did_not_start():
    harness = '''
import streamlit as st
from apps.buyer_ui.workspace import restore_workspace
restore_workspace(st.session_state["client"])
'''
    client = MockClient()
    workspace = client.workspace()
    workspace["latest_snapshot_id"] = "new-confirmed-buyer-snapshot"
    client.workspace = lambda: workspace
    app = AppTest.from_string(harness, default_timeout=15)
    app.session_state["client"] = client
    app.run()
    clean(app)
    assert app.session_state["snapshot_id"] == "new-confirmed-buyer-snapshot"
    assert app.session_state["run_id"] is None
    assert app.session_state["pending_page"] == "Данные"


def test_applying_scenario_preserves_confirmed_sale_classification():
    harness = '''
import streamlit as st
from apps.buyer_ui.secondary_views import render_scenarios
render_scenarios(st.session_state["client"])
'''
    client = MockClient()
    decisions = [{"event_ids": ["sale-001"], "label": "project", "reason": "Подтверждён разовый проект"}]
    client._fixture["run"]["review_overrides"] = decisions
    calls = []

    def create(payload):
        calls.append(payload)
        return {"run_id": "reviewed-scenario-plan"}

    client.create_planning_run = create
    app = AppTest.from_string(harness, default_timeout=15)
    for key, value in {"client": client, "snapshot_id": "demo-snapshot", "run_id": "demo-run", "client_mode": "mock"}.items():
        app.session_state[key] = value
    app.run()
    next(button for button in app.button if button.label == "Сравнить варианты").click().run()
    next(button for button in app.button if button.label == "Применить условия к новому плану").click().run()
    clean(app)
    assert calls[0]["review_overrides"] == decisions
    assert calls[0]["policy"]["service_target"] == 0.99
    assert app.session_state["buyer_flow"]["review_overrides"] == decisions
    assert client._fixture["run"]["review_overrides"] == decisions


def test_opened_run_retains_classification_decisions_in_context():
    harness = '''
import streamlit as st
from apps.buyer_ui.workspace import activate_run
activate_run(st.session_state["run"])
'''
    decisions = [{"event_ids": ["sale-001"], "label": "regular", "reason": "Регулярная закупка клиента"}]
    app = AppTest.from_string(harness, default_timeout=15)
    app.session_state["run"] = {"id": "saved-plan", "snapshot_id": "saved-snapshot", "review_overrides": decisions}
    app.run()
    clean(app)
    assert app.session_state["_secondary_run_contexts"]["saved-plan"]["review_overrides"] == decisions
