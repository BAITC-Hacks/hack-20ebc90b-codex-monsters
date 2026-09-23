from datetime import date, timedelta
from math import isfinite

import pytest

from ekt.forecast.baseline import forecast_daily, infer_seasonality


ORIGIN = date(2026, 4, 1)


def history(values):
    return [{"date": (ORIGIN - timedelta(days=len(values) - index)).isoformat(), "corrected_demand": value}
            for index, value in enumerate(values)]


def horizon(days=90):
    return [ORIGIN + timedelta(days=offset) for offset in range(days)]


def override(rate, mode="replace"):
    return {"category_id": "switch", "mode": mode, "rate": rate, "valid_from": "2026-04-01", "valid_to": "2026-06-30"}


def test_complete_90_day_finite_nonnegative_exact_additive_decomposition():
    result = forecast_daily(history([10] * 56), horizon())
    assert len(result["daily"]) == 90
    assert result["daily_residual_std"] == 0
    for row in result["daily"]:
        assert isfinite(row["mean"]) and row["mean"] >= 0
        assert row["baseline_mean"] + row["seasonal_delta"] + row["growth_delta"] == pytest.approx(row["mean"], abs=1e-8)
        assert row["mean"] == 10


def test_single_outlier_does_not_make_100x_baseline():
    result = forecast_daily(history([10] * 55 + [1000]), horizon())
    assert result["daily"][0]["mean"] < 15
    assert result["metadata"]["learned_monthly_growth_rate"] == 0


def test_intermitttent_known_demand_not_replaced_with_zero():
    result = forecast_daily(history([0, 0, 0, 0, 0, 0, 70] * 8), horizon())
    assert result["daily"][0]["mean"] == 10


def test_sustained_growth_learned_only_from_complete_training_windows():
    result = forecast_daily(history([10] * 14 + [12] * 14 + [14] * 14), horizon())
    assert 0 < result["metadata"]["learned_monthly_growth_rate"] <= 0.35
    assert result["daily"][-1]["mean"] > result["daily"][0]["mean"]
    assert result["daily"][-1]["mean"] <= result["daily"][-1]["baseline_mean"] * 2


def test_growth_replace_does_not_apply_learned_growth_twice():
    data = history([10] * 14 + [12] * 14 + [14] * 14)
    replaced = forecast_daily(data, horizon(), "switch", [override(0.2)])
    additive = forecast_daily(data, horizon(), "switch", [override(0.2, "incremental")])
    learned = forecast_daily(data, horizon(), "switch")
    for replacement, addition, original in zip(replaced["daily"], additive["daily"], learned["daily"]):
        assert replacement["mean"] == pytest.approx(replacement["baseline_mean"] * 1.2)
        assert addition["mean"] == pytest.approx(original["mean"] + addition["baseline_mean"] * 0.2)


def test_verified_seasonality_changes_only_expected_months():
    config = {"verified": True, "factors": {str(month): (1.5 if month == 4 else 1) for month in range(1, 13)},
              "provenance": "synthetic", "source": "synthetic seasonal fixture"}
    result = forecast_daily(history([10] * 56), horizon(), seasonality=config)
    assert result["daily"][0]["baseline_mean"] == 10
    assert result["daily"][0]["seasonal_delta"] == 5
    assert result["daily"][30]["seasonal_delta"] == 0
    assert result["metadata"]["seasonality_provenance"] == "synthetic"
    neutral = forecast_daily(history([10] * 56), horizon(), seasonality={"factors": {4: 100}})
    assert neutral["daily"][0]["mean"] == 10


def test_future_injection_does_not_change_any_forecast_or_uncertainty():
    data = history([10] * 56)
    expected = forecast_daily(data, horizon())
    injected = data + [{"date": "2026-04-01", "corrected_demand": 1e8}, {"date": "2026-05-01", "corrected_demand": 1e9}]
    assert forecast_daily(injected, horizon()) == expected


def test_unknown_history_does_not_become_zero_and_imputation_is_not_residual_truth():
    blocked = forecast_daily(history([None] * 50), horizon())
    assert blocked["daily"] == []
    assert blocked["daily_residual_std"] is None
    data = history([10] * 28)
    for row in data:
        row.update(unavailable_fraction=1, observed_regular=0)
    assert forecast_daily(data, horizon())["daily_residual_std"] is None


def test_unknown_coverage_null_availability_produces_explicit_uncertainty_warning():
    data = history([8, None, 12, None, 10])
    for row in data:
        row.update(unavailable_fraction=None, recovery_status="unavailable_unknown_coverage")
    result = forecast_daily(data, horizon())
    assert result["daily"][0]["mean"] == 10
    assert "UNCERTAINTY_STOCKOUT_STATUS_UNKNOWN" in result["warnings"]
    assert result["daily_residual_std"] == 2


def test_verified_annual_month_pattern_is_not_learned_again_as_growth():
    seasonal = {month: 1 + month / 12 for month in range(1, 13)}
    config = {"verified": True, "factors": seasonal, "provenance": "synthetic", "source": "annual-pattern-test"}
    data = history([10] * 365)
    for row in data:
        row["corrected_demand"] = 10 * seasonal[date.fromisoformat(row["date"]).month]
    result = forecast_daily(data, horizon(), seasonality=config)
    assert result["metadata"]["learned_monthly_growth_rate"] == 0
    for row in result["daily"]:
        assert row["baseline_mean"] == pytest.approx(10)
        assert row["mean"] == pytest.approx(10 * seasonal[date.fromisoformat(row["date"]).month])


def test_partial_history_does_not_treat_missing_days_as_declining_sales():
    data = history([10] * 42)
    del data[-5:]
    result = forecast_daily(data, horizon())
    assert result["metadata"]["learned_monthly_growth_rate"] == 0
    assert result["daily"][0]["mean"] == 10
    assert "GAP_BETWEEN_HISTORY_AND_FORECAST" in result["warnings"]


def test_sustained_growth_survives_explicit_unfinished_day_gap():
    data = history([10] * 14 + [12] * 14 + [14] * 14)
    later_horizon = [day + timedelta(days=1) for day in horizon()]
    result = forecast_daily(data, later_horizon)
    assert result["metadata"]["learned_monthly_growth_rate"] > 0
    assert result["metadata"]["history_to_forecast_gap_days"] == 1
    assert result["metadata"]["training_end_exclusive"] == ORIGIN.isoformat()


def test_interior_unknown_dates_prevent_complete_window_growth_estimation():
    data = history([10] * 14 + [12] * 14 + [14] * 14)
    del data[-5]
    result = forecast_daily(data, horizon())
    assert result["metadata"]["learned_monthly_growth_rate"] == 0
    assert "GROWTH_INSUFFICIENT_COMPLETE_HISTORY_NEUTRAL" in result["warnings"]


def annual_history():
    first = date(2024, 4, 1)
    days = (ORIGIN - first).days
    return [{"date": (day := first + timedelta(days=index)).isoformat(),
             "corrected_demand": 10 * (1 + day.month / 12)} for index in range(days)]


def test_complete_repeated_annual_pattern_is_inferred_without_oracle():
    data = annual_history()
    result = forecast_daily(data, horizon())
    assert "SEASONALITY_ESTIMATED_UNVALIDATED" in result["warnings"]
    assert result["metadata"]["seasonality_provenance"] == "derived"
    assert result["metadata"]["learned_monthly_growth_rate"] == 0
    assert result["metadata"]["seasonality_evidence_end"] == "2026-03-31"
    assert any(abs(row["seasonal_delta"]) > 0.1 for row in result["daily"])
    for row in result["daily"]:
        assert row["mean"] == pytest.approx(10 * (1 + date.fromisoformat(row["date"]).month / 12))


def test_inferred_seasonality_normalizes_between_year_level_changes():
    data = annual_history()
    expected = infer_seasonality(data, ORIGIN)["factors"]
    for row in data:
        if row["date"] >= "2025-04-01":
            row["corrected_demand"] *= 2
    assert infer_seasonality(data, ORIGIN)["factors"] == pytest.approx(expected)


def test_automatic_seasonality_excludes_future_and_partial_months():
    data = annual_history()
    expected = forecast_daily(data, horizon())
    assert forecast_daily(data + [{"date": "2026-04-01", "corrected_demand": 1e12}], horizon()) == expected
    data = [row for row in data if row["date"] != "2025-01-15"]
    config = infer_seasonality(data, ORIGIN)
    assert config is not None
    assert 1 not in config["factors"]  # One complete January is insufficient.
    assert len(config["factors"]) == 11
    assert infer_seasonality(annual_history()[-365:], ORIGIN) is None


def test_explicit_neutral_unverified_config_is_not_replaced_by_auto_estimate():
    result = forecast_daily(annual_history(), horizon(), seasonality={"verified": False})
    assert "SEASONALITY_ESTIMATED_UNVALIDATED" not in result["warnings"]
    assert all(row["seasonal_delta"] == 0 for row in result["daily"])


def test_signed_net_returns_are_clamped_only_at_forecast_boundary():
    result = forecast_daily(history([-2] * 28), horizon())
    assert all(row["mean"] == 0 for row in result["daily"])
    assert "NEGATIVE_NET_DEMAND_CLAMPED_FOR_FORECAST" in result["warnings"]


def test_overlapping_overrides_rejected():
    with pytest.raises(ValueError, match="Overlapping"):
        forecast_daily(history([10] * 28), horizon(), "switch", [override(0.2), override(0.3)])


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_history_rejected(value):
    with pytest.raises(ValueError, match="finite"):
        forecast_daily(history([value] * 28), horizon())
