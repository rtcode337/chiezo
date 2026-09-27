"""`{unreviewed}` に載せる数を巡回ごとに決める口と、全部を未確認に戻す口。

1 件ごとに調べものをさせる回は、載せたぶんを 1 回で調べ切れる数にしないと AI が
途中で手を止める(160 件あまりを渡した回が 18 件で終わった)。数を Chiezo が決めれば、
依頼文は「載せたものを全部」と頼むだけで済む。また、目を通した印は付いたら二度と
`{unreviewed}` に載らないので、依頼文を直したあとに初めから回し直す口が要る。
"""
from __future__ import annotations

import json

import pytest

from app import collect, notes

PROMPT = "まだ見ていないもの:\n{unreviewed}\n載せたものを全部調べて"


@pytest.fixture
def shelf(tmp_path, monkeypatch):
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    collect.create("shelf", prompt=PROMPT, interval_minutes=60)
    collect.update("shelf", sweeps=[{"name": "整理", "unreviewed_per_run": 2}])
    return collect.get("shelf")


def _doc(i, title, *tags):
    return {
        "doc_id": i, "title": title, "body": "説明", "tags": ["技術書", *tags],
        "extra": {"collected_at": f"2026-09-0{i}T00:00:00+00:00"},
    }


def _user(messages):
    return messages[1]["content"] if len(messages) > 1 else messages[0]["content"]


class TestPerRun:
    def test_only_the_oldest_ones_are_passed(self, shelf):
        previous = {
            d["title"]: d for d in (
                _doc(3, "本:c", notes.UNREVIEWED_TAG),
                _doc(1, "本:a", notes.UNREVIEWED_TAG),
                _doc(2, "本:b", notes.UNREVIEWED_TAG),
            )
        }
        seen: set[str] = set()
        messages = collect.build_messages(
            shelf, previous, sweep=collect.sweep_named(shelf, "整理"), seen=seen,
        )

        # 古い順に 2 件だけ。載せなかった c は印が残り、次の回に回る
        assert seen == {"本:a", "本:b"}
        text = _user(messages)
        assert "全 3 件。うち古いほうから 2 件だけ" in text
        assert "本:c" not in text

    def test_an_extra_key_puts_the_larger_ones_first(self, shelf):
        """よく勧められているものから片付ける。値の無いものは後ろ、同じ値なら古い順。"""
        collect.update("shelf", sweeps=[
            {"name": "整理", "unreviewed_per_run": 2, "unreviewed_by": "docs"},
        ])
        item = collect.get("shelf")
        docs = [
            _doc(1, "本:a", notes.UNREVIEWED_TAG),
            _doc(2, "本:b", notes.UNREVIEWED_TAG),
            _doc(3, "本:c", notes.UNREVIEWED_TAG),
            _doc(4, "本:d", notes.UNREVIEWED_TAG),
        ]
        docs[1]["extra"]["docs"] = 5
        docs[2]["extra"]["docs"] = "9"
        docs[3]["extra"]["docs"] = 9
        seen: set[str] = set()
        messages = collect.build_messages(
            item, {d["title"]: d for d in docs}, sweep=collect.sweep_named(item, "整理"), seen=seen,
        )

        assert seen == {"本:c", "本:d"}
        assert "うち「docs」の大きいほうから 2 件だけ" in _user(messages)

    def test_the_number_is_kept_in_the_definition(self, shelf):
        assert collect.sweep_named(shelf, "整理").unreviewed_per_run == 2
        assert collect.sweep_named(shelf, "整理").to_json()["unreviewed_per_run"] == 2


class TestRequeue:
    def test_everything_is_read_as_unreviewed_until_the_next_bake(self, shelf):
        """印はまだ付いていないが、頼んだあとの回は全部を未確認として読む。"""
        previous = {
            d["title"]: d for d in (
                _doc(1, "本:a"), _doc(2, "本:b"), _doc(3, "本:c", notes.REMOVED_TAG),
            )
        }
        item = collect.request_requeue("shelf")
        assert item.requeue_at
        assert collect.nothing_to_pass(item, collect.sweep_named(item, "整理"), {}) == ""

        seen: set[str] = set()
        collect.build_messages(item, previous, sweep=collect.sweep_named(item, "整理"), seen=seen)
        # 消えたものは読み直さない
        assert seen == {"本:a", "本:b"}

    def test_the_bake_marks_all_but_what_was_just_shown(self, shelf):
        rows = {
            d["title"]: d for d in (
                _doc(1, "本:a"), _doc(2, "本:b"), _doc(3, "本:c", notes.REMOVED_TAG),
            )
        }
        lines = list(collect.bake_lines(
            shelf, {}, rows, [], reviewed={"本:a"}, requeue=True,
        ))
        docs = {d["title"]: d for d in map(json.loads, lines[1:])}

        assert notes.UNREVIEWED_TAG not in docs["本:a"]["tags"]
        assert notes.UNREVIEWED_TAG in docs["本:b"]["tags"]
        assert notes.UNREVIEWED_TAG not in docs["本:c"]["tags"]

    def test_a_finished_run_clears_it_and_a_failed_bake_brings_it_back(self, shelf):
        asked = collect.request_requeue("shelf").requeue_at

        done = collect.record_result("shelf", status="ok", sweep="整理")
        assert done.requeue_at == ""
        assert done.last_undo["requeue_at"] == asked

        at = done.last_undo["at"]
        assert collect.rewind_failed_bake("shelf", "落ちた", at, at)
        assert collect.get("shelf").requeue_at == asked

    def test_a_failed_run_keeps_it(self, shelf):
        collect.request_requeue("shelf")
        assert collect.record_result("shelf", status="error", sweep="整理").requeue_at

    def test_a_focus_run_does_not_clear_it(self, shelf):
        """割り込みは控えを残さないので、焼くところで落ちても戻せない。"""
        collect.request_requeue("shelf")
        assert collect.record_result("shelf", status="ok", sweep="整理", focus=True).requeue_at


class TestAdmin:
    @pytest.fixture()
    def client(self, shelf, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_the_button_asks_and_then_says_it_is_asked(self, client):
        assert "全部を未確認に戻す</button>" in client.get("/admin/collect/shelf").text

        res = client.post("/admin/collect/shelf/requeue", follow_redirects=False)
        assert res.status_code == 303
        assert collect.get("shelf").requeue_at
        assert "全部を未確認に戻すよう頼んであります" in client.get("/admin/collect/shelf").text

    def test_the_sweep_form_carries_the_number(self, client):
        html = client.get("/admin/collect/shelf").text
        assert 'name="sweep_unreviewed_per_run"' in html
        assert 'value="2"' in html
        assert 'name="sweep_unreviewed_by"' in html
