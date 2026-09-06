"""集めたソースを焼く側(`ingest/sources/collect.py`)のテスト。

**押さえるのは「払った AI の呼び出しを無駄にしない」だけ**。ダンプを落として焼く
他のソースと違い、`/fetch` は取りに行くたびに AI が動き、`{cursor}` が進むので
同じものは二度と返らない —— 焼く前に落ちたときに取り直すと、集めたぶんが消える。

本物の HTTP サーバーを立てるのは `test_remote_plugins.py` と同じ理由(ストリーム読みが
契約の中身なので、モックに置き換えると何も検証できない)。
"""
from __future__ import annotations

import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from sources import collect

DOCS = [{"doc_id": 1, "title": "見出し", "body": "本文"}]


class _Handler(BaseHTTPRequestHandler):
    fetches: ClassVar[int] = 0

    def do_GET(self):
        if self.path.startswith("/v1/collect/sources"):
            payload = {"sources": [{"name": "sample_news", "kind": "collect", "label": "見本"}]}
        elif self.path.startswith("/v1/collect/fetch"):
            type(self).fetches += 1
            lines = [{"meta": {"dump_date": "20260907010203"}}, *DOCS]
            body = "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in lines).encode()
            self._send(body, "application/x-ndjson")
            return
        else:
            self.send_response(404)
            self.end_headers()
            return
        self._send(json.dumps(payload).encode(), "application/json")

    def _send(self, body: bytes, content_type: str):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # テスト出力を汚さない
        pass


@pytest.fixture
def app(monkeypatch):
    """素材を配る側(chiezo-app)の代わり。"""
    _Handler.fetches = 0
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    monkeypatch.setenv(collect.APP_URL_ENV, f"http://127.0.0.1:{server.server_port}")
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


class TestCatalog:
    def test_the_adapter_list_comes_from_the_app(self, app):
        """ソースは実行時に増えるので、固定で並べず配信側に聞く。"""
        assert list(collect.adapters()) == ["sample_news"]
        assert collect.adapter_for("sample_news").source == "sample_news"
        assert collect.adapter_for("unknown") is None

    def test_an_unreachable_app_is_not_fatal(self, monkeypatch):
        """収集は任意の層。配信側が立っていないだけで取り込み全体を止めない。"""
        # 誰も待っていないポートを取る(即座に断られるので待たされない)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        monkeypatch.setenv(collect.APP_URL_ENV, f"http://127.0.0.1:{port}")
        assert collect.catalog() == []


class TestStagedMaterial:
    def test_it_keeps_the_material_until_it_is_baked(self, app, tmp_path):
        """焼く前に落ちたら、次は AI を呼ばずに残ったものから焼き直す。"""
        first = collect.adapter_for("sample_news")
        path, date = first.fetch(tmp_path)
        assert path.name == "sample_news-20260907010203.ndjson"
        assert date == "20260907010203"
        assert _Handler.fetches == 1

        # ここで焼きに失敗した、という想定(on_success を呼ばない)
        second = collect.adapter_for("sample_news")
        again, date_again = second.fetch(tmp_path)
        assert (again, date_again) == (path, date)
        assert _Handler.fetches == 1  # 取り直していない

    def test_it_drops_the_material_once_it_is_baked(self, app, tmp_path):
        """残すと、次回が AI を呼ばずに同じ中身を焼き直す。"""
        adapter = collect.adapter_for("sample_news")
        path, _ = adapter.fetch(tmp_path)
        adapter.on_success(tmp_path / "sample_news-20260907010203.db")
        assert not path.exists()

        collect.adapter_for("sample_news").fetch(tmp_path)
        assert _Handler.fetches == 2

    def test_the_meta_still_sets_the_validation_bar(self, app, tmp_path):
        """残っていたものから焼くときも、1 行目の meta を読み直す。"""
        collect.adapter_for("sample_news").fetch(tmp_path)
        reused = collect.adapter_for("sample_news")
        reused.fetch(tmp_path)
        assert reused.min_docs == 1

    def test_it_reads_the_docs_it_staged(self, app, tmp_path):
        """meta の行は文書として数えない(remote の契約どおり)。"""
        adapter = collect.adapter_for("sample_news")
        path, _ = adapter.fetch(tmp_path)
        assert [d.title for d in adapter.iter_docs(path)] == ["見出し"]
