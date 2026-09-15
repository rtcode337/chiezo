"""収集層(app/collect.py)のテスト。

**押さえているのは、この層でしか起きない 4 つ**:
①追記していく(全件の作り直しをしない)、②同じ見出しは飛ばす、
③実行ごとにカーソルが進む、④失敗しても次回の予定が入る。

溜め先が notes と別のソースになること(混ざらないこと)も見る —— これを崩すと
短期記憶が収集物で埋まって `recall` が使い物にならなくなる、という設計の芯。
"""
from __future__ import annotations

import datetime as dt
import itertools
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
    # **定義の置き場**(`state/machine.db`)。人が読む短期記憶とは別のファイル
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
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

    def test_without_a_place_for_the_definition_it_is_disabled(self, enabled, monkeypatch):
        """定義の置き場（`state/machine.db`）が無ければ、収集も成り立たない。

        **短期記憶とは別のファイル**。あちらは人と AI が読み書きする場所で、
        機械が毎回書き換える設定を混ぜると、人が消せてしまう。
        """
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        assert not collect.is_enabled()

    def test_it_does_not_need_the_short_term_memory(self, enabled, monkeypatch):
        """短期記憶が無くても成り立つ（定義はもうあちらに置いていない）。"""
        monkeypatch.delenv("CHIEZO_NOTES_DIR", raising=False)
        assert collect.is_enabled()

    def test_without_a_way_to_bake_it_is_disabled(self, enabled, monkeypatch):
        """取り込みを起こせない面では、定義を置いても永遠に走らない。

        corpus を持たない面(タスク専用の構成など)では、定義を置いても走らない。
        """
        monkeypatch.delenv("CHIEZO_TRIGGER_URL", raising=False)
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

        from app import machine_store

        machine_store.put(collect.DEFS_KIND, collect.DEFS_KEY, "これはJSONではない")
        with pytest.raises(fastapi.HTTPException):
            collect.load()


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
        self._with_ledger(["あ〜き", "く〜そ"])
        user = collect.build_messages(collect.get("news"), {}, "く〜そ")[1]["content"]
        assert "く" in user and "{partition}" not in user

    def test_the_only_partition_covers_everything(self, sample):
        """区画が 1 つしか無いなら、その 1 つが全部を引き受ける
        —— 鍵の範囲で伝えると、その外に漏れているものを誰も探しに行かない。"""
        collect.update("news", prompt="この範囲を調べて: {partition}")
        self._with_ledger(["あ〜き"])
        user = collect.build_messages(collect.get("news"), {}, "あ〜き")[1]["content"]
        assert "見出しは問いません" in user and "{partition}" not in user

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
        """割り込むたびに一周が伸びたり、進み具合が飛んだりしない。"""
        collect.request_focus(
            "news", collect.require_focus({"note": "住所を直して", "titles": ["○○食堂"]})
        )
        before = collect.get("news")
        # 画家だけを名指しした割り込みは、区画を渡さない(印も付かない)
        collect.record_result(
            "news", status="ok", next_cursor="2026-09-30", visited=[], focus=True
        )
        after = collect.get("news")
        assert after.cursor == "2026-09-01"
        assert after.next_run_at == before.next_run_at
        assert after.partitions == before.partitions

    def test_a_focus_on_a_partition_marks_it(self, ready):
        """**先に見てほしいところを頼んだのだから**、巡回が同じところを
        もう一度見る必要は無い。"""
        collect.record_result("news", status="ok", visited=["A"], focus=True)

        assert partitioning.progress(collect.get("news").partitions, collect.DEFAULT_SWEEP_NAME)[0] == 1
        # 時計と進み具合は動かないまま
        assert collect.get("news").cursor == "2026-09-01"

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
        # 足すだけの依頼文（今あるものを差し込んでいない）でも
        assert collect.edits_what_is_there(collect.get("news").prompt) is False
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

    def test_no_backend_can_be_set_on_a_mechanical_sweep(self, sample):
        """選んでも何も起きない欄は、設定したつもりを作る。

        機械で引く回は AI を呼ばずに返すので、相手もモデルも読まれない。
        """
        from app.views import admin

        collect.update("news", sweeps=[{"name": "名簿", "use_extract": True}])
        html = admin._sweep_table_body(collect.get("news"), "")
        roster = html.split("<summary>設定</summary>")[1].split("</details>")[0]

        assert '<select name="sweep_backend">' not in roster
        assert "AI を呼ばないので、相手は選べません" in roster
        # 足すための空枠は AI に頼む前提なので、そちらには出る
        assert html.count('<select name="sweep_backend">') == 1

    def test_a_mechanical_sweep_drops_the_backend_on_save(self, sample):
        """口を隠すだけでは足りない —— 効かない設定が控えに残ると、
        後から読む人には「この回は AI で走っている」と見える。
        """
        from app.views import admin

        class _Form:
            def getlist(self, key):
                return {
                    "sweep_name": ["名簿"],
                    "sweep_source": ["extract"],
                    "sweep_backend": ["claude"],
                    "sweep_enabled": ["1"],
                }.get(key, [])

        [sweep] = admin._parse_sweeps_form(_Form())

        assert sweep["use_extract"] is True
        assert "backend" not in sweep

    def test_it_does_not_claim_to_have_walked_the_partitions(self, sample, monkeypatch, tmp_path):
        """**機械で引く回は区画を見ない。** 指定を 1 本引いて全部を返すので、
        区画に印を付けると、見てもいない区画が「回り終えた」に混ざる(一周が嘘になる)。
        """
        import asyncio

        from app import extract, main

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        collect.update(
            "news",
            enabled=True,
            extract={"source": "jawiki", "tag": "画家"},
            partition={"by": "title", "target": 10},
            partitions=[{"key": partitioning.title_key("あ", "お"), "count": 10}],
            sweeps=[{"name": "名簿", "use_extract": True, "only_new": True}],
        )
        monkeypatch.setattr(
            extract, "run", lambda spec, sources: ([{"title": "草間彌生", "body": "本文"}], "")
        )
        async def no_feed(_item):
            return None

        monkeypatch.setattr(main, "_harvest", no_feed)
        monkeypatch.setattr(
            main.collect, "ndjson", lambda *a, **k: ("", {
                "added": 1, "updated": 0, "removed": 0, "skipped": 0, "total": 1,
                "kept": 0, "previous": 0, "collected": 1,
                "removed_titles": [], "added_titles": ["草間彌生"], "updated_titles": [],
            })
        )
        collect.update("news", sweeps=[{"name": "名簿", "use_extract": True, "only_new": True}])
        collect.mark_started("news", "名簿")
        asyncio.run(main._collect_material("news", {}))

        visited, total = partitioning.progress(collect.get("news").partitions, "名簿")
        assert (visited, total) == (0, 1)

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


class TestTheOutwardSweep:
    """外の道具で引く巡回 —— フィードの見出しをそのまま溜める回。

    **AI を呼ばずに外から入る唯一の経路**。取りこぼしも宣伝記事も引き受ける代わりに、
    枠を使わずに回せる —— 重要度を付ける・まとめる・漏れを探す、といった判断の要る
    仕事は別の巡回が AI に頼む(名簿と肉付けを分けるのと同じ形)。
    """

    def test_the_sweep_carries_the_mark(self, sample):
        collect.update("news", sweeps=[
            {"name": "取り込み", "interval_minutes": 60, "use_feed": True, "only_new": True},
            {"name": "整理", "interval_minutes": 360},
        ])
        outward, tidy = collect.sweeps_of(collect.get("news"))
        assert (outward.use_feed, outward.only_new) == (True, True)
        assert tidy.use_feed is False

    def test_it_bakes_what_the_feed_handed_over(self, sample, monkeypatch):
        """AI は呼ばない。**呼ぶと、機械で取れるものまで書き換わる**。"""
        import asyncio

        from app import main

        collect.update("news", sweeps=[{"name": "取り込み", "use_feed": True}])
        item = collect.get("news")
        harvest = {
            "items": [{
                "title": "見出し", "url": "https://example.com/1",
                "summary": "要約", "at": "2026-09-10T03:00:00+00:00", "from": "ためし新聞",
                "tags": ["ニュース"],
            }],
            "failed": 0, "tried": 1,
        }

        def no_ai(*args, **kwargs):
            raise AssertionError("外の道具で引く回は AI を呼ばない")

        monkeypatch.setattr(main, "_ask_for_collection", no_ai)
        items, cursor, _note = asyncio.run(
            main._collect_items(
                item, {}, {}, [], collect.sweep_named(item, "取り込み"), None, harvest
            )
        )

        assert [i["title"] for i in items] == ["見出し"]
        assert items[0]["tags"] == ["ニュース", "ためし新聞"]
        # **進み具合には触らない**。次にどこから読むかは道具の側が決める
        assert cursor is None

    def test_a_missing_tool_is_said_out_loud(self, sample):
        """道具を付け忘れた収集が、黙って 0 件で回り続けないように。"""
        import asyncio

        from app import main

        collect.update("news", sweeps=[{"name": "取り込み", "use_feed": True}])
        item = collect.get("news")
        items, _cursor, note = asyncio.run(
            main._collect_items(item, {}, {}, [], collect.sweep_named(item, "取り込み"))
        )

        assert items == []
        assert "外向きの道具" in note

    def test_the_published_date_is_kept_apart_from_the_day_it_arrived(self, sample):
        """集めた日だけだと、半年前の記事を今日拾ったのかが読めない。"""
        docs, _diff = collect.material(
            sample, {}, [{"title": "見出し", "body": "要約", "at": "2026-09-10T03:00:00+00:00"}]
        )

        assert docs[0]["extra"]["published_at"].startswith("2026-09-10")


class TestTellingTheAiTheTime:
    """`{now}` —— いまの日時(日本時間)。

    **AI はいまが何日の何時かを知らない**(学習した時点で止まっている)。聞けば
    それらしい日付を作ってしまうので、回ごとに違う見出しを付けさせたい場面で要る。
    """

    def test_it_is_replaced_with_japanese_time(self, sample):
        collect.update("news", prompt="いまは {now} です")
        content = collect.build_messages(collect.get("news"), {})[-1]["content"]

        assert "{now}" not in content
        assert "JST" in content

    def test_a_prompt_without_it_is_untouched(self, sample):
        collect.update("news", prompt="{cursor} 以降を集めて")
        content = collect.build_messages(collect.get("news"), {})[-1]["content"]

        assert "JST" not in content


class TestWhatCameInSinceLastTime:
    """`{recent}` —— 前回この巡回が走ってから後に入ったもの。

    溜まっていく一方の収集(ニュースのような)で、要約や重要度付けを頼む回に要る。
    全部を差し込むと入り切らないし、入ったとしても毎回同じものを読み直すことになる。
    """

    def test_it_only_shows_what_is_newer(self):
        previous = {
            "ふるい": {"title": "ふるい", "body": "", "updated_at": "2026-09-01T00:00:00+00:00"},
            "あたらしい": {"title": "あたらしい", "body": "", "updated_at": "2026-09-12T00:00:00+00:00"},
        }
        text = collect.render_recent(previous, "2026-09-10T00:00:00+00:00")

        assert "あたらしい" in text
        assert "ふるい" not in text

    def test_nothing_new_says_so(self):
        previous = {"ふるい": {"title": "ふるい", "body": "", "updated_at": "2026-09-01T00:00:00+00:00"}}

        assert "新しく入ったものはありません" in collect.render_recent(
            previous, "2026-09-10T00:00:00+00:00"
        )

    def test_the_first_run_of_a_sweep_sees_everything(self, sample):
        """**1 回目は区切らない。** 収集ぜんたいの前回へ倒していたせいで、その巡回の
        1 回目が必ず空になった —— 直前に別の巡回が走っていれば基準は数分前になる。

        本番では、見出しが 60 件足した 3 分後に初めての情報更新と要約が走り、
        そろって 0 件で終わった。
        """
        import dataclasses

        collect.update("news", prompt="{recent} をまとめて", sweeps=[{"name": "要約"}])
        # **収集ぜんたいの前回**は、直前に別の巡回が走っていれば数分前になる
        item = dataclasses.replace(collect.get("news"), last_run_at="2026-09-12T00:00:00+00:00")
        previous = {
            "さっき入った": {
                "title": "さっき入った", "body": "",
                "updated_at": "2026-09-11T00:00:00+00:00",
            }
        }
        messages = collect.build_messages(
            item, previous, None, {}, collect.sweep_named(item, "要約")
        )

        assert "さっき入った" in messages[-1]["content"]

    def test_the_clock_is_the_sweeps_own(self, sample):
        """収集の前回を基準にすると、集めたばかりのぶんしか入らない(要約が空になる)。"""
        collect.update(
            "news",
            prompt="{recent} をまとめて",
            last_run_at="2026-09-12T00:00:00+00:00",
            sweeps=[{"name": "要約", "last_run_at": "2026-09-01T00:00:00+00:00"}],
        )
        item = collect.get("news")
        previous = {
            "きのう": {"title": "きのう", "body": "", "updated_at": "2026-09-11T00:00:00+00:00"}
        }
        messages = collect.build_messages(
            item, previous, None, {}, collect.sweep_named(item, "要約")
        )

        assert "きのう" in messages[-1]["content"]


def edited(item, previous, collected):
    """**直す回として**素材を組む。

    直す回かどうかは、その回の依頼文が語る(`edits_what_is_there`)。
    ここを通るテストは「今あるものを読ませている回」を見ているので、印を立てて呼ぶ。
    """
    return collect.material(item, previous, collected, False, True)


def _iso_now():
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


class TestTheKindOfCollection:
    """収集の**種類** —— 流れを追うのか、括りの全部を精査するのか。

    `mode` とは別の軸。あちらは「返ってきた 1 件で何ができるか」で、こちらは
    「その収集が何を集めているのか」。**流れ**は時とともに増えるものを追っていて、
    古いものは順に要らなくなる(ニュース)。**網羅**はある括りの全部が対象で、
    増減はしても古いものが要らなくなることはない(画家の名簿、全国の食事処)。
    """

    def test_the_default_never_deletes_by_age(self, sample):
        """**既定は網羅。** 期限で落とすのは流れだけなので、知らないうちに消える
        ほうへは倒さない。"""
        assert collect.get("news").kind == collect.KIND_STOCK
        assert collect.get("news").keep_days == 0

    def test_a_flow_keeps_thirty_days_by_default(self, sample):
        collect.update("news", kind="flow")
        item = collect.get("news")

        assert (item.kind, item.keep_days) == ("flow", collect.DEFAULT_KEEP_DAYS)

    def test_moving_back_to_stock_clears_the_days(self, sample):
        """網羅へ移したのに日数が残ると、次に流れへ戻したときに古い設定で消え始める。"""
        collect.update("news", kind="flow", keep_days=7)
        collect.update("news", kind="stock")

        assert collect.get("news").keep_days == 0

    def test_old_documents_fall_out_of_a_flow(self, sample):
        collect.update("news", kind="flow", keep_days=30)
        old = "2026-01-01T00:00:00+00:00"
        docs = [
            {"title": "きのう", "extra": {"published_at": _iso_now()}},
            {"title": "むかし", "extra": {"published_at": old}},
        ]

        kept, dropped = collect.expired_docs(collect.get("news"), docs)

        assert [d["title"] for d in kept] == ["きのう"]
        assert dropped == 1

    def test_the_article_date_wins_over_the_day_it_arrived(self, sample):
        """集めた日だけで数えると、半年前の記事を今日拾ったものが 30 日生き残る。"""
        collect.update("news", kind="flow", keep_days=30)
        docs = [
            {"title": "古い記事", "extra": {
                "published_at": "2026-01-01T00:00:00+00:00", "collected_at": _iso_now(),
            }}
        ]

        _kept, dropped = collect.expired_docs(collect.get("news"), docs)

        assert dropped == 1

    def test_a_stock_never_drops_by_age(self, sample):
        """古いものが要らなくなることがないので、期限で消すと穴が開く。"""
        collect.update("news", kind="stock")
        docs = [{"title": "むかし", "extra": {"published_at": "2020-01-01T00:00:00+00:00"}}]

        kept, dropped = collect.expired_docs(collect.get("news"), docs)

        assert len(kept) == 1
        assert dropped == 0

    def test_a_document_without_a_date_stays(self, sample):
        """読めなければ落とさない側へ倒す。"""
        collect.update("news", kind="flow", keep_days=1)

        kept, dropped = collect.expired_docs(collect.get("news"), [{"title": "日付なし"}])

        assert len(kept) == 1
        assert dropped == 0


class TestOrderingTheSweeps:
    """巡回の順番。**区画の材料が揃うまで、区画ごとの仕事は成り立たない**。

    名簿を作った直後の漏れ探しは、区画の外に居るだけの画家を 20 人挙げ、すべて既存
    だった —— 年代の分からない人が 3 割いたので、その人たちは別の区画にいた。
    """

    @pytest.fixture
    def staged(self, enabled):
        item = collect.create("news", prompt="{cursor} と {current}", interval_minutes=60)
        collect.update(
            "news",
            enabled=True,
            partition={"by": "title", "target": 10},
            partitions=[{"key": partitioning.title_key("あ", "お"), "count": 10},
                        {"key": partitioning.title_key("か", "こ"), "count": 10}],
            sweeps=[{"name": "ざっと", "one_lap": True},
                    {"name": "調査", "after": "ざっと"}],
        )
        return item

    def _sweep(self, name):
        item = collect.get("news")
        return item, collect.sweep_named(item, name)

    def test_the_later_sweep_waits_for_a_full_lap(self, staged):
        # 走り出しは、先の回だけ
        assert [s.name for _c, s in collect.due_sweeps()] == ["ざっと"]
        assert collect.waited_for(*self._sweep("調査")) is False

        # 半分だけ見てもまだ待つ
        collect.record_result("news", status="ok", sweep="ざっと", visited=["あ〜お"])
        assert collect.waited_for(*self._sweep("調査")) is False

        collect.record_result("news", status="ok", sweep="ざっと", visited=["か〜こ"])
        assert collect.waited_for(*self._sweep("調査")) is True

    def test_the_first_sweep_stops_after_its_lap(self, staged):
        """一周したら止まる。**止めないと 2 周目 3 周目が回り続け、同じことを
        何度も聞くために枠を使う**。"""
        assert collect.lapped(*self._sweep("ざっと")) is False

        for key in ("あ〜お", "か〜こ"):
            collect.record_result("news", status="ok", sweep="ざっと", visited=[key])

        assert collect.lapped(*self._sweep("ざっと")) is True
        assert "ざっと" not in [s.name for _c, s in collect.due_sweeps()]

    def test_waiting_on_a_mechanical_sweep_needs_only_one_run(self, enabled):
        """**機械で引く回は区画に印を付けない**(指定を 1 本引いて全部を返すため)。

        一周を区画の印で数えると 0 / 全区画 のまま動かず、その回を待つ巡回は
        二度と走らない —— 本番で、名簿を待つ「ざっと」が 1 回目(まだ区画が無く、
        件数の枝に落ちた)を最後に 6 時間止まっていた。
        """
        collect.create("news", prompt="{cursor} と {current}", interval_minutes=60)
        collect.update(
            "news",
            enabled=True,
            extract={"source": "jawiki", "tag": "画家"},
            partition={"by": "title", "target": 10},
            sweeps=[{"name": "名簿", "use_extract": True, "once": True},
                    {"name": "ざっと", "after": "名簿"}],
        )
        # 名簿がまだ走っていないうちは待つ
        assert collect.waited_for(*self._sweep("ざっと")) is False

        collect.record_result("news", status="ok", sweep="名簿")
        # その回が区画を作った(印は付かない —— 機械で引く回は区画を歩かない)
        collect.update("news", partitions=[
            {"key": partitioning.title_key("あ", "お"), "count": 10},
            {"key": partitioning.title_key("か", "こ"), "count": 10},
        ])

        assert collect.waited_for(*self._sweep("ざっと")) is True
        assert "ざっと" in [s.name for _c, s in collect.due_sweeps()]

    def test_a_mechanical_lap_ends_after_one_run(self, enabled):
        """一周したら止まる回が機械で引くなら、1 回で一周(印では数え終わらない)。"""
        collect.create("news", prompt="{cursor}", interval_minutes=60)
        collect.update(
            "news",
            enabled=True,
            extract={"source": "jawiki", "tag": "画家"},
            partition={"by": "title", "target": 10},
            partitions=[{"key": partitioning.title_key("あ", "お"), "count": 10}],
            sweeps=[{"name": "名簿", "use_extract": True, "one_lap": True}],
        )
        assert collect.lapped(*self._sweep("名簿")) is False

        collect.record_result("news", status="ok", sweep="名簿")

        assert collect.lapped(*self._sweep("名簿")) is True

    def test_it_says_why_it_will_not_run(self, staged):
        """**予定を持っていないことと、走らないことは違う。**

        どちらも予定が空なので、区別せずに出すと「いますぐ」と書いてある回を
        3 日待ち続けることになる(実際にそう見えていた)。
        """
        assert collect.blocked_reason(*self._sweep("調査")) == "「ざっと」の一周待ち"
        # 先の回は走る予定なので、理由は無い
        assert collect.blocked_reason(*self._sweep("ざっと")) == ""

        for key in ("あ〜お", "か〜こ"):
            collect.record_result("news", status="ok", sweep="ざっと", visited=[key])

        assert collect.blocked_reason(*self._sweep("調査")) == ""
        assert collect.blocked_reason(*self._sweep("ざっと")) == "一周して止まった"

    def test_the_reason_rides_along_to_the_readers(self, staged):
        """読む側が同じ場合分けを書き写さずに済むよう、理由はこちらが言う。"""
        [_rough, survey] = collect.to_public(collect.get("news"))["sweeps"]

        assert survey["name"] == "調査"
        assert survey["blocked"] == "「ざっと」の一周待ち"

    def test_a_sweep_that_will_run_has_no_reason(self, sample):
        collect.update("news", enabled=True, sweeps=[{"name": "ざっと"}])

        assert collect.blocked_reason(*self._sweep("ざっと")) == ""

    def test_a_stopped_sweep_says_so(self, staged):
        collect.update("news", sweeps=[{"name": "ざっと", "enabled": False}])

        assert collect.blocked_reason(*self._sweep("ざっと")) == "止めている"

    def test_a_once_sweep_says_it_is_done(self, sample):
        collect.update("news", enabled=True, sweeps=[{"name": "名簿", "once": True}])
        collect.record_result("news", status="ok", sweep="名簿")

        assert collect.blocked_reason(*self._sweep("名簿")) == "一度きり(済み)"

    def test_an_unknown_name_does_not_block(self, staged):
        """待つ相手が居ないのに永久に止まる方が悪い。"""
        collect.update("news", sweeps=[{"name": "調査", "after": "居ない巡回"}])

        assert collect.waited_for(*self._sweep("調査")) is True

    def test_a_once_sweep_runs_only_once(self, sample):
        """元のデータが変わらない限り何度やっても同じ回。押せばまた走る。"""
        collect.update("news", enabled=True, sweeps=[{"name": "名簿", "once": True}])
        assert [s.name for _c, s in collect.due_sweeps()] == ["名簿"]

        collect.record_result("news", status="ok", sweep="名簿")

        assert collect.due_sweeps() == []
        # 口のほうでは断らない(押せば走る)
        assert collect.require_runnable(collect.get("news"), "名簿").name == "名簿"


class TestRedoingTheLastRun:
    """最後の 1 回をやり直す。

    設定を直してからやり直したい、が普通に起きる(依頼文を直した・相手を替えた)。
    そのまま「今すぐ実行」を押すと、進み具合が先へ進んでいるので**次のぶんを
    集めてしまう** —— 直したかった回は二度と来ない。

    **戻せるのは定義の側だけ。** 焼いた世代は 1 つ前までしか残らないので、対象の
    巡回が最後でなければ中身は戻せない。
    """

    def test_the_cursor_goes_back(self, sample):
        collect.update("news", cursor="2026-09-01", sweeps=[{"name": "ざっと"}])
        collect.record_result("news", status="ok", sweep="ざっと", next_cursor="2026-09-08")
        assert collect.get("news").cursor == "2026-09-08"

        collect.rewind("news")

        assert collect.get("news").cursor == "2026-09-01"

    def test_the_partition_marks_go_back(self, sample):
        """印が残ったままだと、やり直した回が次の区画へ進んでしまう。"""
        collect.update(
            "news",
            sweeps=[{"name": "ざっと"}],
            partitions=[{"key": "あ", "count": 1}, {"key": "い", "count": 1}],
        )
        collect.record_result("news", status="ok", sweep="ざっと", visited=["あ"])
        assert partitioning.progress(collect.get("news").partitions, "ざっと") == (1, 2)

        collect.rewind("news")

        assert partitioning.progress(collect.get("news").partitions, "ざっと") == (0, 2)

    def test_what_was_removed_stays_removed(self, sample):
        """消したのは意図してのこと。やり直しで連れ戻さない。

        **消えた印は文書側にある**ので、やり直しが戻すのは進み具合だけ ——
        定義に控えを持っていた頃と違い、ここで気にすることが無くなった。
        """
        collect.update("news", sweeps=[{"name": "ざっと"}])
        collect.record_result("news", status="ok", sweep="ざっと", next_cursor="b")

        collect.rewind("news")

        assert collect.get("news").cursor == ""

    def test_it_can_only_be_used_once(self, sample):
        """同じ回を二度は戻せない(1 回ぶんしか控えていない)。"""
        collect.update("news", cursor="a", sweeps=[{"name": "ざっと"}])
        collect.record_result("news", status="ok", sweep="ざっと", next_cursor="b")
        collect.rewind("news")

        with pytest.raises(HTTPException):
            collect.rewind("news")

    def test_nothing_to_redo_is_refused(self, sample):
        """1 回走ってからでないと、戻す先がない。"""
        with pytest.raises(HTTPException):
            collect.rewind("news")

    def test_an_interrupt_leaves_no_trace(self, sample):
        """割り込みは進み具合にも区画にも触らないので、戻すものが無い。"""
        collect.update("news", cursor="a", sweeps=[{"name": "ざっと"}])
        collect.record_result("news", status="ok", sweep="ざっと", next_cursor="b")
        collect.record_result("news", status="ok", sweep="ざっと", focus=True, next_cursor="c")

        # 割り込みの前の回が、そのまま戻し先として残っている
        assert collect.get("news").last_undo["cursor"] == "a"

    def test_the_screen_offers_it_only_when_there_is_something_to_redo(self, sample):
        from app.views import admin

        assert "最後の 1 回をやり直す" not in admin._collect_detail_html(collect.get("news"), "")

        collect.update("news", sweeps=[{"name": "ざっと"}])
        collect.record_result("news", status="ok", sweep="ざっと", next_cursor="b")
        html = admin._collect_detail_html(collect.get("news"), "")

        assert "最後の 1 回をやり直す" in html
        # 中身は戻らないことを、押す前に書く
        assert "集めた中身は戻りません" in html


class TestVerifyingTags:
    """タグの値が実在するかを、**焼く前に**確かめる。

    AI は「その画家の代表作」を挙げられても、**それが記事として存在するかは知らない**。
    読む側は「タグがある = 押せば何か出る」と受け取るので、実在しない見出しが混ざると、
    押しても何も出ないものが並ぶ。**確かめられるのはこちら**(長期記憶を持っている層)。
    """

    def test_it_runs_over_everything_that_is_baked(self, sample, baked):
        """**既に入っているものにも掛ける。** 掛けないと、前に入った間違いが残り続ける。"""
        sources = baked([("印象・日の出", "モネの絵")], name="jawiki")
        collect.update("news", verify_tags=[{"prefix": "代表作", "source": "jawiki"}])
        previous = {
            "ルノワール": {
                "doc_id": 1, "title": "ルノワール", "opening": "", "body": "説明",
                "tags": ["画家", "代表作:実在しない絵"], "updated_at": "2026-01-01T00:00:00+00:00",
                "extra": None,
            }
        }

        docs, diff = collect.verified_docs(
            collect.get("news"),
            [
                {"title": "モネ", "tags": ["画家", "代表作:印象・日の出", "代表作:無い絵"]},
                {"title": "ルノワール", "tags": previous["ルノワール"]["tags"]},
            ],
            sources,
        )

        assert [d["tags"] for d in docs] == [["画家", "代表作:印象・日の出"], ["画家"]]
        assert diff == 2

    def test_three_part_tags_are_judged_by_the_first_piece(self, sample, baked):
        """`影響元:名前:理由` のような形があるので、見出しは 1 つ目だけ。"""
        sources = baked([("クロード・モネ", "画家")], name="jawiki")
        collect.update("news", verify_tags=[{"prefix": "影響元", "source": "jawiki"}])

        docs, dropped = collect.verified_docs(
            collect.get("news"),
            [{"title": "誰か", "tags": ["影響元:クロード・モネ:光の扱い", "影響元:居ない人:何か"]}],
            sources,
        )

        assert docs[0]["tags"] == ["影響元:クロード・モネ:光の扱い"]
        assert dropped == 1

    def test_an_unbaked_source_changes_nothing(self, sample):
        """知らないものを「無い」と読むと、正しいタグまで落ちる。"""
        collect.update("news", verify_tags=[{"prefix": "代表作", "source": "jawiki"}])

        docs, dropped = collect.verified_docs(
            collect.get("news"), [{"title": "モネ", "tags": ["代表作:印象・日の出"]}], {}
        )

        assert docs[0]["tags"] == ["代表作:印象・日の出"]
        assert dropped == 0

    def test_without_the_spec_nothing_is_touched(self, sample, baked):
        sources = baked([("印象・日の出", "モネの絵")], name="jawiki")

        docs, dropped = collect.verified_docs(
            collect.get("news"), [{"title": "モネ", "tags": ["代表作:何でも"]}], sources
        )

        assert docs[0]["tags"] == ["代表作:何でも"]
        assert dropped == 0

    def test_the_spec_survives_a_round_trip(self, sample):
        collect.update("news", verify_tags=[{"prefix": "代表作", "source": "jawiki"}])

        assert collect.get("news").verify_tags == [{"prefix": "代表作", "source": "jawiki"}]

        # 空の配列で外せる(消す手段がここしかない)
        collect.update("news", verify_tags=[])
        assert collect.get("news").verify_tags == []

    def test_a_spec_without_a_source_is_refused(self, sample):
        """黙って無視すると、確かめているつもりの収集が確かめずに回り続ける。"""
        with pytest.raises(HTTPException):
            collect.update("news", verify_tags=[{"prefix": "代表作"}])


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

    def test_a_sweep_without_the_material_just_adds(self, sample):
        """**断らない。** 今あるものを差し込んでいない回は、足すだけの回になる ——
        AI は今あるものを知らないので、消す力を持たせられないだけ。

        収集ぜんたいの設定だった頃は、巡回ごとに決められないので作る時点で弾くしか
        なかった。いまは回ごとに決まるので、弾く理由が無い。
        """
        collect.update("news", prompt="いまの内容:\n{current}\n直して")
        collect.update("news", sweeps=[{"name": "漏れ探し", "prompt": "漏れを足して"}])

        [sweep] = collect.sweeps_of(collect.get("news"))
        assert collect.edits_what_is_there(sweep.prompt, sweep.only_new) is False


class TestTheLedgerCountFollowsTheContents:
    """割り直さない回でも件数は取り直す。

    台帳の数は割ったときの写しで、中身が別の区画へ移っても古い数が出続けていた ——
    本番では、一周目に配った 6 区画の全員が本来の帯へ移って空になったのに、
    画面には割ったときの 25〜27 が出たままだった。
    """

    def test_the_count_is_taken_again_without_resplitting(self, sample):
        collect.update(
            "news",
            partition={"by": "title", "target": 10},
            partitions=[
                {"key": partitioning.title_key("あ", "い"), "count": 9},
                {"key": partitioning.title_key("う", "え"), "count": 9},
            ],
        )
        # **まとめる大きさには落とさない**(`partitioning.merged`)。ここで見たいのは
        # 「割り直さずに数だけ取り直す」ことなので、隣とまとまると確かめられない
        previous = {
            t: {"doc_id": i, "title": t, "tags": []}
            for i, t in enumerate(["あ", "ああ", "あい", "い", "う", "うう", "え", "ええ", "お"])
        }
        item = collect.get("news")

        ledger = collect.plan_partitions(item, {}, previous)

        # 割り直してはいない(鍵はそのまま)
        assert [p["key"] for p in ledger] == [p["key"] for p in item.partitions]
        assert [p["count"] for p in ledger] == [4, 5]


    def test_the_population_leaves_out_what_was_removed(self, sample):
        """**消したものは区画の母集団に入れない。**

        消したものは印を付けて残り続けるので、混ぜると精査を頼むほど区画が太る
        —— 見るものが無い区画にも巡回の 1 回が回ってくることになる。
        """
        collect.update(
            "news",
            partition={"by": "title", "target": 10},
            partitions=[
                {"key": partitioning.title_key("あ", "い"), "count": 9},
                {"key": partitioning.title_key("う", "え"), "count": 9},
            ],
        )
        previous = {
            t: {"doc_id": i, "title": t, "tags": ([notes.REMOVED_TAG] if gone else [])}
            for i, (t, gone) in enumerate([
                ("あ", False), ("ああ", True), ("あい", True), ("い", False),
                ("う", False), ("うう", True), ("え", False), ("ええ", False), ("お", False),
            ])
        }

        ledger = collect.plan_partitions(collect.get("news"), {}, previous)

        # 9 件のうち 3 件は墓標。数えるのは残る 6 件だけ
        assert sum(p["count"] for p in ledger) == 6

    def test_a_partition_with_only_graves_counts_as_empty(self, sample):
        """墓標だけの区画は「空」。空の区画は割り直しで消える(枠を捨てないため)。"""
        item = collect.update(
            "news",
            partition={"by": "title", "target": 10},
            partitions=[
                {"key": partitioning.title_key("あ", "い"), "count": 2},
                {"key": partitioning.title_key("う", "え"), "count": 2},
            ],
        )
        previous = {
            "あ": {"doc_id": 1, "title": "あ", "tags": [notes.REMOVED_TAG]},
            "い": {"doc_id": 2, "title": "い", "tags": [notes.REMOVED_TAG]},
            "う": {"doc_id": 3, "title": "う", "tags": []},
            "え": {"doc_id": 4, "title": "え", "tags": []},
        }
        spec = partitioning.normalize(item.partition)

        counts = partitioning.counts_of(spec, item.partitions, collect.living(previous))

        assert list(counts.values()) == [0, 2]
        # 空の区画があれば割り直す(その区画は組み立てられないので消える)
        assert partitioning.outgrown(spec, counts)


    def test_a_range_left_with_only_graves_stays_on_the_round(self, sample):
        """**行き場の無くなった区画は落とさない。**

        区画は生きているものだけで割るので、中身が消えたものだけになった帯は
        組み立てられない —— 落とすと、そこは以後どの回にも回ってこない。
        「この範囲に足すべきものが無いか」を問う回はそこにしか無いので、
        漏れを探す仕事ごと消えることになる。
        """
        spec = {"by": "band", "prefix": "地域", "value": "年代", "target": 10}
        gone = partitioning.band_key("日本", 1800, 1850)
        collect.update(
            "news", partition=spec,
            partitions=[
                {"key": gone, "count": 10, "visits": {"ざっと": "1"}},
                {"key": partitioning.band_key("日本", 1900, 1950), "count": 10},
            ],
        )
        previous = {
            f"むかしの人{i}": {"doc_id": i, "title": f"むかしの人{i}",
                          "tags": ["地域:日本", "年代:1800-1850", notes.REMOVED_TAG]}
            for i in range(1, 11)
        } | {
            f"いまの人{i}": {"doc_id": 10 + i, "title": f"いまの人{i}",
                         "tags": ["地域:日本", f"年代:19{i:02d}"]}
            for i in range(1, 51)
        }

        ledger = collect.plan_partitions(collect.get("news"), {}, previous)

        kept = [p for p in ledger if p["key"] == gone]
        assert kept, "中身が墓標だけになった帯も、回る先としては残る"
        # 巡回の記録は持ったまま(残したぶんだけ一周が巻き戻らない)
        assert kept[0]["visits"] == {"ざっと": "1"}
        assert kept[0]["count"] == 0

    def test_merging_never_makes_a_range_that_holds_nothing(self, sample):
        """**順が逆のまま帯を組むと、その範囲の文書がどこにも入らなくなる。**

        覆われていない区画は台帳の後ろへ足すので、並び順が範囲の順とは限らない。
        `1900-1850` のような鍵は 1 件も拾えず、静かな穴になる。
        """
        spec = partitioning.normalize({"by": "band", "prefix": "地域", "value": "年代",
                                       "target": 10})
        ledger = partitioning.merged(spec, [
            {"key": partitioning.band_key("日本", 1900, 1950), "count": 1, "visits": {}},
            {"key": partitioning.band_key("日本", 1800, 1850), "count": 1, "visits": {}},
        ])

        assert [p["key"] for p in ledger] == [
            partitioning.band_key("日本", 1900, 1950),
            partitioning.band_key("日本", 1800, 1850),
        ], "並んでいないものはまとめない"

    def test_a_split_parent_is_not_kept_twice(self, sample):
        """割られた区画の親は残さない(子が引き受けているので、残すと二重になる)。"""
        spec = {"by": "title", "target": 10}
        parent = partitioning.title_key("あ", "ん")
        collect.update("news", partition=spec, partitions=[{"key": parent, "count": 1}])
        previous = {
            f"ひと{i:03d}": {"doc_id": i, "title": f"ひと{i:03d}", "tags": []}
            for i in range(1, 61)
        }

        ledger = collect.plan_partitions(collect.get("news"), {}, previous)

        assert len(ledger) > 1, "この中身なら割り直されるはず"
        assert parent not in [p["key"] for p in ledger]


class TestCarryingFactsIntoTheDoc:
    """集める側が運んできた事実を、焼く 1 件の脇に載せる。

    **読む側が 1 件ずつ引き直さなくて済むように。** 知名度は元の長期記憶に載って
    いるので、図を開くたびに画家の数だけ往復するのは筋が悪い。
    """

    def test_it_lands_in_the_extra(self, sample):
        docs, _diff = collect.material(
            collect.get("news"), {},
            [{"title": "モネ", "body": "画家です", "extra": {"pageviews_month": 8869}}],
        )

        assert docs[0]["extra"]["pageviews_month"] == 8869
        # 元から載せているものは消えない
        assert docs[0]["extra"]["collected_at"]

    def test_nested_values_do_not_ride_along(self, sample):
        """1 件の脇に添える札であって、記事を丸ごと写す場所ではない。"""
        docs, _diff = collect.material(
            collect.get("news"), {},
            [{"title": "モネ", "body": "画家です", "extra": {
                "pageviews_month": 8869, "本文": {"節": "長い入れ子"}, "並び": [1, 2, 3],
            }}],
        )

        assert docs[0]["extra"]["pageviews_month"] == 8869
        assert "本文" not in docs[0]["extra"]
        assert "並び" not in docs[0]["extra"]

    def test_an_edit_does_not_drop_the_facts(self, sample):
        """**AI が手を入れる回には、運んだ事実は返ってこない。**

        置き換えてしまうと最初の手入れで静かに消える。1 件ずつ減るので
        消えすぎの歯止めもすり抜ける。
        """
        collect.update("news", prompt="いまの内容:\n{current}\n直して")
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "古い",
            "extra": {"pageviews_month": 8869, "collected_at": "むかし"},
        }}

        docs, _diff = collect.material(
            collect.get("news"), previous,
            [{"title": "モネ", "body": "新しい"}], edits=True,
        )

        assert docs[0]["extra"]["pageviews_month"] == 8869
        # 今回の回の印は新しいほうで上書きする
        assert docs[0]["extra"]["collected_at"] != "むかし"

    def test_an_add_only_run_fills_in_a_missing_fact(self, sample):
        """**指定に鍵を足しても、既にいるものには届かないままだった。**

        名簿は足すだけの回なので、焼き直しても全員が飛ばされる —— 後から
        知名度を運ぶようにしても、既にいる 9,000 人には永遠に入らない。
        """
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "育てた本文",
            "tags": ["画家", "様式:印象派"], "extra": {"collected_at": "むかし"},
        }}

        docs, _diff = collect.material(
            collect.get("news"), previous,
            [{"title": "モネ", "body": "機械の要約", "tags": ["画家"],
              "extra": {"pageviews_month": 8869}}],
            only_new=True,
        )

        assert docs[0]["extra"]["pageviews_month"] == 8869
        # **中身は動かさない**(足すだけの回の約束)
        assert docs[0]["body"] == "育てた本文"
        assert docs[0]["tags"] == ["画家", "様式:印象派"]
        assert docs[0]["extra"]["collected_at"] == "むかし"

    def test_an_add_only_run_does_not_overwrite_a_fact(self, sample):
        """先に入っている値は動かさない。"""
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "本文", "tags": [],
            "extra": {"pageviews_month": 100},
        }}

        docs, _diff = collect.material(
            collect.get("news"), previous,
            [{"title": "モネ", "body": "本文", "extra": {"pageviews_month": 8869}}],
            only_new=True,
        )

        assert docs[0]["extra"]["pageviews_month"] == 100

    def test_a_null_removes_the_key(self, sample):
        """**書かなければ残る作りなので、落とす口がどこかに要る。**

        間違って入った値（ページビューが不当に高い、など）を直せないと、
        重ねる作りは「一度入ったら消せない」になる。
        """
        collect.update("news", prompt="いまの内容:\n{current}\n直して")
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "古い",
            "extra": {"pageviews_month": 8869, "うその値": 1},
        }}

        docs, _diff = collect.material(
            collect.get("news"), previous,
            [{"title": "モネ", "body": "新しい", "extra": {"うその値": None}}],
            edits=True,
        )

        assert "うその値" not in docs[0]["extra"]
        # 言われていないものは残る
        assert docs[0]["extra"]["pageviews_month"] == 8869

    def test_an_add_only_run_never_removes(self, sample):
        """足すだけの回は、印が来ても何も落とさない。"""
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "本文", "tags": [],
            "extra": {"pageviews_month": 8869},
        }}

        docs, _diff = collect.material(
            collect.get("news"), previous,
            [{"title": "モネ", "body": "本文", "extra": {"pageviews_month": None}}],
            only_new=True,
        )

        assert docs[0]["extra"]["pageviews_month"] == 8869

    def test_the_number_of_facts_has_a_ceiling(self, sample):
        """消さない作りなので、天井を置かないと回を重ねるだけ増える。"""
        collect.update("news", prompt="いまの内容:\n{current}\n直して")
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "古い",
            "extra": {f"k{i}": i for i in range(collect.MAX_EXTRA_KEYS + 5)},
        }}

        docs, _diff = collect.material(
            collect.get("news"), previous, [{"title": "モネ", "body": "新しい"}], edits=True,
        )

        assert len(docs[0]["extra"]) <= collect.MAX_EXTRA_KEYS

    def test_the_collected_at_is_not_overwritten(self, sample):
        """運ばれてきた値で、こちらが付ける印を上書きさせない。"""
        docs, _diff = collect.material(
            collect.get("news"), {},
            [{"title": "モネ", "body": "画家です", "extra": {"collected_at": "うそ"}}],
        )

        assert docs[0]["extra"]["collected_at"] != "うそ"


class TestTheCountAfterTheRun:
    """見終わった区画の人数は、**その回で動いたあとの数**で出す。

    回の頭で数えた値のままだと必ず 1 回ぶん古い —— 本番では、ざっとが見終わった
    6 区画が全員よそへ移って空になったのに、台帳には見る前の 26〜33 が出ていた。
    """

    def test_it_counts_the_generation_it_is_about_to_bake(self, sample):
        collect.update(
            "news",
            partition={"by": "band", "prefix": "地域", "value": "年代", "target": 10},
            partitions=[
                {"key": partitioning.band_key("その他", 1850, 1860), "count": 2},
                {"key": partitioning.band_key("日本", 1850, 1860), "count": 0},
            ],
        )
        item = collect.get("news")
        # 地域が入って、2 人とも「その他」から「日本」へ移った世代
        docs = [
            {"title": "あ", "tags": ["年代:1855-1900", "地域:日本"]},
            {"title": "い", "tags": ["年代:1858-1910", "地域:日本"]},
        ]

        counts = collect.partition_counts(item, docs)

        assert counts[partitioning.band_key("その他", 1850, 1860)] == 0
        assert counts[partitioning.band_key("日本", 1850, 1860)] == 2

    def test_a_collection_without_partitions_counts_nothing(self, sample):
        item = collect.get("news")

        assert collect.partition_counts(item, [{"title": "あ", "tags": []}]) == {}

    def test_the_run_writes_the_new_count_into_the_ledger(self, sample, monkeypatch, tmp_path):
        """1 回まわしたあと、台帳に載るのは**焼いたあとの人数**。"""
        import asyncio

        from app import main

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        moved = partitioning.band_key("その他", 1850, 1860)
        landed = partitioning.band_key("日本", 1850, 1860)
        collect.update(
            "news",
            enabled=True,
            partition={"by": "band", "prefix": "地域", "value": "年代", "target": 10},
            # **まとめる大きさには落とさない**(`partitioning.merged`)—— 隣と 1 つに
            # なると、どちらの区画の数を見ているのか確かめられない
            partitions=[{"key": moved, "count": 20}, {"key": landed, "count": 0}],
            sweeps=[{"name": "ざっと"}],
        )

        async def no_feed(_item):
            return None

        monkeypatch.setattr(main, "_harvest", no_feed)

        async def collected(*_args, **_kwargs):
            return [{
                "title": "あ", "body": "本文", "tags": ["年代:1855-1900", "地域:日本"],
            }], None, ""

        monkeypatch.setattr(main, "_collect_items", collected)
        collect.mark_started("news", "ざっと")
        asyncio.run(main._collect_material("news", {}))

        after = {p["key"]: p["count"] for p in collect.get("news").partitions}
        assert after[moved] == 0, "見終わった区画は、移ったあとの人数で出る"
        assert after[landed] == 1


class TestReplanningInTheSameRun:
    """区画を割り直した回でも、素材は**新しい台帳**で組む。

    定義に入っているのは走る前の台帳なので、この回で割り直すと食い違う ——
    区画を選ぶのは新しい台帳から、どの文書がその区画かを判ずるのは古い台帳から、
    になる。**差し込みが丸ごと空になり**、AI は「誰も居ない」と読んで何も返さない。

    名簿を作り直した直後の回がまさにそれだった(本番で、9,439 人を入れ直した次の
    回が `{"items":[]}` で終わっていた)。
    """

    def test_the_material_uses_the_new_ledger(self, sample):
        spec = {"by": "title", "target": 10}
        # 走る前の台帳は 1 区画。いまの中身では割り直される
        collect.update(
            "news",
            prompt="いま入っているもの:\n{current}\n{partition} を直して",
            partition=spec,
            partitions=[{"key": partitioning.title_key("あ", "ん"), "count": 1}],
        )
        previous = {
            f"ひと{i:03d}": {"doc_id": i, "title": f"ひと{i:03d}", "body": "本文", "tags": []}
            for i in range(1, 61)
        }
        item = collect.get("news")
        ledger = collect.plan_partitions(item, {}, previous)
        assert ledger != item.partitions, "この回で割り直されるはず"
        keys = partitioning.pick(ledger, "既定", 1)

        # 古い台帳のまま組むと、どの文書もその区画に入らない
        stale = collect.build_messages(item, previous, keys[0], {})[-1]["content"]
        assert "まだ何も入っていません" in stale

        # 新しい台帳で組めば、その区画の文書が入る
        import dataclasses

        fresh = collect.build_messages(
            dataclasses.replace(item, partitions=ledger), previous, keys[0], {}
        )[-1]["content"]
        assert "まだ何も入っていません" not in fresh


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


    def test_bands_leave_no_gap_between_neighbours(self):
        """**帯にも隙間を作らない。**

        帯は値の詰まっているところで切るので、そのまま書くと
        `1600-1641` と `1700-1741` のあいだの年がどこにも属さない ——
        そこに入るものはどの区画にも出てこないし、**「この範囲に足すべきものが
        無いか」を問う回にも入らない**(誰もその年を探しに行かない)。
        """
        spec = partitioning.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 10}
        )
        own = {}
        for i, year in enumerate(
            [y for y in range(1600, 1612) for _ in range(3)]
            + [y for y in range(1700, 1712) for _ in range(3)]
        ):
            own[f"人{i}"] = {"title": f"人{i}", "tags": ["地域:日本", f"年代:{year}"]}

        built = partitioning.build(spec, {}, own)

        spans = [partitioning.parse_band_key(p["key"]) for p in built]
        for left, right in itertools.pairwise(spans):
            assert left[2] + 1 == right[1], "帯の終わりは、次の帯の始まりの手前まで"
        # 前は誰のものでもなかった年も、いまはどこかに入る
        assert partitioning.partition_of(
            spec, built, {"title": "間の人", "tags": ["地域:日本", "年代:1650"]}
        ) is not None

    def test_a_value_outside_every_band_has_a_home(self):
        """割ったときの値の外(もっと古い・もっと新しい)も、端の帯が引き受ける。"""
        spec = partitioning.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 10}
        )
        ledger = [
            {"key": partitioning.band_key("日本", 1600, 1699), "count": 10},
            {"key": partitioning.band_key("日本", 1700, 1799), "count": 10},
        ]

        older = partitioning.partition_of(
            spec, ledger, {"title": "古い人", "tags": ["地域:日本", "年代:1500"]}
        )
        newer = partitioning.partition_of(
            spec, ledger, {"title": "新しい人", "tags": ["地域:日本", "年代:2020"]}
        )

        assert older == ledger[0]["key"]
        assert newer == ledger[1]["key"]

    def test_the_prompt_says_how_far_the_partition_reaches(self):
        """**受け持ちを鍵のまま伝えない。** 端の区画は外側も引き受けるので、
        鍵どおりに伝えると、引き受けているのに誰も探しに行かない範囲ができる。"""
        band = partitioning.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 10}
        )
        ledger = [
            {"key": partitioning.band_key("日本", 1600, 1699), "count": 10},
            {"key": partitioning.band_key("日本", 1700, 1799), "count": 10},
        ]

        # 値の軸には上限も下限も無いので、端は開いたまま言う
        assert "1699 以下" in partitioning.describe(band, ledger[0]["key"], {}, ledger)
        assert "1700 以上" in partitioning.describe(band, ledger[1]["key"], {}, ledger)
        # 帯が 1 つしか無いなら、値では絞らない
        alone = [ledger[0]]
        only = partitioning.describe(band, alone[0]["key"], {}, alone)
        assert "すべて" in only and "1600" not in only

        title = partitioning.normalize({"by": "title", "target": 10})
        titles = [
            {"key": partitioning.title_key("あ", "お"), "count": 10},
            {"key": partitioning.title_key("さ", "そ"), "count": 10},
            {"key": partitioning.title_key("な", "の"), "count": 10},
        ]

        # 真ん中は次の始まりの手前まで、先頭と末尾はその外側も引き受ける
        assert "「な」の手前まで" in partitioning.describe(title, titles[1]["key"], {}, titles)
        assert "「さ」より前" in partitioning.describe(title, titles[0]["key"], {}, titles)
        assert "「な」以降" in partitioning.describe(title, titles[2]["key"], {}, titles)


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
        docs, diff = edited(refine, previous, [{"title": "ゴッホ", "body": "新しい本文"}])
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


class TestAskingForAPartition:
    """**区画の中身を、AI が自分で引けるようにする。**

    区画は「この範囲の全員」を並べて漏れを問う単位なので、中身を引けないと
    その問いが成り立たない —— 差し込み(`{current}`)でしか見えなかった頃は、
    1 回の依頼に載る量が上限だった(入り切らないぶんは黙って落ちる)。
    """

    @pytest.fixture
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    @pytest.fixture
    def spots(self, enabled, baked):
        collect.create("spots", prompt="{partition} を直して", interval_minutes=60)
        collect.update(
            "spots",
            partition={"by": "title", "target": 10},
            partitions=[
                {"key": partitioning.title_key("あ", "い"), "count": 2},
                {"key": partitioning.title_key("う", "え"), "count": 2},
            ],
        )
        return baked([("あ", "本文"), ("い", "本文"), ("う", "本文")], "spots")

    def _get(self, client, **params):
        return client.get("/v1/collect/spots/partition", params=params)

    def test_it_returns_everyone_in_that_range(self, client, spots, monkeypatch):
        monkeypatch.setattr(client.app.state, "sources", spots, raising=False)

        body = self._get(client, key=partitioning.title_key("あ", "い")).json()

        assert [d["title"] for d in body["docs"]] == ["あ", "い"]
        assert body["total"] == 2
        # どの範囲なのかも言う(鍵だけでは読めない)
        assert body["describes"]

    def test_the_screen_opens_the_same_range(self, client, spots, monkeypatch):
        """画面も同じところを見る(口を二重に持たない)。"""
        monkeypatch.setattr(client.app.state, "sources", spots, raising=False)

        res = client.get(
            "/admin/collect/spots/partition",
            params={"key": partitioning.title_key("あ", "い")},
        )

        assert res.status_code == 200
        assert "あ" in res.text and "い" in res.text
        assert "う" not in res.text.split("<table>")[-1]

    def test_what_was_removed_comes_too(self, client, enabled, baked, monkeypatch):
        """**何を外したのかが分からないと、同じものをもう一度挙げることになる。**

        読み口の既定と違うのは、ここが集める層のための口だから。
        """
        collect.create("spots", prompt="{partition}", interval_minutes=60)
        collect.update(
            "spots",
            partition={"by": "title", "target": 10},
            partitions=[{"key": partitioning.title_key("あ", "ん"), "count": 2}],
        )
        sources = baked([("あ", "本文"), ("消えた人", "外した理由")], "spots")
        monkeypatch.setattr(client.app.state, "sources", sources, raising=False)

        body = self._get(client, key=partitioning.title_key("あ", "ん")).json()

        assert "消えた人" in [d["title"] for d in body["docs"]]

    def test_a_collection_without_partitions_is_refused(self, client, sample):
        """区画を持たない収集では鍵の意味が無い(0 件ではなく理由を返す)。"""
        res = client.get("/v1/collect/news/partition", params={"key": "なんでも"})

        assert res.status_code == 400
        assert "区画を持っていません" in res.json()["error"]


class TestWhatWasRemoved:
    """消したものを、消したままにする。

    **消す回と足す回は別々に走る。** 足すほうは「いま名簿にいるか」しか見られず、
    なぜ居ないのか(一度も入っていないのか、調べたうえで外したのか)までは分からない
    —— 実際に、ざっとが「画家ではない」として外した人を、次の名簿の回が丸ごと
    連れ戻した(本番の履歴で 1,330 件の 1 件目がそれだった)。

    **消さずに印を付けて残すことで、そこが解ける。** 見出しは既にいるので足す回は
    素通りする。見出しの控え(墓場)を定義に持っていた頃は、1 件のメモに収める都合で
    2,000 件の上限が要り、溢れると古いものから静かに戻っていた。
    """

    @pytest.fixture
    def refine(self, sample):
        collect.update("news", prompt="いまの内容:\n{current}\n直して")
        return collect.get("news")

    def _edit(self, item, previous, collected):
        return collect.material(item, previous, collected, edits=True)

    def test_a_tombstone_marks_it_instead_of_deleting(self, refine):
        previous = {"俳優さん": {"doc_id": 1, "title": "俳優さん", "body": "本文", "tags": ["画家"]}}

        docs, diff = self._edit(
            refine, previous,
            [{"title": "俳優さん", "body": "絵ではなく演技で知られる人",
              "tags": [notes.TOMBSTONE_TAG]}],
        )

        [doc] = docs
        assert notes.REMOVED_TAG in doc["tags"]
        # **元のタグは残す** —— どういう条件でここに入ったのかが読めないと、
        # 消し間違いを確かめようがない
        assert "画家" in doc["tags"]
        # **本文はそのまま。** 理由で置き換えていた頃は、消し間違いを戻すときに
        # 元の中身がもう無く、育てたぶんを集め直すことになっていた
        assert doc["body"] == "本文"
        assert collect.removed_reason(doc) == "絵ではなく演技で知られる人"
        assert diff["removed_titles"] == ["俳優さん"]

    def test_a_tombstone_without_a_reason_keeps_the_body(self, refine):
        previous = {"俳優さん": {"doc_id": 1, "title": "俳優さん", "body": "もとの本文"}}

        docs, _diff = self._edit(
            refine, previous, [{"title": "俳優さん", "tags": [notes.TOMBSTONE_TAG]}]
        )

        assert docs[0]["body"] == "もとの本文"
        assert notes.REMOVED_TAG in docs[0]["tags"]

    def test_the_reason_does_not_bury_what_was_already_noted(self, refine):
        """理由は脇書きへ足すだけ。**先に入っていた事実は動かさない**。"""
        previous = {"俳優さん": {
            "doc_id": 1, "title": "俳優さん", "body": "本文",
            "extra": {"pageviews_month": 120},
        }}

        docs, _diff = self._edit(
            refine, previous,
            [{"title": "俳優さん", "body": "画家ではない", "tags": [notes.TOMBSTONE_TAG]}],
        )

        assert docs[0]["extra"]["pageviews_month"] == 120
        assert docs[0]["extra"]["removed_reason"] == "画家ではない"
        assert docs[0]["extra"]["removed_at"]

    def test_what_was_removed_does_not_come_back(self, refine):
        """足す回は「もう持っている」として素通りする(`only_new`)。"""
        previous = {"俳優さん": {
            "doc_id": 1, "title": "俳優さん", "body": "消した理由",
            "tags": ["画家", notes.REMOVED_TAG],
        }}

        docs, diff = collect.material(
            collect.get("news"), previous,
            [{"title": "俳優さん", "body": "画家です"}, {"title": "モネ", "body": "画家です"}],
            only_new=True,
        )

        assert diff["added"] == 1
        assert {d["title"] for d in docs} == {"俳優さん", "モネ"}
        # 印も理由も動かない
        kept = next(d for d in docs if d["title"] == "俳優さん")
        assert notes.REMOVED_TAG in kept["tags"]
        assert kept["body"] == "消した理由"

    def test_a_tombstone_for_something_absent_changes_nothing(self, refine):
        """持っていないものへの墓標は数えない(消すものが無い)。"""
        docs, diff = self._edit(refine, {}, [{"title": "居ない人", "tags": [notes.TOMBSTONE_TAG]}])

        assert docs == []
        assert diff["skipped"] == 1

    def test_the_count_does_not_shrink(self, refine):
        """**焼く件数は減らない**(印が付くだけ)。"""
        previous = {
            f"俳優{i}": {"doc_id": i, "title": f"俳優{i}", "body": "本文"} for i in range(1, 11)
        }

        docs, diff = self._edit(
            refine, previous, [{"title": "俳優1", "tags": [notes.TOMBSTONE_TAG]}]
        )

        assert len(docs) == 10
        assert diff["total"] == 10
        assert collect.shrink_blocked(collect.get("news"), diff, edits=True) is None

    def test_the_guard_counts_the_marks_not_the_count(self, refine):
        """**件数そのものは減らない**ので、結果の件数で見ていると素通りする。

        消えすぎの歯止めは「印を付けた件数」で数える。
        """
        previous = {
            f"俳優{i}": {"doc_id": i, "title": f"俳優{i}", "body": "本文"} for i in range(1, 11)
        }

        _docs, diff = self._edit(
            refine, previous,
            [{"title": f"俳優{i}", "tags": [notes.TOMBSTONE_TAG]} for i in range(1, 11)],
        )

        assert diff["total"] == 10, "件数は減らない"
        assert collect.shrink_blocked(collect.get("news"), diff, edits=True) is not None

    def test_the_material_says_which_ones_are_gone(self, refine):
        """**黙って並べると、消したものを「抜けている」と読んで足し直される。**

        載せているのは、同じものをもう一度挙げさせないため。
        **生きているものとは別の一覧にする** —— 混ぜると同じ枠を取り合うので、
        消すほど直す相手が見えなくなる。
        """
        previous = {
            "モネ": {"doc_id": 1, "title": "モネ", "body": "画家です", "tags": ["画家"]},
            "俳優さん": {"doc_id": 2, "title": "俳優さん", "body": "演技の人",
                      "tags": ["画家", notes.REMOVED_TAG]},
        }

        text, shown = collect.render_material(previous, scoped=True)

        alive, _, removed = text.partition("※ この対象で消したもの")
        assert "モネ" in alive and "俳優さん" not in alive
        assert "俳優さん" in removed
        # 生きているものの件数に、消えたものは入らない
        assert "全 1 件" in alive
        assert shown == 1
        assert "もう一度足さないでください" in text

    def test_the_material_shows_why_it_went_rather_than_the_body(self, refine):
        """**読ませたいのは「なぜもう一度足してはいけないか」**。

        本文は戻すときのために残してあるが、消えた 1 件でそれを読ませても、
        AI には生きているものと区別が付かない。
        """
        previous = {"俳優さん": {
            "doc_id": 1, "title": "俳優さん", "body": "生涯と代表作",
            "tags": ["画家", notes.REMOVED_TAG],
            "extra": {"removed_reason": "絵ではなく演技で知られる人"},
        }}

        text, _shown = collect.render_material(previous, scoped=True)

        assert "絵ではなく演技で知られる人" in text
        assert "生涯と代表作" not in text

    def test_the_mark_stays_in_the_tags_too(self, refine):
        """**外して見せると、AI がタグごと写して返したときに黙って戻る。**

        戻すのは明示的な操作にする。
        """
        previous = {"俳優さん": {
            "doc_id": 1, "title": "俳優さん", "body": "演技の人",
            "tags": ["画家", notes.REMOVED_TAG],
        }}

        text, _shown = collect.render_material(previous, scoped=True)

        assert notes.REMOVED_TAG in text

    def test_dropping_the_mark_brings_it_back(self, refine):
        """消したのが間違いだったときの戻し方。"""
        previous = {"モネ": {
            "doc_id": 1, "title": "モネ", "body": "消した理由",
            "tags": ["画家", notes.REMOVED_TAG],
        }}

        docs, _diff = self._edit(
            refine, previous, [{"title": "モネ", "body": "画家です", "tags": ["画家"]}]
        )

        assert notes.REMOVED_TAG not in docs[0]["tags"]

    def test_the_screen_shows_what_was_removed(self, refine, tmp_path):
        """消し間違いに気づく手立ては、ここを読むことしか無い。"""
        from app.views import admin

        path = tmp_path / "baked_news.db"
        conn = sqlite3.connect(path)
        conn.executescript(notes.SCHEMA_DDL)
        rows = [
            (1, "俳優さん", "生涯と代表作", ["画家", notes.REMOVED_TAG],
             {"removed_reason": "絵ではなく演技で知られる人"}),
            (2, "モネ", "画家です", ["画家"], {}),
        ]
        for doc_id, title, body, tags, extra in rows:
            conn.execute(
                "INSERT INTO docs"
                " (doc_id, title, opening, body, tags, extra, updated_at, rank_score)"
                " VALUES (?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', 0.0)",
                (doc_id, title, body, body, json.dumps(tags, ensure_ascii=False),
                 json.dumps(extra, ensure_ascii=False)),
            )
            for tag in tags:
                conn.execute(
                    "INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id)
                )
        conn.commit()
        conn.close()

        class Src:
            def __init__(self):
                self.path = path
                self.schema_version = 4

        html = admin._removed_html(collect.get("news"), {"news": Src()})

        # 出るのは消した理由（本文ではない）—— ここを読む人が確かめたいのはそちら
        assert "俳優さん —— 絵ではなく演技で知られる人" in html
        assert "生涯と代表作" not in html
        assert "モネ" not in html


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


class TestThePromptExample:
    """収集を追加するときの下書き。**書き方が分からない人に、形を見せるためのもの**。

    **ここから作られる収集は無い。** 見本の収集を勝手に置いていた頃は、消しても
    全部消した拍子に戻ってきた(本番で踏んだ)。
    """

    def test_it_shows_what_to_write(self):
        assert "押さえておくべき" in collect.PROMPT_EXAMPLE
        # 何を入れないかまで書いていないと、集まるものが散らかる
        assert "芸能" in collect.PROMPT_EXAMPLE

    def test_it_appears_in_the_form(self, enabled):
        from app.views import admin

        assert "押さえておくべき" in admin._collect_html({}, "")


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

    def test_what_was_baked_carries_its_tags(self, baked, sample):
        """同じ収集の中に種類の違うもの(記事とまとめ)が混ざる。

        **タグにしか出ていない**ので、無いと読む側が見分けられない。
        """
        sources = baked([("見出し", "本文")])
        conn = sqlite3.connect(sources["news"].path)
        conn.execute("UPDATE docs SET tags = ?", (json.dumps(["まとめ"]),))
        conn.commit()
        conn.close()

        assert collect.recent("news", sources)[0]["tags"] == ["まとめ"]

    def test_deleting_a_running_collection_is_refused(self, client, sample):
        """口を隠すだけでは足りない —— URL は届く。"""
        collect.update("news", enabled=True)

        assert client.post("/admin/collect/news/delete").status_code == 409
        # 断ったのだから残っている
        assert collect.get("news").name == "news"

    def test_running_it_lands_on_that_collection(self, client, sample, monkeypatch):
        """走らせた本人が結果を見に行くのに、もう一度その収集を探すことになっていた。"""
        from app.views import admin

        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.test")
        monkeypatch.setattr(admin, "trigger_run", lambda _name: None)
        res = client.post("/admin/collect/news/run", data={}, follow_redirects=False)

        assert res.status_code == 303
        assert res.headers["location"] == "/admin/collect/news"

    def test_the_pressed_sweep_is_recorded_before_the_trigger_wakes(
        self, client, sample, monkeypatch
    ):
        """取り込みが先に素材を取りに来ると、控えがまだ空で「次に走るはずの巡回」へ倒れる。

        押した巡回ではないものが走る —— しかも控えには走った巡回の名前が残るので、
        後から見ても取り違えに見えない。
        """
        from app.views import admin

        collect.update("news", sweeps=[
            # 予定がいちばん近いのはこちら(倒れるとこっちが走る)
            {"name": "ざっと", "interval_minutes": 5},
            {"name": "じっくり", "interval_minutes": 1440},
        ])
        seen = {}

        def wake(_name):
            # 取り込みが素材を取りに来たときに読むのと同じ控え
            seen["pending"] = collect.get("news").pending_sweep

        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.test")
        monkeypatch.setattr(admin, "trigger_run", wake)
        client.post("/admin/collect/news/run", data={"sweep": "じっくり"})

        assert seen["pending"] == "じっくり"

    def test_an_unknown_collection_is_404(self, client, sample):
        assert client.get("/v1/collect/nosuch").status_code == 404

    def test_creating_carries_the_sweeps(self, client, sample):
        """作る口が受け取らないと、外のアプリが渡した巡回が黙って落ちる。

        PATCH にだけ足して作成側を忘れていたので、外のアプリが作った収集は巡回を
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
        """控えを持たない構成で、読む側が毎回エラーを踏まないように。

        **定義の置き場とは分けて試す。** どちらも `CHIEZO_STATE_DIR` の下に
        あるので、環境変数を消すと「収集そのものが無効」になって、
        確かめたいこと（控えが無いときに空を返す）を通り過ぎてしまう。
        """
        from app import collect_log

        monkeypatch.setattr(collect_log, "db_path", lambda: None)
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
        # **消したら空。** 勝手に見本が湧かない
        assert client.get("/v1/collect").json()["collections"] == []

    def test_deleting_an_unknown_collection_is_404(self, client, sample):
        assert client.delete("/v1/collect/nosuch").status_code == 404

    def test_previewing_a_stopped_collection_is_refused(self, client, sample):
        """焼かないとはいえ AI は 1 回動くので、`run` と同じ扱いにする。

        外のアプリが自分で作った収集を自分で回せる状態にはしない。
        """
        assert client.post("/v1/collect/news/preview").status_code == 403

    def test_the_kind_comes_back_on_the_definition(self, client, enabled):
        """依頼した側が、流れとして扱われるのか網羅なのかを確かめられるようにする。"""
        res = client.post(
            "/v1/collect",
            json={
                "name": "tidy",
                "prompt": "いまの内容:\n{current}\n整理して",
                "interval_minutes": 60,
                "kind": "flow",
            },
        )
        assert res.status_code == 200
        assert res.json()["kind"] == "flow"
        assert res.json()["keep_days"] == collect.DEFAULT_KEEP_DAYS
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
    """今あるものを直す回。**いまの内容を読ませて、直すものと足すものを返させる**。

    **収集の設定ではなく、その回の依頼文が語る**(`edits_what_is_there`)——
    今あるものを差し込んでいれば直す回、差し込んでいなければ足すだけの回。
    収集ぜんたいの設定(`mode`)として持っていた頃は、巡回ごとに決められなかった。

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
        )

    def test_the_prompt_says_whether_it_edits(self, enabled):
        """今あるものを差し込んでいなければ、AI は今あるものを知らない ——
        知らないまま消す力を持たせられない。"""
        assert collect.edits_what_is_there("いまの分類:\n{current}\n整理して") is True
        assert collect.edits_what_is_there("{recent} をまとめて") is True
        assert collect.edits_what_is_there("{cursor} 以降を10件集めて") is False

    def test_an_add_only_sweep_never_edits(self):
        """漏れを足す回は今あるものを見せるが、触れてはいけない。"""
        assert collect.edits_what_is_there("{current} に無いものを足して", only_new=True) is False

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
            False,
            True,
        )
        assert sorted(d["title"] for d in docs) == ["新入り", "残る", "触れない"]
        assert (diff["added"], diff["updated"], diff["removed"]) == (1, 1, 0)
        # 直したほうは中身が入れ替わる
        assert next(d for d in docs if d["title"] == "残る")["body"] == "直した本文"

    def test_a_tombstone_marks_one(self, refine, baked):
        """消すのは明示したときだけ。**消さずに印を付けて残す**。"""
        sources = baked([("残る", "本文"), ("消す", "本文")], "spots")
        docs, diff = edited(
            collect.get("spots"),
            collect.previous_docs("spots", sources),
            [{"title": "消す", "body": "", "tags": [notes.TOMBSTONE_TAG]}],
        )
        marked = {d["title"]: d["tags"] for d in docs}
        assert notes.REMOVED_TAG in marked["消す"]
        assert notes.REMOVED_TAG not in marked["残る"]
        assert diff["removed_titles"] == ["消す"]

    def test_the_diff_names_what_moved(self, refine, baked):
        """件数だけでは、何が起きたのか読めない(変更履歴に残す元になる)。"""
        sources = baked([("残る", "本文")], "spots")
        _docs, diff = edited(
            collect.get("spots"),
            collect.previous_docs("spots", sources),
            [{"title": "残る", "body": "直した本文"}, {"title": "新入り", "body": "本文"}],
        )
        assert diff["added_titles"] == ["新入り"]
        assert diff["updated_titles"] == ["残る"]

    def test_a_tombstone_for_something_absent_does_nothing(self, refine):
        """持っていないものへの墓標は、消すものが無いだけ。"""
        _docs, diff = edited(
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

        **数えるのは印を付けた件数。** 消さずに残すので件数そのものは減らない ——
        結果の件数で見ていると、9 割に印が付いた回が素通りする。
        """
        import fastapi

        sources = baked([(f"分類{i}", "本文") for i in range(1, 11)], "spots")
        previous = collect.previous_docs("spots", sources)
        graves = [{"title": f"分類{i}", "tags": [notes.TOMBSTONE_TAG]} for i in range(1, 10)]
        with pytest.raises(fastapi.HTTPException) as got:
            collect.ndjson(collect.get("spots"), sources, previous, graves, False, True)
        assert got.value.status_code == 409
        assert "整理を止めました" in got.value.detail["error"]

    def test_the_guard_can_be_turned_off_on_purpose(self, refine, baked):
        """意図して大きく減らすときの逃げ道。外したことは定義に残る。"""
        collect.update("spots", keep_ratio=0)
        sources = baked([(f"分類{i}", "本文") for i in range(1, 11)], "spots")
        graves = [{"title": f"分類{i}", "tags": [notes.TOMBSTONE_TAG]} for i in range(1, 10)]
        body, diff = collect.ndjson(
            collect.get("spots"), sources, collect.previous_docs("spots", sources),
            graves, False, True,
        )
        assert diff["removed"] == 9
        # **消さずに残すので、焼く件数は減らない**(印が付くだけ)
        assert len(body.splitlines()) == 11  # meta + 10 件

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

    def _detail(self, name="news"):
        """収集 1 つぶんの面。**一覧には出さないもの**(プロンプト・区画・直す口)。"""
        from app.views import admin

        return admin._collect_detail_html(collect.get(name), "")

    def _table(self, name="news"):
        """巡回の表の中身。**設定はここに畳んで置く**(一覧と詳細で同じもの)。"""
        from app.views import admin

        return admin._sweep_table_body(collect.get(name), "")

    def test_the_progress_link_stays_on_the_page(self, sample):
        """取り込みの様子は玄関にも初期化の面にも出る。

        行き先を書き切っていたせいで、**どこから押しても記憶の画面へ飛んでいた** ——
        見ていた画面から連れ出される。
        """
        from app.views import admin

        html = admin._job_status_html({"state": "running", "source": "jawiki"})

        assert 'href="?#job"' in html
        assert "/admin/memory#job" not in html

    def test_the_kind_is_the_first_mark_on_the_row(self, sample):
        """一覧でまず見えるのは種類。

        「整理」を目立たせていたせいで、それが種類のことだと読まれた —— あれは
        返ってきた 1 件で何ができるかの話でしかない。
        """
        collect.update(
            "news", kind="flow", keep_days=30, mode="refine", prompt="{cursor} {recent}"
        )
        html = self._html(sample)

        assert '<span class="stale">流れ</span>' in html
        # 消えることは押す前に見えている必要がある
        assert "30 日ぶん" in html

    def test_a_stock_says_so(self, sample):
        collect.update("news", kind="stock")
        html = self._html(sample)

        assert '<span class="stale">網羅</span>' in html
        assert "日ぶん" not in html

    def test_a_running_collection_has_no_delete_button(self, sample):
        """動いている収集を消すと、走っている最中の 1 回が焼く先の定義を失う。"""
        collect.update("news", enabled=True)
        html = self._html(sample)

        assert "/delete" not in html
        assert "止めると消せます" in html

    def test_a_stopped_collection_can_be_deleted(self, sample):
        html = self._html(sample)

        assert "/admin/collect/news/delete" in html

    def test_what_is_running_now_is_shown(self, sample, monkeypatch):
        """控えは終わってから 1 行になるので、押した直後は何も出なかった。

        走っているのかを確かめるのに、別の画面まで見に行くことになっていた。
        """
        from app import ai_inflight
        from app.views import admin

        monkeypatch.setattr(
            ai_inflight,
            "running",
            lambda limit=50: [
                {"at": "2026-09-13T00:00:00+00:00", "caller": "collect:news",
                 "backend": "claude", "model": "opus"},
                # 会話のぶんは混ぜない（ここは収集の面）
                {"at": "2026-09-13T00:00:00+00:00", "caller": "chat", "backend": "codex"},
            ],
        )

        html = admin._collect_running_html("news")
        assert "いま 1 件走っています" in html
        assert "収集(news)" in html
        assert "codex" not in html

    def test_nothing_running_shows_nothing(self, sample, monkeypatch):
        """空の表を出すと「動いていない」ではなく「壊れている」に見える。"""
        from app import ai_inflight
        from app.views import admin

        monkeypatch.setattr(ai_inflight, "running", lambda limit=50: [])
        assert admin._collect_running_html("news") == ""

    def test_only_the_first_partitions_are_listed(self, sample):
        """全部を出すと、その下にある変更履歴まで面の外へ押し出される。"""
        from app.views import admin

        collect.update(
            "news",
            partition={"by": "title", "target": 10},
            partitions=[{"key": f"区画{i}", "count": 1} for i in range(25)],
        )
        html = admin._partition_html(collect.get("news"))

        assert "区画0" in html
        assert "区画9" in html
        # **捨てはしない** —— 開けば全部ある
        assert "残りの 15 区画を見る" in html
        assert "区画24" in html
        assert html.index("残りの 15 区画を見る") < html.index("区画24")

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
        assert html.count('name="sweep" value="じっくり"') == 1

        # **ドライランは面のほうだけ。** 押した先で結果を読む口なので、
        # 読みに来る場所に置く —— 一覧に並べると収集の数だけ場所を食う
        assert ">ドライラン</button>" not in html
        assert self._table().count(">ドライラン</button>") == 2

    def test_a_mechanical_sweep_says_it_uses_no_ai(self, sample):
        """「既定にまかせる」は『誰に頼むかは Chiezo が決める』の意味で、
        頼むこと自体は起きるように読める —— 機械で引く回は AI を呼ばない。
        """
        collect.update("news", sweeps=[
            {"name": "名簿", "use_extract": True},
            {"name": "肉付け", "interval_minutes": 360},
        ])
        html = self._html(sample)

        assert "AI 利用無し" in html
        # AI に頼む回のほうは、これまでどおり既定だと分かるように書く
        assert "既定にまかせる" in html

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
        # 区画の一覧は収集の面へ。**途中で切らない**（どこがまだかを読みに来る面なので）
        assert "く〜そ" in self._detail()

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

    def test_the_name_opens_the_collection_page(self, sample):
        """**一覧の中で開かない。** 折り畳みに押し込んでいた頃は、開くたびに表が
        縦へ伸びて他の収集の行が画面外へ出ていた —— 読みに来た人はその収集だけを
        見に来ているので、専用の面に置けば畳む理由が無い。
        """
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        html = self._html(sample)

        assert '<a href="/admin/collect/news">news</a>' in html
        # プロンプトと収集ぜんたいの設定は、一覧には出さない（下の「収集を追加する」は別物）
        assert "<summary>プロンプト</summary>" not in html
        assert "/admin/collect/news/edit" not in html
        # **巡回の設定も一覧には出さない。** 直しに来る場所は収集の面で、
        # 一覧は「動いているか」を読むための表 —— 畳んであっても、収集の数だけ
        # 行が増えて、見たいものが画面の外へ押し出される
        assert '<tr class="sweep-edit">' not in html
        assert "巡回を足す" not in html
        # 面のほうには出る
        assert '<tr class="sweep-edit">' in self._table()
        # **名前は繰り返さない**（すぐ上の行に出ているし、名前入りだと
        # 下の巡回の見出しに見える）
        assert "<summary>設定</summary>" in self._table()
        # 面のほうには畳まずに出る
        detail = self._detail()
        assert '<pre class="prompt-view">' in detail
        assert "<summary>プロンプト</summary>" not in detail

    def test_sweeps_are_fields_not_json(self, sample):
        """JSON を直に書かせると、間隔ひとつ変えるのに配列の構文を相手にすることになる。

        間隔・相手・モデル・考える量はもともと「1 本ぶんの設定」なので、その一組を
        繰り返せるようにすれば足りる。
        """
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        html = self._table()

        assert '<textarea name="sweeps"' not in html
        assert html.count('<input name="sweep_name"') == 3  # 2 本 + 足すための空枠
        assert '<select name="sweep_backend">' in html
        assert "名前を消すと、この巡回は無くなります" in html

    def test_each_sweep_saves_on_its_own(self, sample):
        """全部を送り直していた頃は、1 本の間隔を直すのに他の巡回まで上書きしていた。"""
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        html = self._table()

        assert html.count("この巡回を保存") == 3  # 2 本 + 足すための空枠
        assert html.count('action="/admin/collect/news/sweep"') == 3
        # どの巡回を書き換えるかは鍵で渡す（改名できるように、名前とは別に持つ）
        assert '<input type="hidden" name="sweep_key" value="ざっと">' in html

    def test_one_sweep_needs_no_name_to_delete(self, sample):
        """1 本しか無いときは、消す案内を出さない（消したら回らなくなる）。"""
        html = self._table()
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
        from app import collect_log

        monkeypatch.setattr(collect_log, "db_path", lambda: None)
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


class TestTheCollectionPage:
    """収集 1 つぶんの面(`/admin/collect/{name}`)。

    一覧の折り畳みに押し込んでいた頃は、開くたびに表が縦へ伸びて他の収集の行が
    画面外へ出ていた。読みに来た人はその収集だけを見に来ているので、専用の面に
    置けば畳む理由が無い。
    """

    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_it_opens_with_everything_unfolded(self, client, sample):
        collect.update("news", sweeps=[{"name": "ざっと"}, {"name": "じっくり"}])
        html = client.get("/admin/collect/news").text

        assert '<pre class="prompt-view">' in html
        assert "<summary>プロンプト</summary>" not in html
        # 巡回の表も、直す口も同じ面にある
        assert "ざっと" in html and "じっくり" in html
        assert "/admin/collect/news/edit" in html
        assert "/admin/collect/news/focus" in html
        # まだ焼いていない収集には、溜まったものへの入口を出さない(404 になるだけ)
        assert "まだ焼いていない" in html

    def test_every_partition_is_listed(self, client, sample):
        """**途中で切らない。** ここは「どこを見ていて、どこがまだか」を読む面。"""
        keys = [partitioning.title_key(f"あ{i}", f"い{i}") for i in range(30)]
        collect.update(
            "news",
            partition={"by": "title", "target": 10},
            partitions=[{"key": k, "count": 10} for k in keys],
        )
        html = client.get("/admin/collect/news").text

        for key in keys:
            assert esc_key(key) in html
        assert "ほか" not in html

    def test_the_changes_are_only_this_one(self, client, sample, monkeypatch, tmp_path):
        """その収集の面なので、名前の列は出さない(全部同じ名前が並ぶだけ)。"""
        from app import collect_log

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        collect_log.record("news", status=collect_log.STATUS_OK, diff={"total": 1, "added": 1})
        collect_log.record("other", status=collect_log.STATUS_OK, diff={"total": 9, "added": 9})

        html = client.get("/admin/collect/news").text
        assert "直近の変更" in html
        assert "other" not in html
        assert "<th>収集</th>" not in html

    def test_an_unknown_collection_is_404(self, client, sample):
        assert client.get("/admin/collect/nosuch").status_code == 404


def esc_key(key: str) -> str:
    from app.pages import esc

    return esc(key)


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

    def test_the_page_links_to_what_is_in_there_now(self, generations):
        """差分に出るのは本文とタグだけ。出典も extra も、同じタグの他の文書への
        導線もここには無いので、いまの中身を開ける入口を添える。
        """
        from app.views import admin

        versions = collect.doc_versions("news", generations, "残る店")
        html = admin._doc_diff_page_html("news", "残る店", versions)

        assert f'/search/news/doc/{versions["now"]["doc_id"]}' in html
        assert "いまの中身を見る" in html

    def test_a_removed_one_has_nothing_to_open(self, generations):
        """消えたものはいまの世代に無いので、指す先が無い。"""
        from app.views import admin

        html = admin._doc_diff_page_html(
            "news", "消える店", collect.doc_versions("news", generations, "消える店")
        )
        assert "いまの中身を見る" not in html

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


class TestSeeingTheMachineStore:
    """設定の置き場(`app/machine_store.py`)を読めるようにする。

    **人が触らない置き場だが、見えないままでは確かめようがない。** 収集が消えた・
    戻ってきたのような話を追うとき、まず知りたいのは「設定がどこに、いつの姿で
    残っているか」のほう。
    """

    def test_it_is_registered_as_an_ordinary_source(self, sample, tmp_path):
        """**専用の読み口を作らない。** コアスキーマで持てば、検索も中身の閲覧も
        普通のソースと同じ口でできる(短期記憶が `notes` として登録されているのと同じ)。
        """
        from app import machine_store
        from app.main import scan_all
        from app.registry import SUPPORTED_SCHEMA_VERSIONS

        sources = scan_all(tmp_path / "corpus")
        src = sources[machine_store.SOURCE_NAME]

        assert src.kind == machine_store.SOURCE_KIND
        assert src.schema_version in SUPPORTED_SCHEMA_VERSIONS
        # 追記される置き場なので immutable では開けない
        assert src.mutable
        assert src.doc_count == 1

    def test_the_definition_is_searchable(self, sample):
        """中身は全文検索にも載る(索引は書くときに一緒に入れる)。"""
        from app import db, machine_store

        path = machine_store.db_path()
        rows = db.query(
            path,
            "SELECT d.title FROM docs_fts f JOIN docs d ON d.doc_id = f.rowid"
            " WHERE docs_fts MATCH ?",
            ('"news"',),
        )

        assert [r["title"] for r in rows] == [f"{collect.DEFS_KIND}/{collect.DEFS_KEY}"]

    def test_rewriting_keeps_the_same_doc_id(self, sample):
        """書き換えのたびに文書の URL が変わらないようにする。"""
        from app import machine_store

        [before] = machine_store.records()
        collect.create("another", prompt="p", interval_minutes=60)
        [after] = machine_store.records()

        assert after["doc_id"] == before["doc_id"]
        assert after["bytes"] > before["bytes"]

    def test_the_screen_points_at_the_source(self, sample):
        from app import machine_store
        from app.views import admin

        html = admin._machine_html({})

        assert f"{collect.DEFS_KIND}/{collect.DEFS_KEY}" in html
        assert f"/search/{machine_store.SOURCE_NAME}/doc/" in html

    def test_it_says_so_when_there_is_no_place(self, monkeypatch):
        from app.views import admin

        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)

        assert "設定の置き場は無効です" in admin._machine_html({})


class TestOpeningAPartition:
    """区画の名前から、**その区画に入っているもの**へ。

    鍵と数だけでは、割り方が合っているかを人が確かめようがない —— 合っているか
    どうかは、並んだ顔ぶれを見て初めて分かる。
    """

    @pytest.fixture()
    def partitioned(self, sample):
        collect.update(
            "news",
            partition={"by": "band", "prefix": "地域", "value": "年代", "target": 10},
            partitions=[{"key": partitioning.band_key("日本", 1800, 1900), "count": 2}],
        )
        return collect.get("news")

    def test_the_name_links_to_what_is_inside(self, partitioned):
        from app.views import admin

        html = admin._partition_html(partitioned)

        assert "/admin/collect/news/partition?key=" in html
        assert "1800-1900" in html

    def test_the_key_goes_in_the_query(self, partitioned):
        """鍵はそのままクエリへ(区切りや記号を含む鍵でも指し先が崩れない)。"""
        from app.views import admin

        html = admin._partition_html(partitioned)

        assert "partition?key=%E6%97%A5%E6%9C%AC" in html

    def test_the_page_lists_the_members(self, partitioned):
        from app.views import admin

        members = {
            "北斎": {"doc_id": 1, "title": "北斎", "body": "浮世絵師", "tags": ["地域:日本"]},
            "写楽": {
                "doc_id": 2, "title": "写楽", "body": "生涯と代表作",
                "tags": ["地域:日本", notes.REMOVED_TAG],
                "extra": {"removed_reason": "画家ではない"},
            },
        }
        key = partitioned.partitions[0]["key"]

        html = admin._partition_members_html("news", partitioned, key, members, {})

        assert "北斎" in html and "写楽" in html
        # 件数は生きているものだけ(台帳の数と揃える)。消えたものは別に数える
        assert "1 件" in html and "ほかに消えたもの 1 件" in html
        # 焼けているものは、いまの中身へ飛べる
        assert "/search/news/doc/1" in html
        # **消えたものもここには出す**(何を外したのかが見えないと判断に使えない)
        assert "消えたもの: 画家ではない" in html

    def test_an_empty_partition_says_so(self, partitioned):
        """空欄だけを出すと「読めなかった」のか「居ない」のか分からない。"""
        from app.views import admin

        html = admin._partition_members_html(
            "news", partitioned, partitioned.partitions[0]["key"], {}, {}
        )

        assert "1 件も入っていません" in html


class TestEditingTheSweeps:
    """巡回の設定は、その行の下に畳んで置く。**1 本ずつ保存する**。

    収集ぜんたいの編集フォームに全部を並べていた頃は、1 本の間隔を直すのに他の巡回まで
    送り直していた —— 別のセッションが同時に別の巡回を直していると、後から押したほうで
    上書きされる。**名前を書けば増え、消せば減る**のは変えていない。
    """

    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def _save_one(self, client, row, key=""):
        """巡回 1 本ぶんを保存する。`key` は書き換える相手(空なら足す)。"""
        data = {"sweep_key": key}
        for field in ("name", "interval", "cover_days", "per_run",
                      "backend", "model", "effort", "enabled", "clock", "merge", "source"):
            data[f"sweep_{field}"] = row.get(field, "")
        res = client.post("/admin/collect/news/sweep", data=data, follow_redirects=False)
        assert res.status_code in (200, 303), res.text[:400]
        return res

    def _save(self, client, rows):
        """並べて書いたぶんを、上から 1 本ずつ保存する。

        既にある名前は書き換え、無い名前は足す(画面の「巡回を足す」と同じ)。
        """
        for row in rows:
            existing = [s["name"] for s in collect.get("news").sweeps]
            key = row.get("key", row.get("name") if row.get("name") in existing else "")
            self._save_one(client, row, key)

    def test_it_goes_back_by_the_stored_name(self, client, sample):
        """**行き先は保存できた側の名前から組む。**

        要求に入っていた文字列をそのまま繋ぐと、名前の狭さ(`collect.NAME_RE`)が
        効いていない場所が 1 つだけ残る —— 読む側にも「任意の URL を作れる」と見える。
        """
        res = self._save_one(client, {"name": "ざっと", "interval": "360", "enabled": "1"})

        assert res.headers["location"] == "/admin/collect/news"

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

        self._save_one(client, {"name": "", "interval": "1440", "enabled": "1"}, key="じっくり")
        assert [s.name for s in collect.sweeps_of(collect.get("news"))] == ["ざっと"]

    def test_an_unnamed_lone_sweep_is_kept_as_the_collection_itself(self, client, sample):
        """1 本しか持たない収集に一覧を持たせると、「既定」という名前だけが画面に増える。

        **名前を付けたものは名前のまま残す** —— 2 本目を消したときに 1 本目の名前まで
        化けると、何を消したのか分からなくなる（上のテストがそこを縛っている）。
        """
        self._save_one(client, {"name": "既定", "interval": "120",
                                "backend": "claude", "enabled": "1"})
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
        from app.views import admin

        html = admin._sweep_table_body(collect.get("news"), "")
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


class TestTheTwoInsertionLimits:
    """件数と文字数、どちらが先に当たるか。

    区画を切る側は件数(`target`)で大きさを決めるので、**文字数のほうが先に
    当たると、区画を件数どおりに切ったのに中身が全部は載らない**。そのとき
    AI に届くのは「載っているぶんだけを整理してください」なので、漏れを問う
    前提(この区画の全部が並んでいる)が黙って崩れる。
    """

    # AI が肉付けしたあとの 1 行(実測の中央値)。本文は MATERIAL_BODY_CHARS まで
    # 書かれ、タグも 5 つ付く
    MATURE_LINE_CHARS = 249

    def test_the_count_runs_out_before_the_characters_do(self):
        assert collect.MAX_MATERIAL_DOCS * self.MATURE_LINE_CHARS <= collect.MAX_MATERIAL_CHARS

    def test_a_full_partition_fits_whole(self):
        # 差し込みの件数いっぱいまで育った区画が、切られずに全部載ること
        docs = {
            f"店{n}": {
                "title": f"店{n}",
                "tags": ["食事処", "出典:OSM", "ジャンル:寿司", "地域:東京都", "ランク:C"],
                "body": "所在地: 東京都中央区\n" + "あ" * collect.MATERIAL_BODY_CHARS,
            }
            for n in range(collect.MAX_MATERIAL_DOCS)
        }

        text, shown = collect.render_material(docs, scoped=True)

        assert shown == collect.MAX_MATERIAL_DOCS
        assert "今回の対象外" not in text


class TestBakingWithoutHoldingItAll:
    """焼く素材は 1 行ずつ流す。**丸ごとは持たない**。

    前世代を dict に読んで繋いでいた頃は、50 万件の地図の名簿で 1.8 GB + 460 MB
    かかった。取り込み側は元から流し込みで受けている(`copyfileobj`)ので、
    配信側だけが丸ごと持っていた。
    """

    @staticmethod
    def item(**overrides):
        base = {
            "name": "probe", "description": "", "prompt": "", "interval_minutes": 60,
            "enabled": False, "backend": None, "model": None, "effort": None,
            "web": False, "cursor": "", "created_at": "", "updated_at": "",
        }
        return collect.Collection(**{**base, **overrides})

    @staticmethod
    def rows(count):
        """前世代の行。**呼ぶたびに新しく流れる**(2 周するため)。"""
        def make():
            for n in range(1, count + 1):
                yield {
                    "doc_id": n, "title": f"店{n}", "opening": "要約", "body": "本文",
                    "tags": ["食事処"], "updated_at": "2026-01-01T00:00:00+00:00",
                    "extra": {"lat": 35.0, "lon": 139.0},
                }
        return make

    def test_it_yields_one_line_at_a_time(self):
        lines = collect.bake_lines(self.item(), {}, self.rows(3), [])

        assert not isinstance(lines, list)
        out = list(lines)
        # 1 行目は meta、以降が 1 行 1 文書
        assert json.loads(out[0])["meta"]["min_docs"] == 1
        assert [json.loads(line)["title"] for line in out[1:]] == ["店1", "店2", "店3"]

    def test_the_same_shape_as_the_whole_string(self):
        # 丸ごと組む道(`ndjson`)と、流す道が同じものを返すこと
        whole, _diff = collect.ndjson(self.item(), {}, self.rows(3), [])
        streamed = "\n".join(collect.bake_lines(self.item(), {}, self.rows(3), [])) + "\n"

        assert streamed == whole

    def test_it_refuses_before_the_first_line(self):
        """**流し始めたら断れない。** 空なら 1 行目より前に止まること。"""
        with pytest.raises(HTTPException) as caught:
            list(collect.bake_lines(self.item(), {}, lambda: iter([]), []))

        assert caught.value.status_code == 409

    def test_it_reads_the_previous_generation_more_than_once(self):
        # 数える周と流す周で 2 度読む。**1 度きりの iterator は受け取れない**
        made = []

        def rows():
            made.append(1)
            return iter([])

        with pytest.raises(HTTPException):
            list(collect.bake_lines(self.item(), {}, rows, []))

        assert len(made) >= 1

    def test_edits_are_the_side_it_holds(self):
        """持つのは**小さいほう**。前世代ではなく、その回に直すぶん。"""
        edits = collect.Edits([{"title": "店2", "body": "直した", "tags": ["食事処"]}])
        diff = {}
        docs = list(collect.stream_docs(self.item(), self.rows(3)(), edits, edits=True, diff=diff))

        assert [d["title"] for d in docs] == ["店1", "店2", "店3"]
        assert next(d for d in docs if d["title"] == "店2")["body"] == "直した"
        assert diff["previous"] == 3
        assert diff["updated"] == 1
        assert diff["added"] == 0


class TestShowingWhereItCameFrom:
    """差し込みに出典を載せる。**見せなければ写しようがない**。

    「上に並んでいるものの URL をそのまま写して」と頼んでいたのに、差し込む行に
    URL が無かった —— まとめの節が全部「提供情報に記事URLの記載なし」になった(実測)。
    """

    @staticmethod
    def doc(title, url=None):
        return {
            "doc_id": 1, "title": title, "body": "要約", "tags": ["ニュース"],
            "updated_at": "2026-09-15T00:00:00+00:00",
            "extra": {"url": url} if url else {},
        }

    def test_the_source_url_is_shown(self):
        text = collect.render_recent({"記事": self.doc("記事", "https://example.com/1")}, None)

        assert "https://example.com/1" in text

    def test_an_item_without_one_still_shows(self):
        # 出典が無いものを落とすと、件数だけが黙って減る
        text = collect.render_recent({"記事": self.doc("記事")}, None)

        assert "記事" in text

    def test_a_url_that_is_not_one_is_left_out(self):
        # 押せない文字列を出典として見せると、AI がそれを写す
        text = collect.render_recent({"記事": self.doc("記事", "記載なし")}, None)

        assert "記載なし" not in text
