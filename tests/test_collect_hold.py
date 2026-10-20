"""区画を見終わったことにしない条件(巡回の `hold_tag`)と、引き当ての状態の印
(`resolve` の `failed_when_missing` / `none_tag`)。

押さえているのは:

- **印付きの 1 件が残る区画には、見終わった印を付けない**(次の回もまた選ばれる)
- **歯止め**: その回で 1 件も減らなければ、見終わったことにして次へ進む
- **一周の数え方が嘘にならない**(足止めした区画は「まだ」のまま)
- 手がかりがまだ無い 1 件にも `failed_tag` が付き、「無かった」の印の 1 件には付かない ——
  **その回に AI が返さなかった 1 件にも**(焼くたびに全部へ付け直す)
"""
from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from app import collect, notes, resolve
from app import partition as partitioning
from tests.test_collect_resolve import make_source_for

FAILED = "状態:未定"
NONE = "状態:無し"
RESOLVE = {
    "clue": "shop",
    "sources": [{"source": "osm_japan", "tag": "出典:OSM"}],
    "area_tag": "地域:",
    "failed_tag": FAILED,
    "failed_when_missing": True,
    "none_tag": NONE,
}
PARTITION = {"by": "tag", "prefix": "地域:", "target": 5000, "values": ["東京都", "大阪府"]}
SWEEP = "整理"


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    return tmp_path


@pytest.fixture
def dishes(enabled):
    collect.create(
        "dishes", prompt="{partition}\n{current}", interval_minutes=60, resolve_spec=RESOLVE,
    )
    return collect.update(
        "dishes",
        enabled=True,
        partition=PARTITION,
        partitions=[{"key": "地域:東京都", "count": 3}, {"key": "地域:大阪府", "count": 1}],
        sweeps=[{"name": SWEEP, "interval_minutes": 60, "partitions_per_run": 1,
                 "hold_tag": FAILED}],
    )


def _dish(doc_id: int, title: str, area: str, *tags, shop=None) -> dict:
    extra = {"shop": shop} if shop is not None else {}
    return {"doc_id": doc_id, "title": title, "body": "説明",
            "tags": [f"地域:{area}", *tags], "extra": extra}


def _run(item_name: str, previous: dict[str, dict], answer: list[dict], monkeypatch) -> str:
    """整理の 1 回。AI の答えは `answer`、見た区画は選んだ区画全部(最後まで返った)。"""
    from app import main

    async def no_feed(*_a, **_k):
        return None

    async def answered(_item, _for_prompt, _sources, keys, *_rest, **_k):
        done = _rest[5]
        done.extend(keys)
        return list(answer), None, ""

    monkeypatch.setattr(main, "_harvest", no_feed)
    monkeypatch.setattr(main, "_collect_items", answered)
    collect.mark_started(item_name, SWEEP)
    flow = asyncio.run(main._collect_material(item_name, make_source_for(collect.get(item_name), previous)))
    # 流し切って焼き上がりを返す(数える周と流す周が同じものを渡すかも見る)
    return "".join(flow)


def _visits(name: str) -> dict[str, str | None]:
    return {
        p["key"]: (p.get("visits") or {}).get(SWEEP) for p in collect.get(name).partitions
    }


class TestHoldingAPartition:
    def test_a_partition_with_marked_items_left_is_not_marked(self, dishes, monkeypatch):
        """3 品のうち 1 品だけ片付いた(残り 2 品)—— 東京都は見終わりにしない。"""
        previous = {d["title"]: d for d in [
            _dish(1, "料理A", "東京都"), _dish(2, "料理B", "東京都"), _dish(3, "料理C", "東京都"),
            _dish(4, "料理D", "大阪府"),
        ]}
        answer = [{"title": "料理A", "body": "探したが無い", "tags": ["地域:東京都", NONE],
                   "extra": {"shop": None}}]
        _run("dishes", previous, answer, monkeypatch)

        assert _visits("dishes") == {"地域:東京都": None, "地域:大阪府": None}
        item = collect.get("dishes")
        spec = partitioning.normalize(item.partition)
        # 次の回もまた東京都が選ばれる
        assert partitioning.pick(item.partitions, SWEEP, 1, spec) == ["地域:東京都"]
        # 変更履歴に足止めした区画と残りが出て、見終えた区画には入らない
        from app import collect_log
        row = collect_log.recent("dishes", limit=1)[0]
        assert row["visited"] == []
        assert "地域:東京都 残り 2 件" in (row["error"] or "")

    def test_no_progress_marks_it_and_moves_on(self, dishes, monkeypatch):
        """**歯止め**: 1 件も減らなかった回は見終わったことにする(一周を止めない)。"""
        previous = {d["title"]: d for d in [
            _dish(1, "料理A", "東京都", NONE), _dish(2, "料理B", "東京都", FAILED),
            _dish(3, "料理C", "東京都", FAILED), _dish(4, "料理D", "大阪府", FAILED),
        ]}
        _run("dishes", previous, [], monkeypatch)

        visits = _visits("dishes")
        assert visits["地域:東京都"] and not visits["地域:大阪府"]
        item = collect.get("dishes")
        spec = partitioning.normalize(item.partition)
        assert partitioning.pick(item.partitions, SWEEP, 1, spec) == ["地域:大阪府"]

    def test_clearing_the_last_one_marks_it(self, dishes, monkeypatch):
        previous = {d["title"]: d for d in [
            _dish(1, "料理A", "東京都", NONE), _dish(2, "料理B", "東京都", NONE),
            _dish(3, "料理C", "東京都", FAILED), _dish(4, "料理D", "大阪府", FAILED),
        ]}
        answer = [{"title": "料理C", "body": "探したが無い", "tags": ["地域:東京都", NONE],
                   "extra": {"shop": None}}]
        _run("dishes", previous, answer, monkeypatch)
        assert _visits("dishes")["地域:東京都"]

    def test_the_lap_is_not_counted_while_held(self, dishes, monkeypatch):
        """足止めした区画は「まだ」—— 一周を待つ巡回も、一周で止まる巡回も、まだと読む。"""
        collect.update("dishes", sweeps=[
            {"name": SWEEP, "interval_minutes": 60, "partitions_per_run": 1, "hold_tag": FAILED,
             "one_lap": True},
            {"name": "情報収集", "interval_minutes": 60, "after": SWEEP, "only_new": True},
        ], partitions=[
            {"key": "地域:東京都", "count": 3},
            {"key": "地域:大阪府", "count": 1, "visits": {SWEEP: "2026-10-01T00:00:00+00:00"}},
        ])
        previous = {d["title"]: d for d in [
            _dish(1, "料理A", "東京都"), _dish(2, "料理B", "東京都"), _dish(3, "料理C", "東京都"),
            _dish(4, "料理D", "大阪府"),
        ]}
        answer = [{"title": "料理A", "body": "無い", "tags": ["地域:東京都", NONE],
                   "extra": {"shop": None}}]
        _run("dishes", previous, answer, monkeypatch)

        item = collect.get("dishes")
        tidy, survey = collect.sweeps_of(item)
        assert partitioning.progress(item.partitions, SWEEP) == (1, 2)
        assert not collect.lapped(item, tidy)
        assert not collect.waited_for(item, survey)
        assert partitioning.oldest_visit(item.partitions, SWEEP) is None

    def test_sweeps_without_the_tag_mark_as_before(self, dishes, monkeypatch):
        collect.update("dishes", sweeps=[
            {"name": SWEEP, "interval_minutes": 60, "partitions_per_run": 1},
        ])
        previous = {d["title"]: d for d in [
            _dish(1, "料理A", "東京都"), _dish(2, "料理B", "東京都"), _dish(3, "料理C", "東京都"),
            _dish(4, "料理D", "大阪府"),
        ]}
        answer = [{"title": "料理A", "body": "無い", "tags": ["地域:東京都", NONE],
                   "extra": {"shop": None}}]
        _run("dishes", previous, answer, monkeypatch)
        assert _visits("dishes")["地域:東京都"]


class TestStillHeld:
    @pytest.mark.parametrize(("before", "after", "held"), [
        ({"k": 14}, {"k": 6}, ["k"]),   # 減った・まだ残る → 足止め
        ({"k": 6}, {"k": 0}, []),       # 片付いた
        ({"k": 6}, {"k": 6}, []),       # 減らなかった(歯止め)
        ({"k": 2}, {"k": 5}, []),       # 足したぶんで増えた → 進んでいない
        ({}, {"k": 3}, []),             # 前は 0
    ])
    def test_only_progress_with_some_left_holds(self, before, after, held):
        assert partitioning.still_held(["k"], before, after) == held


class TestTheSweepSpec:
    def test_hold_tag_is_kept_and_read(self, dishes):
        assert collect.sweeps_of(dishes)[0].hold_tag == FAILED
        assert collect.get("dishes").sweeps[0]["hold_tag"] == FAILED

    @pytest.mark.parametrize("bad", [["a"], "_chiezo_x", "あ" * 61])
    def test_unreadable_hold_tags_are_refused(self, dishes, bad):
        with pytest.raises(HTTPException):
            collect.update("dishes", sweeps=[{"name": SWEEP, "hold_tag": bad}])

    def test_an_empty_hold_tag_is_dropped(self, dishes):
        item = collect.update("dishes", sweeps=[{"name": SWEEP, "hold_tag": " "}])
        assert "hold_tag" not in item.sweeps[0]


def _baked(body: str) -> dict[str, dict]:
    return {d["title"]: d for d in map(json.loads, body.splitlines()[1:])}


class TestStateTags:
    """`failed_when_missing` / `none_tag` は、**焼く全部の 1 件**に付け直す。"""

    def test_items_not_returned_are_marked_too(self, dishes):
        resolved = {"name": "店", "source": "osm_japan", "resolved_at": "2026-10-01T00:00:00Z",
                    "clue": {"name": "店"}}
        previous = {d["title"]: d for d in [
            _dish(1, "手がかりなし", "東京都"),
            _dish(2, "引き当てできず", "東京都", shop={"clue": {"name": "x"}, "unresolved": "無い"}),
            _dish(3, "無かった", "東京都", FAILED, NONE),
            _dish(4, "決まった", "東京都", FAILED, NONE, shop=resolved),
            _dish(5, "消えた", "東京都", notes.REMOVED_TAG),
        ]}
        baked = _baked(collect.ndjson(collect.get("dishes"), {}, previous, [], edits=True)[0])

        assert FAILED in baked["手がかりなし"]["tags"]
        assert FAILED in baked["引き当てできず"]["tags"]
        assert FAILED not in baked["無かった"]["tags"] and NONE in baked["無かった"]["tags"]
        assert FAILED not in baked["決まった"]["tags"] and NONE not in baked["決まった"]["tags"]
        assert FAILED not in baked["消えた"]["tags"]

    def test_without_the_options_nothing_changes(self, enabled):
        spec = resolve.normalize({**RESOLVE, "failed_when_missing": False, "none_tag": ""})
        doc = {"title": "手がかりなし", "tags": ["地域:東京都"], "extra": {}}
        assert resolve.settle(spec, doc) is doc

    def test_the_default_is_failures_only(self):
        spec = resolve.normalize({k: v for k, v in RESOLVE.items()
                                  if k not in ("failed_when_missing", "none_tag")})
        assert spec["failed_when_missing"] is False and spec["none_tag"] == ""
        items, _ = resolve.apply(spec, [{"title": "料理", "body": "説明", "tags": ["地域:東京都"],
                                         "extra": {}}], {}, {}, "2026-10-17T00:00:00Z")
        assert items[0]["tags"] == ["地域:東京都"]

    def test_null_with_the_none_tag_is_no_failure(self):
        spec = resolve.normalize(RESOLVE)
        before = {"shop": {"name": "店", "source": "osm_japan", "resolved_at": "2026-10-01T00:00:00Z"}}
        items, report = resolve.apply(spec, [{
            "title": "料理", "body": "閉店して代わりが無い", "tags": ["地域:東京都", FAILED, NONE],
            "extra": {"shop": None},
        }], {"料理": before}, {}, "2026-10-17T00:00:00Z")
        assert items[0]["tags"] == ["地域:東京都", NONE]
        assert report["cleared"] == 1

    def test_null_without_the_none_tag_is_still_waiting(self):
        spec = resolve.normalize(RESOLVE)
        items, _ = resolve.apply(spec, [{
            "title": "料理", "body": "選び直す", "tags": ["地域:東京都"], "extra": {"shop": None},
        }], {}, {}, "2026-10-17T00:00:00Z")
        assert items[0]["tags"] == ["地域:東京都", FAILED]

    def test_the_spec_round_trips(self):
        spec = resolve.normalize(RESOLVE)
        assert resolve.normalize(resolve.to_json(spec)) == spec
        assert resolve.to_json(spec)["failed_when_missing"] is True

    @pytest.mark.parametrize("raw", [
        {**RESOLVE, "failed_tag": ""},
        {**RESOLVE, "none_tag": FAILED},
        {**RESOLVE, "none_tag": "_chiezo_x"},
    ])
    def test_unreadable_specs_are_refused(self, raw):
        with pytest.raises(HTTPException):
            resolve.normalize(raw)

    def test_the_ai_is_told_how_to_say_none(self, dishes):
        system = collect.build_messages(collect.get("dishes"), {})[0]["content"]
        assert f"「{NONE}」" in system


class TestFollowingPartitionByPartition:
    """`after_each` —— 前の巡回の一周を待たず、見終えた区画から回る。"""

    def _sweeps(self, after_each: bool):
        return [
            {"name": SWEEP, "interval_minutes": 60, "partitions_per_run": 1, "hold_tag": FAILED,
             "one_lap": True},
            {"name": "情報収集", "interval_minutes": 60, "partitions_per_run": 1, "after": SWEEP,
             "after_each": after_each, "only_new": True},
        ]

    def test_it_runs_once_one_partition_is_seen(self, dishes):
        collect.update("dishes", sweeps=self._sweeps(True), partitions=[
            {"key": "地域:東京都", "count": 3},
            {"key": "地域:大阪府", "count": 1, "visits": {SWEEP: "2026-10-01T00:00:00+00:00"}},
        ])
        item = collect.get("dishes")
        _tidy, survey = collect.sweeps_of(item)
        assert survey.after_each
        assert collect.waited_for(item, survey)
        assert collect.blocked_reason(item, survey) == ""
        # 配るのは前の巡回が見終えた区画だけ(東京都はまだ足止めされている)
        spec = partitioning.normalize(item.partition)
        seen_by = collect.picks_after(item, survey)
        assert seen_by == SWEEP
        assert partitioning.pick(item.partitions, survey.name, 2, spec, seen_by=seen_by) == [
            "地域:大阪府"
        ]

    def test_nothing_seen_waits(self, dishes):
        collect.update("dishes", sweeps=self._sweeps(True), partitions=[
            {"key": "地域:東京都", "count": 3}, {"key": "地域:大阪府", "count": 1},
        ])
        item = collect.get("dishes")
        _tidy, survey = collect.sweeps_of(item)
        assert not collect.waited_for(item, survey)
        assert collect.blocked_reason(item, survey) == f"「{SWEEP}」が見終えた区画待ち"

    def test_an_empty_pick_is_refused_rather_than_taking_everything(self, dishes):
        from app import main

        collect.update("dishes", sweeps=self._sweeps(True), partitions=[
            {"key": "地域:東京都", "count": 3}, {"key": "地域:大阪府", "count": 1},
        ])
        item = collect.get("dishes")
        _tidy, survey = collect.sweeps_of(item)
        with pytest.raises(HTTPException) as caught:
            main._nothing_seen_yet(item, survey, [])
        assert caught.value.status_code == 409

    def test_without_the_flag_it_still_waits_for_the_whole_lap(self, dishes):
        collect.update("dishes", sweeps=self._sweeps(False), partitions=[
            {"key": "地域:東京都", "count": 3},
            {"key": "地域:大阪府", "count": 1, "visits": {SWEEP: "2026-10-01T00:00:00+00:00"}},
        ])
        item = collect.get("dishes")
        _tidy, survey = collect.sweeps_of(item)
        assert not collect.waited_for(item, survey)
        assert collect.picks_after(item, survey) == ""


class TestHoldingAtMostAFewTimes:
    """続けて足止めするのは `MAX_HOLDS` 回まで(1 回に 1 件ずつしか片付けない相手で、
    同じ区画に何回でも戻るのを止める)。"""

    def test_the_count_goes_up_and_stops_at_the_limit(self):
        ledger = [{"key": "地域:東京都"}, {"key": "地域:大阪府"}]
        for _ in range(partitioning.MAX_HOLDS):
            assert partitioning.within_hold_limit(ledger, ["地域:東京都"], SWEEP) == ["地域:東京都"]
            ledger = partitioning.note_held(ledger, ["地域:東京都"], SWEEP)
        assert partitioning.within_hold_limit(ledger, ["地域:東京都"], SWEEP) == []
        # 巡回ごとに数える
        assert partitioning.within_hold_limit(ledger, ["地域:東京都"], "情報収集") == ["地域:東京都"]

    def test_a_visited_partition_counts_from_zero_again(self, dishes):
        collect.update("dishes", partitions=[
            {"key": "地域:東京都", "count": 3, "held": {SWEEP: 2}}, {"key": "地域:大阪府", "count": 1},
        ])
        collect.record_result("dishes", status="ok", sweep=SWEEP, visited=["地域:東京都"])
        tokyo = next(p for p in collect.get("dishes").partitions if p["key"] == "地域:東京都")
        assert "held" not in tokyo
        assert tokyo["visits"][SWEEP]

    def test_a_held_run_counts_up(self, dishes):
        collect.record_result("dishes", status="ok", sweep=SWEEP, held=["地域:東京都"])
        tokyo = next(p for p in collect.get("dishes").partitions if p["key"] == "地域:東京都")
        assert tokyo["held"] == {SWEEP: 1}
        assert not (tokyo.get("visits") or {}).get(SWEEP)

    def test_a_held_run_takes_off_an_earlier_mark(self, dishes):
        """前の回で付いた印は、足止めした回で外す(一周で止まる巡回がその区画に戻れるように)。
        ほかの巡回の印は残す。"""
        collect.update("dishes", sweeps=[
            {"name": SWEEP, "interval_minutes": 60, "partitions_per_run": 1, "hold_tag": FAILED},
            {"name": "情報収集", "interval_minutes": 60, "partitions_per_run": 1},
        ], partitions=[
            {"key": "地域:東京都", "count": 3,
             "visits": {SWEEP: "2026-10-01T00:00:00+00:00", "情報収集": "2026-10-02T00:00:00+00:00"}},
            {"key": "地域:大阪府", "count": 1},
        ])
        collect.record_result("dishes", status="ok", sweep=SWEEP, held=["地域:東京都"])
        tokyo = next(p for p in collect.get("dishes").partitions if p["key"] == "地域:東京都")
        assert SWEEP not in tokyo["visits"]
        assert tokyo["visits"]["情報収集"] == "2026-10-02T00:00:00+00:00"
