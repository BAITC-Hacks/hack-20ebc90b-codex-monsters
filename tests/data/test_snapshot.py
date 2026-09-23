"""Independent minimal examples; no organizer records are embedded in tests."""
from copy import deepcopy
from decimal import Decimal

import pytest
from openpyxl import Workbook

from ekt.data.artifacts import read_table, write_records
from ekt.data.errors import DataError
from ekt.data.snapshot import build_snapshot_payload
from ekt.data.sources import profile_source, source_records

AS_OF = "2026-09-22T23:59:59+06:00"


def minimal(tmp_path, *, provenance="observed"):
    manifest = {"as_of": AS_OF, "mode": "real_preview", "artifact_root": str(tmp_path / "artifacts"), "sources": [
        {"source_id": "master", "kind": "sku_master", "provenance": provenance, "rows": [
            {"sku_id": "00123", "name": "Synthetic test switch", "supplier_id": "test-systeme", "base_uom": "piece",
             "purchase_uom": "pack", "base_units_per_purchase_uom": "10", "quantity_quantum": "1"}]},
        {"source_id": "sales", "kind": "sales_events", "provenance": provenance,
         "coverage": {"start": "2026-09-01", "end": "2026-09-23", "complete": True,
                      "sku_ids": ["00123"], "warehouse_ids": ["test-warehouse"]}, "rows": [
            {"event_id": "event-1", "sku_id": "00123", "warehouse_id": "test-warehouse", "event_at": "2026-09-21T12:00:00+06:00",
             "quantity_base": "12.50", "event_type": "shipment", "demand_effect": "increase"}]},
        {"source_id": "inventory", "kind": "inventory_snapshots", "provenance": provenance, "rows": [
            {"sku_id": "00123", "warehouse_id": "test-warehouse", "as_of": AS_OF, "accounting_definition_version": "confirmed-fixture-v1",
             "on_hand_base": "20", "reserved_base": "2", "blocked_base": "0", "free_base": "18"}]},
        {"source_id": "terms", "kind": "supplier_terms", "provenance": provenance, "rows": [
            {"sku_id": "00123", "supplier_id": "test-systeme", "warehouse_id": "test-warehouse", "valid_at": AS_OF,
             "moq_purchase": "2", "pack_multiple_purchase": "2", "lead_time_days": 7, "review_days": 7}]},
    ]}
    mapping = {"version": "test-v1", "sources": {"inventory": {"current_stock_verified": True}}}
    return manifest, mapping


def test_leading_zero_decimal_replay_and_completed_refs(tmp_path):
    manifest, mapping = minimal(tmp_path)
    first = build_snapshot_payload(manifest, mapping)
    second = build_snapshot_payload(manifest, mapping)
    assert second == first
    assert read_table(first["tables"]["sku_master"])[0]["sku_id"] == "00123"
    sales = read_table(first["tables"]["sales_events"])
    assert len(sales) == 1
    assert sales[0]["quantity_base"] == Decimal("12.50")
    assert isinstance(sales[0]["quantity_base"], Decimal)
    assert first["quality"]["capabilities"]["can_plan"]
    assert any(item["field"] == "demand_coverage" for item in first["assumptions"])


def test_bad_header_stops_before_publication(tmp_path):
    path = tmp_path / "sales.csv"
    path.write_text("wrong,quantity\n00123,4\n")
    manifest = {"as_of": AS_OF, "mode": "real_preview", "artifact_root": str(tmp_path / "artifacts"),
                "sources": [{"source_id": "S08", "kind": "sales_events", "path": str(path)}]}
    mapping = {"version": "1", "sources": {"S08": {"columns": {"sku_id": "SKU", "quantity_base": "quantity"}}}}
    with pytest.raises(DataError, match="missing headers"):
        build_snapshot_payload(manifest, mapping)
    assert not (tmp_path / "artifacts").exists()


def test_unknown_signed_movements_are_quarantined(tmp_path):
    manifest, mapping = minimal(tmp_path)
    sale = manifest["sources"][1]["rows"][0]
    sale["event_type"] = "shipment"
    manifest["sources"][1]["rows"] += [dict(sale, event_id="order", event_type="customer-order", quantity_base="8"),
                                       dict(sale, event_id="negative", quantity_base="-4")]
    mapping["sources"]["sales"] = {"movements": {"shipment": {"sign": "positive", "demand_effect": "increase", "event_type": "shipment"}}}
    result = build_snapshot_payload(manifest, mapping)
    assert len(read_table(result["tables"]["sales_events"])) == 1
    assert result["quality"]["rejected_rows"] == 2
    assert any(item["code"] == "UNSUPPORTED_SIGNED_MOVEMENT" for item in result["quality"]["issues"])


def test_confirmed_negative_return_keeps_sign_and_effect(tmp_path):
    manifest, mapping = minimal(tmp_path)
    manifest["sources"][1]["rows"][0].update(quantity_base="-3", event_type="return")
    mapping["sources"]["sales"] = {"movements": {"return": {"sign": "negative", "demand_effect": "decrease", "event_type": "return"}}}
    row = read_table(build_snapshot_payload(manifest, mapping)["tables"]["sales_events"])[0]
    assert row["quantity_base"] == Decimal("-3")
    assert row["raw_quantity"] == Decimal("-3")
    assert row["demand_effect"] == "decrease"


def test_missing_moq_is_unknown_and_does_not_hide_other_subset(tmp_path):
    manifest, mapping = minimal(tmp_path)
    for source in manifest["sources"]:
        second = deepcopy(source["rows"][0])
        second["sku_id"] = "blocked"
        if "event_id" in second:
            second["event_id"] = "event-blocked"
        if source["kind"] == "supplier_terms":
            second.pop("moq_purchase")
        source["rows"].append(second)
    result = build_snapshot_payload(manifest, mapping)
    terms = {row["sku_id"]: row for row in read_table(result["tables"]["supplier_terms"])}
    assert terms["blocked"]["moq_purchase"] is None
    assert result["quality"]["status"] == "degraded"
    assert result["quality"]["capabilities"]["can_plan"] is True
    assert any(issue["scope_ids"] == ["blocked"] and "moq_purchase" in issue["message"] for issue in result["quality"]["issues"])


def test_unknown_coil_mapping_blocks_without_changing_master(tmp_path):
    manifest, mapping = minimal(tmp_path)
    master = manifest["sources"][0]["rows"][0]
    master.update(base_uom="metre", purchase_uom="coil", base_units_per_purchase_uom=None)
    result = build_snapshot_payload(manifest, mapping)
    assert not result["quality"]["capabilities"]["can_plan"]
    assert read_table(result["tables"]["sku_master"])[0]["base_units_per_purchase_uom"] is None


def test_unverified_stock_and_opening_balances_never_current(tmp_path):
    manifest, mapping = minimal(tmp_path)
    mapping["sources"]["inventory"] = {}
    manifest["sources"].append({"source_id": "opening", "kind": "monthly_balances", "rows": [
        {"sku_id": "00123", "period_start": "2026-09-01", "period_end": "2026-10-01", "measure": "opening_balance", "value": "400", "coverage": "partial"}]})
    result = build_snapshot_payload(manifest, mapping)
    assert read_table(result["tables"]["inventory_snapshots"]) == []
    assert len(read_table(result["tables"]["monthly_balances"])) == 1
    assert not result["quality"]["capabilities"]["can_plan"]
    assert not result["quality"]["capabilities"]["observed_stockouts_available"]


def test_synthetic_input_taints_result(tmp_path):
    manifest, mapping = minimal(tmp_path)
    manifest["sources"][2]["provenance"] = "synthetic"
    assert build_snapshot_payload(manifest, mapping)["mode"] == "synthetic_demo"


def test_distinct_document_events_not_similarity_deduplicated(tmp_path):
    manifest, mapping = minimal(tmp_path)
    original = manifest["sources"][1]["rows"][0]
    manifest["sources"][1]["rows"].append(dict(original, event_id="event-2", doc_id="document-2"))
    result = build_snapshot_payload(manifest, mapping)
    assert len(read_table(result["tables"]["sales_events"])) == 2


def test_checksums_detect_corruption_and_path_is_immutable(tmp_path):
    path = tmp_path / "records.parquet"
    ref = write_records(path, [{"quantity": Decimal("1.25")}])
    with pytest.raises(DataError, match="different contents"):
        write_records(path, [{"quantity": Decimal("2.50")}])
    path.write_bytes(path.read_bytes() + b"tampered")
    with pytest.raises(DataError, match="checksum"):
        read_table(ref)


def test_excel_text_and_zero_format_preserve_ids(tmp_path):
    path = tmp_path / "fixture.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Rows"
    sheet.append(["SKU", "Qty"])
    sheet.append(["00123", 5])
    sheet.append([123, 2])
    sheet["A3"].number_format = "00000"
    workbook.save(path)
    rows, _, _ = source_records({"source_id": "fixture", "path": str(path), "sheet": "Rows"}, {"columns": {"sku_id": "SKU"}})
    assert [row["SKU"] for _, row in rows] == ["00123", "00123"]


def test_numeric_sku_without_format_is_rejected(tmp_path):
    manifest, mapping = minimal(tmp_path)
    manifest["sources"][0]["rows"][0]["sku_id"] = 123
    result = build_snapshot_payload(manifest, mapping)
    assert not read_table(result["tables"]["sku_master"])
    assert any(issue["code"] == "NON_TEXT_SKU" for issue in result["quality"]["issues"])


def test_authority_skips_overlapping_sales_and_monthly_is_control(tmp_path):
    manifest, mapping = minimal(tmp_path)
    duplicate = deepcopy(manifest["sources"][1])
    duplicate["source_id"] = "overlap"
    manifest["sources"].append(duplicate)
    with pytest.raises(DataError, match="authoritative_sales_sources"):
        build_snapshot_payload(manifest, mapping)
    mapping["authoritative_sales_sources"] = ["sales"]
    result = build_snapshot_payload(manifest, mapping)
    assert len(read_table(result["tables"]["sales_events"])) == 1


def test_read_only_profile_does_not_expose_rows(tmp_path):
    manifest, _ = minimal(tmp_path)
    profile = profile_source(manifest["sources"][1], {})
    assert profile["quantity_sign_counts"] == {"positive": 1}
    assert "00123" not in str(profile)


def test_future_records_excluded_at_as_of(tmp_path):
    manifest, mapping = minimal(tmp_path)
    original = manifest["sources"][1]["rows"][0]
    manifest["sources"][1]["rows"].append(dict(original, event_id="future", event_at="2026-10-01T12:00:00+06:00", quantity_base="999"))
    result = build_snapshot_payload(manifest, mapping)
    assert len(read_table(result["tables"]["sales_events"])) == 1


def test_duplicate_moq_is_not_arbitrary_last_value(tmp_path):
    manifest, mapping = minimal(tmp_path)
    original = manifest["sources"][3]["rows"][0]
    manifest["sources"][3]["rows"].append(dict(original, moq_purchase="3"))
    result = build_snapshot_payload(manifest, mapping)
    assert not read_table(result["tables"]["supplier_terms"])
    assert any(issue["code"] == "DUPLICATE_CANONICAL_KEY" for issue in result["quality"]["issues"])


@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "1e38"])
def test_invalid_or_out_of_range_quantity_is_quarantined(tmp_path, invalid):
    manifest, mapping = minimal(tmp_path)
    manifest["sources"][1]["rows"][0]["quantity_base"] = invalid
    result = build_snapshot_payload(manifest, mapping)
    assert result["tables"]["sales_events"]["row_count"] == 0
    assert result["quality"]["rejected_rows"] == 1


def test_budget_unavailable_with_unknown_currency(tmp_path):
    manifest, mapping = minimal(tmp_path)
    manifest["sources"][3]["rows"][0].update(cost_per_base="0.10", currency="")
    result = build_snapshot_payload(manifest, mapping)
    assert not result["quality"]["capabilities"]["budget_available"]
    assert read_table(result["tables"]["supplier_terms"])[0]["cost_per_base"] == Decimal("0.10")
