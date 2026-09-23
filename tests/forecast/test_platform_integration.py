"""Real A generator -> B model -> A planning/API, without fixture forecasts."""

from __future__ import annotations

import csv
import io
import time
from decimal import Decimal

import pyarrow.parquet as pq
import pytest
from fastapi.testclient import TestClient

from ekt.api.app import create_app
from ekt.contracts import ForecastArtifact, ForecastRequest, PlanningPolicy
from ekt.demo import DEMO_AS_OF, DEMO_SEED, create_demo_snapshot
from ekt.forecast import build_forecast
from ekt.planning import build_proposals


@pytest.fixture(scope="module")
def platform_demo(tmp_path_factory):
    snapshot = create_demo_snapshot(tmp_path_factory.mktemp("real-platform-demo"))
    request = ForecastRequest(run_id="platform-audit", as_of=snapshot.as_of,
                              horizon_days=90, seed=DEMO_SEED)
    artifact = build_forecast(snapshot, request)
    return snapshot, request, artifact


def test_platform_generator_is_consumed_by_real_b_forecast_and_planner(platform_demo):
    snapshot, request, artifact = platform_demo
    assert isinstance(artifact, ForecastArtifact)
    assert artifact.model_version.startswith("robust-daily-")
    assert artifact.mode == "synthetic_demo"
    assert len(artifact.series) == 16
    assert all(len(item.daily) == 90 for item in artifact.series)
    assert not any(issue.code == "UNKNOWN_DEMAND_COVERAGE" for issue in artifact.quality.issues)
    assert not any(issue.code == "FORECAST_PROVIDER_NOT_CONNECTED" for issue in artifact.quality.issues)
    recovered = {item.sku_id: item.estimated_lost_total for item in artifact.series}
    assert recovered["DEMO-004"] > 0
    assert recovered["DEMO-005"] > 0
    events = pq.read_table(artifact.classifications_ref).to_pylist()
    spike = next(event for event in events if event["event_id"] == "event-extra-0001")
    assert spike["label"] == "suspected_project"
    assert spike["review_status"] == "pending"

    policy = PlanningPolicy(service_target=0.95, currency="KZT")
    proposals = build_proposals(snapshot, artifact, policy, request.run_id)
    assert {proposal.supplier_id for proposal in proposals} == {"IEK-DEMO", "SYSTEME-DEMO"}
    lines = [line for proposal in proposals for line in proposal.lines]
    assert len(lines) == 15
    assert "DEMO-016" not in {line.sku_id for line in lines}
    assert any(item.sku_id == "DEMO-016" for proposal in proposals for item in proposal.excluded_lines)
    for proposal in proposals:
        assert proposal.mode == "synthetic_demo"
        assert proposal.capabilities.can_approve
        for line in proposal.lines:
            assert sum(part.delta_base_qty for part in line.explanation) == line.selected_base_qty
            assert line.selected_purchase_qty == 0 or line.selected_purchase_qty >= line.moq_purchase
            assert line.selected_purchase_qty % line.pack_multiple_purchase == 0
            assert line.selected_base_qty == line.selected_purchase_qty * line.conversion
    cable = next(line for line in lines if line.sku_id == "DEMO-006")
    assert cable.base_uom == "m" and cable.purchase_uom == "coil"
    assert cable.conversion == Decimal("100")


def test_real_b_forecast_replay_and_policy_changes_preserve_artifact(platform_demo):
    snapshot, request, artifact = platform_demo
    replay = build_forecast(snapshot, request.model_copy(update={"run_id": "other-run"}))
    assert replay == artifact
    original = artifact.model_dump(mode="json")
    base = build_proposals(snapshot, artifact, PlanningPolicy(service_target=0.95), "base")
    changed = build_proposals(snapshot, artifact, PlanningPolicy(service_target=0.99), "changed")
    before = {line.sku_id: line.safety_stock for proposal in base for line in proposal.lines}
    after = {line.sku_id: line.safety_stock for proposal in changed for line in proposal.lines}
    assert set(before) == set(after) and len(before) == 15
    assert all(after[sku] >= before[sku] for sku in before)
    assert any(after[sku] > before[sku] for sku in before)
    assert artifact.model_dump(mode="json") == original


def _completed(client, path):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        payload = response.json()
        if payload["status"] in {"succeeded", "failed"}:
            assert payload["status"] == "succeeded", payload
            return payload
        time.sleep(0.02)
    pytest.fail(f"Real provider job did not complete: {path}")


def test_api_real_provider_replans_without_refit_and_approves_exact_csv(tmp_path, monkeypatch):
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    monkeypatch.setenv("EKT_DEMO_ROLE", "approver")
    calls = []

    def real_provider(snapshot, request):
        result = build_forecast(snapshot, request)
        calls.append(result)
        return result

    with TestClient(create_app(data_dir=tmp_path, forecast_provider=real_provider)) as client:
        started = client.post("/v1/snapshots", json={
            "source_ids": ["synthetic-demo"], "mapping_version": "synthetic-v1",
            "mode": "synthetic_demo", "as_of": DEMO_AS_OF.isoformat(),
        })
        assert started.status_code == 202, started.text
        snapshot_job = _completed(client, started.json()["status_url"])
        started = client.post("/v1/planning-runs", json={
            "snapshot_id": snapshot_job["result_ref"],
            "policy": {"service_target": 0.95, "currency": "KZT"},
            "idempotency_key": "real-b-model-run",
        })
        assert started.status_code == 202, started.text
        run = _completed(client, started.json()["status_url"])
        assert len(calls) == 1
        assert run["model_version"] == calls[0].model_version
        assert run["forecast_id"] == calls[0].forecast_id
        page = client.get("/v1/proposals", params={"run_id": run["id"]})
        assert page.status_code == 200, page.text
        proposals = [client.get(f"/v1/proposals/{row['proposal_id']}").json()
                     for row in page.json()["items"]]
        assert len(proposals) == 2
        assert sum(len(proposal["lines"]) for proposal in proposals) == 15
        assert not any(warning["code"] == "FORECAST_PROVIDER_NOT_CONNECTED"
                       for proposal in proposals for line in proposal["lines"] for warning in line["warnings"])

        classified = client.get(f"/v1/planning-runs/{run['id']}/demand-events",
                                params={"label": "suspected_project", "limit": 200})
        assert classified.status_code == 200, classified.text
        spike = next(row for row in classified.json()["items"] if row["event_id"] == "event-extra-0001")
        assert spike["review_status"] == "pending"
        assert Decimal(spike["observed_qty"]) == (
            Decimal(spike["regular_qty"]) + Decimal(spike["project_qty"]) + Decimal(spike["uncertain_qty"])
        )
        unfiltered = client.get(f"/v1/planning-runs/{run['id']}/demand-events", params={"limit": 200})
        assert unfiltered.status_code == 200, unfiltered.text
        assert len(unfiltered.json()["items"]) == 200
        assert unfiltered.json()["next_cursor"] is not None
        public_rows = pq.read_table(calls[0].classifications_ref).to_pylist()
        return_position = next(index for index, row in enumerate(public_rows)
                               if row["event_id"] == "event-return-0001")
        returned = client.get(f"/v1/planning-runs/{run['id']}/demand-events",
                              params={"cursor": str(return_position), "limit": 1})
        assert returned.status_code == 200, returned.text
        returned_row = returned.json()["items"][0]
        assert returned_row["event_id"] == "event-return-0001"
        assert Decimal(returned_row["observed_qty"]) == Decimal("2")
        assert "DEMAND_DECREASE" in returned_row["reason_codes"]

        scenario_request = client.post("/v1/scenarios", json={
            "base_run_id": run["id"], "overrides": {"service_target": 0.99, "lead_time_delay_days": 7},
            "seed": DEMO_SEED, "idempotency_key": "same-b-forecast-scenario",
        })
        assert scenario_request.status_code == 202, scenario_request.text
        scenario = _completed(client, scenario_request.json()["status_url"])
        assert len(calls) == 1, "A scenario must reuse B's immutable forecast"
        assert len(scenario["changed_lines"]) == 15
        assert any(calls[0].model_version in note for note in scenario["assumptions"])
        for proposal in proposals:
            path = f"/v1/proposals/{proposal['proposal_id']}"
            assert client.get(path).json() == proposal
            export_payload = {"expected_version": proposal["version"], "idempotency_key": f"csv-{proposal['proposal_id']}"}
            assert client.post(f"{path}/export", json=export_payload).status_code == 403
            approved = client.post(f"{path}/approve", json={
                "expected_version": proposal["version"], "content_hash": proposal["content_hash"],
            })
            assert approved.status_code == 200, approved.text
            exported = client.post(f"{path}/export", json=export_payload)
            assert exported.status_code == 200, exported.text
            rows = list(csv.reader(io.StringIO(exported.content.decode("utf-8-sig"))))
            assert rows[0] == ["ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ"]
            assert len(rows) == len(proposal["lines"]) + 2
            by_sku = {row[3]: row for row in rows[2:]}
            for line in proposal["lines"]:
                row = by_sku[line["sku_id"]]
                assert row[0] == "synthetic_demo" and row[10] == "approved"
                assert Decimal(row[4]) == Decimal(line["selected_purchase_qty"])
                assert Decimal(row[6]) == Decimal(line["selected_base_qty"])
