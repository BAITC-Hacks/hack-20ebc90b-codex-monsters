"""Adapt dictionaries to A's runtime contracts without defining competing models.

While A's foundation is absent, functions return the documented wire dictionary.
Once ``ekt.contracts`` exports the named Pydantic model, every result is validated
and returned as that model. Validation errors are deliberately not swallowed.
"""
from __future__ import annotations

import importlib
from collections.abc import Mapping
from typing import Any

from .errors import DataError


def as_payload(value: Any) -> dict:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="python")
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("Expected a shared contract model or mapping")


def snapshot_inputs(source_manifest: Any, mapping_config: Any) -> tuple[dict, dict]:
    """Adapt A's public models to B's explicit, backwards-compatible IO config.

    MappingConfig.options contains B's ``sources``, ``assumptions`` and
    ``authoritative_sales_sources``. MappingConfig.columns is an optional map of
    source IDs to canonical/header mappings. ``options.source_options`` provides
    sheet/header/coverage metadata when SourceManifest only carries source_refs.
    Model instances keep aware datetimes and Decimals via mode="python".
    """
    source = as_payload(source_manifest)
    public_mapping = as_payload(mapping_config)
    options = dict(public_mapping.get("options") or {})
    mapping = {key: value for key, value in options.items()
               if key not in {"output_root", "artifact_root", "source_options"}}
    for key, value in public_mapping.items():
        if key not in {"options", "columns", "mapping_version", "version"}:
            mapping[key] = value
    mapping["version"] = public_mapping.get("mapping_version", public_mapping.get("version", options.get("version", "1.0")))
    mappings = {key: dict(value) for key, value in mapping.get("sources", {}).items()}
    for source_id, columns in (public_mapping.get("columns") or {}).items():
        item = mappings.setdefault(source_id, {})
        existing = item.get("columns")
        if existing is not None and existing != columns:
            raise DataError("CONFLICTING_COLUMN_MAPPING", f"Source {source_id} has conflicting public/options column mappings")
        item["columns"] = dict(columns)
    mapping["sources"] = mappings
    output_root = source.get("artifact_root") or source.get("output_root") or options.get("artifact_root") or options.get("output_root")
    if output_root:
        source["artifact_root"] = output_root
    source_options = options.get("source_options") or {}
    sources = [dict(item) for item in source.get("sources", [])]
    refs = {item["source_id"]: dict(item) for item in source.get("source_refs", [])}
    if not sources and refs:
        for source_id, reference in refs.items():
            if not reference.get("local_ref"):
                raise DataError("SOURCE_NOT_AVAILABLE", f"Registered source {source_id} has no local reference")
            sources.append({"source_id": source_id, "kind": reference["kind"], "path": reference["local_ref"]})
    for item in sources:
        metadata = source_options.get(item["source_id"], {})
        for field, value in metadata.items():
            if field in item and item[field] != value:
                raise DataError("CONFLICTING_SOURCE_OPTIONS", f"Source {item['source_id']} has conflicting {field}")
            item[field] = value
    selected = set(source.get("source_ids") or [])
    if selected:
        available = {item["source_id"] for item in sources}
        if selected - available:
            raise DataError("SOURCE_NOT_AVAILABLE", "Some requested source IDs have no registered source definition")
        sources = [item for item in sources if item["source_id"] in selected]
    if not sources:
        raise DataError("NO_SOURCES", "A snapshot requires at least one registered source")
    if refs:
        from .artifacts import checksum
        from pathlib import Path
        for item in sources:
            reference = refs.get(item["source_id"])
            if reference and item.get("path"):
                path = Path(item["path"]).expanduser()
                if not path.is_file():
                    raise DataError("SOURCE_NOT_AVAILABLE", f"Registered source {item['source_id']} is unavailable")
                if checksum(path) != reference["checksum"]:
                    raise DataError("SOURCE_CHECKSUM_MISMATCH", f"Registered source {item['source_id']} changed since selection")
    if mapping["version"] == "systeme-preview-v1" and refs and not source.get("sources"):
        if public_mapping.get("columns") or mappings or source_options:
            raise DataError("PRESET_CONFIGURATION_CONFLICT", "Named registry preset cannot be combined with custom field/source mappings")
        from .presets import systeme_preview_manifest
        from .sources import timestamp
        requested = {item["source_id"]: item for item in sources}
        if {"S07", "S08", "S12"} - set(requested) or set(requested) - {"S07", "S08", "S12", "S01"}:
            raise DataError("INVALID_PRESET_SOURCES", "Systeme preset requires registered S07, S08, S12 and optional S01 only")
        if not output_root:
            raise DataError("MISSING_OUTPUT_ROOT", "Registry preset requires an explicit backend artifact output directory")
        preset_source, preset_mapping = systeme_preview_manifest(
            None, output_root, timestamp(source.get("as_of"), "as_of"),
            sku_limit=options.get("sku_limit", 20),
            source_paths={key: item["path"] for key, item in requested.items()},
        )
        preset_source["mode"] = source.get("mode", "real_preview")
        for item in preset_source["sources"]:
            if not item.get("path"):
                continue  # S08-master is derived only from the registered S08.
            reference = refs.get(item["source_id"])
            if reference is None or Path(item["path"]).resolve() != Path(reference["local_ref"]).expanduser().resolve():
                raise DataError("UNREGISTERED_PRESET_SOURCE", "Preset attempted to consume an unregistered file")
            if checksum(item["path"]) != reference["checksum"]:
                raise DataError("SOURCE_CHECKSUM_MISMATCH", f"Registered source {item['source_id']} changed during preset preparation")
        return preset_source, preset_mapping
    source["sources"] = sources
    return source, mapping


def _contracts():
    try:
        return importlib.import_module("ekt.contracts")
    except ModuleNotFoundError as exc:
        if exc.name != "ekt.contracts":
            raise
        return None


def export_contract(name: str, payload: dict):
    contracts = _contracts()
    if contracts is None:
        return payload
    model = getattr(contracts, name, None)
    if model is None:
        raise ImportError(f"ekt.contracts must export {name}; see role B handoff")
    return model.model_validate(payload)


def raise_domain(code: str, message: str, affected_sku_ids=(), retryable=False):
    contracts = _contracts()
    error_type = getattr(contracts, "DomainError", None) if contracts else None
    if error_type:
        raise error_type(code=code, message=message,
                         affected_sku_ids=list(affected_sku_ids), retryable=retryable)
    # An ordinary built-in exception keeps B usable before the foundation lands;
    # these attributes match the documented DomainError boundary for the worker.
    error = ValueError(message)
    error.code = code
    error.message = message
    error.affected_sku_ids = list(affected_sku_ids)
    error.retryable = retryable
    raise error
