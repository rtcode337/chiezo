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
        # 対象地物 12 件(タグ無しノード・タグ無し way・name 無し amenity・
        # 値が対象外の railway=rail / highway=residential は除外)
        assert len(docs) == 12
        assert "ラーメン一番" in docs

    def test_node_doc_fields(self, docs):
        tokyo = docs["東京"]
        assert tokyo.doc_id == 1 * 4 + 0  # node
        assert "Tokyo" in tokyo.aliases
        assert "東京" not in tokyo.aliases  # name:ja はタイトルと同じなので除外
        assert tokyo.tags == ["place=city"]
        assert tokyo.links == ["東京都区部"]  # wikipedia タグの言語プレフィックス除去
        assert tokyo.updated_at == "2026-06-01T00:00:00+00:00"
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

    def test_poi_doc_fields(self, docs):
        ramen = docs["ラーメン一番"]
        assert ramen.doc_id == 5 * 4 + 0  # node
        assert ramen.extra["osm_type"] == "node"
        assert ramen.extra["feature"] == "amenity=restaurant"
        assert ramen.extra["address"] == "京都市 河原町通"
        assert ramen.extra["phone"] == "075-000-0000"
        assert "住所 京都市 河原町通" in ramen.opening
        assert "phone: 075-000-0000" in ramen.body
        assert 0 < ramen.rank_score < 0.85  # place=city (人口補正あり) より低いスコア

    def test_duplicate_title_disambiguated(self, docs):
        # 「中央」が 2 件 → 先勝ちで 2 件目は "(node:4)" 付き、元の名前は alias に残る
        assert "中央" in docs
        assert docs["中央"].extra["osm_id"] == 3
        dup = docs["中央 (node:4)"]
        assert dup.extra["osm_id"] == 4
        assert "中央" in dup.aliases

    def test_transport_features_ingested(self, docs):
        # 旧版では place/natural/POI のどれにも属さず丸ごと取りこぼしていた交通インフラ
        station = docs["京都駅"]
        assert station.extra["feature"] == "railway=station"
        assert station.extra["area"] == "京都府"
        assert "駅" in station.opening
        assert docs["京都南インターチェンジ"].extra["feature"] == "highway=motorway_junction"
        airport = docs["大阪国際空港"]
        assert airport.extra["feature"] == "aeroway=aerodrome"
        assert "Osaka International Airport" in airport.aliases

    def test_transport_ranks_above_poi(self, docs):
        # 「博多駅」で同名のラーメン店を掴む取り違えを rank_score の段階で防ぐ
        assert docs["京都駅"].rank_score > docs["ラーメン一番"].rank_score

    def test_linear_features_excluded(self, docs):
        # 名前付きでも「地点」ではない値は対象外(railway=rail / highway=residential)
        assert "東海道本線" not in docs
        assert "五条通" not in docs

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


class TestOsmArea:
    """admin_level=4 の境界ポリゴンによる extra.area 付与(点内包判定)。"""

    def test_point_inside_boundary_gets_area(self, docs):
        assert docs["ラーメン一番"].extra["area"] == "京都府"
        assert "所在 京都府" in docs["ラーメン一番"].opening
        # admin_centre が府内にある京都市 relation も同じ府に入る
        assert docs["京都市"].extra["area"] == "京都府"

    def test_points_outside_boundary_have_no_area(self, docs):
        # 経度・緯度それぞれで外側にある地物(bbox 近似では取りこぼしうる位置関係)
        assert "area" not in docs["東京"].extra
        assert "area" not in docs["琵琶湖"].extra      # lon 136.0 > 135.9
        assert "area" not in docs["中央 (node:4)"].extra  # lat 34.69 < 34.9

    def test_boundary_relation_itself_is_inside(self, docs):
        kyoto_fu = docs["京都府"]
        assert kyoto_fu.extra["admin_level"] == 4
        assert kyoto_fu.extra["area"] == "京都府"

    def test_area_index_can_be_disabled(self, osm_fixture_dump):
        from sources.osm import OsmAdapter

        adapter = OsmAdapter(
            "osm_japan", region="asia/japan", lang="ja",
            min_docs=5, sample_titles=[], area_admin_level=0,
        )
        docs = {d.title: d for d in adapter.iter_docs(osm_fixture_dump)}
        assert "area" not in docs["ラーメン一番"].extra


class TestOsmBuild:
    def test_built_db_contents(self, built_osm_data_dir):
        conn = connect(built_osm_data_dir / "osm_japan.db")
        try:
            meta = conn.execute("SELECT * FROM meta").fetchone()
            assert meta["source"] == "osm_japan"
            assert meta["source_kind"] == "osm"
            assert meta["lang"] == "ja"

            (count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
            assert count == 12

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
        assert src["docs"] == 12

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


class TestOsmFilter:
    """Overpass 相当の一括抽出(地物種別 × 行政区 / bbox / wikidata 逆引き)。"""

    def test_filter_by_feature_and_area(self, client):
        res = client.get(
            "/v1/osm_japan/filter",
            params={"feature": "amenity=restaurant", "area": "京都府"},
        )
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 1
        (row,) = body["results"]
        assert row["title"] == "ラーメン一番"
        assert row["area"] == "京都府"
        assert row["lat"] == pytest.approx(35.0)

    def test_filter_by_area_only_excludes_outside_points(self, client):
        res = client.get("/v1/osm_japan/filter", params={"area": "京都府", "limit": 50})
        titles = {r["title"] for r in res.json()["results"]}
        assert titles == {"ラーメン一番", "京都市", "京都府", "京都駅", "京都南インターチェンジ"}

    def test_filter_by_multiple_features(self, client):
        res = client.get(
            "/v1/osm_japan/filter",
            params={"feature": "natural=peak,natural=water"},
        )
        titles = {r["title"] for r in res.json()["results"]}
        assert titles == {"富士山", "河口湖", "琵琶湖"}

    def test_filter_by_bbox(self, client):
        res = client.get(
            "/v1/osm_japan/filter",
            params={"bbox": "35.0,138.0,36.0,140.0", "feature": "place=city"},
        )
        assert [r["title"] for r in res.json()["results"]] == ["東京"]

    def test_filter_by_bbox_only(self, client):
        """bbox だけのときは総件数も doc_coords から数える(docs を読まない経路)。"""
        res = client.get(
            "/v1/osm_japan/filter", params={"bbox": "34.9,135.6,35.1,135.9", "limit": 50}
        )
        assert res.status_code == 200
        body = res.json()
        titles = {r["title"] for r in body["results"]}
        # ラーメン一番(lon 135.00)は経度の外。緯度だけで絞ると入ってしまう位置関係
        assert titles == {"京都市", "京都駅", "京都南インターチェンジ"}
        assert body["total"] == len(titles)

    def test_bbox_matches_the_generated_column_path(self, client, tmp_path, built_osm_data_dir):
        """doc_coords 経由(4)と生成列を直接引く旧経路(3)が同じ答えを返すこと。

        4 で足したのは docs の lat/lon と同じ値の写しだけで、答えは変わらない。
        移行前の DB でも動く必要があるので、両方を突き合わせて固定しておく。
        """
        import shutil

        data_dir = tmp_path / "v3"
        data_dir.mkdir()
        db_path = data_dir / "osm_japan.db"
        shutil.copy(built_osm_data_dir / "osm_japan.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE doc_coords")
        conn.execute("UPDATE meta SET schema_version = 3")
        conn.commit()
        conn.close()

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app

            with TestClient(app) as old:
                for params in (
                    {"bbox": "34.9,135.6,35.1,135.9", "limit": 50},
                    {"bbox": "35.0,138.0,36.0,140.0", "feature": "place=city"},
                    # 3 つ以上の条件(索引の集合を交差させる経路)も突き合わせる
                    {"bbox": "34.9,135.6,35.1,135.9", "feature": "amenity=restaurant",
                     "area": "京都府"},
                    {"bbox": "10.0,10.0,10.1,10.1"},  # 該当なし
                ):
                    res = old.get("/v1/osm_japan/filter", params=params)
                    assert res.status_code == 200, params
                    assert res.json() == client.get(
                        "/v1/osm_japan/filter", params=params
                    ).json(), params
        finally:
            mp.undo()

    def test_filter_by_wikidata(self, client):
        res = client.get("/v1/osm_japan/filter", params={"wikidata": "Q7473516"})
        assert [r["title"] for r in res.json()["results"]] == ["東京"]

    def test_filter_pagination_and_fields(self, client):
        first = client.get(
            "/v1/osm_japan/filter",
            params={"area": "京都府", "limit": 1, "offset": 0, "fields": "title,extra"},
        ).json()
        assert first["total"] == 5
        assert set(first["results"][0]) == {"title", "extra"}
        second = client.get(
            "/v1/osm_japan/filter", params={"area": "京都府", "limit": 1, "offset": 1}
        ).json()
        assert second["results"][0]["title"] != first["results"][0]["title"]

    def test_filter_requires_a_condition(self, client):
        res = client.get("/v1/osm_japan/filter")
        assert res.status_code == 400
        assert "at least one" in res.json()["error"]

    def test_filter_rejects_bad_bbox(self, client):
        res = client.get("/v1/osm_japan/filter", params={"bbox": "35.0,138.0"})
        assert res.status_code == 400

    def test_filter_rejects_unknown_field(self, client):
        res = client.get(
            "/v1/osm_japan/filter", params={"area": "京都府", "fields": "title,nope"}
        )
        assert res.status_code == 400
        assert "nope" in res.json()["error"]
