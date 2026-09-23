"""FastAPI entry point. Run: uv run uvicorn ekt.api.app:app."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from ekt import __version__
from ekt.contracts import (
    ApprovalRequest,
    ApprovalResponse,
    DemandEventPage,
    DomainError,
    ExportRequest,
    PlanningRunRequest,
    PlanningRunAccepted,
    PlanningRunStatus,
    JobAccepted,
    JobStatus,
    ProposalDetail,
    ProposalPage,
    ProposalPatchRequest,
    ScenarioRequest,
    ScenarioAccepted,
    ScenarioStatus,
    SnapshotManifest,
    SnapshotRequest,
)
from ekt.storage import (
    AlreadyExistsError,
    IdempotencyConflictError,
    NotFoundError,
    VersionConflictError,
)

from .service import Service, ServiceError, public_snapshot


def create_app(data_dir: Path | str | None = None, *, forecast_provider=None, snapshot_provider=None):
    @asynccontextmanager
    async def lifespan(application):
        service = Service(data_dir or os.environ.get("EKT_DATA_DIR", "var"), forecast_provider=forecast_provider, snapshot_provider=snapshot_provider)
        application.state.service = service
        yield
        service.close()

    application = FastAPI(
        title="Elektrokomplekt Replenishment MVP",
        version=__version__,
        description="Buyer-controlled recommendations. Synthetic replay and local CSV only; no vendor transmission.",
        lifespan=lifespan,
    )

    def service(request: Request) -> Service:
        return request.app.state.service

    def public_job(value, model):
        # Persistence versions and internal requests are not public API fields.
        return {key: value[key] for key in model.model_fields if key in value}

    @application.exception_handler(ServiceError)
    async def service_error(request, exc):
        return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message, "details": exc.details, "retryable": exc.retryable})

    @application.exception_handler(DomainError)
    async def domain_error(request, exc):
        return JSONResponse(status_code=exc.status_code, content=exc.as_api_error().model_dump(mode="json"))

    @application.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        errors = [{"path": list(error["loc"]), "type": error["type"], "message": error["msg"]} for error in exc.errors()]
        return JSONResponse(status_code=422, content={"code": "VALIDATION_ERROR", "message": "Некорректные параметры запроса", "details": {"errors": errors}, "retryable": False})

    async def conflict(request, exc):
        return JSONResponse(status_code=409, content={"code": "VERSION_CONFLICT" if isinstance(exc, VersionConflictError) else "IDEMPOTENCY_CONFLICT", "message": "Версия изменилась или ключ уже использован с другим запросом", "details": {}, "retryable": False})

    for error in (VersionConflictError, IdempotencyConflictError, AlreadyExistsError):
        application.add_exception_handler(error, conflict)

    @application.exception_handler(NotFoundError)
    async def not_found(request, exc):
        return JSONResponse(status_code=404, content={"code": "NOT_FOUND", "message": "Объект не найден", "details": {}, "retryable": False})

    @application.get("/v1/health")
    def health(request: Request):
        return {"status": "ok", "version": __version__, "identity_mode": "demo", "actor": service(request).actor, "role": service(request).role, "vendor_transmission_enabled": False}

    @application.get("/v1/sources")
    def sources(request: Request):
        return service(request).list_sources()

    @application.post("/v1/snapshots", status_code=202, response_model=JobAccepted)
    def create_snapshot(body: SnapshotRequest, request: Request):
        return service(request).start_snapshot(body.model_dump(mode="json"))

    @application.get("/v1/jobs/{job_id}", response_model=JobStatus)
    def job(job_id: str, request: Request):
        return public_job(service(request).job(job_id), JobStatus)

    @application.get("/v1/snapshots/{snapshot_id}", response_model=SnapshotManifest)
    def snapshot(snapshot_id: str, request: Request):
        return public_snapshot(service(request)._require("snapshots", snapshot_id))

    @application.post("/v1/planning-runs", status_code=202, response_model=PlanningRunAccepted)
    def start_run(body: PlanningRunRequest, request: Request):
        return service(request).start_run(body.model_dump(mode="json"))

    @application.get("/v1/planning-runs/{run_id}", response_model=PlanningRunStatus)
    def run(run_id: str, request: Request):
        return public_job(service(request).job(run_id), PlanningRunStatus)

    @application.get("/v1/proposals", response_model=ProposalPage)
    def proposals(request: Request, run_id: str | None = None, supplier_id: str | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        return service(request).list_proposals(run_id, supplier_id, limit, cursor)

    @application.get("/v1/proposals/{proposal_id}", response_model=ProposalDetail)
    def proposal(proposal_id: str, request: Request, version: int | None = Query(None, ge=1)):
        return service(request).proposal(proposal_id, version)

    @application.patch("/v1/proposals/{proposal_id}", response_model=ProposalDetail)
    def edit(proposal_id: str, body: ProposalPatchRequest, request: Request):
        return service(request).edit(proposal_id, body.model_dump(mode="json"))

    @application.post("/v1/proposals/{proposal_id}/approve", response_model=ApprovalResponse)
    def approve(proposal_id: str, body: ApprovalRequest, request: Request):
        return service(request).approve(proposal_id, body.model_dump(mode="json"))

    @application.post("/v1/proposals/{proposal_id}/export")
    def export(proposal_id: str, body: ExportRequest, request: Request):
        csv_text, filename = service(request).export(proposal_id, body.model_dump(mode="json"))
        return Response(content="\ufeff" + csv_text, media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})

    @application.post("/v1/scenarios", status_code=202, response_model=ScenarioAccepted)
    def start_scenario(body: ScenarioRequest, request: Request):
        return service(request).start_scenario(body.model_dump(mode="json"))

    @application.get("/v1/scenarios/{scenario_id}", response_model=ScenarioStatus)
    def scenario(scenario_id: str, request: Request):
        return public_job(service(request).job(scenario_id), ScenarioStatus)

    @application.get("/v1/planning-runs/{run_id}/demand-events", response_model=DemandEventPage)
    def demand_events(run_id: str, request: Request, label: str | None = None, limit: int = Query(50, ge=1, le=200), cursor: str | None = None):
        return service(request).demand_events(run_id, label, limit, cursor)

    return application


app = create_app()
