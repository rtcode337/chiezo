"""取り込みを何本か並べて走らせる(chiezo-trigger の `CHIEZO_INGEST_SLOTS`)。

**断るのは 3 通り**: 同じソース(409)・本数が埋まっている(429)・
ダンプがもう 1 本走っている(429)。**止める印とログは 1 本ごと**。
"""
from __future__ import annotations

import logging
import threading

import pytest
from fastapi.testclient import TestClient


class _Named:
    def __init__(self, name: str) -> None:
        self.name = name


@pytest.fixture()
def trigger(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(tmp_path))
    import core
    import server
    import sources.collect as collect_sources

    # 収集のソース(配信側に聞くもの)を 2 つ、あることにする
    monkeypatch.setattr(
        collect_sources, "catalog", lambda: [_Named("meals"), _Named("news")]
    )
    # 取り込みそのものは走らせない(受けたところまでを見る)。
    # **`threading.Thread` は差し替えない** —— 同じモジュールなので、テストが
    # 自分で立てるスレッドまで止まる
    monkeypatch.setattr(server, "_run_job", lambda source: None)
    server._jobs.clear()
    server._recent.clear()
    core.clear_stop()
    yield server, TestClient(server.app)
    server._jobs.clear()
    server._recent.clear()
    core.clear_stop()


class TestHowManyAtOnce:
    def test_one_by_default(self, trigger, monkeypatch):
        """**既定は 1 本**(1 本ごとにメモリを食う。並べたい機械でだけ上げる)。"""
        server, client = trigger
        monkeypatch.setattr(server, "SLOTS", 1)

        assert client.post("/run/meals").status_code == 202
        res = client.post("/run/news")

        assert res.status_code == 429
        assert res.json()["detail"]["running"] == ["meals"]

    def test_raising_it_lets_two_run(self, trigger, monkeypatch):
        server, client = trigger
        monkeypatch.setattr(server, "SLOTS", 2)

        assert client.post("/run/meals").status_code == 202
        assert client.post("/run/news").status_code == 202

        got = client.get("/status").json()
        assert got["slots"] == 2
        assert sorted(j["source"] for j in got["jobs"]) == ["meals", "news"]

    def test_the_same_source_runs_one_at_a_time(self, trigger, monkeypatch):
        """ブルーグリーンの切り替えが同じリンクを取り合う。"""
        server, client = trigger
        monkeypatch.setattr(server, "SLOTS", 3)

        client.post("/run/meals")
        res = client.post("/run/meals")

        assert res.status_code == 409

    def test_dumps_run_one_at_a_time(self, trigger, monkeypatch):
        """ダウンロードの置き場を共有するので、ダンプの取り込みは並べない。"""
        server, client = trigger
        monkeypatch.setattr(server, "SLOTS", 3)

        assert client.post("/run/jawiki").status_code == 202
        assert client.post("/run/geonames").status_code == 429
        # 収集はダンプと並べてよい
        assert client.post("/run/meals").status_code == 202

    @pytest.mark.parametrize(("raw", "want"), [("", 1), ("3", 3), ("0", 1), ("たくさん", 1)])
    def test_an_unreadable_setting_falls_back_to_one(self, monkeypatch, raw, want):
        import server

        monkeypatch.setenv("CHIEZO_INGEST_SLOTS", raw)

        assert server._slots_from_env() == want


class TestTheStatus:
    def test_an_old_reader_still_sees_one_job(self, trigger, monkeypatch):
        """**頭には 1 本ぶんの形も残す** —— app と取り込みは別々に焼かれるので、
        古い app はこの形しか読めない。
        """
        server, client = trigger
        monkeypatch.setattr(server, "SLOTS", 2)
        client.post("/run/meals")
        client.post("/run/news")

        got = client.get("/status").json()

        assert got["state"] == "running"
        assert got["source"] in ("meals", "news")

    def test_nothing_running_is_idle(self, trigger):
        _server, client = trigger

        got = client.get("/status").json()

        assert got["state"] == "idle" and got["jobs"] == [] and got["recent"] == []

    def test_a_finished_one_moves_to_recent(self, tmp_path, monkeypatch):
        import server

        monkeypatch.setattr(server, "DATA_DIR", tmp_path)
        monkeypatch.setitem(__import__("sys").modules, "main", _FakeMain())
        server._jobs.clear()
        server._jobs["meals"] = server._new_job("meals")

        server._run_job("meals")

        assert "meals" not in server._jobs
        assert server._recent[-1]["state"] == "done"
        assert any("baking meals" in line for line in server._recent[-1]["log_tail"])


class _FakeMain:
    def run(self, source, data_dir):
        logging.getLogger("chiezo.ingest").info("baking %s", source)


class TestOneJobAtATime:
    def test_the_log_goes_to_the_job_that_wrote_it(self, trigger):
        """**ログは書いた 1 本に付ける** —— 並んで走ると混ざる。"""
        server, _client = trigger
        for name in ("meals", "news"):
            server._jobs[name] = server._new_job(name)

        def write(name: str) -> None:
            server._bound.source = name
            logging.getLogger("chiezo.ingest").info("hello from %s", name)

        for name in ("meals", "news"):
            t = threading.Thread(target=write, args=(name,))
            t.start()
            t.join()

        meals = "\n".join(server._jobs["meals"]["log_tail"])
        news = "\n".join(server._jobs["news"]["log_tail"])
        assert "hello from meals" in meals and "hello from news" not in meals
        assert "hello from news" in news and "hello from meals" not in news

    def test_a_line_from_an_unnamed_thread_goes_to_every_running_one(self, trigger):
        """アダプタが自前で立てるスレッドは名乗らない —— 落とすより、紛れるほうが読める。"""
        server, _client = trigger
        for name in ("meals", "news"):
            server._jobs[name] = server._new_job(name)

        t = threading.Thread(
            target=lambda: logging.getLogger("chiezo.ingest").info("from a helper")
        )
        t.start()
        t.join()

        for name in ("meals", "news"):
            assert any("from a helper" in line for line in server._jobs[name]["log_tail"])

    def test_the_stop_flag_is_per_job(self):
        """**1 本を止めたつもりで全部が降りない。**"""
        import core

        core.request_stop("meals")
        got: dict[str, bool] = {}

        def look(name: str) -> None:
            core.bind(name)
            got[name] = core.stopping()
            core.bind(None)

        for name in ("meals", "news"):
            t = threading.Thread(target=look, args=(name,))
            t.start()
            t.join()
        core.clear_stop()

        assert got == {"meals": True, "news": False}

    def test_starting_one_clears_only_its_own_flag(self, trigger, monkeypatch):
        import core

        server, client = trigger
        monkeypatch.setattr(server, "SLOTS", 2)
        client.post("/run/news")
        core.request_stop("news")
        core.request_stop("meals")

        client.post("/run/meals")

        assert core.stopping("meals") is False
        assert core.stopping("news") is True
