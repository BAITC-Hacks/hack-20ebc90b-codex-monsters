"""Reproducible aggregate evidence for a local preview; no source rows are exported.

Run ``python -m ekt.data.assessment --help``. A single elapsed-time observation
describes this machine/run only, not an SLA, forecast accuracy or business value.
"""

from __future__ import annotations

import argparse
import json
import platform
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path
from time import perf_counter

from .artifacts import canonical_json, checksum, read_table
from .errors import DataError
from .presets import systeme_preview_manifest
from .snapshot import build_snapshot_payload
from .sources import decimal_value, source_records


def _sign(value):
    if value is None or value == "":
        return "missing"
    try:
        quantity = decimal_value(value)
    except DataError:
        return "invalid"
    return "positive" if quantity > 0 else "negative" if quantity < 0 else "zero"


def summarize_sales_records(records, columns, *, selected_skus=(), date_format=None):
    """Count all keyed rows separately from blank/summary rows, without leaking IDs.

    The selected row count precedes semantic validation. The accepted count in a
    snapshot is a different denominator and must not be described as that count.
    """
    selected = set(selected_skus)
    skus, uoms, dates = set(), Counter(), []
    sku_uoms = defaultdict(set)
    keyed_signs, nonkeyed_signs = Counter(), Counter()
    keyed_count = nonkeyed_count = selected_count = invalid_dates = 0
    for _, row in records:
        sku = row.get(columns["sku_id"])
        if sku is None or (isinstance(sku, str) and not sku.strip()):
            nonkeyed_count += 1
            nonkeyed_signs[_sign(row.get(columns["quantity_base"]))] += 1
            continue
        keyed_count += 1
        key = str(sku).strip()
        skus.add(key)
        selected_count += key in selected
        keyed_signs[_sign(row.get(columns["quantity_base"]))] += 1
        uom = row.get(columns["base_uom"])
        # Unit labels are source metadata, never SKU/document/customer identities.
        uoms[str(uom) if uom is not None else "unknown"] += 1
        sku_uoms[key].add(uom)
        value = row.get(columns["event_at"])
        try:
            if isinstance(value, str):
                value = datetime.strptime(value, date_format) if date_format else datetime.fromisoformat(value)
            if not isinstance(value, date):
                raise ValueError()
            dates.append(value.date() if isinstance(value, datetime) else value)
        except (TypeError, ValueError):
            invalid_dates += 1
    return {
        "keyed_rows": keyed_count,
        "nonkeyed_rows": nonkeyed_count,
        "distinct_skus": len(skus),
        "piece_only_skus": sum(units == {"шт"} for units in sku_uoms.values()),
        "selected_keyed_rows_before_validation": selected_count,
        "keyed_quantity_signs": dict(sorted(keyed_signs.items())),
        "nonkeyed_quantity_signs": dict(sorted(nonkeyed_signs.items())),
        "uom_counts": dict(sorted(uoms.items())),
        "source_date_min": min(dates).isoformat() if dates else None,
        "source_date_max": max(dates).isoformat() if dates else None,
        "invalid_or_missing_dates": invalid_dates,
    }


def summarize_snapshot(snapshot):
    """Expose counts/codes only; source paths, SKU scopes and row refs stay local."""
    quality = snapshot.get("quality") or {}
    caps = quality.get("capabilities") or {}
    return {
        "snapshot_id": snapshot["snapshot_id"],
        "as_of": str(snapshot["as_of"]),
        "mode": snapshot["mode"],
        "mapping_version": snapshot.get("mapping_version"),
        "table_counts": {name: ref["row_count"] for name, ref in sorted(snapshot["tables"].items())},
        "quality_status": quality.get("status"),
        "issue_counts": dict(sorted(Counter(issue["code"] for issue in quality.get("issues", [])).items())),
        "blocking_issue_counts": dict(sorted(Counter(issue["code"] for issue in quality.get("issues", []) if issue["severity"] == "blocking").items())),
        "capabilities": {name: bool(caps.get(name, False)) for name in (
            "can_plan", "can_approve", "can_export", "budget_available",
            "customer_detection_available", "observed_stockouts_available",
        )},
    }


def build_assessment(*, source_root=None, as_of=None, sku_limit=20, snapshot=None, artifact_root=None):
    """Profile supplied files and build/reuse the conservative Systeme preview.

    With only ``snapshot``, report snapshot evidence with unknown full-source
    denominators. With ``source_root``, the selected preset is checked against
    the supplied snapshot, or a fresh local snapshot is built. No forecast runs.
    """
    started = perf_counter()
    timings, profiles, sources = {}, {}, []
    if source_root is None and snapshot is None:
        raise ValueError("source_root or snapshot is required")
    if snapshot is not None:
        if isinstance(snapshot, (str, Path)):
            snapshot = json.loads(Path(snapshot).read_text(encoding="utf-8"))
        if hasattr(snapshot, "model_dump"):
            snapshot = snapshot.model_dump(mode="json")
        for ref in snapshot["tables"].values():
            read_table(ref)  # Validate local artifact checksums before reporting.
        timings["existing_snapshot_verification"] = perf_counter() - started
    if source_root is not None:
        cutoff = as_of or (snapshot or {}).get("as_of")
        if not cutoff:
            raise ValueError("as_of is required when building from source_root")
        if snapshot is not None and str(snapshot["as_of"]) != str(cutoff):
            raise ValueError("as_of must match the supplied snapshot")
        if snapshot is not None and snapshot.get("mapping_version") != "systeme-preview-v1":
            raise ValueError("The supplied snapshot must use systeme-preview-v1")
        if not artifact_root and snapshot is None:
            raise ValueError("artifact_root is required for a fresh snapshot")
        tick = perf_counter()
        manifest, mapping = systeme_preview_manifest(source_root, artifact_root or "/unused-assessment", str(cutoff), sku_limit)
        timings["preset_selection"] = perf_counter() - tick
        sales_source = next(s for s in manifest["sources"] if s["source_id"] == "S08")
        sales_mapping = mapping["sources"]["S08"]
        selected = sales_mapping["sku_filter"]
        tick = perf_counter()
        rows, fingerprint, _ = source_records(sales_source, sales_mapping)
        profiles["S08"] = {
            "checksum": fingerprint,
            **summarize_sales_records(rows, sales_mapping["columns"], selected_skus=selected,
                                      date_format=sales_mapping["date_formats"]["event_at"]),
        }
        timings["full_source_profile"] = perf_counter() - tick
        del rows
        if snapshot is None:
            tick = perf_counter()
            snapshot = build_snapshot_payload(manifest, mapping)
            timings["snapshot_build"] = perf_counter() - tick
        else:
            refs = {ref["source_id"]: ref for ref in snapshot["source_refs"]}
            for source in manifest["sources"]:
                reference = refs.get(source["source_id"])
                if reference is None:
                    raise ValueError("Source registration differs from the supplied snapshot")
                if source.get("path") and checksum(source["path"]) != reference["checksum"]:
                    raise ValueError(f"{source['source_id']} checksum differs from the supplied snapshot")
            actual_skus = {row["sku_id"] for row in read_table(snapshot["tables"]["sku_master"])}
            if actual_skus != set(selected):
                raise ValueError("sku_limit/preset scope differs from the supplied snapshot")
        sources = [{"source_id": source["source_id"], "role": source["kind"],
                    "status": "metadata_only" if source.get("metadata_only") else "mapped",
                    "checksum": next(ref["checksum"] for ref in snapshot["source_refs"] if ref["source_id"] == source["source_id"])}
                   for source in manifest["sources"]]
    else:
        sources = [{"source_id": ref["source_id"], "role": ref["kind"],
                    "status": "not_reprofiled", "checksum": ref["checksum"]} for ref in snapshot["source_refs"]]
    summary = summarize_snapshot(snapshot)
    denominator = profiles.get("S08", {})
    n_skus = summary["table_counts"].get("sku_master", 0)
    n_sales = summary["table_counts"].get("sales_events", 0)
    total_skus, total_rows = denominator.get("distinct_skus"), denominator.get("keyed_rows")
    timings["total"] = perf_counter() - started
    return {
        "report_version": "1.0", "snapshot": summary, "sources": sources,
        "source_profiles": profiles,
        "coverage": {
            "selected_skus": n_skus, "source_skus": total_skus,
            "selected_sku_fraction": n_skus / total_skus if total_skus else None,
            "accepted_sales_rows": n_sales, "source_keyed_rows": total_rows,
            "accepted_row_fraction": n_sales / total_rows if total_rows else None,
            "selection": "top positive shipment-frequency piece SKUs" if source_root else "not reprofiled",
        },
        "timing": {"seconds": timings, "python": platform.python_version(),
                   "system": platform.system(), "machine": platform.machine(),
                   "scope": "Single local run; filesystem cache uncontrolled. No SLA or scale guarantee. Forecast not timed."},
        "limitations": [
            "Real preview is not a complete ERP reconciliation; source coverage and movement semantics require confirmation.",
            "Full-source signs count keyed rows separately from summaries; profile_sources may include a non-SKU summary quantity.",
            "Full-source denominators include all rows in the supplied export, not just dates before the cutoff; selected keyed rows precede semantic validation.",
            "Subset selection favors frequently observed SKUs and is not representative of the long tail.",
            "Metadata-only sources are registered, not imported as current stock or full supplier demand.",
            "Missing dates are unknown, not observed zero demand. The preview is not eligible for ordering without verified inputs.",
            "No real accuracy, recovered lost-demand truth, achieved service level, savings or time reduction is measured here.",
        ],
    }


def render_markdown(report):
    snapshot, coverage = report["snapshot"], report["coverage"]
    def fraction(value):
        return "unknown" if value is None else f"{value:.2%}"
    lines = ["# Data preview evidence", "", f"Snapshot: `{snapshot['snapshot_id']}`",
             f"Cut-off: {snapshot['as_of']}; mode: `{snapshot['mode']}`; quality: `{snapshot['quality_status']}`.", "",
             "| Measure | Value |", "|---|---:|",
             f"| Selected / source SKUs | {coverage['selected_skus']} / {coverage['source_skus'] or 'unknown'} |",
             f"| SKU coverage | {fraction(coverage['selected_sku_fraction'])} |",
             f"| Accepted / source keyed rows | {coverage['accepted_sales_rows']} / {coverage['source_keyed_rows'] or 'unknown'} |",
             f"| Accepted row coverage | {fraction(coverage['accepted_row_fraction'])} |",
             f"| Can plan / approve / export | {snapshot['capabilities']['can_plan']} / {snapshot['capabilities']['can_approve']} / {snapshot['capabilities']['can_export']} |",
             "", "## Sources", "", "| Source | Role | Status | SHA-256 |", "|---|---|---|---|"]
    lines.extend(f"| {s['source_id']} | {s['role']} | {s['status']} | `{s['checksum']}` |" for s in report["sources"])
    for source_id, profile in report["source_profiles"].items():
        lines.extend(["", f"## {source_id}: full source denominator", "",
                      f"Keyed rows: {profile['keyed_rows']}; non-keyed rows: {profile['nonkeyed_rows']}; distinct SKUs: {profile['distinct_skus']}.",
                      f"Source dates: {profile['source_date_min']} to {profile['source_date_max']} (not a completeness assertion).",
                      f"Keyed quantity signs: `{json.dumps(profile['keyed_quantity_signs'], sort_keys=True)}`.",
                      f"Non-keyed quantity signs: `{json.dumps(profile['nonkeyed_quantity_signs'], sort_keys=True)}`.",
                      f"Selected keyed rows before validation: {profile['selected_keyed_rows_before_validation']}."])
    lines.extend(["", "## Blocking reasons", "", "| Code | Issue count |", "|---|---:|"])
    lines.extend(f"| {code} | {count} |" for code, count in snapshot["blocking_issue_counts"].items())
    lines.extend(["", "Issue counts can exceed SKU counts because one SKU can lack several fields.", "",
                  "## Measured elapsed time", "", report["timing"]["scope"], "",
                  f"Python {report['timing']['python']}; {report['timing']['system']} {report['timing']['machine']}.", ""])
    lines.extend(f"- {name}: {seconds:.3f} s" for name, seconds in report["timing"]["seconds"].items())
    lines.extend(["", "## Limitations", ""] + [f"- {item}" for item in report["limitations"]])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--systeme-root", help="Extracted local IEK and Systeme electric parent directory")
    parser.add_argument("--snapshot", help="Optional existing local snapshot manifest; checksum/scope checked")
    parser.add_argument("--as-of", help="Explicit timezone-aware cutoff; required for a fresh snapshot")
    parser.add_argument("--sku-limit", type=int, default=20)
    parser.add_argument("--output-dir", required=True, type=Path, help="Local output directory; do not commit source artifacts")
    args = parser.parse_args()
    if not args.systeme_root and not args.snapshot:
        parser.error("Supply --systeme-root or --snapshot")
    if args.systeme_root and not args.snapshot and not args.as_of:
        parser.error("A fresh source assessment requires --as-of")
    output = args.output_dir.expanduser().resolve()
    report = build_assessment(source_root=args.systeme_root, as_of=args.as_of, sku_limit=args.sku_limit,
                              snapshot=args.snapshot, artifact_root=output / "artifacts")
    output.mkdir(parents=True, exist_ok=True)
    json_path, markdown_path = output / "report.json", output / "report.md"
    json_path.write_text(canonical_json(report) + "\n", encoding="utf-8")
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    print(canonical_json({"report_json": str(json_path), "report_markdown": str(markdown_path),
                          "snapshot_id": report["snapshot"]["snapshot_id"]}))


if __name__ == "__main__":
    main()
