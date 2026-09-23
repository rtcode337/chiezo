"""区画の割り直しを、押した人を待たせずに走らせる(`app/repartition_job.py`)。

**HTTP の裏に置いたままにできない仕事だった。** 母集団を丸ごと 1 周舐めるので、
本番の食事処(686,602 件)では押すとブラウザが先に切れる —— 処理自体は裏で最後まで
走るのに、押した人からは「死んだ」ように見えていた。
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app import collect, machine_store, repartition_job


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """置き場を用意する(`tests/test_collect.py` の同名のものと同じ段取り)。

    **共有のファイルに寄せない** —— あちらは別のセッションと分け合っていて、
    こちらの都合で触ると衝突する。30 行の重複より、そちらのほうが高い。
    """
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    return tmp_path


@pytest.fixture
def baked(tmp_path):
    """焼き上がった長期記憶の代わり(コアスキーマの読み取り専用 DB)。"""
    import sqlite3

    from app import notes

    def make(docs, name="news"):
        path = tmp_path / f"baked_{name}.db"
        conn = sqlite3.connect(path)
        conn.executescript(notes.SCHEMA_DDL)
        for i, (title, body) in enumerate(docs, start=1):
            conn.execute(
                "INSERT INTO docs (doc_id, title, opening, body, tags, updated_at,"
                " rank_score) VALUES (?, ?, ?, ?, '[]', '2026-01-01T00:00:00+00:00', 0.0)",
                (i, title, body, body),
            )
        conn.commit()
        conn.close()

        class Src:
            def __init__(self, path):
                self.path = path
                self.dump_date = "20260101000000"
                self.schema_version = notes.SCHEMA_VERSION

        return {name: Src(path)}

    return make


@pytest.fixture
def ready(enabled):
    collect.create("news", prompt="{cursor}", interval_minutes=60)
    collect.update("news", partition={"by": "title", "target": 10})
    return collect.get("news")


class TestWhatTheScreenCanRead:
    def test_nothing_pressed_means_nothing_to_show(self, ready):
        assert repartition_job.state("news") == {}
        assert not repartition_job.running("news")

    def test_it_says_it_is_running(self, ready):
        repartition_job.start("news")

        assert repartition_job.running("news")
        assert repartition_job.state("news")["state"] == "running"

    def test_it_keeps_how_many_partitions_it_made(self, ready):
        """**押したら何区画になったか**を、台帳を開かずに読めるように。"""
        repartition_job.start("news")
        repartition_job.finish("news", 8185)

        found = repartition_job.state("news")
        assert found["state"] == "done"
        assert found["partitions"] == 8185
        assert not repartition_job.running("news")

    def test_it_keeps_why_it_failed(self, ready):
        """押した人はその場に居ないことがある(数分かかる)。"""
        repartition_job.start("news")
        repartition_job.fail("news", "母集団が読めません")

        found = repartition_job.state("news")
        assert found["state"] == "error"
        assert "母集団" in found["error"]

    def test_a_run_that_never_came_back_reads_as_failed(self, ready):
        """**面倒を見ているワーカーが消えても控えは残る** —— 印が無いと永遠に
        走っているように見えて、二度と押せなくなる。"""
        stale = datetime.now(UTC) - repartition_job.STALE_AFTER - timedelta(minutes=1)
        machine_store.put(
            repartition_job.KIND, repartition_job.KEY,
            json.dumps({"news": {"state": "running",
                                 "started_at": stale.isoformat(timespec="seconds")}}),
        )

        assert repartition_job.state("news")["state"] == "error"
        assert not repartition_job.running("news")

    def test_a_broken_body_starts_over(self, ready):
        """控えは次に押せば作り直せる(行列と同じ判断)。"""
        machine_store.put(repartition_job.KIND, repartition_job.KEY, "{壊れている")

        assert repartition_job.state("news") == {}

    def test_one_collection_does_not_read_another(self, ready):
        repartition_job.start("news")

        assert repartition_job.state("ほか") == {}


class TestRunningIt:
    def test_it_records_the_result(self, ready, baked):
        sources = baked([(f"見出し{i:03}", "本文") for i in range(40)], name="news")

        repartition_job.run("news", sources)

        found = repartition_job.state("news")
        assert found["state"] == "done"
        assert found["partitions"] == len(collect.get("news").partitions)

    def test_a_failure_lands_in_the_state_not_the_caller(self, ready):
        """**例外をそのまま投げても誰も読まない** —— 受ける HTTP はもう返っている。
        画面に出す唯一の道が控えのほう。
        """
        # 区画を持たない収集 —— `repartition` は 400 で断る
        collect.create("flat", prompt="{cursor}", interval_minutes=60)

        repartition_job.run("flat", {})

        found = repartition_job.state("flat")
        assert found["state"] == "error"
        assert "区画" in found["error"]


class TestTheButton:
    @staticmethod
    def _request():
        from types import SimpleNamespace

        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(sources={})),
        )

    def test_pressing_it_twice_is_refused(self, ready, monkeypatch):
        """**2 本が同時に同じ母集団を読む** —— メモリも時間も倍になるうえ、
        後に終わったほうが勝つだけで早く終わりもしない。"""
        from fastapi import BackgroundTasks

        from app.views import admin

        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: None)
        repartition_job.start("news")

        with pytest.raises(HTTPException) as caught:
            admin.admin_collect_repartition("news", self._request(), BackgroundTasks())

        assert caught.value.status_code == 409

    def test_it_marks_running_before_it_returns(self, ready, monkeypatch):
        """**書く前に走らせると、戻った画面が「押していない」ように見える**
        (押した人はもう一度押す)。"""
        from fastapi import BackgroundTasks

        from app.views import admin

        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: None)
        tasks = BackgroundTasks()

        admin.admin_collect_repartition("news", self._request(), tasks)

        assert repartition_job.running("news")
        assert tasks.tasks, "裏で走らせる仕事が積まれていない"

    def test_the_screen_says_what_happened(self, ready):
        from app.views import admin

        repartition_job.start("news")
        repartition_job.finish("news", 8185)

        html = admin._repartition_state_html("news")

        assert "8,185 区画に割り直しました" in html

    def test_the_button_is_off_while_it_runs(self, ready):
        from app.views import admin

        repartition_job.start("news")

        html = admin._repartition_form(collect.get("news"), busy=False)

        assert "disabled" in html
        assert "割り直しています" in html
