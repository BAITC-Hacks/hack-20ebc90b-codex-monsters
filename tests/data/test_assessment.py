"""Aggregate evidence keeps unknowns/denominators honest and private rows local."""

import json

import pytest
from openpyxl import Workbook

from ekt.data.assessment import build_assessment, main, render_markdown, summarize_sales_records
from ekt.data.errors import DataError


def _workbook(path, headers, rows):
    book = Workbook()
    page = book.active
    page.title = "Лист_1"
    page.append(headers)
    for row in rows:
        page.append(row)
    book.save(path)


@pytest.fixture
def sources(tmp_path):
    root = tmp_path / "private-sources"
    systeme = root / "Systeme electric"
    systeme.mkdir(parents=True)
    iek = root / "IEK"
    iek.mkdir()
    # Most frequent SKU is selected; a negative correction and unknown quantity
    # remain evidence but cannot silently turn into extra customer consumption.
    _workbook(systeme / "Динамика fixture.xlsx",
              ["Дата", "Номер", "Документ", "Код", "Номенклатура", "Ед.", "Склад", "Количество"], [
                  ["01.09.2026 12:00:00", "private-doc-a", "Расходная накладная private-doc-a", "private-sku-a", "private-name", "шт", "Алматы", 4],
                  ["02.09.2026 12:00:00", "private-doc-b", "Расходная накладная private-doc-b", "private-sku-a", "private-name", "шт", "Алматы", 5],
                  ["03.09.2026 12:00:00", "private-doc-c", "Расходная накладная private-doc-c", "private-sku-a", "private-name", "шт", "Алматы", -2],
                  ["04.09.2026 12:00:00", "private-doc-d", "Расходная накладная private-doc-d", "private-sku-a", "private-name", "шт", "Алматы", None],
                  ["04.09.2026 12:00:00", "private-doc-e", "Расходная накладная private-doc-e", "private-sku-b", "private-name", "шт", "Алматы", 10],
                  [None, None, "summary", None, None, None, None, 17],
              ])
    _workbook(systeme / "MOQ fixture.xlsx", ["Номенклатура.Код", "Кратность"],
              [["private-sku-a", 5], ["private-sku-b", 1]])
    _workbook(systeme / "Товар в пути fixture.xlsx", ["unverified"], [["private-stock"]])
    _workbook(iek / "MOQ fixture.xlsx", ["unverified"], [["private-iek"]])
    return root


def test_keyed_signs_exclude_summary_and_retain_missing_invalid_and_zero():
    columns = {name: name for name in ("sku_id", "quantity_base", "base_uom", "event_at")}
    values = [("a", 4), ("a", -2), ("a", None), ("b", 0), ("b", "#N/A"), (None, 100)]
    rows = [(str(n), {"sku_id": sku, "quantity_base": quantity, "base_uom": "шт", "event_at": "2026-09-01"}) for n, (sku, quantity) in enumerate(values)]
    report = summarize_sales_records(rows, columns, selected_skus=["a"])
    assert report["keyed_rows"] == 5
    assert report["nonkeyed_rows"] == 1
    assert report["distinct_skus"] == 2
    assert report["selected_keyed_rows_before_validation"] == 3
    assert report["keyed_quantity_signs"] == {"invalid": 1, "missing": 1, "negative": 1, "positive": 1, "zero": 1}
    assert report["nonkeyed_quantity_signs"] == {"positive": 1}


def test_source_assessment_exposes_scope_quality_and_safe_aggregates(sources, tmp_path):
    report = build_assessment(source_root=sources, artifact_root=tmp_path / "artifacts",
                              as_of="2026-09-22T23:59:59+05:00", sku_limit=1)
    assert report["coverage"] == {
        "selected_skus": 1, "source_skus": 2, "selected_sku_fraction": 0.5,
        "accepted_sales_rows": 2, "source_keyed_rows": 5, "accepted_row_fraction": 0.4,
        "selection": "top positive shipment-frequency piece SKUs",
    }
    assert report["source_profiles"]["S08"]["selected_keyed_rows_before_validation"] == 4
    assert report["snapshot"]["table_counts"]["quarantine"] == 2
    assert report["snapshot"]["quality_status"] == "degraded"
    assert not report["snapshot"]["capabilities"]["can_plan"]
    assert report["snapshot"]["blocking_issue_counts"]["MISSING_CURRENT_STOCK"] == 1
    assert {s["source_id"] for s in report["sources"] if s["status"] == "metadata_only"} == {"S01", "S12"}
    serialized = json.dumps(report) + render_markdown(report)
    for private in ("private-sku", "private-doc", "private-name", "private-stock", str(sources)):
        assert private not in serialized
    assert "Single local run" in serialized
    assert all(value >= 0 for value in report["timing"]["seconds"].values())


def test_existing_snapshot_scope_and_checksum_are_verified(sources, tmp_path):
    fresh = build_assessment(source_root=sources, artifact_root=tmp_path / "artifacts",
                             as_of="2026-09-22T23:59:59+05:00", sku_limit=1)
    manifest = tmp_path / "artifacts" / "snapshots" / fresh["snapshot"]["snapshot_id"] / "manifest.json"
    reused = build_assessment(source_root=sources, snapshot=manifest, sku_limit=1)
    assert reused["coverage"] == fresh["coverage"]
    assert "snapshot_build" not in reused["timing"]["seconds"]
    snapshot_only = build_assessment(snapshot=manifest)
    assert snapshot_only["coverage"]["source_skus"] is None
    assert snapshot_only["coverage"]["selected_sku_fraction"] is None
    with pytest.raises(ValueError, match="scope differs"):
        build_assessment(source_root=sources, snapshot=manifest, sku_limit=2)
    candidate = sources / "Systeme electric" / "Товар в пути fixture.xlsx"
    original_candidate = candidate.read_bytes()
    candidate.write_bytes(b"modified")
    with pytest.raises(ValueError, match="S12 checksum differs"):
        build_assessment(source_root=sources, snapshot=manifest, sku_limit=1)
    candidate.write_bytes(original_candidate)
    snapshot = json.loads(manifest.read_text())
    from pathlib import Path
    table = Path(snapshot["tables"]["sales_events"]["uri"])
    table.write_bytes(b"modified")
    with pytest.raises(DataError, match="checksum"):
        build_assessment(snapshot=manifest)


def test_cli_writes_json_and_markdown_without_printing_rows(sources, tmp_path, monkeypatch, capsys):
    output = tmp_path / "report"
    monkeypatch.setattr("sys.argv", ["assessment", "--systeme-root", str(sources), "--as-of",
                                   "2026-09-22T23:59:59+05:00", "--sku-limit", "1", "--output-dir", str(output)])
    main()
    printed = json.loads(capsys.readouterr().out)
    assert printed["report_json"] == str(output / "report.json")
    report = json.loads((output / "report.json").read_text())
    assert report["coverage"]["source_keyed_rows"] == 5
    assert "40.00%" in (output / "report.md").read_text()
    assert "private-sku" not in (output / "report.md").read_text()
