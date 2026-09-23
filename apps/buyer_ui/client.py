"""Contract-only HTTP adapter and explicitly synthetic, in-memory demo transport.

The mock selects prepared responses; it is not a planning engine. In HTTP mode
all business state and CSV content come from the API, with no mock fallback.
"""

from __future__ import annotations

from copy import deepcopy
import csv
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from email.message import Message
import hashlib
from http.client import HTTPException
import io
import json
import math
from pathlib import Path
import socket
from typing import Any, Protocol
from urllib import error, parse, request


JsonObject = dict[str, Any]


class ApiError(Exception):
    """A safe, displayable API failure; ambiguous mutations require reconciliation."""

    def __init__(
        self,
        status_code: int | None,
        code: str,
        message: str,
        details: JsonObject | None = None,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details if isinstance(details, dict) else {}
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class ExportFile:
    data: bytes
    filename: str


class ApiClient(Protocol):
    def health(self) -> JsonObject: ...
    def list_sources(self) -> JsonObject: ...
    def create_snapshot(self, payload: JsonObject) -> JsonObject: ...
    def get_job(self, job_id: str) -> JsonObject: ...
    def get_snapshot(self, snapshot_id: str) -> JsonObject: ...
    def create_planning_run(self, payload: JsonObject) -> JsonObject: ...
    def get_planning_run(self, run_id: str) -> JsonObject: ...
    def list_proposals(self, *, run_id: str | None = None, supplier_id: str | None = None,
                       cursor: str | None = None, limit: int = 50) -> JsonObject: ...
    def get_proposal(self, proposal_id: str, version: int | None = None) -> JsonObject: ...
    def edit_proposal(self, proposal_id: str, payload: JsonObject) -> JsonObject: ...
    def approve_proposal(self, proposal_id: str, payload: JsonObject) -> JsonObject: ...
    def export_proposal(self, proposal_id: str, payload: JsonObject) -> ExportFile: ...
    def create_scenario(self, payload: JsonObject) -> JsonObject: ...
    def get_scenario(self, scenario_id: str) -> JsonObject: ...
    def list_demand_events(self, run_id: str, *, label: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> JsonObject: ...


class _NoRedirects(request.HTTPRedirectHandler):
    # Never forward a configured bearer token or mutation to another endpoint.
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str,
                         headers: Any, newurl: str) -> None:
        return None


class HttpClient:
    """One bounded urllib request per method. Does not retry mutations itself."""

    def __init__(self, base_url: str, timeout: float = 10, token: str | None = None) -> None:
        parsed = parse.urlsplit(base_url.strip())
        if (parsed.scheme not in {"http", "https"} or not parsed.netloc
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("API URL must be HTTP(S), without credentials, query or fragment")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("API timeout must be a positive finite number")
        if token and ("\n" in token or "\r" in token):
            raise ValueError("Invalid API token")
        self.base_url = base_url.strip().rstrip("/")
        if not self.base_url.endswith("/v1"):
            self.base_url += "/v1"
        self.timeout = timeout
        self._token = token
        self._opener = request.build_opener(_NoRedirects())

    def _safe(self, value: Any) -> Any:
        if isinstance(value, str):
            return value.replace(self._token, "[redacted]") if self._token else value
        if isinstance(value, dict):
            return {str(self._safe(k)): self._safe(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._safe(v) for v in value]
        return value

    def _call(self, method: str, path: str, payload: JsonObject | None = None,
              query: JsonObject | None = None, *, export: bool = False) -> Any:
        url = self.base_url + path
        if query:
            encoded = parse.urlencode({key: val for key, val in query.items() if val is not None})
            if encoded:
                url += "?" + encoded
        headers = {"Accept": "text/csv" if export else "application/json"}
        if self._token:
            headers["Authorization"] = "Bearer " + self._token
        body = None
        if payload is not None:
            try:
                body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
            except (TypeError, ValueError):
                raise ApiError(422, "INVALID_REQUEST", "Запрос содержит недопустимые данные.") from None
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = request.Request(url, data=body, headers=headers, method=method)
        mutation = method not in {"GET", "HEAD"}
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                data = response.read()
                response_headers = response.headers
        except error.HTTPError as exc:
            try:
                content = json.loads(exc.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError, OSError, HTTPException):
                content = {}
            finally:
                exc.close()
            if not isinstance(content, dict):
                content = {}
            # Support FastAPI's standard envelope as well as the contract body.
            if isinstance(content.get("detail"), dict):
                content = content["detail"]
            code = content.get("code")
            message = content.get("message")
            raise ApiError(
                exc.code,
                self._safe(code) if isinstance(code, str) else f"HTTP_{exc.code}",
                self._safe(message) if isinstance(message, str) else f"API вернул ошибку HTTP {exc.code}.",
                self._safe(content.get("details", {})),
                bool(content.get("retryable", exc.code >= 500)),
                ambiguous=mutation and exc.code >= 500,
            ) from None
        except (TimeoutError, socket.timeout):
            raise ApiError(None, "TIMEOUT", "API не ответил за отведённое время.",
                           retryable=True, ambiguous=mutation) from None
        except error.URLError as exc:
            timeout = isinstance(exc.reason, (TimeoutError, socket.timeout))
            raise ApiError(None, "TIMEOUT" if timeout else "NETWORK_ERROR",
                           "API не ответил за отведённое время." if timeout else "Не удалось связаться с API.",
                           retryable=True, ambiguous=mutation) from None
        except (OSError, HTTPException):
            raise ApiError(None, "NETWORK_ERROR", "Соединение с API было прервано.",
                           retryable=True, ambiguous=mutation) from None
        if export:
            if response_headers.get_content_type() != "text/csv":
                raise ApiError(None, "INVALID_RESPONSE", "API не вернул CSV-файл.", ambiguous=mutation)
            disposition = Message()
            disposition["Content-Disposition"] = response_headers.get("Content-Disposition", "")
            filename = disposition.get_filename() or "approved-proposal.csv"
            filename = filename.replace("\\", "/").split("/")[-1]
            filename = "".join(char for char in filename if ord(char) >= 32 and ord(char) != 127)
            if not filename or filename in {".", ".."}:
                filename = "approved-proposal.csv"
            return ExportFile(data=data, filename=filename)
        try:
            result = json.loads(data.decode("utf-8"), parse_constant=self._reject_constant)
        except (ValueError, UnicodeDecodeError):
            raise ApiError(None, "INVALID_RESPONSE", "API вернул некорректный JSON.", ambiguous=mutation) from None
        if not isinstance(result, dict):
            raise ApiError(None, "INVALID_RESPONSE", "Ответ API должен быть JSON-объектом.", ambiguous=mutation)
        return result

    @staticmethod
    def _reject_constant(value: str) -> None:
        raise ValueError("JSON non-finite number")

    @staticmethod
    def _id(value: str) -> str:
        return parse.quote(str(value), safe="")

    def health(self) -> JsonObject:
        return self._call("GET", "/health")

    def list_sources(self) -> JsonObject:
        return self._call("GET", "/sources")

    def create_snapshot(self, payload: JsonObject) -> JsonObject:
        return self._call("POST", "/snapshots", payload)

    def get_job(self, job_id: str) -> JsonObject:
        return self._call("GET", "/jobs/" + self._id(job_id))

    def get_snapshot(self, snapshot_id: str) -> JsonObject:
        return self._call("GET", "/snapshots/" + self._id(snapshot_id))

    def create_planning_run(self, payload: JsonObject) -> JsonObject:
        return self._call("POST", "/planning-runs", payload)

    def get_planning_run(self, run_id: str) -> JsonObject:
        return self._call("GET", "/planning-runs/" + self._id(run_id))

    def list_proposals(self, *, run_id: str | None = None, supplier_id: str | None = None,
                       cursor: str | None = None, limit: int = 50) -> JsonObject:
        return self._call("GET", "/proposals", query=dict(run_id=run_id, supplier_id=supplier_id,
                                                         cursor=cursor, limit=limit))

    def get_proposal(self, proposal_id: str, version: int | None = None) -> JsonObject:
        return self._call("GET", "/proposals/" + self._id(proposal_id), query={"version": version})

    def edit_proposal(self, proposal_id: str, payload: JsonObject) -> JsonObject:
        return self._call("PATCH", "/proposals/" + self._id(proposal_id), payload)

    def approve_proposal(self, proposal_id: str, payload: JsonObject) -> JsonObject:
        return self._call("POST", "/proposals/" + self._id(proposal_id) + "/approve", payload)

    def export_proposal(self, proposal_id: str, payload: JsonObject) -> ExportFile:
        return self._call("POST", "/proposals/" + self._id(proposal_id) + "/export", payload, export=True)

    def create_scenario(self, payload: JsonObject) -> JsonObject:
        return self._call("POST", "/scenarios", payload)

    def get_scenario(self, scenario_id: str) -> JsonObject:
        return self._call("GET", "/scenarios/" + self._id(scenario_id))

    def list_demand_events(self, run_id: str, *, label: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> JsonObject:
        return self._call("GET", "/planning-runs/" + self._id(run_id) + "/demand-events",
                          query=dict(label=label, cursor=cursor, limit=limit))


class MockClient:
    """Temporary fixture state, deliberately isolated from any live API.

    Edits and scenarios select finite, prepared samples from bundle.json. This
    class does not forecast, calculate planning constraints, or claim persistence.
    """

    def __init__(self, quality: str = "ready", fixture_path: str | Path | None = None) -> None:
        if quality not in {"ready", "degraded", "blocked"}:
            raise ValueError("Mock quality must be ready, degraded or blocked")
        path = Path(fixture_path) if fixture_path else Path(__file__).resolve().parents[2] / "assets/demo/ui-fixtures/bundle.json"
        self._fixture = json.loads(path.read_text(encoding="utf-8"))
        self._quality = quality
        self._proposals = {item["proposal_id"]: deepcopy(item) for item in self._fixture["proposals"]}
        self._history: dict[tuple[str, int], JsonObject] = {}
        self._approvals: dict[tuple[str, int], JsonObject] = {}
        self._exports: dict[tuple[str, str], ExportFile] = {}
        self._requests: dict[tuple[str, str], tuple[JsonObject, JsonObject]] = {}
        self._scenarios: dict[str, JsonObject] = {}
        for proposal in self._proposals.values():
            if quality != "ready":
                proposal["capabilities"].update(budget_available=False)
                proposal["currency"] = None
                proposal["total_cost"] = None
                for line in proposal["lines"]:
                    line["unit_cost"] = None
                    line["line_cost"] = None
                proposal["warnings"].append(deepcopy(self._fixture["quality_variants"]["degraded"]["issues"][0]))
            if quality == "blocked":
                proposal["capabilities"] = deepcopy(self._fixture["quality_variants"]["blocked"]["capabilities"])
                proposal["warnings"].extend(deepcopy(self._fixture["quality_variants"]["blocked"]["issues"]))
            self._record(proposal)

    def _record(self, proposal: JsonObject) -> None:
        content = {key: proposal[key] for key in ("proposal_id", "version", "run_id", "snapshot_id", "lines", "excluded_lines")}
        proposal["content_hash"] = hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        self._history[(proposal["proposal_id"], proposal["version"])] = deepcopy(proposal)

    @staticmethod
    def _missing(kind: str) -> ApiError:
        return ApiError(404, "NOT_FOUND", f"Демонстрационный {kind} не найден.")

    def _proposal(self, proposal_id: str, payload: JsonObject | None = None) -> JsonObject:
        if proposal_id not in self._proposals:
            raise self._missing("заказ")
        proposal = self._proposals[proposal_id]
        if payload is not None and payload.get("expected_version") != proposal["version"]:
            raise ApiError(409, "STALE_VERSION", "Версия изменилась. Обновите предложение.",
                           {"current_version": proposal["version"]})
        return proposal

    def _idempotency(self, operation: str, payload: JsonObject) -> tuple[tuple[str, str], JsonObject | None]:
        key = payload.get("idempotency_key")
        if not isinstance(key, str) or not key.strip():
            raise ApiError(422, "IDEMPOTENCY_KEY_REQUIRED", "Укажите ключ попытки.")
        cache_key = (operation, key)
        prior = self._requests.get(cache_key)
        if prior and prior[0] != payload:
            raise ApiError(409, "IDEMPOTENCY_CONFLICT", "Ключ попытки уже использован с другими параметрами.")
        return cache_key, deepcopy(prior[1]) if prior else None

    @staticmethod
    def _page(items: list[JsonObject], cursor: str | None, limit: int) -> JsonObject:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
            raise ApiError(422, "INVALID_LIMIT", "Размер страницы должен быть от 1 до 200.")
        try:
            offset = int(cursor) if cursor is not None else 0
            if offset < 0:
                raise ValueError
        except (TypeError, ValueError):
            raise ApiError(422, "INVALID_CURSOR", "Некорректный курсор страницы.") from None
        end = offset + limit
        return {"items": deepcopy(items[offset:end]), "next_cursor": str(end) if end < len(items) else None}

    def health(self) -> JsonObject:
        return {"status": "ok", "version": "ui-fixture-1.0", "demo_notice": self._fixture["notice"]}

    def list_sources(self) -> JsonObject:
        return deepcopy(self._fixture["sources"])

    def create_snapshot(self, payload: JsonObject) -> JsonObject:
        snapshot = self._fixture["snapshot"]
        if (payload.get("source_ids") != ["demo-source"]
                or payload.get("mapping_version") != snapshot["mapping_version"]
                or payload.get("mode") != "synthetic_demo" or payload.get("as_of") != snapshot["as_of"]):
            raise ApiError(422, "MOCK_UNSUPPORTED_SNAPSHOT", "Mock открывает только подготовленный синтетический снимок.",
                           {"source_ids": ["demo-source"], "mapping_version": snapshot["mapping_version"],
                            "as_of": snapshot["as_of"], "mode": "synthetic_demo"})
        return {"job_id": "demo-snapshot-job", "status_url": "/v1/jobs/demo-snapshot-job"}

    def get_job(self, job_id: str) -> JsonObject:
        if job_id != "demo-snapshot-job":
            raise self._missing("job")
        return deepcopy(self._fixture["snapshot_job"])

    def get_snapshot(self, snapshot_id: str) -> JsonObject:
        if snapshot_id != "demo-snapshot":
            raise self._missing("снимок")
        snapshot = deepcopy(self._fixture["snapshot"])
        snapshot["quality"] = deepcopy(self._fixture["quality_variants"][self._quality])
        return snapshot

    def create_planning_run(self, payload: JsonObject) -> JsonObject:
        cache_key, prior = self._idempotency("planning", payload)
        if prior:
            return prior
        self.get_snapshot(payload.get("snapshot_id", ""))
        if self._quality == "blocked":
            raise ApiError(422, "QUALITY_BLOCKED", "Планирование заблокировано качеством данных.")
        policy = payload.get("policy", {})
        if (policy.get("service_target") != 0.95 or policy.get("lead_time_delay_days", 0) != 0
                or policy.get("budget_cap") is not None or policy.get("service_metric") != "cycle_service"
                or policy.get("review_days") not in (None, 7) or policy.get("max_cover_days") is not None
                or policy.get("currency") not in (None, "KZT")):
            raise ApiError(422, "MOCK_UNSUPPORTED_POLICY", "Доступен подготовленный пример: цель 95%, задержка 0, без бюджета.")
        result = {"run_id": "demo-run", "status_url": "/v1/planning-runs/demo-run"}
        self._requests[cache_key] = (deepcopy(payload), result)
        return deepcopy(result)

    def get_planning_run(self, run_id: str) -> JsonObject:
        if run_id != "demo-run":
            raise self._missing("расчёт")
        result = deepcopy(self._fixture["run"])
        result["quality"] = deepcopy(self._fixture["quality_variants"][self._quality])
        if self._quality == "blocked":
            result.update(status="failed", stage="quality_blocked", proposal_ids=[],
                          error={"code": "QUALITY_BLOCKED", "message": "Обязательные условия отсутствуют.",
                                 "details": {}, "retryable": False})
        return result

    def list_proposals(self, *, run_id: str | None = None, supplier_id: str | None = None,
                       cursor: str | None = None, limit: int = 50) -> JsonObject:
        items = [{key: value for key, value in proposal.items() if key not in {"lines", "excluded_lines"}}
                 | {"line_count": len(proposal["lines"])} for proposal in self._proposals.values()
                 if (run_id is None or proposal["run_id"] == run_id)
                 and (supplier_id is None or proposal["supplier_id"] == supplier_id)]
        return self._page(items, cursor, limit)

    def get_proposal(self, proposal_id: str, version: int | None = None) -> JsonObject:
        proposal = self._proposal(proposal_id)
        if version is None:
            return deepcopy(proposal)
        if (proposal_id, version) not in self._history:
            raise self._missing("версия")
        return deepcopy(self._history[(proposal_id, version)])

    def edit_proposal(self, proposal_id: str, payload: JsonObject) -> JsonObject:
        current = self._proposal(proposal_id, payload)
        reason = payload.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ApiError(422, "REASON_REQUIRED", "Укажите причину изменения.")
        edits = payload.get("edits")
        if not isinstance(edits, list) or len(edits) != 1 or not isinstance(edits[0], dict):
            raise ApiError(422, "MOCK_UNSUPPORTED_EDIT", "В примере редактируется одна строка за раз.")
        edit = edits[0]
        if edit.get("line_id") != current["lines"][0]["line_id"]:
            raise ApiError(422, "UNKNOWN_LINE", "Строка не относится к этому предложению.")
        try:
            if not isinstance(edit.get("purchase_qty"), str):
                raise InvalidOperation
            qty = Decimal(edit["purchase_qty"])
            if not qty.is_finite():
                raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError):
            raise ApiError(422, "INVALID_QUANTITY", "Количество должно быть конечной decimal-строкой.") from None
        variants = self._fixture["edit_variants"][proposal_id]
        variant = next((value for key, value in variants.items() if Decimal(key) == qty), None)
        if variant is None:
            raise ApiError(422, "MOCK_UNSUPPORTED_QUANTITY", "Для mock доступны только подготовленные количества.",
                           {"allowed_purchase_quantities": list(variants)})
        proposal = deepcopy(current)
        proposal["version"] += 1
        proposal["status"] = "draft"
        proposal["capabilities"]["can_export"] = False
        proposal["lines"] = [deepcopy(variant["line"])]
        proposal["total_cost"] = variant["total_cost"]
        proposal["lines"][0]["explanation"][-1]["note"] = reason.strip()
        if self._quality != "ready":
            proposal["total_cost"] = None
            proposal["lines"][0]["unit_cost"] = None
            proposal["lines"][0]["line_cost"] = None
        self._record(proposal)
        self._proposals[proposal_id] = proposal
        return deepcopy(proposal)

    def approve_proposal(self, proposal_id: str, payload: JsonObject) -> JsonObject:
        proposal = self._proposal(proposal_id, payload)
        if payload.get("content_hash") != proposal["content_hash"]:
            raise ApiError(409, "STALE_HASH", "Содержимое изменилось. Обновите предложение.")
        if not proposal["capabilities"]["can_approve"]:
            raise ApiError(422, "QUALITY_BLOCKED", "Обязательные условия отсутствуют; утверждение недоступно.")
        key = (proposal_id, proposal["version"])
        if key not in self._approvals:
            self._approvals[key] = {"approval_id": f"demo-approval-{len(self._approvals) + 1}",
                                    "proposal_id": proposal_id, "version": proposal["version"], "status": "approved"}
        proposal["status"] = "approved"
        proposal["capabilities"]["can_export"] = True
        self._record(proposal)
        return deepcopy(self._approvals[key])

    def export_proposal(self, proposal_id: str, payload: JsonObject) -> ExportFile:
        proposal = self._proposal(proposal_id, payload)
        if proposal["status"] != "approved" or not proposal["capabilities"]["can_export"]:
            raise ApiError(409, "APPROVAL_REQUIRED", "Экспорт доступен только для утверждённой текущей версии.")
        cache_key, prior = self._idempotency("export:" + proposal_id, payload)
        if prior:
            return self._exports[cache_key]
        # This serializer belongs only to the explicit mock transport. The real
        # transport returns server bytes without reconstructing them in the UI.
        output = io.StringIO(newline="")
        writer = csv.writer(output)
        writer.writerow(["ДЕМОНСТРАЦИЯ — НЕ ЗАКАЗ ПОСТАВЩИКУ"])
        writer.writerow([self._fixture["notice"]])
        fields = ["mode", "as_of", "supplier_id", "warehouse_id", "sku_id", "purchase_qty", "purchase_uom",
                  "base_qty", "base_uom", "proposal_id", "version", "approval_id", "approval_status", "explanations"]
        writer.writerow(fields)
        for line in proposal["lines"]:
            writer.writerow([proposal["mode"], proposal["as_of"], proposal["supplier_id"], proposal["warehouse_id"],
                             line["sku_id"], line["selected_purchase_qty"], line["purchase_uom"], line["selected_base_qty"],
                             line["base_uom"], proposal_id, proposal["version"],
                             self._approvals[(proposal_id, proposal["version"])]["approval_id"], "approved",
                             json.dumps(line["explanation"], ensure_ascii=False)])
        result = ExportFile(output.getvalue().encode("utf-8-sig"), f"DEMO-{proposal_id}-v{proposal['version']}.csv")
        self._requests[cache_key] = (deepcopy(payload), {"created": True})
        self._exports[cache_key] = result
        return result

    def create_scenario(self, payload: JsonObject) -> JsonObject:
        cache_key, prior = self._idempotency("scenario", payload)
        if prior:
            return prior
        if payload.get("base_run_id") != "demo-run" or payload.get("seed") != self._fixture["seed"]:
            raise ApiError(422, "MOCK_UNSUPPORTED_BASE", "Для примера нужен demo-run и seed 42.")
        if self._quality == "blocked":
            raise ApiError(422, "QUALITY_BLOCKED", "Базовый расчёт заблокирован.")
        overrides = payload.get("overrides", {})
        if not isinstance(overrides, dict):
            raise ApiError(422, "INVALID_OVERRIDES", "Параметры сценария должны быть объектом.")
        chosen = next((sample for sample in self._fixture["scenarios"]
                       if overrides.get("service_target", 0.95) == sample["overrides"]["service_target"]
                       and overrides.get("lead_time_delay_days", 0) == sample["overrides"]["lead_time_delay_days"]
                       and overrides.get("budget_cap") is None
                       and not set(overrides) - {"service_target", "lead_time_delay_days", "budget_cap"}), None)
        if chosen is None:
            raise ApiError(422, "MOCK_UNSUPPORTED_SCENARIO",
                           "Подготовлены два примера без бюджета: 99% / +0 дней и 95% / +7 дней.")
        result_detail = deepcopy(chosen)
        if self._quality != "ready":
            for key in ("base_total_cost", "scenario_total_cost", "currency"):
                result_detail["summary"][key] = None
            for line in result_detail["changed_lines"]:
                line["base_line_cost"] = None
                line["scenario_line_cost"] = None
        self._scenarios[chosen["id"]] = result_detail
        result = {"scenario_id": chosen["id"], "status_url": "/v1/scenarios/" + chosen["id"]}
        self._requests[cache_key] = (deepcopy(payload), result)
        return deepcopy(result)

    def get_scenario(self, scenario_id: str) -> JsonObject:
        if scenario_id not in self._scenarios:
            raise self._missing("сценарий")
        return deepcopy(self._scenarios[scenario_id])

    def list_demand_events(self, run_id: str, *, label: str | None = None,
                           cursor: str | None = None, limit: int = 50) -> JsonObject:
        self.get_planning_run(run_id)
        items = [event for event in self._fixture["demand_events"]["items"] if label is None or event["label"] == label]
        result = self._page(items, cursor, limit)
        result["diagnostics"] = deepcopy(self._fixture["demand_events"]["diagnostics"])
        return result
