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
from fastapi import HTTPException

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
        collect.record_result("news", status="ok", visited=["A"])
        ledger = collect.get("news").partitions
        assert partitioning.due(ledger, collect.DEFAULT_SWEEP_NAME) == "B"
        collect.record_result("news", status="ok", visited=["B"])
        ledger = collect.get("news").partitions
        assert partitioning.due(ledger, collect.DEFAULT_SWEEP_NAME) == "A"

    def test_a_failed_run_does_not_mark_it_seen(self, sample):
        """一度も見られていない区画が「見終わった」に混ざると、一周が嘘になる。"""
        self._with_ledger(["A", "B"])
        collect.record_result("news", status="error", visited=["A"], error="落ちた")
        ledger = collect.get("news").partitions
        assert partitioning.due(ledger, collect.DEFAULT_SWEEP_NAME) == "A"

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
        collect.record_result("news", status="ok", visited=["A"])
        collect.update("news", partitions=[])
        assert collect.get("news").partitions == []

    def test_the_list_endpoint_leaves_the_ledger_out(self, sample):
        """上限まで割ると 1 件で数百 KB になり、収集の数だけ倍になる。"""
        self._with_ledger(["A", "B"])
        collect.record_result("news", status="ok", visited=["A"])
        listed = collect.to_public(collect.get("news"), with_partitions=False)
        assert "partitions" not in listed
        assert listed["partitions_total"] == 2
        assert listed["sweeps"][0]["partitions_visited"] == 1
        assert listed["sweeps"][0]["next_partition"] == "B"


class TestSweeps:
    """1 つの収集に 2 種類の直し方 —— ざっと全体を拾うものと、少数をじっくり調べるもの。

    進み方も頼む相手も 1 回に食べる量も違うので、`interval_minutes` 1 本では表せない。
    """

    def _two_sweeps(self):
        collect.update(
            "news",
            partition={"by": "title", "target": 100},
            partitions=[{"key": k, "count": 1} for k in ["A", "B", "C", "D"]],
            sweeps=[
                {"name": "ざっと", "interval_minutes": 360, "cover_days": 7},
                {"name": "じっくり", "interval_minutes": 1440,
                 "partitions_per_run": 1, "model": "opus"},
            ],
        )

    def test_a_collection_without_sweeps_is_one_sweep(self, sample):
        """場合分けを外へ漏らさない。呼ぶ側はいつでも巡回の一覧を相手にする。"""
        sweeps = collect.sweeps_of(sample)
        assert [s.name for s in sweeps] == [collect.DEFAULT_SWEEP_NAME]
        assert sweeps[0].prompt == sample.prompt
        assert sweeps[0].interval_minutes == sample.interval_minutes

    def test_what_is_not_written_falls_back_to_the_collection(self, sample):
        """違うところだけ書けば済むほうが、2 本目を足すときに間違えにくい。"""
        self._two_sweeps()
        rough, deep = collect.sweeps_of(collect.get("news"))
        assert rough.prompt == sample.prompt
        assert rough.model is None
        assert deep.model == "opus"
        assert deep.interval_minutes == 1440

    def test_covering_in_a_week_decides_how_many_to_take(self, sample):
        """手で書かせると、区画が増えた日に一周が静かに伸びる。"""
        self._two_sweeps()
        rough, deep = collect.sweeps_of(collect.get("news"))
        # 6 時間ごと・7 日で一周 = 28 回。4 区画なら 1 回 1 区画で足りる
        assert rough.per_run(4) == 1
        # 区画が増えれば 1 回あたりも増える(手で追いかけなくてよい)
        assert rough.per_run(280) == 10
        # 直に書いてあればそちらが勝つ
        assert deep.per_run(280) == 1

    def test_one_run_never_eats_everything(self, sample):
        """区画ごとに AI を 1 回呼ぶので、1 回の取り込みが何十分にもならないように。"""
        self._two_sweeps()
        rough = collect.sweeps_of(collect.get("news"))[0]
        assert rough.per_run(100_000) == collect.MAX_PARTITIONS_PER_RUN

    def test_each_sweep_keeps_its_own_progress(self, sample):
        """ざっとが一周した区画を、じっくりはまだ見ていない、が普通に起きる。"""
        self._two_sweeps()
        collect.record_result("news", status="ok", sweep="ざっと", visited=["A", "B"])
        ledger = collect.get("news").partitions
        assert partitioning.progress(ledger, "ざっと") == (2, 4)
        assert partitioning.progress(ledger, "じっくり") == (0, 4)
        assert partitioning.due(ledger, "じっくり") == "A"

    def test_only_the_sweep_that_ran_moves_its_clock(self, sample):
        self._two_sweeps()
        collect.record_result("news", status="ok", sweep="ざっと")
        rough, deep = collect.sweeps_of(collect.get("news"))
        assert rough.next_run_at is not None
        assert deep.next_run_at is None

    def test_a_stopped_sweep_never_comes_due(self, sample):
        """止めるのに消さなくてよい(消すと進み具合まで消える)。"""
        collect.update(
            "news", enabled=True,
            sweeps=[{"name": "ざっと"}, {"name": "じっくり", "enabled": False}],
        )
        due = collect.due_sweeps()
        assert [s.name for _c, s in due] == ["ざっと"]

    def test_a_stopped_collection_runs_no_sweep_at_all(self, sample):
        """巡回ごとの enabled は、有効な収集の中でどれを回すかの話。"""
        collect.update("news", sweeps=[{"name": "ざっと"}])
        assert collect.due_sweeps() == []

    def test_starting_one_remembers_which(self, sample):
        """取り込みは収集の名前しか運べないので、起こした側が控える。"""
        collect.update("news", enabled=True, sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        collect.mark_started("news", "じっくり")
        item = collect.get("news")
        assert item.pending_sweep == "じっくり"
        assert collect.sweep_named(item, item.pending_sweep).name == "じっくり"
        # 走り終えたら忘れる
        collect.record_result("news", status="ok", sweep="じっくり")
        assert collect.get("news").pending_sweep == ""

    def test_starting_one_does_not_copy_the_last_failure(self, sample):
        """**起こしただけで「走った」ことにしない。**

        収集ぜんたいの前回の状態を渡していたせいで、ざっとが落ちた直後にじっくりを
        起こすと、じっくりにもその失敗が写っていた —— 走っている最中なのに
        「失敗」と出て、どちらが落ちたのか分からなくなる。
        """
        collect.update("news", enabled=True, sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        collect.record_result("news", status="error", sweep="ざっと", error="落ちた")

        collect.mark_started("news", "じっくり")
        rough, deep = collect.sweeps_of(collect.get("news"))

        assert rough.last_status == "error"
        # 起こしただけなので、まだ何も控えていない
        assert deep.last_status is None
        assert deep.last_run_at is None
        # 予定だけは進む
        assert deep.next_run_at is not None

    def test_an_unknown_sweep_falls_back_to_the_next_one(self, sample):
        """巡回を消したあとに、走りかけの取り込みが素材を取りに来ることがある。"""
        collect.update("news", enabled=True, sweeps=[{"name": "ざっと"}])
        assert collect.sweep_named(collect.get("news"), "消えた巡回").name == "ざっと"

    def test_duplicate_names_are_dropped(self, sample):
        """名前が鍵なので、2 本あると片方の進み具合がもう片方に化ける。"""
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "ざっと"}, {"name": ""}])
        assert [s["name"] for s in collect.get("news").sweeps] == ["ざっと"]

    def test_removing_a_sweep_drops_its_marks(self, sample):
        """同じ名前で作り直したとき、前の進み具合が引き継がれないように。"""
        self._two_sweeps()
        collect.record_result("news", status="ok", sweep="じっくり", visited=["A"])
        collect.update("news", sweeps=[{"name": "ざっと"}])
        ledger = collect.get("news").partitions
        assert all("じっくり" not in (p["visits"] or {}) for p in ledger)

    def test_the_soonest_sweep_is_what_the_list_shows(self, sample):
        """巡回を書いた収集では定義側の予定が進まないので、そのまま出すと止まって見える。"""
        collect.update(
            "news", enabled=True,
            sweeps=[
                {"name": "ざっと", "next_run_at": "2030-01-01T00:00:00+00:00"},
                {"name": "じっくり", "next_run_at": "2026-01-01T00:00:00+00:00"},
            ],
        )
        assert collect.to_public(collect.get("news"))["next_run_at"].startswith("2026-01-01")


class TestTheOutwardTool:
    """外向きの道具(`{feed}`)—— 取ってきたものは**参考**で、情報源ではない。"""

    def test_the_harvest_goes_into_the_prompt(self, sample):
        collect.update("news", prompt="拾ってきたもの:\n{feed}\n上を参考に集めて。")
        harvest = {
            "items": [{
                "title": "見出し", "url": "https://example.com/1",
                "summary": "要約", "at": "", "from": "ためし新聞",
            }],
            "failed": 0, "tried": 1,
        }
        user = collect.build_messages(
            collect.get("news"), {}, None, {}, None, None, harvest
        )[1]["content"]
        assert "見出し" in user
        assert "参考です" in user
        assert "{feed}" not in user

    def test_without_a_tool_the_placeholder_says_so(self, sample):
        """差し込み口だけ残ると、AI は「渡されるはずのものが空だった」と読んで待つ。"""
        collect.update("news", prompt="拾ってきたもの: {feed}")
        user = collect.build_messages(collect.get("news"))[1]["content"]
        assert "{feed}" not in user
        assert "道具は付いていません" in user

    def test_the_spec_survives_a_round_trip(self, sample):
        collect.update("news", feed={"urls": ["https://example.com/feed"], "since": "last_run"})
        stored = collect.get("news").feed
        assert stored["urls"] == ["https://example.com/feed"]
        assert stored["since"] == "last_run"

    def test_an_empty_object_takes_the_tool_off(self, sample):
        collect.update("news", feed={"urls": ["https://example.com/feed"]})
        collect.update("news", feed={})
        assert collect.get("news").feed is None

    def test_a_broken_spec_is_refused_when_it_is_written(self, sample):
        """実行時に落ちると、無人で回っている最中に「集められなかった」だけが残る。"""
        with pytest.raises(HTTPException):
            collect.update("news", feed={"urls": ["ftp://example.com/feed"]})


class TestFocus:
    """割り込み —— 「ここが間違っているから直して」を、巡回とは別の道で頼む。

    約束は 1 つ、**定時の巡回に影響を出さない**こと。進み具合・どの巡回の予定・
    区画の巡回記録のどれも動かさない —— 動くのは中身だけ。
    """

    @pytest.fixture
    def ready(self, sample):
        collect.update(
            "news",
            enabled=True,
            cursor="2026-09-01",
            partition={"by": "title", "target": 100},
            partitions=[{"key": k, "count": 1} for k in ["A", "B"]],
        )
        return collect.get("news")

    def test_a_request_without_a_note_is_refused(self, ready):
        """何をどう直すかが無い割り込みは、1 回ぶんの AI の呼び出しにしかならない。"""
        with pytest.raises(HTTPException):
            collect.require_focus({"titles": ["X"]})

    def test_it_does_not_move_the_cursor_or_the_clock(self, ready):
        collect.request_focus(
            "news", collect.require_focus({"note": "住所を直して", "titles": ["○○食堂"]})
        )
        before = collect.get("news")
        collect.record_result(
            "news", status="ok", next_cursor="2026-09-30", visited=["A"], focus=True
        )
        after = collect.get("news")
        assert after.cursor == "2026-09-01"
        assert after.next_run_at == before.next_run_at
        # 見ていない区画に印が付かない
        assert after.partitions == before.partitions

    def test_a_normal_run_still_moves_everything(self, ready):
        """割り込みだけが特別。ふつうの回は今までどおり進む。"""
        before = collect.get("news")
        collect.record_result("news", status="ok", next_cursor="2026-09-30", visited=["A"])
        after = collect.get("news")
        assert after.cursor == "2026-09-30"
        assert after.next_run_at != before.next_run_at
        assert partitioning.progress(after.partitions, collect.DEFAULT_SWEEP_NAME) == (1, 2)

    def test_the_request_is_cleared_even_when_it_fails(self, ready):
        """残すと、次に走る定時の回が割り込みとして走ってしまう。"""
        collect.request_focus("news", collect.require_focus({"note": "直して"}))
        collect.record_result("news", status="error", error="落ちた", focus=True)
        assert collect.get("news").pending_focus is None

    def test_the_named_ones_are_always_shown(self, ready):
        """区画を渡すだけでは、直してほしい 1 件が差し込みに載る保証がない。"""
        focus = collect.normalize_focus({"note": "住所を直して", "titles": ["○○食堂"]})
        previous = {
            "○○食堂": {"title": "○○食堂", "body": "旧住所", "tags": ["飲食店"]},
            "別の店": {"title": "別の店", "body": "関係ない"},
        }
        user = collect.build_messages(
            collect.get("news"), previous, None, {}, None, focus
        )[1]["content"]
        assert "住所を直して" in user
        assert "○○食堂" in user
        assert "いつもの巡回ではありません" in user

    def test_it_does_not_call_a_focus_the_whole_thing(self, ready):
        """区画を渡されていない割り込みの対象は名指しされたものだけで、範囲ではない。"""
        collect.update("news", prompt="この範囲: {partition}")
        focus = collect.normalize_focus({"note": "直して", "titles": ["A"]})
        user = collect.build_messages(collect.get("news"), {}, None, {}, None, focus)[1]["content"]
        assert "(全体)" not in user
        assert "名指しされたものが対象" in user

    def test_a_headline_it_does_not_have_is_said_so(self, ready):
        """名指しされたのに無いのは、見出しの書き方が違うか入っていないか。

        どちらも AI に伝わっていたほうが答えが良くなる(黙って落とさない)。
        """
        focus = collect.normalize_focus({"note": "直して", "titles": ["まだ無い店"]})
        user = collect.build_messages(collect.get("news"), {}, None, {}, None, focus)[1]["content"]
        assert "まだ無い店" in user
        assert "まだ入っていない見出し" in user

    def test_naming_alone_does_not_drag_in_everything(self, ready):
        """区画を渡されていないのに全件を差し込むと、直す相手が切り落とされる。"""
        focus = collect.normalize_focus({"note": "直して", "titles": ["A店"]})
        previous = {f"店{i}": {"title": f"店{i}", "body": "本文"} for i in range(100)}
        previous["A店"] = {"title": "A店", "body": "本文"}
        docs, scoped = collect.scoped_docs(collect.get("news"), previous, None, focus)
        assert list(docs) == ["A店"]
        assert scoped is True

    def test_it_is_always_a_refine(self, ready):
        """足すだけの収集でも、名指しで渡された 1 件を直せないと割り込みの意味が無い。"""
        assert not collect.get("news").is_refine()
        focus = collect.normalize_focus({"note": "直して"})
        system = collect.build_messages(
            collect.get("news"), {}, None, {}, None, focus
        )[0]["content"]
        assert "墓標" in system

    def test_too_many_names_are_cut(self, ready):
        """名指しは訂正のためのもの。数十件も並べるなら区画を指すほうが早い。"""
        focus = collect.normalize_focus(
            {"note": "直して", "titles": [f"店{i}" for i in range(200)]}
        )
        assert len(focus.titles) == collect.MAX_FOCUS_TITLES


class TestTheMechanicalSweep:
    """機械で引く巡回 —— 名簿を最新に保つための回。

    機械で埋めるのは「進み具合が空の 1 回目だけ」だった。外のカテゴリは増えていくのに、
    そのあと増えたぶんは永遠に入らない。**足すだけと組にして使う** ——
    組にしないと、AI が肉付けしたぶんを名簿の薄い内容で上書きする。
    """

    @pytest.fixture
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_the_sweep_carries_the_mark(self, sample):
        collect.update("news", sweeps=[
            {"name": "名簿", "interval_minutes": 1440, "use_extract": True, "only_new": True},
            {"name": "肉付け", "interval_minutes": 360},
        ])
        roster, flesh = collect.sweeps_of(collect.get("news"))
        assert (roster.use_extract, roster.only_new) == (True, True)
        assert (flesh.use_extract, flesh.only_new) == (False, False)

    def test_it_pulls_from_the_index_even_after_the_cursor_moved(self, sample, monkeypatch):
        """1 回目だけでなく、頼まれた回はいつでも機械で引く。"""
        import asyncio

        from app import extract, main

        collect.update(
            "news",
            cursor="2026-09-01",
            extract={"source": "jawiki", "tag": "画家"},
            sweeps=[{"name": "名簿", "use_extract": True, "only_new": True}],
        )
        monkeypatch.setattr(
            extract, "run", lambda spec, sources: ([{"title": "草間彌生", "body": "本文"}], "")
        )
        item = collect.get("news")
        items, _cursor, _note = asyncio.run(
            main._collect_items(item, {}, {}, [], collect.sweep_named(item, "名簿"))
        )
        assert [i["title"] for i in items] == ["草間彌生"]


class TestPerSweepPrompt:
    """依頼文は巡回ごとに書ける。**空なら収集のもの**。

    頼むことが巡回ごとに違う(埋める / 見直して消す / 漏れを足す)のに、1 つの文で
    全部を頼むと、どの回も同じ薄さの仕事になる。
    """

    def test_a_sweep_can_have_its_own(self, sample):
        collect.update("news", sweeps=[
            {"name": "更新", "interval_minutes": 360},
            {"name": "漏れ探し", "interval_minutes": 1440, "prompt": "{cursor} 以降で漏れを足して"},
        ])
        update, find = collect.sweeps_of(collect.get("news"))
        # 書かなかったほうは収集のものを引き継ぐ
        assert update.prompt == collect.get("news").prompt
        assert find.prompt == "{cursor} 以降で漏れを足して"

    def test_the_sweep_prompt_is_what_gets_asked(self, sample):
        collect.update("news", sweeps=[{"name": "漏れ探し", "prompt": "漏れを足して"}])
        sweep = collect.sweep_named(collect.get("news"), "漏れ探し")
        user = collect.build_messages(collect.get("news"), {}, None, {}, sweep)[1]["content"]
        assert "漏れを足して" in user

    def test_a_refine_sweep_without_the_material_is_refused(self, sample):
        """整理の回に `{current}` が無いと、AI は今あるものを知らないまま書く。

        保存してしまうと、次に走ったときに初めて分かる(無人で回る層なので誰も見ていない)。
        """
        collect.update("news", mode="refine", prompt="いまの内容:\n{current}\n直して")
        with pytest.raises(HTTPException):
            collect.update("news", sweeps=[{"name": "漏れ探し", "prompt": "漏れを足して"}])


class TestNoGapBetweenPartitions:
    """見出しで割った区画に**隙間を作らない**。

    鍵は「その区画の最初の見出し〜最後の見出し」なので、鍵の範囲だけで判ずると
    **区画と区画のあいだが誰のものでもなくなる** —— あとから足した見出しがそこへ
    落ちると、以後どの回にも出てこない(`{current}` にも入らないので AI からも見えない)。
    本番の台帳では境目が 324 か所あり、足した見出しのおよそ 25 件に 1 件が当たる。
    """

    @pytest.fixture
    def ledger(self):
        return [
            {"key": partitioning.title_key("あ", "お"), "count": 10},
            {"key": partitioning.title_key("さ", "そ"), "count": 10},
        ]

    def _spec(self):
        return partitioning.normalize({"by": "title", "target": 10})

    def test_a_headline_between_two_partitions_has_a_home(self, ledger):
        # 「か」は「お」と「さ」のあいだ —— 鍵の範囲だけ見るとどこにも入らない
        spec = self._spec()
        assert not partitioning.belongs(spec, ledger[0]["key"], {"title": "か"})
        assert not partitioning.belongs(spec, ledger[1]["key"], {"title": "か"})
        # 手前の区画のものとして扱う
        assert partitioning.partition_of(spec, ledger, {"title": "か"}) == ledger[0]["key"]

    def test_a_headline_before_everything_has_a_home(self, ledger):
        """いちばん手前より前も、最初の区画のもの(端にも隙間を作らない)。"""
        assert partitioning.partition_of(
            self._spec(), ledger, {"title": "ああ"}
        ) == ledger[0]["key"]

    def test_the_material_shows_it(self, sample):
        """区画の素材に入らなければ、AI からは無かったことになる。"""
        collect.update(
            "news",
            mode="refine",
            prompt="いまの内容:\n{current}\n直して",
            partition={"by": "title", "target": 10},
            partitions=[
                {"key": partitioning.title_key("あ", "お"), "count": 10},
                {"key": partitioning.title_key("さ", "そ"), "count": 10},
            ],
        )
        previous = {"か": {"title": "か", "body": "あとから足したもの"}}
        docs, scoped = collect.scoped_docs(
            collect.get("news"), previous, partitioning.title_key("あ", "お")
        )
        assert list(docs) == ["か"]
        assert scoped is True


class TestAddingOnly:
    """足すだけの巡回 —— **既にある見出しには触らない**。

    「漏れているものを足して」と頼む回に要る印で、**AI の判断に頼らずにここで保証する**。
    見せられるのはその区画のぶんだけなので、AI には「もう居るかどうか」が分からない
    (別の括りに入っていることも、タグが間違っていることもある)。触らせると、既にいる
    有名なものが薄い内容で上書きされ、持っていたタグごと落ちる。
    """

    @pytest.fixture
    def refine(self, sample):
        collect.update("news", mode="refine", prompt="いまの内容:\n{current}\n直して")
        return collect.get("news")

    def test_an_existing_headline_is_left_alone(self, refine):
        previous = {
            "ゴッホ": {"doc_id": 1, "title": "ゴッホ", "body": "詳しい本文",
                      "tags": ["画家", "様式:ポスト印象派", "代表作:ひまわり"]},
        }
        # AI が「もう居る」と知らずに、薄い内容で返してきた
        docs, diff = collect.material(
            refine, previous,
            [{"title": "ゴッホ", "body": "画家です", "tags": ["画家"]}],
            only_new=True,
        )
        kept = {d["title"]: d for d in docs}["ゴッホ"]
        assert kept["body"] == "詳しい本文"
        assert "代表作:ひまわり" in kept["tags"]
        assert diff["updated"] == 0
        assert diff["skipped"] == 1

    def test_a_new_headline_is_added(self, refine):
        docs, diff = collect.material(
            refine, {"ゴッホ": {"doc_id": 1, "title": "ゴッホ", "body": "本文"}},
            [{"title": "草間彌生", "body": "画家です", "tags": ["画家"]}],
            only_new=True,
        )
        assert diff["added"] == 1
        assert diff["added_titles"] == ["草間彌生"]
        assert {d["title"] for d in docs} == {"ゴッホ", "草間彌生"}

    def test_it_cannot_delete(self, refine):
        """足すだけの回に消す力を持たせない。"""
        previous = {"ゴッホ": {"doc_id": 1, "title": "ゴッホ", "body": "本文"}}
        docs, diff = collect.material(
            refine, previous,
            [{"title": "ゴッホ", "tags": [notes.TOMBSTONE_TAG]}],
            only_new=True,
        )
        assert diff["removed"] == 0
        assert [d["title"] for d in docs] == ["ゴッホ"]

    def test_without_the_mark_it_updates_as_before(self, refine):
        """印を付けていない回は今までどおり(直しに来ているので置き換わる)。"""
        previous = {"ゴッホ": {"doc_id": 1, "title": "ゴッホ", "body": "古い本文"}}
        docs, diff = collect.material(
            refine, previous, [{"title": "ゴッホ", "body": "新しい本文"}]
        )
        assert diff["updated"] == 1
        assert docs[0]["body"] == "新しい本文"

    def test_the_sweep_carries_the_mark(self, sample):
        collect.update("news", sweeps=[
            {"name": "更新", "interval_minutes": 360},
            {"name": "漏れ探し", "interval_minutes": 1440, "only_new": True},
        ])
        update, find = collect.sweeps_of(collect.get("news"))
        assert update.only_new is False
        assert find.only_new is True


class TestKeepingTheClockAcrossAPatch:
    """設定を送り直しても、巡回の進み具合は引き継ぐ。

    **これが無いと、設定を更新するたびに全部の巡回がいますぐ走る。** 予定を
    持っていない巡回は「いますぐ」として扱う規則があるので、次回の予定を落とした
    瞬間に全部が due になる —— 押した人は設定を直しただけのつもりなのに、
    収集が 1 本走り出す(実際にそうなった)。
    """

    @pytest.fixture
    def running(self, sample):
        collect.update("news", enabled=True, sweeps=[
            {"name": "ざっと", "interval_minutes": 360},
            {"name": "じっくり", "interval_minutes": 1440},
        ])
        # 1 回走ったことにして、予定と前回を持たせる
        collect.mark_started("news", "ざっと")
        collect.record_result("news", status="ok", added=3, sweep="ざっと")
        return collect.get("news")

    def test_the_schedule_survives_a_settings_patch(self, running):
        before = collect.sweep_named(running, "ざっと")
        assert before.next_run_at and before.last_run_at

        # 外のアプリが送ってくるのは設定だけ(予定は持っていない)
        collect.update("news", sweeps=[
            {"name": "ざっと", "interval_minutes": 360, "backend": "codex"},
            {"name": "じっくり", "interval_minutes": 1440},
        ])
        after = collect.sweep_named(collect.get("news"), "ざっと")

        assert after.backend == "codex"
        assert after.next_run_at == before.next_run_at
        assert after.last_run_at == before.last_run_at
        assert after.last_status == before.last_status

    def test_it_does_not_start_running_right_after_a_patch(self, running):
        """設定を直しただけで収集が走り出さないこと(これが本題)。"""
        collect.update("news", sweeps=[
            {"name": "ざっと", "interval_minutes": 360, "backend": "codex"},
        ])
        assert collect.sweep_named(collect.get("news"), "ざっと").is_due() is False

    def test_a_new_sweep_still_runs_at_once(self, running):
        """足したばかりの巡回は今までどおり「いますぐ」。"""
        collect.update("news", sweeps=[
            {"name": "ざっと", "interval_minutes": 360},
            {"name": "三本目", "interval_minutes": 60},
        ])
        assert collect.sweep_named(collect.get("news"), "三本目").is_due() is True

    def test_an_explicit_schedule_still_wins(self, running):
        """名指しで書かれていればそちらが勝つ(予定を動かしたい呼び出しは通る)。"""
        collect.update("news", sweeps=[
            {"name": "ざっと", "interval_minutes": 360,
             "next_run_at": "2030-01-01T00:00:00+00:00"},
        ])
        after = collect.sweep_named(collect.get("news"), "ざっと")
        assert after.next_run_at == "2030-01-01T00:00:00+00:00"


class TestTheOnDemandSweep:
    """割り込みも巡回として定義しておく —— **時計を持たない 1 本**。

    定時の巡回と同じ書き方で置けるので、頼む相手を割り込みだけ別にできる。
    人が待っている場面なので速い相手に頼みたい / 1 件をじっくり調べさせたい、の
    どちらもあり、定時の設定を流用すると「どちらの都合で選んだ相手か」が言えない。
    """

    @pytest.fixture
    def ready(self, sample):
        collect.update(
            "news",
            enabled=True,
            sweeps=[
                {"name": "ざっと", "interval_minutes": 360, "backend": "codex"},
                {"name": "じっくり", "interval_minutes": 1440, "backend": "claude",
                 "effort": "high"},
                {"name": "割り込み", "on_demand": True, "backend": "antigravity"},
            ],
        )
        return collect.get("news")

    def test_it_never_comes_due(self, ready):
        """時計を持たない巡回は、定時には走らない(頼まれたときだけ)。"""
        due = {s.name for _c, s in collect.due_sweeps()}
        assert "割り込み" not in due
        assert "ざっと" in due

    def test_it_does_not_drag_the_collection_forward(self, ready):
        """予定が空なのを「いますぐ」と読む規則をそのまま当てると、毎周走ってしまう。"""
        sweep = collect.sweep_named(ready, "割り込み")
        assert sweep.on_demand is True
        assert sweep.is_due() is False
        assert sweep.due_at() == collect.NEVER

    def test_a_focus_runs_with_its_own_backend(self, ready):
        """割り込みは割り込み用の巡回で走る。"""
        focus = collect.normalize_focus({"note": "直して"})
        assert collect.sweep_for_focus(ready, focus).backend == "antigravity"

    def test_a_focus_can_name_another_sweep(self, ready):
        """「じっくりの相手で、いま 1 回だけ」を頼めるようにするため。"""
        focus = collect.normalize_focus({"note": "直して", "sweep": "じっくり"})
        picked = collect.sweep_for_focus(ready, focus)
        assert (picked.backend, picked.effort) == ("claude", "high")

    def test_without_one_it_falls_back_to_the_next_scheduled_sweep(self, sample):
        """定義していない収集でも割り込みは頼める。"""
        collect.update("news", enabled=True, backend="codex")
        focus = collect.normalize_focus({"note": "直して"})
        assert collect.sweep_for_focus(collect.get("news"), focus).backend == "codex"

    def test_a_scheduled_run_never_picks_it(self, ready):
        """起こした巡回が分からなくなったときも、時計を持たない側へは倒さない。"""
        assert collect.sweep_named(ready, "消えた巡回").on_demand is False


class TestTheInjectedPrompt:
    """その回だけの依頼文。**保存しない**。

    定義のプロンプトは育てながら使うもので、1 回きりの頼みごとで書き換わると、
    次の定時の回が知らない文で走ることになる。
    """

    @pytest.fixture
    def ready(self, sample):
        collect.update("news", enabled=True, prompt="いつもの依頼文 {cursor}")
        return collect.get("news")

    def test_the_given_one_is_used_for_that_run(self, ready):
        focus = collect.normalize_focus(
            {"note": "直して", "prompt": "この回だけの依頼文 {cursor}"}
        )
        user = collect.build_messages(ready, {}, None, {}, None, focus)[1]["content"]
        assert "この回だけの依頼文" in user
        assert "いつもの依頼文" not in user

    def test_the_placeholders_still_work(self, ready):
        collect.update("news", cursor="2026-09-01")
        focus = collect.normalize_focus({"note": "直して", "prompt": "続きから: {cursor}"})
        user = collect.build_messages(collect.get("news"), {}, None, {}, None, focus)[1]["content"]
        assert "続きから: 2026-09-01" in user

    def test_it_is_not_saved(self, ready):
        collect.request_focus(
            "news", collect.require_focus({"note": "直して", "prompt": "この回だけ"})
        )
        assert collect.get("news").prompt == "いつもの依頼文 {cursor}"

    def test_without_one_the_saved_prompt_is_used(self, ready):
        focus = collect.normalize_focus({"note": "直して"})
        user = collect.build_messages(ready, {}, None, {}, None, focus)[1]["content"]
        assert "いつもの依頼文" in user


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
        items, cursor, note = collect.parse_response(
            '調べました。\n```json\n{"items":[{"title":"X","body":"Y"}],"next_cursor":"Z"}\n```'
        )
        assert items == [{"title": "X", "body": "Y"}]
        assert cursor == "Z"
        assert note == ""

    def test_a_cut_off_answer_keeps_what_was_read(self):
        """**丸ごと捨てない。** 答えが長くなると相手の上限に当たって末尾が欠けることが
        あり、実際に本番で起きた。捨てると、その回に払った AI の呼び出しが全部無駄に
        なるうえ、同じ区画を次も同じ長さで聞くので繰り返し落ちる。
        """
        items, cursor, note = collect.parse_response(
            '{"items": [{"title": "A", "body": "あ"},'
            ' {"title": "B", "body": "い{ろ}は"}, {"title": "C", "bo'
        )
        # 本文に「{」が入っていても数え違えない
        assert [i["title"] for i in items] == ["A", "B"]
        # 切れているので次の印は読めない（半端な値を進めると、そこから先が飛ぶ）
        assert cursor is None
        # **拾ったことは黙っていない**
        assert "2 件" in note

    def test_a_cut_off_answer_with_nothing_readable_is_still_an_error(self):
        with pytest.raises(ValueError):
            collect.parse_response('{"items": [{"title": "A"')

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

    def test_creating_carries_the_sweeps(self, client, sample):
        """作る口が受け取らないと、外のアプリが渡した巡回が黙って落ちる。

        PATCH にだけ足して作成側を忘れていたので、antenna が作った収集は巡回を
        1 本も持たないまま動いていた(渡したほうには何も返らない)。
        """
        client.post(
            "/v1/collect",
            json={
                "name": "two_ways",
                "prompt": "{partition} {current}",
                "mode": "refine",
                "interval_minutes": 360,
                "partition": {"by": "title", "target": 20},
                "sweeps": [
                    {"name": "ざっと", "interval_minutes": 360, "cover_days": 7},
                    {"name": "じっくり", "interval_minutes": 1440, "effort": "high"},
                ],
            },
        )
        body = client.get("/v1/collect/two_ways").json()
        assert [s["name"] for s in body["sweeps"]] == ["ざっと", "じっくり"]
        assert body["sweeps"][1]["effort"] == "high"
        assert body["partition"] == {"by": "title", "target": 20}

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

    def test_a_sweep_can_be_named_when_running_now(self, client, sample, monkeypatch):
        """「じっくりのほうを今すぐ 1 回」が頼めないと、分けて持った意味が半分になる。"""
        from app.views import admin

        monkeypatch.setattr(admin, "trigger_run", lambda _source: None)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        collect.update("news", enabled=True, sweeps=[
            {"name": "ざっと", "interval_minutes": 360},
            {"name": "じっくり", "interval_minutes": 1440},
        ])

        res = client.post("/v1/collect/news/run", params={"sweep": "じっくり"})
        assert res.status_code == 200
        assert collect.get("news").pending_sweep == "じっくり"

    def test_a_clockless_sweep_cannot_be_run_on_its_own(self, client, sample, monkeypatch):
        """**画面でボタンを出さないだけにしない。** 口が受け付けるなら、いつか誰かが叩く。

        時計を持たない巡回は自前の依頼文を持たないので、そのまま走らせても
        収集のプロンプトで普通の回が 1 本増えるだけになる。
        """
        from app.views import admin

        woken = []
        monkeypatch.setattr(admin, "trigger_run", woken.append)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        collect.update("news", enabled=True, sweeps=[
            {"name": "ざっと", "interval_minutes": 360},
            {"name": "割り込み", "on_demand": True},
        ])

        res = client.post("/v1/collect/news/run", params={"sweep": "割り込み"})
        assert res.status_code == 400
        # **起こす前に断る** —— 起こしてから断ると、1 本ぶんの取り込みが空振りする
        assert woken == []
        assert client.post(
            "/v1/collect/news/preview", params={"sweep": "割り込み"}
        ).status_code == 400

    def test_an_unknown_sweep_is_refused_instead_of_falling_back(self, client, sample, monkeypatch):
        """名指しで押した側に別の巡回を走らせて返すのは、意図と違うものが動いたことになる。"""
        from app.views import admin

        woken = []
        monkeypatch.setattr(admin, "trigger_run", woken.append)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        collect.update("news", enabled=True, sweeps=[{"name": "ざっと", "interval_minutes": 360}])

        assert client.post(
            "/v1/collect/news/run", params={"sweep": "消えた巡回"}
        ).status_code == 404
        assert woken == []

    def test_the_request_is_written_before_the_ingest_is_woken(self, client, sample, monkeypatch):
        """**控えるのが先、起こすのが後。**

        逆にすると、起こされた取り込みが素材を取りに来たときにまだ依頼が書かれて
        おらず、その回はふつうの巡回として走る —— 依頼は残るので、次の定時の回を
        乗っ取る。押した人からは「押した瞬間に巡回が前倒しで動いただけ」に見えて、
        頼んだものはいつまでも走らない。
        """
        from app.views import admin

        seen = {}

        def fake_trigger(source):
            # 起こされた時点で、もう依頼が読める状態になっていること
            seen["pending"] = collect.get(source).pending_focus

        monkeypatch.setattr(admin, "trigger_run", fake_trigger)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        collect.update("news", enabled=True)

        res = client.post("/v1/collect/news/focus", json={"note": "直して", "titles": ["A"]})
        assert res.status_code == 200
        assert seen["pending"]["note"] == "直して"

    def test_a_refused_wake_up_takes_the_request_back(self, client, sample, monkeypatch):
        """起こせなかったのに依頼だけ残すと、次に走る定時の回が割り込みとして走る。"""
        from app.views import admin

        def refuse(_source):
            raise HTTPException(409, {"error": "取り込みが走っています"})

        monkeypatch.setattr(admin, "trigger_run", refuse)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        collect.update("news", enabled=True)

        assert client.post("/v1/collect/news/focus", json={"note": "直して"}).status_code == 409
        assert collect.get("news").pending_focus is None

    def test_the_prompt_rides_along_without_being_saved(self, client, sample, monkeypatch):
        """その回だけの依頼文。**定義のプロンプトは育てたまま**にしておく。"""
        from app.views import admin

        monkeypatch.setattr(admin, "trigger_run", lambda _source: None)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        collect.update("news", enabled=True, prompt="いつもの依頼文 {cursor}")

        res = client.post(
            "/v1/collect/news/focus",
            json={"note": "直して", "prompt": "この回だけ {cursor}", "sweep": "じっくり"},
        )
        assert res.status_code == 200
        item = collect.get("news")
        assert item.prompt == "いつもの依頼文 {cursor}"
        assert item.pending_focus["prompt"] == "この回だけ {cursor}"
        assert item.pending_focus["sweep"] == "じっくり"

    def test_focusing_on_a_stopped_collection_is_refused(self, client, sample):
        """割り込みも AI を 1 回動かすので、`run` と同じ扱いにする。"""
        res = client.post("/v1/collect/news/focus", json={"note": "直して"})
        assert res.status_code == 403

    def test_a_focus_without_a_note_is_refused(self, client, sample):
        collect.update("news", enabled=True)
        assert client.post("/v1/collect/news/focus", json={"note": " "}).status_code == 400

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

    def test_each_sweep_can_be_run_on_its_own(self, sample):
        """相手も 1 回に見る量も巡回ごとに違うので、収集に 1 つだけ置くと
        「どの設定で走ったのか」が押した本人にも読めない。
        """
        collect.update("news", sweeps=[
            {"name": "ざっと", "interval_minutes": 360},
            {"name": "じっくり", "interval_minutes": 1440},
        ])
        html = self._html(sample)
        assert html.count(">今すぐ実行</button>") == 2
        assert html.count(">ドライラン</button>") == 2
        assert html.count('name="sweep" value="じっくり"') == 2

    def test_a_clockless_sweep_has_no_buttons(self, sample):
        """割り込み用の 1 本は、自前の依頼文を持たないので単独では走らせない。"""
        collect.update("news", sweeps=[
            {"name": "ざっと", "interval_minutes": 360},
            {"name": "割り込み", "on_demand": True},
        ])
        html = self._html(sample)
        assert html.count(">今すぐ実行</button>") == 1
        assert 'name="sweep" value="割り込み"' not in html
        assert "時計なし" in html

    def test_the_changes_say_who_ran_it(self, sample, monkeypatch, tmp_path):
        """巡回ごとに相手を変えられるので、回の名前だけでは何で走ったのか読めない。"""
        from app import collect_log

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"total": 3, "added": 3},
            sweep="じっくり", backend="claude", model="opus", effort="high",
        )
        html = self._html(sample)
        assert "claude" in html
        assert "opus / high" in html

    def test_the_partition_progress_is_shown(self, sample):
        """一周したかどうかが読めて初めて、間隔と 1 回あたりの量を判断できる。"""
        collect.update(
            "news",
            partition={"by": "title", "target": 100},
            partitions=[
                {"key": "あ〜き", "count": 100, "visits": {"ざっと": "2026-09-01T00:00:00+00:00"}},
                {"key": "く〜そ", "count": 80},
            ],
            sweeps=[{"name": "ざっと", "cover_days": 7}],
        )
        html = self._html(sample)
        # 進み具合は巡回ごとに出る
        assert "2 のうち 1" in html
        assert "7 日で一周" in html
        assert "く〜そ" in html

    def test_a_collection_without_partitions_says_nothing(self, sample):
        assert "区画:" not in self._html(sample)

    def test_each_sweep_can_have_its_own_ai(self, sample):
        """ざっとは安い相手で数をこなし、じっくりは考える量を上げる、が書ける。"""
        collect.update(
            "news",
            sweeps=[
                {"name": "ざっと", "interval_minutes": 360},
                {"name": "じっくり", "interval_minutes": 1440, "model": "opus", "effort": "high"},
            ],
        )
        html = self._html(sample)
        assert "opus" in html and "high" in html

    def test_the_prompt_opens_in_a_row_of_its_own(self, sample):
        """名前のセルの中で開くと、巡回のぶん背の高い行のどこかに長いフォームが挟まり、
        どの巡回の設定を触っているのか分からなくなる。
        """
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        body = self._html(sample).split("<tbody>")[1].split("</tbody>")[0]

        # プロンプトは列をまたぐ行に出る（名前のセルの中ではない）
        header = self._html(sample).split("<thead>")[1].split("</thead>")[0]
        assert f'colspan="{header.count("<th>")}"' in body
        assert body.index("colspan=") > body.index("じっくり")

    def test_sweeps_are_fields_not_json(self, sample):
        """JSON を直に書かせると、間隔ひとつ変えるのに配列の構文を相手にすることになる。

        間隔・相手・モデル・考える量はもともと「1 本ぶんの設定」なので、その一組を
        繰り返せるようにすれば足りる。
        """
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        html = self._html(sample)

        assert '<textarea name="sweeps"' not in html
        assert html.count('<input name="sweep_name"') == 3  # 2 本 + 足すための空枠
        assert '<select name="sweep_backend">' in html
        assert "名前を消すと、この巡回は無くなります" in html

    def test_one_sweep_needs_no_name_to_delete(self, sample):
        """1 本しか無いときは、消す案内を出さない（消したら回らなくなる）。"""
        html = self._html(sample)
        assert "名前を消すと、この巡回は無くなります" not in html
        assert "名前を書くと増えます" in html

    def test_each_sweep_gets_its_own_row(self, sample):
        """間隔も次の予定も前回も巡回ごとに違うので、収集に 1 行だけ与えると嘘になる。

        **折り畳みの中ではなく表に出す** —— この表は「動いているか」を読むためのもの
        なので、いちいち開かせるなら出していないのと同じ。
        """
        collect.update(
            "news",
            sweeps=[
                {"name": "ざっと", "interval_minutes": 360},
                {"name": "じっくり", "interval_minutes": 1440},
            ],
        )
        html = self._html(sample)
        body = html.split("<tbody>")[1].split("</tbody>")[0]

        # 名前と操作は行をまたがせる（収集のものなので）
        assert 'rowspan="2"' in body
        # 巡回は表にそのまま並ぶ
        assert "ざっと" in body and "じっくり" in body
        assert "1440 分ごと" in body

    def test_a_collection_without_sweeps_still_shows_one(self, sample):
        """定義そのものが 1 本の巡回として動くので、行が消えると止まって見える。"""
        html = self._html(sample)
        assert collect.DEFAULT_SWEEP_NAME in html

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


class TestWhatChangedInOneDoc:
    """動いた 1 件が、どう書き換わったかを出す。

    「直近の変更」に並ぶのは見出しの名前までで、そこからは何が変わったのか読めない
    —— 件数と名前が分かっても、プロンプトを直す判断に要るのは中身の変化のほう。
    """

    @pytest.fixture()
    def generations(self, tmp_path):
        """世代ファイルを 2 つと、シンボリックリンクを作る(焼き上がりと同じ形)。"""
        from app import notes

        corpus = tmp_path / "corpus"
        corpus.mkdir()

        def write(stamp, docs):
            path = corpus / f"news-{stamp}.db"
            conn = sqlite3.connect(path)
            conn.executescript(notes.SCHEMA_DDL)
            for i, (title, body, tags) in enumerate(docs, start=1):
                conn.execute(
                    "INSERT INTO docs (doc_id, title, opening, body, tags, updated_at,"
                    " rank_score) VALUES (?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', 0.0)",
                    (i, title, body, body, json.dumps(tags, ensure_ascii=False)),
                )
            conn.commit()
            conn.close()
            return path

        write("20260101000000", [
            ("残る店", "旧住所にあります。\n電話は 03-0000-0000。", ["飲食店"]),
            ("消える店", "本文", []),
        ])
        live = write("20260102000000", [
            ("残る店", "新住所へ移転しました。\n電話は 03-0000-0000。", ["飲食店", "移転"]),
            ("増える店", "新しく入りました。", []),
        ])
        link = corpus / "news.db"
        link.symlink_to(live)

        class Src:
            path = link
            dump_date = "20260102000000"

        return {"news": Src()}

    def test_it_shows_both_generations(self, generations):
        v = collect.doc_versions("news", generations, "残る店")
        assert v["before"]["body"].startswith("旧住所")
        assert v["now"]["body"].startswith("新住所")
        assert (v["before_stamp"], v["now_stamp"]) == ("20260101000000", "20260102000000")

    def test_an_added_one_has_no_before(self, generations):
        v = collect.doc_versions("news", generations, "増える店")
        assert v["before"] is None
        assert v["now"]["body"] == "新しく入りました。"

    def test_a_removed_one_has_no_now(self, generations):
        v = collect.doc_versions("news", generations, "消える店")
        assert v["now"] is None
        assert v["before"]["body"] == "本文"

    def test_an_unknown_title_breaks_nothing(self, generations):
        v = collect.doc_versions("news", generations, "知らない店")
        assert (v["now"], v["before"]) == (None, None)

    def test_the_page_shows_what_moved(self, generations):
        from app.views import admin

        html = admin._doc_diff_page_html(
            "news", "残る店", collect.doc_versions("news", generations, "残る店")
        )
        # どの世代どうしを比べたかを必ず出す（古い回の行から来た人が取り違えないように）
        assert "1 つ前(2026-01-01 00:00) → いま(2026-01-02 00:00)" in html
        assert "書き換わったもの" in html
        # 本文は行単位の差分、タグは別に出す
        assert "旧住所にあります。" in html and "新住所へ移転しました。" in html
        assert "足したタグ" in html and "移転" in html
        # 動いていない行は差分に出ない（n=2 の文脈としては出るので、印だけ見る）
        assert '<span class="added">' in html and '<span class="removed">' in html

    def test_the_page_says_when_there_is_nothing_to_compare(self, generations):
        from app.views import admin

        html = admin._doc_diff_page_html(
            "news", "知らない店", collect.doc_versions("news", generations, "知らない店")
        )
        assert "いまの世代にも 1 つ前の世代にもありません" in html

    def test_the_titles_in_the_changes_are_clickable(self, sample, monkeypatch, tmp_path):
        """名前だけ並べても「どう書き換わったか」は読めない。"""
        from app import collect_log
        from app.views import admin

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        collect_log.record(
            "news", status=collect_log.STATUS_OK,
            diff={"total": 1, "updated": 1, "updated_titles": ["A & B"]},
        )
        html = admin._collect_html({}, "")
        assert "/admin/collect/news/doc?title=A%20%26%20B" in html
        # 見出しそのものは HTML としてエスケープして出す
        assert ">A &amp; B</a>" in html


class TestEditingTheSweeps:
    """巡回の欄は繰り返せる。**名前を書けば増え、消せば減る**。

    足す口も消す口も名前 1 つで済ませてある —— 行ごとにボタンを付けると、押した先で
    何が起きるかを別に説明することになる。
    """

    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def _save(self, client, rows, **extra):
        # **繰り返す欄は「鍵 → 値の並び」で渡す。** タプルの列で渡すと httpx が
        # 生のデータとして扱い、フォームとして届かない
        data = {"prompt": "{cursor} 以降", "description": "", "cursor": "",
                "mode": "append", "keep_ratio": "", "extract": "",
                "partition": "", "feed": "", **extra}
        for key in ("name", "interval", "cover_days", "per_run",
                    "backend", "model", "effort", "enabled", "clock", "merge"):
            data[f"sweep_{key}"] = [row.get(key, "") for row in rows]
        res = client.post("/admin/collect/news/edit", data=data, follow_redirects=False)
        assert res.status_code in (200, 303), res.text[:400]
        return res

    def test_writing_a_name_adds_one(self, client, sample):
        self._save(client, [
            {"name": "ざっと", "interval": "360", "cover_days": "7", "enabled": "1"},
            {"name": "じっくり", "interval": "1440", "per_run": "1",
             "backend": "claude", "model": "opus", "effort": "high", "enabled": "1"},
        ])
        rough, deep = collect.sweeps_of(collect.get("news"))
        assert (rough.name, rough.interval_minutes, rough.cover_days) == ("ざっと", 360, 7.0)
        assert (deep.name, deep.partitions_per_run, deep.model) == ("じっくり", 1, "opus")
        assert deep.effort == "high"

    def test_a_sweep_can_be_marked_add_only(self, client, sample):
        """漏れを足す回に、消す力も上書きする力も持たせない。"""
        self._save(client, [
            {"name": "更新", "interval": "360", "enabled": "1", "clock": "interval",
             "merge": "all"},
            {"name": "漏れ探し", "interval": "1440", "enabled": "1", "clock": "interval",
             "merge": "only_new"},
        ])
        update, find = collect.sweeps_of(collect.get("news"))
        assert update.only_new is False
        assert find.only_new is True

    def test_a_form_without_the_merge_field_does_not_change_it(self, client, sample):
        """欄を持たないフォームから保存されても、集め方が黙って変わらないこと。"""
        self._save(client, [{"name": "更新", "interval": "360", "enabled": "1"}])
        assert collect.sweeps_of(collect.get("news"))[0].only_new is False

    def test_a_sweep_can_be_left_without_a_clock(self, client, sample):
        """割り込み用の 1 本。定時には走らず、頼まれたときだけ動く。"""
        self._save(client, [
            {"name": "ざっと", "interval": "360", "enabled": "1", "clock": "interval"},
            {"name": "割り込み", "backend": "codex", "enabled": "1", "clock": "on_demand"},
        ])
        rough, on_demand = collect.sweeps_of(collect.get("news"))
        assert rough.on_demand is False
        assert on_demand.on_demand is True
        assert on_demand.is_due() is False
        assert on_demand.backend == "codex"

    def test_a_form_without_the_clock_still_runs_on_a_clock(self, client, sample):
        """**時計を失うのは名指しされたときだけ。** 欄を持たないフォームから保存されても、
        全部の巡回が黙って止まることがあってはならない。
        """
        self._save(client, [{"name": "ざっと", "interval": "360", "enabled": "1"}])
        assert collect.sweeps_of(collect.get("news"))[0].on_demand is False

    def test_clearing_the_name_removes_it(self, client, sample):
        self._save(client, [
            {"name": "ざっと", "interval": "360", "enabled": "1"},
            {"name": "じっくり", "interval": "1440", "enabled": "1"},
        ])
        assert len(collect.get("news").sweeps) == 2

        self._save(client, [
            {"name": "ざっと", "interval": "360", "enabled": "1"},
            {"name": "", "interval": "1440", "enabled": "1"},
        ])
        assert [s.name for s in collect.sweeps_of(collect.get("news"))] == ["ざっと"]

    def test_an_unnamed_lone_sweep_is_kept_as_the_collection_itself(self, client, sample):
        """1 本しか持たない収集に一覧を持たせると、「既定」という名前だけが画面に増える。

        **名前を付けたものは名前のまま残す** —— 2 本目を消したときに 1 本目の名前まで
        化けると、何を消したのか分からなくなる（上のテストがそこを縛っている）。
        """
        self._save(client, [{"name": "既定", "interval": "120",
                             "backend": "claude", "enabled": "1"}])
        item = collect.get("news")
        assert item.sweeps == []
        assert item.interval_minutes == 120
        assert item.backend == "claude"

    def test_a_stopped_sweep_survives_the_round_trip(self, client, sample):
        """止めるのに消さなくてよい（消すと進み具合まで消える）。"""
        self._save(client, [
            {"name": "ざっと", "interval": "360", "enabled": "1"},
            {"name": "じっくり", "interval": "1440", "enabled": ""},
        ])
        _rough, deep = collect.sweeps_of(collect.get("news"))
        assert deep.enabled is False


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
        # 巡回ごとに 1 組ずつ出る（相手も巡回ごとに変えられるので）
        assert '<select name="sweep_model">' in html
        assert '<select name="sweep_effort">' in html
        assert '<input name="sweep_model"' not in html
        assert '<input name="sweep_effort"' not in html

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

        assert 'select[name$="backend"]' in admin.COLLECT_BACKEND_SCRIPT
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
