from datetime import datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ekt.contracts import (
    ForecastDay, InventorySnapshot, PlanningPolicy, ProposalLine, ProposalPatchRequest,
    SalesEvent, StockoutInterval, Uncertainty,
)


def proposal_line():
    return dict(
        line_id="l1", sku_id="0001", name="Cable", base_uom="m", purchase_uom="coil",
        recommended_base_qty="200", recommended_purchase_qty="2", selected_purchase_qty="2",
        selected_base_qty="200", moq_purchase="2", pack_multiple_purchase="1", conversion="100",
        quantity_quantum="0.1", rop="100", safety_stock="20", raw_need="180", unit_cost="3.25",
        line_cost="650.00", urgency="soon", explanation=[
            dict(code="need", label="Need", delta_base_qty="180"),
            dict(code="pack", label="Round to coil", delta_base_qty="20"),
        ],
    )


def test_decimal_money_and_quantities_roundtrip_without_binary_float():
    line = ProposalLine.model_validate(proposal_line())
    payload = line.model_dump(mode="json")
    assert payload["sku_id"] == "0001"
    assert payload["line_cost"] == "650.00"
    assert payload["selected_base_qty"] == "200"
    assert ProposalLine.model_validate_json(line.model_dump_json()).line_cost == Decimal("650")


@pytest.mark.parametrize("field,value", [
    ("selected_base_qty", "201"), ("selected_purchase_qty", "3"),
    ("line_cost", "649.99"), ("pack_multiple_purchase", "0"), ("conversion", "0"),
])
def test_purchase_units_and_auditable_explanation_cannot_diverge(field, value):
    payload = proposal_line()
    payload[field] = value
    with pytest.raises(ValidationError):
        ProposalLine.model_validate(payload)


def test_moq_and_pack_are_separate_constraints_and_zero_order_allowed():
    payload = proposal_line()
    payload.update(selected_purchase_qty="0", selected_base_qty="0", line_cost="0")
    payload["explanation"] = [dict(code="budget", label="Deferred", delta_base_qty="0")]
    assert ProposalLine.model_validate(payload).selected_purchase_qty == 0
    payload.update(selected_purchase_qty="1", selected_base_qty="100", line_cost="325")
    payload["explanation"][0]["delta_base_qty"] = "100"
    with pytest.raises(ValidationError, match="MOQ"):
        ProposalLine.model_validate(payload)


@pytest.mark.parametrize("target", [0, 1, 95, float("nan"), float("inf")])
def test_invalid_service_targets_rejected(target):
    with pytest.raises(ValidationError):
        PlanningPolicy(service_target=target)


def test_budget_needs_currency_and_positive_limits():
    with pytest.raises(ValidationError, match="currency"):
        PlanningPolicy(budget_cap="100")
    with pytest.raises(ValidationError):
        PlanningPolicy(budget_cap="-1", currency="KZT")


def test_inventory_accounting_must_reconcile():
    with pytest.raises(ValidationError, match="free_base"):
        InventorySnapshot(sku_id="1", warehouse_id="A", as_of=datetime.now(timezone.utc),
            on_hand_base="10", reserved_base="3", blocked_base="2", free_base="10",
            accounting_definition_version="v1", provenance="observed")


def test_timestamps_require_timezone_and_zero_stockout_interval_rejected():
    payload = dict(event_id="1", source_id="s", row_ref="1", doc_id="1", event_at="2026-09-23T10:00:00",
        sku_id="1", warehouse_id="A", event_type="sale", quantity_base="1", demand_effect="increase", provenance="observed")
    with pytest.raises(ValidationError):
        SalesEvent.model_validate(payload)
    with pytest.raises(ValidationError):
        StockoutInterval(sku_id="1", warehouse_id="A", start_at="2026-09-23T00:00:00Z",
            end_at="2026-09-23T00:00:00Z", unavailable_fraction=1, evidence="observed", confidence=1)


def test_forecast_components_must_reconcile_and_uncertainty_be_explicit():
    with pytest.raises(ValidationError):
        ForecastDay(date="2026-09-24", baseline_mean=10, seasonal_delta=3, growth_delta=2, mean=10)
    with pytest.raises(ValidationError, match="daily_residual_std"):
        Uncertainty(method="iid_residual_normal", assumption_note="Assumed")
    with pytest.raises(ValidationError, match="paths_ref"):
        Uncertainty(method="scenario_paths", assumption_note="Assumed")


def test_duplicate_line_edits_rejected_before_mutation():
    with pytest.raises(ValidationError, match="duplicate"):
        ProposalPatchRequest(expected_version=1, reason="Buyer edit", edits=[
            {"line_id": "same", "purchase_qty": "10"}, {"line_id": "same", "purchase_qty": "20"},
        ])
