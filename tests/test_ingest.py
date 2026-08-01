"""ingest 共通フレーム + wikipedia アダプタのテスト(フィクスチャ E2E)。"""
import bz2
import gzip
import json
import math
import sqlite3

import main as ingest_main
import pytest
from conftest import make_test_adapter

import core as ingest_core


def connect(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


class TestAdapter:
    def test_iter_docs_skips_non_zero_namespace(self, fixture_dump):
        adapter = make_test_adapter()
        docs = list(adapter.iter_docs(fixture_dump))
        assert len(docs) == 11  # フィクスチャは 11 記事 + リダイレクト 4 件 + 非 ns=0 2 件
        assert all(d.title != "ノート:東京都" for d in docs)

    def test_field_mapping(self, fixture_dump):
        adapter = make_test_adapter()
        docs = {d.title: d for d in adapter.iter_docs(fixture_dump)}
        tokyo = docs["東京都"]
        assert tokyo.doc_id == 1
        assert tokyo.opening.startswith("東京都は")
        assert "日本の都道府県" in tokyo.tags
        assert "日本" in tokyo.links
        assert tokyo.aliases == ["東京"]
        assert tokyo.updated_at == "2026-06-01T00:00:00Z"
        # ページビューを突合していなければ rank_score は 0(この fixture は突合なし)
        assert tokyo.rank_score == 0.0

    def test_non_zero_namespace_redirects_excluded(self, fixture_dump):
        adapter = make_test_adapter()
        docs = {d.title: d for d in adapter.iter_docs(fixture_dump)}
        # 浅草寺の redirect には ns=4 が混ざっているが aliases には含まれない
        assert docs["浅草寺"].aliases == ["金龍山浅草寺"]

    def test_hidden_section_table_content_included_in_body(self, fixture_dump):
        """{{hidden begin}}/{{hidden end}} で囲まれた表の内容が body に含まれることの回帰テスト。

        CirrusSearch ダンプの text フィールドはこの種の折りたたみセクションを検索
        インデックスから除外していた(ブラタモリの放送回一覧表が欠落していた実例と同型)。
        XML ダンプ + wikitext 解析への切り替えでここが解消されたことを確認する。
        """
        adapter = make_test_adapter()
        docs = {d.title: d for d in adapter.iter_docs(fixture_dump)}
        assert "柴犬" in docs["犬"].body
        assert "プードル" in docs["犬"].body


class TestWikidataIds:
    """page_props ダンプ(記事 → wikidata の Q 番号)の取り込み。"""

    @staticmethod
    def _write_page_props(tmp_path, statements: str):
        path = tmp_path / "jawiki-20260701-page_props.sql.gz"
        with gzip.open(path, "wt", encoding="utf-8") as f:
            f.write("-- MySQL dump\n")
            f.write(statements)
        return path

    def test_parses_only_wikibase_item_rows(self, tmp_path):
        adapter = make_test_adapter()
        # 1 行に複数タプルが並ぶ実ダンプの形。wikibase_item 以外の prop は無視される。
        adapter._page_props_path = self._write_page_props(
            tmp_path,
            "INSERT INTO `page_props` VALUES "
            "(1,'wikibase_item','Q1490',NULL),"
            "(1,'defaultsort','とうきょうと',NULL),"
            "(2,'page_image_free','Sensoji.jpg',NULL),"
            "(2,'wikibase_item','Q188206',NULL);\n",
        )
        with adapter._load_page_props() as props:
            assert len(props) == 2
            assert props.get(1) == "Q1490"
            assert props.get(2) == "Q188206"
            assert props.get(3) is None  # defaultsort / page_image_free は拾わない

    def test_no_dump_means_no_ids(self):
        # ダンプ未取得ならヌルオブジェクト(ディスク上の一時 DB も作らない)
        props = make_test_adapter()._load_page_props()
        assert len(props) == 0
        assert props.get(1) is None

    def test_ids_land_in_extra(self, tmp_path, fixture_dump):
        adapter = make_test_adapter()
        adapter._page_props_path = self._write_page_props(
            tmp_path, "INSERT INTO `page_props` VALUES (1,'wikibase_item','Q1490',NULL);\n"
        )
        docs = {d.title: d for d in adapter.iter_docs(fixture_dump)}
        assert docs["東京都"].extra["wikidata"] == "Q1490"
        # 対応が無い記事は extra ごと None のまま(既存の振る舞いを変えない)
        assert docs["浅草寺"].extra is None

    def test_lookup_temp_files_are_cleaned_up(self, tmp_path, fixture_dump):
        # 対応表の一時 SQLite は取り込み後(および中断時)に残さない。
        adapter = make_test_adapter()
        adapter._page_props_path = self._write_page_props(
            tmp_path, "INSERT INTO `page_props` VALUES (1,'wikibase_item','Q1490',NULL);\n"
        )
        dirs = {fixture_dump.parent, tmp_path}  # redirects は本体の隣、lookup は page_props の隣

        def temp_files():
            out = []
            for d in dirs:
                out += list(d.glob("*.lookup.db")) + list(d.glob("*.redirects.db"))
            return out

        gen = adapter.iter_docs(fixture_dump)
        next(gen)  # パス1(リダイレクト収集)と対応表構築を走らせる
        assert temp_files(), "取り込み中は一時 DB が存在する"
        gen.close()  # ジェネレータを中断 → finally が走る
        assert not temp_files(), "中断後に一時ファイルが残っている"


class TestPageviewRanking:
    """月間ページビューを rank_score(0〜1 の知名度)に写すこと。

    XML ダンプには CirrusSearch の popularity_score 相当が無く、以前は rank_score が
    全記事 0.0 だった。検索の並びで人気度が一切効かず、2 文字語のように数千件ヒットする
    問い合わせで有名な記事が埋もれる原因になっていた。
    """

    @staticmethod
    def _write_pageviews(tmp_path, lines: str):
        path = tmp_path / "pageviews-2026-06.bz2"
        with bz2.open(path, "wt", encoding="utf-8") as f:
            f.write(lines)
        return path

    def test_pageviews_become_rank_score(self, tmp_path, fixture_dump):
        adapter = make_test_adapter()
        # 実ダンプの形: <domain> <title> <page_id> <access> <count> <...>
        adapter._pageview_path = self._write_pageviews(
            tmp_path,
            "ja.wikipedia 東京都 1 desktop 900000 X\n"
            "ja.wikipedia 東京都 1 mobile-web 100000 X\n"
            "ja.wikipedia 浅草寺 2 desktop 100 X\n",
        )
        adapter._pageview_period = "2026-06"
        docs = {d.title: d for d in adapter.iter_docs(fixture_dump)}

        # アクセス種別は合算される(90 万 + 10 万 = 100 万)
        assert docs["東京都"].extra["pageviews_month"] == 1_000_000
        assert docs["東京都"].rank_score == round(math.log10(1_000_001) / 7, 4)
        assert docs["浅草寺"].rank_score == round(math.log10(101) / 7, 4)
        # 突合できなかった記事は 0
        assert docs["富士山"].rank_score == 0.0

    def test_rank_score_stays_within_zero_to_one(self, tmp_path, fixture_dump):
        """API が bm25 に掛け合わせるので、この範囲を外れると並びが壊れる。"""
        adapter = make_test_adapter()
        adapter._pageview_path = self._write_pageviews(
            tmp_path, "ja.wikipedia 東京都 1 desktop 999999999 X\n"
        )
        adapter._pageview_period = "2026-06"
        docs = {d.title: d for d in adapter.iter_docs(fixture_dump)}
        assert docs["東京都"].rank_score == 1.0
        assert all(0.0 <= d.rank_score <= 1.0 for d in docs.values())


class TestBuild:
    def test_built_db_contents(self, built_data_dir):
        conn = connect(built_data_dir / "jawiki.db")
        try:
            meta = conn.execute("SELECT * FROM meta").fetchone()
            assert meta["source"] == "jawiki"
            assert meta["source_kind"] == "wikipedia"
            assert meta["lang"] == "ja"
            assert meta["dump_date"] == "20260701"
            assert meta["schema_version"] == 4

            (count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
            assert count == 11

            row = conn.execute("SELECT * FROM docs WHERE title = '浅草寺'").fetchone()
            assert json.loads(row["tags"]) == ["東京都の寺", "台東区"]
            assert "雷門" in json.loads(row["links"])

            aliases = dict(conn.execute("SELECT alias, doc_id FROM aliases").fetchall())
            assert aliases["東京"] == 1
            assert aliases["金龍山浅草寺"] == 2
            assert "Wikipedia:浅草寺関連" not in aliases
        finally:
            conn.close()

    def test_doc_tags_is_expanded_from_docs_tags(self, built_data_dir):
        conn = connect(built_data_dir / "jawiki.db")
        try:
            rows = conn.execute(
                "SELECT d.title FROM doc_tags t JOIN docs d ON d.doc_id = t.doc_id"
                " WHERE t.tag = '日本の都道府県' ORDER BY d.title"
            ).fetchall()
            assert [r["title"] for r in rows] == ["大阪府", "東京都"]

            # ソートキー付きのカテゴリ(`[[Category:日本の小説家|なつめ そうせき]]`)。
            # 本文からはカテゴリ名が消えるが、tags 経由なので doc_tags には入る。
            (n,) = conn.execute(
                "SELECT COUNT(*) FROM doc_tags WHERE tag = '日本の小説家'"
            ).fetchone()
            assert n == 1
            body = conn.execute(
                "SELECT body FROM docs WHERE title = '夏目漱石'"
            ).fetchone()["body"]
            assert "Category:日本の小説家" not in body, "本文にはソートキーしか残らない"
            assert "なつめ そうせき" in body

            # 同じ (tag, doc_id) が二重に入らない
            (dupes,) = conn.execute(
                "SELECT COUNT(*) FROM (SELECT tag, doc_id FROM doc_tags"
                " GROUP BY tag, doc_id HAVING COUNT(*) > 1)"
            ).fetchone()
            assert dupes == 0
        finally:
            conn.close()

    def test_validation_fails_when_doc_tags_is_empty(self, tmp_path, built_data_dir):
        import shutil

        db_path = tmp_path / "broken.db"
        shutil.copy(built_data_dir / "jawiki.db", db_path)
        conn = sqlite3.connect(db_path)
        conn.execute("DELETE FROM doc_tags")
        conn.commit()
        conn.close()
        with pytest.raises(RuntimeError, match="doc_tags is empty"):
            ingest_main.validate_db(make_test_adapter(), db_path)

    def test_fts_search_works(self, built_data_dir):
        conn = connect(built_data_dir / "jawiki.db")
        try:
            rows = conn.execute(
                "SELECT d.title FROM docs_fts JOIN docs d ON d.doc_id = docs_fts.rowid"
                " WHERE docs_fts MATCH '\"仲見世通り\"'"
            ).fetchall()
            assert [r["title"] for r in rows] == ["浅草寺"]
        finally:
            conn.close()

    def test_validation_fails_on_missing_sample(self, tmp_path, fixture_dump):
        from sources.wikipedia import WikipediaAdapter

        adapter = WikipediaAdapter(
            "jawiki", lang="ja", min_docs=5, sample_titles=["存在しない記事"]
        )
        building = tmp_path / "jawiki-20260701.db.building"
        ingest_main.build_db(adapter, fixture_dump, "20260701", building)
        with pytest.raises(RuntimeError, match="sample title not found"):
            ingest_main.validate_db(adapter, building)

    def test_validation_fails_on_min_docs(self, tmp_path, fixture_dump):
        from sources.wikipedia import WikipediaAdapter

        adapter = WikipediaAdapter("jawiki", lang="ja", min_docs=1000, sample_titles=[])
        building = tmp_path / "jawiki-1.db.building"
        ingest_main.build_db(adapter, fixture_dump, "1", building)
        with pytest.raises(RuntimeError, match="only 11 docs"):
            ingest_main.validate_db(adapter, building)


class TestSwitch:
    def test_symlink_points_to_new_generation(self, built_data_dir):
        link = built_data_dir / "jawiki.db"
        assert link.is_symlink()
        assert link.resolve().name == "jawiki-20260701.db"

    def test_blue_green_keeps_one_old_generation(self, tmp_path, fixture_dump):
        adapter = make_test_adapter()
        for date in ("20260701", "20260708", "20260715"):
            building = tmp_path / f"jawiki-{date}.db.building"
            ingest_main.build_db(adapter, fixture_dump, date, building)
            ingest_main.switch_db(tmp_path, "jawiki", date, building)

        link = tmp_path / "jawiki.db"
        assert link.resolve().name == "jawiki-20260715.db"
        generations = sorted(p.name for p in tmp_path.glob("jawiki-*.db"))
        # 最新 + 直前の 1 世代のみ保持
        assert generations == ["jawiki-20260708.db", "jawiki-20260715.db"]

    def test_rebuild_does_not_corrupt_live_db(self, tmp_path, fixture_dump):
        """中断・再実行しても運用 DB (シンボリックリンク先) は壊れない。"""
        adapter = make_test_adapter()
        building = tmp_path / "jawiki-20260701.db.building"
        ingest_main.build_db(adapter, fixture_dump, "20260701", building)
        ingest_main.switch_db(tmp_path, "jawiki", "20260701", building)

        # 同じ日付で再構築(前回中断からのやり直しを想定)
        building2 = tmp_path / "jawiki-20260701.db.building"
        ingest_main.build_db(adapter, fixture_dump, "20260701", building2)
        # .building がある間も運用 DB は読める
        conn = connect(tmp_path / "jawiki.db")
        (count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        conn.close()
        assert count == 11
        ingest_main.switch_db(tmp_path, "jawiki", "20260701", building2)
        assert (tmp_path / "jawiki.db").resolve().name == "jawiki-20260701.db"


class TestBuildMemoryPreflight:
    """構築前のメモリ検査(足りなければ数時間かける前に中止する)。"""

    class _Adapter:
        source = "dummy"
        min_build_memory_gb = 8.0

    def test_aborts_when_memory_is_short(self, monkeypatch):
        monkeypatch.delenv("SKIP_MEMORY_CHECK", raising=False)
        monkeypatch.delenv("BUILD_MEMORY_GB", raising=False)
        monkeypatch.setattr(ingest_main, "available_memory_bytes", lambda: 2 * ingest_main.GIB)
        with pytest.raises(SystemExit) as excinfo:
            ingest_main.require_build_memory(self._Adapter())
        message = str(excinfo.value)
        assert "not enough memory" in message
        assert "2.0 GiB available" in message and "8.0 GiB required" in message

    def test_proceeds_when_memory_is_sufficient(self, monkeypatch):
        monkeypatch.delenv("SKIP_MEMORY_CHECK", raising=False)
        monkeypatch.delenv("BUILD_MEMORY_GB", raising=False)
        monkeypatch.setattr(ingest_main, "available_memory_bytes", lambda: 16 * ingest_main.GIB)
        ingest_main.require_build_memory(self._Adapter())  # 例外が出なければ通過

    def test_skip_flag_bypasses_check(self, monkeypatch):
        monkeypatch.setenv("SKIP_MEMORY_CHECK", "1")
        monkeypatch.setattr(ingest_main, "available_memory_bytes", lambda: 1 * ingest_main.GIB)
        ingest_main.require_build_memory(self._Adapter())

    def test_required_amount_can_be_overridden(self, monkeypatch):
        monkeypatch.delenv("SKIP_MEMORY_CHECK", raising=False)
        monkeypatch.setenv("BUILD_MEMORY_GB", "1")
        monkeypatch.setattr(ingest_main, "available_memory_bytes", lambda: 2 * ingest_main.GIB)
        ingest_main.require_build_memory(self._Adapter())

    def test_unknown_memory_does_not_block_build(self, monkeypatch):
        monkeypatch.delenv("SKIP_MEMORY_CHECK", raising=False)
        monkeypatch.delenv("BUILD_MEMORY_GB", raising=False)
        monkeypatch.setattr(ingest_main, "available_memory_bytes", lambda: None)
        ingest_main.require_build_memory(self._Adapter())

    def test_osm_requirement_drops_with_disk_backed_index(self, monkeypatch):
        from sources import get_adapter

        monkeypatch.delenv("OSM_NODE_INDEX", raising=False)
        monkeypatch.setenv("BUILD_PROFILE", "fast")
        assert get_adapter("osm_japan").min_build_memory_gb == 12.0
        monkeypatch.setenv("OSM_NODE_INDEX", "sparse_file_array")
        assert get_adapter("osm_japan").min_build_memory_gb == 2.0

    def test_source_can_default_to_disk_index(self, monkeypatch):
        """大陸規模のような RAM 索引が成立しないソース向けに、fast でも既定をディスクにできること。"""
        from sources.osm import OsmAdapter

        monkeypatch.delenv("OSM_NODE_INDEX", raising=False)
        monkeypatch.setenv("BUILD_PROFILE", "fast")
        huge = OsmAdapter("osm_huge", region="europe", default_node_index="sparse_file_array")
        assert huge.node_index_kind == "sparse_file_array"
        assert huge.min_build_memory_gb == 2.0

    def test_env_overrides_per_source_default(self, monkeypatch):
        from sources.osm import OsmAdapter

        huge = OsmAdapter("osm_huge", region="europe", default_node_index="sparse_file_array")
        monkeypatch.setenv("OSM_NODE_INDEX", "sparse_mmap_array")
        assert huge.node_index_kind == "sparse_mmap_array"  # 明示指定なら RAM 索引も選べる

    def test_no_source_requires_more_than_12gb_in_fast_profile(self, monkeypatch):
        """BUILD_PROFILE=fast でも、どのソースも 12GiB のマシンで構築できること。"""
        from sources import ADAPTERS, get_adapter

        monkeypatch.delenv("OSM_NODE_INDEX", raising=False)
        monkeypatch.setenv("BUILD_PROFILE", "fast")
        for name in ADAPTERS:
            assert get_adapter(name).min_build_memory_gb <= 12.0, name

    def test_low_memory_profile_fits_every_source_in_2gb(self, monkeypatch):
        """既定(low_memory)では、どのソースも 2GiB のマシンで構築できること。

        「fast で 12GiB 以内」と並ぶ不変条件。本番(配信)サーバも開発機もメモリ 2GiB 級
        という運用のための既定なので、これを超える宣言のソースを増やさないこと。
        """
        from sources import ADAPTERS, get_adapter

        monkeypatch.delenv("OSM_NODE_INDEX", raising=False)
        monkeypatch.delenv("BUILD_PROFILE", raising=False)  # 未指定 = low_memory が既定
        for name in ADAPTERS:
            assert get_adapter(name).min_build_memory_gb <= 2.0, name

    def test_explicit_node_index_beats_low_memory_profile(self, monkeypatch):
        """既定(low_memory)のままでも、OSM_NODE_INDEX の明示指定で RAM 索引に戻せること。"""
        from sources import get_adapter

        monkeypatch.delenv("BUILD_PROFILE", raising=False)
        monkeypatch.setenv("OSM_NODE_INDEX", "sparse_mmap_array")
        adapter = get_adapter("osm_japan")
        assert adapter.node_index_kind == "sparse_mmap_array"
        assert adapter.min_build_memory_gb == 12.0

    def test_unknown_build_profile_aborts(self, monkeypatch):
        """綴り間違い(low-memory 等)を既定に黙って倒さないこと。"""
        monkeypatch.setenv("BUILD_PROFILE", "low-memory")
        with pytest.raises(SystemExit) as excinfo:
            ingest_core.build_profile()
        assert "BUILD_PROFILE" in str(excinfo.value)

    def test_build_cache_follows_profile(self, monkeypatch):
        """構築用 SQLite キャッシュがプロファイルに追従すること(既定 64MiB / fast 512MiB)。"""
        monkeypatch.delenv("BUILD_PROFILE", raising=False)
        assert "PRAGMA cache_size=-65536" in ingest_main.build_pragmas()
        monkeypatch.setenv("BUILD_PROFILE", "fast")
        assert "PRAGMA cache_size=-524288" in ingest_main.build_pragmas()


class TestOsmRegionCatalog:
    """Geofabrik の国別抽出カタログ(自動生成)とアダプタ登録の整合。"""

    def test_all_countries_are_registered(self):
        from sources import ADAPTERS
        from sources.osm_regions import OSM_REGIONS

        osm_names = [n for n in ADAPTERS if n.startswith("osm_")]
        assert len(osm_names) == len(OSM_REGIONS)
        assert "osm_japan" in osm_names and "osm_france" in osm_names

    def test_source_names_use_underscores(self):
        """ハイフンは世代ファイル名 <source>-<date>.db の区切りと衝突するため使わない。"""
        from sources.osm_regions import OSM_REGIONS

        for region in OSM_REGIONS.values():
            assert "-" not in region.source, region.source
            assert region.source == "osm_" + region.slug.replace("-", "_")

    def test_adapter_carries_region_and_lang(self):
        from sources import get_adapter

        adapter = get_adapter("osm_france")
        assert adapter.region == "europe/france"
        assert adapter.lang == "fr"
        assert adapter.source_kind == "osm"

    def test_large_countries_default_to_disk_index(self, monkeypatch):
        """RAM 索引が 12GiB に収まらない国は、fast でもディスク索引を既定にして構築可能に保つ。"""
        from sources import get_adapter
        from sources.osm_regions import OSM_REGIONS

        monkeypatch.delenv("OSM_NODE_INDEX", raising=False)
        monkeypatch.setenv("BUILD_PROFILE", "fast")
        for region in OSM_REGIONS.values():
            expected = "sparse_mmap_array" if region.memory_gb <= 12 else "sparse_file_array"
            assert region.node_index == expected, region.source
        assert get_adapter("osm_us").node_index_kind == "sparse_file_array"
        assert get_adapter("osm_japan").node_index_kind == "sparse_mmap_array"

    def test_explicit_validation_wins_over_catalog(self):
        """osm_japan の手厚い検証(サンプルタイトル)をカタログの機械的な下限で潰さない。"""
        from sources import get_adapter

        japan = get_adapter("osm_japan")
        assert japan.min_docs == 50_000
        assert "東京駅" in japan.sample_titles


class TestWikipediaEditionCatalog:
    """Wikipedia 言語版カタログ(自動生成)とアダプタ登録の整合。"""

    def test_all_editions_are_registered(self):
        from sources import ADAPTERS
        from sources.wikipedia_editions import WIKIPEDIA_EDITIONS

        for wiki_id in WIKIPEDIA_EDITIONS:
            assert wiki_id in ADAPTERS, wiki_id
        assert "enwiki" in ADAPTERS and "dewiki" in ADAPTERS

    def test_source_names_use_underscores(self):
        """ハイフンは世代ファイル名 <source>-<date>.db の区切りと衝突するため使わない。"""
        from sources.wikipedia_editions import WIKIPEDIA_EDITIONS

        for edition in WIKIPEDIA_EDITIONS.values():
            assert "-" not in edition.wiki_id, edition.wiki_id

    def test_adapter_carries_lang_and_min_docs(self):
        from sources import get_adapter
        from sources.wikipedia_editions import WIKIPEDIA_EDITIONS

        adapter = get_adapter("dewiki")
        assert adapter.source_kind == "wikipedia"
        assert adapter.lang == "de"
        assert adapter.min_docs == WIKIPEDIA_EDITIONS["dewiki"].min_docs
        assert adapter.min_docs > 100_000
        assert adapter.sample_titles == []

    def test_pageview_domain_derived_from_url_lang_code(self):
        """lang は URL のサブドメインなので、pageview ドメインを機械的に導出できる。"""
        from sources import get_adapter

        assert get_adapter("dewiki").pageview_domain() == "de.wikipedia"
        # ハイフン付きの URL コードもそのままドメインコードになる
        assert get_adapter("zh_yuewiki").pageview_domain() == "zh-yue.wikipedia"
        # 明示の対応表(WIKI_DOMAIN)は導出より優先
        assert get_adapter("jawiki").pageview_domain() == "ja.wikipedia"

    def test_explicit_validation_wins_over_catalog(self):
        """jawiki / enwiki の手厚い検証(サンプルタイトル)をカタログの下限で潰さない。"""
        from sources import get_adapter

        jawiki = get_adapter("jawiki")
        assert jawiki.min_docs == 1_000_000
        assert "東京都" in jawiki.sample_titles
        enwiki = get_adapter("enwiki")
        assert enwiki.min_docs == 5_000_000
        assert "Tokyo" in enwiki.sample_titles


class TestTriggerCatalogEndpoint:
    """chiezo-trigger の GET /sources(管理画面の国選択が読むカタログ)。"""

    @pytest.fixture()
    def trigger_client(self):
        from fastapi.testclient import TestClient

        import server

        return TestClient(server.app)

    def test_lists_plain_and_osm_sources(self, trigger_client):
        body = trigger_client.get("/sources").json()
        catalog = body["sources"]
        assert catalog["geonames"] == {"kind": "geonames", "lang": None}
        assert catalog["osm_japan"]["group"] == "osm"
        assert catalog["osm_japan"]["region"] == "asia/japan"
        assert catalog["osm_japan"]["label"] == "日本"
        assert "asia" in body["continents"]

    def test_lists_wikipedia_editions_with_group(self, trigger_client):
        """<lang>wiki は group="wikipedia" 付きで出る(管理画面の言語選択が読む)。"""
        catalog = trigger_client.get("/sources").json()["sources"]
        assert catalog["jawiki"]["group"] == "wikipedia"
        assert catalog["jawiki"]["label"] == "日本語"
        assert catalog["enwiki"]["label"] == "英語"
        assert catalog["enwiki"]["articles"] > 1_000_000
        assert catalog["zh_yuewiki"]["lang"] == "zh-yue"

    def test_matches_the_adapter_registry(self, trigger_client):
        from sources import ADAPTERS

        assert set(trigger_client.get("/sources").json()["sources"]) == set(ADAPTERS)

    def test_reports_schema_version(self, trigger_client):
        """このイメージが焼くスキーマバージョンをカタログと一緒に返す
        (管理画面の「最新のスキーマバージョン」表示の正)。"""
        from core import SCHEMA_VERSION

        assert trigger_client.get("/sources").json()["schema_version"] == SCHEMA_VERSION
