"""収集層(app/collect.py)のテスト。

**押さえているのは、この層でしか起きない 4 つ**:
①追記していく(全件の作り直しをしない)、②同じ見出しは飛ばす、
③実行ごとにカーソルが進む、④失敗しても次回の予定が入る。

溜め先が notes と別のソースになること(混ざらないこと)も見る —— これを崩すと
短期記憶が収集物で埋まって `recall` が使い物にならなくなる、という設計の芯。
"""
from __future__ import annotations

import datetime as dt

import pytest

from app import collect


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """置き場を用意して、**追記される DB を mutable に登録する**。

    `db.query` は登録が無いと `immutable=1` で開いてしまい、書いた直後の行が
    読めない(SQLite が「変わらない」という宣言を信じてキャッシュするため)。
    本番では起動時の `scan_all` がやっていることを、ここでも同じようにやる。
    """
    from app import db

    notes_dir = tmp_path / "notes"
    collect_dir = tmp_path / "collect"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_COLLECT_DIR", str(collect_dir))
    db.set_mutable_paths([notes_dir / "notes.db"])
    return tmp_path


@pytest.fixture
def sample(enabled):
    return collect.create("news", prompt="{cursor} 以降", interval_minutes=60)


class TestDefinitions:
    def test_it_is_disabled_without_a_place_to_put_things(self, tmp_path, monkeypatch):
        """置き場が無ければ機能ごと無効(notes と同じ流儀)。"""
        monkeypatch.delenv("CHIEZO_COLLECT_DIR", raising=False)
        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path))
        assert not collect.is_enabled()

    def test_notes_alone_is_not_enough(self, tmp_path, monkeypatch):
        """定義の置き場が notes なので、notes が無効なら収集も成り立たない。"""
        monkeypatch.setenv("CHIEZO_COLLECT_DIR", str(tmp_path))
        monkeypatch.delenv("CHIEZO_NOTES_DIR", raising=False)
        assert not collect.is_enabled()

    def test_the_name_becomes_a_source_so_it_is_checked(self, enabled):
        """名前はソース名・ファイル名・URL になるので狭く取る。"""
        import fastapi

        for bad in ["News", "ニュース", "a", "with-hyphen", "9start"]:
            with pytest.raises(fastapi.HTTPException):
                collect.create(bad, prompt="p", interval_minutes=60)

    def test_it_refuses_names_that_are_already_sources(self, enabled):
        import fastapi

        with pytest.raises(fastapi.HTTPException):
            collect.create("notes", prompt="p", interval_minutes=60)

    def test_a_new_collection_starts_stopped(self, sample):
        """**作るのは「依頼」まで**。動かすかは Chiezo 側が決める。

        外のアプリからも作れる口なので、呼んだだけで定期実行が始まると、
        頼んでいない AI の呼び出しが枠を食う。
        """
        assert not sample.enabled
        assert not sample.is_due()

    def test_it_runs_as_soon_as_it_is_enabled(self, sample):
        """有効にした時点で 1 回目が来る(間隔を待たせると、動くか分からない)。"""
        assert collect.update("news", enabled=True).is_due()

    def test_it_remembers_who_asked(self, enabled):
        """誰が置いたかは、有効にするか決める人の手がかり(印であって認証ではない)。"""
        item = collect.create(
            "asked", prompt="p", interval_minutes=60, requested_by="travel-log"
        )
        assert item.requested_by == "travel-log"

    def test_changing_the_interval_does_not_postpone_the_first_run(self, sample):
        """一度も走っていないものの予定は動かさない。

        間隔を直しただけで初回が先送りになると、作ってすぐ試せない。
        """
        before = collect.get("news").next_run_at
        assert collect.update("news", interval_minutes=600).next_run_at == before

    def test_the_interval_has_a_floor(self, sample):
        """AI を呼ぶので、分より短い間隔は枠を焼くだけ。"""
        assert collect.update("news", interval_minutes=1).interval_minutes == (
            collect.MIN_INTERVAL_MINUTES
        )

    def test_broken_json_is_shown_not_silently_rebuilt(self, sample, monkeypatch):
        """定義が読めなくなったら黙って作り直さない(中身ごと消えるため)。"""
        import fastapi

        from app import notes

        row = collect._defs_row()
        notes.update(row["doc_id"], text="これはJSONではない")
        with pytest.raises(fastapi.HTTPException):
            collect.load()


class TestSample:
    """最初から置いておく見本。**止めた状態**で、**消したら戻ってこない**。"""

    def test_it_places_one_sample_when_empty(self, enabled):
        collect.ensure_sample()
        names = [c.name for c in collect.load()]
        assert names == [collect.SAMPLE_NAME]

    def test_the_sample_starts_disabled(self, enabled):
        """有効なものを黙って足すと、設定した覚えのない AI の呼び出しが枠を食う。"""
        collect.ensure_sample()
        sample = collect.get(collect.SAMPLE_NAME)
        assert not sample.enabled
        assert [c.name for c in collect.due_collections()] == []

    def test_it_does_not_come_back_after_being_deleted(self, enabled):
        """消せない見本は見本ではない(起動のたびに押し付け直さない)。"""
        collect.ensure_sample()
        collect.remove(collect.SAMPLE_NAME)
        collect.create("mine", prompt="p", interval_minutes=60)
        collect.ensure_sample()
        assert [c.name for c in collect.load()] == ["mine"]

    def test_it_does_not_touch_existing_definitions(self, enabled):
        collect.create("mine", prompt="p", interval_minutes=60)
        collect.ensure_sample()
        assert [c.name for c in collect.load()] == ["mine"]

    def test_the_sample_shows_how_to_use_the_cursor(self, enabled):
        """見本を置く理由は「書き方が分からないと始められない」ことなので、

        `{cursor}` と `next_cursor` の両方が入っていないと見本にならない。
        """
        assert "{cursor}" in collect.SAMPLE["prompt"]
        assert "next_cursor" in collect.SAMPLE["prompt"]

    def test_it_never_makes_a_second_definition_note(self, enabled):
        """定義のメモは 1 件だけ。

        `notes.add` は見出しが衝突すると `(doc_id)` を足して別物として残すので、
        定義が「見えなかった」ときに二重の設定ができる。本番で実際に踏んだ
        (起動順が悪く、追記された行が `immutable` の読み手に見えていなかった)。
        """
        from app import notes

        collect.ensure_sample()
        collect.remove(collect.SAMPLE_NAME)  # 空の定義メモが残る
        collect.ensure_sample()
        titles = [n["title"] for n in notes.recall(limit=50)["notes"]]
        assert titles.count(collect.DEFS_TITLE) == 1
        assert not any(t.startswith(f"{collect.DEFS_TITLE} (") for t in titles)

    def test_turning_it_on_makes_it_due(self, enabled):
        collect.ensure_sample()
        collect.update(collect.SAMPLE_NAME, enabled=True)
        assert [c.name for c in collect.due_collections()] == [collect.SAMPLE_NAME]


class TestRequestingFromOutside:
    """外のアプリは**依頼**までできて、動かすのは Chiezo 側、という線。

    LAN 内・認証なしの前提なので、守れるのは「呼んだだけでは AI が動かない」ところまで。
    """

    def test_rest_cannot_enable_a_collection(self, enabled, monkeypatch, tmp_path):
        """`enabled` が PATCH の受け口に無いこと。

        置いてしまうと、外のアプリが自分で作った収集を自分で動かせることになり、
        依頼と有効化を分けた意味が消える。
        """
        from app.main import CollectionPatch

        assert "enabled" not in CollectionPatch.model_fields

    def test_create_never_returns_an_enabled_collection(self, enabled):
        item = collect.create("outside", prompt="p", interval_minutes=60)
        assert not item.enabled
        assert [c.name for c in collect.due_collections()] == []


class TestAppending:
    def test_it_appends_instead_of_rebuilding(self, sample):
        """2 回に分けて入れたものが両方残る(取り込みのような洗い替えをしない)。"""
        assert collect.append("news", [{"title": "1件目", "body": "本文"}]) == (1, 0)
        assert collect.append("news", [{"title": "2件目", "body": "本文"}]) == (1, 0)
        assert collect.count("news") == 2

    def test_the_same_headline_is_skipped(self, sample):
        """繰り返し同じことを聞く前提なので、見出しが重複の鍵。

        notes は衝突したら `(doc_id)` を足して別物として残すが、それでは同じ
        ニュースが実行のたびに増える。
        """
        item = [{"title": "同じ見出し", "body": "本文"}]
        assert collect.append("news", item) == (1, 0)
        assert collect.append("news", item) == (0, 1)
        assert collect.count("news") == 1

    def test_items_without_a_title_or_body_are_skipped(self, sample):
        assert collect.append("news", [{"title": "", "body": "x"}, {"title": "y"}]) == (0, 2)

    def test_it_records_when_it_was_collected(self, sample):
        """古い情報かどうかを読む側が判断できるように、集めた時刻を必ず残す。"""
        collect.append("news", [{"title": "見出し", "body": "本文", "url": "https://example.com"}])
        assert collect.sample("news")[0]["title"] == "見出し"

    def test_collected_data_does_not_land_in_notes(self, sample):
        """溜め先は notes と別。混ぜると短期記憶が収集物で埋まる(この層の芯)。"""
        from app import notes

        before = notes.count()
        collect.append("news", [{"title": "見出し", "body": "本文"}])
        assert notes.count() == before
        assert collect.count("news") == 1


class TestCursor:
    def test_the_cursor_goes_into_the_prompt(self, sample):
        collect.update("news", cursor="2026-09-01")
        user = collect.build_messages(collect.get("news"))[1]["content"]
        assert user == "2026-09-01 以降"

    def test_an_empty_cursor_does_not_break_the_prompt(self, sample):
        assert "以降" in collect.build_messages(collect.get("news"))[1]["content"]

    def test_a_successful_run_advances_the_cursor(self, sample):
        collect.record_result("news", status="ok", added=3, next_cursor="2026-09-02")
        assert collect.get("news").cursor == "2026-09-02"

    def test_a_run_without_a_next_cursor_leaves_it_alone(self, sample):
        collect.record_result("news", status="ok", added=1, next_cursor="A")
        collect.record_result("news", status="ok", added=1)
        assert collect.get("news").cursor == "A"


class TestSchedule:
    def test_a_failed_run_is_still_scheduled_again(self, sample):
        """止めると、一度こけた収集が二度と走らなくなる。"""
        collect.update("news", enabled=True)
        updated = collect.record_result("news", status="error", error="相手が落ちていた")
        assert updated.next_run_at is not None
        assert not updated.is_due()
        later = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=61)
        assert updated.is_due(later)

    def test_disabled_collections_are_not_due(self, sample):
        collect.update("news", enabled=False)
        assert [c.name for c in collect.due_collections()] == []

    def test_due_ones_come_out_earliest_first(self, enabled):
        collect.create("aaa", prompt="p", interval_minutes=60)
        collect.create("bbb", prompt="p", interval_minutes=60)
        collect.update("aaa", enabled=True)
        collect.update("bbb", enabled=True)
        collect.record_result("aaa", status="ok")  # 次は 60 分後
        assert [c.name for c in collect.due_collections()] == ["bbb"]


class TestDrafting:
    """プロンプトを AI と相談して決める(`build_draft_messages` / `clean_draft`)。

    AI を呼ぶところは main 側なので、ここでは**渡す本文**と**受け取りの整え方**を見る。
    """

    def test_the_contract_is_taught_not_assumed(self):
        """頼む側は「何を集めたいか」だけ書けばよい、が相談の値打ち。

        返させる JSON の形・`{cursor}`・title が重複の鍵、はこちらが system で教える。
        """
        system = collect.build_draft_messages("ニュース")[0]["content"]
        for needed in ["items", "next_cursor", "{cursor}", "title"]:
            assert needed in system

    def test_it_carries_the_previous_draft_so_it_can_be_refined(self):
        """1 往復ずつだが、前の案と注文を渡すので会話として続けられる。"""
        user = collect.build_draft_messages("ニュース", current="前の案", feedback="件数を減らして")[1][
            "content"
        ]
        assert "前の案" in user
        assert "件数を減らして" in user

    def test_a_first_draft_does_not_mention_a_previous_one(self):
        user = collect.build_draft_messages("ニュース")[1]["content"]
        assert "いまの指示文" not in user

    def test_it_strips_code_fences_but_not_the_content(self):
        """手を入れると、画面に出るものと実際に投げるものが食い違う。"""
        assert collect.clean_draft("```json\n本文\n```") == "本文"
        assert collect.clean_draft("  本文の {cursor} は残す  ") == "本文の {cursor} は残す"


class TestSampleContent:
    """見本の中身。**技術ニュースではなく、押さえておくべき一般のニュース**。"""

    def test_the_sample_collects_general_news(self, enabled):
        collect.ensure_sample()
        prompt = collect.get(collect.SAMPLE_NAME).prompt
        assert "押さえておくべき" in prompt
        # 何を入れないかまで書いていないと、集まるものが散らかる
        assert "芸能" in prompt


class TestBaking:
    """長期記憶へ焼く(固化と同じ形)。

    **毎回焼き直すのに積み上がる**のがこの層の芯なので、素材に前世代が入ることと、
    焼き上がりを確かめてから待ち行列を片付けることを押さえる。
    """

    def _fake_source(self, tmp_path, docs):
        """焼き上がった長期記憶の代わり(コアスキーマの読み取り専用 DB)。"""
        import sqlite3

        from app import notes

        path = tmp_path / "baked.db"
        conn = sqlite3.connect(path)
        conn.executescript(notes.SCHEMA_DDL)
        for i, (title, body) in enumerate(docs, start=1):
            conn.execute(
                "INSERT INTO docs (doc_id, title, opening, body, tags, updated_at, rank_score)"
                " VALUES (?, ?, ?, ?, '[]', '2026-01-01T00:00:00+00:00', 0.0)",
                (i, title, body, body),
            )
        conn.commit()
        conn.close()

        class Src:
            def __init__(self, path):
                self.path = path
                self.dump_date = "20260101000000"

        return {"news": Src(path)}

    def test_the_material_is_previous_generation_plus_the_queue(self, sample, tmp_path):
        """ここが「全件の作り直しに乗せたまま追記になる」仕掛け。"""
        from app import db

        sources = self._fake_source(tmp_path, [("焼いてある", "前世代の本文")])
        db.set_mutable_paths([])
        collect.append("news", [{"title": "新しく集めた", "body": "本文"}])
        titles = [d["title"] for d in collect.material("news", sources)]
        assert titles == ["焼いてある", "新しく集めた"]

    def test_it_keeps_doc_ids_so_urls_do_not_move(self, sample, tmp_path):
        """焼き直しても文書の URL が変わらないようにする(固化と同じ)。"""
        sources = self._fake_source(tmp_path, [("焼いてある", "前世代の本文")])
        collect.append("news", [{"title": "焼いてある", "body": "新しい本文"}])
        docs = {d["title"]: d for d in collect.material("news", sources)}
        assert docs["焼いてある"]["doc_id"] == 1
        # 同じ見出しは新しく集めたほうで置き換える
        assert docs["焼いてある"]["body"] == "新しい本文"

    def test_nothing_to_bake_is_refused(self, sample, tmp_path):
        """流し始めた後ではステータスを変えられないので、先に断る。"""
        import fastapi

        with pytest.raises(fastapi.HTTPException) as got:
            collect.ndjson("news", {})
        assert got.value.status_code == 409

    def test_the_queue_is_only_cleared_after_it_landed(self, sample, tmp_path):
        """先に消すと、焼きに失敗したときに集めたものが失われる。"""
        collect.append("news", [{"title": "焼いてある", "body": "本文"}, {"title": "まだ", "body": "本文"}])
        sources = self._fake_source(tmp_path, [("焼いてある", "本文")])
        result = collect.sweep("news", sources)
        assert result["cleared"] == 1
        assert result["remaining"] == 1
        assert [d["title"] for d in collect.staged("news")] == ["まだ"]

    def test_sweeping_before_baking_does_nothing(self, sample):
        collect.append("news", [{"title": "見出し", "body": "本文"}])
        assert collect.sweep("news", {})["cleared"] == 0
        assert collect.count("news") == 1

    def test_the_catalog_lists_definitions_even_when_empty(self, sample):
        """一覧に出ないと、管理画面に焼く導線が出ない。"""
        assert [c["name"] for c in collect.catalog()] == ["news"]


class TestParsing:
    def test_it_digs_the_json_out_of_a_wrapped_answer(self):
        """前置きやコードブロックが混ざるのは普通に起きる。"""
        items, cursor = collect.parse_response(
            '調べました。\n```json\n{"items":[{"title":"X","body":"Y"}],"next_cursor":"Z"}\n```'
        )
        assert items == [{"title": "X", "body": "Y"}]
        assert cursor == "Z"

    def test_a_missing_items_array_is_an_error(self):
        with pytest.raises(ValueError):
            collect.parse_response('{"next_cursor":"Z"}')

    def test_a_non_json_answer_is_an_error(self):
        with pytest.raises(ValueError):
            collect.parse_response("JSON を返しませんでした")


class TestDeletion:
    def test_deleting_the_definition_keeps_the_data(self, sample):
        """定義を消すのと、集めたものを捨てるのは別の意思決定。"""
        collect.append("news", [{"title": "見出し", "body": "本文"}])
        collect.remove("news")
        assert collect.load() == []
        assert collect.count("news") == 1

    def test_it_can_drop_the_data_when_asked(self, sample):
        collect.append("news", [{"title": "見出し", "body": "本文"}])
        collect.remove("news", drop_data=True)
        assert collect.count("news") == 0
