"""収集層(app/collect.py)のテスト。

**押さえているのは、この層でしか起きない 4 つ**:
①追記していく(全件の作り直しをしない)、②同じ見出しは飛ばす、
③実行ごとにカーソルが進む、④失敗しても次回の予定が入る。

溜め先が notes と別のソースになること(混ざらないこと)も見る —— これを崩すと
短期記憶が収集物で埋まって `recall` が使い物にならなくなる、という設計の芯。
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import pytest

from app import collect, notes
from app import partition as partitioning


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """置き場を用意して、**追記される DB を mutable に登録する**。

    `db.query` は登録が無いと `immutable=1` で開いてしまい、書いた直後の行が
    読めない(SQLite が「変わらない」という宣言を信じてキャッシュするため)。
    本番では起動時の `scan_all` がやっていることを、ここでも同じようにやる。
    """
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    # 取り込みを起こせない面では収集そのものが成り立たないので、これも要る
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    return tmp_path


@pytest.fixture
def sample(enabled):
    return collect.create("news", prompt="{cursor} 以降", interval_minutes=60)


@pytest.fixture
def baked(tmp_path):
    """焼き上がった長期記憶の代わり(コアスキーマの読み取り専用 DB)を作る。

    素材に前世代が混ざるかを見るには「1 度焼いた後」の状態が要るが、本物の取り込みは
    ここでは回せない。焼き上がりが満たしている条件はコアスキーマなので、それだけ作る。
    """
    from app import notes

    def make(docs, name="news"):
        path = tmp_path / f"baked_{name}.db"
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

        return {name: Src(path)}

    return make


class TestDefinitions:
    def test_it_needs_no_place_of_its_own(self, enabled):
        """要る置き場は定義のぶんだけ。

        集めたものは取り込みの中で焼かれるので、**途中の置き場は持たない**。
        """
        assert collect.is_enabled()

    def test_without_notes_it_is_disabled(self, enabled, monkeypatch):
        """定義の置き場が notes なので、notes が無効なら収集も成り立たない。"""
        monkeypatch.delenv("CHIEZO_NOTES_DIR", raising=False)
        assert not collect.is_enabled()

    def test_without_a_way_to_bake_it_is_disabled(self, enabled, monkeypatch):
        """取り込みを起こせない面では、定義を置いても永遠に走らない。

        **見本の定義まで作らない**のが要点 —— 使えない機能の設定が短期記憶に
        1 件混ざるだけになる(タスク専用の面のように corpus を持たない構成)。
        """
        monkeypatch.delenv("CHIEZO_TRIGGER_URL", raising=False)
        assert not collect.is_enabled()
        collect.ensure_sample()
        assert collect.load() == []

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


class TestMaterial:
    """焼く素材の組み立て(`material`)。**集めたものはここにしか現れない**。

    途中の置き場を持たないので、集めたぶんは前世代と混ざって素材になり、そのまま
    取り込みへ流れる。積み上がるかどうかはここで決まる。
    """

    def test_it_appends_instead_of_rebuilding(self, sample, baked):
        """前世代に足す形になる(取り込みのような洗い替えをしない)。"""
        sources = baked([("前に集めた", "本文")])
        docs, diff = collect.material(
            collect.get("news"),
            collect.previous_docs("news", sources),
            [{"title": "いま集めた", "body": "本文"}],
        )
        assert [d["title"] for d in docs] == ["前に集めた", "いま集めた"]
        assert (diff["added"], diff["skipped"], diff["removed"]) == (1, 0, 0)

    def test_the_same_headline_is_not_counted_twice(self, sample, baked):
        """繰り返し同じことを聞く前提なので、見出しが重複の鍵。

        notes は衝突したら `(doc_id)` を足して別物として残すが、それでは同じ
        ニュースが実行のたびに増える。
        """
        sources = baked([("同じ見出し", "古い本文")])
        docs, diff = collect.material(
            collect.get("news"),
            collect.previous_docs("news", sources),
            [{"title": "同じ見出し", "body": "新しい本文"}],
        )
        assert len(docs) == 1
        assert (diff["added"], diff["skipped"]) == (0, 1)
        # 数は増えないが、中身は新しく集めたほうで置き換える
        assert docs[0]["body"] == "新しい本文"

    def test_items_without_a_title_or_body_are_skipped(self, sample):
        docs, diff = collect.material(
            collect.get("news"), {}, [{"title": "", "body": "x"}, {"title": "y"}]
        )
        assert docs == []
        assert (diff["added"], diff["skipped"]) == (0, 2)

    def test_it_records_when_it_was_collected(self, sample):
        """古い情報かどうかを読む側が判断できるように、集めた時刻を必ず残す。"""
        docs, _diff = collect.material(
            collect.get("news"), {}, [{"title": "見出し", "body": "本文", "url": "https://example.com"}]
        )
        assert docs[0]["extra"]["collected_at"]
        assert docs[0]["extra"]["url"] == "https://example.com"
        # web を開けて集めたかも残す(公開リポジトリへ出すかの判断材料になる)
        assert docs[0]["extra"]["web"] is True

    def test_collected_data_does_not_land_in_notes(self, sample):
        """溜め先は notes と別。混ぜると短期記憶が収集物で埋まる(この層の芯)。"""
        from app import notes

        before = notes.count()
        collect.material(collect.get("news"), {}, [{"title": "見出し", "body": "本文"}])
        assert notes.count() == before


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


class TestPartitionLedger:
    """区画の台帳 —— 「どこを見終わったか」を Chiezo 側が持つ。

    `cursor` が「次はどこ」を AI に決めさせる 1 本なのに対し、こちらは数え上げられる
    一覧。全部を見きったかも、取りこぼしがどこかも、一覧が無いと分からない。
    """

    def _with_ledger(self, keys):
        collect.update(
            "news",
            partition={"by": "title", "target": 100},
            partitions=[{"key": k, "count": 1} for k in keys],
        )

    def test_the_oldest_one_comes_next(self, sample):
        """まだ見ていないものが先、次に古いもの。"""
        self._with_ledger(["A", "B"])
        collect.record_result("news", status="ok", partition="A")
        assert partitioning.due(collect.get("news").partitions) == "B"
        collect.record_result("news", status="ok", partition="B")
        assert partitioning.due(collect.get("news").partitions) == "A"

    def test_a_failed_run_does_not_mark_it_seen(self, sample):
        """一度も見られていない区画が「見終わった」に混ざると、一周が嘘になる。"""
        self._with_ledger(["A", "B"])
        collect.record_result("news", status="error", partition="A", error="落ちた")
        assert partitioning.due(collect.get("news").partitions) == "A"

    def test_the_partition_goes_into_the_prompt(self, sample):
        collect.update("news", prompt="この範囲を調べて: {partition}")
        self._with_ledger(["あ〜き"])
        user = collect.build_messages(collect.get("news"), {}, "あ〜き")[1]["content"]
        assert "あ" in user and "{partition}" not in user

    def test_without_a_partition_the_placeholder_still_goes_away(self, sample):
        """区画を持たない収集で `{partition}` を書かれても壊さない。"""
        collect.update("news", prompt="この範囲: {partition}")
        user = collect.build_messages(collect.get("news"))[1]["content"]
        assert "{partition}" not in user

    def test_changing_how_it_splits_throws_the_ledger_away(self, sample):
        """鍵の意味が変わるので、引き継ぐと前の割り方で見た記録が新しい区画に付く。"""
        self._with_ledger(["A"])
        collect.update("news", partition={"by": "title", "target": 50})
        assert collect.get("news").partitions == []

    def test_patching_an_empty_ledger_starts_the_round_over(self, sample):
        """2 周目を粗いまま繰り返させず、精度を上げて回り直したいときに使う。"""
        self._with_ledger(["A"])
        collect.record_result("news", status="ok", partition="A")
        collect.update("news", partitions=[])
        assert collect.get("news").partitions == []

    def test_the_list_endpoint_leaves_the_ledger_out(self, sample):
        """上限まで割ると 1 件で数百 KB になり、収集の数だけ倍になる。"""
        self._with_ledger(["A", "B"])
        collect.record_result("news", status="ok", partition="A")
        listed = collect.to_public(collect.get("news"), with_partitions=False)
        assert "partitions" not in listed
        assert (listed["partitions_visited"], listed["partitions_total"]) == (1, 2)
        assert listed["next_partition"] == "B"


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
    doc_id が動かないことを押さえる。
    """

    def test_the_material_is_previous_generation_plus_what_was_just_collected(
        self, sample, baked
    ):
        """ここが「全件の作り直しに乗せたまま追記になる」仕掛け。"""
        sources = baked([("焼いてある", "前世代の本文")])
        body, _diff = collect.ndjson(
            collect.get("news"),
            sources,
            collect.previous_docs("news", sources),
            [{"title": "新しく集めた", "body": "本文"}],
        )
        titles = [json.loads(line)["title"] for line in body.splitlines()[1:]]
        assert titles == ["焼いてある", "新しく集めた"]

    def test_it_keeps_doc_ids_so_urls_do_not_move(self, sample, baked):
        """焼き直しても文書の URL が変わらないようにする(固化と同じ)。"""
        sources = baked([("焼いてある", "前世代の本文")])
        docs, _diff = collect.material(
            collect.get("news"),
            collect.previous_docs("news", sources),
            [{"title": "焼いてある", "body": "新しい本文"}],
        )
        assert docs[0]["doc_id"] == 1

    def test_the_first_line_is_the_meta(self, sample):
        """取り込み側は 1 行目から世代の日付と検証条件を読む。"""
        body, _diff = collect.ndjson(
            collect.get("news"), {}, {}, [{"title": "見出し", "body": "本文"}]
        )
        meta = json.loads(body.splitlines()[0])["meta"]
        assert re.fullmatch(r"\d{14}", meta["dump_date"])
        assert meta["sample_titles"] == ["見出し"]

    def test_nothing_to_bake_is_refused(self, sample):
        """流し始めた後ではステータスを変えられないので、先に断る。

        **集められなかった回で焼かせない**のが要点 —— 前世代のまま新しい世代が
        できると、集められなかったことが世代の履歴から消える。
        """
        import fastapi

        with pytest.raises(fastapi.HTTPException) as got:
            collect.ndjson(collect.get("news"), {}, {}, [])
        assert got.value.status_code == 409

    def test_the_catalog_lists_definitions_even_when_empty(self, sample):
        """一覧に出ないと、取り込み側がこのソースを焼けない。"""
        assert [c["name"] for c in collect.catalog()] == ["news"]


class TestHowMuchIsAccepted:
    """受け取る量の天井は**件数ではなく大きさ**で、超えたら黙って切らずに断る。

    かつては件数(200)で切っていて、しかも黙って切っていた。切られたことは
    返り値からもデータからも分からないので、数千件当たった収集が「そこまでしか
    無い」ように見えた。守るべきものは大きさなので、測る軸をそちらへ移した。
    """

    def test_thousands_of_items_are_accepted(self, sample):
        """**数千件を集めたいことは普通にある。** 件数では切らない。"""
        collected = [{"title": f"見出し {i}", "body": "本文"} for i in range(3_000)]
        docs, diff = collect.material(collect.get("news"), {}, collected)
        assert len(docs) == 3_000
        assert diff["added"] == 3_000

    def test_the_collected_count_is_reported_next_to_what_was_baked(self, sample):
        """集めた件数と焼ける件数を別々に出す。

        一致しないときに「捨てた」のか「前世代と重なった」のかを読み分けられないと、
        プロンプトの直しようがない。
        """
        collected = [{"title": "同じ見出し", "body": "本文"}] * 3
        _docs, diff = collect.material(collect.get("news"), {}, collected)
        assert diff["collected"] == 3
        assert diff["total"] == 1

    def test_material_that_is_too_large_is_refused_not_trimmed(self, sample, monkeypatch):
        """大きすぎたら 409。**焼かないので、いまの内容はそのまま残る。**"""
        import fastapi

        monkeypatch.setattr(collect, "MAX_MATERIAL_BYTES", 2_000)
        collected = [{"title": f"見出し {i}", "body": "本文" * 100} for i in range(50)]
        with pytest.raises(fastapi.HTTPException) as got:
            collect.ndjson(collect.get("news"), {}, {}, collected)
        assert got.value.status_code == 409
        # 断る理由に件数と天井を書く（何件目で超えたのかが分からないと直せない）
        assert "大きすぎます" in got.value.detail["error"]

    def test_a_collection_under_the_ceiling_still_bakes(self, sample, monkeypatch):
        """天井は暴走を止めるためのもので、普段の収集に当たってはいけない。"""
        monkeypatch.setattr(collect, "MAX_MATERIAL_BYTES", 1024 * 1024)
        collected = [{"title": f"見出し {i}", "body": "本文"} for i in range(500)]
        body, diff = collect.ndjson(collect.get("news"), {}, {}, collected)
        assert len(body.splitlines()) == 501  # meta 1 行 + 500 件
        assert diff["collected"] == 500


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
    def test_deleting_the_definition_keeps_the_data(self, sample, baked):
        """定義を消すのと、焼いたものを捨てるのは別の意思決定。

        長期記憶へ書けるのは取り込みだけなので、ここから消す手段はそもそも無い。
        """
        collect.remove("news")
        assert collect.load() == []
        assert baked([("見出し", "本文")])["news"].path.exists()


class TestRest:
    """REST の受け口(`/v1/collect/…`)。

    **固定のパスが名前として解釈されないこと**が主眼 —— `/{name}` を先に宣言すると
    `/sources` や `/fetch` が 404 になる(`app/tasks_api.py` で踏んだのと同じ罠)。
    ついでに、外へ開けておける理由(呼んだだけでは AI が動かない)も押さえる。
    """

    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_the_fixed_paths_are_not_read_as_names(self, client, sample):
        """`/sources` と `/fetch` は収集の名前ではない。"""
        listed = client.get("/v1/collect/sources").json()["sources"]
        # 起動時に見本も置かれるので、作ったぶんが混ざっていることだけ見る
        assert "news" in [s["name"] for s in listed]
        # 名前を渡さない /fetch は 422(名前として 404 にならない)
        assert client.get("/v1/collect/fetch").status_code == 422

    def test_one_collection_comes_with_what_was_baked(self, client, sample):
        """設定だけでは、プロンプトを直すかの判断ができない。"""
        body = client.get("/v1/collect/news").json()
        assert body["name"] == "news"
        # まだ 1 度も焼いていないので空(焼き先のソースがそもそも無い)
        assert body["recent"] == []

    def test_an_unknown_collection_is_404(self, client, sample):
        assert client.get("/v1/collect/nosuch").status_code == 404

    def test_changes_is_not_read_as_a_name(self, client, sample, monkeypatch, tmp_path):
        """`/changes` も固定のパス(`/sources` と同じ罠を踏まない)。"""
        from app import collect_log

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        collect_log.record("news", status=collect_log.STATUS_OK, diff={"added": 3})
        collect_log.record("other", status=collect_log.STATUS_OK, diff={"added": 1})
        body = client.get("/v1/collect/changes").json()
        assert [c["name"] for c in body["changes"]] == ["other", "news"]
        one = client.get("/v1/collect/changes", params={"name": "news"}).json()
        assert [c["added"] for c in one["changes"]] == [3]

    def test_changes_is_empty_without_a_place_to_record(self, client, sample, monkeypatch):
        """控えを持たない構成で、読む側が毎回エラーを踏まないように。"""
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        assert client.get("/v1/collect/changes").json() == {"changes": []}

    def test_running_a_stopped_collection_is_refused(self, client, sample):
        """呼んだだけでは AI が動かない、が外へ開けておける理由。"""
        res = client.post("/v1/collect/news/run")
        assert res.status_code == 403

    def test_it_removes_the_definition(self, client, sample):
        """溜めたものは残る（外のアプリに消す手段は渡さない）。

        `remove` から引数が消えたときに口の側が取り残され、**呼ぶと必ず 500** に
        なっていた。実際に押して初めて分かった。
        """
        assert client.delete("/v1/collect/news").status_code == 200
        assert [c["name"] for c in client.get("/v1/collect").json()["collections"]] == [
            "sample_news"
        ]

    def test_deleting_an_unknown_collection_is_404(self, client, sample):
        assert client.delete("/v1/collect/nosuch").status_code == 404

    def test_previewing_a_stopped_collection_is_refused(self, client, sample):
        """焼かないとはいえ AI は 1 回動くので、`run` と同じ扱いにする。

        外のアプリが自分で作った収集を自分で回せる状態にはしない。
        """
        assert client.post("/v1/collect/news/preview").status_code == 403

    def test_the_mode_comes_back_on_the_definition(self, client, enabled):
        """依頼した側が、足すのか作り直すのかを確かめられるようにする。"""
        res = client.post(
            "/v1/collect",
            json={
                "name": "tidy",
                "prompt": "いまの内容:\n{current}\n整理して",
                "interval_minutes": 60,
                "mode": "refine",
            },
        )
        assert res.status_code == 200
        assert res.json()["mode"] == "refine"
        assert client.get("/v1/collect/tidy").json()["keep_ratio"] == collect.DEFAULT_KEEP_RATIO

    def test_the_backend_comes_back_on_the_definition(self, client, enabled):
        """依頼した側が、誰に頼むことになったかを確かめられる。"""
        res = client.post(
            "/v1/collect",
            json={
                "name": "asked",
                "prompt": "{cursor}",
                "interval_minutes": 60,
                "backend": "antigravity",
                "model": "haiku",
            },
        )
        assert res.status_code == 200
        assert res.json()["backend"] == "antigravity"
        assert client.get("/v1/collect/asked").json()["model"] == "haiku"

    def test_a_rebuild_without_the_material_placeholder_is_refused(self, client, enabled):
        """外から依頼するときも、素材の差し込み口が無いものは作らせない。"""
        res = client.post(
            "/v1/collect",
            json={"name": "bad", "prompt": "整理して", "interval_minutes": 60, "mode": "refine"},
        )
        assert res.status_code == 400

    def test_enabled_is_not_in_the_patch_shape(self, client, sample):
        """有効にするかを REST から触れると、依頼と実行を分けた意味が消える。"""
        client.patch("/v1/collect/news", json={"enabled": True, "interval_minutes": 120})
        assert collect.get("news").enabled is False
        assert collect.get("news").interval_minutes == 120

    def test_a_request_from_outside_is_created_stopped(self, client, enabled):
        res = client.post(
            "/v1/collect",
            json={"name": "asked", "prompt": "{cursor}", "interval_minutes": 60,
                  "requested_by": "travel-log"},
        )
        assert res.status_code == 200
        assert res.json()["enabled"] is False
        assert res.json()["requested_by"] == "travel-log"


class TestRefineMode:
    """整理する(`mode=refine`)。**いまの内容を読ませて、直すものと足すものを返させる**。

    **返さなかったものはそのまま残る**のがこの層の芯。かつては「返ったものが新しい
    全体」にしていたが、それだと返し忘れが黙って消えた —— 無人で毎日回る層でいちばん
    起きやすい壊れ方で、1 件ずつ削れていくのは歯止めをすり抜ける。
    消すのは墓標で明示したときだけ(固化と同じ契約)。
    """

    @pytest.fixture
    def refine(self, enabled):
        return collect.create(
            "spots",
            prompt="いまの分類:\n{current}\nこれを整理し直して",
            interval_minutes=60,
            mode=collect.MODE_REFINE,
        )

    def test_the_material_placeholder_is_required(self, enabled):
        """今あるものを読ませずに整理はできない。作る時点で弾く。"""
        import fastapi

        with pytest.raises(fastapi.HTTPException) as got:
            collect.create("x", prompt="整理して", interval_minutes=60, mode=collect.MODE_REFINE)
        assert got.value.status_code == 400

    def test_switching_to_refine_checks_the_prompt_too(self, sample):
        """集め方だけ切り替えても、組み合わせで確かめ直す。"""
        import fastapi

        with pytest.raises(fastapi.HTTPException):
            collect.update("news", mode=collect.MODE_REFINE)

    def test_the_current_contents_go_into_the_prompt(self, refine, baked):
        """今ある内容を読ませないと「整理し直す」が成り立たない。"""
        sources = baked([("ラーメン", "麺の店"), ("カフェ", "喫茶")], "spots")
        user = collect.build_messages(
            collect.get("spots"), collect.previous_docs("spots", sources)
        )[1]["content"]
        assert "ラーメン" in user and "カフェ" in user
        assert "{current}" not in user

    def test_it_tells_the_ai_that_untouched_things_stay(self, refine):
        """変えないものまで返させない。返し忘れで消えないことが要点。"""
        system = collect.build_messages(collect.get("spots"), {})[0]["content"]
        assert "そのまま残る" in system
        assert notes.TOMBSTONE_TAG in system

    def test_what_is_not_returned_stays(self, refine, baked):
        """**返し忘れで消えない**。ここが「作り直し」から変えたところ。"""
        sources = baked([("残る", "本文"), ("触れない", "本文")], "spots")
        docs, diff = collect.material(
            collect.get("spots"),
            collect.previous_docs("spots", sources),
            [{"title": "残る", "body": "直した本文"}, {"title": "新入り", "body": "本文"}],
        )
        assert sorted(d["title"] for d in docs) == ["新入り", "残る", "触れない"]
        assert (diff["added"], diff["updated"], diff["removed"]) == (1, 1, 0)
        # 直したほうは中身が入れ替わる
        assert next(d for d in docs if d["title"] == "残る")["body"] == "直した本文"

    def test_a_tombstone_removes_one(self, refine, baked):
        """消すのは明示したときだけ。固化と同じ墓標の契約。"""
        sources = baked([("残る", "本文"), ("消す", "本文")], "spots")
        docs, diff = collect.material(
            collect.get("spots"),
            collect.previous_docs("spots", sources),
            [{"title": "消す", "body": "", "tags": [notes.TOMBSTONE_TAG]}],
        )
        assert [d["title"] for d in docs] == ["残る"]
        assert diff["removed"] == 1
        assert diff["removed_titles"] == ["消す"]

    def test_the_diff_names_what_moved(self, refine, baked):
        """件数だけでは、何が起きたのか読めない(変更履歴に残す元になる)。"""
        sources = baked([("残る", "本文")], "spots")
        _docs, diff = collect.material(
            collect.get("spots"),
            collect.previous_docs("spots", sources),
            [{"title": "残る", "body": "直した本文"}, {"title": "新入り", "body": "本文"}],
        )
        assert diff["added_titles"] == ["新入り"]
        assert diff["updated_titles"] == ["残る"]

    def test_a_tombstone_for_something_absent_does_nothing(self, refine):
        """持っていないものへの墓標は、消すものが無いだけ。"""
        _docs, diff = collect.material(
            collect.get("spots"), {}, [{"title": "居ない", "tags": [notes.TOMBSTONE_TAG]}]
        )
        assert diff["removed"] == 0
        assert diff["skipped"] == 1

    def test_appending_ignores_tombstones(self, sample, baked):
        """集めるほうは墓標を読まない(消す経路そのものが無い)。"""
        sources = baked([("消えない", "本文")])
        docs, diff = collect.material(
            collect.get("news"),
            collect.previous_docs("news", sources),
            [{"title": "消えない", "body": "本文", "tags": [notes.TOMBSTONE_TAG]}],
        )
        assert [d["title"] for d in docs] == ["消えない"]
        assert diff["removed"] == 0

    def test_surviving_docs_keep_their_doc_id(self, refine, baked):
        """整理し直しても、残ったものの URL は動かさない。"""
        sources = baked([("残る", "本文"), ("触れない", "本文")], "spots")
        docs, _diff = collect.material(
            collect.get("spots"),
            collect.previous_docs("spots", sources),
            [{"title": "残る", "body": "直した本文"}],
        )
        assert next(d for d in docs if d["title"] == "残る")["doc_id"] == 1

    def test_removing_too_much_is_refused(self, refine, baked):
        """墓標での大量削除は焼く前に断る。

        返し忘れでは減らなくなったので、ここが止めるのは**明示的な大量削除**だけ。
        そのぶん、止まったときの意味が鋭い。
        """
        import fastapi

        sources = baked([(f"分類{i}", "本文") for i in range(1, 11)], "spots")
        previous = collect.previous_docs("spots", sources)
        graves = [{"title": f"分類{i}", "tags": [notes.TOMBSTONE_TAG]} for i in range(1, 10)]
        with pytest.raises(fastapi.HTTPException) as got:
            collect.ndjson(collect.get("spots"), sources, previous, graves)
        assert got.value.status_code == 409
        assert "整理を止めました" in got.value.detail["error"]

    def test_the_guard_can_be_turned_off_on_purpose(self, refine, baked):
        """意図して大きく減らすときの逃げ道。外したことは定義に残る。"""
        collect.update("spots", keep_ratio=0)
        sources = baked([(f"分類{i}", "本文") for i in range(1, 11)], "spots")
        graves = [{"title": f"分類{i}", "tags": [notes.TOMBSTONE_TAG]} for i in range(1, 10)]
        body, diff = collect.ndjson(
            collect.get("spots"), sources, collect.previous_docs("spots", sources), graves
        )
        assert diff["removed"] == 9
        assert len(body.splitlines()) == 2  # meta + 1 件

    def test_appending_never_removes_anything(self, sample, baked):
        """足すほうには消える経路が無い(だから歯止めも要らない)。"""
        sources = baked([("前に集めた", "本文")])
        _docs, diff = collect.material(
            collect.get("news"),
            collect.previous_docs("news", sources),
            [{"title": "いま集めた", "body": "本文"}],
        )
        assert diff["removed"] == 0
        assert collect.shrink_blocked(collect.get("news"), diff) is None

    def test_truncated_material_says_so(self, refine):
        """入り切らなかったことを本文に書く。

        黙って切ると、AI は見えなかったぶんを「無かったもの」として扱う。
        """
        previous = {
            f"見出し{i}": {"doc_id": i, "title": f"見出し{i}", "body": "本文", "tags": []}
            for i in range(collect.MAX_MATERIAL_DOCS + 50)
        }
        text, shown = collect.render_material(previous)
        assert shown == collect.MAX_MATERIAL_DOCS
        assert "対象外" in text

    def test_the_old_name_is_read_as_refining(self, enabled):
        """「作り直し」と呼んでいた頃の定義は、育てる側へ読み替える。

        当時の意図は「整理したい」で、置き換えはその実現手段でしかなかった。
        知らない値は足すほうへ倒す(壊れた定義でいきなり消す側へ寄せない)。
        """
        assert collect.normalize_mode("rebuild") == collect.MODE_REFINE
        assert collect.normalize_mode("いいかげんな値") == collect.MODE_APPEND
        assert collect.normalize_mode(None) == collect.MODE_APPEND

    def test_the_removed_headlines_are_recorded(self, refine):
        """消えたものが見えないと、プロンプトを直す判断ができない。"""
        updated = collect.record_result(
            "spots", status="ok", added=1, removed=2, removed_titles=["A", "B"]
        )
        assert updated.last_removed == 2
        assert updated.last_removed_titles == ["A", "B"]

    def test_the_number_of_edits_is_recorded(self, refine):
        """整理の回は、足すものが無くても大量に直っていることがある。

        追加だけ控えていると「何もしなかった」ように読める(実際に数えては
        いたのに、書き戻すところで落としていた)。
        """
        updated = collect.record_result("spots", status="ok", added=0, updated=7)
        assert updated.last_updated == 7


class TestBackend:
    """誰に頼むか。**未指定なら Chiezo の既定**(有効な相手の先頭)。

    収集は無人で回るので、後から「どの相手に頼んでいたのか」が読めることが要る。
    """

    def test_it_is_unset_by_default(self, sample):
        """既定にまかせる、が初期状態。"""
        item = collect.get("news")
        assert item.backend is None
        assert item.model is None

    def test_it_can_be_named_when_requested(self, enabled):
        """外のアプリから相手まで指定して依頼できる。"""
        item = collect.create(
            "named", prompt="{cursor}", interval_minutes=60,
            backend="antigravity", model="haiku",
        )
        assert item.backend == "antigravity"
        assert item.model == "haiku"

    def test_an_empty_value_means_leave_it_to_chiezo(self, sample):
        """画面のフォームは空欄を空文字で送る。それを「指定しない」に倒す。

        倒さないと、保存直後だけ空文字を持ち回ることになり、読み直した後の
        値(None)とずれる。
        """
        collect.update("news", backend="antigravity", model="haiku")
        assert collect.get("news").backend == "antigravity"

        updated = collect.update("news", backend="", model="")
        assert updated.backend is None
        assert updated.model is None
        # 読み直しても同じ
        assert collect.get("news").backend is None


class TestDeletingWithTheSource:
    """設定を消したら、焼いたものも消す。

    **画面からは 1 つの操作にする** —— 設定だけ消して中身が残ると、一覧から
    消えたのに検索には出続けるものができ、どこから来たのかも読めなくなる。
    """

    def test_the_trigger_is_asked_to_drop_the_source(self, sample, monkeypatch):
        """消せるのは trigger だけ(app は corpus を読み取り専用で持つ)。"""
        from app.views import admin

        called = []
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")

        class FakeResponse:
            status_code = 200
            text = "{}"

        class FakeClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def delete(self, url):
                called.append(url)
                return FakeResponse()

        monkeypatch.setattr(admin.httpx, "Client", FakeClient)
        assert admin._drop_collect_source("news") is True
        assert called == ["http://trigger.invalid/source/news"]

    def test_it_does_not_blow_up_without_a_trigger(self, sample, monkeypatch):
        """trigger が居ない構成は普通にある。そこで操作ごと止めない。"""
        from app.views import admin

        monkeypatch.setattr(admin, "TRIGGER_URL", None)
        assert admin._drop_collect_source("news") is False

    def test_the_definition_goes_even_if_the_source_stays(self, sample, monkeypatch):
        """ソースを消せなくても設定は消す(片付けは「ついで」なので)。"""
        from app.views import admin

        monkeypatch.setattr(admin, "TRIGGER_URL", None)
        assert admin._drop_collect_source("news") is False
        collect.remove("news")
        assert [c.name for c in collect.load()] == []
class TestTheCollectSectionMarkup:
    """画面の HTML そのものを見る。

    組み立てが壊れても例外にはならないので、テストが通ったまま画面だけが
    崩れる(実際に、開始タグを失った form の属性が本文として表示された)。
    """

    def _html(self, sample):
        from app.views import admin

        return admin._collect_html({}, "")

    def test_every_form_is_opened_and_closed(self, sample):
        html = self._html(sample)
        assert html.count("<form") == html.count("</form>")
        # 属性が本文へ漏れていない(開始タグを失った form の証拠)
        assert "onsubmit=" not in html.replace('" onsubmit=', "")

    def test_the_partition_progress_is_shown(self, sample):
        """一周したかどうかが読めて初めて、間隔と 1 回あたりの量を判断できる。"""
        collect.update(
            "news",
            partition={"by": "title", "target": 100},
            partitions=[
                {"key": "あ〜き", "count": 100, "visited_at": "2026-09-01T00:00:00+00:00"},
                {"key": "く〜そ", "count": 80},
            ],
        )
        html = self._html(sample)
        assert "2 のうち 1 を回り終えた" in html
        assert "く〜そ" in html

    def test_a_collection_without_partitions_says_nothing(self, sample):
        assert "区画:" not in self._html(sample)

    def test_the_recent_changes_are_shown(self, sample, monkeypatch, tmp_path):
        """「直近どこに修正が入ったか」は表の「前回」列とは別に要る。

        あちらは最新の 1 回で上書きされるので、並べないと読めない。
        """
        from app import collect_log

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        collect_log.record(
            "news",
            status=collect_log.STATUS_OK,
            diff={"total": 12, "added": 2, "removed": 1, "removed_titles": ["古い見出し"]},
        )
        html = self._html(sample)
        assert "直近の変更" in html
        assert "古い見出し" in html
        assert html.count("<table") == html.count("</table>")

    def test_without_a_place_to_record_it_says_so(self, sample, monkeypatch):
        """空の表を出すと「まだ動いていない」に読める(実際は記録していないだけ)。"""
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        assert "変更履歴は記録していません" in self._html(sample)

    def test_an_empty_collection_can_still_be_deleted(self, sample):
        """まだ何も溜まっていない収集の行にも削除の導線が要る。"""
        html = self._html(sample)
        assert '/admin/collect/news/delete"' in html
        assert "まだ何も溜まっていません" in html
        assert html.count(">削除</button>") == len(collect.load())

    def test_a_collection_with_data_warns_about_what_goes_with_it(self, sample, monkeypatch):
        """溜めたものも一緒に消えることを、押す前に出す。"""
        from app.registry import Source
        from app.views import admin

        baked = Source(
            name="news",
            kind="collect",
            lang="ja",
            dump_date=None,
            schema_version=4,
            built_at="2026-09-07T00:00:00+00:00",
            doc_count=1234,
            path=Path("/data/news.db"),
        )
        html = admin._collect_html({"news": baked}, "")
        assert "1,234 件" in html
        assert html.count("<form") == html.count("</form>")


class TestPickingTheModelAndTheEffort:
    """モデルと考える量は**選ぶ**もので、手で書くものではない。

    自由入力だった頃は、相手ごとに違う候補を画面の下の一覧から読んで写す作りだった
    —— 綴りを間違えても保存でき、走らせるまで気づけない。
    """

    def _html(self, sample):
        from app.views import admin

        return admin._collect_html({}, "")

    def test_they_are_selects_not_free_text(self, sample):
        html = self._html(sample)
        assert '<select name="model">' in html
        assert '<select name="effort">' in html
        assert '<input name="model"' not in html
        assert '<input name="effort"' not in html

    def test_leaving_it_to_the_backend_is_the_first_choice(self, sample):
        """空が「相手の既定」。**先頭に置く** —— 指定しないのが普通の使い方。"""
        from app.views import admin

        html = admin._candidate_select("model", None, ["a", "b"], "相手の既定")
        assert html.index("相手の既定") < html.index(">a<")

    def test_a_value_outside_the_candidates_survives(self, sample):
        """候補から落とすと、保存し直した瞬間に既定へ倒れて指定が消える
        (`_backend_select` と同じ約束)。"""
        from app.views import admin

        html = admin._candidate_select("model", "消えたモデル", ["a"], "相手の既定")
        assert '<option value="消えたモデル" selected>' in html

    def test_the_select_is_there_even_when_the_backend_has_no_candidates(self, sample):
        """候補が空でもセレクトは出す —— 消すと、JS が候補を入れに来たとき入れ先が無い。"""
        from app.views import admin

        assert '<select name="effort">' in admin._candidate_select(
            "effort", None, [], "相手の既定"
        )

    def test_the_script_finds_forms_by_class_not_by_id(self, sample):
        """フォームは 1 ページに何枚もあるので、id で捕まえると 1 枚しか動かない。"""
        from app.views import admin

        assert 'select[name="backend"]' in admin.COLLECT_BACKEND_SCRIPT
        assert "getElementById" not in admin.COLLECT_BACKEND_SCRIPT
        assert admin.COLLECT_BACKEND_SCRIPT in self._html(sample)


class TestTellingWhetherAnIngestIsRunning:
    """外のアプリが**押す前に**判断できるように、取り込みの状態を配る。

    走っている間は「いま集めて」を断るので、押してから断られるのではなく、
    押せないことが見えているほうがよい。
    """

    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_it_says_when_something_is_running(self, client, monkeypatch):
        from app.views import admin

        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")
        monkeypatch.setattr(
            admin,
            "_fetch_trigger_status",
            lambda: {"state": "running", "source": "news", "started_at": "2026-09-07T00:00:00+00:00"},
        )

        body = client.get("/v1/ingest/status").json()

        assert body["running"] is True
        assert body["source"] == "news"

    def test_it_does_not_hand_out_the_log(self, client, monkeypatch):
        """取り込みのログには置き場のパスのような内部の事情が混ざる。"""
        from app.views import admin

        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")
        monkeypatch.setattr(
            admin, "_fetch_trigger_status", lambda: {"state": "idle", "log_tail": ["/data/…"]}
        )

        body = client.get("/v1/ingest/status").json()

        assert body["running"] is False
        assert "log_tail" not in body

    def test_without_a_trigger_it_says_so(self, client, monkeypatch):
        from app.views import admin

        monkeypatch.setattr(admin, "TRIGGER_URL", None)

        assert client.get("/v1/ingest/status").status_code == 503
