"""Small, independent evaluation helpers for a single compatible demand series.

Metrics are fractions, not percentages. Positive bias means overforecasting.
Missing observations must be excluded explicitly by the caller, never filled
with model predictions. No function converts or aggregates different UOMs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from math import fsum, isfinite
from typing import Literal


def _numbers(values: Sequence[float], name: str) -> tuple[float, ...]:
    try:
        result = tuple(float(value) for value in values)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must contain finite numbers; unknown is not zero") from exc
    if not all(isfinite(value) for value in result):
        raise ValueError(f"{name} must contain finite numbers")
    return result


def _paired(
    actual: Sequence[float], predicted: Sequence[float]
) -> tuple[tuple[float, ...], tuple[float, ...]]:
    actual_values = _numbers(actual, "actual")
    predicted_values = _numbers(predicted, "predicted")
    if len(actual_values) != len(predicted_values):
        raise ValueError("actual and predicted must have equal lengths")
    return actual_values, predicted_values


def _unit(uom: str | Sequence[str], count: int) -> str:
    """A unit label certifies one series; row labels additionally verify it."""
    if isinstance(uom, str):
        if not uom.strip():
            raise ValueError("uom must be a nonempty explicit unit")
        return uom
    units = tuple(uom)
    if len(units) != count:
        raise ValueError("row UOM count must equal observation count")
    if not units or any(not isinstance(unit, str) or not unit.strip() for unit in units):
        raise ValueError("each observation must have a nonempty UOM")
    if len(set(units)) != 1:
        raise ValueError("incompatible UOMs cannot be aggregated; evaluate separately")
    return units[0]


def wape(actual: Sequence[float], predicted: Sequence[float]) -> float | None:
    """sum(abs(predicted-actual)) / sum(abs(actual)); zero total is undefined."""
    actual_values, predicted_values = _paired(actual, predicted)
    denominator = fsum(abs(value) for value in actual_values)
    if denominator == 0:
        return None
    return fsum(abs(pred - obs) for obs, pred in zip(actual_values, predicted_values)) / denominator


def signed_bias(actual: Sequence[float], predicted: Sequence[float]) -> float | None:
    """sum(predicted-actual) / sum(abs(actual)); positive means overforecast."""
    actual_values, predicted_values = _paired(actual, predicted)
    denominator = fsum(abs(value) for value in actual_values)
    if denominator == 0:
        return None
    return fsum(pred - obs for obs, pred in zip(actual_values, predicted_values)) / denominator


def _naive_scale(train: tuple[float, ...], seasonal_period: int) -> float | None:
    if isinstance(seasonal_period, bool) or not isinstance(seasonal_period, int) or seasonal_period < 1:
        raise ValueError("seasonal_period must be a positive integer")
    if len(train) <= seasonal_period:
        return None
    return fsum(
        abs(train[index] - train[index - seasonal_period])
        for index in range(seasonal_period, len(train))
    ) / (len(train) - seasonal_period)


def mase(
    actual: Sequence[float],
    predicted: Sequence[float],
    train: Sequence[float],
    *,
    seasonal_period: int = 1,
) -> float | None:
    """MAE divided by the training-only lagged naive MAE.

    The holdout cannot affect the denominator. Constant or insufficient
    training history and an empty evaluation sample return None.
    """
    actual_values, predicted_values = _paired(actual, predicted)
    scale = _naive_scale(_numbers(train, "train"), seasonal_period)
    if not actual_values or scale is None or scale == 0:
        return None
    mae = fsum(abs(pred - obs) for obs, pred in zip(actual_values, predicted_values)) / len(actual_values)
    return mae / scale


def evaluate_forecast(
    actual: Sequence[float],
    predicted: Sequence[float],
    *,
    train: Sequence[float],
    uom: str | Sequence[str],
    seasonal_period: int = 1,
) -> dict:
    """Evaluate one series, preserving undefined values and their reasons.

    ``uom`` may be a single explicit unit or a label per holdout observation.
    Training values must belong to that same series and base unit. Call this
    separately for each SKU/warehouse/UOM; there is no cross-series pooling.
    """
    actual_values, predicted_values = _paired(actual, predicted)
    train_values = _numbers(train, "train")
    unit = _unit(uom, len(actual_values))
    scale = _naive_scale(train_values, seasonal_period)
    undefined: dict[str, str] = {}
    if not actual_values:
        undefined = {name: "empty_evaluation_sample" for name in ("wape", "bias", "mase")}
    else:
        if fsum(abs(value) for value in actual_values) == 0:
            undefined.update(wape="zero_actual_total", bias="zero_actual_total")
        if scale is None:
            undefined["mase"] = "insufficient_training_history"
        elif scale == 0:
            undefined["mase"] = "zero_training_naive_error"
    return {
        "uom": unit,
        "n": len(actual_values),
        "train_n": len(train_values),
        "seasonal_period": seasonal_period,
        "wape": wape(actual_values, predicted_values),
        "bias": signed_bias(actual_values, predicted_values),
        "mase": mase(actual_values, predicted_values, train_values, seasonal_period=seasonal_period),
        "mase_train_scale": scale,
        "undefined": undefined,
        "bias_convention": "positive_is_overforecast",
    }


def rolling_origin_evaluation(
    history: Sequence[float],
    origins: Sequence[int],
    horizon: int,
    forecaster: Callable[[tuple[float, ...], int], Sequence[float]],
    *,
    uom: str,
    recent_window: int = 28,
    seasonal_period: int = 1,
) -> dict:
    """Compare recent mean and the final model on chronological holdouts.

    An origin is the number of completed observations available for training.
    The callback receives only an immutable training prefix and the horizon,
    never holdout values. Calendar features can be supplied in a closure, but
    their coefficients must also be fitted within the callback on that prefix.
    Origins may overlap: evidence stays per-origin, without pooling errors or
    claiming that overlapping samples are independent.
    """
    values = _numbers(history, "history")
    unit = _unit(uom, len(values))
    for name, value in (("horizon", horizon), ("recent_window", recent_window)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
    _naive_scale((), seasonal_period)
    cutoffs = tuple(origins)
    if not cutoffs:
        raise ValueError("at least one temporal origin is required")
    if any(isinstance(origin, bool) or not isinstance(origin, int) for origin in cutoffs):
        raise ValueError("origins must be integer training lengths")
    if len(set(cutoffs)) != len(cutoffs):
        raise ValueError("origins must be unique")
    if any(origin < 1 or origin + horizon > len(values) for origin in cutoffs):
        raise ValueError("each origin needs nonempty training and a complete holdout")
    evidence = []
    for origin in sorted(cutoffs):
        train = values[:origin]
        actual = values[origin : origin + horizon]
        recent = train[-recent_window:]
        recent_predictions = (fsum(recent) / len(recent),) * horizon
        model_predictions = _numbers(forecaster(train, horizon), "model predictions")
        if len(model_predictions) != horizon:
            raise ValueError("forecaster must return exactly horizon predictions")
        if any(value < 0 for value in model_predictions):
            raise ValueError("demand forecast must be nonnegative")
        evidence.append({
            "origin": origin,
            "train_start": 0,
            "train_end_exclusive": origin,
            "holdout_end_exclusive": origin + horizon,
            "actual": list(actual),
            "models": {
                name: {
                    "predicted": list(predicted),
                    "metrics": evaluate_forecast(actual, predicted, train=train, uom=unit, seasonal_period=seasonal_period),
                }
                for name, predicted in (("recent_mean", recent_predictions), ("final_baseline", model_predictions))
            },
        })
    return {
        "method": "temporal_holdout" if len(evidence) == 1 else "rolling_origin",
        "uom": unit,
        "horizon": horizon,
        "recent_window": recent_window,
        "origins": evidence,
        "limitations": ["Small descriptive backtest; no achieved service-level claim.", "Overlapping holdouts, if any, are not independent samples."],
    }


def evaluate_recovery(
    recovered: Sequence[float],
    truth: Sequence[float],
    *,
    truth_source: Literal["synthetic_latent_truth", "masked_in_stock"],
    uom: str | Sequence[str],
) -> dict:
    """Score recovery only against independently supplied oracle observations.

    The truth source is mandatory. A caller remains responsible for provenance;
    renaming/copying model imputation does not make it valid ground truth. The
    same object is rejected to catch accidental direct self-scoring.
    """
    if truth_source not in ("synthetic_latent_truth", "masked_in_stock"):
        raise ValueError("recovery needs independent latent truth or masked in-stock observations")
    if recovered is truth:
        raise ValueError("recovery cannot be scored against its own imputation")
    actual, predicted = _paired(truth, recovered)
    unit = _unit(uom, len(actual))
    if any(value < 0 for value in actual + predicted):
        raise ValueError("recovered demand and independent truth must be nonnegative")
    return {
        "truth_source": truth_source,
        "uom": unit,
        "n": len(actual),
        "mae": fsum(abs(pred - obs) for obs, pred in zip(actual, predicted)) / len(actual) if actual else None,
        "wape": wape(actual, predicted),
        "bias": signed_bias(actual, predicted),
        "undefined": {name: "empty_evaluation_sample" if not actual else "zero_actual_total" for name in ("wape", "bias") if not actual or fsum(abs(value) for value in actual) == 0},
    }


def evaluate_masked_recovery(
    known_in_stock: Sequence[float],
    mask_indices: Sequence[int],
    recover: Callable[[tuple[float | None, ...]], Sequence[float]],
    *,
    uom: str,
) -> dict:
    """Hide known in-stock values from a recovery callback and score those only.

    This is an independent validation experiment, not a claim that real
    stockout intervals are known. The callback returns one value per position.
    """
    known = _numbers(known_in_stock, "known_in_stock")
    _unit(uom, len(known))
    if any(value < 0 for value in known):
        raise ValueError("known in-stock demand must be nonnegative")
    indices = tuple(mask_indices)
    if not indices or any(isinstance(index, bool) or not isinstance(index, int) for index in indices):
        raise ValueError("mask_indices must contain integer positions")
    if len(set(indices)) != len(indices) or any(index < 0 or index >= len(known) for index in indices):
        raise ValueError("mask_indices must be unique valid positions")
    masked_set = set(indices)
    masked = tuple(None if index in masked_set else value for index, value in enumerate(known))
    recovered = _numbers(recover(masked), "recovered")
    if len(recovered) != len(known):
        raise ValueError("recovery callback must return one value per input position")
    report = evaluate_recovery(
        [recovered[index] for index in indices],
        [known[index] for index in indices],
        truth_source="masked_in_stock",
        uom=uom,
    )
    report["masked_indices"] = list(indices)
    return report
