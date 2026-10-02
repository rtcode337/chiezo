"""J-Quants の上場銘柄一覧(`jquants_master`)。

本物の API は叩かない —— 手元に立てた偽の口が、ページに分けて返す。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import main as ingest_main
import pytest

from sources.jquants import API_KEY_ENV, JquantsMasterAdapter, short_code


def _row(code: str, name: str, **kw) -> dict:
    return {
        "Date": "2026-10-01", "Code": code, "CoName": name, "CoNameEn": kw.get("en", ""),
        "S17": "6", "S17Nm": kw.get("s17", "自動車・輸送機"),
        "S33": "3700", "S33Nm": kw.get("s33", "輸送用機器"),
        "ScaleCat": kw.get("scale", "-"), "Mkt": "0111", "MktNm": kw.get("market", "プライム"),
        "Mrgn": "2", "MrgnNm": "貸借", "ProdCat": kw.get("prod", "011"),
    }


class _Api(BaseHTTPRequestHandler):
    pages: ClassVar[list[list[dict]]] = []
    status: ClassVar[int] = 200
    seen_keys: ClassVar[list[str]] = []

    def do_GET(self):
        _Api.seen_keys.append(self.headers.get("x-api-key") or "")
        if _Api.status != 200:
            self.send_response(_Api.status)
            self.end_headers()
            self.wfile.write(b'{"message": "nope"}')
            return
        query = parse_qs(urlparse(self.path).query)
        page = int((query.get("pagination_key") or ["0"])[0])
        body: dict = {"data": _Api.pages[page]}
        if page + 1 < len(_Api.pages):
            body["pagination_key"] = str(page + 1)
        raw = json.dumps(body, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def api(monkeypatch):
    import sources.jquants as jq

    monkeypatch.setattr(jq, "PAGE_INTERVAL_SECONDS", 0)
    monkeypatch.setenv(API_KEY_ENV, "test-key")
    _Api.pages, _Api.status, _Api.seen_keys = [], 200, []
    httpd = HTTPServer(("127.0.0.1", 0), _Api)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield JquantsMasterAdapter(base_url=f"http://127.0.0.1:{httpd.server_port}/v2")
    httpd.shutdown()


class TestFetch:
    def test_it_follows_every_page_and_sends_the_key(self, api, tmp_path):
        _Api.pages = [[_row("72030", "トヨタ自動車")], [_row("72670", "ホンダ")]]

        path, date = api.fetch(tmp_path)

        assert date == "20261001"
        assert [json.loads(line)["Code"] for line in path.read_text().splitlines()] == [
            "72030", "72670",
        ]
        assert _Api.seen_keys == ["test-key", "test-key"]

    def test_no_key_stops_before_asking(self, api, tmp_path, monkeypatch):
        monkeypatch.delenv(API_KEY_ENV)

        with pytest.raises(SystemExit, match=API_KEY_ENV):
            api.fetch(tmp_path)
        assert _Api.seen_keys == []

    def test_a_refused_key_says_so_without_echoing_it(self, api, tmp_path):
        _Api.status = 401

        with pytest.raises(SystemExit) as e:
            api.fetch(tmp_path)
        assert "API キー" in str(e.value) and "test-key" not in str(e.value)


class TestDocs:
    def _docs(self, api, tmp_path, rows):
        _Api.pages = [rows]
        path, _ = api.fetch(tmp_path)
        return list(api.iter_docs(path))

    def test_one_stock_is_one_doc_found_by_its_code(self, api, tmp_path):
        docs = self._docs(api, tmp_path, [
            _row("72030", "トヨタ自動車", en="TOYOTA MOTOR CORPORATION", scale="TOPIX Core30"),
        ])

        (doc,) = docs
        assert doc.title == "トヨタ自動車"
        assert {"7203", "72030", "TOYOTA MOTOR CORPORATION"} <= set(doc.aliases)
        assert {"プライム", "輸送用機器", "TOPIX Core30", "普通株式"} <= set(doc.tags)
        assert doc.extra["code"] == "7203" and doc.extra["sector33"] == "輸送用機器"
        assert doc.rank_score == 1.0
        assert "7203" in doc.opening and "輸送用機器" in doc.opening

    def test_a_preferred_share_does_not_steal_the_company_name(self, api, tmp_path):
        """優先株は普通株と同じ会社名で来る。普通株に素の名前を渡す。"""
        docs = self._docs(api, tmp_path, [
            _row("25935", "伊藤園", prod="011"),
            _row("25930", "伊藤園"),
        ])

        assert [d.title for d in docs] == ["伊藤園", "伊藤園(25935)"]

    def test_letters_in_the_code_are_fine(self, api, tmp_path):
        """2024 年以降の新しいコードは英字が入る(例: 130A)。"""
        (doc,) = self._docs(api, tmp_path, [_row("641A0", "NOK Group")])

        assert doc.extra["code"] == "641A"
        assert "641A" in doc.aliases

    def test_an_unknown_product_code_is_kept_as_is(self, api, tmp_path):
        (doc,) = self._docs(api, tmp_path, [_row("99990", "どこか", prod="099")])

        assert doc.extra["product"] == "099"

    def test_the_sample_is_picked_from_what_was_read(self, api, tmp_path):
        """銘柄は統廃合で消えるので、固定の名前で検証しない。"""
        self._docs(api, tmp_path, [
            _row("13010", "極洋"), _row("72030", "トヨタ自動車", scale="TOPIX Core30"),
        ])

        assert api.sample_titles == ["トヨタ自動車"]


def test_short_code():
    assert short_code("72030") == "7203"
    assert short_code("25935") == "25935"
    assert short_code("641A0") == "641A"


def test_it_builds_and_validates(api, tmp_path):
    """取ってきたものが、そのまま焼けて検証まで通る。"""
    _Api.pages = [
        [_row(f"{1000 + i}0", f"会社{i}") for i in range(1_500)],
        [_row(f"{3000 + i}0", f"会社{1_500 + i}", scale="TOPIX Core30" if i == 0 else "-")
         for i in range(1_000)],
    ]
    path, date = api.fetch(tmp_path / "work")
    building = tmp_path / f"jquants_master-{date}.db.building"

    ingest_main.build_db(api, path, date, building)
    ingest_main.validate_db(api, building)

    conn = sqlite3.connect(building)
    try:
        assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 2_500
        hit = conn.execute(
            "SELECT d.title FROM aliases a JOIN docs d ON d.doc_id = a.doc_id WHERE a.alias = ?",
            ("3000",),
        ).fetchone()
        assert hit == ("会社1500",)
    finally:
        conn.close()
