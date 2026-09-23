"""A buyer can complete real inputs without inventing missing values or writing IDs."""
from copy import deepcopy

import pytest
from streamlit.testing.v1 import AppTest

from apps.buyer_ui.client import ApiError, MockClient
from apps.buyer_ui.procurement_inputs import editable_rows, fill_common, input_payload
from ekt.contracts.models import BuyerPreparationRequest


ITEM = {
    "sku_id": "SKU-001", "warehouse_id": "MAIN", "name": "Кабель", "supplier_id": "SUP-1", "base_uom": "m",
    "purchase_uom": "coil", "base_units_per_purchase_uom": "100", "quantity_quantum": "0.001",
    "free_base": "100.000000000000000001", "moq_purchase": "1", "pack_multiple_purchase": "1",
    "lead_time_days": 7, "review_days": 7, "cost_per_base": None, "currency": None,
    "incoming_base_qty": "0", "incoming_eta": None,
}


def test_real_inputs_preserve_unknowns_decimal_precision_and_contract():
    rows = editable_rows([ITEM])
    assert rows[0]["cost_per_base"] == ""
    assert rows[0]["free_base"] == "100.000000000000000001"
    rows[0]["moq_purchase"] = "1,5"
    payload = input_payload(rows, "Сверено с поставщиком", True)
    payload["idempotency_key"] = "buyer-input-1"
    model = BuyerPreparationRequest.model_validate(payload)
    assert str(model.items[0].free_base) == ITEM["free_base"]
    assert str(model.items[0].moq_purchase) == "1.5"
    assert model.items[0].cost_per_base is None


@pytest.mark.parametrize("field", ["free_base", "moq_purchase", "base_units_per_purchase_uom", "lead_time_days"])
def test_missing_required_values_are_not_replaced_by_zero_or_one(field):
    rows = editable_rows([{**ITEM, field: None}])
    with pytest.raises(ValueError, match="заполните"):
        input_payload(rows, "Проверено", True)


@pytest.mark.parametrize("reason,accepted", [("", True), ("  ", True), ("Проверено", False)])
def test_inputs_require_human_reason_and_explicit_history_assumption(reason, accepted):
    with pytest.raises(ValueError):
        input_payload(editable_rows([ITEM]), reason, accepted)


@pytest.mark.parametrize("value", ["NaN", "Infinity", "2.5"])
def test_delivery_days_are_finite_whole_numbers(value):
    with pytest.raises(ValueError, match="число целых дней"):
        input_payload(editable_rows([{**ITEM, "lead_time_days": value}]), "Проверено", True)


class BuyerInputsClient:
    def __init__(self):
        self.saved = []
        self.started = []
        self.fail = False

    def get_buyer_inputs(self, snapshot_id):
        return {"snapshot_id": snapshot_id, "items": [deepcopy(ITEM)],
                "history_start": "2026-01-01", "history_end": "2026-09-22"}

    def save_buyer_inputs(self, snapshot_id, payload):
        self.saved.append((snapshot_id, payload))
        if self.fail:
            raise ApiError(None, "TIMEOUT", "Ответ не получен", ambiguous=True)
        return {"snapshot_id": "confirmed-real", "mode": "real_preview"}

    def create_planning_run(self, payload):
        self.started.append(payload)
        return {"run_id": "new-real-run"}


HARNESS = '''
import streamlit as st
from apps.buyer_ui.procurement_inputs import render_procurement_inputs
render_procurement_inputs(st.session_state["client"], {"snapshot_id": "original-real", "mode": "real_preview", "quality": {"capabilities": {"can_plan": False}}})
'''


def prepared_app(client):
    app = AppTest.from_string(HARNESS, default_timeout=15)
    app.session_state["client"] = client
    app.session_state["buyer_pending_policy"] = {"service_target": 0.99, "service_metric": "cycle_service"}
    app.run()
    return app


def submit(app):
    next(button for button in app.button if button.label == "Сохранить условия и рассчитать заказ").click().run()


def test_form_saves_separate_snapshot_and_continues_chosen_policy():
    client = BuyerInputsClient()
    app = prepared_app(client)
    submit(app)
    assert not client.saved
    assert any("Подтвердите" in item.value for item in app.error)
    app.checkbox(key="buyer_input_accept:original-real").check()
    app.text_area(key="buyer_input_reason:original-real").set_value("Проверено по учёту склада")
    submit(app)
    assert not app.exception
    assert not app.code and not app.json
    assert len(client.saved) == len(client.started) == 1
    assert app.session_state["snapshot_id"] == "confirmed-real"
    assert app.session_state["buyer_flow"]["run_id"] == "new-real-run"
    assert client.started[0]["policy"]["service_target"] == 0.99
    assert client.saved[0][1]["items"][0]["free_base"] == ITEM["free_base"]


def test_timeout_preserves_input_and_reuses_idempotency_key():
    client = BuyerInputsClient()
    client.fail = True
    app = prepared_app(client)
    app.checkbox(key="buyer_input_accept:original-real").check()
    app.text_area(key="buyer_input_reason:original-real").set_value("Проверено по учёту склада")
    submit(app)
    assert not app.exception
    assert app.text_area(key="buyer_input_reason:original-real").value == "Проверено по учёту склада"
    assert not client.started
    submit(app)
    assert len(client.saved) == 2
    assert client.saved[0][1] == client.saved[1][1]


def test_real_approval_requires_explicit_confirmation_and_sends_it_to_server():
    harness = '''
import streamlit as st
from apps.buyer_ui.views import _approval_and_export
p = st.session_state["client"].get_proposal("demo-proposal-tools")
_approval_and_export(st.session_state["client"], p, p)
'''
    client = MockClient()
    client._proposals["demo-proposal-tools"]["mode"] = "real_preview"
    calls = []
    original = client.approve_proposal

    def approve(proposal_id, payload):
        calls.append(payload)
        return original(proposal_id, payload)

    client.approve_proposal = approve
    app = AppTest.from_string(harness, default_timeout=15)
    app.session_state["client"] = client
    app.run()
    next(button for button in app.button if button.label == "Утвердить и подготовить CSV").click().run()
    assert not calls
    assert any("Подтвердите проверку" in item.value for item in app.error)
    app.checkbox[0].check()
    next(button for button in app.button if button.label == "Утвердить и подготовить CSV").click().run()
    assert not app.exception
    assert len(calls) == 1 and calls[0]["acknowledge_assumptions"] is True


def test_common_terms_fill_only_empty_cells_and_never_invent_stock():
    rows = editable_rows([{**ITEM, "moq_purchase": None, "free_base": None}])
    filled = fill_common(rows, {"moq_purchase": "5", "lead_time_days": "14", "review_days": ""})
    assert filled[0]["moq_purchase"] == "5"
    assert filled[0]["lead_time_days"] == "7"
    assert filled[0]["free_base"] == ""
    assert rows[0]["moq_purchase"] == ""


def test_large_catalog_uses_all_scope_without_hundreds_of_selected_chips():
    client = BuyerInputsClient()
    client.get_buyer_inputs = lambda snapshot_id: {"items": [{**ITEM, "sku_id": f"SKU-{i}"} for i in range(565)]}
    app = prepared_app(client)
    assert not app.exception
    assert not app.multiselect
    assert app.radio(key="buyer_input_scope:original-real").options == ["Все товары (565)", "Выбрать товары"]
    app.radio(key="buyer_input_scope:original-real").set_value("selected").run()
    assert not app.exception
    assert app.multiselect(key="buyer_input_selection:original-real").value == []


def test_new_assumption_and_missing_field_messages_are_readable():
    harness = '''
from apps.buyer_ui.formatting import show_issues, show_error_details
show_issues([{"code": "BUYER_DEMAND_COVERAGE_ASSUMPTION", "message": "Days without imported movements", "severity": "warning"},
             {"code": "BUYER_INPUTS_CONFIRMED", "message": "Internal explanation", "severity": "warning"}])
show_error_details({"sku_id": "SKU-001", "fields": ["free_base", "pack_multiple_purchase"]})
'''
    app = AppTest.from_string(harness, default_timeout=15).run()
    assert not app.exception
    assert any("дни без записей" in item.value for item in app.warning)
    assert any("доступный остаток, кратность упаковки" in item.value for item in app.warning)
    assert all("Days without" not in item.value and "free_base" not in item.value for item in app.warning)


def test_real_import_quality_does_not_leak_codes_or_english_rejection_messages():
    harness = '''
import streamlit as st
from apps.buyer_ui.formatting import show_quality
show_quality(st.session_state["quality"])
'''
    app = AppTest.from_string(harness, default_timeout=15)
    issues = [
        {"code": "INCOMPLETE_DEMAND_COVERAGE", "message": "History not complete", "severity": "warning"},
        {"code": "METADATA_ONLY_SOURCE", "message": "Metadata only", "severity": "info"},
        {"code": "MISSING_CURRENT_STOCK", "message": "Stock not verified", "severity": "blocking"},
        {"code": "UNSUPPORTED_SIGNED_MOVEMENT", "message": "Movement type and sign do not match one confirmed rule", "severity": "warning"},
        {"code": "MISSING_QUANTITY", "message": "Missing quantity_base", "severity": "warning"},
    ]
    app.session_state["quality"] = {
        "status": "blocked", "accepted_rows": 1127, "rejected_rows": 17,
        "issues": issues,
        "capabilities": {"can_plan": False, "reasons": [issue["code"] for issue in issues] + ["MISSING_SUPPLIER_TERMS", "UNKNOWN_NEW_DIAGNOSTIC", "Дополните условия закупки."]},
    }
    app.run()
    assert not app.exception
    visible = "\n".join(item.value for elements in (app.caption, app.warning, app.error, app.info) for item in elements)
    assert "Возвраты и движения с неоднозначным знаком исключены" in visible
    assert "В части исходных строк не указано количество" in visible
    assert "Для заказа нужны условия поставщика" in visible
    assert "Есть дополнительное замечание к данным" in visible
    assert "Дополните условия закупки." in visible
    assert visible.count("Нет подтверждённого текущего остатка") == 1
    for raw in [issue["code"] for issue in issues] + ["UNKNOWN_NEW_DIAGNOSTIC", "Movement type", "quantity_base"]:
        assert raw not in visible


def test_combined_reason_codes_are_deduplicated_and_unknown_code_is_humanized():
    from apps.buyer_ui.formatting import human_reasons
    messages = human_reasons("MISSING_CURRENT_STOCK MISSING_SUPPLIER_TERMS UNKNOWN_NEW_DIAGNOSTIC UNKNOWN_OTHER", [{"code": "MISSING_CURRENT_STOCK"}])
    assert len(messages) == 2
    assert messages[0].startswith("Для заказа нужны условия поставщика")
    assert messages[1].startswith("Есть дополнительное замечание")
