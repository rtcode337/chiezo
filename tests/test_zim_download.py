"""Wikipedia のオフライン版を画面から落とす(`ingest/zim_download.py`・trigger の `/zim`・状況の面のボタン)。
外へは出ない(Kiwix の一覧と大きさは差し替え、落とすのは `download.fetch` を差し替える)。"""
from __future__ import annotations

import io

import pytest
from fastapi.testclient import TestClient

LISTING = """<html><body><pre>
<a href="wikipedia_ja_all_maxi_2025-10.zim">wikipedia_ja_all_maxi_2025-10.zim</a>   2025-10-05 21:12   25G
<a href="wikipedia_ja_all_maxi_2026-06.zim">wikipedia_ja_all_maxi_2026-06.zim</a>   2026-06-07 02:33   29G
<a href="wikipedia_ja_all_nopic_2026-09.zim">wikipedia_ja_all_nopic_2026-09.zim</a>   2026-09-01 00:00   15G
<a href="wikipedia_en_all_maxi_2026-09.zim">wikipedia_en_all_maxi_2026-09.zim</a>   2026-09-01 00:00   110G
</pre></body></html>"""
SIZE = 5_000


class _Response(io.BytesIO):
    def __init__(self, body: bytes = b"", headers: dict | None = None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False


@pytest.fixture
def zim(tmp_path, monkeypatch):
    import zim_download

    folder = tmp_path / "zim"
    monkeypatch.setenv("CHIEZO_ZIM_DIR", str(folder))
    monkeypatch.setattr(zim_download, "_cache", (0.0, None))
    monkeypatch.setattr(zim_download, "SPARE_BYTES", 100)

    def read(url, method="GET"):
        if method == "HEAD":
            return _Response(headers={"Content-Length": str(SIZE)})
        return _Response(LISTING.encode())

    monkeypatch.setattr(zim_download, "_read", read)
    return zim_download, folder


class TestLatest:
    def test_picks_the_newest_japanese_edition_with_pictures(self, zim):
        # nopic には絵が入っていない。英語版は別物
        zim_download, _folder = zim
        assert zim_download.latest() == {
            "name": "wikipedia_ja_all_maxi_2026-06.zim",
            "url": "https://download.kiwix.org/zim/wikipedia/wikipedia_ja_all_maxi_2026-06.zim",
            "bytes": SIZE,
        }

    def test_remembers_the_list(self, zim, monkeypatch):
        zim_download, _folder = zim
        zim_download.latest()
        monkeypatch.setattr(zim_download, "_read", lambda *_a, **_k: (_ for _ in ()).throw(OSError("offline")))
        assert zim_download.latest()["name"] == "wikipedia_ja_all_maxi_2026-06.zim"


class TestRun:
    def test_downloads_then_removes_the_older_edition(self, zim, monkeypatch):
        zim_download, folder = zim
        import download

        folder.mkdir()
        (folder / "wikipedia_ja_all_maxi_2025-10.zim").write_bytes(b"old")
        fetched = []

        def fetch(url, dest):
            fetched.append(url)
            dest.write_bytes(b"new")
            return dest

        monkeypatch.setattr(download, "fetch", fetch)
        zim_download.run()

        assert fetched == ["https://download.kiwix.org/zim/wikipedia/wikipedia_ja_all_maxi_2026-06.zim"]
        assert sorted(p.name for p in folder.iterdir()) == ["wikipedia_ja_all_maxi_2026-06.zim"]

    def test_does_nothing_when_it_already_has_the_newest(self, zim, monkeypatch):
        zim_download, folder = zim
        import download

        folder.mkdir()
        (folder / "wikipedia_ja_all_maxi_2026-06.zim").write_bytes(b"have")
        monkeypatch.setattr(download, "fetch", lambda *_a: pytest.fail("落とし直さない"))
        zim_download.run()

    def test_refuses_when_the_disk_cannot_hold_it(self, zim, monkeypatch):
        zim_download, _folder = zim
        import download

        monkeypatch.setattr(zim_download, "free_bytes", lambda: 10)
        monkeypatch.setattr(download, "fetch", lambda *_a: pytest.fail("落とし始めない"))
        with pytest.raises(SystemExit, match="空きが足りません"):
            zim_download.run()

    def test_counts_what_is_already_partly_downloaded(self, zim):
        zim_download, folder = zim
        folder.mkdir()
        (folder / "wikipedia_ja_all_maxi_2026-06.zim.part").write_bytes(b"x" * 4_000)
        info = zim_download.latest()
        assert zim_download.needed_bytes(info) == SIZE - 4_000 + 100
        assert zim_download.placed() == [
            {"name": "wikipedia_ja_all_maxi_2026-06.zim", "bytes": 4_000, "partial": True}
        ]


@pytest.fixture
def trigger(tmp_path, monkeypatch, zim):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(tmp_path))
    import core
    import server

    started = []
    monkeypatch.setattr(server, "_run_job", lambda *a, **k: started.append((a, k)))
    server._jobs.clear()
    server._recent.clear()
    core.clear_stop()
    yield server, TestClient(server.app), started
    server._jobs.clear()
    server._recent.clear()
    core.clear_stop()


class TestTrigger:
    def test_status_does_not_wait_for_the_list(self, trigger, zim):
        # 状況の面から聞かれるので、外へ聞きに行くのを待たせない(初めは null で、裏で読み始める)
        _server, client, _started = trigger
        zim_download, _folder = zim
        got = client.get("/zim").json()
        assert got["latest"] is None or got["latest"]["name"] == "wikipedia_ja_all_maxi_2026-06.zim"
        assert got["running"] is False
        zim_download.latest()
        assert client.get("/zim").json()["latest"]["name"] == "wikipedia_ja_all_maxi_2026-06.zim"

    def test_runs_in_the_dump_lane_one_at_a_time(self, trigger):
        server, client, started = trigger
        assert client.post("/zim/run").status_code == 202
        assert server._jobs[server.ZIM_JOB]["lane"] == server.LANE_DUMP
        assert started and started[0][1]["work"].__name__ == "run"
        assert client.post("/zim/run").status_code == 409

    def test_does_not_run_beside_a_dump_ingest(self, trigger, monkeypatch):
        server, client, _started = trigger
        server._jobs["jawiki"] = server._new_job("jawiki", server.LANE_DUMP)
        assert client.post("/zim/run").status_code == 429

    def test_a_dump_ingest_waits_for_the_download(self, trigger):
        _server, client, _started = trigger
        client.post("/zim/run")
        assert client.post("/run/jawiki").status_code == 429


class TestStatusPage:
    def offer(self, **over):
        return {
            "latest": {"name": "wikipedia_ja_all_maxi_2026-06.zim", "url": "u", "bytes": 30 * 1024**3},
            "placed": [],
            "free_bytes": 100 * 1024**3,
            "needed_bytes": 31 * 1024**3,
            "running": False,
            **over,
        }

    @pytest.fixture
    def admin(self, monkeypatch, tmp_path):
        from app.views import admin

        monkeypatch.setenv("CHIEZO_ZIM_DIR", str(tmp_path / "none"))
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.test")
        return admin

    def test_offers_the_newest_edition(self, admin, monkeypatch):
        monkeypatch.setattr(admin, "_fetch_zim_offer", lambda: self.offer())
        html = admin._zim_offer_html({"state": "idle", "jobs": []})
        assert "wikipedia_ja_all_maxi_2026-06.zim" in html
        assert '<button type="submit">最新版を落とす</button>' in html

    def test_cannot_press_without_room(self, admin, monkeypatch):
        monkeypatch.setattr(admin, "_fetch_zim_offer", lambda: self.offer(free_bytes=10 * 1024**3))
        html = admin._zim_offer_html({"state": "idle", "jobs": []})
        assert "disabled" in html and "空きが足りません" in html

    def test_says_when_it_already_has_it(self, admin, monkeypatch):
        placed = [{"name": "wikipedia_ja_all_maxi_2026-06.zim", "bytes": 1, "partial": False}]
        monkeypatch.setattr(admin, "_fetch_zim_offer", lambda: self.offer(placed=placed))
        html = admin._zim_offer_html({"state": "idle", "jobs": []})
        assert "置いています" in html and "button" not in html

    def test_cannot_press_while_a_dump_runs(self, admin, monkeypatch):
        monkeypatch.setattr(admin, "_fetch_zim_offer", lambda: self.offer())
        job = {"state": "running", "dump_lane": True, "slots": 1,
               "jobs": [{"source": "jawiki", "lane": "dump", "state": "running"}]}
        html = admin._zim_offer_html(job)
        assert "disabled" in html

    def test_no_trigger_no_button(self, admin, monkeypatch):
        monkeypatch.setattr(admin, "_fetch_zim_offer", lambda: None)
        assert admin._zim_offer_html(None) == ""
