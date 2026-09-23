from openpyxl import Workbook

from ekt.data.artifacts import read_table
from ekt.data.presets import systeme_preview_manifest
from ekt.data.snapshot import build_snapshot_payload
from ekt.data.sources import profile_source


def workbook(path, headers, rows, sheet="Лист_1"):
    book = Workbook()
    page = book.active
    page.title = sheet
    page.append(headers)
    for row in rows:
        page.append(row)
    book.save(path)


def test_portable_systeme_preset_imports_observed_subset_without_certifying_gaps(tmp_path):
    source_root = tmp_path / "sources"
    systeme = source_root / "Systeme electric"
    systeme.mkdir(parents=True)
    iek = source_root / "IEK"
    iek.mkdir()
    workbook(systeme / "Динамика test.xlsx",
             ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"], [
                 ["01.09.2026 12:00:00", "doc-1", "Расходная накладная doc-1", "00123", "Independent test product", "шт", "Алматы", 4],
                 ["05.09.2026 12:00:00", "doc-2", "Расходная накладная doc-2", "00123", "Independent test product", "шт", "Алматы", 6],
                 ["09.09.2026 12:00:00", "doc-3", "Расходная накладная doc-3", "00123", "Independent test product", "шт", "Алматы", -2],
                 ["10.09.2026 12:00:00", "doc-4", "Заказ клиента doc-4", "00123", "Independent test product", "шт", "Алматы", 50],
             ])
    workbook(systeme / "MOQ test.xlsx", ["№", "Номенклатура", "Номенклатура.Код", "Артикул", "Кратность"],
             [[1, "Independent test product", "00123", "test-article", 5]])
    workbook(systeme / "Товар в пути test.xlsx", ["unverified"], [["stock candidate"]])
    workbook(iek / "MOQ test.xlsx", ["unverified"], [["IEK metadata"]])
    manifest, mapping = systeme_preview_manifest(source_root, tmp_path / "artifacts", "2026-09-22T23:59:59+05:00")
    result = build_snapshot_payload(manifest, mapping)
    assert result["mode"] == "real_preview"
    import json
    coverage = next(item for item in result["assumptions"] if item["field"] == "demand_coverage")
    assert json.loads(coverage["value"])["complete"] is False
    sales = read_table(result["tables"]["sales_events"])
    assert len(sales) == 2
    assert all(row["sku_id"] == "00123" for row in sales)
    assert all(row["event_at"].endswith("+05:00") for row in sales)
    assert result["tables"]["quarantine"]["row_count"] == 2
    assert result["tables"]["inventory_snapshots"]["row_count"] == 0
    terms = read_table(result["tables"]["supplier_terms"])[0]
    assert terms["pack_multiple_purchase"] == 5
    assert terms["moq_purchase"] is None
    assert not result["quality"]["capabilities"]["can_plan"]
    assert {ref["source_id"] for ref in result["source_refs"]} >= {"S01", "S07", "S08", "S12"}
    profile = profile_source(manifest["sources"][1], mapping["sources"]["S08"])
    assert profile["date_min"].startswith("2026-09-01")
    assert profile["quantity_sign_counts"] == {"positive": 3, "negative": 1}
    assert "doc-1" not in str(profile)
    for source in manifest["sources"]:
        profile_source(source, mapping["sources"].get(source["source_id"], {}))
