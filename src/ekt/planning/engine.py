"""Deterministic periodic-review replenishment with an auditable quantity ledger.

The horizon begins on the calendar day following ``as_of``. Demand quantiles are
computed for aggregate lead-time/protection-period demand, never summed daily
quantiles. Normal uncertainty explicitly assumes independent daily residuals.
Budget allocation is a feasible urgency-prioritized heuristic, not an optimum.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from fractions import Fraction
from hashlib import sha256
import json
from math import gcd, lcm, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from ekt.contracts import (
    Capabilities, DomainError, ExcludedLine, ExplanationComponent, ForecastArtifact,
    PlanningPolicy, ProposalDetail, ProposalLine, QualityIssue, SnapshotManifest,
)
from ekt.contracts.io import load_table

ZERO = Decimal("0")
ACTIVE_PIPELINE = {"open", "ordered", "confirmed", "in_transit", "partially_received", "approved"}


def _dict(value: Any) -> dict:
    return value.model_dump(mode="python") if hasattr(value, "model_dump") else dict(value)


def _decimal(value: Any) -> Decimal:
    if value is None:
        raise ValueError("required quantity is unknown")
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("quantity must be finite")
    return result


def _date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _issue(code: str, message: str, sku: str, severity: str = "warning") -> QualityIssue:
    return QualityIssue(code=code, message=message, scope_ids=[sku], severity=severity)


def _error(code: str, message: str, skus: list[str] | None = None) -> DomainError:
    return DomainError(code=code, message=message, affected_sku_ids=skus or [], retryable=False)


def _component(code: str, label: str, amount: Decimal, note: str | None = None) -> ExplanationComponent:
    return ExplanationComponent(code=code, label=label, delta_base_qty=amount, source_refs=[], note=note)


def _table(snapshot: SnapshotManifest, name: str) -> list[dict]:
    if name not in snapshot.tables:
        return []
    return [_dict(row) for row in load_table(snapshot, name)]


def _increment(pack: Decimal, conversion: Decimal, quantum: Decimal) -> Decimal:
    """Least common *base* increment respecting both pack and stock quantum."""
    a, b = Fraction(pack * conversion), Fraction(quantum)
    value = Fraction(lcm(a.numerator, b.numerator), gcd(a.denominator, b.denominator))
    return Decimal(value.numerator) / Decimal(value.denominator)


def _ceil(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


def _floor(value: Decimal, increment: Decimal) -> Decimal:
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def revalidate_line_quantity(line: ProposalLine, purchase_qty: Decimal | str) -> Decimal:
    """Validate buyer quantity and return base quantity; zero is a valid deferral."""
    try:
        quantity = _decimal(purchase_qty)
        if quantity < 0:
            raise ValueError("quantity must be nonnegative")
        conversion = _decimal(line.conversion)
        multiple = _decimal(line.pack_multiple_purchase)
        quantum = _decimal(getattr(line, "quantity_quantum", "1"))
        if min(conversion, multiple, quantum) <= 0:
            raise ValueError("invalid conversion, pack or quantity quantum")
        if quantity and quantity < line.moq_purchase:
            raise ValueError("positive quantity is below MOQ")
        if quantity % multiple:
            raise ValueError("quantity does not respect the purchase pack multiple")
        base = quantity * conversion
        if base % quantum:
            raise ValueError("base quantity does not respect the stock quantity quantum")
        return base
    except (ValueError, InvalidOperation) as exc:
        raise _error("INVALID_QUANTITY", str(exc), [line.sku_id]) from exc


def _read_paths(uri: str) -> list[dict]:
    path = Path(uri.removeprefix("file://"))
    if path.suffix == ".json":
        rows = json.loads(path.read_text())
        return rows["rows"] if isinstance(rows, dict) else rows
    if path.suffix in {".jsonl", ".ndjson"}:
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    import pyarrow.parquet as pq
    return pq.read_table(path).to_pylist()


def _quantile(values: list[Decimal], probability: float) -> Decimal:
    ordered = sorted(values)
    rank = Decimal(str(probability)) * (len(ordered) - 1)
    lower = int(rank)
    fraction = rank - lower
    if lower == len(ordered) - 1:
        return ordered[lower]
    return ordered[lower] + fraction * (ordered[lower + 1] - ordered[lower])


def _forecast_values(series: Any, start: date, horizon: int, lead: int, target: float):
    sku = series.sku_id
    daily = {}
    for item in series.daily:
        row = _dict(item)
        day = _date(row["date"])
        if day in daily:
            raise _error("INVALID_FORECAST", "Duplicate forecast day", [sku])
        baseline, seasonal, growth, mean = (
            _decimal(row[name]) for name in ("baseline_mean", "seasonal_delta", "growth_delta", "mean")
        )
        if mean < 0 or abs(baseline + seasonal + growth - mean) > Decimal("0.00000001"):
            raise _error("INVALID_FORECAST", "Forecast decomposition does not reconcile", [sku])
        daily[day] = (baseline, seasonal, growth, mean)
    days = [start + timedelta(days=i) for i in range(horizon)]
    if any(day not in daily for day in days):
        raise _error("HORIZON_TOO_SHORT", f"Forecast must cover {horizon} consecutive days from {start}", [sku])
    def sums(count, index):
        return sum((daily[day][index] for day in days[:count]), ZERO)
    mean = sums(horizon, 3)
    lead_mean = sums(lead, 3)
    uncertainty = _dict(series.uncertainty)
    if uncertainty["method"] == "scenario_paths":
        if not uncertainty.get("paths_ref"):
            raise _error("INVALID_UNCERTAINTY", "Scenario path reference is missing", [sku])
        try:
            records = _read_paths(uncertainty["paths_ref"])
        except (OSError, ValueError) as exc:
            raise _error("INVALID_UNCERTAINTY", "Scenario path artifact cannot be read", [sku]) from exc
        paths: dict[str, dict[date, Decimal]] = defaultdict(dict)
        for row in records:
            if row.get("sku_id", sku) != sku or row.get("warehouse_id", series.warehouse_id) != series.warehouse_id:
                continue
            key, day = str(row["scenario_id"]), _date(row["date"])
            quantity = _decimal(row["quantity"])
            if quantity < 0 or day in paths[key]:
                raise _error("INVALID_UNCERTAINTY", "Invalid or duplicate scenario demand", [sku])
            paths[key][day] = quantity
        if not paths or (uncertainty.get("path_count") is not None and len(paths) != uncertainty["path_count"]):
            raise _error("INVALID_UNCERTAINTY", "Scenario path count does not match manifest", [sku])
        if any(any(day not in path for day in days) for path in paths.values()):
            raise _error("HORIZON_TOO_SHORT", "Scenario paths do not cover the protection period", [sku])
        protection = [sum((path[day] for day in days), ZERO) for path in paths.values()]
        lead_totals = [sum((path[day] for day in days[:lead]), ZERO) for path in paths.values()]
        ss = max(ZERO, _quantile(protection, target) - mean)
        lead_ss = max(ZERO, _quantile(lead_totals, target) - lead_mean)
    elif uncertainty["method"] == "iid_residual_normal":
        try:
            std = _decimal(uncertainty.get("daily_residual_std"))
        except (ValueError, InvalidOperation) as exc:
            raise _error("INVALID_UNCERTAINTY", "Daily residual uncertainty is missing", [sku]) from exc
        if std < 0:
            raise _error("INVALID_UNCERTAINTY", "Residual standard deviation is negative", [sku])
        z = max(0.0, NormalDist().inv_cdf(target))
        ss = std * Decimal(str(z * sqrt(horizon)))
        lead_ss = std * Decimal(str(z * sqrt(lead)))
    else:
        raise _error("INVALID_UNCERTAINTY", "Unsupported uncertainty method", [sku])
    return daily, (sums(horizon, 0), sums(horizon, 1), sums(horizon, 2)), ss, lead_mean + lead_ss


def _latest(rows: list[dict], field: str, as_of: datetime) -> dict | None:
    def moment(value):
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=as_of.tzinfo)
    valid = [row for row in rows if row.get(field) is not None and moment(row[field]) <= as_of]
    return max(valid, key=lambda row: moment(row[field])) if valid else None


def build_proposals(
    snapshot: SnapshotManifest, forecast: ForecastArtifact, policy: PlanningPolicy, run_id: str,
) -> list[ProposalDetail]:
    """Return supplier proposals with excluded lines retained, or fail explicitly.

    Missing SKU business terms block only that line. Invalid/missing forecast
    horizon and an unsupported global budget fail the run, preventing partial
    recommendations masquerading as a complete scenario.
    """
    if forecast.snapshot_id != snapshot.snapshot_id or forecast.as_of != snapshot.as_of:
        raise _error("SNAPSHOT_MISMATCH", "Forecast must refer to the exact planning snapshot")
    day0 = snapshot.as_of.date()
    start = day0 + timedelta(days=1)
    masters = defaultdict(list)
    stocks = defaultdict(list)
    terms = defaultdict(list)
    pipes = defaultdict(list)
    for row in _table(snapshot, "sku_master"):
        masters[row["sku_id"]].append(row)
    for row in _table(snapshot, "inventory_snapshots"):
        stocks[row["sku_id"], row["warehouse_id"]].append(row)
    for row in _table(snapshot, "supplier_terms"):
        terms[row["supplier_id"], row["sku_id"], row["warehouse_id"]].append(row)
    for row in _table(snapshot, "pipeline_lines"):
        pipes[row["sku_id"], row["warehouse_id"]].append(row)
    exclusions = []
    candidates: list[tuple[tuple, ProposalLine, Decimal, bool]] = []
    seen = set()
    for series in forecast.series:
        sku, warehouse = series.sku_id, series.warehouse_id
        if (sku, warehouse) in seen:
            raise _error("DUPLICATE_SERIES", "Duplicate SKU/warehouse forecast", [sku])
        seen.add((sku, warehouse))
        reasons = []
        master_rows = masters.get(sku, [])
        if len(master_rows) != 1:
            exclusions.append(ExcludedLine(sku_id=sku, reasons=[_issue("MISSING_OR_DUPLICATE_SKU", "SKU master must contain one unambiguous mapping", sku, "blocking")]))
            continue
        master = master_rows[0]
        if master.get("eligible") is False or master.get("lifecycle", "active") not in {"active", "new", "eligible"}:
            reasons.append(_issue("INELIGIBLE_LIFECYCLE", "SKU is not eligible for automatic replenishment", sku, "blocking"))
        supplier = master.get("supplier_id")
        if not supplier:
            reasons.append(_issue("MISSING_SUPPLIER", "Supplier mapping is required", sku, "blocking"))
        stock = _latest(stocks.get((sku, warehouse), []), "as_of", snapshot.as_of)
        term = _latest(terms.get((supplier, sku, warehouse), []), "valid_at", snapshot.as_of)
        for issue in [*snapshot.quality.issues, *forecast.quality.issues]:
            if issue.severity == "blocking" and (sku in issue.scope_ids or not issue.scope_ids):
                reasons.append(issue)
        if not stock or stock.get("free_base") is None or not stock.get("accounting_definition_version"):
            reasons.append(_issue("MISSING_STOCK", "Verified free stock and its accounting definition are required", sku, "blocking"))
        if stock and stock.get("free_base") is not None:
            try:
                expected_free = _decimal(stock.get("on_hand_base")) - _decimal(stock.get("reserved_base")) - _decimal(stock.get("blocked_base"))
                if _decimal(stock["free_base"]) != expected_free:
                    raise ValueError("free stock does not reconcile to on hand less reserved and blocked")
            except (ValueError, InvalidOperation) as exc:
                reasons.append(_issue("INVALID_STOCK_ACCOUNTING", str(exc), sku, "blocking"))
        if not term:
            reasons.append(_issue("MISSING_TERMS", "Effective supplier terms are required", sku, "blocking"))
        try:
            conversion = _decimal(master.get("base_units_per_purchase_uom"))
            quantum = _decimal(master.get("quantity_quantum"))
            if not master.get("purchase_uom") or master.get("base_uom") != series.base_uom or min(conversion, quantum) <= 0:
                raise ValueError("Missing or inconsistent purchase/base UOM mapping")
            if term is not None:
                moq = _decimal(term.get("moq_purchase"))
                pack = _decimal(term.get("pack_multiple_purchase"))
                lead_value = term.get("lead_time_days")
                review_value = policy.review_days if policy.review_days is not None else term.get("review_days")
                if moq < 0 or pack <= 0 or lead_value is None or review_value is None:
                    raise ValueError("MOQ, packs, lead time and review interval must be confirmed")
                if int(lead_value) != lead_value or int(review_value) != review_value or lead_value < 0 or review_value <= 0:
                    raise ValueError("Lead time must be a nonnegative integer; review must be a positive integer")
                lead = int(lead_value) + policy.lead_time_delay_days
                review = int(review_value)
        except (ValueError, TypeError, InvalidOperation) as exc:
            reasons.append(_issue("INVALID_OR_MISSING_TERMS", str(exc), sku, "blocking"))
        if reasons:
            exclusions.append(ExcludedLine(sku_id=sku, reasons=reasons))
            continue
        assert stock is not None and term is not None
        horizon = lead + review
        required_horizon = max(horizon, policy.max_cover_days or 0)
        daily, decomposition, ss, rop = _forecast_values(series, start, horizon, lead, policy.service_target)
        if any(start + timedelta(days=i) not in daily for i in range(required_horizon)):
            raise _error("HORIZON_TOO_SHORT", "Forecast does not cover the requested maximum-cover horizon", [sku])
        free = _decimal(stock["free_base"])
        warnings = list(series.warnings)
        uncertainty = _dict(series.uncertainty)
        if uncertainty["method"] == "iid_residual_normal":
            warnings.append(_issue("IID_NORMAL_ASSUMPTION", "Safety stock assumes independent normal daily residuals; target CSL is not measured achieved service", sku))
        elif uncertainty.get("calibration") != "backtested":
            warnings.append(_issue("UNCALIBRATED_SCENARIOS", "Scenario service target is unvalidated, not measured achieved service", sku))
        arrivals = defaultdict(lambda: ZERO)
        pipeline = ZERO
        used_pipe_rows = []
        for incoming in pipes.get((sku, warehouse), []):
            if incoming.get("status") not in ACTIVE_PIPELINE:
                continue
            remaining = _decimal(incoming.get("remaining_base_qty"))
            if remaining < 0:
                raise _error("INVALID_PIPELINE", "Open receipt quantity cannot be negative", [sku])
            if incoming.get("eta_end") is None or incoming.get("eta_semantics") == "unknown":
                warnings.append(_issue("UNKNOWN_ETA_EXCLUDED", "Open receipt with unknown ETA is excluded from usable pipeline", sku))
                continue
            eta_original = _date(incoming["eta_end"])
            if eta_original <= day0:
                warnings.append(_issue("PAST_DUE_PIPELINE_EXCLUDED", "Past-due unreceived supply requires a confirmed new ETA", sku))
                continue
            eta = eta_original + timedelta(days=policy.lead_time_delay_days)
            if eta < start + timedelta(days=horizon):
                arrivals[eta] += remaining
                pipeline += remaining
                used_pipe_rows.append(incoming)
        stockout = None
        balance = free
        for offset in range(horizon):
            current = start + timedelta(days=offset)
            balance += arrivals[current]
            balance -= daily[current][3]
            if balance < 0 and stockout is None:
                stockout = current
        urgency = "routine"
        if stockout is not None:
            urgency = "critical" if stockout <= day0 + timedelta(days=lead) else "soon"
            warnings.append(_issue("PROJECTED_STOCKOUT", f"Expected-demand projection first runs short on {stockout}; later receipts cannot repair earlier service", sku))
        if policy.lead_time_delay_days:
            warnings.append(_issue("DELAY_SCENARIO", f"Calendar lead time and known future receipt deadlines shifted by {policy.lead_time_delay_days} days", sku))
        ledger = [
            _component("baseline_demand", "Базовый спрос", decomposition[0]),
            _component("seasonal_delta", "Сезонная корректировка", decomposition[1]),
            _component("growth_delta", "Корректировка роста", decomposition[2]),
            _component("safety_stock", "Страховой запас", ss),
            _component("free_stock", "Доступный остаток", -free, "Free stock already excludes reservations and blocked stock; no second subtraction"),
            _component("eligible_pipeline", "Поставки в пределах горизонта", -pipeline, "Conservative eta_end; early shortage is reported separately"),
        ]
        before_clip = sum((entry.delta_base_qty for entry in ledger), ZERO)
        raw = max(ZERO, before_clip)
        ledger.append(_component("nonnegative_clip", "Ограничение потребности снизу", raw - before_clip))
        after_moq = max(raw, moq * conversion) if raw else ZERO
        ledger.append(_component("moq_adjustment", "Минимальная партия", after_moq - raw))
        after_pack = _ceil(after_moq, pack * conversion) if raw else ZERO
        ledger.append(_component("pack_adjustment", "Кратность упаковки", after_pack - after_moq))
        increment = _increment(pack, conversion, quantum)
        candidate = _ceil(after_pack, increment) if raw else ZERO
        ledger.append(_component("quantity_quantum_adjustment", "Точность складского учёта", candidate - after_pack))
        if policy.max_cover_days is not None:
            max_demand = sum((daily[start + timedelta(days=i)][3] for i in range(policy.max_cover_days)), ZERO)
            cap = max(ZERO, max_demand - free - pipeline)
            capped = min(candidate, _floor(cap, increment))
            if capped < moq * conversion:
                capped = ZERO
            ledger.append(_component("max_cover_adjustment", "Лимит покрытия запасом", capped - candidate))
            if capped < candidate:
                warnings.append(_issue("MAX_COVER_LIMIT", "Coverage limit reduces order; service target may not be achieved", sku))
            candidate = capped
        cost = None if term.get("cost_per_base") is None else _decimal(term["cost_per_base"])
        if cost is not None and cost < 0:
            raise _error("INVALID_COST", "Landed cost must be nonnegative", [sku])
        for entry in ledger:
            entry.source_refs = ([forecast.forecast_id] if entry.code in {
                "baseline_demand", "seasonal_delta", "growth_delta", "safety_stock"
            } else [snapshot.snapshot_id, policy.policy_version])
        line = ProposalLine(
            line_id=str(uuid5(NAMESPACE_URL, f"{run_id}/{supplier}/{warehouse}/{sku}")),
            sku_id=sku, name=master.get("name", sku), base_uom=master["base_uom"], purchase_uom=master["purchase_uom"],
            recommended_base_qty=candidate, recommended_purchase_qty=candidate / conversion,
            selected_base_qty=candidate, selected_purchase_qty=candidate / conversion,
            moq_purchase=moq, pack_multiple_purchase=pack, conversion=conversion,
            quantity_quantum=quantum, rop=rop, safety_stock=ss, raw_need=raw,
            unit_cost=cost, line_cost=None if cost is None else cost * candidate,
            urgency=urgency, projected_stockout_date=stockout, explanation=ledger, warnings=warnings,
        )
        revalidate_line_quantity(line, line.selected_purchase_qty)
        synthetic = snapshot.mode == "synthetic_demo" or forecast.mode == "synthetic_demo" or any(
            row.get("provenance") in {"synthetic", "assumed"} for row in [master, stock, term, *used_pipe_rows]
        ) or bool(snapshot.assumptions)
        candidates.append(((supplier, warehouse, term.get("currency")), line, increment, synthetic))
    # SKU/warehouse inventory omitted by the forecast remains visible as an exclusion.
    for sku, warehouse in stocks:
        if (sku, warehouse) not in seen:
            exclusions.append(ExcludedLine(sku_id=sku, reasons=[_issue("MISSING_FORECAST", f"No forecast supplied for warehouse {warehouse}", sku, "blocking")]))
    budget_available = bool(candidates) and all(
        line.unit_cost is not None and bool(key[2]) for key, line, _, _ in candidates
    ) and len({key[2] for key, _, _, _ in candidates}) == 1
    if policy.budget_cap is not None:
        if not budget_available or not policy.currency or any(key[2] != policy.currency for key, _, _, _ in candidates):
            raise _error("UNSUPPORTED_BUDGET", "A global budget requires complete comparable costs and one matching currency for all eligible lines")
        remaining_budget = policy.budget_cap
        ordered = sorted(candidates, key=lambda item: (
            {"critical": 0, "soon": 1, "routine": 2}[item[1].urgency],
            item[1].projected_stockout_date or date.max, item[1].sku_id, item[0][0],
        ))
        updated_lines = {}
        for _, line, increment, _ in ordered:
            before = line.selected_base_qty
            affordable = before if line.unit_cost == 0 else _floor(remaining_budget / line.unit_cost, increment)
            selected = min(before, affordable)
            if selected < line.moq_purchase * line.conversion:
                selected = ZERO
            updated = line.model_dump(mode="python")
            updated.update(selected_base_qty=selected, recommended_base_qty=selected,
                           selected_purchase_qty=selected / line.conversion,
                           recommended_purchase_qty=selected / line.conversion,
                           line_cost=selected * line.unit_cost)
            remaining_budget -= updated["line_cost"]
            updated["explanation"].append(_component("budget_adjustment", "Распределение общего бюджета", selected - before, "Feasible urgency-first greedy allocation; no optimality claim"))
            if selected < before:
                updated["warnings"].append(_issue("BUDGET_DEFERRED", "Shared supplier budget reduces this order; service may fall below target", line.sku_id))
            replacement = ProposalLine.model_validate(updated)
            revalidate_line_quantity(replacement, replacement.selected_purchase_qty)
            updated_lines[line.line_id] = replacement
        candidates = [(key, updated_lines[line.line_id], increment, synthetic)
                      for key, line, increment, synthetic in candidates]
    grouped = defaultdict(list)
    for key, line, _, synthetic in candidates:
        grouped[key, synthetic].append(line)
    if not grouped:
        grouped[(("unassigned", "unassigned", None), snapshot.mode == "synthetic_demo")] = []
    proposals = []
    for (key, synthetic), lines in grouped.items():
        supplier, warehouse, currency = key
        cap_source = snapshot.quality.capabilities
        line_ready = bool(lines) and not any(w.severity == "blocking" for line in lines for w in line.warnings)
        approve = line_ready and cap_source.can_approve
        capabilities = Capabilities(
            can_plan=bool(lines), can_approve=approve,
            can_export=approve and cap_source.can_export,
            budget_available=budget_available,
            customer_detection_available=cap_source.customer_detection_available,
            observed_stockouts_available=cap_source.observed_stockouts_available,
            reasons=list(cap_source.reasons) + ([] if lines else ["No eligible SKU lines"]),
        )
        warnings = []
        if policy.budget_cap is not None:
            warnings.append(_issue("FEASIBLE_HEURISTIC", "Global budget allocated by urgency-first greedy heuristic; result is feasible, not proven optimal", supplier, "info"))
        if exclusions:
            warnings.append(_issue("EXCLUDED_LINES", f"{len(exclusions)} SKU/warehouse records excluded; inspect reasons before interpreting coverage", supplier))
        total = sum((line.line_cost for line in lines), ZERO) if lines and currency and all(line.line_cost is not None for line in lines) else None
        proposal = ProposalDetail(
            proposal_id=str(uuid5(NAMESPACE_URL, f"{run_id}/{supplier}/{warehouse}/{currency}/{synthetic}")),
            version=1, status="draft", content_hash="pending", run_id=run_id,
            snapshot_id=snapshot.snapshot_id, mode="synthetic_demo" if synthetic else "real_preview",
            supplier_id=supplier, warehouse_id=warehouse, as_of=snapshot.as_of, currency=currency,
            total_cost=total, capabilities=capabilities, warnings=warnings, lines=lines, excluded_lines=exclusions,
        )
        payload = proposal.model_dump(mode="json", exclude={"content_hash"})
        proposal.content_hash = sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        proposals.append(proposal)
    return proposals


plan = build_proposals
