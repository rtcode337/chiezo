"""管理画面にしか無かった収集とワーカーの操作を、REST の口から頼む。

依頼元のアプリの画面だけで済むようにするための口。中身は管理画面のボタンと同じ
関数を呼ぶので、ここでは「口から届くか」と「断る場合を書き分けているか」を見る。
"""
from __future__ import annotations

import pytest

from app import collect, repartition_job, search_queries, workers
from app import partition as partitioning


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
def made(enabled):
    collect.create("news", prompt="{cursor}", interval_minutes=60)
    collect.update(
        "news",
        sweeps=[{"name": "ざっと"}, {"name": "整理"}],
        partitions=[{"key": "あ", "count": 1}, {"key": "い", "count": 1}],
    )
    return collect.get("news")


@pytest.fixture
def client(enabled, built_data_dir, monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestRedo:
    def test_the_last_run_is_rewound_and_run_again(self, client, made, monkeypatch):
        from app import main

        collect.record_result("news", status="ok", sweep="ざっと", visited=["あ"], next_cursor="b")
        ran = []
        monkeypatch.setattr(main, "run_or_queue", lambda name, sweep, by="": ran.append(
            (name, sweep, by)) or {"queued": False})

        res = client.post("/v1/collect/news/redo", params={"manual": "true"})

        assert res.status_code == 200
        assert ran == [("news", "ざっと", "api")]
        assert partitioning.progress(collect.get("news").partitions, "ざっと") == (0, 2)

    def test_nothing_to_redo_is_409(self, client, made):
        assert client.post("/v1/collect/news/redo", params={"manual": "true"}).status_code == 409

    def test_a_stopped_collection_needs_a_person(self, client, made):
        """定時の頼みで AI を動かさないための約束は `run` と同じ。"""
        collect.record_result("news", status="ok", sweep="ざっと", visited=["あ"], next_cursor="b")
        assert client.post("/v1/collect/news/redo").status_code == 403


class TestRestartAndRequeue:
    def test_the_lap_starts_over_without_running(self, client, made):
        collect.record_result("news", status="ok", sweep="ざっと", visited=["あ", "い"])

        res = client.post("/v1/collect/news/restart", params={"sweep": "ざっと"})

        assert res.status_code == 200
        assert partitioning.progress(collect.get("news").partitions, "ざっと") == (0, 2)
        assert "partitions" not in res.json()

    def test_an_unknown_sweep_is_refused(self, client, made):
        res = client.post("/v1/collect/news/restart", params={"sweep": "いない"})
        assert res.status_code >= 400

    def test_requeue_is_asked_for_the_next_bake(self, client, made):
        res = client.post("/v1/collect/news/requeue")

        assert res.status_code == 200
        assert collect.get("news").requeue_at
        assert res.json()["requeue_at"]


class TestRepartition:
    def test_it_starts_in_the_background_and_says_so(self, client, made, monkeypatch):
        from app.views import admin

        monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: None)
        monkeypatch.setattr(repartition_job, "run", lambda name, sources: None)

        res = client.post("/v1/collect/news/repartition")

        assert res.status_code == 202
        assert res.json()["repartition"]["state"] == "running"
        assert client.get("/v1/collect/news").json()["repartition"]["state"] == "running"
        # 二度押しは断る(2 本が同じ母集団を読むと、メモリも時間も倍になる)
        assert client.post("/v1/collect/news/repartition").status_code == 409


class TestStop:
    def test_the_ingest_of_that_collection_is_stopped(self, client, made, monkeypatch):
        from app.views import admin

        asked = []
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.internal")

        class Ok:
            status_code = 200

        monkeypatch.setattr(admin.httpx, "post", lambda url, params=None, timeout=None: asked.append(
            (url, params)) or Ok())

        assert client.post("/v1/collect/news/stop").status_code == 200
        assert asked == [("http://trigger.internal/stop", {"source": "news"})]

    def test_an_unknown_collection_is_404(self, client, made):
        assert client.post("/v1/collect/nope/stop").status_code == 404


class TestQueries:
    def test_the_ledger_is_read_and_forgotten(self, client, made):
        search_queries.record("news", ["技術書", "SRE 本"], "codex")

        got = client.get("/v1/collect/news/queries").json()
        assert [r["query"] for r in got["queries"]] == ["技術書", "SRE 本"]

        res = client.delete("/v1/collect/news/queries", params={"query": "SRE 本"})
        assert res.json() == {"ok": True, "forgot": 1}
        assert [r["query"] for r in search_queries.history("news")] == ["技術書"]

        client.delete("/v1/collect/news/queries")
        assert search_queries.history("news") == []


class TestWorkers:
    def _made(self) -> workers.Worker:
        workers.save(workers.merged([], "", "精査", (workers.Step("codex"),)))
        return workers.load()[0]

    def test_listed_paused_and_resumed(self, client, enabled):
        worker = self._made()

        [listed] = client.get("/v1/ai/workers").json()["workers"]
        assert listed["id"] == worker.key and listed["paused"] is False and listed["queued"] == 0

        res = client.post(f"/v1/ai/workers/{worker.key}/paused", json={"paused": True})
        assert res.status_code == 200 and res.json()["paused"] is True
        assert workers.get(worker.key).paused

        client.post(f"/v1/ai/workers/{worker.key}/paused", json={"paused": False})
        assert not workers.get(worker.key).paused

    def test_an_unknown_worker_is_404(self, client, enabled):
        assert client.post("/v1/ai/workers/nope/paused", json={"paused": True}).status_code == 404
        assert client.post("/v1/ai/workers/nope/wake").status_code == 404

    def test_waking_with_nothing_queued_says_why(self, client, enabled):
        worker = self._made()

        res = client.post(f"/v1/ai/workers/{worker.key}/wake")

        assert res.status_code == 409
        assert "待ち行列は空" in res.json()["error"]
