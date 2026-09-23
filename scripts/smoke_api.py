"""Exercise a running API through buyer approval and export; synthetic data only.

Run: uv run python scripts/smoke_api.py --url http://127.0.0.1:8000
"""
from __future__ import annotations

import argparse
import json
import time
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import httpx


def wait(client: httpx.Client, path: str, timeout: int = 90) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = client.get(path)
        response.raise_for_status()
        job = response.json()
        if job["status"] == "failed":
            raise RuntimeError(json.dumps(job["error"], ensure_ascii=False))
        if job["status"] == "succeeded":
            return job
        time.sleep(0.1)
    raise TimeoutError(f"Job did not finish: {path}")


def run(url: str, output: Path) -> dict:
    key = uuid4().hex
    with httpx.Client(base_url=url.rstrip("/"), timeout=30) as client:
        health = client.get("/v1/health")
        health.raise_for_status()
        if health.json()["identity_mode"] != "demo" or health.json()["role"] != "approver":
            raise RuntimeError("Smoke workflow requires a local demo approver API")
        assert health.json()["vendor_transmission_enabled"] is False
        created = client.post("/v1/snapshots", json={
            "source_ids": ["synthetic-demo"], "mapping_version": "synthetic-v1",
            "mode": "synthetic_demo", "as_of": "2026-09-23T00:00:00Z",
        })
        created.raise_for_status()
        snapshot_job = wait(client, created.json()["status_url"])
        snapshot_id = snapshot_job["result_ref"]
        accepted = client.post("/v1/planning-runs", json={
            "snapshot_id": snapshot_id,
            "policy": {"service_target": 0.95, "currency": "KZT"},
            "idempotency_key": f"smoke-run-{key}",
        })
        accepted.raise_for_status()
        planned = wait(client, accepted.json()["status_url"])
        proposal_id = planned["proposal_ids"][0]
        path = f"/v1/proposals/{proposal_id}"
        response = client.get(path)
        response.raise_for_status()
        proposal = response.json()
        assert proposal["mode"] == "synthetic_demo"
        assert proposal["capabilities"]["can_export"] is False
        blocked = client.post(f"{path}/export", json={
            "expected_version": proposal["version"], "idempotency_key": f"blocked-{key}",
        })
        assert blocked.status_code == 403, blocked.text
        line = next(line for line in proposal["lines"] if Decimal(line["selected_purchase_qty"]) > 0)
        edited = client.patch(path, json={
            "expected_version": proposal["version"],
            "edits": [{"line_id": line["line_id"], "purchase_qty": str(
                Decimal(line["selected_purchase_qty"]) + Decimal(line["pack_multiple_purchase"])
            )}],
            "reason": "Проверка ручной корректировки на синтетическом демонстрационном наборе",
        })
        edited.raise_for_status()
        proposal = edited.json()
        assert proposal["status"] == "draft" and proposal["version"] == 2
        approval = client.post(f"{path}/approve", json={
            "expected_version": proposal["version"], "content_hash": proposal["content_hash"],
        })
        approval.raise_for_status()
        export = client.post(f"{path}/export", json={
            "expected_version": proposal["version"], "idempotency_key": f"csv-{key}",
        })
        export.raise_for_status()
        assert "ДЕМОНСТРАЦИЯ" in export.text
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(export.content)
        scenario = client.post("/v1/scenarios", json={
            "base_run_id": planned["id"], "overrides": {"service_target": 0.99},
            "seed": 42, "idempotency_key": f"what-if-{key}",
        })
        scenario.raise_for_status()
        compared = wait(client, scenario.json()["status_url"])
        unchanged = client.get(path).json()
        assert unchanged["version"] == 2 and unchanged["status"] == "approved"
        return {
            "status": "passed", "mode": "synthetic_demo", "snapshot_id": snapshot_id,
            "run_id": planned["id"], "model_version": planned["model_version"],
            "supplier_proposals": len(planned["proposal_ids"]),
            "approved_version": proposal["version"], "csv": str(output.resolve()),
            "scenario": compared["summary"],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--output", type=Path, default=Path("var/demo-approved.csv"))
    args = parser.parse_args()
    print(json.dumps(run(args.url, args.output), ensure_ascii=False, indent=2))
