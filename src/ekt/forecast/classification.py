"""Auditable, label-blind demand-event classification.

All quantities stay allocated, including signed returns. This is a conservative
review detector, not a classifier of confirmed customer projects. Callers must
filter events to their replay origin before calling it.
"""

from collections import defaultdict
from decimal import Decimal, InvalidOperation
from statistics import median


def _reference_stats(values):
    center = median(values)
    mad = median([abs(value - center) for value in values])
    return len(values), center, mad, max(center * 6, center + Decimal("11.8608") * mad)


def _recurrence_counts(indices, quantities, events, threshold):
    """Distinct-day counts for large values using one sliding sorted window.

    Each event enters and leaves the range once, including when thousands of
    different document quantities belong to the same calendar date.
    """
    ordered = sorted((quantities[i], str(events[i]["event_at"])[:10]) for i in indices)
    candidates = sorted({value for value, _ in ordered if value > threshold})
    counts, active_days = {}, defaultdict(int)
    left = right = 0
    for quantity in candidates:
        while right < len(ordered) and ordered[right][0] <= quantity * 2:
            active_days[ordered[right][1]] += 1
            right += 1
        while left < right and ordered[left][0] < quantity / 2:
            day = ordered[left][1]
            active_days[day] -= 1
            if active_days[day] == 0:
                del active_days[day]
            left += 1
        counts[quantity] = len(active_days)
    return counts


def _quantity(event):
    try:
        quantity = Decimal(str(event["quantity_base"]))
    except (KeyError, InvalidOperation) as exc:
        raise ValueError("quantity_base must be a finite decimal") from exc
    if not quantity.is_finite():
        raise ValueError("quantity_base must be a finite decimal")
    effect = event.get("demand_effect")
    if effect not in {"increase", "decrease", "none"}:
        raise ValueError("Unknown demand_effect must be quarantined before classification")
    return abs(quantity) * {"increase": 1, "decrease": -1, "none": 0}[effect]


def classify_events(events, overrides=()):
    """Return copied event dictionaries with signed, conserved allocations.

    A 6x robust-size threshold plus 8 scaled MADs identifies large candidates.
    Three comparable purchases on distinct dates count as recurrence (including
    sustained shifts), not one-off projects. Two are left uncertain. Short
    histories use a category+UOM fallback if available and explicit low confidence
    otherwise. Oracle/fixture fields are never inspected.
    """
    events = [dict(event) for event in events]
    quantities = [_quantity(event) for event in events]
    groups = defaultdict(list)
    categories = defaultdict(list)
    documents = defaultdict(Decimal)
    customers = defaultdict(Decimal)
    for index, (event, quantity) in enumerate(zip(events, quantities)):
        if quantity <= 0:
            continue
        key = (event["sku_id"], event["warehouse_id"])
        groups[key].append(index)
        # Equal category alone does not establish comparable physical quantities.
        if event.get("category_id") and event.get("base_uom"):
            categories[(event["category_id"], event["base_uom"])].append(index)
        if event.get("doc_id"):
            documents[(key, event["doc_id"])] += quantity
        if event.get("customer_token"):
            customers[(key, event["customer_token"])] += quantity

    # Compute robust reference statistics once per SKU/warehouse, not for every
    # event. This keeps large real exports practical without changing inference.
    profiles = {}
    category_profiles = {}
    for key, indices in groups.items():
        reference = [quantities[i] for i in indices]
        reasons, confidence = [], 0.65
        if len(reference) < 5:
            event = events[indices[0]]
            category_key = (event.get("category_id"), event.get("base_uom"))
            fallback_key = (key[0], category_key)
            if fallback_key not in category_profiles:
                comparable = [quantities[i] for i in categories.get(category_key, []) if events[i]["sku_id"] != key[0]]
                category_profiles[fallback_key] = _reference_stats(comparable) if len(comparable) >= 5 else None
            fallback = category_profiles[fallback_key]
            if fallback:
                stats = fallback
                reasons.append("category_uom_fallback")
            else:
                stats = _reference_stats(reference)
                reasons.append("short_history_low_confidence")
                confidence = 0.4
        else:
            stats = _reference_stats(reference)
        recurrence = _recurrence_counts(indices, quantities, events, stats[3]) if any(value > stats[3] for value in reference) else {}
        profiles[key] = (stats, recurrence, reasons, confidence, sum(reference, Decimal(0)))

    review = {}
    for override in overrides or ():
        if override.get("label") not in {"regular", "project"}:
            raise ValueError("Classification override must be regular or project")
        if not str(override.get("reason", "")).strip():
            raise ValueError("Classification override requires a reason")
        for event_id in override.get("event_ids", []):
            if event_id in review:
                raise ValueError("Overlapping classification overrides are ambiguous")
            review[event_id] = override

    results = []
    for event, quantity in zip(events, quantities):
        label, confidence, reasons, status = "regular", 0.65, [], "not_required"
        key = (event["sku_id"], event["warehouse_id"])
        override = review.get(event.get("event_id"))
        if quantity == 0:
            label, confidence, reasons = "non_demand", 1.0, ["no_demand_effect"]
        elif override:
            label, confidence = override["label"], 1.0
            reasons, status = ["buyer_override"], "confirmed"
        elif quantity < 0:
            reasons = ["signed_demand_decrease"]
        else:
            stats, recurrence, profile_reasons, confidence, total = profiles[key]
            reasons = list(profile_reasons)
            count, center, mad, threshold = stats
            large = count >= 3 and center > 0 and quantity > threshold
            if large:
                comparable_days = recurrence[quantity]
                if comparable_days >= 3:
                    reasons.append("recurrent_large_demand_or_level_shift")
                    confidence = 0.75
                elif comparable_days == 2:
                    label, confidence, status = "uncertain", 0.5, "pending"
                    reasons.append("limited_large_purchase_recurrence")
                else:
                    label, status = "suspected_project", "pending"
                    confidence = 0.7 if count < 5 else 0.9
                    reasons.append("one_off_robust_size_outlier")
                    if mad == 0:
                        reasons.append("zero_mad_ratio_fallback")
                    if event.get("doc_id") and quantity >= documents[(key, event["doc_id"])] * Decimal("0.8"):
                        reasons.append("document_quantity_concentrated")
                    if event.get("customer_token"):
                        if customers[(key, event["customer_token"])] >= total * Decimal("0.8"):
                            reasons.append("customer_quantity_concentrated")
            elif count < 3:
                label, status = "uncertain", "pending"
                reasons.append("insufficient_size_reference")
            if not reasons:
                reasons.append("within_robust_regular_range")

        result = dict(event)
        result.update(
            observed_qty=quantity,
            regular_qty=quantity if label == "regular" else Decimal(0),
            project_qty=quantity if label == "project" else Decimal(0),
            uncertain_qty=quantity if label in {"suspected_project", "uncertain"} else Decimal(0),
            label=label,
            reason_codes=reasons,
            confidence=confidence,
            review_status=status,
        )
        if override and quantity != 0:
            result["override_reason"] = override["reason"]
            result["classification_provenance"] = "override"
        results.append(result)
    return results
