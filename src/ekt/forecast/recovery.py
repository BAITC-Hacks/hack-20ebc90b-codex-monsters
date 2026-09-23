"""Demand recovery from explicit fractional [start, end) stockout evidence."""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from math import isfinite
from statistics import mean


def _datetime(value):
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, date):
        parsed = datetime.combine(value, time.min)
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)


def _fraction(day_start, intervals):
    day_end = day_start + timedelta(days=1)
    clipped = [(max(start, day_start), min(end, day_end), fraction) for start, end, fraction in intervals
               if start < day_end and end > day_start]
    points = sorted({day_start, day_end, *(start for start, _, _ in clipped), *(end for _, end, _ in clipped)})
    unavailable = 0.0
    for start, end in zip(points, points[1:]):
        # Fractions refer to the same unavailable SKU stock; overlaps use max,
        # never a sum. Without identities for disjoint sub-stocks this is the
        # conservative union convention.
        fraction = max((fraction for left, right, fraction in clipped if left < end and right > start), default=0.0)
        unavailable += fraction * (end - start).total_seconds() / 86400
    return min(1.0, unavailable)


def recover_daily(events, intervals, start_date, end_date, *, scopes=()):
    """Return ``(daily_rows, diagnostics)`` for complete covered UTC dates.

    Window is [start_date, end_date). Caller must establish sales coverage; an
    absent transaction is then a real zero. ``intervals=None`` means logs are
    unavailable; ``[]`` means known no stockouts. No unavailable intervals are
    inferred from sales. Donors are fully available days (weekday matched when
    at least two exist). Partial days add donor_mean * unavailable_fraction to
    their actual sales. Missing donors leave loss/corrected demand explicitly
    unknown instead of manufacturing zero. Raw signed net demand is retained.
    ``scopes`` optionally lists {sku_id, warehouse_id} mappings (or tuples) for
    known-covered series without any transactions. Absent logs retain observed
    demand as an explicit observed-only fallback, with loss unknown.
    """
    start = _datetime(start_date).replace(hour=0, minute=0, second=0, microsecond=0)
    end = _datetime(end_date).replace(hour=0, minute=0, second=0, microsecond=0)
    if end <= start:
        raise ValueError("Recovery history window must have positive duration")
    event_groups = defaultdict(lambda: defaultdict(Decimal))
    totals = defaultdict(lambda: {"observed_regular_total": Decimal(0), "project_total": Decimal(0), "uncertain_total": Decimal(0)})
    scope_intervals = defaultdict(list)
    scope_keys = {(scope["sku_id"], scope["warehouse_id"]) if isinstance(scope, dict) else tuple(scope)
                  for scope in scopes}
    for event in events:
        event_at = _datetime(event["event_at"])
        if not start <= event_at < end:
            continue
        key = (event["sku_id"], event["warehouse_id"])
        scope_keys.add(key)
        quantity = Decimal(str(event.get("regular_qty", 0)))
        if not quantity.is_finite() or not isfinite(float(quantity)):
            raise ValueError("Regular quantity must be finite")
        event_groups[key][event_at.date()] += quantity
        totals[key]["observed_regular_total"] += quantity
        for allocation in ("project", "uncertain"):
            amount = Decimal(str(event.get(f"{allocation}_qty", 0)))
            if not amount.is_finite():
                raise ValueError("Allocated quantity must be finite")
            totals[key][f"{allocation}_total"] += amount
    for interval in intervals or ():
        left, right = _datetime(interval["start_at"]), _datetime(interval.get("end_at") or end)
        fraction = float(interval.get("unavailable_fraction", 1.0))
        if not isfinite(fraction) or not 0 <= fraction <= 1:
            raise ValueError("unavailable_fraction must lie in [0, 1]")
        if right <= left:
            raise ValueError("Stockout interval must have positive duration")
        if right <= start or left >= end:
            continue
        key = (interval["sku_id"], interval["warehouse_id"])
        scope_keys.add(key)
        scope_intervals[key].append((left, right, fraction))

    rows, summaries = [], []
    day_starts = [start + timedelta(days=offset) for offset in range((end - start).days)]
    for key in sorted(scope_keys):
        fractions = {day.date(): _fraction(day, scope_intervals[key]) for day in day_starts}
        donors = [(day.date(), max(0.0, float(event_groups[key][day.date()])))
                  for day in day_starts if fractions[day.date()] == 0]
        known_lost, unknown_days = 0.0, 0
        for day in day_starts:
            observed = event_groups[key][day.date()]
            fraction = fractions[day.date()]
            lost = 0.0 if intervals is not None else None
            method = "not_needed" if intervals is not None else "unavailable_no_logs"
            donor_count = 0
            if fraction > 0:
                comparable = [value for donor_date, value in donors if donor_date.weekday() == day.weekday()]
                if len(comparable) < 2:
                    comparable = [value for _, value in donors]
                    method = "in_stock_mean"
                else:
                    method = "weekday_in_stock_mean"
                donor_count = len(comparable)
                if comparable:
                    lost = max(0.0, mean(comparable) * fraction)
                    known_lost += lost
                    if donor_count < 3:
                        method += "_low_confidence"
                else:
                    lost, method = None, "unknown_no_donors"
                    unknown_days += 1
            corrected = observed if intervals is None else (None if lost is None else observed + Decimal(str(lost)))
            rows.append({
                "sku_id": key[0], "warehouse_id": key[1], "date": day.date().isoformat(),
                "observed_regular": observed, "estimated_lost": None if lost is None else Decimal(str(lost)),
                "corrected_demand": corrected, "unavailable_fraction": fraction,
                "recovery_status": method, "donor_count": donor_count,
                "corrected_policy": "observed_only" if intervals is None else "observed_plus_estimated_lost",
            })
        summaries.append({
            "sku_id": key[0], "warehouse_id": key[1], **totals[key],
            "estimated_lost_total": None if unknown_days or intervals is None else Decimal(str(known_lost)),
            "known_estimated_lost_total": Decimal(str(known_lost)),
            "unknown_recovery_days": unknown_days,
            "recovery_status": "unavailable_no_logs" if intervals is None else ("unknown_no_donors" if unknown_days else "available"),
        })
    return rows, {
        "history_start": start.date().isoformat(), "history_end_exclusive": end.date().isoformat(),
        "calendar": "UTC", "overlap_method": "time_integrated_max_fraction",
        "logs_available": intervals is not None, "series": summaries,
    }
