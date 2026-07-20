"""ingest 共通フレーム + wikipedia アダプタのテスト(フィクスチャ E2E)。"""
import json
import sqlite3

import pytest

import main as ingest_main
from conftest import make_test_adapter


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
        assert tokyo.rank_score == 0.0  # XML ダンプには popularity_score 相当が無いため固定値

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


class TestBuild:
    def test_built_db_contents(self, built_data_dir):
        conn = connect(built_data_dir / "jawiki.db")
        try:
            meta = conn.execute("SELECT * FROM meta").fetchone()
            assert meta["source"] == "jawiki"
            assert meta["source_kind"] == "wikipedia"
            assert meta["lang"] == "ja"
            assert meta["dump_date"] == "20260701"
            assert meta["schema_version"] == 1

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
