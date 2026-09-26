"""取り込みを途中で降ろす(`core.check_stop` / `chiezo-trigger` の `POST /stop`)。

**殺すのではなく、安全なところで降りる。** 走っているのは daemon スレッドで、
外から止める手段はそもそも無い —— 印を立てて、取り込みの側が区切りのいいところで
見に行く。降りるのは切り替えより前なので、**いま配信している世代はそのまま残る**。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(autouse=True)
def clean():
    import core

    core.clear_stop()
    yield
    core.clear_stop()


class TestTheFlag:
    def test_nothing_happens_until_it_is_set(self):
        import core

        assert core.stopping() is False
        core.check_stop()  # 何も起きない

    def test_it_gets_off_at_the_next_check(self):
        import core

        core.request_stop()
        assert core.stopping() is True
        with pytest.raises(core.Stopped):
            core.check_stop()

    def test_the_next_run_clears_it(self):
        import core

        core.request_stop()
        core.clear_stop()
        core.check_stop()


class TestStoppingABuild:
    def test_the_half_written_db_is_cleaned_up(self, tmp_path, monkeypatch, fixture_dump):
        """**降りたあとに `.building` を残さない**(次の回が拾うものではない)。"""
        import main as ingest_main

        import core
        from tests.conftest import make_test_adapter

        adapter = make_test_adapter()
        data_dir = tmp_path / "data"
        data_dir.mkdir()
        monkeypatch.setenv("DUMP_FILE", str(fixture_dump))
        monkeypatch.setenv("DUMP_DATE", "20260101")
        core.request_stop()

        with pytest.raises(core.Stopped):
            monkeypatch.setattr("sources.get_adapter", lambda _name: adapter)
            ingest_main.run("jawiki", data_dir)

        assert not list(data_dir.glob("*.building")), "書きかけの DB が残っている"

    def test_the_live_generation_is_untouched(self, tmp_path, monkeypatch, built_data_dir):
        """切り替えの前で降りるので、いま配信しているものはそのまま。"""
        import shutil

        import main as ingest_main

        import core
        from tests.conftest import make_test_adapter

        data_dir = tmp_path / "data"
        shutil.copytree(built_data_dir, data_dir, symlinks=True)
        before = (data_dir / "jawiki.db").resolve().name

        monkeypatch.setenv("DUMP_FILE", str(_fixture()))
        monkeypatch.setenv("DUMP_DATE", "20260102")
        monkeypatch.setattr("sources.get_adapter", lambda _name: make_test_adapter())
        core.request_stop()
        with pytest.raises(core.Stopped):
            ingest_main.run("jawiki", data_dir)

        assert (data_dir / "jawiki.db").resolve().name == before


def _fixture():
    from tests.conftest import FIXTURE_DUMP

    return FIXTURE_DUMP


class TestTheStopEndpoint:
    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(tmp_path))
        import server

        server._jobs.clear()
        server._recent.clear()
        return TestClient(server.app)

    def test_it_refuses_when_nothing_is_running(self, client):
        assert client.post("/stop").status_code == 409

    def test_it_marks_the_running_job(self, client):
        import core
        import server

        server._jobs["tazuna_meals"] = server._new_job("tazuna_meals")

        res = client.post("/stop")

        assert res.status_code == 200 and res.json()["stopping"] is True
        assert core.stopping("tazuna_meals") is True
        assert client.get("/status").json()["stopping"] is True

    def test_only_the_named_one_gets_off(self, client):
        """**並んで走っている別の 1 本は降ろさない。** 印が 1 つだと、
        1 本を止めたつもりで全部が降りる。
        """
        import core
        import server

        for name in ("tazuna_meals", "tazuna_news"):
            server._jobs[name] = server._new_job(name)

        res = client.post("/stop", params={"source": "tazuna_news"})

        assert res.status_code == 200
        assert core.stopping("tazuna_news") is True
        assert core.stopping("tazuna_meals") is False
        jobs = {j["source"]: j for j in client.get("/status").json()["jobs"]}
        assert jobs["tazuna_news"]["stopping"] and not jobs["tazuna_meals"]["stopping"]

    def test_a_named_one_that_is_not_running_is_refused(self, client):
        import server

        server._jobs["tazuna_meals"] = server._new_job("tazuna_meals")

        assert client.post("/stop", params={"source": "jawiki"}).status_code == 409

    def test_starting_again_lowers_the_flag(self, client, monkeypatch):
        """**印を下ろし忘れると、始めた瞬間に降りる。**"""
        import core
        import server

        core.request_stop("jawiki")
        monkeypatch.setattr(server.threading, "Thread", lambda **kw: _NoThread())
        client.post("/run/jawiki")

        assert core.stopping("jawiki") is False

    def test_a_stopped_job_is_not_an_error(self, client, monkeypatch):
        """人が降ろしたのと落ちたのとでは、次にすることが逆になる。"""
        import core
        import server

        def _stopped(_source, _dir):
            raise core.Stopped("取り込みを止めました")

        monkeypatch.setitem(__import__("sys").modules["main"].__dict__, "run", _stopped)
        server._jobs["jawiki"] = server._new_job("jawiki")
        server._run_job("jawiki")

        done = server._recent[-1]
        assert done["state"] == "stopped"
        assert done["stopping"] is False
        assert "jawiki" not in server._jobs


class TestKeepingTheLastFailure:
    """**次の取り込みが始まっても、落ちた回の理由は残す。**

    状態もログも「いまの 1 本」ぶんしか無いので、読みに来たときには消えている、が
    普通に起きる(本番で、落ちた 56 秒後に次が始まって何も残らなかった)。
    """

    @pytest.fixture()
    def client(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(tmp_path))
        import server

        server._jobs.clear()
        server._recent.clear()
        server._last_failure = None
        return TestClient(server.app)

    def _fail(self, monkeypatch, source="tazuna_meals"):
        import server

        def _boom(_source, _dir):
            raise RuntimeError("UNIQUE constraint failed: docs.title")

        monkeypatch.setitem(__import__("sys").modules["main"].__dict__, "run", _boom)
        server._jobs[source] = server._new_job(source)
        server._jobs[source]["log_tail"].append("2026-09-20 13:02:24 JST INFO building …")
        server._run_job(source)

    def test_it_is_kept_after_the_next_run_starts(self, client, monkeypatch):
        import server

        self._fail(monkeypatch)
        monkeypatch.setattr(server.threading, "Thread", lambda **kw: _NoThread())
        client.post("/run/jawiki")

        got = client.get("/status").json()

        assert got["state"] == "running" and got["source"] == "jawiki"
        assert got["log_tail"] == [], "いまの 1 本のログは新しくなる"
        assert got["last_failure"]["source"] == "tazuna_meals"
        assert "UNIQUE constraint" in got["last_failure"]["error"]
        assert got["last_failure"]["log_tail"], "落ちた回のログも残す"

    def test_nothing_is_kept_when_nothing_failed(self, client):
        assert client.get("/status").json()["last_failure"] is None

    def test_the_screen_shows_it_while_another_job_runs(self, client, monkeypatch):
        import server
        from app.views import admin

        self._fail(monkeypatch)
        server._jobs["jawiki"] = server._new_job("jawiki")
        job = client.get("/status").json()

        html = admin._job_status_html(job)

        assert "前に落ちた回: tazuna_meals" in html
        assert "UNIQUE constraint" in html

    def test_it_is_not_shown_twice(self, client, monkeypatch):
        """いまの 1 本がその失敗そのものなら、同じものが二度並ぶ。"""
        from app.views import admin

        self._fail(monkeypatch)
        job = client.get("/status").json()

        assert job["state"] == "error"
        assert "前に落ちた回" not in admin._job_status_html(job)


class _NoThread:
    def start(self) -> None:
        return None
