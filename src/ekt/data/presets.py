"""Portable, conservative adapters for the supplied supplier workbook layouts.

The preset recognizes positive ``Расходная накладная`` rows as shipment preview
evidence. Negative corrections and all other document families are quarantined.
This rule is disclosed as a preview interpretation and does not certify ERP
posting completeness. Purchase conditions/current stock need buyer input.
"""

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from fnmatch import fnmatch
from pathlib import Path
from zoneinfo import ZoneInfo

from .errors import DataError
from .sources import source_records


def _single(directory, pattern):
    # Finder can unpack CP866 ZIP names as MacRoman. Match the decoded display
    # name as well, without renaming user files or reading an ambiguous source.
    matches = []
    for path in directory.iterdir() if directory.is_dir() else []:
        if not path.is_file():
            continue
        names = {path.name}
        try:
            names.add(path.name.encode("mac_roman").decode("cp866"))
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
        if any(fnmatch(name, pattern) for name in names):
            matches.append(path)
    matches.sort()
    if len(matches) != 1:
        raise DataError("SOURCE_NOT_FOUND", f"Expected one workbook matching {pattern}")
    return matches[0]


def systeme_preview_manifest(source_root, artifact_root, as_of, sku_limit=20, *, source_paths=None):
    """Return ``(SourceManifest payload, MappingConfig payload)`` for local files.

    ``source_root`` contains extracted ``IEK`` and ``Systeme electric`` directories.
    The scope is up to ``sku_limit`` most frequent piece SKUs (default 20;
    ties use the textual SKU). No
    source paths or commercial rows are embedded in this module. Missing days are
    not declared zero: coverage.complete remains false until ERP confirmation.
    ``source_paths`` optionally supplies exact registered paths keyed by S07,
    S08, S12 and optional S01. In that mode no directory discovery is performed
    and no unregistered optional workbook is read.
    """
    if isinstance(sku_limit, bool) or not isinstance(sku_limit, int) or sku_limit < 1:
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
    cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise DataError("MISSING_TIMEZONE", "Explicit timezone required for as_of")
    source_timezone = ZoneInfo("Asia/Almaty")
    end_date = cutoff.astimezone(source_timezone).date()
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
        if not isinstance(event_date, datetime):
            continue
        if event_date.tzinfo is None:
            event_date = event_date.replace(tzinfo=source_timezone)
        if event_date > cutoff:
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


def iek_preview_manifest(source_root, artifact_root, as_of, sku_limit=20, *, source_paths=None):
    """Import IEK shipment history in its observed, consistent SKU units.

    Piece, metre and pack histories are separate physical units, never converted
    by a guessed factor. S01's allowed-shipment minimum and S05's dated pipeline
    stay registered reference evidence: the buyer supplies purchase units,
    conversion, MOQ/multiple, stock and dated incoming quantities separately.
    """
    if isinstance(sku_limit, bool) or not isinstance(sku_limit, int) or sku_limit < 1:
        raise DataError("INVALID_SCOPE", "sku_limit must be a positive integer")
    if source_paths is not None:
        if set(source_paths) != {"S01", "S02", "S05"}:
            raise DataError("INVALID_PRESET_SOURCES", "IEK preset requires registered S01, S02 and S05 only")
        paths = {key: Path(value).expanduser().resolve() for key, value in source_paths.items()}
    else:
        directory = Path(source_root).expanduser().resolve() / "IEK"
        paths = {"S01": _single(directory, "MOQ*"), "S02": _single(directory, "Динамика*"),
                 "S05": _single(directory, "Путь*")}
    columns = {"event_at": "Дата", "doc_id": "Номер", "event_type": "Документ", "sku_id": "Код",
               "name": "Номенклатура", "base_uom": "Ед.", "warehouse_id": "Склад", "quantity_base": "Количество"}
    sales = {"source_id": "S02", "kind": "sales_events", "path": str(paths["S02"]),
             "sheet": "Лист_1", "header_row": 1, "provenance": "observed"}
    records, source_hash, _ = source_records(sales, {"columns": columns})
    cutoff = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise DataError("MISSING_TIMEZONE", "Explicit timezone required for as_of")
    source_timezone = ZoneInfo("Asia/Almaty")
    counts, uoms, names, dates, warehouses = Counter(), defaultdict(set), {}, [], set()
    for _, row in records:
        sku = row.get("Код")
        if not isinstance(sku, str) or not sku.strip():
            continue
        instant = row.get("Дата")
        if isinstance(instant, str):
            try:
                instant = datetime.strptime(instant, "%d.%m.%Y %H:%M:%S")
            except ValueError:
                continue
        if not isinstance(instant, datetime):
            continue
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=source_timezone)
        if instant > cutoff:
            continue
        uoms[sku].add(row.get("Ед."))
        quantity = row.get("Количество")
        if (str(row.get("Документ", "")).startswith("Расходная накладная ")
                and not isinstance(quantity, bool) and isinstance(quantity, (int, float)) and quantity > 0):
            counts[sku] += 1
            names[sku] = str(row.get("Номенклатура", ""))
            dates.append(instant.date())
            warehouses.add(str(row.get("Склад")))
    eligible = [sku for sku in counts if len(uoms[sku]) == 1 and uoms[sku] <= {"шт", "м", "упак"}]
    selected = sorted(eligible, key=lambda sku: (-counts[sku], sku))[:sku_limit]
    if not selected:
        raise DataError("NO_SUPPORTED_SUBSET", "No positive shipment preview with consistent supported SKU units found")
    sales["coverage"] = {
        "start": min(dates).isoformat(),
        "end": (min(cutoff.astimezone(source_timezone).date(), max(dates)) + timedelta(days=1)).isoformat(),
        "complete": False, "sku_ids": selected, "warehouse_ids": sorted(warehouses),
    }
    master = {"source_id": "S02-master", "kind": "sku_master", "provenance": "derived",
              "derived_from": {"source_id": "S02", "checksum": source_hash}, "rows": [
                  {"sku_id": sku, "name": names[sku], "base_uom": next(iter(uoms[sku])), "supplier_id": "iek",
                   "purchase_uom": None, "quantity_quantum": None, "base_units_per_purchase_uom": None}
                  for sku in selected]}
    sources = [master, sales,
               {"source_id": "S01", "kind": "supplier_terms", "path": str(paths["S01"]),
                "metadata_only": True, "provenance": "observed"},
               {"source_id": "S05", "kind": "pipeline_lines", "path": str(paths["S05"]),
                "metadata_only": True, "provenance": "observed"}]
    mapping = {"version": "iek-preview-v1", "authoritative_sales_sources": ["S02"], "sources": {
        "S02": {"columns": columns, "timezone": "Asia/Almaty", "date_formats": {"event_at": "%d.%m.%Y %H:%M:%S"},
                "sku_filter": selected, "movement_prefixes": {"Расходная накладная ": "shipment_document"},
                "movements": {"shipment_document": {"sign": "positive", "demand_effect": "increase", "event_type": "shipment"}}},
    }, "assumptions": [
        {"field": "supplier_registration", "value": "iek:S01/S02/S05", "provenance": "observed",
         "reason": "IEK positive shipment preview; terms and pipeline are reference evidence", "scope_ids": ["iek"]},
        {"field": "movement_interpretation", "value": "positive shipment-document preview only", "provenance": "derived",
         "reason": "Negative corrections, receipts and customer orders quarantined; ERP completeness not certified", "scope_ids": selected},
        {"field": "purchase_mapping", "value": "buyer_input_required", "provenance": "derived",
         "reason": "Observed piece/metre/pack units retained; coil conversion, minimum, multiple and pipeline deadlines require buyer input", "scope_ids": selected},
    ]}
    return {"as_of": as_of, "mode": "real_preview",
            "artifact_root": str(Path(artifact_root).expanduser().resolve()), "sources": sources}, mapping
