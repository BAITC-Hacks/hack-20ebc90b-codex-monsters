"""Train-only robust daily baseline with explicit, unvalidated uncertainty."""

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from math import exp, isfinite, log
from statistics import mean, median, stdev


def _date(value):
    if isinstance(value, datetime):
        return value.date()
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _finite(value, name):
    result = float(value)
    if not isfinite(result):
        raise ValueError(f"{name} must be finite")
    return result


def _robust_mean(values):
    if not values:
        return None
    if len(values) < 7:
        return mean(values)
    center = median(values)
    mad = median([abs(value - center) for value in values])
    if center > 0:
        cap = max(center * 6, center + 8 * 1.4826 * mad)
    else:
        # Preserve intermittent demand; only isolated extreme positive values
        # are capped relative to the positive-demand median.
        positives = [value for value in values if value > 0]
        cap = max(positives, default=0.0) if len(positives) < 3 else median(positives) * 6
    return mean([min(value, cap) for value in values])


def _seasonality(config, warnings):
    if not config or config.get("verified") is not True:
        warnings.append("SEASONALITY_NEUTRAL_UNVERIFIED")
        return {}, "neutral"
    provenance = config.get("provenance")
    if provenance not in {"observed", "derived", "synthetic", "override"} or not config.get("source"):
        raise ValueError("Verified seasonal factors require provenance and source")
    factors = {}
    for month, value in config.get("factors", {}).items():
        month, factor = int(month), _finite(value, "Seasonal factor")
        if month not in range(1, 13) or factor <= 0:
            raise ValueError("Seasonal factors require months 1..12 and positive values")
        factors[month] = factor
    if len(factors) < 12:
        warnings.append("SEASONALITY_MISSING_MONTHS_NEUTRAL")
    return factors, provenance


def infer_seasonality(history, origin):
    """Estimate bounded calendar-month factors from covered training dates.

    A calendar month is usable only when every date is present and known before
    ``origin``. Require two complete instances of each estimated calendar month
    and at least ten supported months. Normalize monthly means within 12-month
    cycles anchored at the earliest complete month to reduce between-year level
    drift; each cycle needs ten supported months. Missing months stay neutral.
    Factors are capped to [0.5, 2]. ``verified`` certifies these coverage checks,
    not forecast accuracy: the estimate is derived and explicitly unvalidated.
    No fixture labels, future rows or future partial-month extrapolation enter.
    """
    origin = _date(origin)
    months = defaultdict(dict)
    seen = set()
    for row in history:
        day = _date(row["date"])
        if day >= origin:
            continue
        if day in seen:
            raise ValueError("History must contain one row per covered date and series")
        seen.add(day)
        value = row.get("corrected_demand")
        if value is not None:
            months[(day.year, day.month)][day.day] = max(0.0, _finite(value, "Historical demand"))
    complete = {
        (year, month): mean(values.values())
        for (year, month), values in months.items()
        if len(values) == monthrange(year, month)[1]
        and date(year, month, monthrange(year, month)[1]) < origin
    }
    repetitions = defaultdict(int)
    for _, month in complete:
        repetitions[month] += 1
    supported = {month for month, count in repetitions.items() if count >= 2}
    if len(supported) < 10:
        return None
    first_year, first_month = min(complete)
    anchor = first_year * 12 + first_month
    cycles = defaultdict(dict)
    for (year, month), value in sorted(complete.items()):
        if month in supported:
            cycles[(year * 12 + month - anchor) // 12][month] = value
    relative = defaultdict(list)
    for cycle in cycles.values():
        if len(cycle) < 10:
            continue
        scale = mean(cycle.values())
        if scale <= 0:
            continue
        for month, value in cycle.items():
            relative[month].append(value / scale)
    factors = {month: min(2.0, max(0.5, mean(values)))
               for month, values in relative.items() if len(values) >= 2}
    if len(factors) < 10:
        return None
    last_year, last_month = max(complete)
    return {
        "verified": True, "factors": factors, "provenance": "derived",
        "source": "train_only_complete_month_cycle_normalization",
        "method": "full_calendar_month_means_normalized_within_12_month_cycles",
        "evidence_end": date(last_year, last_month, monthrange(last_year, last_month)[1]).isoformat(),
        "complete_month_count": len(complete), "supported_calendar_months": sorted(factors),
        "calibration": "unvalidated",
    }


def forecast_daily(history, horizon_dates, category_id=None, growth_overrides=(), seasonality=None):
    """Return daily decomposition, residual std, warnings and method metadata.

    ``history`` contains one SKU/warehouse, covered dates and corrected_demand
    (unknown values are None). Only dates strictly before min(horizon_dates)
    train the model. ``seasonality`` is {verified, factors:{month:factor}, source,
    provenance}; unverified factors are neutral. Known factors deseasonalize
    training observations before estimating the recent 28-day level.
    With no supplied config, infer_seasonality can estimate factors from at
    least two sufficiently complete annual cycles; shorter history is neutral.

    Learned growth needs three complete fortnight windows with the same
    sustained direction, is bounded to +/-35% per 30 days and to a forecast
    factor [0.5,2]. Override rate is a cumulative proportional uplift per active
    day, not a monthly/annual rate: replace sets 1+rate; incremental adds rate
    once to learned_factor. One matching override per date is allowed.
    """
    history = list(history)
    horizons = [_date(value) for value in horizon_dates]
    if not horizons or len(set(horizons)) != len(horizons):
        raise ValueError("Forecast horizon must contain unique dates")
    origin = min(horizons)
    warnings = []
    if seasonality is None:
        seasonality = infer_seasonality(history, origin)
        if seasonality:
            warnings.append("SEASONALITY_ESTIMATED_UNVALIDATED")
    factors, provenance = _seasonality(seasonality, warnings)
    observations = {}
    unknown = 0
    for row in history:
        day = _date(row["date"])
        if day >= origin:
            continue
        if day in observations:
            raise ValueError("History must contain one row per covered date and series")
        value = row.get("corrected_demand")
        if value is None:
            unknown += 1
            continue
        value = _finite(value, "Historical demand")
        if value < 0:
            warnings.append("NEGATIVE_NET_DEMAND_CLAMPED_FOR_FORECAST")
        observations[day] = _finite(max(0.0, value) / factors.get(day.month, 1.0), "Deseasonalized demand")
    if unknown:
        warnings.append("UNKNOWN_HISTORY_EXCLUDED")
    if not observations:
        return {
            "daily": [], "daily_residual_std": None,
            "warnings": list(dict.fromkeys(warnings + ["NO_KNOWN_DEMAND_HISTORY"])),
            "metadata": {"status": "blocked", "origin": origin.isoformat(), "known_history_days": 0},
        }
    # A midnight replay may deliberately skip its unfinished date before the
    # first forecast date. Learn complete windows through the last known day,
    # then extrapolate the gap; never turn that missing day into a sales zero.
    training_end = max(observations) + timedelta(days=1)
    history_gap = (origin - training_end).days
    if history_gap:
        warnings.append("GAP_BETWEEN_HISTORY_AND_FORECAST")
    if history_gap >= 28:
        warnings.append("STALE_HISTORY_RECENT_LEVEL_UNAVAILABLE")
    recent = [value for day, value in sorted(observations.items()) if 0 < (training_end - day).days <= 28]
    if len(recent) < 14:
        warnings.append("SHORT_HISTORY_LOW_CONFIDENCE")
    baseline = _robust_mean(recent)
    monthly_rate = 0.0
    blocks = []
    for lower, upper in ((29, 42), (15, 28), (1, 14)):
        values = [value for day, value in observations.items() if lower <= (training_end - day).days <= upper]
        blocks.append(_robust_mean(values) if len(values) == 14 else None)
    if all(value is not None and value > 0 for value in blocks):
        first, middle, last = blocks
        changes = (middle / first - 1, last / middle - 1)
        if min(changes) > 0.02 or max(changes) < -0.02:
            monthly_log_growth = (log(last) - log(first)) * (30 / 28)
            monthly_rate = min(0.35, max(-0.35, exp(min(log(1.35), max(log(0.65), monthly_log_growth))) - 1))
        else:
            warnings.append("GROWTH_NOT_SUSTAINED_NEUTRAL")
    else:
        warnings.append("GROWTH_INSUFFICIENT_COMPLETE_HISTORY_NEUTRAL")

    applicable_overrides = []
    for override in growth_overrides or ():
        if override.get("category_id") != category_id or category_id is None:
            continue
        rate = _finite(override["rate"], "Growth override rate")
        if rate <= -1 or override.get("mode") not in {"replace", "incremental"}:
            raise ValueError("Growth override requires rate > -1 and replace/incremental mode")
        left, right = _date(override["valid_from"]), _date(override["valid_to"])
        if right < left:
            raise ValueError("Growth override valid_to precedes valid_from")
        applicable_overrides.append((left, right, override["mode"], rate))

    daily = []
    for day in sorted(horizons):
        seasonal = _finite(baseline * factors.get(day.month, 1.0), "Seasonal demand")
        log_growth = log(1 + monthly_rate) * (((day - training_end).days + 1) / 30)
        factor = exp(min(log(2.0), max(log(0.5), log_growth)))
        active = [(mode, rate) for left, right, mode, rate in applicable_overrides if left <= day <= right]
        if len(active) > 1:
            raise ValueError("Overlapping growth overrides are ambiguous")
        if active:
            mode, rate = active[0]
            factor = 1 + rate if mode == "replace" else max(0.0, factor + rate)
        predicted = _finite(max(0.0, seasonal * factor), "Forecast mean")
        daily.append({
            "date": day.isoformat(), "baseline_mean": baseline,
            "seasonal_delta": seasonal - baseline, "growth_delta": predicted - seasonal, "mean": predicted,
        })
    # Residuals use actual in-stock observations where provided, excluding
    # imputation and unknown recovery so recovered values cannot imply precision.
    residuals = []
    for row in history:
        day = _date(row["date"])
        fraction = row.get("unavailable_fraction", 0)
        if day not in observations or (fraction is not None and fraction > 0):
            continue
        if fraction is None or row.get("recovery_status") in {"unavailable_no_logs", "unavailable_unknown_coverage"}:
            warnings.append("UNCERTAINTY_STOCKOUT_STATUS_UNKNOWN")
        observed = row.get("observed_regular", row.get("corrected_demand"))
        if observed is not None:
            residuals.append(_finite(observed, "Observed demand") - baseline * factors.get(day.month, 1.0))
    sigma = _finite(stdev(residuals), "Residual standard deviation") if len(residuals) >= 2 else None
    if sigma is None:
        warnings.append("UNCERTAINTY_INSUFFICIENT_OBSERVED_HISTORY")
    return {
        "daily": daily, "daily_residual_std": sigma,
        "warnings": list(dict.fromkeys(warnings)),
        "metadata": {
            "status": "degraded" if warnings else "ready", "origin": origin.isoformat(),
            "training_end_exclusive": training_end.isoformat(), "history_to_forecast_gap_days": history_gap,
            "known_history_days": len(observations), "recent_history_days": len(recent),
            "learned_monthly_growth_rate": monthly_rate, "seasonality_provenance": provenance,
            "seasonality_source": None if not seasonality else seasonality.get("source"),
            "seasonality_evidence_end": None if not seasonality else seasonality.get("evidence_end"),
            "seasonality_factors": factors,
            "growth_override_semantics": "cumulative_rate_per_active_date; replace=1+rate; incremental=learned_factor+rate",
            "residual_observation_count": len(residuals), "calibration": "unvalidated",
            "uncertainty_assumption": "Observed daily residuals excluding known stockouts; missing logs may hide censored demand; IID normal approximation; temporal dependence and achieved service unvalidated",
        },
    }
