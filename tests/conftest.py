import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "api"))
sys.path.insert(0, str(ROOT / "ingest"))

FIXTURE_DUMP = ROOT / "tests" / "fixtures" / "mini_jawiki.json.gz"
OSM_FIXTURE_DUMP = ROOT / "tests" / "fixtures" / "mini_osm.osm.pbf"


@pytest.fixture(scope="session")
def fixture_dump() -> Path:
    assert FIXTURE_DUMP.exists(), "run tests/fixtures/make_fixture.py first"
    return FIXTURE_DUMP


@pytest.fixture(scope="session")
def osm_fixture_dump() -> Path:
    assert OSM_FIXTURE_DUMP.exists(), "run tests/fixtures/make_osm_fixture.py first"
    return OSM_FIXTURE_DUMP


def make_test_adapter():
    """フィクスチャ向けに検証パラメータを緩めた jawiki アダプタ。"""
    from sources.wikipedia import WikipediaAdapter

    return WikipediaAdapter(
        "jawiki", lang="ja", min_docs=5, sample_titles=["東京都", "浅草寺"]
    )


def make_osm_test_adapter():
    """フィクスチャ向けに検証パラメータを緩めた osm_japan アダプタ。"""
    from sources.osm import OsmAdapter

    return OsmAdapter(
        "osm_japan", region="asia/japan", lang="ja",
        min_docs=5, sample_titles=["東京", "富士山"],
    )


@pytest.fixture(scope="session")
def built_data_dir(tmp_path_factory, fixture_dump) -> Path:
    """フィクスチャから構築済みの data ディレクトリ(jawiki.db シンボリックリンク付き)。"""
    import main as ingest_main

    data_dir = tmp_path_factory.mktemp("data")
    adapter = make_test_adapter()
    building = data_dir / "jawiki-20260701.db.building"
    ingest_main.build_db(adapter, fixture_dump, "20260701", building)
    ingest_main.validate_db(adapter, building)
    ingest_main.switch_db(data_dir, "jawiki", "20260701", building)
    return data_dir


@pytest.fixture(scope="session")
def built_osm_data_dir(tmp_path_factory, osm_fixture_dump) -> Path:
    """OSM フィクスチャから構築済みの data ディレクトリ(osm_japan.db シンボリックリンク付き)。"""
    import main as ingest_main

    data_dir = tmp_path_factory.mktemp("osm_data")
    adapter = make_osm_test_adapter()
    building = data_dir / "osm_japan-20260701.db.building"
    ingest_main.build_db(adapter, osm_fixture_dump, "20260701", building)
    ingest_main.validate_db(adapter, building)
    ingest_main.switch_db(data_dir, "osm_japan", "20260701", building)
    return data_dir
