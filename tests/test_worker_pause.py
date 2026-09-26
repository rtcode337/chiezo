"""ワーカーを止める / 動かす(`workers.set_paused`)。

枠が細いときに、そのワーカーだけ回復まで止めておく。止めているあいだは相手を
1 つも選ばないので、どの道からも流れない。待ち行列は残り、動かせば続きから流れる。
"""

from __future__ import annotations

import pytest
from test_api import client, monkeypatch_module  # noqa: F401

from app import usage_store, workers


@pytest.fixture
def state(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _made(name: str = "精査") -> workers.Worker:
    workers.save(workers.merged([], "", name, (workers.Step("codex"), workers.Step("claude"))))
    return workers.load()[0]


class TestPausing:
    def test_a_paused_worker_picks_no_one(self, state):
        worker = _made()
        assert workers.pick(worker) == workers.Step("codex")

        paused = workers.set_paused(worker.key, True)

        assert paused.paused
        assert workers.pick(workers.get(worker.key)) is None

    def test_resuming_picks_again(self, state):
        worker = _made()
        workers.set_paused(worker.key, True)

        workers.set_paused(worker.key, False)

        assert not workers.get(worker.key).paused
        assert workers.pick(workers.get(worker.key)) == workers.Step("codex")

    def test_pausing_again_keeps_the_first_time(self, state):
        """最初に止めた時刻のほうが、回復を待っている長さを表す。"""
        worker = _made()
        first = workers.set_paused(worker.key, True, now="2026-09-26T00:00:00+00:00")

        again = workers.set_paused(worker.key, True, now="2026-09-26T05:00:00+00:00")

        assert again.paused_at == first.paused_at == "2026-09-26T00:00:00+00:00"

    def test_editing_a_paused_worker_keeps_it_paused(self, state):
        """保存し直しただけで勝手に動き出さない。"""
        worker = _made()
        workers.set_paused(worker.key, True)

        workers.save(workers.merged(
            workers.load(), worker.key, "精査(改名)", (workers.Step("claude"),),
        ))

        assert workers.get(worker.key).paused

    def test_the_queue_is_kept_while_paused(self, state):
        """止めていたあいだに積まれたぶんを黙って捨てない。"""
        worker = _made()
        workers.enqueue(worker.key, "news", "整理", "2026-09-26T00:00:00+00:00")

        workers.set_paused(worker.key, True)

        assert [e["collection"] for e in workers.queued(worker.key)] == ["news"]

    def test_an_unknown_worker_is_a_key_error(self, state):
        with pytest.raises(KeyError):
            workers.set_paused("no-such", True)


class TestSayingItIsPaused:
    def test_the_refusal_says_paused_not_full(self, state):
        """「枠に余裕がありません」と出すと、止めたことを忘れた人が枠の表を探し回る。"""
        from app import main

        worker = _made()
        workers.set_paused(worker.key, True)

        said = main._all_full(worker.key)["error"]

        assert "止めています" in said
        assert "枠に余裕" not in said

    def test_a_full_worker_still_says_full(self, state):
        from app import main

        worker = _made()
        for backend in ("codex", "claude"):
            usage_store.save_quota(backend, [{"id": "primary", "label": "直近 5 時間",
                                              "used_percent": 99.0}])

        assert "枠に余裕がありません" in main._all_full(worker.key)["error"]


class TestTheScreen:
    def test_the_button_pauses_and_resumes(self, client, state):  # noqa: F811
        worker = _made()

        res = client.post("/admin/ai/workers/pause",
                          data={"worker_id": worker.key, "paused": "1"}, follow_redirects=False)
        assert res.status_code == 303
        assert workers.get(worker.key).paused
        page = client.get("/admin/collect").text
        assert "⏸ 止めている" in page and ">動かす</button>" in page
        assert "止めているワーカー: 精査" in client.get("/admin/status").text

        client.post("/admin/ai/workers/pause", data={"worker_id": worker.key, "paused": "0"})
        assert not workers.get(worker.key).paused
        assert "止めているワーカー" not in client.get("/admin/status").text

    def test_an_unknown_worker_is_ignored(self, client, state):  # noqa: F811
        res = client.post("/admin/ai/workers/pause",
                          data={"worker_id": "no-such", "paused": "1"}, follow_redirects=False)
        assert res.status_code == 303
