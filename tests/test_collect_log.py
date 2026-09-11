"""収集が 1 回走るたびの変更履歴(`app/collect_log.py` / `/v1/collect/changes`)。

定義側の控え(`last_added` など)は最新の 1 回で上書きされるので、6 時間ごとに
回る収集なら朝には昨夜の 1 回しか残っていない。「減り続けているのか、ある日だけ
荒れたのか」はここでしか読めない。
"""
from __future__ import annotations

import pytest

from app import collect_log


@pytest.fixture()
def state_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return monkeypatch


DIFF = {
    "total": 42,
    "added": 3,
    "updated": 2,
    "removed": 1,
    "skipped": 5,
    "added_titles": ["足したもの"],
    "updated_titles": ["直したもの"],
    "removed_titles": ["消したもの"],
}


class TestRecord:
    def test_it_keeps_what_moved_not_just_how_many(self, state_env):
        """件数だけでは、プロンプトを直せるかどうかが変わる。"""
        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF)
        rows = collect_log.recent()
        assert len(rows) == 1
        assert rows[0]["name"] == "spots"
        assert (rows[0]["added"], rows[0]["updated"], rows[0]["removed"]) == (3, 2, 1)
        assert rows[0]["total"] == 42
        assert rows[0]["added_titles"] == ["足したもの"]
        assert rows[0]["updated_titles"] == ["直したもの"]
        assert rows[0]["removed_titles"] == ["消したもの"]

    def test_a_failure_is_a_row_too(self, state_env):
        """「走ったが何も入らなかった」と「走っていない」は別物。

        成功だけ残すと、両方が同じ空白に見える。
        """
        collect_log.record("spots", status=collect_log.STATUS_ERROR, error="llm error 502")
        rows = collect_log.recent()
        assert rows[0]["status"] == collect_log.STATUS_ERROR
        assert rows[0]["error"] == "llm error 502"
        assert rows[0]["added"] == 0

    def test_it_returns_the_newest_first(self, state_env):
        for i in range(3):
            collect_log.record("spots", status=collect_log.STATUS_OK, diff={"added": i})
        assert [r["added"] for r in collect_log.recent()] == [2, 1, 0]

    def test_it_can_be_read_for_one_collection(self, state_env):
        collect_log.record("spots", status=collect_log.STATUS_OK, diff={"added": 1})
        collect_log.record("news", status=collect_log.STATUS_OK, diff={"added": 2})
        assert [r["name"] for r in collect_log.recent("spots")] == ["spots"]
        assert len(collect_log.recent()) == 2

    def test_it_only_keeps_the_head_of_the_headlines(self, state_env, monkeypatch):
        """初期構築の 1 回で数千件が並ぶ。読むのは手がかりで、全件の一覧ではない。"""
        monkeypatch.setattr(collect_log, "MAX_TITLES", 3)
        collect_log.record(
            "spots",
            status=collect_log.STATUS_OK,
            diff={"added_titles": [f"{i}" for i in range(10)]},
        )
        assert collect_log.recent()[0]["added_titles"] == ["0", "1", "2"]

    def test_it_does_not_grow_without_bound(self, state_env, monkeypatch):
        monkeypatch.setattr(collect_log, "MAX_ROWS", 5)
        for i in range(12):
            collect_log.record("spots", status=collect_log.STATUS_OK, diff={"added": i})
        rows = collect_log.recent(limit=100)
        assert len(rows) <= 5
        assert rows[0]["added"] == 11

    def test_it_is_off_without_a_state_dir(self, monkeypatch):
        """`CHIEZO_STATE_DIR` が機能フラグを兼ねる(設定・notes と同じ流儀)。"""
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF)
        assert collect_log.recent() == []
        assert collect_log.db_path() is None

    def test_a_broken_log_does_not_break_the_collection(self, state_env, monkeypatch):
        """履歴が取れないことと、集められないことは別の話。"""
        import sqlite3

        def boom(_path):
            raise sqlite3.Error("disk is full")

        monkeypatch.setattr(collect_log, "_connect", boom)
        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF)


class TestForget:
    def test_deleting_a_collection_drops_its_history(self, state_env):
        """名前がソース名なので、同じ名前で作り直すのは普通に起きる。

        残しておくと、前の収集が足した・消したものが新しい履歴に混ざって見える。
        """
        collect_log.record("spots", status=collect_log.STATUS_OK, diff={"added": 1})
        collect_log.record("news", status=collect_log.STATUS_OK, diff={"added": 2})
        collect_log.forget("spots")
        assert [r["name"] for r in collect_log.recent()] == ["news"]

    def test_forgetting_without_a_state_dir_is_fine(self, monkeypatch):
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        collect_log.forget("spots")
