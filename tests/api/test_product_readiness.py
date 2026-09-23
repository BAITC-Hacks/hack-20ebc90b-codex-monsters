"""Buyer acceptance: resume work, retry safely, and export an actual order.

These checks use isolated SQLite/Parquet state and the explicitly synthetic
forecast provider. They establish workflow correctness, not forecast accuracy.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import csv
from datetime import timedelta
from decimal import Decimal
import io
import json

from fastapi.testclient import TestClient
import pytest

from ekt.api.app import create_app
from ekt.contracts import QualityReport, SnapshotManifest, SourcePage, Workspace
from test_buyer_workflow import (
    SNAPSHOT_REQUEST, build_run, integration_forecast, wait_for_job,
)


@pytest.fixture
def buyer(tmp_path, monkeypatch):
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    monkeypatch.delenv("EKT_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("EKT_DEMO_ROLE", "approver")
    with TestClient(create_app(data_dir=tmp_path, forecast_provider=integration_forecast)) as client:
        yield client


def test_new_buyer_workspace_has_selectable_sources_without_internal_configuration(buyer, tmp_path):
    response = buyer.get("/v1/workspace")
    assert response.status_code == 200, response.text
    workspace = Workspace.model_validate(response.json())
    assert workspace.runs == [] and workspace.snapshots == []
    assert workspace.latest_run_id is None and workspace.latest_snapshot_id is None
    source = next(source for source in workspace.sources if source.source_id == "synthetic-demo")
    assert source.available_for_import
    assert source.mode == "synthetic_demo"
    assert source.name and source.default_as_of and source.mapping_version
    listed = SourcePage.model_validate(buyer.get("/v1/sources").json())
    assert listed.items == workspace.sources
    assert str(tmp_path) not in response.text
    assert '"path"' not in response.text and '"request"' not in response.text


def test_workspace_restores_completed_work_and_approval_after_backend_restart(tmp_path, monkeypatch):
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    monkeypatch.delenv("EKT_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("EKT_DEMO_ROLE", "approver")
    with TestClient(create_app(data_dir=tmp_path, forecast_provider=integration_forecast)) as client:
        request, run, proposals = build_run(client)
        proposal = next(p for p in proposals if p["capabilities"]["can_approve"])
        path = f"/v1/proposals/{proposal['proposal_id']}"
        approved = client.post(f"{path}/approve", json={
            "expected_version": proposal["version"], "content_hash": proposal["content_hash"],
        })
        assert approved.status_code == 200, approved.text

    with TestClient(create_app(data_dir=tmp_path, forecast_provider=integration_forecast)) as client:
        response = client.get("/v1/workspace")
        assert response.status_code == 200, response.text
        workspace = Workspace.model_validate(response.json())
        assert workspace.latest_run_id == run["id"]
        assert workspace.latest_snapshot_id == request["snapshot_id"]
        restored = next(item for item in workspace.runs if item.id == run["id"])
        assert restored.status == "succeeded"
        assert set(restored.proposal_ids) == {p["proposal_id"] for p in proposals}
        current = client.get(path).json()
        assert current["status"] == "approved" and current["capabilities"]["can_export"]
        assert current["lines"] == proposal["lines"]
        assert str(tmp_path) not in response.text
        assert '"request"' not in response.text
        for snapshot in workspace.snapshots:
            assert all(table.uri.startswith("artifact://") for table in snapshot.tables.values())
            assert all(source.local_ref is None for source in snapshot.source_refs)


def test_zero_order_cannot_be_approved_but_buyer_can_restore_one_position_and_export(buyer):
    _, _, proposals = build_run(buyer)
    proposal = next(p for p in proposals if p["capabilities"]["can_approve"])
    path = f"/v1/proposals/{proposal['proposal_id']}"
    selected = next(line for line in proposal["lines"] if Decimal(line["selected_purchase_qty"]) > 0)
    zero = buyer.patch(path, json={
        "expected_version": proposal["version"],
        "edits": [{"line_id": line["line_id"], "purchase_qty": "0"} for line in proposal["lines"]],
        "reason": "Закупку всех позиций временно откладываем",
    })
    assert zero.status_code == 200, zero.text
    empty = zero.json()
    assert not empty["capabilities"]["can_approve"]
    assert not empty["capabilities"]["can_export"]
    assert Decimal(empty["total_cost"]) == 0
    rejected = buyer.post(f"{path}/approve", json={
        "expected_version": empty["version"], "content_hash": empty["content_hash"],
    })
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "EMPTY_ORDER"

    restored_response = buyer.patch(path, json={
        "expected_version": empty["version"],
        "edits": [{"line_id": selected["line_id"], "purchase_qty": selected["selected_purchase_qty"]}],
        "reason": "Возвращаем подтверждённую потребность по одной позиции",
    })
    assert restored_response.status_code == 200, restored_response.text
    restored = restored_response.json()
    assert restored["capabilities"]["can_approve"]
    assert not restored["capabilities"]["can_export"]
    assert not any(reason.startswith("Нет товаров к заказу") for reason in restored["capabilities"]["reasons"])
    approval = buyer.post(f"{path}/approve", json={
        "expected_version": restored["version"], "content_hash": restored["content_hash"],
    })
    assert approval.status_code == 200, approval.text
    export = buyer.post(f"{path}/export", json={
        "expected_version": restored["version"], "idempotency_key": "only-positive-lines",
    })
    assert export.status_code == 200, export.text
    rows = list(csv.reader(io.StringIO(export.text)))
    assert "ДЕМОНСТРАЦИЯ" in rows[0][0]
    assert len(rows) == 3  # Watermark, header, one selected position.
    record = dict(zip(rows[1], rows[2], strict=True))
    assert record["sku_id"] == selected["sku_id"]
    assert Decimal(record["purchase_qty"]) == Decimal(selected["selected_purchase_qty"])
    assert "Базовый спрос:" in record["explanation"]
    assert "delta_base_qty" not in record["explanation"]
    assert not record["explanation"].startswith(("{", "["))


def test_readonly_role_cannot_edit_quantities_and_failure_preserves_state(tmp_path, monkeypatch):
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    monkeypatch.delenv("EKT_SOURCE_ROOT", raising=False)
    monkeypatch.setenv("EKT_DEMO_ROLE", "planner")
    with TestClient(create_app(data_dir=tmp_path, forecast_provider=integration_forecast)) as client:
        _, _, proposals = build_run(client)
        proposal = proposals[0]
        path = f"/v1/proposals/{proposal['proposal_id']}"
        response = client.patch(path, json={
            "expected_version": proposal["version"],
            "edits": [{"line_id": proposal["lines"][0]["line_id"], "purchase_qty": "0"}],
            "reason": "Попытка изменения без роли закупщика",
        })
        assert response.status_code == 403, response.text
        assert response.json()["code"] == "FORBIDDEN"
        assert client.get(path).json() == proposal
        assert client.app.state.service.store.list_audit("proposals", proposal["proposal_id"]) == []


def test_import_retry_reuses_job_and_conflicting_payload_is_rejected(buyer):
    request = {**SNAPSHOT_REQUEST, "idempotency_key": "buyer-import-once"}
    first = buyer.post("/v1/snapshots", json=request)
    second = buyer.post("/v1/snapshots", json=request)
    assert first.status_code == second.status_code == 202
    assert first.json() == second.json()
    result = wait_for_job(buyer, first.json()["status_url"])
    assert result["status"] == "succeeded", result
    changed = buyer.post("/v1/snapshots", json={**request, "mapping_version": "different-mapping"})
    assert changed.status_code == 409, changed.text
    workspace = buyer.get("/v1/workspace").json()
    assert len(workspace["snapshots"]) == 1
    assert workspace["latest_snapshot_id"] == result["result_ref"]
    persisted = buyer.app.state.service.store.list_items("jobs")
    assert len([job for job in persisted if job["kind"] == "snapshot"]) == 1


def test_simultaneous_approval_retries_share_one_confirmation_and_audit_event(buyer):
    _, _, proposals = build_run(buyer)
    proposal = next(p for p in proposals if p["capabilities"]["can_approve"])
    path = f"/v1/proposals/{proposal['proposal_id']}/approve"
    payload = {"expected_version": proposal["version"], "content_hash": proposal["content_hash"]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(buyer.post, path, json=payload) for _ in range(2)]
        responses = [future.result() for future in futures]
    assert all(response.status_code == 200 for response in responses), [r.text for r in responses]
    assert responses[0].json() == responses[1].json()
    replay = buyer.post(path, json=payload)
    assert replay.status_code == 200
    assert replay.json() == responses[0].json()
    audit = buyer.app.state.service.store.list_audit("proposals", proposal["proposal_id"])
    approvals = [event for event in audit if event["payload"].get("action") == "approve"]
    assert len(approvals) == 1
    assert approvals[0]["payload"]["approval_id"] == replay.json()["approval_id"]
    assert proposal["content_hash"] in json.dumps(approvals)


@pytest.mark.parametrize("returned_mode,day_offset,error_code", [
    ("synthetic_demo", 0, "SNAPSHOT_MODE_MISMATCH"),
    ("real_preview", 1, "SNAPSHOT_DATE_MISMATCH"),
])
def test_invalid_importer_provenance_is_rejected_before_publishing_snapshot(
    tmp_path, monkeypatch, returned_mode, day_offset, error_code,
):
    monkeypatch.delenv("EKT_SOURCE_ROOT", raising=False)
    source_file = tmp_path / "private-source.xlsx"
    # The injected provider supplies a contract directly; it never parses this
    # deliberately inert source marker or accesses corporate files.
    source_file.write_text("provider boundary test only")
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"sources": [{
        "source_id": "registered-real-source", "name": "Registered workbook",
        "kind": "xlsx", "mode": "real_preview", "path": str(source_file),
    }]}))
    monkeypatch.setenv("EKT_SOURCE_CONFIG", str(config))

    def faulty_provider(manifest, mapping):
        return SnapshotManifest(
            snapshot_id="invalid-provider-result", mapping_version=mapping.mapping_version,
            manifest_hash="provider-output-hash", mode=returned_mode,
            as_of=manifest.as_of + timedelta(days=day_offset), created_at=manifest.as_of,
            source_refs=manifest.source_refs, tables={}, quality=QualityReport(),
        )

    with TestClient(create_app(data_dir=tmp_path / "state", snapshot_provider=faulty_provider)) as client:
        accepted = client.post("/v1/snapshots", json={
            **SNAPSHOT_REQUEST, "source_ids": ["registered-real-source"], "mode": "real_preview",
        })
        assert accepted.status_code == 202, accepted.text
        result = wait_for_job(client, accepted.json()["status_url"])
        assert result["status"] == "failed", result
        assert result["error"]["code"] == error_code
        workspace = client.get("/v1/workspace")
        assert workspace.status_code == 200
        assert workspace.json()["snapshots"] == []
        assert workspace.json()["runs"] == []
        assert str(tmp_path) not in workspace.text
        assert client.app.state.service.store.list_items("snapshots") == []
        assert client.get("/v1/proposals").json()["items"] == []
