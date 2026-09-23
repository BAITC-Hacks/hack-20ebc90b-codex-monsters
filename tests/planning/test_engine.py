"""Independent numerical acceptance checks for inventory policy and constraints."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from math import sqrt
from statistics import NormalDist

import pytest

from ekt.contracts import (
    Assumption, Capabilities, DomainError, ForecastArtifact, ForecastDay, ForecastSeries,
    PlanningPolicy, QualityReport, SnapshotManifest, Uncertainty,
)
from ekt.contracts.io import load_table, write_table
from ekt.planning import build_proposals, revalidate_line_quantity

D = Decimal
ASOF = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)


def fixture(tmp_path, *, count=1, stock="0", moq="1", pack="1", conversion="1",
            quantum="1", lead=7, review=3, mean=10.0, std=0.0, horizon=90,
            pipeline=None, costs=None, lifecycle="active", forecasts=True):
    masters, stocks, terms, series = [], [], [], []
    for i in range(count):
        sku, supplier = f"SKU-{i}", f"SUP-{i}"
        masters.append(dict(sku_id=sku, name=sku, base_uom="m", purchase_uom="coil",
                            base_units_per_purchase_uom=conversion, quantity_quantum=quantum,
                            supplier_id=supplier, provenance="synthetic", lifecycle=lifecycle))
        stocks.append(dict(sku_id=sku, warehouse_id="WH", as_of=ASOF.isoformat(),
                           on_hand_base=str(D(stock) + D(15)), reserved_base="10", blocked_base="5",
                           free_base=stock, accounting_definition_version="net-v1", provenance="synthetic"))
        terms.append(dict(sku_id=sku, supplier_id=supplier, warehouse_id="WH", moq_purchase=moq,
                          pack_multiple_purchase=pack, lead_time_days=lead, review_days=review,
                          cost_per_base="2" if costs is None else costs[i], currency="KZT",
                          valid_at=ASOF.isoformat(), provenance="synthetic"))
        if forecasts:
            series.append(ForecastSeries(
                sku_id=sku, warehouse_id="WH", base_uom="m",
                daily=[ForecastDay(date=ASOF.date() + timedelta(days=day), baseline_mean=mean,
                                   seasonal_delta=0, growth_delta=0, mean=mean) for day in range(1, horizon + 1)],
                uncertainty=Uncertainty(method="iid_residual_normal", daily_residual_std=std,
                                        assumption_note="Independent residual fixture"),
                classification_policy="regular_only",
            ))
    quality = QualityReport(capabilities=Capabilities(can_plan=True, can_approve=True, can_export=True))
    tables = {name: write_table(tmp_path, name, rows) for name, rows in {
        "sku_master": masters, "inventory_snapshots": stocks, "supplier_terms": terms,
        "pipeline_lines": pipeline or [],
    }.items()}
    snapshot = SnapshotManifest(snapshot_id="s1", mapping_version="1", manifest_hash="abc",
                                mode="synthetic_demo", as_of=ASOF, created_at=ASOF, tables=tables, quality=quality)
    forecast = ForecastArtifact(forecast_id="f1", snapshot_id="s1", request_hash="req", model_version="fixture",
                                seed=42, as_of=ASOF, mode="synthetic_demo", series=series,
                                classifications_ref="test-only", corrected_demand_ref="test-only", quality=quality)
    return snapshot, forecast


def line_of(result):
    return result[0].lines[0]


def assert_ledger(line):
    assert sum((component.delta_base_qty for component in line.explanation), D(0)) == line.selected_base_qty
    assert line.selected_purchase_qty * line.conversion == line.selected_base_qty
    revalidate_line_quantity(line, line.selected_purchase_qty)


def incoming(quantity, day, *, semantics="deadline", status="in_transit"):
    return dict(po_line_id=f"P-{quantity}-{day}", sku_id="SKU-0", supplier_id="SUP-0", warehouse_id="WH",
                remaining_base_qty=str(quantity), eta_end=None if day is None else (ASOF + timedelta(days=day)).isoformat(),
                eta_semantics=semantics, status=status, provenance="synthetic")


def test_golden_108_decomposes_every_unit_and_does_not_double_subtract_reservations(tmp_path):
    snapshot, forecast = fixture(tmp_path, stock="40", moq="108", pack="12", pipeline=[incoming(20, 2)])
    original = forecast.series[0]
    payload = original.model_dump()
    payload["daily"] = [dict(date=day.date, baseline_mean=10, seasonal_delta=2, growth_delta=1, mean=13) for day in original.daily]
    paths = tmp_path / "golden.json"
    paths.write_text(json.dumps([dict(scenario_id=str(i), date=day.date.isoformat(), quantity=16)
                                for i in range(2) for day in original.daily]))
    payload["uncertainty"] = dict(method="scenario_paths", paths_ref=str(paths), path_count=2,
                                  calibration="unvalidated", assumption_note="Known fixture horizon quantile 160")
    forecast.series[0] = ForecastSeries.model_validate(payload)
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "r1"))
    assert line.raw_need == D(100)
    assert line.safety_stock == D(30)
    assert line.selected_base_qty == D(108)
    parts = {part.code: part.delta_base_qty for part in line.explanation}
    assert parts["baseline_demand"] == 100
    assert parts["seasonal_delta"] == 20
    assert parts["growth_delta"] == 10
    assert parts["free_stock"] == -40
    assert parts["eligible_pipeline"] == -20
    assert parts["moq_adjustment"] == 8
    assert_ledger(line)


def test_coil_and_base_quantum_intersection(tmp_path):
    snapshot, forecast = fixture(tmp_path, conversion="2.5", quantum="1", pack="1", mean=0.6)
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "r1"))
    assert line.raw_need == D(6)
    assert line.selected_base_qty == D(10)
    assert line.selected_purchase_qty == D(4)
    assert_ledger(line)
    with pytest.raises(DomainError, match="stock quantity quantum"):
        revalidate_line_quantity(line, "3")


def test_zero_need_stays_zero_despite_moq_and_pack(tmp_path):
    snapshot, forecast = fixture(tmp_path, stock="500", moq="108", pack="12")
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "r1"))
    assert line.raw_need == line.selected_base_qty == 0
    assert {part.code: part.delta_base_qty for part in line.explanation}["nonnegative_clip"] == 400
    assert_ledger(line)


def test_late_receipt_reduces_horizon_need_but_cannot_hide_early_shortage(tmp_path):
    snapshot, forecast = fixture(tmp_path, pipeline=[incoming(100, 9), incoming(500, None, semantics="unknown")])
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "r1"))
    assert line.selected_base_qty == 0
    assert line.projected_stockout_date == ASOF.date() + timedelta(days=1)
    assert line.urgency == "critical"
    assert {issue.code for issue in line.warnings} >= {"UNKNOWN_ETA_EXCLUDED", "PROJECTED_STOCKOUT"}
    assert_ledger(line)


@pytest.mark.parametrize("kwargs", [{"moq": None}, {"pack": None}, {"conversion": None}, {"lifecycle": "discontinued"}])
def test_missing_or_ineligible_terms_are_visible_exclusions(tmp_path, kwargs):
    snapshot, forecast = fixture(tmp_path, **kwargs)
    result = build_proposals(snapshot, forecast, PlanningPolicy(), "r1")
    assert not result[0].lines
    assert result[0].excluded_lines[0].sku_id == "SKU-0"
    assert not result[0].capabilities.can_approve


def test_explicit_zero_moq_means_no_supplier_minimum(tmp_path):
    snapshot, forecast = fixture(tmp_path, moq="0", pack="3", mean=0.1)
    assert line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "r1")).selected_base_qty == 3


def test_global_budget_is_not_reused_for_each_supplier(tmp_path):
    snapshot, forecast = fixture(tmp_path, count=2, pack="12", moq="24")
    proposals = build_proposals(snapshot, forecast, PlanningPolicy(budget_cap=D(250), currency="KZT"), "r1")
    assert len(proposals) == 2
    assert sum(proposal.total_cost for proposal in proposals) == D(216)
    quantities = [line.selected_base_qty for proposal in proposals for line in proposal.lines]
    assert quantities == [D(108), D(0)]
    assert all(any(issue.code == "FEASIBLE_HEURISTIC" for issue in proposal.warnings) for proposal in proposals)
    for proposal in proposals:
        for line in proposal.lines:
            assert_ledger(line)


def test_budget_missing_cost_fails_whole_cohort(tmp_path):
    snapshot, forecast = fixture(tmp_path, count=2, costs=["2", None])
    with pytest.raises(DomainError) as exc:
        build_proposals(snapshot, forecast, PlanningPolicy(budget_cap=D(250), currency="KZT"), "r1")
    assert exc.value.code == "UNSUPPORTED_BUDGET"


def test_horizon_not_silently_truncated(tmp_path):
    snapshot, forecast = fixture(tmp_path, horizon=9)
    with pytest.raises(DomainError) as exc:
        build_proposals(snapshot, forecast, PlanningPolicy(), "r1")
    assert exc.value.code == "HORIZON_TOO_SHORT"


def test_normal_safety_stock_and_unconstrained_target_monotonicity(tmp_path):
    snapshot, forecast = fixture(tmp_path, std=4)
    low = line_of(build_proposals(snapshot, forecast, PlanningPolicy(service_target=.95), "low"))
    high = line_of(build_proposals(snapshot, forecast, PlanningPolicy(service_target=.99), "high"))
    assert float(low.safety_stock) == pytest.approx(4 * sqrt(10) * NormalDist().inv_cdf(.95))
    assert float(low.rop) == pytest.approx(70 + 4 * sqrt(7) * NormalDist().inv_cdf(.95))
    assert high.raw_need >= low.raw_need
    assert_ledger(low)
    assert_ledger(high)


def test_scenario_quantile_preserves_cross_day_dependence(tmp_path):
    snapshot, forecast = fixture(tmp_path, lead=1, review=1, horizon=2)
    paths = tmp_path / "correlated.json"
    rows = [dict(scenario_id=str(i), date=day.date.isoformat(), quantity=quantities[j])
            for i, quantities in enumerate([[0, 20], [20, 0]]) for j, day in enumerate(forecast.series[0].daily)]
    paths.write_text(json.dumps(rows))
    forecast.series[0].uncertainty = Uncertainty(method="scenario_paths", paths_ref=str(paths), path_count=2,
                                               assumption_note="Each two-day demand path totals exactly 20")
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "r1"))
    assert line.safety_stock == 0
    assert line.selected_base_qty == 20


def test_same_day_future_stock_is_not_used(tmp_path):
    snapshot, forecast = fixture(tmp_path)
    future = dict(sku_id="SKU-0", warehouse_id="WH", as_of=(ASOF + timedelta(hours=1)).isoformat(),
                  on_hand_base="999", reserved_base="0", blocked_base="0", free_base="999",
                  accounting_definition_version="net-v1", provenance="synthetic")
    snapshot.tables["inventory_snapshots"] = write_table(tmp_path, "future_inventory", [future])
    result = build_proposals(snapshot, forecast, PlanningPolicy(), "r1")
    assert not result[0].lines
    assert any(issue.code == "MISSING_STOCK" for issue in result[0].excluded_lines[0].reasons)


def test_missing_forecast_does_not_disappear(tmp_path):
    snapshot, forecast = fixture(tmp_path, forecasts=False)
    result = build_proposals(snapshot, forecast, PlanningPolicy(), "r1")
    assert result[0].excluded_lines[0].reasons[0].code == "MISSING_FORECAST"


def test_assumptions_never_relabel_real_preview_as_approvable_demo(tmp_path):
    snapshot, forecast = fixture(tmp_path)
    snapshot.mode = forecast.mode = "real_preview"
    snapshot.quality.capabilities.can_approve = False
    snapshot.assumptions = [Assumption(
        field="demand_granularity", value="monthly", provenance="derived",
        reason="Daily demand inferred from monthly observations",
    )]
    proposals = build_proposals(snapshot, forecast, PlanningPolicy(), "real")
    assert proposals[0].mode == "real_preview"
    assert not proposals[0].capabilities.can_approve
    assert not proposals[0].capabilities.can_export
    assert {issue.code for issue in proposals[0].lines[0].warnings} >= {
        "PLANNING_ASSUMPTION", "ASSUMED_PLANNING_INPUT",
    }


def test_buyer_prepared_real_snapshot_can_be_approved_without_becoming_demo(tmp_path):
    snapshot, forecast = fixture(tmp_path)
    snapshot.mode = forecast.mode = "real_preview"
    snapshot.assumptions = [Assumption(
        field="buyer_preparation", value="confirmed", provenance="override",
        reason="Buyer checked purchase terms and accepted source coverage for local use",
    )]
    proposal = build_proposals(snapshot, forecast, PlanningPolicy(), "prepared")[0]
    assert proposal.mode == "real_preview"
    assert proposal.capabilities.can_approve
    assert not proposal.capabilities.can_export
    assert {warning.code for warning in proposal.lines[0].warnings} >= {
        "PLANNING_ASSUMPTION", "ASSUMED_PLANNING_INPUT",
    }
    assert not any("ERP" in reason for reason in proposal.capabilities.reasons)


@pytest.mark.parametrize("table,field,replacement,error_code", [
    ("inventory_snapshots", "free_base", "40", "AMBIGUOUS_STOCK"),
    ("supplier_terms", "lead_time_days", 14, "AMBIGUOUS_TERMS"),
])
def test_conflicting_effective_records_do_not_choose_an_arbitrary_order(
    tmp_path, table, field, replacement, error_code,
):
    snapshot, forecast = fixture(tmp_path, count=2)
    rows = load_table(snapshot, table)
    conflict = {**rows[0], field: replacement}
    if table == "inventory_snapshots":
        conflict["on_hand_base"] = "55"
    original = [*rows, conflict]
    for records in (original, list(reversed(original))):
        snapshot.tables[table] = write_table(tmp_path, table, records)
        proposals = build_proposals(snapshot, forecast, PlanningPolicy(), "conflicting")
        assert [line.sku_id for proposal in proposals for line in proposal.lines] == ["SKU-1"]
        assert any(
            issue.code == error_code
            for proposal in proposals for excluded in proposal.excluded_lines
            if excluded.sku_id == "SKU-0" for issue in excluded.reasons
        )


@pytest.mark.parametrize("table,timestamp", [
    ("inventory_snapshots", "as_of"), ("supplier_terms", "valid_at"),
])
def test_identical_effective_records_do_not_prevent_replenishment(tmp_path, table, timestamp):
    snapshot, forecast = fixture(tmp_path)
    rows = load_table(snapshot, table)
    duplicate = {**rows[0], timestamp: "2026-09-01T17:00:00+05:00"}
    snapshot.tables[table] = write_table(tmp_path, table, [*rows, duplicate])
    proposal = build_proposals(snapshot, forecast, PlanningPolicy(), "duplicate-record")[0]
    assert not proposal.excluded_lines
    assert proposal.lines[0].selected_base_qty == 100
    assert_ledger(proposal.lines[0])


def test_forecast_cannot_change_snapshot_data_mode(tmp_path):
    snapshot, forecast = fixture(tmp_path)
    snapshot.mode = "real_preview"
    with pytest.raises(DomainError) as exc:
        build_proposals(snapshot, forecast, PlanningPolicy(), "mismatched")
    assert exc.value.code == "SNAPSHOT_MISMATCH"


def test_duplicate_receipt_cannot_reduce_replenishment_twice(tmp_path):
    receipt = incoming(50, 2)
    snapshot, forecast = fixture(tmp_path, pipeline=[receipt, receipt])
    with pytest.raises(DomainError) as exc:
        build_proposals(snapshot, forecast, PlanningPolicy(), "duplicate")
    assert exc.value.code == "DUPLICATE_PIPELINE"


@pytest.mark.parametrize("receipt_day,receipt_qty,cover_days,expected", [
    (9, 30, 5, 50),  # Late receipt cannot reduce the earlier maximum-cover cap.
    (15, 180, 20, 20),  # Cover cap also sees receipts beyond L + R.
])
def test_maximum_cover_uses_receipts_within_its_own_dated_horizon(
    tmp_path, receipt_day, receipt_qty, cover_days, expected,
):
    snapshot, forecast = fixture(tmp_path, pipeline=[incoming(receipt_qty, receipt_day)])
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(max_cover_days=cover_days), "cover"))
    assert line.selected_base_qty == expected
    assert_ledger(line)


def test_no_order_is_a_valid_calculation_but_not_an_approvable_purchase(tmp_path):
    snapshot, forecast = fixture(tmp_path, stock="500")
    proposal = build_proposals(snapshot, forecast, PlanningPolicy(), "no-order")[0]
    assert proposal.capabilities.can_plan
    assert not proposal.capabilities.can_approve
    assert not proposal.capabilities.can_export
    assert any("Нет товаров к заказу" in reason for reason in proposal.capabilities.reasons)


def test_draft_is_never_exportable_even_when_approvable(tmp_path):
    snapshot, forecast = fixture(tmp_path)
    proposal = build_proposals(snapshot, forecast, PlanningPolicy(), "draft")[0]
    assert proposal.capabilities.can_approve
    assert not proposal.capabilities.can_export


def test_unknown_cost_stays_unknown_and_is_visible_to_buyer(tmp_path):
    snapshot, forecast = fixture(tmp_path, costs=[None])
    proposal = build_proposals(snapshot, forecast, PlanningPolicy(), "unknown-cost")[0]
    assert proposal.total_cost is None
    assert proposal.lines[0].line_cost is None
    assert not proposal.capabilities.budget_available
    assert "UNKNOWN_ORDER_COST" in {issue.code for issue in proposal.lines[0].warnings}


def test_future_receipt_later_on_snapshot_day_covers_the_first_forecast_day(tmp_path):
    receipt = incoming(100, 0)
    receipt["eta_end"] = (ASOF + timedelta(hours=4)).isoformat()
    snapshot, forecast = fixture(tmp_path, pipeline=[receipt])
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "same-day"))
    assert line.selected_base_qty == 0
    assert line.projected_stockout_date is None
    assert "PAST_DUE_PIPELINE_EXCLUDED" not in {issue.code for issue in line.warnings}
    assert_ledger(line)


def test_receipt_calendar_date_uses_forecast_utc_timezone(tmp_path):
    receipt = incoming(100, 1)
    # September 3 in the supplier's zone is the first forecast day in UTC.
    receipt["eta_end"] = "2026-09-03T00:30:00+05:00"
    snapshot, forecast = fixture(tmp_path, pipeline=[receipt])
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "timezone"))
    assert line.selected_base_qty == 0
    assert line.projected_stockout_date is None
    assert_ledger(line)


def test_snapshot_offset_cannot_shift_forecast_or_receipt_calendar(tmp_path):
    snapshot, forecast = fixture(tmp_path, lead=1, review=1, horizon=2,
                                 pipeline=[incoming(20, 1)])
    # The same instant falls on September 2 in +14 but September 1 in UTC.
    # B's UTC forecast covers September 2 and 3, exactly the protection window.
    snapshot.as_of = ASOF.astimezone(timezone(timedelta(hours=14)))
    forecast.as_of = snapshot.as_of
    line = line_of(build_proposals(snapshot, forecast, PlanningPolicy(), "offset-calendar"))
    assert line.selected_base_qty == 0
    assert line.projected_stockout_date is None
    assert_ledger(line)
