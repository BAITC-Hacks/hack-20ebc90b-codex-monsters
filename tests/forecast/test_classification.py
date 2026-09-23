from datetime import date, timedelta
from decimal import Decimal

import pytest

from ekt.forecast.classification import classify_events


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


@pytest.mark.parametrize("quantity", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_input_rejected(quantity):
    with pytest.raises(ValueError, match="finite"):
        classify_events([event(0, quantity)])
