"""Buyer-entered planning inputs create a new, auditable local snapshot."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import json

from ekt.contracts import (
    Assumption, BuyerInput, BuyerInputs, BuyerPreparationRequest, Capabilities,
    InventorySnapshot, PipelineLine, QualityIssue, SkuMaster, SnapshotManifest,
    SupplierTerms, load_table, write_table,
)

from .service import ServiceError, artifact_payload, digest, now, public_snapshot


def _table(snapshot, name):
    return load_table(snapshot, name) if name in snapshot.tables else []


def _time(value):
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _latest(rows, field, cutoff):
    candidates = [row for row in rows if row.get(field) and _time(row[field]) <= cutoff]
    if not candidates:
        return {}
    stamp = max(_time(row[field]) for row in candidates)
    latest = [row for row in candidates if _time(row[field]) == stamp]
    # Conflicting source values need the buyer's input, never arbitrary selection.
    return latest[0] if all(row == latest[0] for row in latest) else {}


def read_inputs(service, snapshot_id):
    snapshot = SnapshotManifest.model_validate(artifact_payload(service._require("snapshots", snapshot_id)))
    masters = _table(snapshot, "sku_master")
    sales = _table(snapshot, "sales_events")
    stocks = _table(snapshot, "inventory_snapshots")
    terms = _table(snapshot, "supplier_terms")
    pipeline = _table(snapshot, "pipeline_lines")
    scopes = {(row["sku_id"], row["warehouse_id"]) for row in [*sales, *stocks, *terms]}
    items = []
    for master in masters:
        sku = master["sku_id"]
        for _, warehouse in sorted(scope for scope in scopes if scope[0] == sku):
            stock = _latest([r for r in stocks if (r["sku_id"], r["warehouse_id"]) == (sku, warehouse)], "as_of", snapshot.as_of)
            term = _latest([r for r in terms if (r["sku_id"], r["warehouse_id"], r["supplier_id"]) == (sku, warehouse, master["supplier_id"])], "valid_at", snapshot.as_of)
            incoming = [r for r in pipeline if (r["sku_id"], r["warehouse_id"]) == (sku, warehouse)
                        and r["status"] in {"open", "confirmed", "in_transit"}]
            # A single buyer-entered receipt uses the latest date conservatively.
            dated = incoming and all(r.get("eta_end") and _time(r["eta_end"]) > snapshot.as_of for r in incoming)
            items.append(BuyerInput(
                **{k: master.get(k) for k in ("sku_id", "name", "supplier_id", "base_uom", "purchase_uom", "base_units_per_purchase_uom", "quantity_quantum")},
                warehouse_id=warehouse, free_base=stock.get("free_base"),
                **{k: term.get(k) for k in ("moq_purchase", "pack_multiple_purchase", "lead_time_days", "review_days", "cost_per_base", "currency")},
                incoming_base_qty=sum((Decimal(str(r["remaining_base_qty"])) for r in incoming), Decimal(0)),
                incoming_eta=max(_time(r["eta_end"]).astimezone(snapshot.as_of.tzinfo).date() for r in incoming) if dated else None,
            ))
    dates = [_time(row["event_at"]).astimezone(timezone.utc).date() for row in sales
             if _time(row["event_at"]) <= snapshot.as_of]
    return BuyerInputs(snapshot_id=snapshot_id, mode=snapshot.mode, as_of=snapshot.as_of,
                       history_start=min(dates) if dates else None,
                       history_end=snapshot.as_of.astimezone(timezone.utc).date(), items=items)


def prepare(service, snapshot_id, payload):
    service._ensure_role()
    request = BuyerPreparationRequest.model_validate(payload)
    if not request.accept_history_estimate:
        raise ServiceError("HISTORY_CONFIRMATION_REQUIRED", "Подтвердите допущение о днях без продаж для этого расчёта")
    source = SnapshotManifest.model_validate(artifact_payload(service._require("snapshots", snapshot_id)))
    inputs = read_inputs(service, snapshot_id)
    known = {(row.sku_id, row.warehouse_id): row for row in inputs.items}
    required = ("purchase_uom", "base_units_per_purchase_uom", "quantity_quantum", "free_base",
                "moq_purchase", "pack_multiple_purchase", "lead_time_days", "review_days")
    for row in request.items:
        original = known.get((row.sku_id, row.warehouse_id))
        if original is None or (row.supplier_id, row.base_uom) != (original.supplier_id, original.base_uom):
            raise ServiceError("UNKNOWN_BUYER_ITEM", "Товар, склад, поставщик или единица не совпадает с исходными данными")
        missing = [field for field in required if getattr(row, field) is None]
        if missing:
            raise ServiceError("INCOMPLETE_BUYER_INPUT", "Заполните остаток и условия закупки выбранного товара", details={"sku_id": row.sku_id, "fields": missing})
        if row.cost_per_base is not None and not row.currency:
            raise ServiceError("MISSING_CURRENCY", "Укажите валюту закупочной цены", details={"sku_id": row.sku_id})
        if row.incoming_base_qty > 0 and (row.incoming_eta is None or datetime.combine(row.incoming_eta, datetime.max.time(), tzinfo=source.as_of.tzinfo) <= source.as_of):
            raise ServiceError("MISSING_RECEIPT_DATE", "Для ожидаемой поставки укажите дату после даты расчёта", details={"sku_id": row.sku_id})
        if row.lead_time_days + row.review_days > 365:
            raise ServiceError("INVALID_PROTECTION_PERIOD", "Сумма срока поставки и периода закупки не должна превышать 365 дней")

    scopes = {(row.sku_id, row.warehouse_id) for row in request.items}
    skus = {sku for sku, _ in scopes}
    masters = {row["sku_id"]: row for row in _table(source, "sku_master")}
    # Conversion/quantum belong to the SKU, not its warehouse.
    by_sku = {}
    for row in request.items:
        conversion = (row.purchase_uom, row.base_units_per_purchase_uom, row.quantity_quantum)
        if row.sku_id in by_sku and by_sku[row.sku_id] != conversion:
            raise ServiceError("CONFLICTING_PURCHASE_UNITS", "Единицы закупки одного товара должны совпадать на всех складах")
        by_sku[row.sku_id] = conversion

    signature = digest({"preparation_version": "buyer-inputs-v1", "source_snapshot_id": snapshot_id,
                        "source": source.manifest_hash,
                        "request": request.model_dump(mode="json", exclude={"idempotency_key"})})
    result_id = f"buyer-{signature[:32]}"
    with service.store.transaction() as tx:
        reservation = tx.reserve_idempotency("buyer-inputs", request.idempotency_key, digest({"source": snapshot_id, **payload}), result_id)
        if not reservation["created"]:
            return public_snapshot(tx.get_item("snapshots", reservation["result_id"]))
        existing = tx.get_item("snapshots", result_id)
        if existing:
            return public_snapshot(existing)
        root = service.root / "artifacts" / result_id
        tables = {}
        for name in source.tables:
            if name in {"sku_master", "inventory_snapshots", "supplier_terms", "pipeline_lines"}:
                continue
            rows = [row for row in _table(source, name)
                    if (not row.get("sku_id") or row["sku_id"] in skus)
                    and (not row.get("warehouse_id") or not row.get("sku_id") or (row["sku_id"], row["warehouse_id"]) in scopes)]
            tables[name] = write_table(root, name, rows)
        master_rows, stock_rows, term_rows, pipe_rows = [], [], [], []
        for sku, (purchase_uom, conversion, quantum) in sorted(by_sku.items()):
            canonical = {key: value for key, value in masters[sku].items() if key in SkuMaster.model_fields}
            master_rows.append(SkuMaster.model_validate({**canonical, "purchase_uom": purchase_uom,
                "base_units_per_purchase_uom": conversion, "quantity_quantum": quantum, "provenance": "override"}))
        for row in request.items:
            stock_rows.append(InventorySnapshot(sku_id=row.sku_id, warehouse_id=row.warehouse_id,
                as_of=source.as_of, on_hand_base=max(row.free_base, Decimal(0)),
                reserved_base=max(-row.free_base, Decimal(0)), blocked_base=0, free_base=row.free_base,
                accounting_definition_version="buyer-free-stock-v1", provenance="override"))
            term_rows.append(SupplierTerms(**{k: getattr(row, k) for k in (
                "supplier_id", "sku_id", "warehouse_id", "moq_purchase", "pack_multiple_purchase",
                "lead_time_days", "review_days", "cost_per_base", "currency")}, valid_at=source.as_of, provenance="override"))
            if row.incoming_base_qty > 0:
                # Date-only arrival means end of that day, never an earlier receipt.
                eta = datetime.combine(row.incoming_eta, datetime.max.time(), tzinfo=source.as_of.tzinfo)
                pipe_rows.append(PipelineLine(po_line_id=f"buyer-{digest([row.sku_id, row.warehouse_id])[:20]}",
                    sku_id=row.sku_id, warehouse_id=row.warehouse_id, supplier_id=row.supplier_id,
                    remaining_base_qty=row.incoming_base_qty, eta_end=eta, eta_semantics="deadline",
                    status="confirmed", provenance="override"))
        for name, rows in (("sku_master", master_rows), ("inventory_snapshots", stock_rows),
                           ("supplier_terms", term_rows), ("pipeline_lines", pipe_rows)):
            tables[name] = write_table(root, name, rows)

        solved = {"MISSING_PURCHASE_MAPPING", "MISSING_CURRENT_STOCK", "CURRENT_STOCK_UNVERIFIED",
                  "MISSING_SUPPLIER_TERMS", "UNKNOWN_DEMAND_COVERAGE", "INCOMPLETE_DEMAND_COVERAGE"}
        sales_sources = {ref.source_id for ref in source.source_refs if ref.kind == "sales_events"}
        issues = []
        for issue in source.quality.issues:
            scope = [sku for sku in issue.scope_ids if sku in skus]
            if issue.scope_ids and not scope:
                continue
            revised = issue.model_copy(deep=True)
            revised.scope_ids = scope
            if issue.code in solved:
                revised.severity = "info"
                revised.message = f"Исходное ограничение: {issue.message}. Для этого плана закупщик ввёл данные и принял допущения."
            elif issue.code in {"UNSUPPORTED_SIGNED_MOVEMENT", "MISSING_QUANTITY"} and any(
                (issue.source_ref or "").startswith(source_id + ":") for source_id in sales_sources
            ):
                # These rows remain quarantined, never fabricated or reintroduced.
                # Explicit history acceptance permits estimating from surviving sales.
                revised.severity = "warning"
                revised.message = "Исключённые движения и строки без количества не учтены. Для расчёта по оставшейся истории принято допущение закупщика."
            issues.append(revised)
        notice = "План использует введённые закупщиком остатки, условия и поставки. Дни без записей приняты за нулевые продажи; неподтверждённые движения не учтены."
        issues.append(QualityIssue(code="BUYER_INPUTS_CONFIRMED", severity="warning", scope_ids=sorted(skus), message=notice))
        assumptions = [a for a in source.assumptions if a.field not in {"buyer_preparation", "current_stock"}
                       and (not a.scope_ids or set(a.scope_ids).intersection(skus))]
        sales = _table(source, "sales_events")
        for sku, warehouse in sorted(scopes):
            dates = [_time(r["event_at"]).astimezone(timezone.utc).date() for r in sales
                     if (r["sku_id"], r["warehouse_id"]) == (sku, warehouse) and _time(r["event_at"]) <= source.as_of]
            if not dates:
                raise ServiceError("MISSING_DEMAND_HISTORY", "У выбранного товара нет истории продаж", details={"sku_id": sku})
            # Forecast uses complete UTC days and excludes the current partial day.
            end = source.as_of.astimezone(timezone.utc).date()
            if min(dates) >= end:
                raise ServiceError("INSUFFICIENT_DEMAND_HISTORY", "Для прогноза нужна история до даты расчёта", details={"sku_id": sku})
            assumptions.append(Assumption(field="demand_coverage", value=json.dumps({
                "start": min(dates).isoformat(), "end": end.isoformat(), "complete": True,
                "sku_ids": [sku], "warehouse_ids": [warehouse]}, ensure_ascii=False), provenance="override",
                reason=f"Дни без записей приняты за нулевые продажи по решению закупщика. {request.reason.strip()}", scope_ids=[sku]))
        assumptions.append(Assumption(field="buyer_preparation", value=json.dumps({
            "source_snapshot_id": snapshot_id, "actor": service.actor, "confirmed_at": now(),
            "accept_history_estimate": True}, ensure_ascii=False), provenance="override",
            reason=request.reason.strip(), scope_ids=sorted(skus)))
        quality = source.quality.model_copy(deep=True)
        quality.issues = issues
        quality.status = "degraded"
        quality.affected_skus = len(skus)
        quality.capabilities = Capabilities(can_plan=True, can_approve=True, can_export=False,
            budget_available=all(row.cost_per_base is not None and row.currency for row in request.items)
                and len({row.currency for row in request.items}) == 1,
            customer_detection_available=source.quality.capabilities.customer_detection_available,
            observed_stockouts_available=source.quality.capabilities.observed_stockouts_available,
            reasons=[notice])
        result = SnapshotManifest(snapshot_id=result_id, mapping_version=source.mapping_version,
            manifest_hash=signature, mode=source.mode, as_of=source.as_of, created_at=now(),
            source_refs=source.source_refs, tables=tables, quality=quality, assumptions=assumptions)
        tx.create_item("snapshots", result_id, result.model_dump(mode="json"))
        tx.append_audit("snapshots", result_id, "buyer_preparation", {"source_snapshot_id": snapshot_id,
            "actor": service.actor, "reason": request.reason.strip(), "input_hash": signature,
            "selected_items": len(request.items), "accept_history_estimate": True})
        return public_snapshot(result.model_dump(mode="json"))
