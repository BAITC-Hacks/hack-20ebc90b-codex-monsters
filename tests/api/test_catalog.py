"""Source discovery is structural, conservative and never exposes local paths."""

import pytest
from fastapi.testclient import TestClient

from ekt.api.catalog import discover_sources, readable_name


FILES = {
    "S01": ("IEK", "MOQ ИЭК.xlsx"),
    "S02": ("IEK", "Динамика продаж.xlsx"),
    "S03": ("IEK", "Ежемесячные остатки.xlsx"),
    "S04": ("IEK", "Ежемесячные продажи.xlsx"),
    "S05": ("IEK", "Путь ИЭК.xlsx"),
    "S06": ("IEK", "Сезонность.xlsx"),
    "S07": ("Systeme electric", "MOQ SystemElectric.xlsx"),
    "S08": ("Systeme electric", "Динамика продаж Systeme.xlsx"),
    "S09": ("Systeme electric", "Ежемесячные остатки Systeme.xlsx"),
    "S10": ("Systeme electric", "Ежемесячные продажи Systeme.xlsx"),
    "S11": ("Systeme electric", "Сезонность Systeme.xlsx"),
    "S12": ("Systeme electric", "Товар в пути Systeme.xlsx"),
}


def populate(root, *, garbled=False, omit=()):
    paths = {}
    for source_id, (folder, name) in FILES.items():
        if source_id in omit:
            continue
        if garbled:
            name = name.encode("cp866").decode("mac_roman")
        path = root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()  # Discovery must not parse or certify workbook contents.
        paths[source_id] = path
    return paths


@pytest.mark.parametrize("garbled", [False, True])
def test_catalog_discovers_all_twelve_sources_without_claiming_all_are_importable(tmp_path, garbled):
    paths = populate(tmp_path, garbled=garbled)
    sources = discover_sources(tmp_path)
    assert set(sources) == set(FILES)
    assert {key for key, value in sources.items() if value["available_for_import"]} == {"S01", "S02", "S05", "S07", "S08", "S12"}
    assert all(source["mode"] == "real_preview" for source in sources.values())
    assert all(source["path"] == str(paths[key].resolve()) for key, source in sources.items())
    assert sources["S12"]["kind"] == "inventory_snapshots"
    assert "сверки" in sources["S12"]["name"]
    assert all(sources[key]["mapping_version"] == "systeme-preview-v1" for key in ("S07", "S08", "S12"))
    assert all(sources[key]["mapping_version"] == "iek-preview-v1" for key in ("S01", "S02", "S05"))
    assert all(sources[key]["mapping_version"] is None for key in set(FILES) - {"S01", "S02", "S05", "S07", "S08", "S12"})


@pytest.mark.parametrize("missing", ["S07", "S08", "S12"])
def test_incomplete_preview_group_is_blocked_as_a_whole(tmp_path, missing):
    populate(tmp_path, omit={missing})
    sources = discover_sources(tmp_path)
    assert missing not in sources
    assert len(sources) == 11
    assert {key for key, source in sources.items() if source["available_for_import"]} == {"S01", "S02", "S05"}


def test_ambiguous_normal_and_garbled_matches_are_not_chosen(tmp_path):
    populate(tmp_path)
    duplicate = "Динамика дополнительная.xlsx".encode("cp866").decode("mac_roman")
    (tmp_path / "Systeme electric" / duplicate).touch()
    sources = discover_sources(tmp_path)
    assert "S08" not in sources
    assert {key for key, source in sources.items() if source["available_for_import"]} == {"S01", "S02", "S05"}


def test_excel_lock_files_do_not_make_catalog_ambiguous(tmp_path):
    paths = populate(tmp_path)
    for path in paths.values():
        path.with_name("~$" + path.name).touch()
    assert len(discover_sources(tmp_path)) == 12


def test_readable_name_preserves_original_cyrillic_and_ascii():
    assert readable_name("MOQ SystemElectric.xlsx") == "MOQ SystemElectric.xlsx"
    assert readable_name("Динамика продаж.xlsx") == "Динамика продаж.xlsx"
    assert readable_name("Динамика.xlsx".encode("cp866").decode("mac_roman")) == "Динамика.xlsx"


def test_http_sources_and_workspace_never_reveal_local_paths(tmp_path, monkeypatch):
    from ekt.api.app import create_app

    source_root = tmp_path / "private-source-location"
    paths = populate(source_root, garbled=True)
    monkeypatch.setenv("EKT_SOURCE_ROOT", str(source_root))
    monkeypatch.delenv("EKT_SOURCE_CONFIG", raising=False)
    with TestClient(create_app(data_dir=tmp_path / "state")) as client:
        for endpoint in ("/v1/sources", "/v1/workspace"):
            response = client.get(endpoint)
            assert response.status_code == 200, response.text
            assert str(source_root) not in response.text
            assert all(path.name not in response.text for path in paths.values())
            payload = response.json()
            sources = payload["items"] if endpoint == "/v1/sources" else payload["sources"]
            assert len(sources) == 13  # Includes the labelled synthetic fixture.
            assert {source["source_id"] for source in sources} == {*FILES, "synthetic-demo"}
            assert all("path" not in source and "local_ref" not in source for source in sources)
