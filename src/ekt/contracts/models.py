"""Versioned public contracts shared by ingestion, planning, API and UI.

Quantities and money deliberately serialize as JSON strings. The schemas carry
explicit provenance and replay time: a fixture can never look like live stock.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, AwareDatetime, BaseModel, ConfigDict, Field, model_validator

ID = Annotated[str, Field(min_length=1, pattern=r"\S")]
Qty = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
PositiveQty = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
SignedQty = Annotated[Decimal, Field(allow_inf_nan=False)]
Money = Qty
FiniteFloat = Annotated[float, Field(allow_inf_nan=False)]
NonnegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
Probability = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
ServiceTarget = Annotated[float, Field(gt=0, lt=1, allow_inf_nan=False)]
Mode = Literal["synthetic_demo", "real_preview"]
Provenance = Literal["observed", "derived", "synthetic", "override"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, allow_inf_nan=False)


class Capabilities(Contract):
    can_plan: bool = False
    can_approve: bool = False
    can_export: bool = False
    budget_available: bool = False
    customer_detection_available: bool = False
    observed_stockouts_available: bool = False
    reasons: list[str] = Field(default_factory=list)


class QualityIssue(Contract):
    code: ID
    severity: Literal["info", "warning", "blocking"]
    scope_ids: list[ID] = Field(default_factory=list)
    source_ref: str | None = None
    message: str


class QualityReport(Contract):
    status: Literal["ready", "degraded", "blocked"] = "ready"
    accepted_rows: int = Field(default=0, ge=0)
    rejected_rows: int = Field(default=0, ge=0)
    affected_skus: int = Field(default=0, ge=0)
    issues: list[QualityIssue] = Field(default_factory=list)
    capabilities: Capabilities = Field(default_factory=Capabilities)


class SourceRef(Contract):
    source_id: ID
    checksum: ID
    kind: ID
    local_ref: str | None = None


class TableRef(Contract):
    uri: ID
    format: Literal["parquet"] = "parquet"
    row_count: int = Field(ge=0)
    checksum: ID


class Assumption(Contract):
    field: ID
    value: str
    provenance: Provenance
    reason: str
    scope_ids: list[ID] = Field(default_factory=list)


class SnapshotManifest(Contract):
    schema_version: Literal["1.0"] = "1.0"
    snapshot_id: ID
    mapping_version: ID
    manifest_hash: ID
    mode: Mode
    as_of: AwareDatetime
    created_at: AwareDatetime
    source_refs: list[SourceRef] = Field(default_factory=list)
    tables: dict[str, TableRef]
    quality: QualityReport
    assumptions: list[Assumption] = Field(default_factory=list)


class SourceManifest(Contract):
    source_ids: list[ID] = Field(default_factory=list)
    source_refs: list[SourceRef] = Field(default_factory=list)
    sources: list[dict[str, Any]] = Field(default_factory=list)
    output_root: str | None = None
    mode: Mode = "real_preview"
    as_of: AwareDatetime


class MappingConfig(Contract):
    mapping_version: ID = Field(default="1.0", validation_alias=AliasChoices("mapping_version", "version"))
    columns: dict[str, dict[str, str]] = Field(default_factory=dict)
    options: dict[str, Any] = Field(default_factory=dict)

    @property
    def version(self) -> str:
        return self.mapping_version


class SkuMaster(Contract):
    sku_id: ID
    name: str
    category_id: ID | None = None
    base_uom: ID
    purchase_uom: ID | None = None
    base_units_per_purchase_uom: PositiveQty | None = None
    quantity_quantum: PositiveQty
    supplier_id: ID
    supplier_article: str | None = None
    provenance: Provenance
    eligible: bool = True
    lifecycle: Literal["active", "discontinued", "new", "inactive"] = "active"


class SalesEvent(Contract):
    event_id: ID
    source_id: ID
    row_ref: str
    doc_id: ID
    line_id: ID | None = None
    revision: int | None = Field(default=None, ge=0)
    event_at: AwareDatetime
    sku_id: ID
    warehouse_id: ID
    event_type: ID
    quantity_base: Qty
    demand_effect: Literal["increase", "decrease", "none"]
    customer_token: ID | None = None
    provenance: Provenance


class InventorySnapshot(Contract):
    sku_id: ID
    warehouse_id: ID
    as_of: AwareDatetime
    on_hand_base: Qty
    reserved_base: Qty
    blocked_base: Qty
    free_base: SignedQty
    accounting_definition_version: ID
    provenance: Provenance

    @model_validator(mode="after")
    def reconciles(self):
        if self.free_base != self.on_hand_base - self.reserved_base - self.blocked_base:
            raise ValueError("free_base must equal on_hand_base - reserved_base - blocked_base")
        return self


class StockoutInterval(Contract):
    sku_id: ID
    warehouse_id: ID
    start_at: AwareDatetime
    end_at: AwareDatetime | None = None
    unavailable_fraction: Probability
    evidence: Literal["observed", "inferred", "synthetic"]
    confidence: Probability

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_at is not None and self.end_at <= self.start_at:
            raise ValueError("stockout end_at must be after start_at")
        return self


class PipelineLine(Contract):
    po_line_id: ID
    sku_id: ID
    supplier_id: ID
    warehouse_id: ID
    remaining_base_qty: Qty
    eta_start: AwareDatetime | None = None
    eta_end: AwareDatetime | None = None
    eta_semantics: Literal["window", "deadline", "unknown"]
    status: Literal["open", "confirmed", "in_transit", "received", "cancelled"]
    provenance: Provenance

    @model_validator(mode="after")
    def valid_eta(self):
        if self.eta_start and self.eta_end and self.eta_end < self.eta_start:
            raise ValueError("eta_end precedes eta_start")
        if self.eta_semantics == "window" and (self.eta_start is None or self.eta_end is None):
            raise ValueError("window ETA requires both bounds")
        if self.eta_semantics == "deadline" and self.eta_end is None:
            raise ValueError("deadline ETA requires eta_end")
        return self


class SupplierTerms(Contract):
    supplier_id: ID
    sku_id: ID
    warehouse_id: ID
    moq_purchase: Qty | None = None
    pack_multiple_purchase: PositiveQty | None = None
    lead_time_days: int | None = Field(default=None, ge=0)
    review_days: int | None = Field(default=None, ge=1)
    cost_per_base: Money | None = None
    currency: ID | None = None
    valid_at: AwareDatetime
    provenance: Provenance

    @model_validator(mode="after")
    def currency_for_cost(self):
        if self.cost_per_base is not None and self.currency is None:
            raise ValueError("cost_per_base requires currency")
        return self


class PlanningOverride(Contract):
    scope_id: ID
    parameter: ID
    value: str
    version: ID = "1"
    valid_from: AwareDatetime
    valid_to: AwareDatetime | None = None
    provenance: Provenance
    reason: str


class MonthlyMeasure(Contract):
    period_start: date
    period_end: date
    sku_id: ID
    measure: ID
    value: SignedQty | None = None
    source_id: ID
    coverage: str

    @model_validator(mode="after")
    def valid_period(self):
        if self.period_end <= self.period_start:
            raise ValueError("period_end must be after period_start")
        return self


class GrowthOverride(Contract):
    category_id: ID
    mode: Literal["replace", "incremental"]
    rate: Annotated[float, Field(gt=-1, allow_inf_nan=False)]
    valid_from: date
    valid_to: date

    @model_validator(mode="after")
    def valid_interval(self):
        if self.valid_to < self.valid_from:
            raise ValueError("valid_to precedes valid_from")
        return self


class ClassificationOverride(Contract):
    event_ids: list[ID] = Field(min_length=1)
    label: Literal["regular", "project"]
    reason: Annotated[str, Field(min_length=1)]


class ForecastRequest(Contract):
    run_id: ID
    as_of: AwareDatetime
    sku_ids: list[ID] = Field(default_factory=list)
    warehouse_ids: list[ID] = Field(default_factory=list)
    horizon_days: int = Field(default=90, ge=1, le=1095)
    seed: int = 42
    growth_overrides: list[GrowthOverride] = Field(default_factory=list)
    review_overrides: list[ClassificationOverride] = Field(default_factory=list)


class ForecastDay(Contract):
    date: date
    baseline_mean: NonnegativeFloat
    seasonal_delta: FiniteFloat
    growth_delta: FiniteFloat
    mean: NonnegativeFloat

    @model_validator(mode="after")
    def reconciles(self):
        if abs(self.mean - self.baseline_mean - self.seasonal_delta - self.growth_delta) > 1e-8:
            raise ValueError("forecast mean must reconcile to baseline + seasonality + growth")
        return self


class Uncertainty(Contract):
    method: Literal["scenario_paths", "iid_residual_normal"]
    daily_residual_std: NonnegativeFloat | None = None
    paths_ref: str | None = None
    path_count: int | None = Field(default=None, gt=0)
    calibration: Literal["unvalidated", "backtested"] = "unvalidated"
    assumption_note: str

    @model_validator(mode="after")
    def method_inputs(self):
        if self.method == "iid_residual_normal" and self.daily_residual_std is None:
            raise ValueError("iid_residual_normal requires daily_residual_std")
        if self.method == "scenario_paths" and (not self.paths_ref or not self.path_count):
            raise ValueError("scenario_paths requires paths_ref and path_count")
        return self


class ForecastSeries(Contract):
    sku_id: ID
    warehouse_id: ID
    base_uom: ID
    daily: list[ForecastDay]
    uncertainty: Uncertainty
    observed_regular_total: Qty = Decimal("0")
    estimated_lost_total: Qty = Decimal("0")
    project_total: Qty = Decimal("0")
    uncertain_total: Qty = Decimal("0")
    classification_policy: Literal["review_confirmed", "robust_suspected_exclusion", "regular_only"]
    warnings: list[QualityIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_ordered_days(self):
        days = [day.date for day in self.daily]
        if days != sorted(set(days)):
            raise ValueError("forecast dates must be unique and ascending")
        return self


class Metric(Contract):
    name: ID
    value: FiniteFloat | None = None
    unit: str | None = None
    scope: str | None = None
    note: str | None = None


class ForecastArtifact(Contract):
    schema_version: Literal["1.0"] = "1.0"
    forecast_id: ID
    snapshot_id: ID
    request_hash: ID
    model_version: ID
    seed: int
    as_of: AwareDatetime
    mode: Mode
    series: list[ForecastSeries]
    classifications_ref: str
    corrected_demand_ref: str
    quality: QualityReport
    metrics: list[Metric] = Field(default_factory=list)

    @model_validator(mode="after")
    def unique_series(self):
        keys = [(series.sku_id, series.warehouse_id) for series in self.series]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate SKU/warehouse forecast series")
        return self


class ClassificationRecord(Contract):
    event_id: ID
    sku_id: ID
    warehouse_id: ID
    observed_qty: Qty
    regular_qty: Qty
    project_qty: Qty
    uncertain_qty: Qty
    label: Literal["regular", "project", "suspected_project", "uncertain"]
    reason_codes: list[ID] = Field(default_factory=list)
    confidence: Probability
    review_status: Literal["pending", "confirmed", "not_required"] = "not_required"

    @model_validator(mode="after")
    def quantities_reconcile(self):
        if self.regular_qty + self.project_qty + self.uncertain_qty != self.observed_qty:
            raise ValueError("classification quantities must reconcile to observed_qty")
        return self


class CorrectedDemand(Contract):
    sku_id: ID
    warehouse_id: ID
    date: date
    observed_regular_qty: Qty
    estimated_lost_qty: Qty
    corrected_qty: Qty
    availability_fraction: Probability
    method: ID
    provenance: Provenance

    @model_validator(mode="after")
    def quantities_reconcile(self):
        if self.corrected_qty != self.observed_regular_qty + self.estimated_lost_qty:
            raise ValueError("corrected_qty must equal observed_regular_qty + estimated_lost_qty")
        return self


class PlanningPolicy(Contract):
    service_metric: Literal["cycle_service"] = "cycle_service"
    service_target: ServiceTarget = 0.95
    review_days: int | None = Field(default=None, ge=1)
    budget_cap: Money | None = None
    currency: ID | None = None
    max_cover_days: int | None = Field(default=None, ge=1)
    lead_time_delay_days: int = Field(default=0, ge=0, le=365)
    policy_version: ID = "mvp-v1"

    @model_validator(mode="after")
    def budget_currency(self):
        if self.budget_cap is not None and not self.currency:
            raise ValueError("budget_cap requires currency")
        return self


class ScenarioOverrides(Contract):
    service_target: ServiceTarget | None = None
    budget_cap: Money | None = None
    lead_time_delay_days: int | None = Field(default=None, ge=0, le=365)


class ExplanationComponent(Contract):
    code: ID
    label: str
    delta_base_qty: SignedQty
    source_refs: list[str] = Field(default_factory=list)
    note: str | None = None


class ExcludedLine(Contract):
    sku_id: ID
    reasons: list[QualityIssue] = Field(min_length=1)


class ProposalLine(Contract):
    line_id: ID
    sku_id: ID
    name: str
    base_uom: ID
    purchase_uom: ID
    recommended_base_qty: Qty
    recommended_purchase_qty: Qty
    selected_purchase_qty: Qty
    selected_base_qty: Qty
    moq_purchase: Qty
    pack_multiple_purchase: PositiveQty
    conversion: PositiveQty
    quantity_quantum: PositiveQty = Decimal("1")
    rop: Qty
    safety_stock: Qty
    raw_need: Qty
    unit_cost: Money | None = None
    line_cost: Money | None = None
    urgency: Literal["critical", "soon", "routine"]
    projected_stockout_date: date | None = None
    explanation: list[ExplanationComponent]
    warnings: list[QualityIssue] = Field(default_factory=list)

    @model_validator(mode="after")
    def quantities_and_ledger(self):
        if self.selected_base_qty != self.selected_purchase_qty * self.conversion:
            raise ValueError("selected base/purchase quantities do not reconcile")
        if self.recommended_base_qty != self.recommended_purchase_qty * self.conversion:
            raise ValueError("recommended base/purchase quantities do not reconcile")
        if sum((part.delta_base_qty for part in self.explanation), Decimal(0)) != self.selected_base_qty:
            raise ValueError("explanation components must sum to selected_base_qty")
        for quantity in (self.selected_purchase_qty, self.recommended_purchase_qty):
            if quantity and (quantity < self.moq_purchase or quantity % self.pack_multiple_purchase != 0):
                raise ValueError("positive purchase quantities must satisfy MOQ and pack multiple")
        if self.selected_base_qty % self.quantity_quantum != 0 or self.recommended_base_qty % self.quantity_quantum != 0:
            raise ValueError("base quantities must satisfy quantity_quantum")
        if self.unit_cost is not None and self.line_cost != self.unit_cost * self.selected_base_qty:
            raise ValueError("line_cost must equal unit_cost per base UOM times selected_base_qty")
        return self


class ProposalDetail(Contract):
    proposal_id: ID
    version: int = Field(default=1, ge=1)
    status: Literal["draft", "approved"] = "draft"
    content_hash: ID
    run_id: ID
    snapshot_id: ID
    mode: Mode
    supplier_id: ID
    warehouse_id: ID
    as_of: AwareDatetime
    currency: ID | None = None
    total_cost: Money | None = None
    capabilities: Capabilities
    warnings: list[QualityIssue] = Field(default_factory=list)
    lines: list[ProposalLine]
    excluded_lines: list[ExcludedLine] = Field(default_factory=list)

    @model_validator(mode="after")
    def totals_reconcile(self):
        if len({line.line_id for line in self.lines}) != len(self.lines):
            raise ValueError("duplicate proposal line_id")
        if self.total_cost is not None:
            if not self.currency or any(line.line_cost is None for line in self.lines):
                raise ValueError("total_cost requires currency and complete line costs")
            if self.total_cost != sum((line.line_cost for line in self.lines), Decimal(0)):
                raise ValueError("total_cost must equal sum of line costs")
        return self


class ProposalSummary(Contract):
    proposal_id: ID
    version: int = Field(ge=1)
    status: Literal["draft", "approved"]
    run_id: ID
    snapshot_id: ID
    mode: Mode
    supplier_id: ID
    warehouse_id: ID
    as_of: AwareDatetime
    currency: ID | None = None
    total_cost: Money | None = None
    line_count: int = Field(ge=0)
    excluded_count: int = Field(default=0, ge=0)
    capabilities: Capabilities


class InventoryTarget(Contract):
    sku_id: ID
    warehouse_id: ID
    protection_days: int = Field(ge=1)
    expected_demand: Qty
    safety_stock: Qty
    rop: Qty
    target_stock: Qty
    inventory_position: SignedQty
    raw_need: Qty
    projected_stockout_date: date | None = None


class ApiError(Contract):
    code: ID
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False


class DomainError(Exception):
    """Transport-independent domain failure converted by the API coordinator."""

    def __init__(self, code: str, message: str, affected_sku_ids: list[str] | None = None,
                 retryable: bool = False, details: dict[str, Any] | None = None,
                 status_code: int = 422):
        self.code = code
        self.message = message
        self.affected_sku_ids = affected_sku_ids or []
        self.retryable = retryable
        self.details = details or {}
        self.status_code = status_code
        super().__init__(message)

    def as_api_error(self) -> ApiError:
        return ApiError(code=self.code, message=self.message,
                        details={**self.details, "affected_sku_ids": self.affected_sku_ids},
                        retryable=self.retryable)


class JobStatus(Contract):
    id: ID
    status: Literal["queued", "running", "succeeded", "failed"]
    stage: str
    progress: Probability
    result_ref: str | None = None
    snapshot_id: ID | None = None
    quality: QualityReport | None = None
    model_version: ID | None = None
    mode: Mode | None = None
    error: ApiError | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime


class SnapshotRequest(Contract):
    source_ids: list[ID]
    mapping_version: ID
    mode: Mode
    as_of: AwareDatetime


class JobAccepted(Contract):
    job_id: ID
    status_url: str


class PlanningRunRequest(Contract):
    snapshot_id: ID
    policy: PlanningPolicy
    idempotency_key: ID


class PlanningRunAccepted(Contract):
    run_id: ID
    status_url: str


class PlanningRunStatus(JobStatus):
    proposal_ids: list[ID] = Field(default_factory=list)
    quality: QualityReport | None = None
    forecast_id: ID | None = None


class ProposalEdit(Contract):
    line_id: ID
    purchase_qty: Qty


class ProposalPatchRequest(Contract):
    expected_version: int = Field(ge=1)
    edits: list[ProposalEdit] = Field(min_length=1)
    reason: Annotated[str, Field(min_length=1)]

    @model_validator(mode="after")
    def unique_edits(self):
        if len({edit.line_id for edit in self.edits}) != len(self.edits):
            raise ValueError("duplicate line edits are ambiguous")
        return self


class ApprovalRequest(Contract):
    expected_version: int = Field(ge=1)
    content_hash: ID


class ApprovalResponse(Contract):
    approval_id: ID
    proposal_id: ID
    version: int = Field(ge=1)
    status: Literal["approved"] = "approved"


class ExportRequest(Contract):
    expected_version: int = Field(ge=1)
    idempotency_key: ID


class ScenarioRequest(Contract):
    base_run_id: ID
    overrides: ScenarioOverrides
    seed: int = 42
    idempotency_key: ID


class ScenarioAccepted(Contract):
    scenario_id: ID
    status_url: str


class ScenarioChangedLine(Contract):
    sku_id: ID
    supplier_id: ID
    warehouse_id: ID
    baseline_base_qty: Qty
    scenario_base_qty: Qty
    delta_base_qty: SignedQty
    baseline_cost: Money | None = None
    scenario_cost: Money | None = None


class ScenarioSummary(Contract):
    baseline_total_cost: Money | None = None
    scenario_total_cost: Money | None = None
    delta_cost: SignedQty | None = None
    currency: ID | None = None
    changed_line_count: int = Field(default=0, ge=0)


class ScenarioStatus(JobStatus):
    base_run_id: ID
    changed_lines: list[ScenarioChangedLine] = Field(default_factory=list)
    summary: ScenarioSummary | None = None
    assumptions: list[str] = Field(default_factory=list)


class ProposalPage(Contract):
    items: list[ProposalSummary]
    next_cursor: str | None = None


class DemandEventPage(Contract):
    items: list[ClassificationRecord]
    next_cursor: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class SourceMetadata(Contract):
    source_id: ID
    name: str
    kind: ID
    import_state: Literal["registered", "importing", "ready", "failed"] = "registered"
    row_count: int | None = Field(default=None, ge=0)
    mode: Mode


class SourcePage(Contract):
    items: list[SourceMetadata]


class HealthResponse(Contract):
    status: Literal["ok"] = "ok"
    version: str
    identity_mode: Literal["demo"] = "demo"
    actor: str
    role: str
    vendor_transmission_enabled: Literal[False] = False


# Descriptive aliases keep integrations readable without duplicating schemas.
DailyForecast = ForecastDay
ForecastUncertainty = Uncertainty
SkuMasterRow = SkuMaster
SalesEventRow = SalesEvent
InventorySnapshotRow = InventorySnapshot
StockoutIntervalRow = StockoutInterval
PipelineLineRow = PipelineLine
SupplierTermsRow = SupplierTerms
