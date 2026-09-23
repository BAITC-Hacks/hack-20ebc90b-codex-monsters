from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from ekt.contracts import DomainError, InventorySnapshot, SalesEvent, SkuMaster, SupplierTerms, load_table
from ekt.demo import create_demo_snapshot, fixture_forecast


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory):
    return create_demo_snapshot(tmp_path_factory.mktemp("demo"))


def test_fixture_is_canonical_and_contains_no_detector_truth(snapshot):
    skus = [SkuMaster.model_validate(row) for row in load_table(snapshot, "sku_master")]
    sales = [SalesEvent.model_validate(row) for row in load_table(snapshot, "sales_events")]
    stocks = [InventorySnapshot.model_validate(row) for row in load_table(snapshot, "inventory_snapshots")]
    terms = [SupplierTerms.model_validate(row) for row in load_table(snapshot, "supplier_terms")]
    assert len(skus) == len(stocks) == len(terms) == 16
    assert len({sku.supplier_id for sku in skus}) == 2
    assert min(event.event_at for event in sales).date().isoformat() == "2024-09-23"
    assert max(event.event_at for event in sales) < snapshot.as_of
    assert all(event.provenance == "synthetic" for event in sales)
    assert len({event.event_id for event in sales}) == len(sales)
    assert "label" not in load_table(snapshot, "sales_events")[0]
    assert "ground_truth" not in snapshot.model_dump_json()


def test_independent_fixture_oracle_is_test_only(snapshot):
    sales = {row["event_id"]: row for row in load_table(snapshot, "sales_events")}
    # This oracle is deliberately confined to tests; the detector receives only
    # ordinary canonical fields (customer, document, quantity and date).
    assert sales["event-extra-0001"]["sku_id"] == "DEMO-003"
    assert Decimal(sales["event-extra-0001"]["quantity_base"]) == 600
    terms = {row["sku_id"]: row for row in load_table(snapshot, "supplier_terms")}
    assert terms["DEMO-016"]["moq_purchase"] is None
    cable = next(row for row in load_table(snapshot, "sku_master") if row["sku_id"] == "DEMO-006")
    assert Decimal(cable["base_units_per_purchase_uom"]) == 100
    assert cable["base_uom"] == "m" and cable["purchase_uom"] == "coil"
    stockout = load_table(snapshot, "stockout_intervals")[0]
    begin, end = datetime.fromisoformat(stockout["start_at"]), datetime.fromisoformat(stockout["end_at"])
    assert not [row for row in sales.values() if row["sku_id"] == stockout["sku_id"]
                and begin <= datetime.fromisoformat(row["event_at"]) < end]


def test_fixture_forecast_is_explicitly_incomplete_and_never_accepts_real_data(snapshot):
    forecast = fixture_forecast(snapshot)
    assert forecast.model_version == "fixture-v1"
    assert len(forecast.series) == 16
    assert all(len(series.daily) == 90 for series in forecast.series)
    expected_dates = [snapshot.as_of.date() + timedelta(days=offset) for offset in range(1, 91)]
    assert all([day.date for day in series.daily] == expected_dates for series in forecast.series)
    assert "FORECAST_PROVIDER_NOT_CONNECTED" in {issue.code for issue in forecast.quality.issues}
    assert all(series.uncertainty.calibration == "unvalidated" for series in forecast.series)
    with pytest.raises(DomainError, match="synthetic_demo"):
        fixture_forecast(snapshot.model_copy(update={"mode": "real_preview"}))


def test_snapshot_checksums_detect_modifications(tmp_path):
    from ekt.contracts import write_table
    snapshot = create_demo_snapshot(tmp_path)
    path = tmp_path / snapshot.snapshot_id
    write_table(path, "inventory_snapshots", [{"corrupt": "changed"}])
    with pytest.raises(DomainError) as error:
        load_table(snapshot, "inventory_snapshots")
    assert error.value.code == "ARTIFACT_CHANGED"
