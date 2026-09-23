"""Train-only robust daily baseline with explicit, unvalidated uncertainty."""

from datetime import date, datetime
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


def forecast_daily(history, horizon_dates, category_id=None, growth_overrides=(), seasonality=None):
    """Return daily decomposition, residual std, warnings and method metadata.

    ``history`` contains one SKU/warehouse, covered dates and corrected_demand
    (unknown values are None). Only dates strictly before min(horizon_dates)
    train the model. ``seasonality`` is {verified, factors:{month:factor}, source,
    provenance}; unverified factors are neutral. Known factors deseasonalize
    training observations before estimating the recent 28-day level.

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
    recent = [value for day, value in sorted(observations.items()) if 0 < (origin - day).days <= 28]
    if not recent:
        warnings.append("STALE_HISTORY_RECENT_LEVEL_UNAVAILABLE")
        recent = [value for _, value in sorted(observations.items())[-28:]]
    if len(recent) < 14:
        warnings.append("SHORT_HISTORY_LOW_CONFIDENCE")
    baseline = _robust_mean(recent)
    monthly_rate = 0.0
    blocks = []
    for lower, upper in ((29, 42), (15, 28), (1, 14)):
        values = [value for day, value in observations.items() if lower <= (origin - day).days <= upper]
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
        log_growth = log(1 + monthly_rate) * (((day - origin).days + 1) / 30)
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
            "known_history_days": len(observations), "recent_history_days": len(recent),
            "learned_monthly_growth_rate": monthly_rate, "seasonality_provenance": provenance,
            "seasonality_source": None if not seasonality else seasonality.get("source"),
            "growth_override_semantics": "cumulative_rate_per_active_date; replace=1+rate; incremental=learned_factor+rate",
            "residual_observation_count": len(residuals), "calibration": "unvalidated",
            "uncertainty_assumption": "Observed daily residuals excluding known stockouts; missing logs may hide censored demand; IID normal approximation; temporal dependence and achieved service unvalidated",
        },
    }
