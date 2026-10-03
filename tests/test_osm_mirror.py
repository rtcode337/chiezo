"""OpenStreetMap の配布元を画面で選び、取り込みを起こすたびに渡す。

Geofabrik は大きなファイルを何度も落とす IP を絞ることがあり、絞られると 1 日かかる。
そのときにミラーへ逃がす口で、trigger の環境変数ではなく画面から選べる。
"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

MIRROR = "https://download.openstreetmap.fr/extracts/"


class TestTheScreen:
    @pytest.fixture()
    def admin(self, tmp_path, monkeypatch, built_data_dir):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        from app.main import app
        from app.views import admin

        with TestClient(app) as client:
            yield admin, client

    def test_chosen_mirror_is_sent_to_osm_sources_only(self, admin, monkeypatch):
        admin_module, client = admin
        assert "OpenStreetMap の配布元" in client.get("/admin/memory").text

        monkeypatch.setattr(admin_module, "TRIGGER_URL", "http://trigger.internal")
        sent = []

        class Ok:
            status_code = 202

        monkeypatch.setattr(admin_module.httpx, "post",
                            lambda url, timeout, json=None: sent.append((url, json)) or Ok())

        # 選んでいなければ渡さない(取り込み側の既定に任せる)
        admin_module.trigger_run("osm_japan")
        res = client.post("/admin/osm-mirror", data={"mirror": "osmfr", "back": "/admin/osm"},
                          follow_redirects=False)
        assert res.status_code == 303 and res.headers["location"].startswith("/admin/osm#")
        admin_module.trigger_run("osm_japan")
        admin_module.trigger_run("geonames")

        assert sent == [
            ("http://trigger.internal/run/osm_japan", None),
            ("http://trigger.internal/run/osm_japan", {"osm_download_base": MIRROR}),
            ("http://trigger.internal/run/geonames", None),
        ]
        assert 'value="osmfr" selected' in client.get("/admin/memory").text

    def test_an_unknown_choice_falls_back_to_none(self, admin):
        from app import settings_store

        _admin_module, client = admin
        client.post("/admin/osm-mirror", data={"mirror": "http://evil.invalid/"})
        assert settings_store.osm_download_base() is None


class TestTheTrigger:
    @pytest.fixture()
    def trigger(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(tmp_path))
        import core
        import server

        got: dict = {}
        done = threading.Event()

        def run(source, credential=None, download_base=None):
            got.update(source=source, download_base=download_base)
            done.set()

        monkeypatch.setattr(server, "_run_job", run)
        server._jobs.clear()
        server._recent.clear()
        core.clear_stop()
        yield TestClient(server.app), got, done
        server._jobs.clear()
        server._recent.clear()

    def test_the_mirror_reaches_the_job(self, trigger):
        client, got, done = trigger
        assert client.post("/run/osm_japan", json={"osm_download_base": MIRROR}).status_code == 202
        assert done.wait(5)
        assert got == {"source": "osm_japan", "download_base": MIRROR}

    def test_only_https_is_accepted(self, trigger):
        """どこへでも取りに行かせる口にしない。"""
        client, got, done = trigger
        assert client.post("/run/osm_japan", json={"osm_download_base": "file:///etc/passwd"}).status_code == 202
        assert done.wait(5)
        assert got["download_base"] is None


def test_run_puts_the_mirror_on_the_osm_adapter(tmp_path, monkeypatch):
    import main as ingest_main

    import sources
    from sources.osm import OsmAdapter

    seen = {}

    class Stop(Exception):
        pass

    class Probe(OsmAdapter):
        def fetch(self, workdir):
            seen["url"] = self._latest_url()
            raise Stop

    monkeypatch.delenv("OSM_DOWNLOAD_BASE", raising=False)
    monkeypatch.setattr(sources, "get_adapter", lambda _s: Probe(source="osm_japan", region="asia/japan"))
    monkeypatch.setattr(ingest_main, "require_build_memory", lambda _a: None)

    with pytest.raises(Stop):
        ingest_main.run("osm_japan", tmp_path, download_base=MIRROR)
    assert seen["url"] == MIRROR + "asia/japan-latest.osm.pbf"
