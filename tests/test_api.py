"""API エンドポイントのテスト(フィクスチャ DB 使用)。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def client(built_data_dir, monkeypatch_module):
    monkeypatch_module.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


class TestHealthAndSources:
    def test_healthz(self, client):
        res = client.get("/healthz")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "ok"
        assert body["sources"]["jawiki"]["docs"] == 11
        assert body["sources"]["jawiki"]["dump_date"] == "20260701"

    def test_sources(self, client):
        res = client.get("/v1/sources")
        assert res.status_code == 200
        (src,) = res.json()["sources"]
        assert src["name"] == "jawiki"
        assert src["kind"] == "wikipedia"
        assert src["lang"] == "ja"
        assert src["docs"] == 11

    def test_unknown_source_returns_404_with_source_list(self, client):
        res = client.get("/v1/nosuch/search", params={"q": "東京都"})
        assert res.status_code == 404
        body = res.json()
        assert "unknown source" in body["error"]
        assert body["sources"] == ["jawiki"]


class TestSearch:
    def test_fts_search(self, client):
        res = client.get("/v1/jawiki/search", params={"q": "仲見世通り"})
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "fts"
        assert body["source"] == "jawiki"
        titles = [r["title"] for r in body["results"]]
        assert titles == ["浅草寺"]
        assert "仲見世通り" in body["results"][0]["snippet"]

    def test_multi_term_and_search(self, client):
        # 「歴史」(2文字) は trigram 不可のため除外され「浅草寺」で検索される
        res = client.get("/v1/jawiki/search", params={"q": "浅草寺 歴史"})
        body = res.json()
        assert body["mode"] == "fts"
        assert [r["title"] for r in body["results"]] == ["浅草寺"]

    def test_short_query_falls_back_to_title_prefix(self, client):
        res = client.get("/v1/jawiki/search", params={"q": "犬"})
        assert res.status_code == 200
        body = res.json()
        assert body["mode"] == "title_prefix"
        assert [r["title"] for r in body["results"]] == ["犬"]

    def test_limit_and_offset(self, client):
        all_res = client.get("/v1/jawiki/search", params={"q": "である", "limit": 50}).json()
        assert len(all_res["results"]) > 2
        page = client.get(
            "/v1/jawiki/search", params={"q": "である", "limit": 2, "offset": 1}
        ).json()
        assert len(page["results"]) == 2
        assert page["results"][0] == all_res["results"][1]

    def test_limit_over_max_rejected(self, client):
        res = client.get("/v1/jawiki/search", params={"q": "東京都", "limit": 51})
        assert res.status_code == 422

    def test_quote_injection_is_escaped(self, client):
        res = client.get("/v1/jawiki/search", params={"q": '寺院" OR x'})
        assert res.status_code == 200
        assert res.json()["mode"] == "fts"


class TestDoc:
    def test_default_fields(self, client):
        res = client.get("/v1/jawiki/doc", params={"title": "浅草寺"})
        assert res.status_code == 200
        body = res.json()
        assert sorted(body) == sorted(["title", "opening", "body", "tags", "updated_at"])
        assert body["title"] == "浅草寺"
        assert body["tags"] == ["東京都の寺", "台東区"]

    def test_alias_resolution(self, client):
        res = client.get("/v1/jawiki/doc", params={"title": "金龍山浅草寺"})
        assert res.status_code == 200
        assert res.json()["title"] == "浅草寺"

    def test_not_found_returns_candidates(self, client):
        res = client.get("/v1/jawiki/doc", params={"title": "浅草"})
        assert res.status_code == 404
        body = res.json()
        assert "not found" in body["error"]
        assert "浅草寺" in body["candidates"]

    def test_fields_filter(self, client):
        res = client.get(
            "/v1/jawiki/doc", params={"title": "東京都", "fields": "title,opening,tags"}
        )
        body = res.json()
        assert sorted(body) == ["opening", "tags", "title"]

    def test_unknown_field_rejected(self, client):
        res = client.get("/v1/jawiki/doc", params={"title": "東京都", "fields": "title,bogus"})
        assert res.status_code == 400
        assert "bogus" in res.json()["error"]

    def test_max_chars_truncates_body(self, client):
        res = client.get(
            "/v1/jawiki/doc", params={"title": "東京都", "fields": "body", "max_chars": 10}
        )
        assert len(res.json()["body"]) == 10

    def test_doc_by_id(self, client):
        res = client.get("/v1/jawiki/doc/2", params={"fields": "doc_id,title"})
        assert res.status_code == 200
        assert res.json() == {"doc_id": 2, "title": "浅草寺"}

    def test_doc_by_id_not_found(self, client):
        res = client.get("/v1/jawiki/doc/424242")
        assert res.status_code == 404


class TestTitlesLinksRandom:
    def test_titles_prefix(self, client):
        res = client.get("/v1/jawiki/titles", params={"prefix": "浅草"})
        assert res.status_code == 200
        assert [t["title"] for t in res.json()["titles"]] == ["浅草寺"]

    def test_titles_like_wildcards_escaped(self, client):
        res = client.get("/v1/jawiki/titles", params={"prefix": "%"})
        assert res.status_code == 200
        assert res.json()["titles"] == []


class TestAdminAndBrowse:
    def test_root_redirects_to_admin(self, client):
        res = client.get("/", follow_redirects=False)
        assert res.status_code in (302, 307)
        assert res.headers["location"] == "/admin"

    def test_admin_lists_registered_source(self, client):
        res = client.get("/admin")
        assert res.status_code == 200
        assert "jawiki" in res.text

    def test_admin_lists_uninitialized_source(self, client):
        res = client.get("/admin")
        assert res.status_code == 200
        assert "geonames" in res.text

    def test_admin_groups_osm_countries_into_one_row(self, client):
        """osm_<国> は 195 件あるので、一覧には出さず 1 行にまとめて国選択へ誘導する。"""
        res = client.get("/admin")
        assert res.status_code == 200
        assert "osm_japan" not in res.text
        assert '<a href="/admin/osm">' in res.text

    def test_admin_osm_lists_countries(self, client):
        res = client.get("/admin/osm")
        assert res.status_code == 200
        assert "osm_japan" in res.text
        assert "日本" in res.text
        assert "asia/japan" in res.text

    def test_admin_osm_filters_by_query(self, client):
        res = client.get("/admin/osm", params={"q": "japan"})
        assert "osm_japan" in res.text
        res = client.get("/admin/osm", params={"q": "nosuchcountry"})
        assert "該当する国・地域がありません" in res.text

    def test_admin_osm_uses_trigger_catalog_when_available(self, client, monkeypatch):
        """国の一覧の正は ingest 側。chiezo-trigger から取れたらそちらを使う。"""
        catalog = {
            "osm_france": {
                "kind": "osm", "lang": "fr", "group": "osm", "slug": "france",
                "label": "フランス", "label_en": "France", "continent": "europe",
                "region": "europe/france", "pbf_bytes": 4_700_000_000,
                "memory_gb": 24.0, "node_index": "sparse_file_array",
            },
        }
        monkeypatch.setattr("app.main._fetch_trigger_catalog", lambda: catalog)
        res = client.get("/admin/osm")
        assert "フランス" in res.text
        assert "4.7 GB" in res.text
        # RAM に載らない国はディスク索引が既定なので、要件は 2GiB として案内される
        assert "2 GiB(ディスク索引・低速。RAM 索引なら 24 GiB)" in res.text

    def test_admin_osm_marks_initialized_source(self, client, monkeypatch):
        """構築済みの国は初期化ボタンではなく件数リンクを出す。"""
        # フィクスチャで登録済みの jawiki を osm カタログに見立てる
        catalog = {
            "jawiki": {"kind": "osm", "group": "osm", "label": "構築済みの国", "continent": "asia"}
        }
        monkeypatch.setattr("app.main._fetch_trigger_catalog", lambda: catalog)
        res = client.get("/admin/osm")
        assert "初期化済み" in res.text
        assert "初期化</button>" not in res.text

    def test_admin_init_without_trigger_configured(self, client):
        res = client.post("/admin/init/osm_japan")
        assert res.status_code == 503

    def test_admin_init_unknown_source(self, client, monkeypatch):
        monkeypatch.setattr("app.main.TRIGGER_URL", "http://example.invalid")
        res = client.post("/admin/init/bogus")
        assert res.status_code == 404

    def test_admin_init_already_initialized(self, client, monkeypatch):
        monkeypatch.setattr("app.main.TRIGGER_URL", "http://example.invalid")
        res = client.post("/admin/init/jawiki")
        assert res.status_code == 409

    def test_browse_source_top_shows_search_form_only(self, client):
        res = client.get("/jawiki/")
        assert res.status_code == 200
        assert "<form" in res.text
        assert "浅草寺" not in res.text

    def test_browse_source_search(self, client):
        res = client.get("/jawiki/", params={"q": "浅草寺"})
        assert res.status_code == 200
        assert "浅草寺" in res.text

    def test_browse_source_unknown(self, client):
        res = client.get("/nosuch/")
        assert res.status_code == 404

    def test_browse_doc(self, client):
        res = client.get("/jawiki/doc/2")
        assert res.status_code == 200
        assert "浅草寺" in res.text

    def test_browse_doc_not_found(self, client):
        res = client.get("/jawiki/doc/424242")
        assert res.status_code == 404

    def test_links_out(self, client):
        res = client.get("/v1/jawiki/links", params={"title": "浅草寺"})
        assert res.status_code == 200
        body = res.json()
        assert body["direction"] == "out"
        assert "雷門" in body["links"]

    def test_links_resolves_alias(self, client):
        res = client.get("/v1/jawiki/links", params={"title": "東京"})
        assert res.status_code == 200
        assert res.json()["title"] == "東京都"

    def test_links_direction_in_rejected(self, client):
        res = client.get("/v1/jawiki/links", params={"title": "浅草寺", "direction": "in"})
        assert res.status_code == 400

    def test_random(self, client):
        res = client.get("/v1/jawiki/random", params={"limit": 3})
        assert res.status_code == 200
        results = res.json()["results"]
        assert len(results) == 3
        assert all({"doc_id", "title"} <= set(r) for r in results)


class TestFilterSchemaGuard:
    """schema_version 1 で作られた既存 DB は /filter を持たない(生成列も索引も無い)。"""

    def test_old_schema_returns_409(self, tmp_path, built_data_dir):
        import shutil
        import sqlite3

        from fastapi.testclient import TestClient

        data_dir = tmp_path / "legacy"
        data_dir.mkdir()
        db_path = data_dir / "jawiki.db"
        shutil.copy(built_data_dir / "jawiki.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE meta SET schema_version = 1")
        conn.commit()
        conn.close()

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app

            with TestClient(app) as c:
                res = c.get("/v1/jawiki/filter", params={"wikidata": "Q1490"})
                assert res.status_code == 409
                assert "re-run ingest" in res.json()["error"]
                # 既存エンドポイントは 1 のままでも従来どおり使える
                assert c.get("/v1/jawiki/doc", params={"title": "東京都"}).status_code == 200
        finally:
            mp.undo()
