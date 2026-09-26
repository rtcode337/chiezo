"""取り込みの待ち行列(`app/ingest_queue.py`)と、それを流す時計(`main._dispatch`)。

**埋まっているときに来た依頼を待たせる。** かつては断って「次の周期でもう一度
起こしに行く」だけだったので、待っているものがどこにも見えず、スキップされて
いるのか待っているのかが外から読めなかった。
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app import collect, ingest_queue, usage_store, workers


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


@pytest.fixture
def trigger(state, monkeypatch):
    """取り込み側の代わり。**起こしたものは走っている中に入る**(本物と同じ)。"""
    from app.views import admin

    status = {"state": "idle", "slots": 1, "jobs": []}
    started: list[str] = []

    def run(name: str) -> None:
        import fastapi

        if len(status["jobs"]) >= status["slots"]:
            raise fastapi.HTTPException(429, {"error": "all slots are busy"})
        status["jobs"].append({"source": name, "state": "running",
                               "started_at": datetime.now(UTC).isoformat()})
        started.append(name)

    monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.invalid")
    monkeypatch.setattr(admin, "_fetch_trigger_status", lambda: status)
    monkeypatch.setattr(admin, "trigger_run", run)
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(state / "notes"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://trigger.invalid")
    return status, started


def _collection(name: str, *sweeps: dict, enabled: bool = True) -> None:
    collect.create(name, prompt="{cursor}", interval_minutes=60)
    collect.update(name, enabled=enabled,
                   sweeps=list(sweeps) or [{"name": "ざっと", "interval_minutes": 60}])


def _finish(status: dict, name: str) -> None:
    status["jobs"] = [j for j in status["jobs"] if j["source"] != name]


def _later(minutes: int) -> datetime:
    return datetime.now(UTC) + timedelta(minutes=minutes)


class TestTheLine:
    def test_the_same_pair_is_not_queued_twice(self, state):
        """待っているあいだに予定がまた来ても、走るのは 1 回でよい。"""
        first, pos, added = ingest_queue.add("news", "ざっと", origin="schedule")
        again, pos2, added2 = ingest_queue.add("news", "ざっと", origin="schedule")

        assert (pos, added) == (1, True)
        assert (again["id"], pos2, added2) == (first["id"], 1, False)

    def test_named_partitions_are_kept_apart(self, state):
        """区画を名指しした依頼は、それぞれ違うところを見てほしいので畳まない。"""
        ingest_queue.add("meals", "情報収集", origin="manual", run_once={"partitions": ["aa"]})
        ingest_queue.add("meals", "情報収集", origin="manual", run_once={"partitions": ["bb"]})

        assert len(ingest_queue.waiting()) == 2

    def test_a_refused_one_goes_back_to_the_front(self, state):
        """**順番を飛ばさない。**"""
        a, _, _ = ingest_queue.add("aa", "s", origin="manual")
        ingest_queue.add("bb", "s", origin="manual")

        taken = ingest_queue.take(a["id"])
        ingest_queue.put_back(taken)

        assert [e["collection"] for e in ingest_queue.waiting()] == ["aa", "bb"]

    def test_taking_twice_gives_it_once(self, state):
        """もう 1 本の時計(`--workers 2`)が先に持っていったら None。"""
        a, _, _ = ingest_queue.add("aa", "s", origin="manual")

        assert ingest_queue.take(a["id"]) is not None
        assert ingest_queue.take(a["id"]) is None

    def test_running_it_another_way_drops_the_plain_wait(self, state):
        ingest_queue.add("news", "ざっと", origin="schedule")
        ingest_queue.add("news", "ざっと", origin="manual", run_once={"partitions": ["x"]})

        ingest_queue.drop("news", "ざっと")

        [left] = ingest_queue.waiting()
        assert left["run_once"] == {"partitions": ["x"]}

    def test_a_started_one_is_settled_once_it_is_gone(self, state):
        entry, _, _ = ingest_queue.add("news", "ざっと", origin="schedule")
        ingest_queue.started(ingest_queue.take(entry["id"]),
                             (datetime.now(UTC) - timedelta(minutes=5)).isoformat())

        still = ingest_queue.settle({"state": "running", "jobs": [{"source": "news"}]})
        done = ingest_queue.settle({"state": "idle", "jobs": []})

        assert still == []
        assert [e["collection"] for e in done] == ["news"]
        assert ingest_queue.running() == []

    def test_a_just_started_one_is_not_settled_yet(self, state):
        """**状態を読んだのが起こす前**だと、まだ載っていないことがある。"""
        entry, _, _ = ingest_queue.add("news", "ざっと", origin="schedule")
        ingest_queue.started(ingest_queue.take(entry["id"]))

        assert ingest_queue.settle({"state": "idle", "jobs": []}) == []

    def test_an_unreachable_trigger_settles_nothing(self, state):
        entry, _, _ = ingest_queue.add("news", "ざっと", origin="schedule")
        ingest_queue.started(ingest_queue.take(entry["id"]), "2020-01-01T00:00:00+00:00")

        assert ingest_queue.settle({"state": "unreachable"}) == []

    def test_an_old_trigger_is_read_as_one_slot(self):
        """古い trigger は 1 本ぶんの形しか返さない。"""
        old = {"state": "running", "source": "jawiki"}

        assert ingest_queue.jobs(old) == [old]
        assert ingest_queue.slots(old) == 1
        assert ingest_queue.is_full(old)
        assert ingest_queue.finished({"state": "error", "source": "x"})[0]["source"] == "x"


class TestFlowing:
    def test_only_as_many_as_there_are_slots(self, trigger):
        status, started = trigger
        status["slots"] = 2
        for name in ("aa", "bb", "cc"):
            _collection(name)
            ingest_queue.add(name, "ざっと", origin="manual")

        from app import main

        assert main._dispatch(status) == 2
        assert started == ["aa", "bb"]
        assert [e["collection"] for e in ingest_queue.waiting()] == ["cc"]

    def test_the_same_collection_waits_and_others_pass(self, trigger):
        """同じソースは 1 本ずつ。**後ろを先に流す**(待てば流れるので残す)。"""
        from app import main

        status, started = trigger
        status["slots"] = 2
        status["jobs"] = [{"source": "aa", "state": "running"}]
        _collection("aa")
        _collection("bb")
        ingest_queue.add("aa", "ざっと", origin="manual")
        ingest_queue.add("bb", "ざっと", origin="manual")

        main._dispatch(status)

        assert started == ["bb"]
        assert [e["collection"] for e in ingest_queue.waiting()] == ["aa"]

    def test_a_scheduled_one_that_is_no_longer_due_is_dropped(self, trigger):
        """待っているあいだに手で走らされれば予定は進んでいる —— 流すと 2 回走る。"""
        from app import main

        status, started = trigger
        _collection("news")
        collect.mark_started("news", "ざっと")  # 予定が先へ進んだ
        ingest_queue.add("news", "ざっと", origin="schedule")

        main._dispatch(status)

        assert started == []
        assert ingest_queue.waiting() == []

    def test_a_manual_one_runs_even_if_the_collection_is_stopped(self, trigger):
        """押した人の試し撃ちを、無人の側の判断で落とさない。"""
        from app import main

        status, started = trigger
        _collection("news", enabled=False)
        ingest_queue.add("news", "ざっと", origin="manual")

        main._dispatch(status)

        assert started == ["news"]

    def test_a_refusal_puts_it_back_in_front(self, trigger, monkeypatch):
        import fastapi

        from app import main
        from app.views import admin

        status, _started = trigger
        _collection("aa")
        _collection("bb")
        ingest_queue.add("aa", "ざっと", origin="manual")
        ingest_queue.add("bb", "ざっと", origin="manual")

        def busy(_name):
            raise fastapi.HTTPException(429, {"error": "all slots are busy"})

        monkeypatch.setattr(admin, "trigger_run", busy)

        main._dispatch(status)

        assert [e["collection"] for e in ingest_queue.waiting()] == ["aa", "bb"]


class TestRunOrQueue:
    def test_it_starts_when_there_is_room(self, trigger):
        from app import main

        _status, started = trigger
        _collection("news")

        got = main.run_or_queue("news", "ざっと")

        assert got["queued"] is False
        assert started == ["news"]

    def test_it_queues_when_full_and_runs_later(self, trigger):
        """**断らずに並べる。** 空いた周で、名指しした区画のまま流れる。"""
        from app import main

        status, started = trigger
        status["jobs"] = [{"source": "jawiki", "state": "running"}]
        _collection("news")

        got = main.run_or_queue("news", "ざっと", {"note": "ここを見て"}, by="admin")

        assert got["queued"] is True and got["position"] == 1
        assert started == []

        _finish(status, "jawiki")
        main._dispatch(status)

        assert started == ["news"]
        assert collect.get("news").pending_run == {"note": "ここを見て"}

    def test_the_rest_endpoint_says_202(self, trigger, enabled_collect, built_data_dir,
                                        monkeypatch):
        from fastapi.testclient import TestClient

        status, _started = trigger
        status["jobs"] = [{"source": "jawiki", "state": "running"}]
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        _collection("news")
        with TestClient(app) as client:
            res = client.post("/v1/collect/news/run")

        assert res.status_code == 202
        assert res.json()["queued"] is True


@pytest.fixture
def enabled_collect(state, monkeypatch):
    """REST を通すときは、追記される DB を mutable に登録しておく(`test_collect` と同じ)。"""
    from app import db

    notes_dir = state / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    db.set_mutable_paths([notes_dir / "notes.db"])
    return notes_dir


class TestWorkersRestAfterFinishing:
    """**間隔は流し終えてから数える。** 起きた時刻から数えていた頃は、塊を流すのに
    間隔より長くかかると流し終えた瞬間に次が始まり、いつも何かが走っていた。
    """

    @pytest.fixture
    def ready(self, trigger):
        status, started = trigger
        usage_store.save_quota("codex", [{"id": "primary", "label": "直近 5 時間",
                                          "used_percent": 10.0}])
        workers.save([workers.Worker("精査", (workers.Step("codex"),),
                                     interval_minutes=30, per_run=2)])
        for name in ("aa", "bb", "cc"):
            _collection(name, {"name": "ざっと", "worker": "精査", "interval_minutes": 60})
        return status, started

    def _tick(self, status, now):
        from app import main

        ingest_queue.settle(status, now)
        main._advance_workers(now)
        main._dispatch(status, now)

    def test_one_at_a_time_even_with_room(self, ready):
        """**1 つのワーカーから出るのは 1 本ずつ** —— 同じ相手へ同時に 2 本投げない。"""
        from app import main

        status, started = ready
        status["slots"] = 3
        main._fill_worker_queues()

        self._tick(status, datetime.now(UTC))

        assert started == ["aa"]
        assert ingest_queue.waiting() == []

    def test_the_interval_starts_when_the_batch_is_done(self, ready):
        from app import main

        status, started = ready
        main._fill_worker_queues()
        now = datetime.now(UTC)

        self._tick(status, now)                    # aa を流す
        _finish(status, "aa")
        self._tick(status, now + timedelta(minutes=5))   # 塊の残り bb を流す
        assert started == ["aa", "bb"]
        _finish(status, "bb")

        finished = now + timedelta(minutes=50)     # 起きてから 50 分後に流し終えた
        self._tick(status, finished)
        assert workers.finished_at("精査") == finished.isoformat(timespec="seconds")

        # 起きてから 30 分は過ぎているが、**流し終えてからはまだ**
        self._tick(status, finished + timedelta(minutes=10))
        assert started == ["aa", "bb"]

        self._tick(status, finished + timedelta(minutes=31))
        assert started == ["aa", "bb", "cc"]


class TestTheStatusPage:
    def test_every_running_job_and_the_line_are_shown(self, trigger):
        """並んで走っていれば 1 本ずつ出し、**止める口はその 1 本を名指しする**。"""
        from app.views import admin

        status, _started = trigger
        status["slots"] = 2
        status["jobs"] = [
            {"source": "meals", "state": "running", "log_tail": []},
            {"source": "news", "state": "running", "log_tail": []},
        ]
        ingest_queue.add("painters", "整理", origin="manual", by="admin")
        ingest_queue.add("meals", "ざっと", origin="schedule")

        html = admin._job_status_html(status)

        assert "同時に走らせられるのは 2 本まで" in html
        assert 'name="source" value="meals"' in html
        assert 'name="source" value="news"' in html
        assert "取り込みの待ち行列(2 本)" in html
        assert "空き待ち" in html
        assert "同じ収集が焼いている最中" in html
        # **予定の来た回は外せない**(外しても次の周でまた積まれる)
        assert html.count("/admin/ingest/queue/remove") == 1

    def test_the_collect_buttons_stay_pressable_when_full(self, trigger):
        """押せば並ぶので、埋まっていても押せる。ダンプの初期化は押せない。"""
        from app.views import admin

        status, _started = trigger
        status["jobs"] = [{"source": "jawiki", "state": "running"}]

        assert admin.queue_buttons_disabled(status) == ""
        assert admin.run_buttons_disabled(status) == " disabled"


class TestTheIngestHistory:
    """夜のうちに何が焼かれ、どれが落ちたのかは、並べないと読めない。"""

    def test_finished_runs_are_listed_newest_first(self):
        from app.views import admin

        html = admin._ingest_history_html({
            "state": "idle",
            "recent": [
                {"source": "news", "state": "error", "error": "boom",
                 "started_at": "2026-09-26T18:00:00+00:00",
                 "finished_at": "2026-09-26T18:01:30+00:00", "log_tail": ["落ちた行"]},
                {"source": "jawiki", "state": "done",
                 "started_at": "2026-09-26T15:00:00+00:00",
                 "finished_at": "2026-09-26T16:00:00+00:00", "log_tail": []},
            ],
        })

        assert '<details id="ingest-history">' in html
        assert html.index("news") < html.index("jawiki")
        assert "2026-09-27 03:00" in html, "日本時間で出す"
        assert '<span class="stale">落ちた</span>' in html
        assert '<div class="stale">boom</div>' in html
        assert "落ちた行" in html
        assert "コンテナを作り直すと消える" in html

    def test_nothing_finished_shows_nothing(self):
        from app.views import admin

        assert admin._ingest_history_html({"state": "idle", "recent": []}) == ""
        assert admin._ingest_history_html(None) == ""
