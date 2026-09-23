"""Snapshot -> deterministic forecast; policy, inventory netting and HTTP stay in A."""
from __future__ import annotations

import json
import math
import os
import tempfile
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from ekt.data.artifacts import canonical_json, digest_payload, read_table, write_records
from ekt.data.boundary import as_payload, export_contract, raise_domain
from ekt.data.errors import DataError
from .baseline import forecast_daily
from .classification import classify_events
from .recovery import recover_daily

MODEL_VERSION = "robust-daily-v1.2"


def _timestamp(value):
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamp must contain timezone")
    return parsed.astimezone(timezone.utc)


def _day(value):
    return value.date() if isinstance(value, datetime) else date.fromisoformat(str(value)[:10])


def _coverage_day(value, *, start):
    if len(str(value)) == 10:
        return _day(value)
    instant = _timestamp(value)
    return instant.date() + timedelta(days=int(start and instant.timetz().replace(tzinfo=None) != time.min))


def _issue(code, message, scope=(), severity="warning"):
    return {"code": code, "severity": severity, "scope_ids": list(scope), "source_ref": None, "message": message}


def _assumptions(snapshot, field):
    found = []
    for item in snapshot.get("assumptions", []):
        if item.get("field") != field:
            continue
        value = item.get("value")
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                continue
        if isinstance(value, dict):
            found.append((item, value))
    return found


def _covered_window(snapshot, sku, warehouse, end):
    windows = []
    for assumption, value in _assumptions(snapshot, "demand_coverage"):
        skus = value.get("sku_ids") or assumption.get("scope_ids") or []
        warehouses = value.get("warehouse_ids") or []
        if value.get("complete") is not True or (skus and sku not in skus) or (warehouses and warehouse not in warehouses):
            continue
        if value.get("start") and value.get("end"):
            left, right = _coverage_day(value["start"], start=True), min(_coverage_day(value["end"], start=False), end)
            if right > left:
                windows.append((left, right))
    if not windows:
        return None
    # Only the most recent contiguous segment: do not fill unknown gaps as zero.
    merged = []
    for left, right in sorted(windows):
        if merged and left <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    return merged[-1]


def _seasonality(snapshot, category, as_of, warnings, sku):
    for assumption, value in _assumptions(snapshot, "seasonality"):
        if value.get("category_id") != category:
            continue
        if any(scope and sku not in scope for scope in (
            assumption.get("scope_ids"), value.get("sku_ids"),
        )):
            continue
        config = dict(value)
        config.setdefault("provenance", assumption.get("provenance"))
        evidence_end = config.get("evidence_end")
        if config.get("provenance") in {"observed", "derived"} and (
            not evidence_end or _timestamp(evidence_end) > as_of
        ):
            warnings.append(_issue("SEASONALITY_NOT_POINT_IN_TIME", "Seasonal evidence is not verified before replay origin; neutral factors used.", [sku]))
            return None
        return config
    return None


def _unknown_coverage_history(events):
    """Keep gaps unknown; this preview estimates observed sales-day demand only."""
    by_day = defaultdict(lambda: Decimal(0))
    for event in events:
        by_day[_timestamp(event["event_at"]).date()] += Decimal(str(event["regular_qty"]))
    if not by_day:
        return []
    start, end = min(by_day), max(by_day)
    return [{"date": (day := start + timedelta(days=i)).isoformat(),
             "observed_regular": by_day.get(day), "estimated_lost": None,
             "corrected_demand": by_day.get(day), "unavailable_fraction": None,
             "recovery_status": "unavailable_unknown_coverage"}
            for i in range((end - start).days + 1)]


def _publish_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    content = canonical_json(payload)
    handle, temp = tempfile.mkstemp(prefix=".manifest-", dir=path.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temp, path)
        except FileExistsError:
            if path.read_text(encoding="utf-8") != content:
                raise DataError("ARTIFACT_CONFLICT", "Forecast manifest identity has conflicting contents")
    finally:
        Path(temp).unlink(missing_ok=True)


def _shared_demo_coverage(snapshot, events, masters):
    """Compatibility with A's explicitly synthetic, complete generator v1.

    This narrow adapter does not certify coverage of arbitrary real exports.
    The reviewed generator iterates every date from 2024-09-23 to its cutoff,
    omits zero sales and enumerates all outages. Record those assumptions here
    until A puts their equivalent on its shared fixture manifest.
    """
    if snapshot["mode"] != "synthetic_demo" or not any(
        ref.get("source_id") == "synthetic-generator-v1"
        and ref.get("local_ref") == "generated:ekt.demo.create_demo_snapshot"
        for ref in snapshot.get("source_refs", [])
    ) or _assumptions(snapshot, "demand_coverage"):
        return snapshot
    payload = dict(snapshot)
    value = {"start": "2024-09-23", "end": _timestamp(snapshot["as_of"]).date().isoformat(),
             "complete": True, "sku_ids": sorted(m["sku_id"] for m in masters),
             "warehouse_ids": sorted({e["warehouse_id"] for e in events})}
    payload["assumptions"] = [*snapshot.get("assumptions", []), *[
        {"field": field, "value": canonical_json(value), "provenance": "synthetic",
         "scope_ids": value["sku_ids"], "reason": "Complete date loop and enumerated outages of shared synthetic-generator-v1."}
        for field in ("demand_coverage", "stockout_coverage")]]
    return payload


def _public_classifications(classified):
    """Shared ClassificationRecord uses nonnegative quantities, with direction in reasons.

    Full signed, source-linked records remain in signed_classifications.parquet
    and are the only allocations used by recovery. Returns are never fed back
    into demand as positive sales by this presentation projection.
    """
    fields = ("event_id", "sku_id", "warehouse_id", "observed_qty", "regular_qty", "project_qty",
              "uncertain_qty", "label", "reason_codes", "confidence", "review_status")
    result = []
    for event in classified:
        row = {field: event[field] for field in fields}
        row["reason_codes"] = list(row["reason_codes"])
        if event.get("demand_effect") == "decrease":
            row["reason_codes"].append("DEMAND_DECREASE")
            for field in ("observed_qty", "regular_qty", "project_qty", "uncertain_qty"):
                row[field] = abs(row[field])
        if row["label"] == "non_demand":
            row["label"] = "regular"
            row["reason_codes"].append("DEMAND_NONE")
        result.append(row)
    return result


def build_forecast_payload(snapshot: dict, request: dict) -> dict:
    as_of = _timestamp(request["as_of"])
    if as_of > _timestamp(snapshot["as_of"]):
        raise_domain("FORECAST_AFTER_SNAPSHOT", "Forecast as_of exceeds the snapshot cut-off")
    horizon = request.get("horizon_days", 90)
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon < 90:
        raise_domain("HORIZON_TOO_SHORT", "Forecast requires at least 90 daily dates")
    if horizon > 3660:
        raise_domain("INVALID_HORIZON", "Forecast horizon exceeds the supported 3660 days")
    if snapshot.get("mode") not in {"real_preview", "synthetic_demo"}:
        raise_domain("INVALID_MODE", "Snapshot mode must be real_preview or synthetic_demo")
    tables = snapshot.get("tables", {})
    if "sales_events" not in tables or "sku_master" not in tables:
        raise_domain("MISSING_TABLE", "Snapshot requires sales_events and sku_master")
    masters = read_table(tables["sku_master"])
    events = read_table(tables["sales_events"])
    snapshot = _shared_demo_coverage(snapshot, events, masters)
    intervals = read_table(tables["stockout_intervals"]) if "stockout_intervals" in tables else []
    # UTC calendar days; omit unfinished origin day from learning, preserve its
    # events in the snapshot. Forecast begins on the day after the replay date.
    origin = as_of.date() + timedelta(days=1)
    end = origin if as_of.timetz().replace(tzinfo=None) >= time(23, 59, 59) else as_of.date()
    horizons = [origin + timedelta(days=i) for i in range(horizon)]
    skus, warehouses = set(request.get("sku_ids") or []), set(request.get("warehouse_ids") or [])
    masters = {m["sku_id"]: m for m in masters if not skus or m["sku_id"] in skus}
    events = [dict(e, category_id=masters.get(e["sku_id"], {}).get("category_id"),
                   base_uom=masters.get(e["sku_id"], {}).get("base_uom")) for e in events
              if e["sku_id"] in masters and (not warehouses or e["warehouse_id"] in warehouses)
              and _timestamp(e["event_at"]) <= as_of and _timestamp(e["event_at"]).date() < end]
    intervals = [i for i in intervals if i["sku_id"] in masters
                 and (not warehouses or i["warehouse_id"] in warehouses)
                 and _timestamp(i["start_at"]) <= as_of]
    classified = classify_events(events, request.get("review_overrides") or [])
    grouped = defaultdict(list)
    for event in classified:
        grouped[(event["sku_id"], event["warehouse_id"])].append(event)
    scopes = set(grouped)
    scopes.update((i["sku_id"], i["warehouse_id"]) for i in intervals)
    for assumption, coverage in _assumptions(snapshot, "demand_coverage"):
        for sku in coverage.get("sku_ids") or assumption.get("scope_ids") or masters:
            for warehouse in coverage.get("warehouse_ids") or warehouses:
                if sku in masters and (not warehouses or warehouse in warehouses):
                    scopes.add((sku, warehouse))
    series, corrected, all_issues = [], [], []
    for sku in sorted(skus - set(masters)):
        all_issues.append(_issue("MISSING_SKU_MASTER", "Requested SKU has no master record.", [sku], "blocking"))
    scoped_skus = {sku for sku, _ in scopes}
    for sku in sorted(set(masters) - scoped_skus):
        all_issues.append(_issue("NO_DEMAND_HISTORY", "Selected SKU has no events or complete coverage in the requested warehouses.", [sku], "blocking"))
    mode = snapshot["mode"]
    for sku, warehouse in sorted(scopes):
        master = masters[sku]
        scoped_events = grouped[(sku, warehouse)]
        warnings = []
        coverage = _covered_window(snapshot, sku, warehouse, end)
        scoped_intervals = [i for i in intervals if i["sku_id"] == sku and i["warehouse_id"] == warehouse]
        # An empty table alone does not establish complete stockout logging.
        logs_known = any(
            value.get("complete") is True
            and (not (value.get("sku_ids") or assumption.get("scope_ids"))
                 or sku in (value.get("sku_ids") or assumption.get("scope_ids")))
            and (not value.get("warehouse_ids") or warehouse in value["warehouse_ids"])
            and (not value.get("start") or (coverage and _coverage_day(value["start"], start=True) <= coverage[0]))
            and (not value.get("end") or (coverage and _coverage_day(value["end"], start=False) >= coverage[1]))
            for assumption, value in _assumptions(snapshot, "stockout_coverage")
        )
        if scoped_intervals and all(i.get("evidence") == "synthetic" for i in scoped_intervals) and mode == "synthetic_demo":
            logs_known = True
            warnings.append(_issue("SYNTHETIC_LOG_COMPLETENESS", "Demo assumes synthetic intervals enumerate all unavailable periods.", [sku], "info"))
        elif scoped_intervals and not logs_known:
            warnings.append(_issue("STOCKOUT_LOG_COMPLETENESS_UNVERIFIED", "Known intervals exist but available donor days are not certified; recovery was not applied.", [sku]))
        if coverage:
            left, right = coverage
            scoped_events = [e for e in scoped_events if left <= _timestamp(e["event_at"]).date() < right]
            history, diagnostics = recover_daily(scoped_events, scoped_intervals if logs_known else None,
                                                 left, right, scopes=[(sku, warehouse)])
            if right < end:
                warnings.append(_issue("STALE_DEMAND_COVERAGE", f"Last complete covered day is {(right-timedelta(days=1)).isoformat()}.", [sku]))
            summary = diagnostics["series"][0]
        else:
            history = _unknown_coverage_history(scoped_events)
            summary = {key: sum((Decimal(str(e[field])) for e in scoped_events), Decimal(0))
                       for key, field in (("observed_regular_total", "regular_qty"), ("project_total", "project_qty"), ("uncertain_total", "uncertain_qty"))}
            summary["estimated_lost_total"] = None
            warnings.append(_issue("UNKNOWN_DEMAND_COVERAGE", "Conditional observed-sales-day preview only; unobserved dates stay unknown. Not eligible for order planning.", [sku], "blocking"))
        if not history:
            all_issues.append(_issue("NO_DEMAND_HISTORY", "No covered demand history for this scope.", [sku], "blocking"))
            continue
        if any(Decimal(str(event["uncertain_qty"])) > 0 for event in scoped_events) and not any(
            Decimal(str(event["regular_qty"])) > 0 for event in scoped_events
        ):
            warnings.append(_issue(
                "ALL_DEMAND_UNCERTAIN",
                "All positive unconfirmed demand is excluded from the regular estimate. "
                "A zero forecast does not establish zero demand; buyer classification review is required before planning.",
                [sku], "blocking",
            ))
        seasonal = _seasonality(snapshot, master.get("category_id"), as_of, warnings, sku)
        model = forecast_daily(history, horizons, category_id=master.get("category_id"),
                               growth_overrides=request.get("growth_overrides") or [], seasonality=seasonal)
        if not model["daily"]:
            all_issues.append(_issue("NO_KNOWN_DEMAND_HISTORY", "All history values are unknown; no forecast was manufactured.", [sku], "blocking"))
            continue
        corrected.extend(dict(row, sku_id=sku, warehouse_id=warehouse) for row in history)
        if model["daily_residual_std"] is None:
            all_issues.extend(warnings + [_issue("UNCERTAINTY_UNAVAILABLE", "Insufficient observed history for shared uncertainty contract; no fabricated sigma was supplied.", [sku], "blocking")])
            continue
        if any(summary[key] < 0 for key in ("observed_regular_total", "project_total", "uncertain_total")):
            all_issues.extend(warnings + [_issue("NEGATIVE_NET_HISTORY", "Net-return history cannot be represented by nonnegative ForecastSeries totals; signed records are retained for review.", [sku], "blocking")])
            continue
        if seasonal and seasonal.get("provenance") in {"synthetic", "override"}:
            mode = "synthetic_demo"
        if any(o.get("category_id") == master.get("category_id")
               and _day(o["valid_from"]) <= horizons[-1] and _day(o["valid_to"]) >= origin
               for o in request.get("growth_overrides") or []):
            mode = "synthetic_demo"
        for code in model["warnings"]:
            warnings.append(_issue(code, code.replace("_", " ").lower(), [sku]))
        if summary.get("estimated_lost_total") is None:
            warnings.append(_issue("RECOVERY_UNAVAILABLE", "Loss is unknown. Diagnostic total 0 means no estimate was applied, not zero true lost demand; row-level loss is null.", [sku]))
        warnings.append(_issue("HISTORY_WINDOW", f"Diagnostics cover {history[0]['date']} through {history[-1]['date']} inclusive, UTC; unfinished replay day excluded.", [sku], "info"))
        for row in model["daily"]:
            values = [float(row[k]) for k in ("baseline_mean", "seasonal_delta", "growth_delta", "mean")]
            if not all(math.isfinite(v) for v in values) or values[-1] < 0 or abs(sum(values[:3])-values[-1]) > 1e-8:
                raise_domain("INVALID_FORECAST", "Forecast decomposition is invalid", [sku])
        if any(event["review_status"] == "pending" for event in scoped_events):
            classification_policy = "robust_suspected_exclusion"
        elif any(event["review_status"] == "confirmed" for event in scoped_events):
            classification_policy = "review_confirmed"
        else:
            classification_policy = "regular_only"
        series.append({"sku_id": sku, "warehouse_id": warehouse, "base_uom": master["base_uom"],
                       "daily": model["daily"],
                       "uncertainty": {"method": "iid_residual_normal", "daily_residual_std": model["daily_residual_std"],
                           "paths_ref": None, "path_count": None, "calibration": "unvalidated",
                           "assumption_note": "Residual normal approximation assumes independent days and stationary residual scale. No measured service-level guarantee; imputed losses are not ground truth. Growth rate is a one-time active-date proportional uplift; calendar is UTC."},
                       "observed_regular_total": summary["observed_regular_total"],
                       "estimated_lost_total": summary.get("estimated_lost_total") or Decimal(0),
                       "project_total": summary["project_total"], "uncertain_total": summary["uncertain_total"],
                       "classification_policy": classification_policy,
                       "warnings": warnings})
        all_issues.extend(warnings)
    quality = dict(snapshot.get("quality") or {})
    quality["issues"] = list(quality.get("issues", [])) + all_issues
    quality["status"] = "blocked" if not series else "degraded" if quality["issues"] else "ready"
    quality.setdefault("accepted_rows", len(events))
    quality.setdefault("rejected_rows", 0)
    quality["affected_skus"] = len({s for issue in quality["issues"] for s in issue.get("scope_ids", [])})
    capabilities = dict(quality.get("capabilities") or {})
    for name in ("can_plan", "can_approve", "can_export", "budget_available", "customer_detection_available", "observed_stockouts_available"):
        capabilities.setdefault(name, False)
    capabilities["reasons"] = list(capabilities.get("reasons", []))
    blocking_skus = {sku for issue in quality["issues"] if issue["severity"] == "blocking"
                     for sku in issue.get("scope_ids", [])}
    usable_series = [s for s in series if s["sku_id"] not in blocking_skus]
    if not usable_series:
        capabilities.update(can_plan=False, can_approve=False, can_export=False)
        capabilities["reasons"].append("Forecast has no usable series; inspect per-SKU blocking warnings.")
    quality["capabilities"] = capabilities
    # run_id is coordinator identity, not a model input: sliders/runs reuse content.
    content_request = {k: v for k, v in request.items() if k != "run_id"}
    request_hash = digest_payload(content_request)
    forecast_id = "forecast-" + digest_payload({"snapshot_id": snapshot["snapshot_id"], "request_hash": request_hash, "model_version": MODEL_VERSION})[:24]
    artifact_dir = Path(tables["sales_events"]["uri"]).resolve().parent / "forecasts" / forecast_id
    classes_ref = write_records(artifact_dir / "classifications.parquet", _public_classifications(classified))
    signed_ref = write_records(artifact_dir / "signed_classifications.parquet", classified)
    corrected_ref = write_records(artifact_dir / "corrected_demand.parquet", corrected)
    artifact = {"schema_version": "1.0", "forecast_id": forecast_id, "snapshot_id": snapshot["snapshot_id"],
                "request_hash": request_hash, "model_version": MODEL_VERSION, "seed": request.get("seed", 0),
                "as_of": as_of.isoformat(), "mode": mode, "series": series,
                "classifications_ref": classes_ref["uri"], "corrected_demand_ref": corrected_ref["uri"],
                "quality": quality, "metrics": []}
    # This backend-only sidecar adds artifact integrity; public contract unchanged.
    _publish_json(artifact_dir / "artifact_refs.json", {"classifications": classes_ref, "signed_classifications": signed_ref, "corrected_demand": corrected_ref})
    _publish_json(artifact_dir / "forecast.json", artifact)
    return artifact


def build_forecast(snapshot, request):
    try:
        payload = build_forecast_payload(as_payload(snapshot), as_payload(request))
    except DataError as error:
        raise_domain(error.code, str(error), error.affected_sku_ids, error.retryable)
    except (ValueError, KeyError, TypeError) as error:
        if hasattr(error, "code"):
            raise
        raise_domain("INVALID_FORECAST_INPUT", str(error))
    return export_contract("ForecastArtifact", payload)
