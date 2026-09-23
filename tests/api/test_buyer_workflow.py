"""Black-box buyer workflow checks against real SQLite, Parquet and planning.

Forecasting here is explicitly the labelled synthetic integration fixture; the
data/forecast participant owns model accuracy tests. No real supplier data or
external network service is needed by these checks.
"""

from __future__ import annotations

import csv
import io
import json
import time
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from ekt.contracts import ProposalDetail


SNAPSHOT_REQUEST = {
    "source_ids": ["synthetic-demo"], "mapping_version": "1.0",
    "mode": "synthetic_demo", "as_of": "2026-09-23T00:00:00+05:00",
}


def integration_forecast(snapshot, request):
    from ekt.demo import fixture_forecast
    return fixture_forecast(snapshot)


def wait_for_job(client, path, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(path)
        assert response.status_code == 200, response.text
        data = response.json()
        if data["status"] in {"succeeded", "failed"}:
            return data
        time.sleep(0.01)
    pytest.fail(f"Job did not complete within {timeout}s: {path}")


def build_run(client, *, policy=None, key="initial-run"):
    created = client.post("/v1/snapshots", json=SNAPSHOT_REQUEST)
    assert created.status_code == 202, created.text
    snapshot_job = wait_for_job(client, created.json()["status_url"])
    assert snapshot_job["status"] == "succeeded", snapshot_job
    snapshot_id = snapshot_job.get("snapshot_id") or snapshot_job["result_ref"]
    request = {
        "snapshot_id": snapshot_id,
        "policy": policy or {"service_target": 0.95, "currency": "KZT"},
        "idempotency_key": key,
    }
    response = client.post("/v1/planning-runs", json=request)
    assert response.status_code == 202, response.text
    run = wait_for_job(client, response.json()["status_url"])
    assert run["status"] == "succeeded", run
    page = client.get("/v1/proposals", params={"run_id": response.json()["run_id"]})
    assert page.status_code == 200, page.text
    proposals = [client.get(f"/v1/proposals/{item['proposal_id']}").json() for item in page.json()["items"]]
    assert proposals, "Completed fixture run must expose actual supplier proposals"
    return request, run, proposals


@pytest.fixture
def client(tmp_path, monkeypatch):
    from ekt.api.app import create_app
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    monkeypatch.setenv("EKT_DEMO_ROLE", "approver")
    with TestClient(create_app(data_dir=tmp_path, forecast_provider=integration_forecast)) as instance:
        yield instance


def test_end_to_end_run_has_valid_quantities_explanations_and_provenance(client, tmp_path):
    request, run, proposals = build_run(client)
    assert run["progress"] == 1
    assert len({proposal["supplier_id"] for proposal in proposals}) == 2
    lines = [line for proposal in proposals for line in proposal["lines"]]
    assert lines
    for proposal in proposals:
        ProposalDetail.model_validate(proposal)
        assert proposal["mode"] == "synthetic_demo"
        assert proposal["status"] == "draft"
        assert proposal["version"] == 1
        assert proposal["capabilities"]["can_export"] is False
        for line in proposal["lines"]:
            quantity = Decimal(line["selected_purchase_qty"])
            assert quantity == 0 or quantity >= Decimal(line["moq_purchase"])
            assert quantity % Decimal(line["pack_multiple_purchase"]) == 0
            assert Decimal(line["selected_base_qty"]) == quantity * Decimal(line["conversion"])
            assert sum(Decimal(part["delta_base_qty"]) for part in line["explanation"]) == Decimal(line["selected_base_qty"])
        assert Decimal(proposal["total_cost"]) == sum(Decimal(line["line_cost"]) for line in proposal["lines"])
    exclusions = [item for proposal in proposals for item in proposal["excluded_lines"]]
    assert any(item["sku_id"] == "DEMO-016" for item in exclusions)
    assert any("FORECAST_PROVIDER_NOT_CONNECTED" in json.dumps(line["warnings"]) for line in lines)
    public = client.get(f"/v1/snapshots/{request['snapshot_id']}")
    assert public.status_code == 200
    assert str(tmp_path) not in public.text
    assert all(ref["uri"].startswith("artifact://") for ref in public.json()["tables"].values())
    events = client.get(f"/v1/planning-runs/{run['id']}/demand-events")
    assert events.status_code == 200
    assert events.json()["items"] == []  # Fixture never invents model-classification results.


def test_run_idempotency_reuses_result_and_rejects_changed_request(client):
    request, run, _ = build_run(client)
    replay = client.post("/v1/planning-runs", json=request)
    assert replay.status_code == 202
    assert replay.json()["run_id"] == run["id"]
    changed = {**request, "policy": {"service_target": 0.99, "currency": "KZT"}}
    conflict = client.post("/v1/planning-runs", json=changed)
    assert conflict.status_code == 409, conflict.text
    assert conflict.json()["code"]


def test_approve_export_edit_requires_new_exact_approval(client):
    _, _, proposals = build_run(client)
    proposal = next(p for p in proposals if p["lines"])
    path = f"/v1/proposals/{proposal['proposal_id']}"
    export_request = {"expected_version": 1, "idempotency_key": "csv-v1"}
    assert client.post(f"{path}/export", json=export_request).status_code == 403
    wrong_hash = client.post(f"{path}/approve", json={"expected_version": 1, "content_hash": "wrong"})
    assert wrong_hash.status_code == 409
    approval = client.post(f"{path}/approve", json={"expected_version": 1, "content_hash": proposal["content_hash"]})
    assert approval.status_code == 200, approval.text
    assert approval.json()["version"] == 1
    approved = client.get(path).json()
    assert approved["status"] == "approved" and approved["version"] == 1
    assert approved["content_hash"] == proposal["content_hash"]
    exported = client.post(f"{path}/export", json=export_request)
    assert exported.status_code == 200, exported.text
    assert exported.headers["content-type"].startswith("text/csv")
    assert "ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ" in exported.text
    replay = client.post(f"{path}/export", json=export_request)
    assert replay.status_code == 200
    assert replay.content == exported.content
    assert replay.headers["content-disposition"] == exported.headers["content-disposition"]
    rows = list(csv.reader(io.StringIO(exported.text)))
    assert len(rows) == sum(Decimal(line["selected_purchase_qty"]) > 0 for line in proposal["lines"]) + 2
    assert all(row[0] == "synthetic_demo" and row[10] == "approved" for row in rows[2:])

    line = proposal["lines"][0]
    amount = Decimal(line["selected_purchase_qty"]) + Decimal(line["pack_multiple_purchase"])
    amount = max(amount, Decimal(line["moq_purchase"]))
    change = {"expected_version": 1, "edits": [{"line_id": line["line_id"], "purchase_qty": str(amount)}], "reason": "Подтверждён дополнительный проект"}
    edited = client.patch(path, json=change)
    assert edited.status_code == 200, edited.text
    latest = edited.json()
    assert latest["version"] == 2 and latest["status"] == "draft"
    assert latest["content_hash"] != proposal["content_hash"]
    edited_line = next(item for item in latest["lines"] if item["line_id"] == line["line_id"])
    assert edited_line["recommended_purchase_qty"] == line["recommended_purchase_qty"]
    assert Decimal(edited_line["selected_purchase_qty"]) == amount
    assert any(part["code"] == "manual_override_delta" for part in edited_line["explanation"])
    assert sum(Decimal(part["delta_base_qty"]) for part in edited_line["explanation"]) == Decimal(edited_line["selected_base_qty"])
    assert client.post(f"{path}/export", json=export_request).status_code == 409
    assert client.post(f"{path}/export", json={"expected_version": 2, "idempotency_key": "csv-v2"}).status_code == 403
    assert client.patch(path, json=change).status_code == 409
    assert client.post(f"{path}/approve", json={"expected_version": 1, "content_hash": proposal["content_hash"]}).status_code == 409
    assert client.get(path, params={"version": 1}).json()["lines"] == proposal["lines"]
    assert client.post(f"{path}/approve", json={"expected_version": 2, "content_hash": latest["content_hash"]}).status_code == 200
    second_export = client.post(f"{path}/export", json={"expected_version": 2, "idempotency_key": "csv-v2"})
    assert second_export.status_code == 200, second_export.text
    assert second_export.content != exported.content


def test_invalid_edits_leave_proposal_unchanged(client):
    _, _, proposals = build_run(client)
    proposal = proposals[0]
    path = f"/v1/proposals/{proposal['proposal_id']}"
    line = next(item for item in proposal["lines"] if Decimal(item["pack_multiple_purchase"]) > 1)
    cases = [
        {"expected_version": 1, "edits": [{"line_id": "unknown-line", "purchase_qty": "10"}], "reason": "test"},
        {"expected_version": 1, "edits": [{"line_id": line["line_id"], "purchase_qty": "1"}], "reason": "test"},
        {"expected_version": 1, "edits": [{"line_id": line["line_id"], "purchase_qty": "-1"}], "reason": "test"},
        {"expected_version": 1, "edits": [{"line_id": line["line_id"], "purchase_qty": "10"}], "reason": ""},
        {"expected_version": 1, "edits": [], "reason": "test"},
    ]
    for request in cases:
        rejected = client.patch(path, json=request)
        assert rejected.status_code == 422, rejected.text
        assert rejected.json()["code"]
        assert client.get(path).json() == proposal


def test_manual_edit_cannot_exceed_shared_run_budget(client):
    _, _, proposals = build_run(client, policy={
        "service_target": 0.95, "currency": "KZT", "budget_cap": "30000",
    })
    assert sum(Decimal(p["total_cost"]) for p in proposals) <= Decimal("30000")
    proposal = next(p for p in proposals if p["lines"])
    line = proposal["lines"][0]
    amount = Decimal(line["pack_multiple_purchase"]) * 100000
    rejected = client.patch(f"/v1/proposals/{proposal['proposal_id']}", json={
        "expected_version": 1, "edits": [{"line_id": line["line_id"], "purchase_qty": str(amount)}],
        "reason": "Проверка общего лимита",
    })
    assert rejected.status_code == 422, rejected.text
    assert rejected.json()["code"] == "BUDGET_EXCEEDED"
    for original in proposals:
        assert client.get(f"/v1/proposals/{original['proposal_id']}").json() == original


def test_scenarios_are_isolated_and_budget_is_global(client):
    _, run, proposals = build_run(client)
    before = {p["proposal_id"]: p for p in proposals}
    budget = "30000"
    request = {"base_run_id": run["id"], "overrides": {"service_target": 0.99, "budget_cap": budget}, "seed": 42, "idempotency_key": "scenario-budget"}
    started = client.post("/v1/scenarios", json=request)
    assert started.status_code == 202, started.text
    scenario = wait_for_job(client, started.json()["status_url"])
    assert scenario["status"] == "succeeded", scenario
    assert scenario["base_run_id"] == run["id"]
    assert scenario["assumptions"]
    # A single run-wide budget must cover both suppliers together.
    assert Decimal(scenario["summary"]["scenario_total_cost"]) <= Decimal(budget)
    assert scenario["summary"]["currency"] == "KZT"
    assert {p["proposal_id"] for p in client.get("/v1/proposals").json()["items"]} == set(before)
    for proposal_id, original in before.items():
        assert client.get(f"/v1/proposals/{proposal_id}").json() == original
    repeat = client.post("/v1/scenarios", json=request)
    assert repeat.status_code == 202 and repeat.json()["scenario_id"] == started.json()["scenario_id"]
    changed = {**request, "overrides": {"budget_cap": "10000"}}
    assert client.post("/v1/scenarios", json=changed).status_code == 409
    assert client.post(f"/v1/scenarios/{started.json()['scenario_id']}/approve", json={}).status_code in {404, 405}
    assert client.post(f"/v1/scenarios/{started.json()['scenario_id']}/export", json={}).status_code in {404, 405}


def test_scenario_baseline_is_original_run_even_after_buyer_edit(client):
    _, run, proposals = build_run(client)
    original_total = sum(Decimal(p["total_cost"]) for p in proposals)
    proposal = proposals[0]
    line = proposal["lines"][0]
    increased = Decimal(line["selected_purchase_qty"]) + Decimal(line["pack_multiple_purchase"]) * 10
    edited = client.patch(f"/v1/proposals/{proposal['proposal_id']}", json={
        "expected_version": 1,
        "edits": [{"line_id": line["line_id"], "purchase_qty": str(increased)}],
        "reason": "Дополнительная потребность после расчёта",
    })
    assert edited.status_code == 200, edited.text
    started = client.post("/v1/scenarios", json={
        "base_run_id": run["id"], "overrides": {"service_target": 0.99},
        "seed": 42, "idempotency_key": "original-run-comparison",
    })
    assert started.status_code == 202
    scenario = wait_for_job(client, started.json()["status_url"])
    assert scenario["status"] == "succeeded", scenario
    assert Decimal(scenario["summary"]["baseline_total_cost"]) == original_total
    current = client.get(f"/v1/proposals/{proposal['proposal_id']}").json()
    assert current == edited.json()


def test_request_validation_unknown_sources_and_cursor(client):
    assert client.get("/v1/health").status_code == 200
    assert client.get("/v1/proposals/absent").status_code == 404
    unknown = {**SNAPSHOT_REQUEST, "source_ids": ["/arbitrary/private/file.xlsx"]}
    assert client.post("/v1/snapshots", json=unknown).status_code == 422
    mixed = {**SNAPSHOT_REQUEST, "mode": "real_preview"}
    assert client.post("/v1/snapshots", json=mixed).status_code == 422
    assert client.get("/v1/proposals", params={"cursor": "-1"}).status_code == 422
    assert client.get("/v1/proposals", params={"limit": 201}).status_code == 422


def test_missing_real_import_provider_is_explicit_failure(tmp_path, monkeypatch):
    import ekt.api.service as service_module
    from ekt.api.app import create_app
    config = tmp_path / "sources.json"
    config.write_text(json.dumps({"sources": [{"source_id": "real-source", "name": "Real workbook", "kind": "xlsx", "mode": "real_preview", "path": str(tmp_path / "private.xlsx")}]}))
    monkeypatch.setenv("EKT_SOURCE_CONFIG", str(config))
    original_import = service_module.importlib.import_module

    def missing_data(name, *args, **kwargs):
        if name == "ekt.data":
            raise ModuleNotFoundError(name, name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(service_module.importlib, "import_module", missing_data)
    with TestClient(create_app(data_dir=tmp_path / "state")) as client:
        sources = client.get("/v1/sources")
        assert str(tmp_path) not in sources.text
        accepted = client.post("/v1/snapshots", json={**SNAPSHOT_REQUEST, "source_ids": ["real-source"], "mode": "real_preview"})
        assert accepted.status_code == 202, accepted.text
        job = wait_for_job(client, accepted.json()["status_url"])
        assert job["status"] == "failed"
        assert job["error"]["code"] == "DATA_PROVIDER_NOT_CONNECTED"
        assert client.get("/v1/proposals").json()["items"] == []


def test_repeating_synthetic_import_does_not_break_the_next_demo_run(client):
    first_request, first_run, first_proposals = build_run(client, key="first-demo")
    second_request, second_run, second_proposals = build_run(client, key="second-demo")
    assert first_request["snapshot_id"] == second_request["snapshot_id"]
    assert first_run["id"] != second_run["id"]
    assert {p["proposal_id"] for p in first_proposals}.isdisjoint(
        {p["proposal_id"] for p in second_proposals}
    )
    for proposal in first_proposals:
        assert client.get(f"/v1/proposals/{proposal['proposal_id']}").json() == proposal


def test_default_missing_forecast_uses_disclosed_synthetic_fixture_only(tmp_path, monkeypatch):
    import ekt.api.service as service_module
    from ekt.api.app import create_app
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    original_import = service_module.importlib.import_module

    def missing_forecast(name, *args, **kwargs):
        if name == "ekt.forecast":
            raise ModuleNotFoundError(name, name=name)
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(service_module.importlib, "import_module", missing_forecast)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        _, run, proposals = build_run(client)
        assert run["status"] == "succeeded"
        assert any(
            warning["code"] == "FORECAST_PROVIDER_NOT_CONNECTED"
            for proposal in proposals for line in proposal["lines"] for warning in line["warnings"]
        )
        assert all(p["mode"] == "synthetic_demo" for p in proposals)


def test_forecast_failure_is_retryable_and_publishes_no_partial_proposals(tmp_path, monkeypatch):
    from ekt.api.app import create_app
    from ekt.contracts import DomainError
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)

    def fail_forecast(snapshot, request):
        raise DomainError("FORECAST_UNAVAILABLE", "Провайдер временно недоступен", retryable=True)

    with TestClient(create_app(data_dir=tmp_path, forecast_provider=fail_forecast)) as client:
        accepted = client.post("/v1/snapshots", json=SNAPSHOT_REQUEST)
        job = wait_for_job(client, accepted.json()["status_url"])
        assert job["status"] == "succeeded", job
        started = client.post("/v1/planning-runs", json={
            "snapshot_id": job["result_ref"], "policy": {"service_target": 0.95},
            "idempotency_key": "provider-failed",
        })
        failed = wait_for_job(client, started.json()["status_url"])
        assert failed["status"] == "failed"
        assert failed["error"]["code"] == "FORECAST_UNAVAILABLE"
        assert failed["error"]["message"] == "Провайдер временно недоступен"
        assert failed["error"]["retryable"] is True
        assert client.get("/v1/proposals").json()["items"] == []


def test_nonapprover_cannot_approve_or_export_even_with_valid_payload(tmp_path, monkeypatch):
    from ekt.api.app import create_app
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    monkeypatch.setenv("EKT_DEMO_ROLE", "planner")
    with TestClient(create_app(data_dir=tmp_path, forecast_provider=integration_forecast)) as client:
        _, _, proposals = build_run(client)
        proposal = proposals[0]
        path = f"/v1/proposals/{proposal['proposal_id']}"
        approval = client.post(f"{path}/approve", json={
            "expected_version": 1, "content_hash": proposal["content_hash"],
        })
        assert approval.status_code == 403
        export = client.post(f"{path}/export", json={"expected_version": 1, "idempotency_key": "forbidden"})
        assert export.status_code == 403
        assert client.get(path).json()["status"] == "draft"


def test_budget_scenario_infers_unique_currency_from_default_policy(client):
    _, run, proposals = build_run(client, policy={"service_target": 0.95})
    assert {proposal["currency"] for proposal in proposals} == {"KZT"}
    started = client.post("/v1/scenarios", json={
        "base_run_id": run["id"], "overrides": {"budget_cap": "30000"},
        "seed": 42, "idempotency_key": "budget-with-inferred-currency",
    })
    assert started.status_code == 202, started.text
    scenario = wait_for_job(client, started.json()["status_url"])
    assert scenario["status"] == "succeeded", scenario
    assert scenario["summary"]["currency"] == "KZT"
    assert Decimal(scenario["summary"]["scenario_total_cost"]) <= Decimal("30000")


def test_reusing_forecast_id_for_different_content_fails_without_partial_publication(tmp_path, monkeypatch):
    from ekt.api.app import create_app
    from ekt.contracts import ForecastArtifact
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    calls = 0

    def inconsistent_provider(snapshot, request):
        nonlocal calls
        calls += 1
        artifact = integration_forecast(snapshot, request)
        if calls == 1:
            return artifact
        data = artifact.model_dump(mode="json")
        # The artifact is individually schema-valid but illegally reuses the
        # original immutable ID for a materially different forecast.
        for day in data["series"][0]["daily"]:
            day["baseline_mean"] += 10
            day["mean"] += 10
        return ForecastArtifact.model_validate(data)

    with TestClient(create_app(data_dir=tmp_path, forecast_provider=inconsistent_provider)) as client:
        request, first_run, original = build_run(client)
        accepted = client.post("/v1/planning-runs", json={
            **request, "idempotency_key": "same-id-different-content",
        })
        assert accepted.status_code == 202
        failed = wait_for_job(client, accepted.json()["status_url"])
        assert failed["status"] == "failed", failed
        assert failed["error"]["code"] not in {None, "INTERNAL_ERROR"}
        assert client.get("/v1/proposals", params={"run_id": accepted.json()["run_id"]}).json()["items"] == []
        assert client.get(f"/v1/planning-runs/{first_run['id']}").json()["status"] == "succeeded"
        for proposal in original:
            assert client.get(f"/v1/proposals/{proposal['proposal_id']}").json() == proposal


def test_parquet_demand_event_decimals_are_exact_json_strings(tmp_path, monkeypatch):
    from ekt.api.app import create_app
    from ekt.contracts import ForecastArtifact, write_table
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    quantities = {
        "observed_qty": "9007199254740993.125",
        "regular_qty": "0.125", "project_qty": "9007199254740993.000",
        "uncertain_qty": "0.000",
    }

    def provider_with_decimal_events(snapshot, request):
        artifact = integration_forecast(snapshot, request)
        record = {
            "event_id": "decimal-event", "sku_id": "DEMO-001", "warehouse_id": "ALA-DEMO",
            "label": "project", "reason_codes": ["buyer_confirmed"],
            "confidence": 1.0, "review_status": "confirmed",
            **{key: Decimal(value) for key, value in quantities.items()},
        }
        ref = write_table(tmp_path / "events", "classification_decimals", [record])
        data = artifact.model_dump(mode="json")
        data["classifications_ref"] = ref.uri
        return ForecastArtifact.model_validate(data)

    with TestClient(create_app(data_dir=tmp_path, forecast_provider=provider_with_decimal_events)) as client:
        _, run, _ = build_run(client)
        response = client.get(f"/v1/planning-runs/{run['id']}/demand-events", params={"label": "project"})
        assert response.status_code == 200, response.text
        assert len(response.json()["items"]) == 1
        row = response.json()["items"][0]
        for key, expected in quantities.items():
            assert isinstance(row[key], str), f"{key} must be a decimal string, not a JSON number"
            assert Decimal(row[key]) == Decimal(expected)


def test_missing_nested_forecast_dependency_does_not_silently_use_fixture(tmp_path, monkeypatch):
    import ekt.api.service as service_module
    from ekt.api.app import create_app
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    original_import = service_module.importlib.import_module

    def broken_forecast_dependency(name, *args, **kwargs):
        if name == "ekt.forecast":
            # ekt.forecast exists, but importing its own dependency failed.
            # Treating this as "B not connected" would silently hide a defect.
            raise ModuleNotFoundError("No module named some_dependency", name="some_dependency")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(service_module.importlib, "import_module", broken_forecast_dependency)
    with TestClient(create_app(data_dir=tmp_path)) as client:
        snapshot = client.post("/v1/snapshots", json=SNAPSHOT_REQUEST)
        assert snapshot.status_code == 202
        snapshot_job = wait_for_job(client, snapshot.json()["status_url"])
        assert snapshot_job["status"] == "succeeded", snapshot_job
        accepted = client.post("/v1/planning-runs", json={
            "snapshot_id": snapshot_job["result_ref"],
            "policy": {"service_target": 0.95}, "idempotency_key": "broken-dependency",
        })
        assert accepted.status_code == 202
        failed = wait_for_job(client, accepted.json()["status_url"])
        assert failed["status"] == "failed", failed
        assert failed["error"]["code"] != "FORECAST_PROVIDER_NOT_CONNECTED"
        assert client.get("/v1/proposals").json()["items"] == []
