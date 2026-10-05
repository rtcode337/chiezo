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
        # 入力欄の見本は、API キーではない形のものだけが名乗る
        assert catalog["jquants_master"]["credential"]["placeholder"] is None


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


class TestTheKeysPage:
    """API キーの面。chiezo 全体の外部 API のキーを名前で持ち、ソースは名前で引く。"""

    @pytest.fixture()
    def admin(self, tmp_path, monkeypatch, built_data_dir):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        from app.main import app
        from app.views import admin

        spec = {"label": "J-Quants の API キー", "help_url": "https://example.com/", "group": "jquants"}
        monkeypatch.setattr(admin, "initializable_sources", lambda: {
            "jquants_master": {"kind": "jquants", "dump": True, "credential": spec},
            "jquants_earnings": {"kind": "jquants", "dump": True, "credential": spec},
            "geonames": {"kind": "geonames", "dump": True},
        })
        with TestClient(app) as client:
            yield admin, client

    def test_one_key_serves_every_source_that_names_it(self, admin, monkeypatch):
        admin_module, client = admin
        html = client.get("/admin/keys").text
        assert "<code>jquants</code>" in html and "jquants_earnings, jquants_master" in html
        assert "未登録" in html
        # 見本を名乗らないキーは「API キー」と出す
        assert 'placeholder="API キー"' in html
        # 収集の道具が使うキーも並ぶ(取り込みのソースではないのでカタログには載らない)
        assert "<code>pihole_url</code>" in html and "<code>pihole_password</code>" in html
        # URL は見ながら打てる欄、パスワードは隠す欄
        assert '<input type="text" name="credential" placeholder="http://pi.hole"' in html
        # 長期記憶の面は、未登録を知らせて API キーの面へ案内する
        assert "取り込みに要るキーが未登録です: jquants" in client.get("/admin/memory").text

        res = client.post("/admin/keys", data={"name": "jquants", "credential": KEY},
                          follow_redirects=False)
        assert res.status_code == 303
        html = client.get("/admin/keys").text
        assert "登録済み" in html and KEY not in html

        monkeypatch.setattr(admin_module, "TRIGGER_URL", "http://trigger.internal")
        sent = []

        class Ok:
            status_code = 202

        monkeypatch.setattr(admin_module.httpx, "post",
                            lambda url, timeout, json=None: sent.append((url, json)) or Ok())
        admin_module.trigger_run("jquants_master")
        admin_module.trigger_run("jquants_earnings")
        admin_module.trigger_run("geonames")
        assert sent == [
            ("http://trigger.internal/run/jquants_master", {"credential": KEY}),
            ("http://trigger.internal/run/jquants_earnings", {"credential": KEY}),
            ("http://trigger.internal/run/geonames", None),
        ]

    def test_a_key_nobody_uses_yet_can_be_kept_and_deleted(self, admin):
        """収集の道具が後から名前で引けるよう、ソースが名乗っていないキーも置いておける。"""
        _admin, client = admin
        from app import settings_store

        client.post("/admin/keys", data={"name": "google_books", "label": "Google Books", "credential": KEY})
        assert settings_store.api_key("google_books") == KEY
        html = client.get("/admin/keys").text
        assert "<code>google_books</code>" in html and "Google Books" in html

        client.post("/admin/keys", data={"name": "google_books", "action": "delete"})
        assert settings_store.api_key("google_books") is None

    def test_a_bad_name_is_refused(self, admin):
        _admin, client = admin
        assert client.post("/admin/keys", data={"name": "Bad Name", "credential": KEY}).status_code == 400

    def test_a_key_registered_per_source_moves_to_the_group(self, admin):
        """組ごとに持つ形に変える前に、ソース名で登録したキーを入れ直さずに使える。"""
        admin_module, _client = admin
        from app import settings_store

        settings_store.set_api_key("jquants_master", KEY)

        assert admin_module._credential_for("jquants_earnings") == KEY
        assert settings_store.api_key("jquants") == KEY
        assert settings_store.api_key("jquants_master") is None


def test_the_old_table_moves_into_api_keys(tmp_path, monkeypatch):
    """ソースごとに持っていた頃の表(source_credentials)の中身は、そのまま api_keys へ移る。"""
    import sqlite3

    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path))
    conn = sqlite3.connect(tmp_path / "settings.db")
    conn.execute("CREATE TABLE source_credentials (source TEXT PRIMARY KEY, credential TEXT NOT NULL,"
                 " updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO source_credentials VALUES ('jquants_master', ?, '2026-10-01T00:00:00+00:00')",
                 (KEY,))
    conn.commit()
    conn.close()
    from app import settings_store

    assert settings_store.api_key("jquants_master") == KEY


def test_the_catalog_names_the_group():
    from fastapi.testclient import TestClient

    import server

    catalog = TestClient(server.app).get("/sources").json()["sources"]
    assert catalog["jquants_master"]["credential"]["group"] == "jquants"
    assert catalog["jquants_earnings"]["credential"]["group"] == "jquants"
