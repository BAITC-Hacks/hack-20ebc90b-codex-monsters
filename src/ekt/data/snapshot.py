"""Explicitly mapped local sources -> deterministic, immutable canonical snapshot.

SourceManifest (plain payload at the internal boundary)::

    {"as_of": "2026-09-22T23:59:59+06:00", "mode": "real_preview",
     "artifact_root": "/private/local/artifacts", "sources": [
       {"source_id": "S08", "kind": "sales_events", "path": "/private/sales.xlsx",
        "sheet": "Лист_1", "header_row": 1, "provenance": "observed",
        "coverage": {"start": "2026-01-01", "end": "2026-09-23",
                     "complete": true, "warehouse_ids": ["Алматы"]}}]}

MappingConfig uses ``version``, ``authoritative_sales_sources`` and ``sources``
keyed by source ID. Each source mapping has ``columns`` (canonical->raw header),
``constants``, optional ``timezone``, and raw ``movements`` entries containing
``sign`` (positive/negative/zero), ``demand_effect`` and ``event_type``. A list of
rules per raw type is supported for separately confirmed return/correction signs.
Current stock needs ``current_stock_verified: true``. Conversions are explicit
``uom_conversions: {raw_uom: {base_uom: ..., factor: "..."}}``. No missing
conversion, MOQ, multiple, quantity quantum or stock is replaced with 1 or 0.

Portable canonical fixtures replace ``path`` with ``rows``; all sales must still
provide demand_effect. Coverage intervals are half-open [start,end); completeness
is an explicit assertion about source extraction, never inferred from min/max.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from .artifacts import canonical_json, digest_payload, read_table, write_records
from .errors import DataError
from .sources import decimal_value, source_records, timestamp

TABLES = (
    "sku_master", "sales_events", "inventory_snapshots", "stockout_intervals",
    "pipeline_lines", "supplier_terms", "planning_overrides", "monthly_sales", "monthly_balances",
)
PROVENANCE = {"observed", "derived", "synthetic", "override"}
QUANTITIES = {
    "sku_master": ("base_units_per_purchase_uom", "quantity_quantum"),
    "sales_events": ("quantity_base",),
    "inventory_snapshots": ("on_hand_base", "reserved_base", "blocked_base", "free_base"),
    "pipeline_lines": ("remaining_base_qty",),
    "supplier_terms": ("moq_purchase", "pack_multiple_purchase", "cost_per_base"),
    "monthly_sales": ("value",), "monthly_balances": ("value",),
}
REQUIRED = {
    "sku_master": ("sku_id", "name", "base_uom", "supplier_id"),
    "sales_events": ("sku_id", "warehouse_id", "event_at", "quantity_base", "event_type", "demand_effect"),
    "inventory_snapshots": ("sku_id", "warehouse_id", "as_of", "accounting_definition_version"),
    "stockout_intervals": ("sku_id", "warehouse_id", "start_at", "unavailable_fraction", "evidence", "confidence"),
    "pipeline_lines": ("po_line_id", "sku_id", "supplier_id", "warehouse_id", "remaining_base_qty", "eta_semantics", "status"),
    "supplier_terms": ("sku_id", "supplier_id", "warehouse_id", "valid_at"),
    "planning_overrides": ("scope_id", "parameter", "value", "valid_from", "reason"),
    "monthly_sales": ("period_start", "period_end", "sku_id", "measure", "coverage"),
    "monthly_balances": ("period_start", "period_end", "sku_id", "measure", "coverage"),
}
TIME_FIELDS = {"event_at", "as_of", "start_at", "end_at", "valid_at", "valid_from", "valid_to"}


def _present(value):
    return value is not None and value != ""


def _time(value, mapping, field):
    if isinstance(value, str):
        try:
            explicit_format = mapping.get("date_formats", {}).get(field)
            value = datetime.strptime(value, explicit_format) if explicit_format else datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    if isinstance(value, datetime) and value.tzinfo is None:
        if not mapping.get("timezone"):
            raise DataError("MISSING_TIMEZONE", f"Explicit source timezone required for {field}")
        value = value.replace(tzinfo=ZoneInfo(mapping["timezone"]))
    return timestamp(value, field)


def _instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _issue(code, message, sku=None, source_ref=None, severity="blocking"):
    return {"code": code, "severity": severity, "scope_ids": [str(sku)] if sku else [],
            "source_ref": source_ref, "message": message}


def _normalize(raw, source, mapping, locator, fingerprint):
    kind = source["kind"]
    columns = mapping.get("columns")
    row = ({canonical: raw.get(header) for canonical, header in columns.items()} if columns else dict(raw))
    for field, value in mapping.get("constants", {}).items():
        if _present(row.get(field)) and row[field] != value:
            raise DataError("CONSTANT_CONFLICT", f"Source value conflicts with confirmed constant {field}")
        row[field] = value
    if not any(_present(value) for value in raw.values()):
        return None
    if not _present(row.get("sku_id")) and kind != "planning_overrides":
        # Report summary rows are not SKU records; no arbitrary product is created.
        return None
    if "sku_id" in row:
        if not isinstance(row["sku_id"], str):
            raise DataError("NON_TEXT_SKU", "SKU must be text; leading zeros cannot be inferred")
        row["sku_id"] = row["sku_id"].strip()
        if not row["sku_id"]:
            return None
        if mapping.get("sku_filter") is not None and row["sku_id"] not in mapping["sku_filter"]:
            return None
    provenance = row.get("provenance", source.get("provenance", "observed"))
    if provenance not in PROVENANCE:
        raise DataError("INVALID_PROVENANCE", "Unknown source provenance")
    row["provenance"] = provenance
    row["source_id"] = source["source_id"]
    row["row_ref"] = locator
    for field in QUANTITIES.get(kind, ()):
        row[field] = decimal_value(row.get(field), field, nullable=field not in REQUIRED[kind])
    for field in TIME_FIELDS.intersection(row):
        if _present(row[field]):
            row[field] = _time(row[field], mapping, field)
        else:
            row[field] = None
    if kind == "sales_events":
        raw_quantity = row["quantity_base"]
        row["raw_quantity"] = raw_quantity
        row["raw_event_type"] = str(row.get("event_type", ""))
        movements = mapping.get("movements")
        if movements is not None:
            movement_type = row["raw_event_type"]
            matching_prefixes = [mapped for prefix, mapped in mapping.get("movement_prefixes", {}).items()
                                 if movement_type.startswith(prefix)]
            if len(matching_prefixes) > 1:
                raise DataError("AMBIGUOUS_MOVEMENT_TYPE", "Document matches multiple configured movement prefixes")
            if matching_prefixes:
                movement_type = matching_prefixes[0]
            candidates = movements.get(movement_type, [])
            if isinstance(candidates, dict):
                candidates = [candidates]
            sign = "positive" if raw_quantity > 0 else "negative" if raw_quantity < 0 else "zero"
            rules = [candidate for candidate in candidates if candidate.get("sign") == sign]
            if len(rules) != 1:
                raise DataError("UNSUPPORTED_SIGNED_MOVEMENT", "Movement type and sign do not match one confirmed rule")
            row["event_type"] = rules[0]["event_type"]
            row["demand_effect"] = rules[0]["demand_effect"]
        elif "rows" not in source and not mapping.get("canonical_sales_verified"):
            raise DataError("UNCONFIRMED_MOVEMENT_SEMANTICS", "Imported sales require explicit signed movement mapping")
        if row.get("demand_effect") not in {"increase", "decrease", "none"}:
            raise DataError("UNSUPPORTED_DEMAND_EFFECT", "Sales movement has no confirmed demand effect")
        uom = row.get("raw_uom", row.get("base_uom"))
        if "uom_conversions" in mapping:
            conversion = mapping["uom_conversions"].get(uom)
            if not isinstance(conversion, dict) or not conversion.get("base_uom"):
                raise DataError("UNKNOWN_UOM_CONVERSION", "No confirmed base conversion for source UOM")
            factor = decimal_value(conversion.get("factor"), "uom_conversion")
            if factor <= 0:
                raise DataError("INVALID_UOM_CONVERSION", "UOM conversion must be positive")
            row["quantity_base"] *= factor
            row["base_uom"] = conversion["base_uom"]
            row["raw_uom"] = str(uom)
        elif _present(row.get("raw_uom")) and row.get("raw_uom") != row.get("base_uom"):
            raise DataError("UNKNOWN_UOM_CONVERSION", "Source UOM differs from canonical UOM without a conversion")
        row.setdefault("customer_token", None)
        row.setdefault("doc_id", None)
        row.setdefault("event_id", digest_payload({"source": source["source_id"], "checksum": fingerprint, "row": locator})[:32])
    if kind == "inventory_snapshots":
        if provenance != "synthetic" and not mapping.get("current_stock_verified"):
            raise DataError("CURRENT_STOCK_UNVERIFIED", "Current stock definition and timestamp have not been verified")
        row["current_stock_verified"] = provenance == "synthetic" or mapping.get("current_stock_verified") is True
    for field in REQUIRED[kind]:
        if not _present(row.get(field)):
            raise DataError("MISSING_REQUIRED_FIELD", f"Missing required {kind}.{field}")
    for field in ("base_units_per_purchase_uom", "quantity_quantum", "pack_multiple_purchase"):
        if row.get(field) is not None and row[field] <= 0:
            raise DataError("INVALID_PURCHASE_CONSTRAINT", f"{field} must be positive")
    for field in ("moq_purchase", "cost_per_base", "remaining_base_qty", "reserved_base", "blocked_base"):
        if row.get(field) is not None and row[field] < 0:
            raise DataError("NEGATIVE_CONSTRAINT", f"{field} must not be negative")
    for field in ("lead_time_days", "review_days"):
        if row.get(field) is not None:
            value = decimal_value(row[field], field)
            if value < 0 or value != value.to_integral_value():
                raise DataError("INVALID_TERM_DAYS", f"{field} must be a nonnegative integer")
            row[field] = int(value)
    if kind == "stockout_intervals":
        for field in ("unavailable_fraction", "confidence"):
            value = decimal_value(row[field], field)
            if not 0 <= value <= 1:
                raise DataError("INVALID_INTERVAL", f"{field} must be between zero and one")
            row[field] = float(value)
        if row.get("end_at") and _instant(row["end_at"]) <= _instant(row["start_at"]):
            raise DataError("INVALID_INTERVAL", "Stockout end must follow start")
        if row["evidence"] not in {"observed", "inferred", "synthetic"}:
            raise DataError("INVALID_EVIDENCE", "Unknown stockout evidence")
    if kind == "pipeline_lines" and row["eta_semantics"] not in {"window", "deadline", "unknown"}:
        raise DataError("INVALID_ETA_SEMANTICS", "Unknown ETA semantics")
    if kind == "planning_overrides":
        row["value"] = str(row["value"])
    return row


def _deduplicate(table_name, rows, issues, quarantine):
    keys = {
        "sku_master": ("sku_id",), "sales_events": ("event_id",),
        "supplier_terms": ("supplier_id", "sku_id", "warehouse_id", "valid_at"),
        "inventory_snapshots": ("sku_id", "warehouse_id", "as_of"),
        "pipeline_lines": ("po_line_id",),
    }.get(table_name)
    if not keys:
        return rows
    grouped = defaultdict(list)
    for row in rows:
        grouped[tuple(row.get(key) for key in keys)].append(row)
    result = []
    for group in grouped.values():
        if len(group) == 1:
            result.append(group[0])
            continue
        # Only explicit event IDs authorize event replay deduplication. Similar
        # document quantities with distinct IDs remain independent observations.
        contents = [{k: v for k, v in row.items() if k not in {"row_ref", "source_id"}} for row in group]
        if table_name == "sales_events" and len({canonical_json(row) for row in contents}) == 1:
            result.append(group[0])
            continue
        for row in group:
            issue = _issue("DUPLICATE_CANONICAL_KEY", f"Conflicting or duplicate {table_name} key requires source reconciliation", row.get("sku_id"), row["source_id"])
            issues.append(issue)
            quarantine.append({"source_id": row["source_id"], "row_ref": row["row_ref"], "sku_id": row.get("sku_id"), "code": issue["code"]})
    return result


def _quality(tables, issues, rejected_rows, all_skus):
    master = {row["sku_id"]: row for row in tables["sku_master"]}
    sales_pairs = {(row["sku_id"], row["warehouse_id"]) for row in tables["sales_events"] if row["demand_effect"] != "none"}
    stock = {(row["sku_id"], row["warehouse_id"]): row for row in sorted(tables["inventory_snapshots"], key=lambda row: _instant(row["as_of"]))}
    terms = {(row["sku_id"], row["warehouse_id"]): row for row in sorted(tables["supplier_terms"], key=lambda row: _instant(row["valid_at"]))}
    eligible = []
    for sku in sorted(all_skus):
        item = master.get(sku)
        if item is None:
            issues.append(_issue("MISSING_SKU_MASTER", "SKU has no valid canonical master record", sku))
            continue
        for field in ("purchase_uom", "base_units_per_purchase_uom", "quantity_quantum", "supplier_id"):
            if not _present(item.get(field)):
                issues.append(_issue("MISSING_PURCHASE_MAPPING", f"Missing confirmed {field}", sku))
        pairs = [pair for pair in sales_pairs if pair[0] == sku]
        if not pairs:
            issues.append(_issue("MISSING_DEMAND_HISTORY", "No supported demand history for SKU", sku))
        for pair in pairs:
            inventory = stock.get(pair)
            if inventory is None or any(inventory.get(field) is None for field in QUANTITIES["inventory_snapshots"]):
                issues.append(_issue("MISSING_CURRENT_STOCK", "Verified current stock/netting fields are incomplete", sku))
            supplier_terms = terms.get(pair)
            if supplier_terms is None:
                issues.append(_issue("MISSING_SUPPLIER_TERMS", "Supplier terms are missing", sku))
            else:
                if supplier_terms["supplier_id"] != item["supplier_id"]:
                    issues.append(_issue("SUPPLIER_MAPPING_MISMATCH", "Terms supplier does not match SKU supplier", sku))
                for field in ("moq_purchase", "pack_multiple_purchase", "lead_time_days", "review_days"):
                    if supplier_terms.get(field) is None:
                        issues.append(_issue("MISSING_SUPPLIER_TERMS", f"Missing confirmed {field}", sku))
            if not any(issue["severity"] == "blocking" and sku in issue["scope_ids"] for issue in issues):
                eligible.append(pair)
    can_plan = bool(eligible)
    currencies = {terms[pair].get("currency") for pair in eligible}
    budget_available = can_plan and len(currencies) == 1 and all(_present(value) for value in currencies) and all(terms[pair].get("cost_per_base") is not None for pair in eligible)
    observed_stockouts = any(row["evidence"] == "observed" for row in tables["stockout_intervals"])
    if not observed_stockouts:
        issues.append(_issue("OBSERVED_STOCKOUTS_UNAVAILABLE", "Historical observed stockout logs unavailable; no real recovery intervals inferred", severity="warning"))
    forecastable = any(sku in master for sku, _warehouse in sales_pairs)
    capabilities = {"can_plan": can_plan, "can_approve": can_plan, "can_export": can_plan,
                    "budget_available": bool(budget_available),
                    "customer_detection_available": any(row.get("customer_token") for row in tables["sales_events"]),
                    "observed_stockouts_available": observed_stockouts,
                    "reasons": sorted({issue["code"] for issue in issues})}
    return {"status": "ready" if not issues else "degraded" if forecastable else "blocked",
            "accepted_rows": sum(len(rows) for rows in tables.values()), "rejected_rows": rejected_rows,
            "affected_skus": len({sku for issue in issues for sku in issue["scope_ids"]}),
            "issues": issues, "capabilities": capabilities}


def build_snapshot_payload(source_manifest: dict, mapping_config: dict) -> dict:
    """Build a completed snapshot; never publish partial tables or guessed values."""
    as_of = timestamp(source_manifest.get("as_of"), "as_of")
    mode = source_manifest.get("mode")
    if mode not in {"real_preview", "synthetic_demo"}:
        raise DataError("INVALID_MODE", "Explicit real_preview or synthetic_demo mode required")
    version = mapping_config.get("version", mapping_config.get("mapping_version"))
    if not isinstance(version, str) or not version:
        raise DataError("MISSING_MAPPING_VERSION", "Mapping version is required")
    sources = source_manifest.get("sources", [])
    ids = [source.get("source_id") for source in sources]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise DataError("INVALID_SOURCE_ID", "Source IDs must be nonempty and unique")
    authoritative = mapping_config.get("authoritative_sales_sources")
    sales_ids = [source["source_id"] for source in sources if source.get("kind") == "sales_events"]
    if authoritative is None:
        if len(sales_ids) > 1:
            raise DataError("AMBIGUOUS_DEMAND_AUTHORITY", "Declare authoritative_sales_sources when multiple sales sources are registered")
        authoritative = sales_ids
    if set(authoritative) - set(sales_ids):
        raise DataError("INVALID_DEMAND_AUTHORITY", "Authoritative sales source is not registered")
    tables = {name: [] for name in TABLES}
    issues, quarantine, source_refs, assumptions, coverage = [], [], [], [], []
    for assumption in mapping_config.get("assumptions", []):
        if assumption.get("provenance") not in PROVENANCE:
            raise DataError("INVALID_PROVENANCE", "Assumption provenance is required")
        normalized_assumption = dict(assumption)
        if not isinstance(normalized_assumption.get("value"), str):
            normalized_assumption["value"] = canonical_json(normalized_assumption.get("value"))
        assumptions.append(normalized_assumption)
        if assumption["provenance"] == "synthetic" or assumption.get("assumed"):
            mode = "synthetic_demo"
    all_skus, fingerprints = set(), []
    for source in sources:
        source_id, kind = source["source_id"], source.get("kind")
        if kind not in TABLES:
            raise DataError("UNSUPPORTED_SOURCE_KIND", f"Unsupported canonical source kind: {kind}")
        mapping = mapping_config.get("sources", {}).get(source_id, {})
        if source.get("metadata_only"):
            from .artifacts import checksum
            path = Path(source["path"]).expanduser().resolve()
            fingerprint = checksum(path)
            source_refs.append({"source_id": source_id, "checksum": fingerprint, "kind": kind, "local_ref": str(path)})
            fingerprints.append({"metadata": {key: value for key, value in source.items() if key != "path"}, "checksum": fingerprint})
            issues.append(_issue("METADATA_ONLY_SOURCE", "Source registered for supplier/quality metadata; business definitions are unverified", source_ref=source_id, severity="warning"))
            continue
        records, fingerprint, _headers = source_records(source, mapping)
        source_refs.append({"source_id": source_id, "checksum": fingerprint, "kind": kind,
                            "local_ref": str(Path(source["path"]).expanduser().resolve()) if source.get("path") else f"inline:{source_id}"})
        fingerprints.append({"metadata": {key: value for key, value in source.items() if key not in {"rows", "path"}}, "checksum": fingerprint})
        if kind == "sales_events" and source_id not in authoritative:
            issues.append(_issue("NON_AUTHORITATIVE_SOURCE", "Sales source retained as metadata only to prevent overlapping demand double counting", source_ref=source_id, severity="info"))
            continue
        if kind == "sales_events":
            value = source.get("coverage")
            if value:
                if not {"start", "end", "complete"}.issubset(value) or not isinstance(value["complete"], bool):
                    raise DataError("INVALID_COVERAGE", "Coverage requires start, end and explicit boolean complete")
                try:
                    start = datetime.fromisoformat(value["start"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(value["end"].replace("Z", "+00:00"))
                    if end <= start:
                        raise ValueError()
                except (ValueError, TypeError):
                    raise DataError("INVALID_COVERAGE", "Coverage must be a nonempty half-open ISO interval") from None
                value = {**value, "source_id": source_id}
                coverage.append(value)
                assumptions.append({"field": "demand_coverage", "value": canonical_json(value),
                                    "provenance": source.get("provenance", "observed"),
                                    "reason": "Authoritative extraction coverage; end is exclusive, completeness explicitly declared",
                                    "scope_ids": value.get("sku_ids", [])})
                if not value["complete"]:
                    issues.append(_issue("INCOMPLETE_DEMAND_COVERAGE", "Source extraction has incomplete demand coverage", source_ref=source_id, severity="warning"))
            else:
                issues.append(_issue("UNKNOWN_DEMAND_COVERAGE", "No completeness assertion; missing dates must not be filled as observed zero demand", source_ref=source_id, severity="warning"))
            assumptions.append({"field": "event_identity", "value": "explicit event_id or source checksum + row locator",
                                "provenance": "derived", "reason": "File replay is idempotent; cross-export deduplication requires stable ERP event IDs", "scope_ids": []})
        for locator, raw in records:
            sku_header = mapping.get("columns", {}).get("sku_id", "sku_id")
            raw_sku = raw.get(sku_header)
            if mapping.get("sku_filter") is not None and raw_sku not in mapping["sku_filter"]:
                continue
            if isinstance(raw_sku, str) and raw_sku.strip():
                all_skus.add(raw_sku.strip())
            try:
                row = _normalize(raw, source, mapping, locator, fingerprint)
                if row is None:
                    continue
                temporal_field = {"sales_events": "event_at", "inventory_snapshots": "as_of", "supplier_terms": "valid_at", "stockout_intervals": "start_at"}.get(kind)
                if temporal_field and _instant(row[temporal_field]) > _instant(as_of):
                    continue
                if row.get("sku_id"):
                    all_skus.add(row["sku_id"])
                if row["provenance"] == "synthetic" or source.get("assumed") or row.get("assumed"):
                    mode = "synthetic_demo"
                tables[kind].append(row)
            except DataError as error:
                sku = str(raw_sku) if raw_sku is not None else None
                issues.append(_issue(error.code, str(error), sku, f"{source_id}:{locator}"))
                quarantine.append({"source_id": source_id, "row_ref": locator, "sku_id": sku, "code": error.code})
    for table_name in TABLES:
        tables[table_name] = _deduplicate(table_name, tables[table_name], issues, quarantine)
        tables[table_name].sort(key=canonical_json)
    # A sales UOM must match its canonical master, otherwise the quantity is unusable.
    master = {row["sku_id"]: row for row in tables["sku_master"]}
    accepted_sales = []
    for row in tables["sales_events"]:
        if row.get("base_uom") and row["sku_id"] in master and row["base_uom"] != master[row["sku_id"]]["base_uom"]:
            issues.append(_issue("SALES_UOM_MISMATCH", "Sales base UOM differs from canonical SKU UOM", row["sku_id"], row["source_id"]))
            quarantine.append({"source_id": row["source_id"], "row_ref": row["row_ref"], "sku_id": row["sku_id"], "code": "SALES_UOM_MISMATCH"})
        else:
            accepted_sales.append(row)
    tables["sales_events"] = accepted_sales
    quality = _quality(tables, issues, len(quarantine), all_skus)
    identity = {"schema_version": "1.0", "data_version": "b1.1", "mapping": mapping_config,
                "sources": fingerprints, "mode": mode, "as_of": as_of}
    snapshot_id = digest_payload(identity)
    root = Path(source_manifest.get("artifact_root", "~/.local/share/ekt/artifacts")).expanduser().resolve()
    final_dir = root / "snapshots" / snapshot_id
    manifest_path = final_dir / "manifest.json"
    if final_dir.exists():
        if not manifest_path.is_file():
            raise DataError("INCOMPLETE_SNAPSHOT", "Existing snapshot directory has no completed manifest")
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected = existing.pop("manifest_hash")
        if digest_payload(existing) != expected:
            raise DataError("MANIFEST_CHECKSUM_MISMATCH", "Existing snapshot manifest was modified")
        existing["manifest_hash"] = expected
        for ref in existing["tables"].values():
            read_table(ref)
        return existing
    final_dir.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=final_dir.parent))
    try:
        refs = {}
        for name, rows in {**tables, "quarantine": quarantine}.items():
            ref = write_records(stage / f"{name}.parquet", rows)
            ref["uri"] = str(final_dir / f"{name}.parquet")
            refs[name] = ref
        payload = {"schema_version": "1.0", "snapshot_id": snapshot_id, "mapping_version": version,
                   "mode": mode, "as_of": as_of, "created_at": as_of, "source_refs": source_refs,
                   "tables": refs, "quality": quality, "assumptions": assumptions}
        payload["manifest_hash"] = digest_payload(payload)
        (stage / "manifest.json").write_text(canonical_json(payload) + "\n", encoding="utf-8")
        try:
            os.rename(stage, final_dir)
        except OSError:
            if not manifest_path.is_file():
                raise
            shutil.rmtree(stage)
            return build_snapshot_payload(source_manifest, mapping_config)
        return payload
    finally:
        if stage.exists():
            shutil.rmtree(stage)
