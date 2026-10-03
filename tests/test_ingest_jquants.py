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
        # 素の会社名は extra に持つ(銘柄マスタとして読む側はこちらを使う)
        assert [d.extra["name"] for d in docs] == ["伊藤園", "伊藤園"]

    def test_a_full_width_name_is_found_in_half_width(self, api, tmp_path):
        """J-Quants は英数字を全角で返す。半角で打っても引けるようにする。"""
        (doc,) = self._docs(api, tmp_path, [_row("72400", "ＮＯＫ")])

        assert doc.title == "ＮＯＫ"
        assert "NOK" in doc.aliases

    def test_every_stock_carries_the_listed_tag(self, api, tmp_path):
        """filter は条件を 1 つ要求するので、全件はこのタグで引く。"""
        docs = self._docs(api, tmp_path, [
            _row("72030", "トヨタ自動車"), _row("13060", "TOPIX連動ETF", prod="014"),
        ])

        assert all("東証上場" in d.tags for d in docs)

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


class _EarningsApi(BaseHTTPRequestHandler):
    """`/fins/earnings-date` の代わり。`date`(公表日)の問い合わせはプランの判別、
    `scheduled_date` はその日を予定日とする会社を返す(`by_offset[何日目]`)。"""

    free: ClassVar[bool] = False
    by_offset: ClassVar[dict[int, list[tuple[str, str]]]] = {}
    seen_days: ClassVar[list[str]] = []

    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        if "date" in query:
            if _EarningsApi.free:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b'{"message": "Your subscription covers the following dates: ..."}')
                return
            rows: list[dict] = []
        else:
            day = query["scheduled_date"][0]
            _EarningsApi.seen_days.append(day)
            offset = len(_EarningsApi.seen_days) - 1
            rows = [
                {"PubDate": "2026-10-01", "SchDate": day, "FQName": "2Q", "FYE": "0331",
                 "Code": code, "CoName": name, "CoNameEn": ""}
                for code, name in _EarningsApi.by_offset.get(offset, [])
            ]
        raw = json.dumps({"data": rows}, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *_args):
        pass


@pytest.fixture()
def earnings(monkeypatch):
    import sources.jquants as jq

    monkeypatch.setattr(jq, "EARNINGS_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(jq, "EARNINGS_INTERVAL_FREE_SECONDS", 0)
    monkeypatch.setattr(jq, "EARNINGS_WINDOW_DAYS", 3)
    monkeypatch.setenv(API_KEY_ENV, "test-key")
    _EarningsApi.free, _EarningsApi.by_offset, _EarningsApi.seen_days = False, {}, []
    httpd = HTTPServer(("127.0.0.1", 0), _EarningsApi)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield jq.JquantsEarningsAdapter(base_url=f"http://127.0.0.1:{httpd.server_port}/v2")
    httpd.shutdown()


class TestEarnings:
    def test_it_asks_day_by_day_and_makes_one_doc_per_announcement(self, earnings, tmp_path):
        _EarningsApi.by_offset = {1: [("72030", "トヨタ自動車")], 3: [("72400", "ＮＯＫ")]}

        path, _date = earnings.fetch(tmp_path)
        docs = list(earnings.iter_docs(path))

        # 今日から 3 日先まで、予定日を 1 日ずつ聞く
        assert len(_EarningsApi.seen_days) == 4
        toyota, nok, status = docs
        day = _EarningsApi.seen_days[1]
        assert toyota.extra["announcement_date"] == day
        assert {"決算発表予定", day, day[:7], "2Q"} <= set(toyota.tags)
        assert "7203" in toyota.aliases
        assert "NOK" in nok.aliases
        # 有料プランで取ったので「全部」
        assert status.title == "J-Quants 決算発表予定日の取得状況"
        assert status.tags == ["取得状況"]
        assert (status.extra["plan"], status.extra["coverage"], status.extra["count"]) == ("paid", "full", 2)

    def test_a_free_plan_takes_what_it_can_see_and_says_so(self, earnings, tmp_path):
        """無料プランでも止めずに見える分だけ入れ、**一部だけであることを 1 件に残す**。
        読む側(pta)はそれを見て、売買提案の AI に「一部しか載っていない」と添えて渡す。"""
        _EarningsApi.free = True
        _EarningsApi.by_offset = {2: [("72030", "トヨタ自動車")]}

        path, _date = earnings.fetch(tmp_path)
        docs = list(earnings.iter_docs(path))

        assert len(_EarningsApi.seen_days) == 4
        toyota, status = docs
        assert toyota.extra["code"] == "7203"
        assert (status.extra["plan"], status.extra["coverage"], status.extra["count"]) == ("free", "partial", 1)
        assert "決算が無い" in status.body

    def test_a_free_plan_with_nothing_visible_still_bakes(self, earnings, tmp_path):
        """見える予定が 0 件の日でも、取得状況の 1 件があるので焼ける(空の理由が読める)。"""
        _EarningsApi.free = True
        path, date = earnings.fetch(tmp_path / "work")
        building = tmp_path / f"jquants_earnings-{date}.db.building"

        ingest_main.build_db(earnings, path, date, building)
        ingest_main.validate_db(earnings, building, 0)

    def test_the_older_material_without_a_status_line_counts_as_paid(self, earnings, tmp_path):
        """状況の行を持たない前の形の素材は、有料プランでしか焼けなかったもの。"""
        raw = tmp_path / "old.jsonl"
        raw.write_text(json.dumps({"SchDate": "2026-11-05", "Code": "72030", "CoName": "トヨタ自動車",
                                   "FQName": "2Q", "FYE": "0331"}, ensure_ascii=False) + "\n",
                       encoding="utf-8")

        *_rows, status = list(earnings.iter_docs(raw))
        assert status.extra["plan"] == "paid"

    def test_a_smaller_generation_still_bakes(self, earnings, tmp_path):
        """決算の山を過ぎると件数は大きく減る。それが本来の姿なので、減っても焼く。"""
        _EarningsApi.by_offset = {0: [("72030", "トヨタ自動車")]}
        path, date = earnings.fetch(tmp_path / "work")
        building = tmp_path / f"jquants_earnings-{date}.db.building"

        ingest_main.build_db(earnings, path, date, building)
        ingest_main.validate_db(earnings, building, 3000)  # 前の世代は 3,000 件あった

        conn = sqlite3.connect(building)
        try:
            # 予定 1 件 + 取得状況 1 件
            assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == 2
        finally:
            conn.close()
