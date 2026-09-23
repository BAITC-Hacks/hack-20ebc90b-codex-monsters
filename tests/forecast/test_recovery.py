from decimal import Decimal

import pytest

from ekt.forecast.recovery import recover_daily


def event(day, qty):
    return {"event_at": f"2026-01-{day:02d}T08:00:00Z", "sku_id": "00123", "warehouse_id": "almaty", "regular_qty": str(qty)}


def interval(start, end, fraction=1):
    return {"sku_id": "00123", "warehouse_id": "almaty", "start_at": start, "end_at": end, "unavailable_fraction": fraction}


def test_stockout_adds_loss_but_in_stock_zero_stays_zero():
    events = [event(1, 10), event(2, 10), event(3, 0), event(5, 0)]
    rows, diagnostics = recover_daily(events, [interval("2026-01-04", "2026-01-05")], "2026-01-01", "2026-01-06")
    assert rows[3]["estimated_lost"] == 5
    assert rows[4]["corrected_demand"] == 0
    for row in rows:
        assert row["corrected_demand"] == row["observed_regular"] + row["estimated_lost"]
    assert diagnostics["series"][0]["estimated_lost_total"] == 5


def test_overlap_union_and_half_open_end_do_not_double_count():
    intervals = [interval("2026-01-03T00:00:00Z", "2026-01-04T00:00:00Z"),
                 interval("2026-01-03T06:00:00Z", "2026-01-03T20:00:00Z")]
    rows, _ = recover_daily([event(1, 10), event(2, 10), event(4, 10)], intervals, "2026-01-01", "2026-01-05")
    assert rows[2]["estimated_lost"] == 10
    assert rows[2]["unavailable_fraction"] == 1
    assert rows[3]["unavailable_fraction"] == 0


def test_fractional_time_and_fractional_unavailability_preserve_sales():
    intervals = [interval("2026-01-03T00:00:00Z", "2026-01-03T12:00:00Z", 0.5)]
    rows, _ = recover_daily([event(1, 12), event(2, 12), event(3, 9)], intervals, "2026-01-01", "2026-01-04")
    assert rows[2]["unavailable_fraction"] == 0.25
    assert rows[2]["estimated_lost"] == 3
    assert rows[2]["corrected_demand"] == 12


def test_overlapping_fractions_use_max_over_time():
    intervals = [interval("2026-01-03", "2026-01-04", 0.5), interval("2026-01-03T12:00:00Z", "2026-01-04", 0.75)]
    rows, _ = recover_daily([event(1, 8), event(2, 8)], intervals, "2026-01-01", "2026-01-04")
    assert rows[2]["unavailable_fraction"] == 0.625
    assert rows[2]["estimated_lost"] == 5


def test_no_donors_stays_unknown():
    rows, diagnostics = recover_daily([event(1, 2)], [interval("2026-01-01", None)], "2026-01-01", "2026-01-03")
    assert all(row["estimated_lost"] is None for row in rows)
    assert all(row["corrected_demand"] is None for row in rows)
    assert diagnostics["series"][0]["estimated_lost_total"] is None
    assert rows[0]["observed_regular"] == 2


def test_absent_logs_differ_from_known_no_intervals_and_preserve_signed_returns():
    rows, diagnostics = recover_daily([event(1, "-2.5")], None, "2026-01-01", "2026-01-03")
    assert rows[0]["corrected_demand"] == Decimal("-2.5")
    assert rows[0]["recovery_status"] == "unavailable_no_logs"
    assert rows[0]["estimated_lost"] is None
    assert rows[0]["corrected_policy"] == "observed_only"
    assert diagnostics["series"][0]["estimated_lost_total"] is None
    assert diagnostics["logs_available"] is False
    _, known = recover_daily([event(1, 0)], [], "2026-01-01", "2026-01-03")
    assert known["logs_available"] is True


def test_known_covered_scope_with_no_events_returns_real_zero_history():
    rows, diagnostics = recover_daily([], [], "2026-01-01", "2026-01-04", scopes=[{"sku_id": "00123", "warehouse_id": "almaty"}])
    assert len(rows) == 3
    assert all(row["corrected_demand"] == 0 for row in rows)
    assert diagnostics["series"][0]["observed_regular_total"] == 0


def test_future_sales_and_intervals_do_not_change_recovery():
    events = [event(1, 10), event(2, 10)]
    old = recover_daily(events, [], "2026-01-01", "2026-01-04")
    new = recover_daily(events + [event(6, 99999)], [interval("2026-01-07", "2026-01-08")], "2026-01-01", "2026-01-04")
    assert old == new


@pytest.mark.parametrize("fraction", [-0.1, 1.01, float("nan")])
def test_invalid_fraction_rejected(fraction):
    with pytest.raises(ValueError, match="unavailable_fraction"):
        recover_daily([event(1, 10)], [interval("2026-01-02", "2026-01-03", fraction)], "2026-01-01", "2026-01-04")
