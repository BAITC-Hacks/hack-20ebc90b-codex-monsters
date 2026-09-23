"""B-only integration fixtures: independent of the A-owned common fixture factory."""
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from ekt.data import build_snapshot
from ekt.data.boundary import as_payload
from ekt.forecast import build_forecast

AS_OF = "2026-09-22T23:59:59+00:00"


def fixture_inputs(tmp_path):
    days = [date(2026, 7, 1) + timedelta(days=i) for i in range(84)]
    sales = [{"event_id": f"sale-{i}", "doc_id": f"doc-{i}", "event_at": f"{day}T12:00:00+00:00",
              "sku_id": "00123", "warehouse_id": "test-warehouse", "quantity_base": "10",
              "event_type": "shipment", "demand_effect": "increase"}
             for i, day in enumerate(days) if day != date(2026, 9, 10)]
    sales.append(dict(sales[0], event_id="project", doc_id="one-off", event_at="2026-09-15T12:00:00+00:00", quantity_base="1000"))
    def source(source_id, kind, rows, **kwargs):
        return {"source_id": source_id, "kind": kind, "rows": rows, "provenance": "synthetic", **kwargs}
    manifest = {"as_of": AS_OF, "mode": "synthetic_demo", "artifact_root": str(tmp_path / "artifacts"), "sources": [
        source("master", "sku_master", [{"sku_id": "00123", "name": "Switch", "base_uom": "piece", "category_id": "electrical",
             "purchase_uom": "piece", "base_units_per_purchase_uom": "1", "quantity_quantum": "1", "supplier_id": "synthetic-systeme"}]),
        source("sales", "sales_events", sales, coverage={"start": "2026-07-01", "end": "2026-09-23", "complete": True,
                                                          "sku_ids": ["00123"], "warehouse_ids": ["test-warehouse"]}),
        source("stockouts", "stockout_intervals", [{"sku_id": "00123", "warehouse_id": "test-warehouse",
             "start_at": "2026-09-10T00:00:00+00:00", "end_at": "2026-09-11T00:00:00+00:00", "unavailable_fraction": 1.0,
             "evidence": "synthetic", "confidence": 1.0}]),
    ]}
    mapping = {"version": "b-service-test-v1"}
    request = {"run_id": "run-one", "as_of": AS_OF, "sku_ids": ["00123"], "warehouse_ids": ["test-warehouse"],
               "horizon_days": 90, "seed": 42, "growth_overrides": [], "review_overrides": []}
    return manifest, mapping, request


def run(tmp_path):
    manifest, mapping, request = fixture_inputs(tmp_path)
    snapshot = build_snapshot(manifest, mapping)
    return as_payload(snapshot), as_payload(build_forecast(snapshot, request)), request


def test_real_entry_points_publish_90_day_forecast_and_recover(tmp_path):
    snapshot, result, _ = run(tmp_path)
    assert result["mode"] == "synthetic_demo"
    series = result["series"][0]
    assert len(series["daily"]) == 90
    assert series["daily"][0]["date"] == "2026-09-23"
    assert series["daily"][-1]["date"] == "2026-12-21"
    assert series["estimated_lost_total"] > 0
    assert series["uncertainty"]["method"] == "iid_residual_normal"
    assert series["uncertainty"]["calibration"] == "unvalidated"
    for row in series["daily"]:
        assert row["mean"] == pytest.approx(row["baseline_mean"] + row["seasonal_delta"] + row["growth_delta"])
        assert row["mean"] >= 0
    classes = pq.read_table(result["classifications_ref"]).to_pylist()
    project = next(e for e in classes if e["event_id"] == "project")
    assert project["label"] == "suspected_project"
    assert project["review_status"] == "pending"
    for event in classes:
        assert event["regular_qty"] + event["project_qty"] + event["uncertain_qty"] == event["observed_qty"]
    corrected = pq.read_table(result["corrected_demand_ref"]).to_pylist()
    absent = next(r for r in corrected if r["date"] == "2026-09-10")
    assert absent["estimated_lost"] > 0
    assert absent["corrected_demand"] == absent["observed_regular"] + absent["estimated_lost"]
    assert Path(result["corrected_demand_ref"]).parent.joinpath("forecast.json").exists()


def test_run_identity_reuses_identical_artifact(tmp_path):
    snapshot, first, request = run(tmp_path)
    second = as_payload(build_forecast(snapshot, dict(request, run_id="another-run")))
    assert first == second


def test_history_classification_override_is_consumed(tmp_path):
    manifest, mapping, request = fixture_inputs(tmp_path)
    snapshot = build_snapshot(manifest, mapping)
    request["review_overrides"] = [{"event_ids": ["project"], "label": "regular", "reason": "Confirmed repeat business"}]
    result = as_payload(build_forecast(snapshot, request))
    classes = pq.read_table(result["classifications_ref"]).to_pylist()
    assert next(e for e in classes if e["event_id"] == "project")["label"] == "regular"
    assert result["series"][0]["classification_policy"] == "review_confirmed"


def test_budget_service_fields_not_present_in_forecast_interface(tmp_path):
    _, result, _ = run(tmp_path)
    assert "safety_stock" not in result["series"][0]
    assert "recommended_purchase_qty" not in result["series"][0]


def test_short_horizon_explicit_failure(tmp_path):
    manifest, mapping, request = fixture_inputs(tmp_path)
    request["horizon_days"] = 30
    with pytest.raises(ValueError) as caught:
        build_forecast(build_snapshot(manifest, mapping), request)
    assert caught.value.code == "HORIZON_TOO_SHORT"
