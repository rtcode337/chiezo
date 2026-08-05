"""サイドカー方式のプラグイン(`CHIEZO_PLUGIN_SOURCES`)のテスト。

本物の HTTP サーバーを立てて確かめる —— urllib のストリーム読みとヘッダの扱いが
契約の中身なので、そこをモックで置き換えると何も検証できなくなる。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar

import pytest

from sources import remote

DOCS = [
    {"doc_id": 7, "title": "那覇新都心郵便局", "body": "沖縄県那覇市",
     "tags": ["直営郵便局"], "rank_score": 0.5, "extra": {"lat": 26.2, "lon": 127.7}},
    {"title": "首里郵便局", "opening": "沖縄県那覇市首里"},
]


class _Handler(BaseHTTPRequestHandler):
    catalog: ClassVar[dict] = {}
    meta: ClassVar[dict | None] = None
    docs: ClassVar[list] = []

    def do_GET(self):
        if self.path == "/sources":
            body = json.dumps(self.catalog).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/fetch"):
            lines = [{"meta": self.meta}] if self.meta is not None else []
            lines += self.docs
            body = "".join(json.dumps(d, ensure_ascii=False) + "\n" for d in lines).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):  # テスト出力を汚さない
        pass


@pytest.fixture
def plugin():
    """テスト用のプラグインサーバー。`base` にベース URL を返す。"""
    _Handler.catalog = {"sources": [{
        "name": "post_office", "kind": "post_office", "lang": "ja",
        "label": "郵便局", "min_docs": 2, "memory_gb": 0.5,
    }]}
    _Handler.meta = {"dump_date": "20260805"}
    _Handler.docs = DOCS
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


class TestCatalog:
    def test_lists_sources_from_the_plugin(self, plugin):
        (src,) = remote.catalog(plugin)
        assert (src.name, src.kind, src.lang, src.label) == ("post_office", "post_office", "ja", "郵便局")
        assert src.min_docs == 2

    def test_empty_setting_means_no_plugins(self):
        assert remote.catalog("") == []
        assert remote.catalog("  ,  ") == []

    def test_unreachable_plugin_is_skipped(self, caplog):
        """**落ちていても本体は動く**(警告のみ)。別コンテナなので一時的な不通は正常。"""
        assert remote.catalog("http://127.0.0.1:9") == []
        assert "catalog unavailable" in caplog.text

    def test_malformed_catalog_is_an_error(self, plugin):
        """繋がったのに形が違うのは直すべき不具合なので、黙って無視しない。"""
        _Handler.catalog = {"sources": [{"name": "no-hyphens-allowed", "kind": "x"}]}
        with pytest.raises(remote.PluginError, match="invalid source name"):
            remote.catalog(plugin)

    def test_same_source_from_two_plugins_is_an_error(self, plugin):
        with pytest.raises(remote.PluginError, match="already provided"):
            remote.catalog(f"{plugin},{plugin}")


class TestAdapter:
    def test_fetch_and_iter_docs(self, plugin, tmp_path):
        adapter = remote.load_remote_adapters(plugin)["post_office"]()
        path, dump_date = adapter.fetch(tmp_path)

        assert dump_date == "20260805"
        assert path.name == "post_office-20260805.ndjson"

        docs = list(adapter.iter_docs(path))
        # meta の行は文書として数えない
        assert [d.title for d in docs] == ["那覇新都心郵便局", "首里郵便局"]
        assert docs[0].doc_id == 7
        assert docs[0].tags == ["直営郵便局"]
        assert docs[0].extra == {"lat": 26.2, "lon": 127.7}
        # doc_id を省いた行は行番号で埋める(元データに安定した id が無いソース向け)
        assert docs[1].doc_id == 3  # meta が 1 行目なので行番号は 3

    def test_missing_meta_falls_back_to_today(self, plugin, tmp_path):
        _Handler.meta = None
        adapter = remote.load_remote_adapters(plugin)["post_office"]()
        _, dump_date = adapter.fetch(tmp_path)
        assert len(dump_date) == 8 and dump_date.isdigit()

    def test_meta_line_overrides_the_catalog(self, plugin, tmp_path):
        """取り込んだ中身を見てからでないと代表を選べないソースのための口。

        **日本語のタイトルが通ること**まで見る(ヘッダで渡す設計はここで壊れた ——
        HTTP ヘッダは latin-1 しか運べない)。
        """
        _Handler.meta = {
            "dump_date": "20260805", "min_docs": 1, "sample_titles": ["那覇新都心郵便局"],
        }
        adapter = remote.load_remote_adapters(plugin)["post_office"]()
        adapter.fetch(tmp_path)
        assert adapter.min_docs == 1
        assert adapter.sample_titles == ["那覇新都心郵便局"]

    def test_broken_line_is_an_error(self, plugin, tmp_path):
        adapter = remote.load_remote_adapters(plugin)["post_office"]()
        path = tmp_path / "broken.ndjson"
        path.write_text('{"title": "ok"}\nnot json\n', encoding="utf-8")
        with pytest.raises(remote.PluginError, match="line 2"):
            list(adapter.iter_docs(path))

    def test_line_without_title_is_an_error(self, plugin, tmp_path):
        adapter = remote.load_remote_adapters(plugin)["post_office"]()
        path = tmp_path / "no-title.ndjson"
        path.write_text('{"body": "本文だけ"}\n', encoding="utf-8")
        with pytest.raises(remote.PluginError, match="must be an object with a title"):
            list(adapter.iter_docs(path))


class TestLookup:
    def test_get_adapter_finds_plugin_sources(self, plugin, monkeypatch):
        """`SOURCE=post_office` で取り込みが始められること(main.run が通る道)。"""
        import sources

        monkeypatch.setenv(remote.PLUGIN_ENV, plugin)
        adapter = sources.get_adapter("post_office")
        assert adapter.source == "post_office"
        assert adapter.source_kind == "post_office"

    def test_unknown_source_lists_plugin_sources_too(self, plugin, monkeypatch):
        import sources

        monkeypatch.setenv(remote.PLUGIN_ENV, plugin)
        with pytest.raises(SystemExit, match=r"post_office \(plugin\)"):
            sources.get_adapter("nope")
