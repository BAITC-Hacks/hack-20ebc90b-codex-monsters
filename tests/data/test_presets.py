from openpyxl import Workbook
import pytest

from ekt.data.artifacts import read_table
from ekt.data.presets import iek_preview_manifest, systeme_preview_manifest
from ekt.data.presets import _single
from ekt.data.errors import DataError
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


def test_discovery_accepts_macroman_unzip_filename_without_renaming(tmp_path):
    decoded = "Динамика продаж.xlsx"
    original = tmp_path / decoded.encode("cp866").decode("mac_roman")
    original.touch()
    assert _single(tmp_path, "Динамика*") == original
    assert original.exists()
    (tmp_path / decoded).touch()
    with pytest.raises(DataError, match="Expected one workbook"):
        _single(tmp_path, "Динамика*")


def test_preset_selection_does_not_use_later_same_day_shipments(tmp_path):
    systeme = tmp_path / "Systeme electric"
    systeme.mkdir()
    workbook(systeme / "Динамика test.xlsx",
             ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"], [
                 ["01.09.2026 09:00:00", "d1", "Расходная накладная d1", "001", "Visible", "шт", "Алматы", 4],
                 ["01.09.2026 18:00:00", "d2", "Расходная накладная d2", "002", "Future", "шт", "Алматы", 6],
                 ["01.09.2026 18:01:00", "d3", "Расходная накладная d3", "002", "Future", "шт", "Алматы", 6],
             ])
    workbook(systeme / "MOQ test.xlsx", ["Номенклатура.Код", "Кратность"], [["001", 5]])
    workbook(systeme / "Товар в пути test.xlsx", ["unverified"], [["stock candidate"]])
    manifest, mapping = systeme_preview_manifest(tmp_path, tmp_path / "artifacts", "2026-09-01T12:00:00+05:00", sku_limit=1)
    assert mapping["sources"]["S08"]["sku_filter"] == ["001"]
    assert manifest["sources"][0]["rows"][0]["name"] == "Visible"


def test_iek_preview_preserves_units_and_does_not_guess_purchase_or_pipeline(tmp_path):
    directory = tmp_path / "IEK"
    directory.mkdir()
    headers = ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"]
    rows = [
        ["01.09.2026 12:00:00", f"doc-{sku}", f"Расходная накладная doc-{sku}", sku, "Invented product", unit, "Алматы", 10]
        for sku, unit in (("001", "шт"), ("002", "м"), ("003", "упак"))
    ]
    rows += [
        ["02.09.2026 12:00:00", "return", "Расходная накладная return", "002", "Invented cable", "м", "Алматы", -3],
        ["03.09.2026 12:00:00", "receipt", "Поступление товаров receipt", "001", "Invented product", "шт", "Алматы", 30],
        ["22.09.2026 18:00:00", "future", "Расходная накладная future", "001", "Future name", "упак", "Алматы", 999],
    ]
    workbook(directory / "Динамика test.xlsx", headers, rows)
    workbook(directory / "MOQ test.xlsx", ["Код 1с", "Мин. разр. к отгр."], [["002", "#N/A"]], sheet="Лист7")
    workbook(directory / "Путь test.xlsx", ["Код 1с", "arrival deadline"], [["002", 10]], sheet="Лист4")
    manifest, mapping = iek_preview_manifest(tmp_path, tmp_path / "output", "2026-09-22T12:00:00+05:00", sku_limit=100)
    snapshot = build_snapshot_payload(manifest, mapping)
    masters = {row["sku_id"]: row for row in read_table(snapshot["tables"]["sku_master"])}
    assert {sku: row["base_uom"] for sku, row in masters.items()} == {"001": "шт", "002": "м", "003": "упак"}
    assert all(row["supplier_id"] == "iek" for row in masters.values())
    assert all(row["base_units_per_purchase_uom"] is None and row["purchase_uom"] is None for row in masters.values())
    assert masters["001"]["name"] == "Invented product"
    assert snapshot["tables"]["sales_events"]["row_count"] == 3
    assert snapshot["tables"]["quarantine"]["row_count"] == 2
    assert snapshot["tables"]["supplier_terms"]["row_count"] == 0
    assert snapshot["tables"]["pipeline_lines"]["row_count"] == 0
    assert snapshot["mapping_version"] == "iek-preview-v1"
    assert snapshot["mode"] == "real_preview"


def test_iek_named_preset_uses_only_registered_files(tmp_path, monkeypatch):
    from datetime import datetime
    from ekt.contracts import MappingConfig, SourceManifest
    from ekt.data import build_snapshot
    from ekt.data.artifacts import checksum
    from ekt.data import presets

    files = {"S02": tmp_path / "history.xlsx", "S01": tmp_path / "terms.xlsx", "S05": tmp_path / "incoming.xlsx"}
    workbook(files["S02"], ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"], [
        ["01.09.2026 12:00:00", "d1", "Расходная накладная d1", "001", "Invented cable", "м", "Алматы", 10],
    ])
    workbook(files["S01"], ["unverified"], [["reference"]])
    workbook(files["S05"], ["unverified"], [["reference"]])
    refs = [{"source_id": sid, "kind": {"S01": "supplier_terms", "S02": "sales_events", "S05": "pipeline_lines"}[sid],
             "local_ref": str(path), "checksum": checksum(path)} for sid, path in files.items()]
    def prohibit_discovery(*_args):
        raise AssertionError("No directory discovery is allowed for registered paths")
    monkeypatch.setattr(presets, "_single", prohibit_discovery)
    result = build_snapshot(SourceManifest(source_refs=refs, source_ids=list(files), as_of=datetime.fromisoformat("2026-09-22T23:59:59+05:00")),
                            MappingConfig(mapping_version="iek-preview-v1", options={"output_root": str(tmp_path / "output"), "sku_limit": 100}))
    assert result.tables["sku_master"].row_count == 1
    assert result.mapping_version == "iek-preview-v1"
    assert {ref.source_id for ref in result.source_refs} == {"S01", "S02", "S02-master", "S05"}
    assert not result.quality.capabilities.can_plan


def test_iek_inconsistent_source_units_are_not_combined(tmp_path):
    directory = tmp_path / "IEK"
    directory.mkdir()
    workbook(directory / "Динамика test.xlsx", ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"], [
        ["01.09.2026 12:00:00", "d1", "Расходная накладная d1", "001", "Invented cable", "м", "Алматы", 10],
        ["02.09.2026 12:00:00", "d2", "Расходная накладная d2", "001", "Invented cable", "упак", "Алматы", 10],
    ])
    workbook(directory / "MOQ test.xlsx", ["unverified"], [["reference"]])
    workbook(directory / "Путь test.xlsx", ["unverified"], [["reference"]])
    with pytest.raises(DataError) as caught:
        iek_preview_manifest(tmp_path, tmp_path / "output", "2026-09-22T23:59:59+05:00")
    assert caught.value.code == "NO_SUPPORTED_SUBSET"
