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

    def test_exact_title_survives_a_full_candidate_pool(self, client, monkeypatch):
        """候補の絞り込み(bm25 上位 N 件)から漏れても、タイトル完全一致は出てくる。

        本番では「東京都」のような語が 17 万件に当たり、関連度の上位だけを候補に
        するので、記事「東京都」がそこに入る保証はない。索引から直接拾う経路が
        効いていることを、候補を 1 件に絞り切って確かめる。
        """
        from app import main

        for name in ("SEARCH_POOL_MIN", "SEARCH_POOL_FACTOR", "SEARCH_POOL_MAX"):
            monkeypatch.setattr(main, name, 1)
        res = client.get("/v1/jawiki/search", params={"q": "東京都", "limit": 5})
        assert res.status_code == 200
        assert res.json()["results"][0]["title"] == "東京都"

    def test_candidate_pool_matches_the_unpooled_ranking(self, client, monkeypatch):
        """候補が該当件数以上あるとき(= 通常の検索)の並びは絞り込み前と同じ。"""
        from app import main

        params = {"q": "である", "limit": 50}
        full = client.get("/v1/jawiki/search", params=params).json()
        monkeypatch.setattr(main, "SEARCH_POOL_MIN", 10**6)
        assert client.get("/v1/jawiki/search", params=params).json() == full
        assert len(full["results"]) > 1, full

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


class TestExactTitleFirst:
    """タイトルが検索語と完全一致する文書を最上位に出すこと。

    bm25 は「その語をよく含む文書」を上げるが、「その語そのものを説明している文書」は
    特別扱いしない。本番データでも `京都` の検索で京都市・近鉄京都線が上に来て、
    記事「京都」は 5 位以内に入らなかった(長い記事ほど長さ正規化で不利になる)。
    """

    def test_exact_title_outranks_a_better_bm25_match(self, client):
        # 「東京都」は本文で東京都に触れる他の記事より bm25 が高いとは限らないが、
        # 同名の記事がある以上それが答え
        res = client.get("/v1/jawiki/search", params={"q": "東京都", "limit": 10})
        titles = [r["title"] for r in res.json()["results"]]
        assert len(titles) > 1
        assert titles[0] == "東京都"

    def test_exact_title_wins_in_the_title_prefix_fallback_too(self, tmp_path, built_data_dir):
        """3 文字未満はタイトル前方一致に落ちる別経路。ここでも完全一致が先頭。

        この経路は rank_score 降順で並ぶので、完全一致段が無いと「日本」より人気な
        「日本橋」が先に来てしまう。前方一致は docs だけを見る(FTS を使わない)ので、
        検証用の 1 件は docs へ直接入れれば足りる。
        """
        import shutil
        import sqlite3

        from fastapi.testclient import TestClient

        data_dir = tmp_path / "prefix"
        data_dir.mkdir()
        db_path = data_dir / "jawiki.db"
        shutil.copy(built_data_dir / "jawiki.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO docs (doc_id, title, opening, rank_score)"
            " VALUES (9001, '日本橋', 'にほんばし', 1.0)"
        )
        conn.execute("UPDATE docs SET rank_score = 0.1 WHERE title = '日本'")
        conn.commit()
        conn.close()

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app

            with TestClient(app) as c:
                body = c.get("/v1/jawiki/search", params={"q": "日本"}).json()
            assert body["mode"] == "title_prefix"
            titles = [r["title"] for r in body["results"]]
            assert titles == ["日本", "日本橋"], "完全一致が人気度より先に来ていない"
        finally:
            mp.undo()

    def test_no_exact_match_leaves_the_order_alone(self, client):
        """完全一致が無いクエリでは何も起きない(関連度と人気度だけで決まる)。"""
        res = client.get("/v1/jawiki/search", params={"q": "である"})
        titles = [r["title"] for r in res.json()["results"]]
        assert titles and "である" not in titles

    def test_whitespace_around_the_query_is_ignored(self, client):
        res = client.get("/v1/jawiki/search", params={"q": "  東京都  "})
        assert [r["title"] for r in res.json()["results"]][0] == "東京都"

    def test_browse_html_uses_the_same_order(self, client):
        html = client.get("/search/jawiki/", params={"q": "東京都"}).text
        rows = [line for line in html.splitlines() if "/search/jawiki/doc/" in line]
        assert rows and "東京都" in rows[0]


class TestPopularityRanking:
    """検索の並びに rank_score(0〜1 の知名度)を混ぜること。

    bm25 だけだと、2 文字語のように数千件ヒットする問い合わせで有名な記事が埋もれる。
    一方で rank_score をそのまま信じると古い DB(geonames は人口の生値が入っている)で
    並びが壊れるので、0〜1 に丸めてから使う。その両方をここで固定する。
    """

    @pytest.fixture()
    def ranked_client(self, tmp_path, built_data_dir):
        """同点になりやすい 2 文書の rank_score だけを差し替えたクライアント。"""
        import shutil
        import sqlite3

        from fastapi.testclient import TestClient

        data_dir = tmp_path / "ranked"
        data_dir.mkdir()
        db_path = data_dir / "jawiki.db"
        shutil.copy(built_data_dir / "jawiki.db", db_path)

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app

            def with_scores(scores: dict[str, float]):
                conn = sqlite3.connect(db_path)
                conn.execute("UPDATE docs SET rank_score = 0")
                for title, score in scores.items():
                    conn.execute(
                        "UPDATE docs SET rank_score = ? WHERE title = ?", (score, title)
                    )
                conn.commit()
                conn.close()
                return TestClient(app)

            yield with_scores
        finally:
            mp.undo()

    def titles_for(self, client, q: str) -> list[str]:
        return [r["title"] for r in client.get(
            "/v1/jawiki/search", params={"q": q, "limit": 10}
        ).json()["results"]]

    def test_popular_doc_outranks_equally_relevant_one(self, ranked_client):
        with ranked_client({}) as c:
            baseline = self.titles_for(c, "である")
        assert len(baseline) > 1
        underdog = baseline[-1]  # 素の bm25 では最下位の記事

        with ranked_client({underdog: 1.0}) as c:
            boosted = self.titles_for(c, "である")
        assert boosted.index(underdog) < baseline.index(underdog), (
            f"{underdog} が人気度で上がっていない: {baseline} -> {boosted}"
        )

    def test_out_of_range_rank_score_is_clamped(self, ranked_client):
        """geonames の古い DB は rank_score に人口の生値(数千万)が入っている。

        丸めずに使うとその 1 列だけで並びが決まるので、0〜1 に丸めていることを確かめる。
        丸めた結果は全件同じ倍率になり、並びは bm25 のみのときと一致する。
        """
        with ranked_client({}) as c:
            baseline = self.titles_for(c, "である")
        with ranked_client({t: 30_000_000.0 for t in baseline}) as c:
            clamped = self.titles_for(c, "である")
        assert clamped == baseline

    def test_negative_rank_score_does_not_flip_the_order(self, ranked_client):
        with ranked_client({}) as c:
            baseline = self.titles_for(c, "である")
        with ranked_client({baseline[0]: -5.0}) as c:
            assert self.titles_for(c, "である") == baseline


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
        res = client.get("/search/jawiki/doc/1")
        assert 'href="/search/jawiki/?tag=%E9%96%A2%E6%9D%B1%E5%9C%B0%E6%96%B9"' in res.text
        listing = client.get("/search/jawiki/", params={"tag": "日本の都道府県"})
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

    def test_apple_touch_icon_served_as_png(self, client):
        """iPhone の「ホーム画面に追加」用。iOS は data URI のファビコンを使わないため、
        PNG を固定パスで配信し、page_shell が <link rel="apple-touch-icon"> で参照する。"""
        res = client.get("/apple-touch-icon.png")
        assert res.status_code == 200
        assert res.headers["content-type"] == "image/png"
        assert res.content.startswith(b"\x89PNG\r\n\x1a\n")

    def test_pages_link_apple_touch_icon(self, client):
        res = client.get("/admin")
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in res.text

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

    def test_admin_shows_latest_schema_version(self, client):
        from app.registry import SUPPORTED_SCHEMA_VERSIONS

        res = client.get("/admin")
        assert f"最新のスキーマバージョン: {max(SUPPORTED_SCHEMA_VERSIONS)}" in res.text

    def test_admin_marks_stale_schema_version(self, client, monkeypatch):
        """最新より古い DB の行には最新版への注意書きが付く(最新なら付かない)。"""
        from app.registry import SUPPORTED_SCHEMA_VERSIONS

        latest = max(SUPPORTED_SCHEMA_VERSIONS)
        assert f"(最新: {latest})" not in client.get("/admin").text
        monkeypatch.setattr(client.app.state.sources["jawiki"], "schema_version", 1)
        assert f"(最新: {latest})" in client.get("/admin").text

    def test_admin_shows_rebuild_button_for_registered_source(self, client):
        res = client.get("/admin")
        assert 'action="/admin/rebuild/jawiki"' in res.text

    def test_admin_rebuild_without_trigger_configured(self, client):
        res = client.post("/admin/rebuild/jawiki")
        assert res.status_code == 503

    def test_admin_rebuild_unregistered_source(self, client, monkeypatch):
        """未登録ソースの再構築は断る(初期化は /admin/init 側の担当)。"""
        monkeypatch.setattr("app.main.TRIGGER_URL", "http://example.invalid")
        res = client.post("/admin/rebuild/osm_japan")
        assert res.status_code == 404

    def test_admin_rebuild_proxies_to_trigger(self, client, monkeypatch):
        monkeypatch.setattr("app.main.TRIGGER_URL", "http://trigger.internal")
        calls = []

        class FakeResponse:
            status_code = 202

            def json(self):
                return {"status": "started", "source": "jawiki"}

        def fake_post(url, timeout):
            calls.append(url)
            return FakeResponse()

        monkeypatch.setattr("app.main.httpx.post", fake_post)
        res = client.post("/admin/rebuild/jawiki", follow_redirects=False)
        assert res.status_code == 303
        assert res.headers["location"] == "/admin"
        assert calls == ["http://trigger.internal/run/jawiki"]


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

    def test_config_txt_documents_the_links_pitfalls(self, client):
        """links の例示には、素直に使うと外す 3 点(出リンクのみ・重複・節付き)を添える。

        これが無いと「被リンクも取れる」前提の依頼を組み立てたり、`記事名#節名` を
        そのまま doc に渡して 404 を踏んだりする。
        """
        text = client.get("/admin/claude-config.txt").text
        assert "/v1/jawiki/links" in text
        assert "被リンク(この記事を指している記事)は取れない" in text
        assert "`#` の前で切ること" in text

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

    def test_mcp_json_returns_server_entry(self, client):
        """MCP 登録断片(.mcp.json の中身)。URL はアクセス元から導出した <base>/mcp。"""
        res = client.get(
            "/admin/claude-config.mcp.json", headers={"Host": "192.168.1.10:9000"}
        )
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/json")
        assert res.json() == {
            "mcpServers": {
                "chiezo": {"type": "http", "url": "http://192.168.1.10:9000/mcp"}
            }
        }

    def test_config_txt_mentions_mcp_only_with_mcp(self, client):
        """MCP の使い分けの指示は ?mcp=1 のときだけ出す。

        登録はスクリプト側の既定なので通常は付いてくるが、`--no-mcp` の環境に
        「MCP ツールを優先」と書くと嘘になるため、API はパラメータで受け取る。
        """
        plain = client.get("/admin/claude-config.txt").text
        assert "mcp__chiezo__" not in plain
        assert "--with-mcp" not in plain

        withmcp = client.get("/admin/claude-config.txt?mcp=1").text
        assert "mcp__chiezo__" in withmcp
        assert "単発の参照は MCP ツールを優先" in withmcp
        # 再生成の案内にフラグが引き継がれる
        assert "--with-mcp" in withmcp
        # それ以外は同じブロック
        assert withmcp.startswith("<!-- BEGIN chiezo (auto-generated) -->")
        assert "- **jawiki**" in withmcp

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
        # CLAUDE.md ブロック・権限ファイル・フック設定・MCP 登録を、それぞれのコピーボタン付きで出す
        assert 'id="config-block"' in res.text
        assert 'id="config-perms"' in res.text
        assert 'id="config-hook"' in res.text
        assert 'id="config-mcp"' in res.text
        assert 'id="copy-block"' in res.text
        assert 'id="copy-perms"' in res.text
        assert 'id="copy-hook"' in res.text
        assert 'id="copy-mcp"' in res.text
        assert "BEGIN chiezo" in res.text
        assert "permissions" in res.text
        assert "/admin/claude-config.hook.py" in res.text
        assert "/admin/claude-config.mcp.json" in res.text

    def test_config_html_puts_defaults_before_optional_sections(self, client):
        """既定で入る設定(CLAUDE.md ブロック → MCP → 権限)を先に、任意のフックを後に置く。

        画面を上から読んだ順が「既定で何が入るか」になるようにするための並び。
        """
        text = client.get("/admin/claude-config").text
        order = [text.index(f'id="config-{n}"') for n in ("block", "mcp", "perms", "hook")]
        assert order == sorted(order), "既定の設定が任意のフックより後に来ている"

    def test_config_html_previews_the_script_defaults(self, client):
        """プレビューはスクリプトの既定と一致させる: MCP 登録は入り、フックは入らない。

        ここがずれると、管理画面で見た内容と実際に書き込まれる内容が食い違う。
        """
        text = client.get("/admin/claude-config").text
        assert "mcp__chiezo__" in text          # MCP は既定で登録するので使い分けが載る
        assert "許可プロンプトは出ない" not in text  # フックは --with-hook のときだけ

    def test_admin_links_to_config_page(self, client):
        res = client.get("/admin")
        assert '/admin/claude-config' in res.text


class TestBrowsePages:
    def test_browse_source_top_shows_search_form_only(self, client):
        res = client.get("/search/jawiki/")
        assert res.status_code == 200
        assert "<form" in res.text
        assert "浅草寺" not in res.text

    def test_browse_source_search(self, client):
        res = client.get("/search/jawiki/", params={"q": "浅草寺"})
        assert res.status_code == 200
        assert "浅草寺" in res.text

    def test_browse_source_unknown(self, client):
        res = client.get("/nosuch/")
        assert res.status_code == 404

    def test_browse_doc(self, client):
        res = client.get("/search/jawiki/doc/2")
        assert res.status_code == 200
        assert "浅草寺" in res.text

    def test_browse_doc_not_found(self, client):
        res = client.get("/search/jawiki/doc/424242")
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
        assert "?tag=" not in client.get("/search/jawiki/doc/1").text

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
            assert version == 4
            rows = conn.execute(
                "SELECT doc_id FROM doc_tags WHERE tag = '日本の都道府県'"
            ).fetchall()
            assert sorted(r[0] for r in rows) == [1, 10]
            # 4 の分(tag_counts)も同じ実行で作られ、doc_tags と一致する
            (docs,) = conn.execute(
                "SELECT docs FROM tag_counts WHERE tag = '日本の都道府県'"
            ).fetchone()
            assert docs == 2
            mismatch = conn.execute(
                "SELECT tag FROM tag_counts WHERE docs <>"
                " (SELECT COUNT(DISTINCT doc_id) FROM doc_tags WHERE doc_tags.tag = tag_counts.tag)"
            ).fetchall()
            assert mismatch == []
        finally:
            conn.close()

        # 二度目は何もしない(冪等)
        again = subprocess.run(
            [sys.executable, str(script), str(db_path)], capture_output=True, text=True
        )
        assert again.returncode == 0
        assert "nothing to do" in again.stdout


class TestTagCountsFallback:
    """schema_version 3 の DB は tag_counts を持たない(遅いが今までどおり動く)。

    4 で足したのは doc_tags を畳んだ集計表だけで、答えは変わらない。移行前の DB を
    断らずに旧経路へ落とすようにしてあるので、両経路が同じ答えを返すことを固定する。
    """

    @pytest.fixture()
    def v3_client(self, tmp_path, built_data_dir):
        import shutil
        import sqlite3

        from fastapi.testclient import TestClient

        data_dir = tmp_path / "v3"
        data_dir.mkdir()
        db_path = data_dir / "jawiki.db"
        shutil.copy(built_data_dir / "jawiki.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DROP TABLE tag_counts")
        conn.execute("UPDATE meta SET schema_version = 3")
        conn.commit()
        conn.close()

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app

            with TestClient(app) as c:
                yield c
        finally:
            mp.undo()

    def test_tags_endpoint_matches_the_tag_counts_path(self, v3_client, client):
        for params in ({"limit": 3}, {"prefix": "日本の"}, {"contains": "地方"}, {"contains": "%"}):
            old = v3_client.get("/v1/jawiki/tags", params=params)
            new = client.get("/v1/jawiki/tags", params=params)
            assert old.status_code == 200, params
            assert old.json() == new.json(), params

    def test_tag_filter_total_matches(self, v3_client, client):
        for params in ({"tag": "日本の都道府県"}, {"tag": "日本の山,日本の合戦"}):
            old = v3_client.get("/v1/jawiki/filter", params=params)
            assert old.status_code == 200, params
            assert old.json() == client.get("/v1/jawiki/filter", params=params).json(), params

    def test_rank_index_is_not_named_on_v3(self, v3_client, monkeypatch):
        """3 の DB には idx_docs_rank が無いので、INDEXED BY を書いてはいけない。"""
        from app import main

        monkeypatch.setattr(main, "DOC_ROW_VS_INDEX_COST", 10**9)  # 常に使いたがる状態にする
        res = v3_client.get("/v1/jawiki/filter", params={"tag": "日本の都道府県"})
        assert res.status_code == 200
        assert res.json()["total"] == 2


class TestRankIndexPath:
    """大きいタグ向けの INDEXED BY 経路。並びも結果も既定の経路と同じでなければならない。

    経路の選択は本番規模の費用で決まるので、テスト用の小さな DB では選ばれない。
    費用比を振り切って経路だけ通し、両者が一致することを見る。
    """

    def test_matches_the_default_path(self, client, monkeypatch):
        from app import main

        params = {"tag": "日本の都道府県,日本の山", "limit": 3, "fields": "doc_id,title"}
        default = client.get("/v1/jawiki/filter", params=params).json()
        monkeypatch.setattr(main, "DOC_ROW_VS_INDEX_COST", 10**9)
        indexed = client.get("/v1/jawiki/filter", params=params).json()
        assert indexed == default
        assert default["results"], default  # 空同士の一致で通してしまわないため

    def test_switches_on_how_deep_the_page_is(self, built_data_dir):
        """浅い頁では索引を名指しし、末尾に近づいたら素直に docs を読む。

        索引経路の費用は総件数ではなく `doc_count * (offset+limit) / total` 件の走査で、
        頁が末尾に近づくと索引の端まで舐めることになる(336 件のタグの offset=300 が
        150 万件の全走査に落ちて 504 になっていた)。判定に offset が入っていることを
        ここで固定する。
        """
        import dataclasses

        from app import main
        from app.registry import scan_sources

        # app.state は他のテストの TestClient と共有なので、DB から直に読む。
        # 判定は文書数との比で決まるので、件数だけ本番(jawiki 150 万件)に差し替える。
        src = scan_sources(built_data_dir)["jawiki"]
        src = dataclasses.replace(src, doc_count=1_500_000)

        # 25 万件のタグ(「存命人物」)。上位を返す限り索引を数百件走るだけで済む
        assert main.rank_index_hint(src, total=250_000, need=50)
        assert main.rank_index_hint(src, total=250_000, need=200_000)  # 深い頁でもこちらが安い
        # 336 件のタグ(「日本のレストラン」)。索引側は該当が薄すぎて割に合わない
        assert main.rank_index_hint(src, total=336, need=50) == ""
        assert main.rank_index_hint(src, total=336, need=350) == ""  # 末尾 = 索引の全走査
        assert main.rank_index_hint(src, total=0, need=50) == ""

    def test_small_tag_can_be_paged_to_the_end(self, client):
        """末尾の頁でも取りこぼさない(1 リクエストで全件取れる)。"""
        whole = client.get("/v1/jawiki/filter", params={"tag": "日本の都道府県"}).json()
        assert whole["total"] == 2
        tail = client.get(
            "/v1/jawiki/filter", params={"tag": "日本の都道府県", "limit": 1, "offset": 1}
        ).json()
        assert tail["results"] == whole["results"][1:]
        # 総件数ぴったりの limit(索引経路だと端まで走ることになる形)
        exact = client.get(
            "/v1/jawiki/filter", params={"tag": "日本の都道府県", "limit": 2}
        ).json()
        assert exact["results"] == whole["results"]


class TestAutoReload:
    """ブルーグリーン切り替え(シンボリックリンク差し替え)を再起動なしで拾うこと。

    実運用では lifespan の常駐タスクが数秒ごとに refresh_sources を呼ぶ。テストでは
    時間待ちを避けて refresh_sources を直接呼び、検知(登録の差し替え)と
    接続の開き直し(db.get_connection の inode 確認)の両方を見る。
    """

    @staticmethod
    def _swap_symlink(data_dir, target_name):
        """ingest の switch_db と同じ手順(tmp リンク → アトミック rename)。"""
        tmp = data_dir / "jawiki.db.tmp"
        tmp.symlink_to(target_name)
        tmp.replace(data_dir / "jawiki.db")

    def test_get_connection_reopens_when_the_symlink_target_changes(self, tmp_path):
        import sqlite3

        from app import db

        for name, marker in (("a.db", "旧"), ("b.db", "新")):
            conn = sqlite3.connect(tmp_path / name)
            conn.execute("CREATE TABLE t (v TEXT)")
            conn.execute("INSERT INTO t VALUES (?)", (marker,))
            conn.commit()
            conn.close()
        link = tmp_path / "jawiki.db"
        link.symlink_to("a.db")

        try:
            first = db.get_connection(link)
            assert first is db.get_connection(link), "同じ実体の間はキャッシュが効くこと"
            self._swap_symlink(tmp_path, "b.db")
            second = db.get_connection(link)
            assert second is not first
            assert second.execute("SELECT v FROM t").fetchone()[0] == "新"
        finally:
            db.close_thread_connections()

    def test_symlink_swap_is_served_without_restart(self, tmp_path, built_data_dir):
        import shutil
        import sqlite3

        from fastapi.testclient import TestClient

        data_dir = tmp_path / "reload"
        data_dir.mkdir()
        shutil.copy(built_data_dir / "jawiki.db", data_dir / "jawiki-20260701.db")
        data_dir.joinpath("jawiki.db").symlink_to("jawiki-20260701.db")

        mp = pytest.MonkeyPatch()
        mp.setenv("CHIEZO_DATA_DIR", str(data_dir))
        try:
            from app.main import app, refresh_sources

            with TestClient(app) as c:
                assert c.get("/v1/sources").json()["sources"][0]["dump_date"] == "20260701"
                # 接続をキャッシュさせておく(旧世代を掴んだままにならないことを見るため)
                assert c.get("/v1/jawiki/doc", params={"title": "東京都"}).status_code == 200

                # 新世代を用意して切り替え(タイトルを 1 件だけ変えて世代を見分ける)
                new_gen = data_dir / "jawiki-20260801.db"
                shutil.copy(data_dir / "jawiki-20260701.db", new_gen)
                conn = sqlite3.connect(new_gen)
                conn.execute("UPDATE meta SET dump_date = '20260801'")
                conn.execute("UPDATE docs SET title = '新東京都' WHERE title = '東京都'")
                conn.commit()
                conn.close()
                self._swap_symlink(data_dir, new_gen.name)

                assert refresh_sources(app) is True
                assert refresh_sources(app) is False, "変化が無ければ走査しないこと"
                assert c.get("/v1/sources").json()["sources"][0]["dump_date"] == "20260801"
                assert c.get("/v1/jawiki/doc", params={"title": "新東京都"}).status_code == 200
        finally:
            mp.undo()


class TestHasLinks:
    """links を持つソースかの判定(claude_config._has_links)。"""

    @staticmethod
    def _src(path):
        from app.registry import Source

        return Source(
            name="dummy", kind="wikipedia", lang="ja", dump_date="20260701",
            schema_version=4, built_at="", doc_count=0, path=path,
        )

    def _build(self, path, rows, links_at=None):
        import sqlite3

        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE docs (doc_id INTEGER PRIMARY KEY, links TEXT)")
        conn.executemany(
            "INSERT INTO docs (doc_id, links) VALUES (?, ?)",
            [(i, '["x"]' if i == links_at else None) for i in range(rows)],
        )
        conn.commit()
        conn.close()

    def test_detects_links(self, tmp_path):
        from app import claude_config

        path = tmp_path / "with_links.db"
        self._build(path, 10, links_at=3)
        assert claude_config._has_links(self._src(path)) is True

    def test_reports_absence_without_scanning_the_whole_table(self, tmp_path, monkeypatch):
        """判定は先頭 _LINKS_SAMPLE_ROWS 行で打ち切る(全表を舐めない)。

        links は索引が無いので、1 件も無いソースほど全表スキャンが長引く
        (実測: geonames 1300 万件で 3.2 秒)。判定 1 個にその時間は払えないので、
        窓の外にしか links が無い DB では False になるのが仕様どおり。
        """
        from app import claude_config

        monkeypatch.setattr(claude_config, "_LINKS_SAMPLE_ROWS", 5)
        path = tmp_path / "late_links.db"
        self._build(path, 50, links_at=40)
        assert claude_config._has_links(self._src(path)) is False


class TestAttributeGuard:
    """持っていない属性での絞り込みは、0 件ではなく理由を返す。

    条件としては正しいのに必ず 0 件になる組み合わせ(wikipedia に area / feature)は、
    人にも分かりにくく、agent モードでは「絞り込みを付けたまま検索語だけ変えて空振り」を
    延々と繰り返す原因になっていた(実測)。
    """

    def test_area_on_a_wikipedia_source_is_400(self, client):
        res = client.get("/v1/jawiki/search", params={"q": "東京", "area": "東京都"})
        assert res.status_code == 400
        assert "has no feature/area" in res.json()["error"]
        assert "tag" in res.json()["hint"]

    def test_feature_on_a_wikipedia_source_is_400(self, client):
        res = client.get("/v1/jawiki/filter", params={"feature": "tourism=museum"})
        assert res.status_code == 400

    def test_tag_and_bbox_still_work(self, client):
        """wikipedia が持っている属性(タグ・座標)はそのまま引ける。"""
        assert client.get("/v1/jawiki/filter", params={"tag": "日本の都道府県"}).status_code == 200
        assert client.get(
            "/v1/jawiki/filter", params={"bbox": "34.0,134.0,36.0,140.0"}
        ).status_code == 200


class TestUrlLayout:
    """画面の URL は「前置きの下」に置く。

    以前はソース名をそのままルート直下(`/{source}/`)に置いていたため、ルートが
    キャッチオールになり、`ask` や `admin` という名前のソースを足せなかった
    (既存の画面に食われる)。逆に画面を足すときもソース名との衝突を気にする必要があった。
    """

    def test_source_pages_live_under_search(self, client):
        assert client.get("/search/jawiki/").status_code == 200
        assert client.get("/search/jawiki/doc/1").status_code == 200

    def test_root_is_no_longer_a_catch_all(self, client):
        """ソース名と同じ名前の画面を足せる = ルート直下が空いている。"""
        assert client.get("/jawiki/", follow_redirects=False).status_code == 404
        assert client.get("/ask", follow_redirects=False).status_code == 404

    def test_every_link_on_the_admin_page_points_at_the_new_layout(self, client):
        import re

        html = client.get("/admin").text
        stale = [
            href for href in re.findall(r'href="(/[^"]*)"', html)
            if not href.startswith(
                ("/admin", "/search/", "/localllm/", "/v1/", "/healthz", "/apple-touch-icon")
            )
        ]
        assert not stale, f"古い URL が残っている: {stale}"

    def test_no_response_builds_a_url_outside_the_new_layout(self, client):
        """応答に載る URL は全部 pages.browse_url / doc_url を通っていること。

        URL の前置きを足したとき、notes の応答だけ手組みのままで `/notes/doc/N` を
        返し続け、たどると 404 になっていた(実際に踏んだ)。組み立てを 1 か所に
        寄せてあっても、そこを通っていない場所が残っていないかは別に確かめる。
        """
        import re

        bodies = [
            client.get("/v1/jawiki/search", params={"q": "浅草寺"}).text,
            client.get("/v1/jawiki/filter", params={"tag": "日本の都道府県"}).text,
        ]
        for body in bodies:
            for url in re.findall(r'"url"\s*:\s*"(/[^"]*)"', body):
                assert url.startswith("/search/"), f"古い形の URL: {url}"

    def test_source_names_never_produce_an_empty_segment(self, client):
        """`/search//doc/4` のような空のソース名を作らない。"""
        from app.pages import browse_url, doc_url

        assert "//" not in doc_url("notes", 4)
        assert browse_url("notes").startswith("/search/notes/")

    def test_user_input_is_escaped_in_the_browse_pages(self, client):
        """URL に紛れ込んだスクリプトが、そのまま HTML に出ないこと。

        CodeQL が反射型 XSS として指摘したのはブラウズ画面で、原因は URL の組み立てに
        `urllib.parse.quote` しか通っていなかったこと。percent-encode は HTML の
        エスケープではないので、**HTML に埋めるところでは必ず esc() を通す**。
        """
        payload = '<script>alert(1)</script>'
        for params in ({"q": payload}, {"tag": payload}):
            res = client.get("/search/jawiki/", params=params)
            assert res.status_code == 200
            assert payload not in res.text, f"{params} が生のまま出ている"
            assert "&lt;script&gt;" in res.text

    def test_upstream_details_do_not_reach_the_client(self, client):
        """例外の文言・相手の応答本文は返さない(ログにだけ残す)。

        認証の無い画面から内部の構成(接続先ホスト名など)が読めてしまうため。
        """
        res = client.get("/search/nosuch/")
        assert res.status_code == 404
        assert "Traceback" not in res.text

    def test_the_doc_page_does_not_print_the_text_twice(self, client):
        """opening は body の冒頭の写しなので、本文と並べない。

        短いメモ(notes)では opening == body になり、同じ文章が 2 回出ていた。
        """
        res = client.get("/search/jawiki/doc/1")
        assert res.status_code == 200
        body = res.text.split('<pre class="doc-body">')[1].split("</pre>")[0]
        head = body.strip()[:40]
        assert head and body.count(head) == 1, "本文が 2 回出ている"
