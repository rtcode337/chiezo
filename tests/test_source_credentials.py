"""取り込みに API キーが要るソースのキーを、画面で登録して取り込みへ渡す。

**キーは画面にも、取り込みの状態にもログにも出さない。**
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

KEY = "secret-key-123"


class TestTheTrigger:
    @pytest.fixture()
    def trigger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(tmp_path))
        import core
        import server

        got: dict = {}
        done = threading.Event()

        def run(source, credential=None):
            got.update(source=source, credential=credential)
            done.set()

        monkeypatch.setattr(server, "_run_job", run)
        server._jobs.clear()
        server._recent.clear()
        core.clear_stop()
        yield server, TestClient(server.app), got, done
        server._jobs.clear()
        server._recent.clear()

    def test_the_key_reaches_the_job_but_not_the_status(self, trigger):
        _server, client, got, done = trigger

        res = client.post("/run/jquants_master", json={"credential": KEY})

        assert res.status_code == 202
        assert done.wait(5)
        assert got == {"source": "jquants_master", "credential": KEY}
        assert KEY not in client.get("/status").text

    def test_without_a_body_nothing_is_passed(self, trigger):
        _server, client, got, done = trigger

        assert client.post("/run/jquants_master").status_code == 202
        assert done.wait(5)
        assert got["credential"] is None

    def test_the_catalog_says_which_sources_need_a_key(self, trigger):
        _server, client, _got, _done = trigger

        catalog = client.get("/sources").json()["sources"]

        assert catalog["jquants_master"]["credential"]["label"] == "J-Quants の API キー"
        assert "credential" not in catalog["geonames"]


def test_run_hands_the_key_to_the_adapter_only(tmp_path, monkeypatch):
    """環境変数には書かない(並んで走る別の 1 本に見えてしまう)。"""
    import main as ingest_main

    import sources
    from sources.jquants import API_KEY_ENV, JquantsMasterAdapter

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    seen = {}

    class Stop(Exception):
        pass

    class Probe(JquantsMasterAdapter):
        def fetch(self, workdir):
            seen["key"] = self._api_key()
            raise Stop

    monkeypatch.setattr(sources, "get_adapter", lambda _s: Probe())
    monkeypatch.setattr(ingest_main, "require_build_memory", lambda _a: None)

    with pytest.raises(Stop):
        ingest_main.run("jquants_master", tmp_path, credential=KEY)

    assert seen["key"] == KEY
    import os
    assert API_KEY_ENV not in os.environ


class TestTheAdminScreen:
    @pytest.fixture()
    def admin(self, tmp_path, monkeypatch, built_data_dir):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        from app.main import app
        from app.views import admin

        monkeypatch.setattr(admin, "initializable_sources", lambda: {
            "jquants_master": {
                "kind": "jquants", "dump": True,
                "credential": {"label": "J-Quants の API キー", "help_url": "https://example.com/"},
            },
            "geonames": {"kind": "geonames", "dump": True},
        })
        with TestClient(app) as client:
            yield admin, client

    def test_it_is_stored_and_never_shown(self, admin):
        _admin, client = admin

        res = client.post("/admin/source-key", data={"source": "jquants_master", "credential": KEY},
                          follow_redirects=False)

        assert res.status_code == 303
        from app import settings_store
        assert settings_store.source_credential("jquants_master") == KEY
        html = client.get("/admin/memory").text
        assert "取り込みに要るキー" in html and "登録済み" in html
        assert KEY not in html

    def test_it_can_be_deleted(self, admin):
        _admin, client = admin
        client.post("/admin/source-key", data={"source": "jquants_master", "credential": KEY})

        client.post("/admin/source-key", data={"source": "jquants_master", "action": "delete"})

        from app import settings_store
        assert settings_store.source_credential("jquants_master") is None

    def test_a_source_that_takes_no_key_is_refused(self, admin):
        _admin, client = admin

        res = client.post("/admin/source-key", data={"source": "geonames", "credential": KEY})

        assert res.status_code == 404

    def test_a_rebuild_carries_the_stored_key(self, admin, monkeypatch):
        admin_module, client = admin
        monkeypatch.setattr(admin_module, "TRIGGER_URL", "http://trigger.internal")
        client.post("/admin/source-key", data={"source": "jquants_master", "credential": KEY})
        sent = []

        class Ok:
            status_code = 202

        def fake_post(url, timeout, json=None):
            sent.append((url, json))
            return Ok()

        monkeypatch.setattr(admin_module.httpx, "post", fake_post)
        admin_module.trigger_run("jquants_master")
        admin_module.trigger_run("geonames")

        assert sent == [
            ("http://trigger.internal/run/jquants_master", {"credential": KEY}),
            ("http://trigger.internal/run/geonames", None),
        ]
