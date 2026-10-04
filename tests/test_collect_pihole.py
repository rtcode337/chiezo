"""Pi-hole から引く巡回(`app/pihole.py` / `Sweep.use_pihole`)。

見ているのは、**集計を前の回に足して積み上げること**(読み直した重なりを二重に数えない)、
**新しく溜めるのは止めたものと気になる通信だけ**、**AI を呼ばない回として扱われること**。
本物の Pi-hole は叩かない —— 手元に立てた偽の口が、認証・集計・問い合わせの一覧を返す。
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from app import collect, pihole


def _q(domain: str, at: float, *, client: str = "192.0.2.10", qtype: str = "A",
       status: str = "FORWARDED", reply: str = "IP", name: str = "") -> dict:
    return {
        "id": int(at * 1000), "time": at, "domain": domain, "type": qtype, "status": status,
        "client": {"ip": client, "name": name}, "reply": {"type": reply},
    }


def _rows(*raw: dict) -> list[dict]:
    return [pihole._record(r) for r in raw]


class _Pihole(BaseHTTPRequestHandler):
    known: ClassVar[list[str]] = []
    queries: ClassVar[list[dict]] = []
    asked: ClassVar[list[dict]] = []
    logged_out: ClassVar[int] = 0

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if body.get("password") != "secret":
            self._send(401, {"session": {"valid": False}})
            return
        self._send(200, {"session": {"valid": True, "sid": "sid-1"}})

    def do_DELETE(self):
        _Pihole.logged_out += 1
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        assert self.headers.get("sid") == "sid-1"
        if url.path == "/api/stats/database/top_domains":
            self._send(200, {"domains": [{"domain": d, "count": 1} for d in _Pihole.known]})
            return
        if url.path == "/api/queries":
            _Pihole.asked.append(query)
            start, length = int(query["start"]), int(query["length"])
            self._send(200, {"queries": _Pihole.queries[start:start + length]})
            return
        self._send(404, {})


@pytest.fixture()
def server(monkeypatch):
    monkeypatch.setattr(pihole.settings_store, "api_key", lambda _name: None)
    _Pihole.known, _Pihole.queries, _Pihole.asked, _Pihole.logged_out = [], [], [], 0
    httpd = HTTPServer(("127.0.0.1", 0), _Pihole)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    monkeypatch.setenv(pihole.URL_ENV, f"http://127.0.0.1:{httpd.server_port}")
    monkeypatch.setenv(pihole.PASSWORD_ENV, "secret")
    yield
    httpd.shutdown()


class TestCredential:
    def test_the_url_and_the_password_are_split_at_the_first_space(self):
        assert pihole.parse_credential("http://pi.hole:80/ pass word") == ("http://pi.hole:80", "pass word")
        assert pihole.parse_credential("http://pi.hole") == ("http://pi.hole", "")

    def test_the_registered_key_comes_first(self, monkeypatch):
        monkeypatch.setattr(pihole.settings_store, "api_key", lambda _name: "http://pi.hole secret")
        assert pihole.target() == ("http://pi.hole", "secret")

    def test_no_target_is_refused_with_a_hint(self, monkeypatch):
        monkeypatch.setattr(pihole.settings_store, "api_key", lambda _name: None)
        monkeypatch.delenv(pihole.URL_ENV, raising=False)
        with pytest.raises(HTTPException) as e:
            pihole.target()
        assert "API キー" in e.value.detail["hint"]

    def test_a_refused_password_says_so_without_echoing_it(self, server, monkeypatch):
        monkeypatch.setenv(pihole.PASSWORD_ENV, "wrong")
        with pytest.raises(HTTPException) as e:
            pihole.harvest({})
        assert "パスワード" in e.value.detail["error"] and "wrong" not in str(e.value.detail)


class TestMerge:
    def test_counts_are_added_to_what_was_there(self):
        before = {
            "first_seen": "2026-10-01T00:00:00+00:00", "last_seen": "2026-10-02T00:00:00+00:00",
            "queries": 10, "blocked": 4,
            "clients": ["192.0.2.10"], "client_counts": [10], "client_names": ["tablet"],
        }
        later = time.time()
        merged = pihole.merge(before, _rows(
            _q("ads.example.com", later, status="GRAVITY"),
            _q("ads.example.com", later + 1, client="192.0.2.20", name="tv"),
        ))

        assert merged["queries"] == 12 and merged["blocked"] == 5
        assert merged["first_seen"] == "2026-10-01T00:00:00+00:00"
        assert merged["clients"] == ["192.0.2.10", "192.0.2.20"]
        assert merged["client_counts"] == [11, 1]
        assert merged["client_names"] == ["tablet", "tv"]

    def test_rows_already_counted_are_not_counted_again(self):
        """読み直しの重なり(最後に見た時刻より前の行)は足さない。"""
        at = time.time()
        before = {"last_seen": pihole._iso(at), "queries": 3}

        assert pihole.merge(before, _rows(_q("a.example.com", at - 30))) == {}
        assert pihole.merge(before, _rows(_q("a.example.com", at + 30)))["queries"] == 4


class TestItems:
    def test_only_blocked_or_watched_domains_are_newly_kept(self):
        now = time.time()
        rows = _rows(
            _q("ads.example.com", now - 60, status="GRAVITY"),
            _q("normal.example.com", now - 60),
            _q("new.example.net", now - 30),
        )
        known = {"normal.example.com", "ads.example.com"}

        items = {i["title"]: i for i in pihole.build_items({}, rows, rows, known, now)}

        assert set(items) == {"ads.example.com", "new.example.net"}
        assert pihole.BLOCKED_TAG in items["ads.example.com"]["tags"]
        assert "理由:はじめて見た" in items["new.example.net"]["tags"]
        assert items["new.example.net"]["extra"]["watched_at"]
        # 気になる通信を先に調べる
        assert items["new.example.net"]["extra"]["priority"] > items["ads.example.com"]["extra"]["priority"]

    def test_a_kept_domain_keeps_being_counted_even_when_normal(self):
        now = time.time()
        previous = {"once.example.com": {"title": "once.example.com", "extra": {
            "last_seen": pihole._iso(now - 3600), "queries": 1, "blocked": 1,
            "watched_at": pihole._iso(now - 86400 * 3),
        }}}
        rows = _rows(_q("once.example.com", now - 60))

        (item,) = pihole.build_items(previous, rows, rows, {"once.example.com"}, now)

        assert item["extra"]["queries"] == 2
        # 今回は挙がっていないので、前の理由は脇書きで入れ替えない(運ばない)
        assert "reasons" not in item["extra"]

    def test_reverse_lookups_are_left_out(self):
        now = time.time()
        rows = _rows(*[_q("10.2.0.192.in-addr.arpa", now - i, reply="NXDOMAIN") for i in range(9)])
        assert pihole.build_items({}, rows, rows, set(), now) == []

    def test_the_carried_facts_fit_what_a_doc_can_carry(self):
        """脇書きに運べる鍵の数・並びの長さの内側に収まる(`collect._carried`)。"""
        now = time.time()
        rows = _rows(*[_q("ads.example.com", now - i, status="GRAVITY", client=f"192.0.2.{i}")
                       for i in range(40)])
        (item,) = pihole.build_items({}, rows, rows, set(), now)

        assert len(item["extra"]) <= collect.MAX_CARRIED_KEYS
        assert collect._carried(item["extra"]) == item["extra"]


class TestDetectors:
    def test_repeated_nxdomain(self):
        now = time.time()
        window = _rows(*[_q("typo.example.org", now - i, reply="NXDOMAIN") for i in range(5)])
        kinds = [k for k, _ in pihole.reasons(window, {"typo.example.org"}, now)["typo.example.org"]]
        assert kinds == ["nxdomain"]

    def test_rare_query_type(self):
        now = time.time()
        window = _rows(*[_q("t.example.com", now - i, qtype="TXT") for i in range(5)])
        assert pihole.reasons(window, {"t.example.com"}, now)["t.example.com"][0][0] == "rare_qtype"

    def test_regular_beacon(self):
        now = time.time()
        window = _rows(*[_q("beacon.example.com", now - 300 * i) for i in range(8)])
        ((kind, detail),) = pihole.reasons(window, {"beacon.example.com"}, now)["beacon.example.com"]
        assert kind == "beacon" and "5 分おき" in detail

    def test_jittery_human_traffic_is_not_a_beacon(self):
        assert pihole.periodicity([0.0, 30, 400, 420, 1500, 1520, 3000, 3100]) is None

    def test_many_random_names_under_one_parent(self):
        names = {f"q{i:02d}x7k2m9v4b8n1c5z3w6r0t8y2u4i.data.example.com": 1 for i in range(12)}
        assert pihole.tunneling(names) == {"data.example.com": 12}

    def test_a_cdn_reusing_names_is_not_tunneling(self):
        names = {f"a{i:02d}q7x2m9v4b8n1c5z3w6r0t8y.cdn.example.com": 50 for i in range(12)}
        assert pihole.tunneling(names) == {}


class TestHarvest:
    def test_it_reads_from_the_last_seen_and_closes_the_session(self, server):
        now = time.time()
        _Pihole.known = ["ads.example.com"]
        _Pihole.queries = [_q("ads.example.com", now - 60, status="GRAVITY")]
        previous = {"ads.example.com": {"title": "ads.example.com", "extra": {
            "last_seen": pihole._iso(now - 3600 * 48), "queries": 5, "blocked": 5,
        }}}

        items, note = pihole.harvest(previous)

        assert [i["extra"]["blocked"] for i in items] == [6]
        assert "1 件の問い合わせ" in note
        # 窓(24 時間)より前から読む(最後に見た時刻から続ける)
        assert int(_Pihole.asked[0]["from"]) < now - 3600 * 47
        assert _Pihole.logged_out == 1

    def test_pages_are_followed(self, server, monkeypatch):
        monkeypatch.setattr(pihole, "PAGE_ROWS", 2)
        now = time.time()
        _Pihole.queries = [_q(f"n{i}.example.com", now - i) for i in range(5)]

        items, _note = pihole.harvest({})

        assert len(items) == 5


class TestSweep:
    def _item(self, **sweep):
        return collect.Collection(
            name="tazuna_pihole", description="", prompt="{unreviewed}", interval_minutes=60,
            enabled=False, backend=None, model=None, effort=None, web=False, cursor="",
            created_at="", updated_at="",
            sweeps=[{"name": "機械収集", "use_pihole": True, "only_new": True, **sweep}],
        )

    def test_it_is_a_machine_sweep_that_carries_facts(self):
        item = self._item()
        sweep = collect.sweeps_of(item)[0]

        assert sweep.use_pihole
        assert not collect.asks_ai(item, sweep)
        assert collect.carries_facts(item, sweep)
        assert collect.nothing_to_pass(item, sweep, {}) == ""

    def test_no_backend_is_kept_for_it(self):
        (made,) = collect.normalize_sweeps(
            [{"name": "機械収集", "use_pihole": True, "backend": "claude", "model": "sonnet"}]
        )
        assert made["backend"] is None and made["model"] is None
