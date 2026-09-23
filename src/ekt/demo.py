"""Deterministic, labelled synthetic inputs for the integration demonstration.

The fallback forecast is a fixture scaffold, not the B participant's demand
model. It is intentionally disclosed in every artifact and proposal warning.
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from statistics import median

from ekt.contracts import (
    Assumption, Capabilities, ForecastArtifact, ForecastDay, ForecastSeries,
    InventorySnapshot, PipelineLine, QualityIssue, QualityReport, SalesEvent,
    SkuMaster, SnapshotManifest, SourceRef, StockoutInterval, SupplierTerms,
    Uncertainty, load_table, write_table,
)

DEMO_AS_OF = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
DEMO_SEED = 42
DEMO_SNAPSHOT_ID = "demo-20260923-v1"
WAREHOUSE_ID = "ALA-DEMO"


def create_demo_snapshot(root: Path) -> SnapshotManifest:
    """Write a reproducible 24-month, 16-SKU snapshot under a backend data root."""
    directory = Path(root).resolve() / DEMO_SNAPSHOT_ID
    directory.mkdir(parents=True, exist_ok=True)
    rng = random.Random(DEMO_SEED)
    skus, sales, inventory, pipeline, terms, stockouts = [], [], [], [], [], []
    start = datetime(2024, 9, 23, tzinfo=timezone.utc)
    day_count = (DEMO_AS_OF - start).days
    names = [
        "Автоматический выключатель 16A", "Розетка двойная", "Контактор 25A",
        "Светильник промышленный", "Кабельный наконечник", "Кабель ВВГ 3×2.5",
        "Щит распределительный", "Клемма соединительная", "Выключатель белый",
        "Автоматический выключатель 32A", "Дифференциальный автомат",
        "Рамка декоративная", "Реле напряжения", "Кабельный канал",
        "Розетка с заземлением", "Аксессуар без условий поставки",
    ]
    for index, name in enumerate(names):
        supplier = "IEK-DEMO" if index < 8 else "SYSTEME-DEMO"
        sku_id = f"DEMO-{index + 1:03d}"
        is_cable = index == 5
        conversion = Decimal("100") if is_cable else Decimal("1")
        baseline = 4 + index % 6
        if is_cable:
            baseline *= 10
        sku = SkuMaster(
            sku_id=sku_id, name=name, category_id="CABLE" if is_cable else "EQUIPMENT",
            base_uom="m" if is_cable else "pcs", purchase_uom="coil" if is_cable else "pcs",
            base_units_per_purchase_uom=conversion, quantity_quantum=Decimal("1"),
            supplier_id=supplier, supplier_article=f"SYN-{index + 1:05d}", provenance="synthetic",
        )
        skus.append(sku)
        unavailable = []
        if index in (3, 4):
            outage_start = DEMO_AS_OF - timedelta(days=45 if index == 3 else 180)
            outage_end = outage_start + timedelta(days=14 if index == 3 else 21)
            unavailable.append((outage_start, outage_end))
            stockouts.append(StockoutInterval(
                sku_id=sku_id, warehouse_id=WAREHOUSE_ID, start_at=outage_start,
                end_at=outage_end, unavailable_fraction=1, evidence="synthetic", confidence=1,
            ))
        for day_index in range(day_count):
            day = start + timedelta(days=day_index)
            seasonal = 1 + (0.24 if index in (1, 3, 5) else 0.1) * math.sin(2 * math.pi * (day.timetuple().tm_yday - 60) / 365.25)
            growth = 1 + (0.32 * day_index / day_count if index in (0, 4, 8) else 0)
            weekday = 0.55 if day.weekday() >= 5 else 1.15
            quantity = max(0, round(baseline * seasonal * growth * weekday + rng.gauss(0, baseline * 0.18)))
            if any(begin <= day < end for begin, end in unavailable):
                quantity = 0
            if not quantity:
                continue
            event_id = f"event-{index + 1:03d}-{day_index:04d}"
            sales.append(SalesEvent(
                event_id=event_id, source_id="synthetic-generator-v1", row_ref=event_id,
                doc_id=f"sale-{index + 1:03d}-{day_index:04d}", line_id="1", revision=1,
                event_at=day + timedelta(hours=12), sku_id=sku_id, warehouse_id=WAREHOUSE_ID,
                event_type="sale", quantity_base=Decimal(quantity), demand_effect="increase",
                customer_token=f"customer-{rng.randrange(1, 31):03d}", provenance="synthetic",
            ))
        # No ground-truth labels are present in the input. The same canonical
        # document shape is used for ordinary and unusually large transactions.
        if index == 2:
            sales.append(SalesEvent(
                event_id="event-extra-0001", source_id="synthetic-generator-v1", row_ref="extra:1",
                doc_id="sale-extra-0001", line_id="1", revision=1,
                event_at=DEMO_AS_OF - timedelta(days=60), sku_id=sku_id, warehouse_id=WAREHOUSE_ID,
                event_type="sale", quantity_base=Decimal(baseline * 100), demand_effect="increase",
                customer_token="customer-999", provenance="synthetic",
            ))
        # Explicit returns exercise direction-aware ingestion without negative quantities.
        if index == 6:
            sales.append(SalesEvent(
                event_id="event-return-0001", source_id="synthetic-generator-v1", row_ref="returns:1",
                doc_id="return-0001", line_id="1", revision=1,
                event_at=DEMO_AS_OF - timedelta(days=20), sku_id=sku_id, warehouse_id=WAREHOUSE_ID,
                event_type="return", quantity_base=Decimal("2"), demand_effect="decrease",
                customer_token="customer-010", provenance="synthetic",
            ))
        free = Decimal(baseline * (2 if index in (0, 3, 8) else 12))
        inventory.append(InventorySnapshot(
            sku_id=sku_id, warehouse_id=WAREHOUSE_ID, as_of=DEMO_AS_OF,
            on_hand_base=free + Decimal("3"), reserved_base=Decimal("2"), blocked_base=Decimal("1"),
            free_base=free, accounting_definition_version="onhand-minus-reserved-minus-blocked-v1",
            provenance="synthetic",
        ))
        terms.append(SupplierTerms(
            supplier_id=supplier, sku_id=sku_id, warehouse_id=WAREHOUSE_ID,
            moq_purchase=None if index == 15 else Decimal("2" if is_cable else "10"),
            pack_multiple_purchase=None if index == 15 else Decimal("1" if is_cable else "5"),
            lead_time_days=None if index == 15 else (14 if index < 8 else 21), review_days=7,
            cost_per_base=Decimal("450") if is_cable else Decimal(250 + index * 125),
            currency="KZT", valid_at=DEMO_AS_OF, provenance="synthetic",
        ))
        if index in (0, 1, 5, 9):
            arrival = DEMO_AS_OF + timedelta(days=45 if index == 0 else 5)
            pipeline.append(PipelineLine(
                po_line_id=f"po-{index + 1:03d}-1", sku_id=sku_id, supplier_id=supplier,
                warehouse_id=WAREHOUSE_ID, remaining_base_qty=Decimal(baseline * 8),
                eta_start=arrival, eta_end=arrival + timedelta(days=2), eta_semantics="window",
                status="in_transit", provenance="synthetic",
            ))
    sales.sort(key=lambda row: (row.event_at, row.event_id))
    tables = {
        name: write_table(directory, name, rows)
        for name, rows in {
            "sku_master": skus, "sales_events": sales, "inventory_snapshots": inventory,
            "stockout_intervals": stockouts, "pipeline_lines": pipeline, "supplier_terms": terms,
            "planning_overrides": [], "monthly_sales": [], "monthly_balances": [],
        }.items()
    }
    issues = [
        QualityIssue(code="SYNTHETIC_DEMO", severity="warning", message="Все данные сгенерированы. Демонстрация — не заказ поставщику."),
        QualityIssue(code="MISSING_SUPPLIER_TERMS", severity="blocking", scope_ids=["DEMO-016"],
                     message="Не указаны MOQ, кратность и срок поставки; товар исключается из заказа."),
    ]
    capabilities = Capabilities(can_plan=True, can_approve=True, can_export=True,
        budget_available=True, customer_detection_available=True, observed_stockouts_available=False,
        reasons=["Только демонстрационный CSV; внешняя отправка отсутствует.", "Один SKU исключён из-за неизвестных условий."])
    checksums = {name: ref.checksum for name, ref in sorted(tables.items())}
    manifest_hash = hashlib.sha256(json.dumps(checksums, sort_keys=True).encode()).hexdigest()
    snapshot = SnapshotManifest(
        snapshot_id=DEMO_SNAPSHOT_ID, mapping_version="synthetic-v1", manifest_hash=manifest_hash,
        mode="synthetic_demo", as_of=DEMO_AS_OF, created_at=DEMO_AS_OF,
        source_refs=[SourceRef(source_id="synthetic-generator-v1", checksum=manifest_hash,
                               kind="synthetic", local_ref="generated:ekt.demo.create_demo_snapshot")],
        tables=tables, quality=QualityReport(status="degraded", accepted_rows=sum(ref.row_count for ref in tables.values()),
            rejected_rows=0, affected_skus=1, issues=issues, capabilities=capabilities),
        assumptions=[
            Assumption(field="all_inputs", value="synthetic", provenance="synthetic",
                reason="Фиксированный набор для демонстрации интеграции; не коммерческие данные."),
            Assumption(field="calendar", value="calendar_days", provenance="synthetic",
                reason="Сроки и интервал обзора измеряются в календарных днях."),
        ],
    )
    (directory / "snapshot.json").write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return snapshot


def fixture_forecast(snapshot: SnapshotManifest) -> ForecastArtifact:
    """Explicitly approximate fixture-only forecasting scaffold until B connects.

    Uses recent positive sales' median for a stable synthetic baseline, with
    disclosed IID normal uncertainty. Does not claim to classify projects,
    reconstruct censored demand or measure an achieved service level.
    """
    if snapshot.mode != "synthetic_demo":
        from ekt.contracts import DomainError
        raise DomainError("FORECAST_PROVIDER_NOT_CONNECTED", "Fixture forecast accepts synthetic_demo only")
    skus = load_table(snapshot, "sku_master")
    observations: dict[str, list[float]] = defaultdict(list)
    totals: dict[str, Decimal] = defaultdict(Decimal)
    cutoff = snapshot.as_of - timedelta(days=90)
    for row in load_table(snapshot, "sales_events"):
        event_at = datetime.fromisoformat(str(row["event_at"]).replace("Z", "+00:00"))
        if row["demand_effect"] == "increase" and cutoff <= event_at < snapshot.as_of:
            quantity = Decimal(str(row["quantity_base"]))
            observations[row["sku_id"]].append(float(quantity))
            totals[row["sku_id"]] += quantity
    notice = QualityIssue(code="FORECAST_PROVIDER_NOT_CONNECTED", severity="warning",
        message="Прогноз fixture-v1 — временное синтетическое приближение. Модель участника B, классификация проектов и восстановление спроса ещё не подключены.")
    directory = Path(snapshot.tables["sku_master"].uri).parent
    classifications = write_table(directory, "fixture_classifications", [])
    corrected = write_table(directory, "fixture_corrected_demand", [])
    series = []
    for sku in skus:
        baseline = float(median(observations[sku["sku_id"]])) if observations[sku["sku_id"]] else 0.0
        daily = [ForecastDay(date=(snapshot.as_of + timedelta(days=day)).date(),
            baseline_mean=baseline, seasonal_delta=0, growth_delta=0, mean=baseline) for day in range(1, 91)]
        series.append(ForecastSeries(
            sku_id=sku["sku_id"], warehouse_id=WAREHOUSE_ID, base_uom=sku["base_uom"], daily=daily,
            uncertainty=Uncertainty(method="iid_residual_normal", daily_residual_std=baseline * 0.35,
                calibration="unvalidated", assumption_note="Synthetic scaffold: independent normal daily errors, assumed standard deviation 35% of median; no measured CSL."),
            observed_regular_total=totals[sku["sku_id"]], estimated_lost_total=Decimal("0"),
            project_total=Decimal("0"), uncertain_total=Decimal("0"), classification_policy="regular_only", warnings=[notice],
        ))
    quality = snapshot.quality.model_copy(deep=True)
    quality.issues.append(notice)
    quality.status = "degraded"
    artifact = ForecastArtifact(
        forecast_id=f"fixture-{snapshot.snapshot_id}", snapshot_id=snapshot.snapshot_id,
        request_hash=hashlib.sha256(f"fixture-v1:{snapshot.manifest_hash}".encode()).hexdigest(),
        model_version="fixture-v1", seed=DEMO_SEED, as_of=snapshot.as_of, mode="synthetic_demo",
        series=series, classifications_ref=classifications.uri, corrected_demand_ref=corrected.uri,
        quality=quality, metrics=[],
    )
    (directory / "fixture_forecast.json").write_text(artifact.model_dump_json(indent=2), encoding="utf-8")
    return artifact
