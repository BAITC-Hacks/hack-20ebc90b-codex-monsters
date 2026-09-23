"""Portable, conservative adapters for the supplied Systeme workbook layout.

The preset recognizes positive ``Расходная накладная`` rows as shipment preview
evidence. Negative corrections and all other document families are quarantined.
This rule is disclosed as a preview interpretation and does not certify ERP
posting completeness. Purchase terms/current stock remain blocked until confirmed.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from .errors import DataError
from .sources import source_records


def _single(directory, pattern):
    matches = sorted(directory.glob(pattern))
    if len(matches) != 1:
        raise DataError("SOURCE_NOT_FOUND", f"Expected one workbook matching {pattern}")
    return matches[0]


def systeme_preview_manifest(source_root, artifact_root, as_of, sku_limit=20, *, source_paths=None):
    """Return ``(SourceManifest payload, MappingConfig payload)`` for local files.

    ``source_root`` contains extracted ``IEK`` and ``Systeme electric`` directories.
    The scope is the most frequent 20 piece SKUs (ties use the textual SKU). No
    source paths or commercial rows are embedded in this module. Missing days are
    not declared zero: coverage.complete remains false until ERP confirmation.
    ``source_paths`` optionally supplies exact registered paths keyed by S07,
    S08, S12 and optional S01. In that mode no directory discovery is performed
    and no unregistered optional workbook is read.
    """
    if not isinstance(sku_limit, int) or sku_limit < 1:
        raise DataError("INVALID_SCOPE", "sku_limit must be a positive integer")
    optional_iek = None
    if source_paths is not None:
        if set(source_paths) - {"S07", "S08", "S12", "S01"} or {"S07", "S08", "S12"} - set(source_paths):
            raise DataError("INVALID_PRESET_SOURCES", "Systeme preset requires registered S07, S08, S12 and optional S01 only")
        paths = {key: Path(value).expanduser().resolve() for key, value in source_paths.items()}
        sales_path, multiple_path, candidate_path = paths["S08"], paths["S07"], paths["S12"]
        optional_iek = paths.get("S01")
    else:
        root = Path(source_root).expanduser().resolve()
        systeme = root / "Systeme electric"
        sales_path = _single(systeme, "Динамика*")
        multiple_path = _single(systeme, "MOQ*")
        candidate_path = _single(systeme, "Товар в пути*")
        iek = root / "IEK"
        if iek.is_dir():
            optional_iek = _single(iek, "MOQ*")
    sales_source = {"source_id": "S08", "kind": "sales_events", "path": str(sales_path),
                    "sheet": "Лист_1", "header_row": 1, "provenance": "observed"}
    columns = {"event_at": "Дата", "doc_id": "Номер", "event_type": "Документ", "sku_id": "Код",
               "name": "Номенклатура", "base_uom": "Ед.", "warehouse_id": "Склад", "quantity_base": "Количество"}
    records, source_hash, _ = source_records(sales_source, {"columns": columns})
    counts, uoms, names, dates, warehouses = Counter(), defaultdict(set), {}, [], set()
    end_date = datetime.fromisoformat(as_of.replace("Z", "+00:00")).date()
    movement_counts = Counter()
    for _, row in records:
        sku = row.get("Код")
        if not isinstance(sku, str) or not sku:
            continue
        document = str(row.get("Документ", ""))
        movement_type = "shipment_document" if document.startswith("Расходная накладная ") else "customer_order" if document.startswith("Заказ клиента ") else "other"
        quantity = row.get("Количество")
        sign = "positive" if isinstance(quantity, (int, float)) and quantity > 0 else "negative" if isinstance(quantity, (int, float)) and quantity < 0 else "zero_or_unknown"
        movement_counts[f"{movement_type}:{sign}"] += 1
        event_date = row.get("Дата")
        if isinstance(event_date, str):
            try:
                event_date = datetime.strptime(event_date, "%d.%m.%Y %H:%M:%S")
            except ValueError:
                continue
        if not isinstance(event_date, datetime) or event_date.date() > end_date:
            continue
        uoms[sku].add(row.get("Ед."))
        if movement_type == "shipment_document" and sign == "positive":
            counts[sku] += 1
            names[sku] = str(row.get("Номенклатура", ""))
            dates.append(event_date.date())
            warehouses.add(str(row.get("Склад")))
    eligible = [sku for sku in counts if uoms[sku] == {"шт"}]
    selected = sorted(eligible, key=lambda sku: (-counts[sku], sku))[:sku_limit]
    if not selected:
        raise DataError("NO_SUPPORTED_SUBSET", "No positive piece-unit shipment preview subset found")
    sales_source["coverage"] = {"start": min(dates).isoformat(), "end": (min(end_date, max(dates)) + timedelta(days=1)).isoformat(),
                                "complete": False, "sku_ids": selected, "warehouse_ids": sorted(warehouses)}
    master = {"source_id": "S08-master", "kind": "sku_master", "provenance": "derived",
              "derived_from": {"source_id": "S08", "checksum": source_hash},
              "rows": [{"sku_id": sku, "name": names[sku], "base_uom": "шт", "supplier_id": "systeme",
                        "purchase_uom": None, "quantity_quantum": None, "base_units_per_purchase_uom": None}
                       for sku in selected]}
    sources = [master, sales_source,
               {"source_id": "S07", "kind": "supplier_terms", "path": str(multiple_path), "sheet": "Лист_1", "provenance": "observed"},
               {"source_id": "S12", "kind": "inventory_snapshots", "path": str(candidate_path), "metadata_only": True, "provenance": "observed"}]
    if optional_iek is not None:
        sources.append({"source_id": "S01", "kind": "supplier_terms", "path": str(optional_iek), "metadata_only": True, "provenance": "observed"})
    mapping = {"version": "systeme-preview-v1", "authoritative_sales_sources": ["S08"], "sources": {
        "S08": {"columns": columns, "timezone": "Asia/Almaty", "date_formats": {"event_at": "%d.%m.%Y %H:%M:%S"}, "sku_filter": selected,
                "movement_prefixes": {"Расходная накладная ": "shipment_document"},
                "movements": {"shipment_document": {"sign": "positive", "demand_effect": "increase", "event_type": "shipment"}}},
        "S07": {"columns": {"sku_id": "Номенклатура.Код", "pack_multiple_purchase": "Кратность"},
                "sku_filter": selected, "constants": {"supplier_id": "systeme", "warehouse_id": "Алматы", "valid_at": as_of}},
    }, "assumptions": [
        {"field": "supplier_registration", "value": "systeme:S07/S08/S12" + ("; iek:S01 metadata-only" if optional_iek is not None else ""), "provenance": "observed", "reason": "Separate supplier/source identifiers; IEK full ingest unavailable", "scope_ids": ["systeme", "iek"] if optional_iek is not None else ["systeme"]},
        {"field": "movement_interpretation", "value": "positive shipment-document preview only", "provenance": "derived", "reason": "Document-family interpretation; negative corrections and customer orders quarantined, ERP completeness not certified", "scope_ids": selected},
        {"field": "source_sign_profile", "value": str(dict(sorted(movement_counts.items()))), "provenance": "derived", "reason": "Aggregate source structure; no document IDs or customer identity inferred", "scope_ids": []},
        {"field": "current_stock", "value": "unverified", "provenance": "observed", "reason": "S12 stock/UOM/cost definitions need confirmation; no canonical current stock or inferred stockouts emitted", "scope_ids": selected},
    ]}
    manifest = {"as_of": as_of, "mode": "real_preview", "artifact_root": str(Path(artifact_root).expanduser().resolve()), "sources": sources}
    return manifest, mapping
