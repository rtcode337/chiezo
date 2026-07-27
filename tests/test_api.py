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


class TestTagFilter:
    """タグ(Wikipedia のカテゴリ)での絞り込み。

    全文検索で本文の "Category:" 行を拾う代用は、ソートキー付きのカテゴリ
    (`[[Category:日本の小説家|なつめ そうせき]]`)を取りこぼす。tags 経由なら引ける、
    というのがこの機能の存在理由なので、その対比もここで固定しておく。
    """

    def test_filter_by_tag(self, client):
        res = client.get("/v1/jawiki/filter", params={"tag": "日本の都道府県"})
        assert res.status_code == 200
        body = res.json()
        assert body["total"] == 2
        assert sorted(r["title"] for r in body["results"]) == ["大阪府", "東京都"]

    def test_filter_by_tag_finds_articles_full_text_search_misses(self, client):
        # カテゴリ列挙を全文検索で代用する方法(本文の "Category:" 行を引く)。
        # ソートキーが無い記事は引けるが……
        hit = client.get("/v1/jawiki/search", params={"q": "Category:東京都の寺"})
        assert [r["title"] for r in hit.json()["results"]] == ["浅草寺"]
        # ソートキー付きの記事は本文にカテゴリ名が残らないので引けない
        miss = client.get("/v1/jawiki/search", params={"q": "Category:日本の小説家"})
        assert miss.json()["results"] == []

        res = client.get("/v1/jawiki/filter", params={"tag": "日本の小説家"})
        assert res.status_code == 200
        assert [r["title"] for r in res.json()["results"]] == ["夏目漱石"]

    def test_filter_by_multiple_tags_is_or(self, client):
        res = client.get("/v1/jawiki/filter", params={"tag": "台東区,世界遺産"})
        assert res.status_code == 200
        assert sorted(r["title"] for r in res.json()["results"]) == ["富士山", "浅草寺"]

    def test_filter_by_unknown_tag_is_empty(self, client):
        res = client.get("/v1/jawiki/filter", params={"tag": "存在しないカテゴリ"})
        assert res.status_code == 200
        assert res.json() == {
            "source": "jawiki", "total": 0, "limit": 50, "offset": 0, "results": []
        }

    def test_filter_tag_pagination_and_fields(self, client):
        res = client.get(
            "/v1/jawiki/filter",
            params={"tag": "日本の都道府県", "fields": "title,tags", "limit": 1},
        )
        (row,) = res.json()["results"]
        assert sorted(row) == ["tags", "title"]
        assert "日本の都道府県" in row["tags"]
        assert res.json()["total"] == 2  # limit を絞っても総数は返る

    def test_search_can_be_narrowed_by_tag(self, client):
        params = {"q": "である"}  # FTS 経路
        assert len(client.get("/v1/jawiki/search", params=params).json()["results"]) > 1
        res = client.get("/v1/jawiki/search", params={**params, "tag": "近畿地方"})
        assert [r["title"] for r in res.json()["results"]] == ["大阪府"]

    def test_short_query_title_prefix_fallback_honors_tag(self, client):
        # 3 文字未満はタイトル前方一致へ落ちる別経路。ここでも tag が効く
        params = {"q": "大阪"}
        assert client.get("/v1/jawiki/search", params=params).json()["mode"] == "title_prefix"
        hit = client.get("/v1/jawiki/search", params={**params, "tag": "近畿地方"})
        assert [r["title"] for r in hit.json()["results"]] == ["大阪府"]
        miss = client.get("/v1/jawiki/search", params={**params, "tag": "関東地方"})
        assert miss.json()["results"] == []

    def test_doc_can_be_narrowed_by_tag(self, client):
        assert client.get(
            "/v1/jawiki/doc", params={"title": "大阪府", "tag": "近畿地方", "fields": "title"}
        ).json() == {"title": "大阪府"}
        # タグが一致しなければ 404(同名の別文書を掴まないための絞り込み)
        res = client.get("/v1/jawiki/doc", params={"title": "大阪府", "tag": "関東地方"})
        assert res.status_code == 404

    def test_tags_lists_names_with_doc_counts(self, client):
        res = client.get("/v1/jawiki/tags", params={"limit": 3})
        assert res.status_code == 200
        tags = res.json()["tags"]
        assert tags[0] == {"tag": "日本の都道府県", "docs": 2}  # 文書数の多い順
        assert len(tags) == 3

    def test_tags_prefix_and_contains(self, client):
        by_prefix = client.get("/v1/jawiki/tags", params={"prefix": "日本の"}).json()["tags"]
        assert {t["tag"] for t in by_prefix} == {"日本の都道府県", "日本の合戦", "日本の山",
                                                 "日本の小説家", "日本の鉄道"}
        by_contains = client.get("/v1/jawiki/tags", params={"contains": "地方"}).json()["tags"]
        assert {t["tag"] for t in by_contains} == {"関東地方", "近畿地方"}

    def test_tags_like_wildcards_escaped(self, client):
        assert client.get("/v1/jawiki/tags", params={"prefix": "%"}).json()["tags"] == []
        assert client.get("/v1/jawiki/tags", params={"contains": "%"}).json()["tags"] == []

    def test_browse_doc_links_tags_to_tag_listing(self, client):
        res = client.get("/jawiki/doc/1")
        assert 'href="/jawiki/?tag=%E9%96%A2%E6%9D%B1%E5%9C%B0%E6%96%B9"' in res.text
        listing = client.get("/jawiki/", params={"tag": "日本の都道府県"})
        assert "大阪府" in listing.text and "東京都" in listing.text


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

    def test_admin_groups_wikipedia_languages_into_one_row(self, client):
        """<lang>wiki は 348 件あるので、一覧には出さず 1 行にまとめて言語選択へ誘導する。"""
        res = client.get("/admin")
        assert res.status_code == 200
        assert "enwiki" not in res.text
        assert '<a href="/admin/wikipedia">' in res.text

    def test_admin_wikipedia_lists_languages(self, client):
        res = client.get("/admin/wikipedia")
        assert res.status_code == 200
        assert "enwiki" in res.text
        assert "英語" in res.text
        # フィクスチャで構築済みの jawiki は初期化ボタンではなく件数リンクになる
        assert "初期化済み" in res.text

    def test_admin_wikipedia_filters_by_query(self, client):
        res = client.get("/admin/wikipedia", params={"q": "english"})
        assert "enwiki" in res.text
        res = client.get("/admin/wikipedia", params={"q": "nosuchlanguage"})
        assert "該当する言語がありません" in res.text

    def test_admin_wikipedia_uses_trigger_catalog_when_available(self, client, monkeypatch):
        """言語の一覧の正は ingest 側。chiezo-trigger から取れたらそちらを使う。"""
        catalog = {
            "dewiki": {
                "kind": "wikipedia", "lang": "de", "group": "wikipedia",
                "label": "ドイツ語", "label_en": "German", "autonym": "Deutsch",
                "articles": 3_138_349,
            },
        }
        monkeypatch.setattr("app.main._fetch_trigger_catalog", lambda: catalog)
        res = client.get("/admin/wikipedia")
        assert "ドイツ語" in res.text
        assert "3,138,349" in res.text
        # 記事数の階層でグルーピングされる
        assert "100 万記事以上" in res.text

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


class TestClaudeConfig:
    """「いま設定を吐き出したら何が出るか」のプレビュー(管理画面)。"""

    def test_config_txt_returns_block(self, client):
        res = client.get("/admin/claude-config.txt")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/plain")
        text = res.text
        assert text.startswith("<!-- BEGIN chiezo (auto-generated) -->")
        assert text.rstrip().endswith("<!-- END chiezo -->")
        # 登録済みソースが例示コマンドとして載る
        assert "- **jawiki**" in text
        assert "/v1/jawiki/search" in text

    def test_config_txt_steers_category_lookups_to_the_tag_filter(self, client):
        """カテゴリ列挙を本文の全文検索でやらせないための指示が載ること。

        これが無いと Claude は `search?q=Category:ラーメン店` のような代用に走り、
        ソートキー付きの記事(ラーメン二郎など)を黙って取りこぼす。
        """
        text = client.get("/admin/claude-config.txt").text
        assert "/v1/jawiki/filter?limit=200&fields=title,tags" in text
        assert 'tag=日本の都道府県' in text  # 実在するタグから例示している
        assert "本文の全文検索で `Category:` 行を探してはいけない" in text
        assert "/v1/jawiki/tags?limit=20" in text

    def test_config_txt_base_url_is_derived_from_request(self, client):
        """curl 例のベース URL はアクセス元(プロトコル・ホスト名・ポート)から導出する。"""
        res = client.get("/admin/claude-config.txt")
        # TestClient のベースは http://testserver
        assert 'ベース URL: `http://testserver`' in res.text
        assert "http://testserver/v1/jawiki/search" in res.text

    def test_config_txt_host_header_keeps_port(self, client):
        """Host ヘッダのポートが生成 URL に残る(非標準ポート公開)。"""
        res = client.get(
            "/admin/claude-config.txt", headers={"Host": "192.168.1.10:9000"}
        )
        assert 'ベース URL: `http://192.168.1.10:9000`' in res.text
        assert "http://192.168.1.10:9000/v1/jawiki/search" in res.text

    def test_config_txt_honors_forwarded_headers(self, client):
        """リバースプロキシ越しは X-Forwarded-Proto / X-Forwarded-Host を優先する。

        X-Forwarded-Host が無いプロキシでは Host ヘッダにフォールバックする
        (Host はポートを保持しているのでポートが落ちない)。
        """
        res = client.get(
            "/admin/claude-config.txt",
            headers={"X-Forwarded-Proto": "https", "Host": "example.me:8443"},
        )
        assert 'ベース URL: `https://example.me:8443`' in res.text

        res = client.get(
            "/admin/claude-config.txt",
            headers={
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": "chiezo.example.me",
                "Host": "backend:9000",
            },
        )
        assert 'ベース URL: `https://chiezo.example.me`' in res.text

    def test_permissions_json_returns_allow_rules(self, client):
        import json

        res = client.get("/admin/claude-config.permissions.json")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/json")
        allow = res.json()["permissions"]["allow"]
        # chiezo への curl 許可は -s/-sG × クォート有無の 4 本。ベース URL はアクセス元から導出
        assert allow == sorted(allow)
        assert "Bash(curl -s http://testserver/:*)" in allow
        assert 'Bash(curl -s "http://testserver/:*)' in allow
        assert "Bash(curl -sG http://testserver/:*)" in allow
        assert 'Bash(curl -sG "http://testserver/:*)' in allow
        assert len(allow) == 4

    def test_permissions_json_honors_forwarded_headers(self, client):
        res = client.get(
            "/admin/claude-config.permissions.json",
            headers={"X-Forwarded-Proto": "https", "Host": "example.me:8443"},
        )
        allow = res.json()["permissions"]["allow"]
        assert "Bash(curl -s https://example.me:8443/:*)" in allow

    def test_config_txt_mentions_bulk_style_only_with_hook(self, client):
        """自動許可される書き方の指示は ?hook=1 のときだけ出す。

        フックは --with-hook を明示したときしか入らないので、既定の応答に
        「自動許可される」と書くと、入れていない環境には嘘になる。
        """
        plain = client.get("/admin/claude-config.txt").text
        assert "許可プロンプトは出ない" not in plain

        withhook = client.get("/admin/claude-config.txt?hook=1").text
        assert "許可プロンプトは出ない" in withhook
        assert "`$(...)`" in withhook
        # それ以外は同じブロック
        assert withhook.startswith("<!-- BEGIN chiezo (auto-generated) -->")
        assert "- **jawiki**" in withhook

    def test_hook_script_is_served_with_origin_baked_in(self, client):
        """フック本体は、アクセス元から導出したベース URL を埋め込んで配られる。"""
        res = client.get(
            "/admin/claude-config.hook.py", headers={"Host": "192.168.1.10:9000"}
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/x-python")
        assert res.text.startswith("#!/usr/bin/env python3")
        assert 'CHIEZO_ORIGIN = "http://192.168.1.10:9000"' in res.text
        # 差し替え漏れ(localhost のまま配る)は起きていない
        assert "http://localhost:9000" not in res.text

    def test_hook_settings_json_keeps_path_placeholder(self, client):
        """設置先はクライアント側で決まるので、パスはプレースホルダのまま返す。"""
        res = client.get("/admin/claude-config.hook.json")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/json")
        entries = res.json()["hooks"]["PreToolUse"]
        assert entries[0]["matcher"] == "Bash"
        assert entries[0]["hooks"][0]["command"] == "{{HOOK_PATH}}"

    def test_config_html_has_all_blocks_and_copy_buttons(self, client):
        res = client.get("/admin/claude-config")
        assert res.status_code == 200
        # CLAUDE.md ブロック・権限ファイル・フック設定を、それぞれのコピーボタン付きで出す
        assert 'id="config-block"' in res.text
        assert 'id="config-perms"' in res.text
        assert 'id="config-hook"' in res.text
        assert 'id="copy-block"' in res.text
        assert 'id="copy-perms"' in res.text
        assert 'id="copy-hook"' in res.text
        assert "BEGIN chiezo" in res.text
        assert "permissions" in res.text
        assert "/admin/claude-config.hook.py" in res.text

    def test_admin_links_to_config_page(self, client):
        res = client.get("/admin")
        assert '/admin/claude-config' in res.text


class TestBrowsePages:
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


class TestTagSchemaGuard:
    """schema_version 2 で作られた既存 DB は doc_tags を持たない(tag 絞り込みは 409)。"""

    @pytest.fixture()
    def legacy_client(self, tmp_path, built_data_dir):
        import shutil
        import sqlite3

        from fastapi.testclient import TestClient

        data_dir = tmp_path / "v2"
        data_dir.mkdir()
        db_path = data_dir / "jawiki.db"
        shutil.copy(built_data_dir / "jawiki.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE doc_tags")
        conn.execute("UPDATE meta SET schema_version = 2")
        conn.commit()
        conn.close()

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app

            with TestClient(app) as c:
                yield c, db_path
        finally:
            mp.undo()

    def test_tag_filter_returns_409_pointing_at_the_migration(self, legacy_client):
        client, _ = legacy_client
        for path, params in (
            ("/v1/jawiki/filter", {"tag": "日本の都道府県"}),
            ("/v1/jawiki/search", {"q": "日本", "tag": "日本の都道府県"}),
            ("/v1/jawiki/tags", {}),
        ):
            res = client.get(path, params=params)
            assert res.status_code == 409, path
            assert "add_tag_index.py" in res.json()["error"]

    def test_other_endpoints_still_work_on_v2(self, legacy_client):
        client, _ = legacy_client
        assert client.get("/v1/jawiki/doc", params={"title": "東京都"}).status_code == 200
        assert client.get("/v1/jawiki/filter", params={"wikidata": "Q1490"}).status_code == 200
        # tags は取れるが、リンクにはしない(飛んだ先が 409 になるため)
        assert "?tag=" not in client.get("/jawiki/doc/1").text

    def test_add_tag_index_migrates_in_place(self, legacy_client):
        import sqlite3
        import subprocess
        import sys
        from pathlib import Path

        _, db_path = legacy_client
        script = Path(__file__).resolve().parents[1] / "scripts" / "add_tag_index.py"
        # --batch を文書数より小さくして、分割ループが 2 周以上回る経路を通す
        # (一時領域を小さく保つために件数で刻んでおり、刻み目で取りこぼすと静かに欠ける)
        run = subprocess.run(
            [sys.executable, str(script), "--batch", "3", str(db_path)],
            capture_output=True, text=True,
        )
        assert run.returncode == 0, run.stderr
        assert run.stdout.count("docs ->") >= 4, run.stdout  # 11 文書 / 3 件ずつ

        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            (version,) = conn.execute("SELECT schema_version FROM meta").fetchone()
            assert version == 3
            rows = conn.execute(
                "SELECT doc_id FROM doc_tags WHERE tag = '日本の都道府県'"
            ).fetchall()
            assert sorted(r[0] for r in rows) == [1, 10]
        finally:
            conn.close()

        # 二度目は何もしない(冪等)
        again = subprocess.run(
            [sys.executable, str(script), str(db_path)], capture_output=True, text=True
        )
        assert again.returncode == 0
        assert "nothing to do" in again.stdout
