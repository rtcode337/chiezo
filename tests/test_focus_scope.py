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
        kept, dropped = collect.drop_unseen_edits(
            items, allowed={"見せた人"}, existing={"見せた人", "見せていない人"}
        )
        assert [d["title"] for d in kept] == ["見せた人", "新しい人"]
        assert dropped == ["見せていない人"]

    def test_a_new_one_passes_even_when_nothing_was_shown(self):
        """漏れを足す回の本分。居ない見出しは足すものなので通す。"""
        kept, dropped = collect.drop_unseen_edits(
            [{"title": "新しい人"}], allowed=set(), existing=set()
        )
        assert [d["title"] for d in kept] == ["新しい人"]
        assert dropped == []


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
