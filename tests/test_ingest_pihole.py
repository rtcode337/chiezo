"""Pi-hole の止めたドメインと気になる通信(`pihole`)。

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

from sources.pihole import (
    BLOCKED_TAG,
    PASSWORD_ENV,
    URL_ENV,
    WATCH_TAG,
    PiholeAdapter,
    entropy,
    parse_credential,
    periodicity,
    tunneling,
)


class _Pihole(BaseHTTPRequestHandler):
    password: ClassVar[str] = "secret"
    # (blocked?, 窓の前か?) → [{"domain", "count"}]
    top: ClassVar[dict] = {}
    queries: ClassVar[list[dict]] = []
    seen: ClassVar[list[str]] = []
    logged_out: ClassVar[int] = 0

    def _send(self, status: int, body: dict) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or b"{}")
        if body.get("password") != _Pihole.password:
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
        _Pihole.seen.append(f"{url.path} sid={self.headers.get('sid')}")
        if url.path == "/api/stats/database/top_domains":
            blocked = query["blocked"] == "true"
            # 窓の前(「前から知っている」を引く回)は until が now より 1 日早い
            before = int(query["until"]) < time.time() - 3600
            self._send(200, {"domains": _Pihole.top.get((blocked, before), [])})
            return
        if url.path == "/api/queries":
            start, length = int(query["start"]), int(query["length"])
            self._send(200, {"queries": _Pihole.queries[start:start + length]})
            return
        self._send(404, {})

    def log_message(self, *_args):
        pass


@pytest.fixture()
def pihole(monkeypatch):
    import sources.pihole as ph

    monkeypatch.setattr(ph, "PAGE_INTERVAL_SECONDS", 0)
    monkeypatch.delenv(URL_ENV, raising=False)
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    _Pihole.top, _Pihole.queries, _Pihole.seen, _Pihole.logged_out = {}, [], [], 0
    httpd = HTTPServer(("127.0.0.1", 0), _Pihole)
    httpd.timeout = 0.05
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    adapter = PiholeAdapter()
    adapter.credential = f"http://127.0.0.1:{httpd.server_port} secret"
    yield adapter
    httpd.shutdown()


def _q(domain: str, at: float, *, client: str = "192.0.2.10", qtype: str = "A",
       status: str = "FORWARDED", reply: str = "IP", name: str = "") -> dict:
    return {
        "id": int(at * 1000), "time": at, "domain": domain, "type": qtype, "status": status,
        "client": {"ip": client, "name": name}, "reply": {"type": reply},
    }


def _docs(adapter, tmp_path):
    path, _ = adapter.fetch(tmp_path)
    return {d.title: d for d in adapter.iter_docs(path)}


class TestCredential:
    def test_the_url_and_the_password_are_split_at_the_first_space(self):
        """パスワードに空白があっても、最初の空白で 1 回だけ切る。"""
        assert parse_credential("http://pi.hole:80/ pass word") == ("http://pi.hole:80", "pass word")
        assert parse_credential("http://pi.hole") == ("http://pi.hole", "")

    def test_no_target_stops_before_asking(self, tmp_path):
        with pytest.raises(SystemExit, match=URL_ENV):
            PiholeAdapter().fetch(tmp_path)

    def test_a_refused_password_says_so_without_echoing_it(self, pihole, tmp_path):
        pihole.credential = pihole.credential.replace("secret", "wrong")

        with pytest.raises(SystemExit) as e:
            pihole.fetch(tmp_path)
        assert "パスワード" in str(e.value) and "wrong" not in str(e.value)

    def test_the_session_is_sent_and_closed(self, pihole, tmp_path):
        """Pi-hole は同時に開けるセッションに上限があるので、取り込みのたびに閉じる。"""
        pihole.fetch(tmp_path)

        assert all(s.endswith("sid=sid-1") for s in _Pihole.seen)
        assert _Pihole.logged_out == 1


class TestDocs:
    def test_every_blocked_domain_becomes_a_doc(self, pihole, tmp_path):
        _Pihole.top = {(True, False): [{"domain": "ads.example.com", "count": 120}]}

        docs = _docs(pihole, tmp_path)

        doc = docs["ads.example.com"]
        assert BLOCKED_TAG in doc.tags and WATCH_TAG not in doc.tags
        assert "Pi-hole" in doc.tags
        assert doc.extra["blocked_count"] == 120
        assert "120 回止めた" in doc.opening

    def test_a_domain_never_seen_before_is_watched(self, pihole, tmp_path):
        now = time.time()
        _Pihole.top = {(False, True): [{"domain": "known.example.com", "count": 3}]}
        _Pihole.queries = [
            _q("new.example.net", now - 7200, name="tablet"),
            _q("known.example.com", now - 60),
        ]

        docs = _docs(pihole, tmp_path)

        doc = docs["new.example.net"]
        assert WATCH_TAG in doc.tags and "理由:はじめて見た" in doc.tags
        assert "はじめて見た" in doc.opening
        # 端末は名前があれば添える
        assert doc.extra["clients"] == ["tablet(192.0.2.10): 1 回"]
        assert "known.example.com" not in docs

    def test_a_blocked_domain_is_not_new_just_because_it_was_blocked(self, pihole, tmp_path):
        """許可したぶんだけを見ると、止め続けているドメインが窓に入った瞬間に「はじめて見た」になる。"""
        now = time.time()
        _Pihole.top = {(True, True): [{"domain": "ads.example.com", "count": 50}]}
        _Pihole.queries = [_q("ads.example.com", now - 60, status="GRAVITY")]

        docs = _docs(pihole, tmp_path)

        assert "ads.example.com" not in docs

    def test_repeated_nxdomain_is_watched(self, pihole, tmp_path):
        now = time.time()
        _Pihole.top = {(False, True): [{"domain": "typo.example.org", "count": 1}]}
        _Pihole.queries = [_q("typo.example.org", now - i, reply="NXDOMAIN") for i in range(5)]

        doc = _docs(pihole, tmp_path)["typo.example.org"]

        assert "理由:存在しない名前" in doc.tags
        assert "存在しない名前として 5 回" in doc.opening

    def test_reverse_lookups_are_left_out(self, pihole, tmp_path):
        """逆引きは Pi-hole 自身が出すもので、手元に名前解決が無ければ必ず NXDOMAIN になる。"""
        now = time.time()
        _Pihole.queries = [_q("10.2.0.192.in-addr.arpa", now - i, reply="NXDOMAIN") for i in range(9)]

        assert _docs(pihole, tmp_path) == {}

    def test_a_rare_query_type_is_watched(self, pihole, tmp_path):
        now = time.time()
        _Pihole.top = {(False, True): [{"domain": "t.example.com", "count": 1}]}
        _Pihole.queries = [_q("t.example.com", now - i, qtype="TXT") for i in range(5)]

        doc = _docs(pihole, tmp_path)["t.example.com"]

        assert "理由:珍しい種別" in doc.tags and "TXT" in doc.opening

    def test_a_regular_beacon_is_watched(self, pihole, tmp_path):
        now = time.time()
        _Pihole.top = {(False, True): [{"domain": "beacon.example.com", "count": 1}]}
        _Pihole.queries = [_q("beacon.example.com", now - 300 * i) for i in range(8)]

        doc = _docs(pihole, tmp_path)["beacon.example.com"]

        assert "理由:規則正しい周期" in doc.tags
        assert "5 分おき" in doc.opening

    def test_many_random_names_under_one_parent_are_watched(self, pihole, tmp_path):
        now = time.time()
        names = [f"q{i:02d}x7k2m9v4b8n1c5z3w6r0t8y2u4i.data.example.com" for i in range(12)]
        _Pihole.queries = [_q(n, now - i) for i, n in enumerate(names)]
        _Pihole.top = {(False, True): [{"domain": n, "count": 1} for n in names]}

        doc = _docs(pihole, tmp_path)["data.example.com"]

        assert "理由:毎回ちがう名前" in doc.tags
        assert "12 個" in doc.opening

    def test_watched_ones_come_first(self, pihole, tmp_path):
        now = time.time()
        _Pihole.top = {(True, False): [{"domain": "ads.example.com", "count": 999}]}
        _Pihole.queries = [_q("new.example.net", now - 60)]

        path, _ = pihole.fetch(tmp_path)
        titles = [d.title for d in pihole.iter_docs(path)]

        assert titles == ["new.example.net", "ads.example.com"]
        # 引く側が調べる順に使う重みも、同じ並びになる
        docs = {d.title: d for d in pihole.iter_docs(path)}
        assert docs["new.example.net"].extra["priority"] > docs["ads.example.com"].extra["priority"]
        assert pihole.sample_titles == ["new.example.net"]

    def test_pages_are_followed(self, pihole, tmp_path, monkeypatch):
        import sources.pihole as ph

        monkeypatch.setattr(ph, "PAGE_ROWS", 2)
        now = time.time()
        _Pihole.queries = [_q(f"n{i}.example.com", now - i) for i in range(5)]

        assert len(_docs(pihole, tmp_path)) == 5


class TestDetectors:
    def test_entropy_tells_random_labels_from_words(self):
        assert entropy("q7x2m9v4b8n1c5z3w6r0t8y2u4") > 3.5
        assert entropy("aaaaaaaaaa") == 0.0

    def test_jittery_human_traffic_is_not_a_beacon(self):
        times = [0, 30, 400, 420, 1500, 1520, 3000, 3100]
        assert periodicity([float(t) for t in times]) is None

    def test_same_shot_queries_fold_into_one(self):
        """A / AAAA / HTTPS は同時に飛ぶので 1 回に畳む。"""
        times = []
        for i in range(8):
            times += [i * 60.0, i * 60.0 + 0.2]
        median, n = periodicity(times)
        assert median == pytest.approx(59.8) and n == 8

    def test_a_cdn_reusing_names_is_not_tunneling(self):
        """CDN は同じ名前を繰り返し引く(異なる名前 ÷ 問い合わせ回数が小さい)。"""
        names = {f"a{i:02d}q7x2m9v4b8n1c5z3w6r0t8y.cdn.example.com": 50 for i in range(12)}
        assert tunneling(names) == {}
