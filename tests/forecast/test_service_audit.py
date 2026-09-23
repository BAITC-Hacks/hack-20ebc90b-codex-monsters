"""Independent service checks using real immutable snapshots and Parquet IO.

Every input below is invented test data. No copied runtime contract or shared
fixture implementation is needed to exercise the actual internal boundary.
"""

from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
import json
from pathlib import Path

from ekt.data.artifacts import read_table
from ekt.data.snapshot import build_snapshot_payload
from ekt.forecast.service import build_forecast_payload


AS_OF = "2026-03-31T23:59:59+00:00"
SNAPSHOT_AS_OF = "2026-04-30T23:59:59+00:00"


def fixture_inputs(tmp_path, sku_ids=("covered",)):
    """Complete local rows, with dated demand coverage asserted separately."""
    masters, events, inventory, terms = [], [], [], []
    for sku in sku_ids:
        masters.append({
            "sku_id": sku, "name": f"Invented audit {sku}", "category_id": "audit",
            "supplier_id": "test-supplier", "base_uom": "piece", "purchase_uom": "piece",
            "base_units_per_purchase_uom": "1", "quantity_quantum": "1",
        })
        for index in range(90):
            day = date(2026, 1, 1) + timedelta(days=index)
            events.append({
                "event_id": f"{sku}-{index}", "sku_id": sku, "warehouse_id": "W",
                "event_at": day.isoformat() + "T12:00:00+00:00", "quantity_base": "10",
                "event_type": "shipment", "demand_effect": "increase", "base_uom": "piece",
            })
        inventory.append({
            "sku_id": sku, "warehouse_id": "W", "as_of": AS_OF,
            "accounting_definition_version": "audit-confirmed", "on_hand_base": "10",
            "reserved_base": "0", "blocked_base": "0", "free_base": "10",
        })
        terms.append({
            "sku_id": sku, "warehouse_id": "W", "supplier_id": "test-supplier", "valid_at": AS_OF,
            "moq_purchase": "1", "pack_multiple_purchase": "1", "lead_time_days": 5, "review_days": 5,
        })
    sources = [
        {"source_id": "master", "kind": "sku_master", "rows": masters},
        {"source_id": "sales", "kind": "sales_events", "rows": events,
         "coverage": {"start": "2026-01-01", "end": "2026-05-01", "complete": True,
                      "sku_ids": list(sku_ids), "warehouse_ids": ["W"]}},
        {"source_id": "stock", "kind": "inventory_snapshots", "rows": inventory},
        {"source_id": "terms", "kind": "supplier_terms", "rows": terms},
    ]
    manifest = {"as_of": SNAPSHOT_AS_OF, "mode": "real_preview",
                "artifact_root": str(tmp_path / "artifacts"), "sources": sources}
    mapping = {"version": "audit-v1", "sources": {"stock": {"current_stock_verified": True}}}
    return manifest, mapping


def request(**overrides):
    return {"run_id": "audit-run", "as_of": AS_OF, "horizon_days": 90, "seed": 17,
            "sku_ids": [], "warehouse_ids": ["W"], "growth_overrides": [], "review_overrides": [],
            **overrides}


def forecast(manifest, mapping, **overrides):
    snapshot = build_snapshot_payload(manifest, mapping)
    return snapshot, build_forecast_payload(snapshot, request(**overrides))


def corrected_rows(artifact):
    directory = Path(artifact["corrected_demand_ref"]).parent
    refs = json.loads((directory / "artifact_refs.json").read_text())
    return read_table(refs["corrected_demand"])


def test_future_records_do_not_change_forecast_at_earlier_origin(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    baseline_snapshot, baseline = forecast(manifest, mapping)
    changed = deepcopy(manifest)
    changed["sources"][1]["rows"] += [{
        "event_id": "future-spike", "sku_id": "covered", "warehouse_id": "W",
        "event_at": "2026-04-02T12:00:00+00:00", "quantity_base": "1000000",
        "event_type": "shipment", "demand_effect": "increase", "base_uom": "piece",
    }]
    changed["sources"].append({"source_id": "future-stockout", "kind": "stockout_intervals", "rows": [{
        "sku_id": "covered", "warehouse_id": "W", "start_at": "2026-04-02T00:00:00+00:00",
        "end_at": "2026-04-05T00:00:00+00:00", "unavailable_fraction": 1,
        "confidence": 1, "evidence": "observed",
    }]})
    changed_snapshot, after = forecast(changed, mapping)
    assert baseline_snapshot["snapshot_id"] != changed_snapshot["snapshot_id"]
    assert len(read_table(changed_snapshot["tables"]["sales_events"])) == 91
    assert after["series"] == baseline["series"]
    assert corrected_rows(after) == corrected_rows(baseline)


def test_unfinished_replay_day_does_not_train_the_daily_model(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    for row in manifest["sources"][1]["rows"]:
        if row["event_at"].startswith("2026-03-31"):
            row["event_at"] = "2026-03-31T09:00:00+00:00"
            row["quantity_base"] = "999999"
    _, artifact = forecast(manifest, mapping, as_of="2026-03-31T12:00:00+00:00")
    assert max(row["date"] for row in corrected_rows(artifact)) == "2026-03-30"
    assert {day["mean"] for day in artifact["series"][0]["daily"]} == {10.0}
    assert artifact["series"][0]["daily"][0]["date"] == "2026-04-01"


def test_run_id_only_replay_reuses_artifacts_and_business_identity(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    snapshot, first = forecast(manifest, mapping)
    second = build_forecast_payload(snapshot, request(run_id="another-coordinator-run"))
    assert second == first
    assert corrected_rows(second) == corrected_rows(first)
    directory = Path(first["corrected_demand_ref"]).parent
    assert (directory / "forecast.json").is_file()
    assert not list(directory.glob(".manifest-*"))
    assert not list(directory.glob(".parquet-*"))


def test_complete_zero_sales_scope_is_retained(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path, ("covered", "known-zero"))
    manifest["sources"][1]["rows"] = [row for row in manifest["sources"][1]["rows"] if row["sku_id"] != "known-zero"]
    _, artifact = forecast(manifest, mapping)
    series = {item["sku_id"]: item for item in artifact["series"]}
    assert set(series) == {"covered", "known-zero"}
    assert len(series["known-zero"]["daily"]) == 90
    assert {day["mean"] for day in series["known-zero"]["daily"]} == {0.0}
    zero_rows = [row for row in corrected_rows(artifact) if row["sku_id"] == "known-zero"]
    assert len(zero_rows) == 90
    assert {row["observed_regular"] for row in zero_rows} == {Decimal(0)}


def test_request_scope_excludes_other_skus_and_warehouses(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path, ("covered", "excluded"))
    _, selected = forecast(manifest, mapping, sku_ids=["covered"])
    assert [item["sku_id"] for item in selected["series"]] == ["covered"]
    _, no_warehouse = forecast(manifest, mapping, warehouse_ids=["absent"])
    assert no_warehouse["series"] == []
    assert no_warehouse["quality"]["status"] == "blocked"
    assert not no_warehouse["quality"]["capabilities"]["can_plan"]


def test_blocked_selected_scope_cannot_inherit_other_skus_readiness(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path, ("covered", "blocked"))
    for row in manifest["sources"][3]["rows"]:
        if row["sku_id"] == "blocked":
            row.pop("moq_purchase")
    snapshot, artifact = forecast(manifest, mapping, sku_ids=["blocked"])
    assert snapshot["quality"]["capabilities"]["can_plan"]
    assert [item["sku_id"] for item in artifact["series"]] == ["blocked"]
    assert not artifact["quality"]["capabilities"]["can_plan"]


def test_coverage_gap_is_not_filled_with_zero_demand(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    manifest["sources"][1]["coverage"].update(start="2026-01-01", end="2026-01-11")
    mapping["assumptions"] = [{
        "field": "demand_coverage", "provenance": "observed", "reason": "Second verified extraction",
        "scope_ids": ["covered"], "value": {"start": "2026-03-01", "end": "2026-03-11",
        "complete": True, "sku_ids": ["covered"], "warehouse_ids": ["W"]},
    }]
    _, artifact = forecast(manifest, mapping)
    rows = corrected_rows(artifact)
    assert len(rows) == 10
    assert rows[0]["date"] == "2026-03-01"
    assert rows[-1]["date"] == "2026-03-10"
    assert artifact["series"][0]["observed_regular_total"] == Decimal("100")
    assert any(issue["code"] == "STALE_DEMAND_COVERAGE" for issue in artifact["series"][0]["warnings"])


def test_partial_first_coverage_day_is_not_treated_as_complete(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    manifest["sources"][1]["coverage"].update(
        start="2026-03-01T12:00:00+00:00", end="2026-03-11T12:00:00+00:00",
    )
    _, artifact = forecast(manifest, mapping)
    rows = corrected_rows(artifact)
    # UTC daily totals are known only for dates wholly inside [start,end).
    assert rows[0]["date"] == "2026-03-02"
    assert rows[-1]["date"] == "2026-03-10"
    assert len(rows) == 9


def test_unknown_coverage_preview_keeps_unobserved_dates_unknown(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    manifest["sources"][1].pop("coverage")
    manifest["sources"][1]["rows"] = manifest["sources"][1]["rows"][::2]
    _, artifact = forecast(manifest, mapping)
    assert len(artifact["series"]) == 1
    rows = corrected_rows(artifact)
    assert any(row["observed_regular"] is None for row in rows)
    assert all(row["corrected_demand"] is None for row in rows if row["observed_regular"] is None)
    assert {day["mean"] for day in artifact["series"][0]["daily"]} == {10.0}
    assert not artifact["quality"]["capabilities"]["can_plan"]
    assert any(issue["code"] == "UNKNOWN_DEMAND_COVERAGE" and issue["severity"] == "blocking"
               for issue in artifact["series"][0]["warnings"])


def test_one_unknown_scope_does_not_disable_covered_usable_subset(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path, ("covered", "unknown"))
    manifest["sources"][1]["coverage"]["sku_ids"] = ["covered"]
    snapshot, artifact = forecast(manifest, mapping)
    assert snapshot["quality"]["capabilities"]["can_plan"]
    series = {item["sku_id"]: item for item in artifact["series"]}
    assert any(issue["code"] == "UNKNOWN_DEMAND_COVERAGE" and issue["severity"] == "blocking"
               for issue in series["unknown"]["warnings"])
    assert not any(issue["severity"] == "blocking" for issue in series["covered"]["warnings"])
    assert artifact["quality"]["capabilities"]["can_plan"]


def test_stockout_coverage_assumption_does_not_escape_its_scope(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path, ("covered", "unlogged"))
    mapping["assumptions"] = [{
        "field": "stockout_coverage", "provenance": "observed", "reason": "Only covered SKU has logs",
        "scope_ids": ["covered"], "value": {"complete": True, "warehouse_ids": ["W"]},
    }]
    _, artifact = forecast(manifest, mapping)
    rows = corrected_rows(artifact)
    assert {row["estimated_lost"] for row in rows if row["sku_id"] == "covered"} == {Decimal(0)}
    assert all(row["estimated_lost"] is None for row in rows if row["sku_id"] == "unlogged")
    unlogged = next(item for item in artifact["series"] if item["sku_id"] == "unlogged")
    assert any(issue["code"] == "RECOVERY_UNAVAILABLE" for issue in unlogged["warnings"])


def test_seasonality_with_future_evidence_is_neutral_at_replay_origin(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    mapping["assumptions"] = [{
        "field": "seasonality", "provenance": "derived", "reason": "Evidence deliberately after origin",
        "scope_ids": ["covered"], "value": {"category_id": "audit", "verified": True,
        "source": "test-late-evidence", "evidence_end": "2026-04-10T00:00:00+00:00",
        "factors": {str(month): month for month in range(1, 13)}},
    }]
    _, artifact = forecast(manifest, mapping)
    assert all(day["seasonal_delta"] == 0 and day["mean"] == 10 for day in artifact["series"][0]["daily"])
    assert any(issue["code"] == "SEASONALITY_NOT_POINT_IN_TIME" for issue in artifact["series"][0]["warnings"])


def test_synthetic_source_provenance_survives_to_forecast(tmp_path):
    manifest, mapping = fixture_inputs(tmp_path)
    manifest["sources"][1]["provenance"] = "synthetic"
    snapshot, artifact = forecast(manifest, mapping)
    assert snapshot["mode"] == "synthetic_demo"
    assert artifact["mode"] == "synthetic_demo"
    assert all(row["provenance"] == "synthetic" for row in read_table(snapshot["tables"]["sales_events"]))
