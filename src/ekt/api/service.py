"""Durable single-worker MVP orchestration; no supplier transmission."""

from __future__ import annotations

import csv
import hashlib
import importlib
import io
import json
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from ekt.contracts import ForecastArtifact, ForecastRequest, PlanningPolicy, ProposalDetail, SnapshotManifest
from ekt.storage import Store

log = logging.getLogger(__name__)


class ServiceError(Exception):
    def __init__(self, code, message, status=422, details=None, retryable=False):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status
        self.details, self.retryable = details or {}, retryable


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def proposal_hash(value):
    return digest({k: v for k, v in value.items() if k not in {"content_hash", "status", "capabilities", "updated_at"}})


def artifact_payload(value):
    return {k: v for k, v in value.items() if k != "version"}


def public_snapshot(value):
    """Expose identities and checksums without machine-specific file paths."""
    data = json.loads(json.dumps(artifact_payload(value)))
    for source in data.get("source_refs", []):
        source.pop("local_ref", None)
    for name, table in data.get("tables", {}).items():
        table["uri"] = f"artifact://{data['snapshot_id']}/{name}"
    return data


class Service:
    def __init__(self, root, *, forecast_provider=None, snapshot_provider=None):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.store = Store(self.root / "state.sqlite3")
        self.forecast_provider = forecast_provider
        self.snapshot_provider = snapshot_provider
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="ekt-planning")
        self.store.recover_running_jobs()
        self.actor = os.environ.get("EKT_DEMO_ACTOR", "demo-buyer")
        self.role = os.environ.get("EKT_DEMO_ROLE", "approver")
        self.sources = self._sources()

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=False)

    def _sources(self):
        sources = {"synthetic-demo": {"source_id": "synthetic-demo", "kind": "synthetic", "name": "Синтетические данные для проверки интеграции", "mode": "synthetic_demo"}}
        config = os.environ.get("EKT_SOURCE_CONFIG")
        if config:
            payload = json.loads(Path(config).read_text())
            for source in payload.get("sources", []):
                sources[source["source_id"]] = source
        return sources

    def list_sources(self):
        return {"items": [{k: v for k, v in value.items() if k not in {"path", "local_ref", "credentials"}} for value in self.sources.values()]}

    def _require(self, kind, id, version=None):
        result = self.store.get_item(kind, id, version=version)
        if result is None:
            raise ServiceError("NOT_FOUND", "Объект не найден", 404)
        return result

    def job(self, id):
        return self._require("jobs", id)

    def _update_job(self, id, **changes):
        current = self.job(id)
        changes["updated_at"] = now()
        return self.store.mutate_item("jobs", id, current["version"], lambda value: {**value, **changes})

    def _new_job(self, kind, payload, key=None):
        id = f"{kind}-{uuid4().hex}"
        with self.store.transaction() as tx:
            if key:
                reserved = tx.reserve_idempotency(kind, key, digest(payload), id)
                if not reserved["created"]:
                    return reserved["result_id"], False
            tx.create_item("jobs", id, {"id": id, "kind": kind, "status": "queued", "stage": "queued", "progress": 0.0, "created_at": now(), "updated_at": now(), "error": None, "result_ref": None, "request": payload, "proposal_ids": [], "snapshot_id": payload.get("snapshot_id"), "mode": payload.get("mode"), **({"base_run_id": payload["base_run_id"]} if kind == "scenario" else {})})
        return id, True

    def _guarded(self, id, action):
        try:
            self._update_job(id, status="running", stage="loading", progress=0.05)
            action()
        except Exception as exc:
            log.exception("Planning job %s failed", id)
            expected = hasattr(exc, "code")
            self._update_job(id, status="failed", stage="failed", error={"code": getattr(exc, "code", "INTERNAL_ERROR"), "message": getattr(exc, "message", "Ошибка выполнения. Проверьте журнал backend."), "details": getattr(exc, "details", {}) if expected else {}, "retryable": getattr(exc, "retryable", False)})

    def start_snapshot(self, request):
        ids = request["source_ids"]
        if not ids or any(id not in self.sources for id in ids):
            raise ServiceError("UNKNOWN_SOURCE", "Выберите зарегистрированный источник данных")
        if "synthetic-demo" in ids and (ids != ["synthetic-demo"] or request["mode"] != "synthetic_demo"):
            raise ServiceError("MIXED_PROVENANCE", "Синтетические источники требуют отдельного демонстрационного снимка")
        id, _ = self._new_job("snapshot", request)
        self.executor.submit(self._guarded, id, lambda: self._build_snapshot(id, request))
        return {"job_id": id, "status_url": f"/v1/jobs/{id}"}

    def _build_snapshot(self, id, request):
        if request["source_ids"] == ["synthetic-demo"]:
            from ekt.demo import create_demo_snapshot
            snapshot = create_demo_snapshot(self.root / "artifacts" / id)
        else:
            provider = self.snapshot_provider
            if provider is None:
                try:
                    provider = importlib.import_module("ekt.data").build_snapshot
                except ModuleNotFoundError as exc:
                    if exc.name != "ekt.data":
                        raise
                    raise ServiceError("DATA_PROVIDER_NOT_CONNECTED", "Модуль импорта участника B ещё не подключён. Доступен synthetic-demo.") from None
            from ekt.contracts import MappingConfig, SourceManifest
            refs = []
            for sid in request["source_ids"]:
                source = self.sources[sid]
                path = Path(source.get("local_ref", source.get("path", "")))
                if not path.is_file():
                    raise ServiceError("SOURCE_NOT_AVAILABLE", "Зарегистрированный файл недоступен", details={"source_id": sid})
                refs.append({"source_id": sid, "kind": source["kind"], "local_ref": str(path.resolve()), "checksum": hashlib.sha256(path.read_bytes()).hexdigest()})
            manifest = SourceManifest.model_validate({"source_ids": request["source_ids"], "source_refs": refs, "mode": request["mode"], "as_of": request["as_of"]})
            mapping = MappingConfig.model_validate({"mapping_version": request["mapping_version"], "options": {"output_root": str(self.root / "artifacts" / id)}})
            snapshot = provider(manifest, mapping)
        snapshot = SnapshotManifest.model_validate(snapshot)
        # Demo generator owns its fixed time. It is explicitly replay data, not live stock.
        data = snapshot.model_dump(mode="json")
        with self.store.transaction() as tx:
            existing = tx.get_item("snapshots", snapshot.snapshot_id)
            if existing is None:
                tx.create_item("snapshots", snapshot.snapshot_id, data)
            elif any(existing[key] != data[key] for key in ("manifest_hash", "as_of", "mode", "mapping_version")):
                raise ServiceError("SNAPSHOT_ID_COLLISION", "ID снимка уже соответствует другому содержимому", 409)
        self._update_job(id, status="succeeded", stage="complete", progress=1.0, result_ref=snapshot.snapshot_id, snapshot_id=snapshot.snapshot_id, mode=snapshot.mode, quality=data["quality"])

    def start_run(self, request):
        self._require("snapshots", request["snapshot_id"])
        PlanningPolicy.model_validate(request["policy"])
        id, created = self._new_job("run", request, request["idempotency_key"])
        if created:
            self.executor.submit(self._guarded, id, lambda: self._run(id, request))
        return {"run_id": id, "status_url": f"/v1/planning-runs/{id}"}

    def _forecast(self, snapshot, run_id):
        provider = self.forecast_provider
        if provider is None:
            try:
                provider = importlib.import_module("ekt.forecast").build_forecast
            except ModuleNotFoundError as exc:
                if exc.name != "ekt.forecast":
                    raise
                if snapshot.mode != "synthetic_demo":
                    raise ServiceError("FORECAST_PROVIDER_NOT_CONNECTED", "Модуль прогнозирования B не подключён") from None
                from ekt.demo import fixture_forecast
                # An explicitly labelled upstream fixture exercises real inventory/API logic.
                return fixture_forecast(snapshot)
        request = ForecastRequest(run_id=run_id, as_of=snapshot.as_of, sku_ids=[], warehouse_ids=[], horizon_days=90, seed=42, growth_overrides=[], review_overrides=[])
        return ForecastArtifact.model_validate(provider(snapshot, request))

    def _run(self, id, request):
        from ekt.planning import build_proposals
        snapshot = SnapshotManifest.model_validate(artifact_payload(self._require("snapshots", request["snapshot_id"])))
        self._update_job(id, stage="forecast", progress=0.2)
        forecast = self._forecast(snapshot, id)
        self._update_job(id, stage="planning", progress=0.65)
        policy = PlanningPolicy.model_validate(request["policy"])
        proposals = build_proposals(snapshot, forecast, policy, id)
        forecast_data = forecast.model_dump(mode="json")
        values = []
        for proposal in proposals:
            data = proposal.model_dump(mode="json")
            data["version"] = 1
            data["capabilities"]["can_export"] = False
            if data["mode"] == "real_preview":
                data["capabilities"]["can_approve"] = False
                data["capabilities"]["reasons"].append("Real orders require ERP freshness checks and verified buyer identity")
            data["content_hash"] = proposal_hash(data)
            values.append(data)
        # Completed forecast/proposals become visible together.
        with self.store.transaction() as tx:
            existing = tx.get_item("forecasts", forecast.forecast_id)
            if existing is None:
                tx.create_item("forecasts", forecast.forecast_id, forecast_data)
            elif digest(artifact_payload(existing)) != digest(forecast_data):
                raise ServiceError("FORECAST_ID_COLLISION", "ID прогноза уже соответствует другому содержимому", 409)
            for data in values:
                tx.create_item("proposals", data["proposal_id"], data)
            current = tx.get_item("jobs", id)
            tx.mutate_item("jobs", id, current["version"], lambda value: {**value, "status": "succeeded", "stage": "complete", "progress": 1.0, "forecast_id": forecast.forecast_id, "proposal_ids": [data["proposal_id"] for data in values], "quality": snapshot.quality.model_dump(mode="json"), "model_version": forecast.model_version, "mode": snapshot.mode, "as_of": snapshot.as_of.isoformat(), "seed": forecast.seed, "policy": policy.model_dump(mode="json"), "snapshot_manifest_hash": snapshot.manifest_hash, "forecast_hash": digest(forecast_data), "result_ref": id, "updated_at": now()})

    def list_proposals(self, run_id=None, supplier_id=None, limit=50, cursor=None):
        values = self.store.list_items("proposals")
        values = [value for value in values if (not run_id or value["run_id"] == run_id) and (not supplier_id or value["supplier_id"] == supplier_id)]
        values.sort(key=lambda value: value["proposal_id"])
        try:
            start = int(cursor or 0)
            if start < 0:
                raise ValueError
        except ValueError:
            raise ServiceError("INVALID_CURSOR", "Некорректный cursor") from None
        end = start + limit
        from ekt.contracts import ProposalSummary
        summaries = []
        for value in values[start:end]:
            payload = {key: value[key] for key in ProposalSummary.model_fields if key in value}
            payload.update(line_count=len(value["lines"]), excluded_count=len(value["excluded_lines"]))
            summaries.append(ProposalSummary.model_validate(payload).model_dump(mode="json"))
        return {"items": summaries, "next_cursor": str(end) if end < len(values) else None}

    def proposal(self, id, version=None):
        return self._require("proposals", id, version)

    def _ensure_role(self):
        if self.role != "approver":
            raise ServiceError("FORBIDDEN", "Для действия требуется роль approver", 403)

    def edit(self, id, request):
        from ekt.planning import revalidate_line_quantity
        edits = {edit["line_id"]: Decimal(edit["purchase_qty"]) for edit in request["edits"]}
        if len(edits) != len(request["edits"]):
            raise ServiceError("DUPLICATE_EDIT", "Одна строка указана несколько раз")

        def mutate(data):
            known = {line["line_id"] for line in data["lines"]}
            if not set(edits).issubset(known):
                raise ServiceError("UNKNOWN_LINE", "Строка не найдена в предложении")
            for line in data["lines"]:
                if line["line_id"] not in edits:
                    continue
                from ekt.contracts import ProposalLine
                quantity = edits[line["line_id"]]
                base = revalidate_line_quantity(ProposalLine.model_validate(line), quantity)
                line["selected_purchase_qty"], line["selected_base_qty"] = str(quantity), str(base)
                line["explanation"] = [part for part in line["explanation"] if part["code"] != "manual_override_delta"]
                delta = base - sum((Decimal(part["delta_base_qty"]) for part in line["explanation"]), Decimal(0))
                line["explanation"].append({"code": "manual_override_delta", "label": "Корректировка закупщика", "delta_base_qty": str(delta), "source_refs": [], "note": request["reason"]})
                line["line_cost"] = str(base * Decimal(line["unit_cost"])) if line["unit_cost"] is not None else None
            data["total_cost"] = str(sum((Decimal(line["line_cost"]) for line in data["lines"]), Decimal(0))) if data["lines"] and all(line["line_cost"] is not None for line in data["lines"]) else None
            run = self.job(data["run_id"])
            budget = run["request"]["policy"].get("budget_cap")
            if budget is not None:
                other = [p for p in self.store.list_items("proposals") if p["run_id"] == data["run_id"] and p["proposal_id"] != id]
                if data["total_cost"] is None or any(p["total_cost"] is None for p in other):
                    raise ServiceError("MISSING_COSTS", "Нельзя проверить бюджет без стоимости всех строк")
                total = Decimal(data["total_cost"]) + sum((Decimal(p["total_cost"]) for p in other), Decimal(0))
                if total > Decimal(budget):
                    raise ServiceError("BUDGET_EXCEEDED", "Изменение превышает бюджет всего расчёта")
            data["status"] = "draft"
            data["capabilities"]["can_export"] = False
            data["version"] = request["expected_version"] + 1
            data["content_hash"] = proposal_hash(data)
            ProposalDetail.model_validate(data)
            return data

        return self.store.mutate_proposal(id, request["expected_version"], mutate, audit_event={"action": "edit", "actor": self.actor, "reason": request["reason"]})

    def _validate_approvable(self, data):
        if not data["capabilities"]["can_approve"] or not data["lines"]:
            raise ServiceError("APPROVAL_BLOCKED", "Предложение нельзя утвердить", details={"reasons": data["capabilities"].get("reasons", [])})
        if any(issue["severity"] == "blocking" for issue in data.get("warnings", [])):
            raise ServiceError("DATA_BLOCKED", "Есть блокирующие замечания к данным")
        if data["mode"] == "real_preview":
            # MVP has no live ERP freshness verification or enterprise identity.
            raise ServiceError("REAL_EXPORT_NOT_ENABLED", "MVP поддерживает утверждённый демонстрационный CSV; для реального заказа нужна проверка ERP")
        from ekt.planning import revalidate_line_quantity
        from ekt.contracts import ProposalLine
        for line in data["lines"]:
            revalidate_line_quantity(ProposalLine.model_validate(line), Decimal(line["selected_purchase_qty"]))
            total = sum((Decimal(part["delta_base_qty"]) for part in line["explanation"]), Decimal(0))
            if total != Decimal(line["selected_base_qty"]):
                raise ServiceError("LEDGER_MISMATCH", "Объяснение количества не сходится")

    def approve(self, id, request):
        self._ensure_role()
        approval_id = f"approval-{uuid4().hex}"

        def mutate(data):
            if request["content_hash"] != data["content_hash"] or proposal_hash(data) != data["content_hash"]:
                raise ServiceError("STALE_CONTENT", "Предложение изменилось, обновите экран", 409)
            self._validate_approvable(data)
            data["status"] = "approved"
            data["capabilities"]["can_export"] = True
            return data

        result = self.store.mutate_proposal(id, request["expected_version"], mutate, audit_event={"action": "approve", "approval_id": approval_id, "actor": self.actor, "content_hash": request["content_hash"]}, bump_version=False)
        return {"approval_id": approval_id, "proposal_id": id, "version": result["version"], "status": "approved"}

    def export(self, id, request):
        self._ensure_role()
        with self.store.transaction() as tx:
            data = tx.get_item("proposals", id)
            if data is None:
                raise ServiceError("NOT_FOUND", "Предложение не найдено", 404)
            if data["version"] != request["expected_version"]:
                raise ServiceError("STALE_VERSION", "Версия предложения изменилась", 409)
            if data["status"] != "approved":
                raise ServiceError("NOT_APPROVED", "Сначала утвердите текущую версию", 403)
            if proposal_hash(data) != data["content_hash"]:
                raise ServiceError("STALE_CONTENT", "Содержимое предложения изменилось", 409)
            self._validate_approvable(data)
            reserved = tx.reserve_idempotency("export", request["idempotency_key"], digest({"id": id, "version": data["version"], "hash": data["content_hash"]}), f"export-{uuid4().hex}")
            export_id = reserved["result_id"]
            if not reserved["created"]:
                saved = tx.get_item("exports", export_id)
                return saved["csv"], saved["filename"]
            stream = io.StringIO(newline="")
            writer = csv.writer(stream)
            writer.writerow(["ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ"])
            writer.writerow(["mode", "as_of", "supplier_id", "sku_id", "purchase_qty", "purchase_uom", "base_qty", "base_uom", "proposal_id", "version", "status", "explanation"])
            for line in data["lines"]:
                cells = [data["mode"], data["as_of"], data["supplier_id"], line["sku_id"], line["selected_purchase_qty"], line["purchase_uom"], line["selected_base_qty"], line["base_uom"], id, str(data["version"]), "approved", json.dumps(line["explanation"], ensure_ascii=False)]
                # Spreadsheet programs must not interpret supplied identifiers as formulas.
                writer.writerow(["'" + cell if str(cell).startswith(("=", "+", "-", "@", "\t", "\r")) else cell for cell in cells])
            text = stream.getvalue()
            filename = f"demo-{id}-v{data['version']}.csv"
            tx.create_item("exports", export_id, {"csv": text, "filename": filename, "proposal_id": id, "proposal_version": data["version"], "actor": self.actor, "created_at": now()})
            tx.append_audit("proposals", id, "export", {"action": "export", "actor": self.actor, "export_id": export_id, "version": data["version"]})
            return text, filename

    def start_scenario(self, request):
        base = self.job(request["base_run_id"])
        if base["status"] != "succeeded" or base.get("kind") != "run":
            raise ServiceError("BASE_RUN_NOT_READY", "Для сравнения нужен завершённый основной расчёт")
        id, created = self._new_job("scenario", request, request["idempotency_key"])
        if created:
            self.executor.submit(self._guarded, id, lambda: self._scenario(id, request, base))
        return {"scenario_id": id, "status_url": f"/v1/scenarios/{id}"}

    def _scenario(self, id, request, base):
        from ekt.planning import build_proposals
        snapshot = SnapshotManifest.model_validate(artifact_payload(self._require("snapshots", base["request"]["snapshot_id"])))
        forecast = ForecastArtifact.model_validate(artifact_payload(self._require("forecasts", base["forecast_id"])))
        params = {**base["request"]["policy"], **{k: v for k, v in request["overrides"].items() if v is not None}}
        originals = [self.proposal(pid, version=1) for pid in base["proposal_ids"]]
        currencies = {p["currency"] for p in originals}
        if params.get("budget_cap") is not None and not params.get("currency"):
            if len(currencies) != 1 or None in currencies:
                raise ServiceError("UNSUPPORTED_BUDGET", "Общий бюджет требует одной известной валюты")
            params["currency"] = next(iter(currencies))
        policy = PlanningPolicy.model_validate(params)
        self._update_job(id, stage="replanning", progress=0.4)
        proposals = build_proposals(snapshot, forecast, policy, id)
        # Keys are business identities; generated line IDs need not match across runs.
        original = {(line["sku_id"], p["warehouse_id"]): line for p in originals for line in p["lines"]}
        changed = []
        for proposal in proposals:
            for item in proposal.lines:
                line = item.model_dump(mode="json")
                before = original.get((line["sku_id"], proposal.warehouse_id))
                changed.append({"sku_id": line["sku_id"], "supplier_id": proposal.supplier_id, "warehouse_id": proposal.warehouse_id, "baseline_base_qty": before["recommended_base_qty"] if before else "0", "scenario_base_qty": line["recommended_base_qty"], "delta_base_qty": str(Decimal(line["recommended_base_qty"]) - Decimal(before["recommended_base_qty"] if before else "0")), "baseline_cost": before["line_cost"] if before else None, "scenario_cost": line["line_cost"]})
        currencies.update(p.currency for p in proposals)
        comparable = len(currencies) == 1 and None not in currencies
        currency = next(iter(currencies)) if comparable else None
        complete = comparable and bool(proposals) and all(p.total_cost is not None for p in proposals)
        base_total = sum((Decimal(p["total_cost"]) for p in originals), Decimal(0)) if comparable and originals and all(p["total_cost"] is not None for p in originals) else None
        scenario_total = sum((p.total_cost for p in proposals), Decimal(0)) if complete else None
        self._update_job(id, status="succeeded", stage="complete", progress=1.0, base_run_id=base["id"], changed_lines=changed, summary={"baseline_total_cost": str(base_total) if base_total is not None else None, "scenario_total_cost": str(scenario_total) if scenario_total is not None else None, "delta_cost": str(scenario_total - base_total) if scenario_total is not None and base_total is not None else None, "currency": currency, "changed_line_count": sum(Decimal(line["delta_base_qty"]) != 0 for line in changed)}, assumptions=["Используется тот же прогноз и снимок. Уровень сервиса целевой, не измеренный.", f"Forecast provider: {forecast.model_version}; seed={forecast.seed} (seed сценария не перегенерирует прогноз); target CSL={policy.service_target}"], result_ref=id)

    def demand_events(self, id, label=None, limit=50, cursor=None):
        run = self.job(id)
        if run["status"] != "succeeded":
            raise ServiceError("RUN_NOT_READY", "Расчёт ещё не завершён")
        forecast = self._require("forecasts", run["forecast_id"])
        path = forecast.get("classifications_ref")
        rows = []
        if path and Path(path).is_file():
            if str(path).endswith(".parquet"):
                import pyarrow.parquet as pq
                rows = pq.read_table(path).to_pylist()
            else:
                rows = json.loads(Path(path).read_text())
        rows = [row for row in rows if label is None or row.get("label") == label]
        try:
            start = int(cursor or 0)
            if start < 0:
                raise ValueError
        except ValueError:
            raise ServiceError("INVALID_CURSOR", "Некорректный cursor") from None
        from ekt.contracts import ClassificationRecord
        return {"items": [ClassificationRecord.model_validate(row).model_dump(mode="json") for row in rows[start:start + limit]], "next_cursor": str(start + limit) if start + limit < len(rows) else None, "diagnostics": {"total": len(rows), "model_version": forecast["model_version"]}}
