"""ダンプのダウンロード(`ingest/download.py`)。curl に落とさせ、進み具合と止める印を見る。"""
from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import download
import pytest

import core

BODY = b"x" * 64 * 1024


def _serve(delay: float = 0.0):
    """BODY を 4 KiB ずつ、`delay` 秒ずつ空けて返す(HEAD には大きさだけ)。"""

    class Handler(BaseHTTPRequestHandler):
        def _head(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()

        def do_HEAD(self):
            self._head()

        def do_GET(self):
            self._head()
            try:
                for i in range(0, len(BODY), 4096):
                    self.wfile.write(BODY[i:i + 4096])
                    self.wfile.flush()
                    time.sleep(delay)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server


@pytest.fixture
def quick(monkeypatch):
    monkeypatch.setattr(download, "POLL_SECONDS", 0.05)
    yield
    core.clear_stop()
    core.report_progress(None)


def test_it_downloads_and_clears_the_progress(tmp_path, quick):
    server = _serve()
    try:
        dest = download.fetch(f"http://127.0.0.1:{server.server_port}/dump.pbf", tmp_path / "dump.pbf")
    finally:
        server.shutdown()
    assert dest.read_bytes() == BODY
    assert not (tmp_path / "dump.pbf.part").exists()
    assert core.progress_of("") is None


def test_progress_is_reported_while_downloading(tmp_path, quick):
    """落としている最中に、量・全体・割合のもとになる値が置かれる。"""
    server = _serve(delay=0.02)
    seen: list[dict] = []
    real = core.report_progress
    download.report_progress = lambda info: (seen.append(info) if info else None, real(info))
    try:
        download.fetch(f"http://127.0.0.1:{server.server_port}/d.pbf", tmp_path / "d.pbf")
    finally:
        download.report_progress = real
        server.shutdown()
    assert seen, "進み具合が一度も置かれなかった"
    assert seen[-1]["total"] == len(BODY)
    assert seen[-1]["file"] == "d.pbf"
    assert "MiB" in download.progress_line(seen[-1])


def test_stop_ends_curl_and_keeps_the_part(tmp_path, quick):
    """止める印が立てば curl を終わらせて降りる。`.part` は残す(次の回が続きから落とす)。"""
    server = _serve(delay=0.05)
    threading.Timer(0.3, core.request_stop).start()
    try:
        with pytest.raises(core.Stopped):
            download.fetch(f"http://127.0.0.1:{server.server_port}/d.pbf", tmp_path / "d.pbf")
    finally:
        server.shutdown()
    assert (tmp_path / "d.pbf.part").exists()
    assert not (tmp_path / "d.pbf").exists()


def test_an_existing_download_is_kept(tmp_path, quick):
    (tmp_path / "d.pbf").write_bytes(b"done")
    assert download.fetch("http://127.0.0.1:1/never", tmp_path / "d.pbf").read_bytes() == b"done"


def test_the_screen_shows_the_progress():
    from app.views import admin

    html = admin._progress_html({
        "phase": "download", "file": "osm_japan-20261003.osm.pbf",
        "bytes": 512 * 1024 * 1024, "total": 2048 * 1024 * 1024,
        "rate": 2.5 * 1024 * 1024, "eta_seconds": 615,
    })
    assert "osm_japan-20261003.osm.pbf" in html
    assert "25.0%" in html and "2.50 MiB/s" in html and "残り約 10 分" in html
    assert "<progress" in html
    # 大きさが分からなければ、割合と棒は出さない
    assert "<progress" not in admin._progress_html({"phase": "download", "file": "x", "bytes": 1})
    assert admin._progress_html(None) == ""
