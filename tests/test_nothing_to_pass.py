"""渡すものが無い回は AI を呼ばない(`collect.nothing_to_pass` / `main._skipped_for_nothing`)。

未読を読ませる回・新着をまとめる回は、増えたものが無ければ仕事が無い。それでも
頼んでいた頃は、「(まだ目を通していないものはありません)」と書いた依頼文で AI を
1 回呼び、取り込みを 1 本流し、ワーカーの待ち行列の順番も 1 つ使っていた。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app import collect, collect_log, notes, workers
from app.registry import Source

NOW = "2026-09-27T00:00:00+00:00"


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    # 取り込みを起こせない構成では収集そのものが成り立たない(時計も回らない)
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://trigger.invalid")
    return tmp_path


def _baked(tmp_path: Path, name: str, docs: list[dict]) -> dict:
    """焼き上がった収集の代わり(コアスキーマ)。"""
    path = tmp_path / f"{name}.db"
    conn = sqlite3.connect(path)
    conn.executescript(notes.SCHEMA_DDL)
    for doc_id, doc in enumerate(docs, start=1):
        conn.execute(
            "INSERT INTO docs (doc_id, title, opening, body, tags, extra, links,"
            " updated_at, rank_score) VALUES (?, ?, '', '', ?, '{}', '[]', ?, 0)",
            (doc_id, doc["title"], json.dumps(doc.get("tags", []), ensure_ascii=False),
             doc.get("updated_at", NOW)),
        )
        for tag in doc.get("tags", []):
            conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id))
    conn.commit()
    conn.close()
    return {name: Source(
        name=name, kind="collect", lang="ja", dump_date="20260101", schema_version=4,
        built_at=NOW, doc_count=len(docs), path=path,
    )}


def _made(prompt: str, **sweep) -> tuple:
    collect.create("posts", prompt=prompt, interval_minutes=60)
    collect.update("posts", enabled=True, sweeps=[{"name": "整理", **sweep}])
    item = collect.get("posts")
    return item, collect.sweeps_of(item)[0]


class TestWhenThereIsNothingToPass:
    def test_no_unreviewed_means_nothing_to_pass(self, state):
        item, sweep = _made("読んでタグを付けて {unreviewed}")
        sources = _baked(state, "posts", [{"title": "読み終えた記事"}])

        reason = collect.nothing_to_pass(item, sweep, sources)

        assert "まだ目を通していないもの" in reason

    def test_one_unreviewed_is_enough(self, state):
        item, sweep = _made("読んでタグを付けて {unreviewed}")
        sources = _baked(state, "posts", [{"title": "新しい記事", "tags": [notes.UNREVIEWED_TAG]}])

        assert collect.nothing_to_pass(item, sweep, sources) == ""

    def test_a_removed_unreviewed_one_does_not_count(self, state):
        """消えたものは目を通す相手ではない(`render_unreviewed` も出さない)。"""
        item, sweep = _made("読んでタグを付けて {unreviewed}")
        sources = _baked(state, "posts", [
            {"title": "消えた記事", "tags": [notes.UNREVIEWED_TAG, notes.REMOVED_TAG]},
        ])

        assert collect.nothing_to_pass(item, sweep, sources) != ""

    def test_nothing_new_since_the_last_run(self, state):
        item, sweep = _made(
            "まとめて {recent}", last_run_at="2026-09-27T01:00:00+00:00",
        )
        sources = _baked(state, "posts", [{"title": "古い記事", "updated_at": NOW}])

        assert "前回から新しく入ったもの" in collect.nothing_to_pass(item, sweep, sources)

    def test_something_new_since_the_last_run(self, state):
        item, sweep = _made("まとめて {recent}", last_run_at="2026-09-26T00:00:00+00:00")
        sources = _baked(state, "posts", [{"title": "新しい記事", "updated_at": NOW}])

        assert collect.nothing_to_pass(item, sweep, sources) == ""

    def test_a_prompt_with_other_work_is_not_skipped(self, state):
        """今あるもの全部を見る・外の道具の見出しを読む回は、未読が無くても仕事がある。"""
        sources = _baked(state, "posts", [{"title": "読み終えた記事"}])
        _made("読んで {unreviewed}")
        for other in ("{names}", "{current}", "{feed}", "{cursor}"):
            collect.update("posts", sweeps=[{"name": "整理", "prompt": f"畳んで {other} 調べて {{unreviewed}}"}])
            item = collect.get("posts")
            sweep = collect.sweeps_of(item)[0]
            assert collect.nothing_to_pass(item, sweep, sources) == "", other

    def test_a_prompt_without_deltas_is_not_skipped(self, state):
        item, sweep = _made("新しいものを集めて")

        assert collect.nothing_to_pass(item, sweep, {}) == ""

    def test_a_sweep_that_calls_no_ai_is_not_looked_at(self, state):
        item, sweep = _made("読んで {unreviewed}", use_extract=True, only_new=True)

        assert collect.nothing_to_pass(item, sweep, {}) == ""


class TestTheClockSkipsIt:
    @pytest.fixture
    def live(self, state, monkeypatch):
        from app import main

        sources = _baked(state, "posts", [{"title": "読み終えた記事"}])
        monkeypatch.setattr(main.app.state, "sources", sources, raising=False)
        return sources

    def test_a_worker_sweep_is_not_queued_and_counts_as_run(self, live):
        from app import main

        workers.save(workers.merged([], "", "整理用", (workers.Step("codex"),)))
        worker = workers.load()[0]
        _made("読んでタグを付けて {unreviewed}", worker=worker.key)

        main._fill_worker_queues()

        assert workers.queued(worker.key) == []
        sweep = collect.sweeps_of(collect.get("posts"))[0]
        assert sweep.last_run_at, "走ったことにしないと、毎周積み直す"
        assert sweep.last_status == "ok"
        # 黙って飛ばさない(走っていないのと見分けが付かない)
        row = collect_log.recent("posts")[0]
        assert "AI を呼びませんでした" in row["error"]
        assert row["status"] == collect_log.STATUS_OK

    def test_a_queued_one_is_dropped_when_it_runs_dry(self, live):
        """積んだあとに別の回が未読を読み終えることがある。"""
        from app import main

        workers.save(workers.merged([], "", "整理用", (workers.Step("codex"),)))
        worker = workers.load()[0]
        _made("読んでタグを付けて {unreviewed}", worker=worker.key)
        workers.enqueue(worker.key, "posts", "整理", NOW)

        main._drop_stale_from_queues()

        assert workers.queued(worker.key) == []
