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

    def test_it_keeps_how_long_it_took(self, state_env):
        """**遅くなったことは件数からは読めない。**

        同じ件数を返していても、5 分が 20 分になっていれば一周の見込みが 4 倍ずれる。
        """
        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF, ms=372_000)
        collect_log.record("spots", status=collect_log.STATUS_ERROR, error="切れた", ms=900_000)

        rows = collect_log.recent()

        assert [r["ms"] for r in rows] == [900_000, 372_000], "失敗した回も測る"

    def test_a_run_that_was_not_measured_says_nothing(self, state_env):
        """測っていない古い行は空のまま(0 秒と混ぜない)。"""
        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF)

        assert collect_log.recent()[0]["ms"] is None

    def test_the_screen_shows_how_long_it_took(self, state_env):
        from app.views import admin

        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF, ms=372_000)

        html = admin._collect_changes_html(name="spots")

        assert "6 分 12 秒" in html

    def test_the_screen_leaves_an_unmeasured_run_blank(self, state_env):
        from app.views import admin

        collect_log.record("spots", status=collect_log.STATUS_OK, diff=DIFF)

        assert "ミリ秒" not in admin._collect_changes_html(name="spots")

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


class TestReadingOneSweepOnly:
    """回ごとの間隔は桁違いなので、新しい順に並べるだけでは短い回が長い回を押し流す。"""

    def _three_sweeps(self) -> None:
        for _ in range(4):
            collect_log.record(
                "news", status=collect_log.STATUS_OK, diff={"added": 1}, sweep="見出し",
            )
        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 9}, sweep="整理",
        )
        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 1}, sweep="要約",
        )

    def test_the_long_sweep_survives_a_short_limit(self, state_env):
        """1 時間ごとの回に押し流されると、4 時間ごとの回の差分が読めない。"""
        self._three_sweeps()

        assert [r["sweep"] for r in collect_log.recent("news", limit=2)] == ["要約", "整理"]
        rows = collect_log.recent("news", limit=2, sweep="見出し")
        assert [r["sweep"] for r in rows] == ["見出し", "見出し"]

    def test_the_names_come_from_the_history(self, state_env):
        """絞り込みの選択肢は控えの側から引く —— 定義から作ると、名前を変えた回や
        消した回の行が目の前に出ているのに選べない。
        """
        self._three_sweeps()
        assert collect_log.sweeps("news") == ["要約", "整理", "見出し"]

    def test_another_collection_does_not_leak_in(self, state_env):
        self._three_sweeps()
        collect_log.record(
            "spots", status=collect_log.STATUS_OK, diff={"added": 1}, sweep="ざっと",
        )
        assert "ざっと" not in collect_log.sweeps("news")
        assert collect_log.sweeps() == ["ざっと", "要約", "整理", "見出し"]

    def test_a_sweep_that_never_ran_is_empty_not_everything(self, state_env):
        """絞ったのに全件が返ると、その回が走っていることになってしまう。"""
        self._three_sweeps()
        assert collect_log.recent("news", sweep="居ない回") == []

    def test_without_a_state_dir_there_are_no_choices(self, monkeypatch):
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        assert collect_log.sweeps() == []


class TestThePickerOnTheScreen:
    def test_it_offers_every_sweep_and_a_way_back(self, state_env):
        from app.views import admin

        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 1}, sweep="見出し",
        )
        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 9}, sweep="整理",
        )

        html = admin._collect_changes_html(name="news", sweep="整理")

        assert "/admin/collect/news?sweep=%E8%A6%8B%E5%87%BA%E3%81%97#changes" in html
        assert "/admin/collect/news#changes" in html, "すべてへ戻れる"
        assert "<strong>整理</strong>" in html, "いま見ている回は押せない"

    def test_one_sweep_alone_gets_no_picker(self, state_env):
        """選ぶ先が無いのに選択肢を出すと、押せるものを探して読まれる。"""
        from app.views import admin

        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 1}, sweep="見出し",
        )

        assert "どの回を見るか" not in admin._collect_changes_html(name="news")

    def test_an_empty_filter_says_which_emptiness_it_is(self, state_env):
        """「その回はまだ」と「1 回も走っていない」は別物。"""
        from app.views import admin

        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 1}, sweep="見出し",
        )
        collect_log.record(
            "news", status=collect_log.STATUS_OK, diff={"added": 9}, sweep="整理",
        )

        html = admin._collect_changes_html(name="news", sweep="居ない回")

        assert "その回はまだ走っていません" in html
        assert "どの回を見るか" in html, "戻る道を残す"
