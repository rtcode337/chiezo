"""geonames アダプタのテスト(ミニフィクスチャ E2E)。"""
import sqlite3

import main as ingest_main
import pytest
from conftest import make_geonames_test_adapter


def docs_by_title(adapter, dump):
    return {d.title: d for d in adapter.iter_docs(dump)}


class TestGeonamesAdapter:
    def test_yields_places_and_skips_roads(self, geonames_fixture_dump, monkeypatch):
        monkeypatch.delenv("GEONAMES_FEATURE_CLASSES", raising=False)
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = docs_by_title(adapter, geonames_fixture_dump)
        assert {"Paris", "Tokyo", "New York City", "London", "Galdhopiggen"} <= set(docs)
        # feature class R(道路)は既定では取り込まない
        assert "Some Road" not in docs

    def test_skips_rows_with_broken_coordinates(self, geonames_fixture_dump):
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        assert "Broken" not in docs_by_title(adapter, geonames_fixture_dump)

    def test_japanese_alias_from_alternate_names(self, geonames_fixture_dump, monkeypatch):
        """「パリ」で引けること = 全世界カバーの肝。"""
        monkeypatch.delenv("GEONAMES_ALT_LANGS", raising=False)
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = docs_by_title(adapter, geonames_fixture_dump)
        assert "パリ" in docs["Paris"].aliases
        assert "東京" in docs["Tokyo"].aliases
        assert "ニューヨーク" in docs["New York City"].aliases

    def test_excludes_languages_outside_the_default_set(self, geonames_fixture_dump, monkeypatch):
        monkeypatch.delenv("GEONAMES_ALT_LANGS", raising=False)
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        paris = docs_by_title(adapter, geonames_fixture_dump)["Paris"]
        assert "Париж" not in paris.aliases            # ru は既定の対象外
        assert not any(a.startswith("http") for a in paris.aliases)  # link は別名ではない

    def test_alt_langs_can_be_widened(self, geonames_fixture_dump, monkeypatch):
        monkeypatch.setenv("GEONAMES_ALT_LANGS", "*")
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        paris = docs_by_title(adapter, geonames_fixture_dump)["Paris"]
        assert "Париж" in paris.aliases

    def test_wikidata_id_is_captured(self, geonames_fixture_dump):
        """jawiki 側の extra.wikidata と突合するための Q 番号。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = docs_by_title(adapter, geonames_fixture_dump)
        assert docs["Paris"].extra["wikidata"] == "Q90"
        assert docs["Tokyo"].extra["wikidata"] == "Q1490"

    def test_extra_fields(self, geonames_fixture_dump):
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        paris = docs_by_title(adapter, geonames_fixture_dump)["Paris"]
        extra = paris.extra
        assert extra["lat"] == pytest.approx(48.85341)
        assert extra["lon"] == pytest.approx(2.3488)
        assert extra["feature"] == "P=PPLC"          # OSM と同じ key=value 形式
        assert extra["area"] == "Île-de-France"      # admin1 が引けていれば行政区名
        assert extra["country"] == "France"
        assert extra["country_code"] == "FR"
        assert extra["population"] == 2138551
        assert extra["timezone"] == "Europe/Paris"

    def test_falls_back_to_country_when_admin1_unknown(self, geonames_fixture_dump):
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        adapter._admin1_path = None  # admin1 が引けない状況
        assert docs_by_title(adapter, geonames_fixture_dump)["Paris"].extra["area"] == "France"

    def test_population_becomes_rank_score(self, geonames_fixture_dump):
        """人口は 0.0〜1.0 に正規化して rank_score に入れる。

        生の人口のままだと、API が bm25 に掛け合わせて並べるときに人口だけで順位が
        決まってしまう(rank_score は全ソース共通で 0〜1 という約束)。
        対数変換なので人口による大小関係そのものは変わらない。
        """
        import math

        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = docs_by_title(adapter, geonames_fixture_dump)
        assert docs["Paris"].rank_score == round(math.log10(1 + 2138551) / 10, 4)
        assert 0.0 < docs["Paris"].rank_score <= 1.0
        assert docs["Galdhopiggen"].rank_score == 0.0
        # 対数変換なので人口の大小関係は保たれる
        by_pop = sorted(docs.values(), key=lambda d: (d.extra or {}).get("population", 0))
        assert [d.rank_score for d in by_pop] == sorted(d.rank_score for d in by_pop)

    def test_opening_is_searchable_text(self, geonames_fixture_dump):
        """GeoNames は本文を持たないので、FTS が効くよう 1 行の説明を組み立てている。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        paris = docs_by_title(adapter, geonames_fixture_dump)["Paris"]
        assert "Île-de-France" in paris.opening and "France" in paris.opening
        assert "PPLC" in paris.opening
        assert paris.body

    def test_feature_classes_can_be_overridden(self, geonames_fixture_dump, monkeypatch):
        monkeypatch.setenv("GEONAMES_FEATURE_CLASSES", "PR")
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = docs_by_title(adapter, geonames_fixture_dump)
        assert "Some Road" in docs            # R を明示的に含めた
        assert "Galdhopiggen" not in docs     # T は外れる

    def test_works_without_alternate_names(self, geonames_fixture_dump):
        """補助ファイルが無くても本体だけで構築できること(縮退動作)。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        adapter._alt_path = None
        paris = docs_by_title(adapter, geonames_fixture_dump)["Paris"]
        assert paris.title == "Paris"
        assert "wikidata" not in paris.extra

    def test_temp_lookups_are_cleaned_up(self, geonames_fixture_dump):
        """中断・完走のいずれでも一時ファイルを残さない(lookup.py の作法)。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        list(adapter.iter_docs(geonames_fixture_dump))
        leftovers = list(geonames_fixture_dump.parent.glob("*.altnames.db"))
        leftovers += list(geonames_fixture_dump.parent.glob("*.wikidata.db"))
        assert leftovers == []


class TestDuplicateTitles:
    """同名地名の解決。`docs.title` に UNIQUE 制約があるため必須。

    GeoNames には同名が大量にある(Paris は仏/テキサス/オンタリオ…)。素朴に流すと
    全行 INSERT 後の `CREATE UNIQUE INDEX idx_docs_title` で落ちる。
    """

    def test_titles_are_unique(self, geonames_fixture_dump):
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        titles = [d.title for d in adapter.iter_docs(geonames_fixture_dump)]
        assert len(titles) == len(set(titles))

    def test_most_populous_keeps_the_plain_name(self, geonames_fixture_dump):
        """「Paris と言えばフランス」。ファイル順(geonameid 順)ではなく人口で決める。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = {d.doc_id: d for d in adapter.iter_docs(geonames_fixture_dump)}
        assert docs[2988507].title == "Paris"           # フランス(人口 213 万)
        assert docs[4717560].title != "Paris"           # テキサス(人口 2.5 万)
        assert docs[6942553].title != "Paris"           # オンタリオ(人口 1.2 万)

    def test_losers_are_disambiguated_and_keep_the_name_as_alias(self, geonames_fixture_dump):
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = {d.doc_id: d for d in adapter.iter_docs(geonames_fixture_dump)}
        texas = docs[4717560]
        assert texas.title == "Paris (US:4717560)"      # geonameid 込みなので必ず一意
        assert "Paris" in texas.aliases                 # 元の名前でも引ける

    def test_ties_are_broken_by_geoname_id(self, geonames_fixture_dump):
        """人口が同じ(0)なら geonameid が小さいほうを代表にする = 実行ごとにぶれない。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        docs = {d.doc_id: d for d in adapter.iter_docs(geonames_fixture_dump)}
        assert docs[7000010].title == "Springfield"
        assert docs[7000020].title == "Springfield (US:7000020)"

    def test_owner_lookup_is_cleaned_up(self, geonames_fixture_dump):
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        list(adapter.iter_docs(geonames_fixture_dump))
        assert list(geonames_fixture_dump.parent.glob("*.titleowners.db")) == []


class TestGeonamesBuild:
    def test_build_and_validate(self, geonames_fixture_dump, tmp_path):
        """共通フレームで DB を構築し、検証と全文検索が通ること。"""
        adapter = make_geonames_test_adapter(geonames_fixture_dump)
        building = tmp_path / "geonames-20260724.db.building"
        ingest_main.build_db(adapter, geonames_fixture_dump, "20260724", building)
        ingest_main.validate_db(adapter, building)

        conn = sqlite3.connect(f"file:{building}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        (count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        assert count == 9

        # docs.title は UNIQUE。同名地名があっても全行 INSERT 後の索引作成が通ること
        # (ここが通らないと本番で 1,100 万行入れた後に落ちる)
        (distinct,) = conn.execute("SELECT COUNT(DISTINCT title) FROM docs").fetchone()
        assert distinct == count

        # 日本語別名からの解決(aliases 経由)
        row = conn.execute(
            "SELECT d.title FROM aliases a JOIN docs d ON d.doc_id = a.doc_id WHERE a.alias = ?",
            ("パリ",),
        ).fetchone()
        assert row["title"] == "Paris"

        # schema v2 の生成列が extra から引けていること(filter が使える)
        row = conn.execute(
            "SELECT title, feature, area, lat, lon, wikidata FROM docs WHERE title = 'Paris'"
        ).fetchone()
        assert row["feature"] == "P=PPLC"
        assert row["area"] == "Île-de-France"
        assert row["wikidata"] == "Q90"
        assert row["lat"] == pytest.approx(48.85341)

        # 全文検索
        hits = conn.execute(
            "SELECT d.title FROM docs_fts f JOIN docs d ON d.doc_id = f.rowid"
            " WHERE docs_fts MATCH ? ORDER BY d.rank_score DESC",
            ('"France"',),
        ).fetchall()
        assert "Paris" in [h["title"] for h in hits]
        conn.close()

    def test_registered_in_adapter_registry(self):
        from sources import ADAPTERS, get_adapter

        assert "geonames" in ADAPTERS
        assert "osm_europe" not in ADAPTERS  # 大陸単位 OSM は廃止し geonames に置き換えた
        adapter = get_adapter("geonames")
        assert adapter.source_kind == "geonames"
        assert adapter.min_build_memory_gb <= 12.0
