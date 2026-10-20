"""割り込みと区画で絞った直す回が、**見せていない中身を置き換えない**ことのテスト。

割り直しで消えた区画を名指しした割り込みが、差し込みの空のまま AI を呼び、
返りが材料を見ないまま既存の 1 件を丸ごと置き換えた(機械で引いたタグが消えた)。
塞いでいるのは 2 か所 —— 台帳に無い区画では走らせない、見せていない既存の
1 件は置き換えない。
"""
from __future__ import annotations

import pytest

from app import collect


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
def ready(enabled):
    collect.create("news", prompt="{cursor} 以降", interval_minutes=60)
    collect.update(
        "news",
        enabled=True,
        partition={"by": "title", "target": 100},
        partitions=[{"key": k, "count": 1} for k in ["A", "B"]],
    )
    return collect.get("news")


class TestMissingPartition:
    def test_a_key_the_ledger_no_longer_has_is_reported(self, ready):
        """割り直すと鍵は消える。押した時点の台帳で作った鍵でも起きる。"""
        focus = collect.normalize_focus({"note": "直して", "partition": "C"})
        assert collect.missing_focus_partition(focus, ready.partitions) == "C"

    def test_a_key_on_the_ledger_passes(self, ready):
        focus = collect.normalize_focus({"note": "直して", "partition": "A"})
        assert collect.missing_focus_partition(focus, ready.partitions) is None

    def test_a_focus_without_a_partition_passes(self, ready):
        """名指しだけの割り込みは区画を持たない(対象は名指しされたもの)。"""
        focus = collect.normalize_focus({"note": "直して", "titles": ["X"]})
        assert collect.missing_focus_partition(focus, ready.partitions) is None
        assert collect.missing_focus_partition(None, ready.partitions) is None


class TestDropUnseenEdits:
    def test_an_existing_one_it_was_not_shown_is_not_replaced(self):
        """材料を見ないまま書いた 1 件で、いま付いているタグを消さない。"""
        items = [
            {"title": "見せた人", "tags": ["直した"]},
            {"title": "見せていない人", "tags": ["想像"]},
            {"title": "新しい人", "tags": ["足した"]},
        ]
        kept, dropped, renamed = collect.drop_unseen_edits(
            items, allowed={"見せた人"}, existing={"見せた人", "見せていない人"}
        )
        assert renamed == []
        assert [d["title"] for d in kept] == ["見せた人", "新しい人"]
        assert dropped == ["見せていない人"]

    def test_a_new_one_passes_even_when_nothing_was_shown(self):
        """漏れを足す回の本分。居ない見出しは足すものなので通す。"""
        kept, dropped, _renamed = collect.drop_unseen_edits(
            [{"title": "新しい人"}], allowed=set(), existing=set()
        )
        assert [d["title"] for d in kept] == ["新しい人"]
        assert dropped == []


    def test_a_same_name_far_away_is_added_with_a_number(self):
        """見せていない区画の同じ名前の別の店は、番号を付けて別の 1 件として足す。
        AI は自分の区画と隣しか見ていないので、遠くの同じ名前を知りようがない。"""
        existing = {"和": {"lat": 35.50883, "lon": 139.67551}, "和 (2)": {}}
        items = [{"title": "和", "body": "飲み屋", "lat": 35.50578, "lon": 139.67713}]

        kept, dropped, renamed = collect.drop_unseen_edits(
            items, allowed=set(), existing=existing, taken=lambda t: t == "和 (3)",
        )

        # 「和 (2)」は既にあり、「和 (3)」も使われているので「和 (4)」
        assert [d["title"] for d in kept] == ["和 (4)"]
        assert dropped == []
        assert renamed == [("和", "和 (4)")]

    def test_a_same_name_at_the_same_place_or_without_coords_is_still_dropped(self):
        """近い点(隣の区画の 1 件を書き写した)と、座標の分からない返りは今までどおり落とす。"""
        existing = {"和": {"lat": 35.50883, "lon": 139.67551}}
        near = [{"title": "和", "extra": {"lat": 35.50890, "lon": 139.67560}}]
        no_coords = [{"title": "和"}]

        for items in (near, no_coords):
            kept, dropped, renamed = collect.drop_unseen_edits(
                items, allowed=set(), existing=existing,
            )
            assert kept == [] and dropped == ["和"] and renamed == []


class TestFocusEndpoint:
    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_a_partition_the_ledger_does_not_have_is_refused(self, client, ready, monkeypatch):
        """受け付けてしまうと、差し込みが空のまま AI を 1 回呼ぶ。"""
        from app.views import admin

        called = []
        monkeypatch.setattr(admin, "trigger_run", called.append)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")

        res = client.post("/v1/collect/news/focus", json={"note": "直して", "partition": "C"})
        assert res.status_code == 409
        assert "台帳にありません" in res.json()["error"]
        # 控えてもいない(残すと次の回を乗っ取る)し、起こしてもいない
        assert collect.get("news").pending_focus is None
        assert called == []

    def test_a_partition_on_the_ledger_is_taken(self, client, ready, monkeypatch):
        from app.views import admin

        monkeypatch.setattr(admin, "trigger_run", lambda _source: None)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: {})

        res = client.post("/v1/collect/news/focus", json={"note": "直して", "partition": "A"})
        assert res.status_code == 200
        assert collect.get("news").pending_focus["partition"] == "A"


class TestRunNamedPartitions:
    """区画を名指しした 1 回(`POST /v1/collect/{name}/run` の `once.partitions`)。"""

    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_missing_names_in_written_order(self):
        ledger = [{"key": "A"}, {"key": "B"}]
        assert collect.missing_partitions(["C", "A", "D"], ledger) == ["C", "D"]
        assert collect.missing_partitions(["A"], ledger) == []
        assert collect.missing_partitions(None, ledger) == []

    def test_a_partition_the_ledger_does_not_have_is_refused_when_taken(
        self, client, ready, monkeypatch
    ):
        """走らせるときに断っても、頼んだ側は受け付けの 200 で通ったと読む。"""
        from app.views import admin

        called = []
        monkeypatch.setattr(admin, "trigger_run", called.append)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: {})

        res = client.post("/v1/collect/news/run?manual=true", json={"partitions": ["C"]})
        assert res.status_code == 409
        assert "台帳にありません" in res.json()["error"]
        assert collect.get("news").pending_run is None
        assert called == []

    def test_it_is_refused_rather_than_queued_when_ingest_is_busy(
        self, client, ready, monkeypatch
    ):
        """埋まっているときの 409 は並べる合図なので、それより前で断る。"""
        from app import ingest_queue
        from app.views import admin

        monkeypatch.setattr(admin, "trigger_run", lambda _name: None)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: {})
        monkeypatch.setattr(ingest_queue, "is_full", lambda _status: True)

        res = client.post("/v1/collect/news/run?manual=true", json={"partitions": ["C"]})
        assert res.status_code == 409
        assert ingest_queue.waiting() == []

    def test_a_partition_on_the_ledger_is_taken(self, client, ready, monkeypatch):
        from app.views import admin

        called = []
        monkeypatch.setattr(admin, "trigger_run", called.append)
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://chiezo-trigger:7011")
        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: {})

        res = client.post("/v1/collect/news/run?manual=true", json={"partitions": ["A"]})
        assert res.status_code == 200
        assert collect.get("news").pending_run["partitions"] == ["A"]
        assert called == ["news"]
