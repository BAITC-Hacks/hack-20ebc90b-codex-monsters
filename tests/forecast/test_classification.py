from datetime import date, timedelta
from decimal import Decimal

import pytest

from ekt.forecast.classification import classify_events
from ekt.forecast.baseline import forecast_daily
from ekt.forecast.recovery import recover_daily


def event(day, quantity=10, **extra):
    result = {"event_id": f"e-{day}", "event_at": (date(2026, 1, 1) + timedelta(days=day)).isoformat(),
              "sku_id": "00123", "warehouse_id": "almaty", "quantity_base": str(quantity),
              "demand_effect": "increase", "doc_id": f"doc-{day}"}
    result.update(extra)
    return result


def test_blind_one_off_spike_is_pending_with_exact_quantity_conservation():
    source = [event(day, 1000 if day == 40 else 10) for day in range(60)]
    results = classify_events(source)
    spike = results[40]
    assert spike["label"] == "suspected_project"
    assert spike["review_status"] == "pending"
    assert spike["regular_qty"] == 0
    assert spike["uncertain_qty"] == 1000
    assert source[40].get("label") is None
    for row in results:
        assert row["observed_qty"] == row["regular_qty"] + row["project_qty"] + row["uncertain_qty"]


@pytest.mark.parametrize("large_days", [{7, 21, 35, 49}, set(range(50, 60))])
def test_repeated_large_purchases_and_sustained_shift_are_not_projects(large_days):
    rows = classify_events([event(day, 1000 if day in large_days else 10) for day in range(60)])
    assert all(rows[day]["label"] == "regular" for day in large_days)


def test_oracle_fields_never_affect_classification():
    rows = [event(day, 1000 if day == 9 else 10) for day in range(20)]
    altered = [dict(row, is_project=True, expected_class="project", truth=100) for row in rows]
    first, second = classify_events(rows), classify_events(altered)
    assert [r["label"] for r in first] == [r["label"] for r in second]
    assert [r["reason_codes"] for r in first] == [r["reason_codes"] for r in second]


def test_sign_semantics_no_demand_and_overrides():
    rows = [event(day) for day in range(8)] + [event(8, "-2.75", demand_effect="decrease"), event(9, -100, demand_effect="none")]
    result = classify_events(rows, [{"event_ids": ["e-1"], "label": "project", "reason": "Buyer confirmed"}])
    assert result[1]["project_qty"] == 10
    assert result[1]["review_status"] == "confirmed"
    assert result[8]["regular_qty"] == Decimal("-2.75")
    assert result[9]["observed_qty"] == 0
    assert result[9]["label"] == "non_demand"
    with pytest.raises(ValueError, match="quarantined"):
        classify_events([event(0, demand_effect="unknown")])


def test_short_history_and_category_fallback_are_explicit():
    assert classify_events([event(0)])[0]["label"] == "uncertain"
    rows = [event(day, sku_id="other", category_id="switch", base_uom="piece") for day in range(6)]
    rows.append(event(9, 1000, category_id="switch", base_uom="piece"))
    last = classify_events(rows)[-1]
    assert last["label"] == "suspected_project"
    assert "category_uom_fallback" in last["reason_codes"]


def test_same_day_split_documents_do_not_establish_recurrence():
    rows = [event(day) for day in range(20)]
    rows.extend([event(25, 1000, event_id=f"large-{i}", doc_id=f"large-doc-{i}") for i in range(3)])
    assert all(row["label"] == "suspected_project" for row in classify_events(rows)[-3:])


def test_two_large_purchases_remain_uncertain_and_visible():
    rows = classify_events([event(day, 1000 if day in {5, 15} else 10) for day in range(25)])
    assert rows[5]["label"] == "uncertain"
    assert rows[5]["uncertain_qty"] == 1000


def test_recurrence_compares_amount_ranges_and_distinct_days():
    rows = [event(day) for day in range(40)]
    rows += [event(50, 1000), event(51, 600), event(52, 1500), event(60, 10000)]
    result = classify_events(rows)
    assert result[-4]["label"] == "regular"
    assert result[-3]["label"] == "uncertain"  # 1500 is outside 2x600.
    assert result[-2]["label"] == "uncertain"  # 600 is outside 0.5x1500.
    assert result[-1]["label"] == "suspected_project"


def test_many_distinct_same_day_large_lines_still_count_as_one_occurrence():
    rows = [event(day) for day in range(500)]
    rows += [event(1000, 1000 + index, event_id=f"split-{index}") for index in range(100)]
    result = classify_events(rows)
    assert all(row["label"] == "suspected_project" for row in result[-100:])


def test_document_line_splitting_preserves_allocations_recovery_and_forecast():
    regular = [event(day) for day in range(56)]
    single = classify_events(regular + [event(55, 1000, event_id="large", doc_id="large-order")])
    split = classify_events(regular + [event(55, 10, event_id=f"large-{index}", doc_id="large-order")
                                       for index in range(100)])
    for allocation in ("observed_qty", "regular_qty", "project_qty", "uncertain_qty"):
        assert sum(row[allocation] for row in split) == sum(row[allocation] for row in single)
    assert all(row["label"] == "suspected_project" for row in split[-100:])
    assert all("document_lines_aggregated" in row["reason_codes"] for row in split[-100:])
    assert len({row["event_id"] for row in split}) == 156
    first = date(2026, 1, 1)
    origin = first + timedelta(days=56)
    single_history, _ = recover_daily(single, [], first, origin)
    split_history, _ = recover_daily(split, [], first, origin)
    assert single_history == split_history
    horizon = [origin + timedelta(days=index) for index in range(90)]
    single_forecast = forecast_daily(single_history, horizon)
    assert forecast_daily(split_history, horizon) == single_forecast
    assert single_forecast["daily"][0]["mean"] == 10
    assert single_forecast["daily_residual_std"] == 0


def test_repeated_large_split_documents_stay_regular_on_distinct_dates():
    regular = [event(day) for day in range(56)]
    # The same document ID can occur on different dates; those occurrences must
    # remain distinct size observations and establish recurrence.
    large = [event(day, 10, event_id=f"large-{day}-{index}", doc_id="repeated-order")
             for day in (7, 21, 35) for index in range(100)]
    classified = classify_events(regular + large)
    assert all(row["label"] == "regular" for row in classified[-300:])
    assert all("recurrent_large_demand_or_level_shift" in row["reason_codes"] for row in classified[-300:])
    assert sum(row["regular_qty"] for row in classified) == 3560


def test_document_grouping_keeps_sku_and_warehouse_scopes_separate():
    scopes = [("00123", "almaty"), ("other", "almaty"), ("00123", "astana")]
    regular = [event(day, event_id=f"{sku}-{warehouse}-{day}", sku_id=sku, warehouse_id=warehouse)
               for sku, warehouse in scopes for day in range(20)]
    large = [event(25, 500, event_id=f"large-{index}", doc_id="shared-document") for index in range(2)]
    controls = [event(25, event_id=f"control-{index}", doc_id="shared-document", sku_id=sku, warehouse_id=warehouse)
                for index, (sku, warehouse) in enumerate(scopes[1:])]
    result = {row["event_id"]: row for row in classify_events(regular + large + controls)}
    assert result["large-0"]["label"] == result["large-1"]["label"] == "suspected_project"
    assert result["control-0"]["label"] == result["control-1"]["label"] == "regular"
    assert "document_lines_aggregated" not in result["control-0"]["reason_codes"]
    assert "document_lines_aggregated" not in result["control-1"]["reason_codes"]


def test_missing_document_identity_does_not_merge_independent_rows():
    source = [event(day) for day in range(56)] + [
        event(55, 10, event_id=f"independent-{index}", doc_id=None) for index in range(100)
    ]
    result = classify_events(source)
    assert all(row["label"] == "regular" for row in result)
    assert not any("document_lines_aggregated" in row["reason_codes"] for row in result)


def test_document_returns_and_row_overrides_keep_signed_exact_allocations():
    regular = [event(day) for day in range(56)]
    split = [event(55, 100, event_id=f"large-{index}", doc_id="large-order") for index in range(10)]
    movements = [
        event(55, -50, event_id="return", doc_id="large-order", demand_effect="decrease"),
        event(55, -10, event_id="project-return", doc_id="large-order", demand_effect="decrease"),
        event(55, 999, event_id="transfer", doc_id="large-order", demand_effect="none"),
    ]
    overrides = [
        {"event_ids": ["large-0"], "label": "regular", "reason": "Only this line is regular"},
        {"event_ids": ["large-1", "project-return"], "label": "project", "reason": "Confirmed project lines"},
    ]
    rows = classify_events(regular + split + movements, overrides)
    result = {row["event_id"]: row for row in rows}
    assert result["large-0"]["regular_qty"] == 100
    assert result["large-1"]["project_qty"] == 100
    assert result["project-return"]["project_qty"] == -10
    assert result["return"]["regular_qty"] == -50
    assert result["transfer"]["observed_qty"] == 0
    assert all(result[f"large-{index}"]["label"] == "suspected_project" for index in range(2, 10))
    assert sum(row["regular_qty"] for row in rows) == 610
    assert sum(row["project_qty"] for row in rows) == 90
    assert sum(row["uncertain_qty"] for row in rows) == 800
    for row in rows:
        assert row["regular_qty"] + row["project_qty"] + row["uncertain_qty"] == row["observed_qty"]


def test_document_grouping_and_recurrence_share_the_utc_calendar():
    regular = [event(day) for day in range(56)]
    split = [
        event(55, 500, event_id="offset-line", doc_id="large-order", event_at="2026-02-26T01:00:00+05:00"),
        event(55, 500, event_id="utc-line", doc_id="large-order", event_at="2026-02-25T22:00:00Z"),
    ]
    result = classify_events(regular + split)
    assert all(row["label"] == "suspected_project" for row in result[-2:])
    assert all("document_lines_aggregated" in row["reason_codes"] for row in result[-2:])


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_input_rejected(quantity):
    with pytest.raises(ValueError, match="finite"):
        classify_events([event(0, quantity)])
