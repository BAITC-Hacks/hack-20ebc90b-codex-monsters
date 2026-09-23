"""Local buyer preparation and CSV workflow using the actual forecast provider.

Every sale in this suite is generated test data. ``real_preview`` exercises the
real-import workflow contract; these checks make no accuracy or business claim.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ekt.api.app import create_app
from ekt.contracts import (
    Assumption, Capabilities, QualityIssue, QualityReport, SalesEvent, SkuMaster,
    SnapshotManifest, load_table, write_table,
)
from test_buyer_workflow import wait_for_job


AS_OF = datetime(2026, 9, 23, 12, tzinfo=timezone.utc)


def _incomplete_snapshot(root, manifest, pipeline):
    masters, sales = [], []
    for sku, amount in (("TEST-A", 10), ("TEST-B", 4)):
        master = SkuMaster(
            sku_id=sku, name=f"Тестовый товар {sku}", base_uom="pcs",
            supplier_id="TEST-SUPPLIER", quantity_quantum=Decimal("0.5"),
            provenance="synthetic",
        )
        # B's canonical tables retain source lineage in addition to model fields.
        masters.append({**master.model_dump(mode="json"), "source_id": "generated-test-history",
                        "row_ref": f"sku-master:{sku}"})
        for offset in range(1, 121):
            event_id = f"{sku}-{offset}"
            sales.append(SalesEvent(
                event_id=event_id, source_id="generated-test-history", row_ref=event_id,
                doc_id=event_id, event_at=manifest.as_of - timedelta(days=offset),
                sku_id=sku, warehouse_id="TEST-WH", event_type="sale",
                quantity_base=Decimal(amount + offset % 3), demand_effect="increase",
                customer_token=f"test-customer-{offset % 10}", provenance="synthetic",
            ))
    tables = {name: write_table(root, name, rows) for name, rows in {
        "sku_master": masters, "sales_events": sales, "inventory_snapshots": [],
        "supplier_terms": [], "pipeline_lines": pipeline, "stockout_intervals": [],
    }.items()}
    issues = [QualityIssue(code=code, severity="blocking", scope_ids=["TEST-A", "TEST-B"],
                           message="Test import intentionally lacks verified purchase inputs")
              for code in ("MISSING_PURCHASE_MAPPING", "MISSING_CURRENT_STOCK",
                           "MISSING_SUPPLIER_TERMS", "UNKNOWN_DEMAND_COVERAGE")]
    return SnapshotManifest(
        snapshot_id="incomplete-test-source", manifest_hash="generated-test-data-v1",
        mapping_version="buyer-test-v1", mode="real_preview", as_of=manifest.as_of,
        created_at=manifest.as_of, source_refs=manifest.source_refs, tables=tables,
        quality=QualityReport(status="blocked", accepted_rows=len(sales), affected_skus=2,
                              issues=issues, capabilities=Capabilities()),
        assumptions=[Assumption(field="test_fixture", value="synthetic", provenance="synthetic",
                                reason="Generated test history; no corporate observations")],
    )


@pytest.fixture
def buyer_api(tmp_path, monkeypatch, request):
    options = getattr(request, "param", {})
    as_of = options.get("as_of", AS_OF)
    pipeline = options.get("pipeline", [])
    registered = tmp_path / "generated-test-data.csv"
    registered.write_text("fixture\nsynthetic test input\n")
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"sources": [{
        "source_id": "test-import", "name": "Generated test fixture", "kind": options.get("source_kind", "csv"),
        "mode": "real_preview", "path": str(registered),
    }]}))
    monkeypatch.delenv("EKT_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("EKT_SOURCE_CONFIG", str(config))
    monkeypatch.setenv("EKT_DEMO_ROLE", "approver")
    monkeypatch.setenv("EKT_DEMO_ACTOR", "test-buyer")

    def snapshot_provider(manifest, mapping):
        snapshot = _incomplete_snapshot(Path(mapping.options["output_root"]), manifest, pipeline)
        if options.get("quarantine"):
            snapshot.quality.rejected_rows = 2
            snapshot.quality.issues.extend(QualityIssue(code=code, severity="blocking", scope_ids=["TEST-A"],
                source_ref="test-import:row-7", message="Test quarantined source row")
                for code in ("UNSUPPORTED_SIGNED_MOVEMENT", "MISSING_QUANTITY"))
        return snapshot

    with TestClient(create_app(data_dir=tmp_path / "state", snapshot_provider=snapshot_provider)) as client:
        accepted = client.post("/v1/snapshots", json={
            "source_ids": ["test-import"], "mapping_version": "buyer-test-v1",
            "mode": "real_preview", "as_of": as_of.isoformat(),
            "idempotency_key": "test-import",
        })
        assert accepted.status_code == 202, accepted.text
        job = wait_for_job(client, accepted.json()["status_url"])
        assert job["status"] == "succeeded", job
        snapshot_id = job["snapshot_id"]
        yield SimpleNamespace(client=client, snapshot_id=snapshot_id,
                              path=f"/v1/snapshots/{snapshot_id}/buyer-inputs",
                              service=client.app.state.service)


def _request(api, *, key="prepare-v1", all_items=False):
    response = api.client.get(api.path)
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    if not all_items:
        items = items[:1]
    for row in items:
        row.update(purchase_uom="pack", base_units_per_purchase_uom="2.5", quantity_quantum="0.5",
                   free_base="5", moq_purchase="4", pack_multiple_purchase="2",
                   lead_time_days=7, review_days=7, cost_per_base="12.5", currency="KZT")
    return {"items": items, "reason": "Остатки и условия проверены закупщиком для локального плана",
            "accept_history_estimate": True, "idempotency_key": key}


def _prepare(api, payload=None):
    response = api.client.post(api.path, json=payload or _request(api))
    assert response.status_code == 200, response.text
    return response.json()


def _plan(api, snapshot_id, *, policy=None):
    accepted = api.client.post("/v1/planning-runs", json={
        "snapshot_id": snapshot_id, "policy": policy or {"service_target": 0.95, "currency": "KZT"},
        "idempotency_key": "prepared-plan",
    })
    assert accepted.status_code == 202, accepted.text
    run = wait_for_job(api.client, accepted.json()["status_url"])
    assert run["status"] == "succeeded", run
    assert run["model_version"].startswith("robust-daily-")
    page = api.client.get("/v1/proposals", params={"run_id": run["id"]}).json()
    proposals = [api.client.get(f"/v1/proposals/{row['proposal_id']}").json()
                 for row in page["items"]]
    assert len(proposals) == 1
    return run, proposals[0]


def test_missing_purchase_inputs_are_null_and_source_provenance_is_visible(buyer_api):
    response = buyer_api.client.get(buyer_api.path)
    assert response.status_code == 200
    data = response.json()
    assert data["mode"] == "real_preview"
    assert data["history_start"] == (AS_OF - timedelta(days=120)).date().isoformat()
    assert {row["sku_id"] for row in data["items"]} == {"TEST-A", "TEST-B"}
    for row in data["items"]:
        for field in ("purchase_uom", "base_units_per_purchase_uom", "free_base", "moq_purchase",
                      "pack_multiple_purchase", "lead_time_days", "review_days", "cost_per_base"):
            assert row[field] is None
    source = buyer_api.client.get(f"/v1/snapshots/{buyer_api.snapshot_id}").json()
    assert not source["quality"]["capabilities"]["can_approve"]
    assert source["assumptions"][0]["provenance"] == "synthetic"


@pytest.mark.parametrize("change,expected_code", [
    ({"pack_multiple_purchase": None}, "INCOMPLETE_BUYER_INPUT"),
    ({"free_base": None}, "INCOMPLETE_BUYER_INPUT"),
    ({"supplier_id": "OTHER"}, "UNKNOWN_BUYER_ITEM"),
    ({"cost_per_base": "10", "currency": None}, "MISSING_CURRENCY"),
    ({"incoming_base_qty": "10", "incoming_eta": None}, "MISSING_RECEIPT_DATE"),
    ({"lead_time_days": 365, "review_days": 7}, "INVALID_PROTECTION_PERIOD"),
])
def test_incomplete_or_inconsistent_input_does_not_create_partial_snapshot(buyer_api, change, expected_code):
    payload = _request(buyer_api)
    payload["items"][0].update(change)
    rejected = buyer_api.client.post(buyer_api.path, json=payload)
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == expected_code
    assert len(buyer_api.service.store.list_items("snapshots")) == 1
    assert buyer_api.service.store.get_idempotency("buyer-inputs", "prepare-v1") is None


def test_history_assumption_needs_explicit_confirmation(buyer_api):
    payload = {**_request(buyer_api), "accept_history_estimate": False}
    rejected = buyer_api.client.post(buyer_api.path, json=payload)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "HISTORY_CONFIRMATION_REQUIRED"
    assert len(buyer_api.service.store.list_items("snapshots")) == 1


def test_preparation_is_isolated_immutable_and_idempotent(buyer_api):
    api = buyer_api
    source_before = api.service.store.get_item("snapshots", api.snapshot_id)
    original_bytes = {name: Path(ref["uri"]).read_bytes() for name, ref in source_before["tables"].items()}
    source_public = api.client.get(f"/v1/snapshots/{api.snapshot_id}").json()
    payload = _request(api)
    prepared = _prepare(api, payload)
    assert prepared["snapshot_id"] != api.snapshot_id
    assert prepared["mode"] == "real_preview"
    assert prepared["quality"]["capabilities"]["can_approve"]
    assert prepared["quality"]["capabilities"]["budget_available"]
    assert not prepared["quality"]["capabilities"]["can_export"]
    assert not any(issue["severity"] == "blocking" for issue in prepared["quality"]["issues"])
    assert _prepare(api, payload) == prepared
    assert _prepare(api, {**payload, "idempotency_key": "equivalent-request"}) == prepared
    conflict = api.client.post(api.path, json={**payload, "reason": "Другое основание"})
    assert conflict.status_code == 409
    assert api.service.store.get_item("snapshots", api.snapshot_id) == source_before
    assert api.client.get(f"/v1/snapshots/{api.snapshot_id}").json() == source_public
    assert all(Path(source_before["tables"][name]["uri"]).read_bytes() == content
               for name, content in original_bytes.items())
    stored = api.service.store.get_item("snapshots", prepared["snapshot_id"])
    snapshot = SnapshotManifest.model_validate({key: value for key, value in stored.items() if key != "version"})
    assert {row["sku_id"] for row in load_table(snapshot, "sales_events")} == {"TEST-A"}
    assert {row["sku_id"] for row in load_table(snapshot, "sku_master")} == {"TEST-A"}
    assert load_table(snapshot, "inventory_snapshots")[0]["free_base"] == "5"
    assert load_table(snapshot, "supplier_terms")[0]["provenance"] == "override"
    assert len(api.service.store.list_audit("snapshots", snapshot.snapshot_id)) == 1
    assert len(api.service.store.list_items("snapshots")) == 2


def test_canonical_master_lineage_is_preserved_in_source_without_breaking_preparation(buyer_api):
    source = buyer_api.service.store.get_item("snapshots", buyer_api.snapshot_id)
    source_snapshot = SnapshotManifest.model_validate({key: value for key, value in source.items() if key != "version"})
    assert load_table(source_snapshot, "sku_master")[0]["row_ref"] == "sku-master:TEST-A"
    prepared = _prepare(buyer_api)
    read_back = buyer_api.client.get(f"/v1/snapshots/{prepared['snapshot_id']}/buyer-inputs")
    assert read_back.status_code == 200, read_back.text
    assert read_back.json()["items"][0]["purchase_uom"] == "pack"
    assert load_table(source_snapshot, "sku_master")[0]["source_id"] == "generated-test-history"


def test_actual_forecast_edit_approval_and_csv_keep_buyer_assumptions(buyer_api):
    api = buyer_api
    payload = _request(api)
    prepared = _prepare(api, payload)
    _, proposal = _plan(api, prepared["snapshot_id"])
    assert proposal["mode"] == "real_preview"
    assert proposal["capabilities"]["can_approve"]
    assert not proposal["excluded_lines"]
    line = proposal["lines"][0]
    assert line["sku_id"] == "TEST-A"
    assert Decimal(line["selected_purchase_qty"]) > 0
    assert "BUYER_DEMAND_COVERAGE_ASSUMPTION" in {issue["code"] for issue in line["warnings"]}
    assert sum(Decimal(part["delta_base_qty"]) for part in line["explanation"]) == Decimal(line["selected_base_qty"])
    path = f"/v1/proposals/{proposal['proposal_id']}"
    approval_request = {"expected_version": 1, "content_hash": proposal["content_hash"]}
    rejected = api.client.post(f"{path}/approve", json=approval_request)
    assert rejected.status_code == 422
    assert rejected.json()["code"] == "ASSUMPTIONS_ACKNOWLEDGEMENT_REQUIRED"
    assert api.client.get(path).json()["status"] == "draft"
    approval_request["acknowledge_assumptions"] = True
    approved = api.client.post(f"{path}/approve", json=approval_request)
    assert approved.status_code == 200, approved.text
    assert api.client.post(f"{path}/approve", json=approval_request).json() == approved.json()
    export_request = {"expected_version": 1, "idempotency_key": "local-csv-v1"}
    exported = api.client.post(f"{path}/export", json=export_request)
    assert exported.status_code == 200, exported.text
    assert api.client.post(f"{path}/export", json=export_request).content == exported.content
    records = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert records[0] == ["ЛОКАЛЬНЫЙ ПЛАН ЗАКУПКИ — УСЛОВИЯ И ДОПУЩЕНИЯ ПОДТВЕРЖДЕНЫ ЗАКУПЩИКОМ; НЕ ОТПРАВЛЕН"]
    row = dict(zip(records[1], records[2]))
    assert row["mode"] == "real_preview"
    assert row["sku_id"] == "TEST-A"
    assert Decimal(row["purchase_qty"]) == Decimal(line["selected_purchase_qty"])
    assert Decimal(row["base_qty"]) == Decimal(row["purchase_qty"]) * Decimal("2.5")
    assert Decimal(row["line_cost"]) == Decimal(row["base_qty"]) * Decimal("12.5")
    assert payload["reason"] in row["assumptions"]
    assert "нулевые продажи" in row["assumptions"]
    assert "Generated test history; no corporate observations" in row["assumptions"]
    assert "purchase-" in exported.headers["content-disposition"]
    changed_qty = Decimal(line["selected_purchase_qty"]) + Decimal("2")
    edited = api.client.patch(path, json={
        "expected_version": 1, "edits": [{"line_id": line["line_id"], "purchase_qty": str(changed_qty)}],
        "reason": "Дополнительная упаковка для подтверждённой потребности",
    })
    assert edited.status_code == 200, edited.text
    assert edited.json()["status"] == "draft"
    assert edited.json()["version"] == 2
    assert edited.json()["capabilities"]["can_approve"]
    assert not edited.json()["capabilities"]["can_export"]
    assert api.client.post(f"{path}/export", json=export_request).status_code == 409
    assert api.client.post(f"{path}/export", json={"expected_version": 2, "idempotency_key": "local-csv-v2"}).status_code == 403
    approval_request.update(expected_version=2, content_hash=edited.json()["content_hash"])
    assert api.client.post(f"{path}/approve", json=approval_request).status_code == 200
    fresh_export = api.client.post(f"{path}/export", json={"expected_version": 2, "idempotency_key": "local-csv-v2"})
    assert fresh_export.status_code == 200
    assert fresh_export.content != exported.content
    assert api.client.get(path, params={"version": 1}).json()["lines"] == proposal["lines"]


@pytest.mark.parametrize("buyer_api", [{"pipeline": [{
    "po_line_id": "test-receipt", "sku_id": "TEST-A", "supplier_id": "TEST-SUPPLIER",
    "warehouse_id": "TEST-WH", "remaining_base_qty": "12.5", "eta_start": None,
    "eta_end": "2026-09-24T01:00:00+05:00", "eta_semantics": "deadline",
    "status": "in_transit", "provenance": "synthetic",
}]}], indirect=True)
def test_future_receipt_on_snapshot_day_roundtrips_in_snapshot_timezone(buyer_api):
    inputs = buyer_api.client.get(buyer_api.path).json()
    row = next(row for row in inputs["items"] if row["sku_id"] == "TEST-A")
    assert row["incoming_eta"] == "2026-09-23"
    assert Decimal(row["incoming_base_qty"]) == Decimal("12.5")
    prepared = _prepare(buyer_api)
    refreshed = buyer_api.client.get(f"/v1/snapshots/{prepared['snapshot_id']}/buyer-inputs").json()
    assert refreshed["items"][0]["incoming_eta"] == "2026-09-23"


@pytest.mark.parametrize("buyer_api", [
    {"as_of": datetime(2026, 9, 23, 23, tzinfo=timezone(timedelta(hours=-5)))},
    {"as_of": datetime(2026, 9, 23, 0, tzinfo=timezone(timedelta(hours=5)))},
], indirect=True)
def test_prepared_plan_uses_one_calendar_at_timezone_day_boundaries(buyer_api):
    prepared = _prepare(buyer_api)
    _, proposal = _plan(buyer_api, prepared["snapshot_id"], policy={
        "service_target": 0.95, "currency": "KZT", "max_cover_days": 90,
    })
    assert proposal["lines"]
    assert proposal["capabilities"]["can_approve"]


@pytest.mark.parametrize("buyer_api", [
    {"source_kind": "sales_events", "quarantine": True},
    {"source_kind": "supplier_terms", "quarantine": True},
], indirect=True)
def test_explicit_history_assumption_only_allows_quarantined_sales_not_other_bad_inputs(buyer_api):
    source = buyer_api.client.get(f"/v1/snapshots/{buyer_api.snapshot_id}").json()
    prepared = _prepare(buyer_api)
    is_sales = source["source_refs"][0]["kind"] == "sales_events"
    expected = "warning" if is_sales else "blocking"
    notices = [i for i in prepared["quality"]["issues"] if i["code"] in {"UNSUPPORTED_SIGNED_MOVEMENT", "MISSING_QUANTITY"}]
    assert len(notices) == 2 and all(i["severity"] == expected for i in notices)
    assert prepared["quality"]["rejected_rows"] == 2
    assert buyer_api.client.get(f"/v1/snapshots/{buyer_api.snapshot_id}").json() == source
    _, proposal = _plan(buyer_api, prepared["snapshot_id"])
    assert bool(proposal["lines"]) is is_sales
    assert proposal["capabilities"]["can_approve"] is is_sales
