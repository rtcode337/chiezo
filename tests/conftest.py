from pathlib import Path

import pytest

# api/ と ingest/ への import パスは pyproject.toml の pythonpath で通している。
ROOT = Path(__file__).resolve().parents[1]

# 画面の URL(人が打つもの・リンクされるものなので、変えたらテストが落ちてよい契約)。
# ソースの画面は /search/ の下(ルート直下をキャッチオールにすると、ask や admin という
# 名前のソースを足せなくなる)、会話は「Chiezo を使う側」の機能なので /ai/ の下。
CHAT_PATH = "/ai/chat"

FIXTURE_DUMP = ROOT / "tests" / "fixtures" / "mini_jawiki.xml.gz"
OSM_FIXTURE_DUMP = ROOT / "tests" / "fixtures" / "mini_osm.osm.pbf"
GEONAMES_FIXTURE_DUMP = ROOT / "tests" / "fixtures" / "mini_geonames.zip"


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


@pytest.fixture()
def geonames_fixture_dump(tmp_path) -> Path:
    """フィクスチャ一式を tmp へコピーし、本体 zip のパスを返す。

    iter_docs() は一時 lookup をダンプと同じディレクトリに作るため、リポジトリを
    汚さないようコピーしてから使う。
    """
    import shutil

    assert GEONAMES_FIXTURE_DUMP.exists(), "run tests/fixtures/make_geonames_fixture.py first"
    src = GEONAMES_FIXTURE_DUMP.parent
    dest = tmp_path / "dumps"
    dest.mkdir()
    for name in (
        "mini_geonames.zip",
        "mini_geonames_alternate.zip",
        "mini_geonames_countryInfo.txt",
        "mini_geonames_admin1.txt",
    ):
        shutil.copy(src / name, dest / name)
    return dest / "mini_geonames.zip"


def make_geonames_test_adapter(dump_path: Path):
    """フィクスチャ向けの geonames アダプタ(補助ファイルのパスを直接差し込む)。"""
    from sources.geonames import GeonamesAdapter

    adapter = GeonamesAdapter(min_docs=3, sample_titles=["Paris", "Tokyo"])
    workdir = dump_path.parent
    adapter._alt_path = workdir / "mini_geonames_alternate.zip"
    adapter._country_path = workdir / "mini_geonames_countryInfo.txt"
    adapter._admin1_path = workdir / "mini_geonames_admin1.txt"
    return adapter


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
