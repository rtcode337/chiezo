"""osm アダプタのテスト(フィクスチャ E2E: 変換 → 構築 → API)。"""
import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

import main as ingest_main
from conftest import make_osm_test_adapter


def connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


@pytest.fixture(scope="module")
def docs(osm_fixture_dump):
    adapter = make_osm_test_adapter()
    return {d.title: d for d in adapter.iter_docs(osm_fixture_dump)}


class TestOsmAdapter:
    def test_doc_count_and_exclusions(self, docs):
        # 対象地物 7 件のみ(amenity ノード・タグ無しノード・タグ無し way は除外)
        assert len(docs) == 7
        assert "ラーメン一番" not in docs

    def test_node_doc_fields(self, docs):
        tokyo = docs["東京"]
        assert tokyo.doc_id == 1 * 4 + 0  # node
        assert "Tokyo" in tokyo.aliases
        assert "東京" not in tokyo.aliases  # name:ja はタイトルと同じなので除外
        assert tokyo.tags == ["place=city"]
        assert tokyo.links == ["東京都区部"]  # wikipedia タグの言語プレフィックス除去
        assert tokyo.updated_at == "2026-06-01T00:00:00Z"
        assert tokyo.extra["osm_type"] == "node"
        assert tokyo.extra["osm_id"] == 1
        assert tokyo.extra["lat"] == pytest.approx(35.6895)
        assert tokyo.extra["lon"] == pytest.approx(139.6917)
        assert tokyo.extra["population"] == 13960000
        assert tokyo.extra["wikidata"] == "Q7473516"
        # 人口 1396 万 → log10 補正で place=city の基礎スコアより上がる
        assert tokyo.rank_score > 0.85
        assert "市" in tokyo.opening

    def test_alias_multi_value_split(self, docs):
        fuji = docs["富士山"]
        assert set(fuji.aliases) == {"Mount Fuji", "富士の山", "不二山"}
        assert "標高 3776m" in fuji.opening

    def test_duplicate_title_disambiguated(self, docs):
        # 「中央」が 2 件 → 先勝ちで 2 件目は "(node:4)" 付き、元の名前は alias に残る
        assert "中央" in docs
        assert docs["中央"].extra["osm_id"] == 3
        dup = docs["中央 (node:4)"]
        assert dup.extra["osm_id"] == 4
        assert "中央" in dup.aliases

    def test_way_centroid_is_node_average(self, docs):
        lake = docs["河口湖"]
        assert lake.doc_id == 100 * 4 + 1  # way
        assert lake.extra["lat"] == pytest.approx((35.50 + 35.52 + 35.51) / 3)
        assert lake.extra["lon"] == pytest.approx((138.75 + 138.76 + 138.77) / 3)

    def test_relation_uses_admin_centre_node(self, docs):
        kyoto = docs["京都市"]
        assert kyoto.doc_id == 200 * 4 + 2  # relation
        assert kyoto.extra["lat"] == pytest.approx(35.0116)
        assert kyoto.extra["lon"] == pytest.approx(135.7681)
        assert kyoto.extra["admin_level"] == 7
        assert kyoto.tags == ["boundary=administrative", "admin_level=7"]
        assert kyoto.links == ["京都市"]

    def test_relation_without_label_averages_member_ways(self, docs):
        biwa = docs["琵琶湖"]
        assert biwa.extra["lat"] == pytest.approx((35.0 + 35.2) / 2)
        assert biwa.extra["lon"] == pytest.approx((135.9 + 136.1) / 2)


class TestOsmBuild:
    def test_built_db_contents(self, built_osm_data_dir):
        conn = connect(built_osm_data_dir / "osm_japan.db")
        try:
            meta = conn.execute("SELECT * FROM meta").fetchone()
            assert meta["source"] == "osm_japan"
            assert meta["source_kind"] == "osm"
            assert meta["lang"] == "ja"

            (count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
            assert count == 7

            row = conn.execute("SELECT * FROM docs WHERE title = '京都市'").fetchone()
            extra = json.loads(row["extra"])
            assert extra["feature"] == "boundary=administrative"
            assert extra["tags"]["population"] == "1463723"

            aliases = dict(conn.execute("SELECT alias, doc_id FROM aliases").fetchall())
            assert aliases["Tokyo"] == 4
            assert aliases["中央"] == 16  # 重複タイトルの 2 件目の弁別 alias
        finally:
            conn.close()

    def test_fts_search_works(self, built_osm_data_dir):
        conn = connect(built_osm_data_dir / "osm_japan.db")
        try:
            rows = conn.execute(
                "SELECT d.title FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
                " WHERE docs_fts MATCH '\"河口湖\"'"
            ).fetchall()
            assert [r["title"] for r in rows] == ["河口湖"]
        finally:
            conn.close()


@pytest.fixture(scope="module")
def client(built_osm_data_dir):
    mp = pytest.MonkeyPatch()
    mp.setenv("CHIEZO_DATA_DIR", str(built_osm_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c
    mp.undo()


class TestOsmApi:
    def test_source_registered(self, client):
        res = client.get("/v1/sources")
        assert res.status_code == 200
        (src,) = res.json()["sources"]
        assert src["name"] == "osm_japan"
        assert src["kind"] == "osm"
        assert src["docs"] == 7

    def test_search(self, client):
        res = client.get("/v1/osm_japan/search", params={"q": "富士山"})
        assert res.status_code == 200
        assert "富士山" in [r["title"] for r in res.json()["results"]]

    def test_doc_with_coordinates(self, client):
        res = client.get(
            "/v1/osm_japan/doc", params={"title": "京都市", "fields": "title,extra"}
        )
        assert res.status_code == 200
        body = res.json()
        assert body["extra"]["lat"] == pytest.approx(35.0116)
        assert body["extra"]["lon"] == pytest.approx(135.7681)

    def test_doc_alias_resolution(self, client):
        res = client.get("/v1/osm_japan/doc", params={"title": "Mount Fuji"})
        assert res.status_code == 200
        assert res.json()["title"] == "富士山"
