"""1 件の URL を確かめる(`app/link_checks.py`)。外へは出ず、応答を差し替えて確かめる。"""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3

import httpx
import pytest

from app import feeds, link_checks, notes, thumbs

PUBLIC = "93.184.216.34"


@pytest.fixture
def serve(monkeypatch, tmp_path):
    """外へ出ずに応答を差し替える(`tests/test_thumbs.py` と同じ作り)。"""
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(thumbs, "MIN_INTERVAL", 0.0)

    def fake_getaddrinfo(host, *_args, **_kwargs):
        if host.startswith("gone."):
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def make(responses: dict):
        async def handler(request: httpx.Request) -> httpx.Response:
            response = responses[str(request.url)]
            if isinstance(response, Exception):
                raise response
            return response

        def client():
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                headers={"User-Agent": feeds.USER_AGENT},
                follow_redirects=False,
            )

        monkeypatch.setattr(thumbs, "_client", client)

    return make


def page(title: str, charset: str = "utf-8") -> httpx.Response:
    body = f'<html><head><meta charset="{charset}"><title>{title}</title></head></html>'
    return httpx.Response(200, content=body.encode(charset), headers={"content-type": "text/html"})


def collection_db(tmp_path, rows) -> str:
    path = tmp_path / "docs.sqlite"
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE docs (doc_id INTEGER, title TEXT, tags TEXT, extra TEXT)")
    for n, (extra, tags) in enumerate(rows, 1):
        con.execute(
            "INSERT INTO docs VALUES (?, ?, ?, ?)", (n, f"店{n}", json.dumps(tags), json.dumps(extra))
        )
    con.commit()
    con.close()
    return str(path)


def tick(paths):
    return asyncio.run(link_checks.tick(paths))


class TestCheck:
    def test_records_status_redirect_and_title(self, serve, tmp_path):
        serve({
            "http://shop.example/": httpx.Response(301, headers={"location": "https://shop.example/"}),
            "https://shop.example/": page("キッチン 例"),
            "https://review.example/1": httpx.Response(404),
        })
        path = collection_db(tmp_path, [
            ({"url": "https://review.example/1", "website": "http://shop.example/"}, []),
        ])

        assert tick([path]) == 2

        found = link_checks.lookup(["http://shop.example/", "https://review.example/1"])
        assert found["http://shop.example/"]["status"] == "ok"
        # http から https へ移ったものは、読む側が https のほうを使える
        assert found["http://shop.example/"]["final_url"] == "https://shop.example/"
        assert found["http://shop.example/"]["title"] == "キッチン 例"
        assert found["https://review.example/1"]["status"] == "HTTP 404"

    def test_refused_is_not_dead(self, serve, tmp_path):
        # 機械の取得を断るサイトでも、人のブラウザでは開ける
        serve({"https://strict.example/": httpx.Response(403)})
        path = collection_db(tmp_path, [({"url": "https://strict.example/"}, [])])

        tick([path])
        assert link_checks.lookup(["https://strict.example/"])["https://strict.example/"][
            "status"
        ].startswith("断られた")

    def test_missing_host_is_dead(self, serve, tmp_path):
        serve({})
        path = collection_db(tmp_path, [({"url": "https://gone.example/"}, [])])

        tick([path])
        assert link_checks.lookup(["https://gone.example/"])["https://gone.example/"][
            "status"
        ].startswith("名前を引けない")

    def test_transient_failures_count_up_before_giving_up(self, serve, tmp_path):
        serve({"https://down.example/": httpx.Response(503)})
        path = collection_db(tmp_path, [({"url": "https://down.example/"}, [])])

        for _ in range(link_checks.MAX_TRANSIENT - 1):
            tick([path])
            # 途中のものは「分からない」(返さない)
            assert link_checks.lookup(["https://down.example/"]) == {}
        tick([path])
        status = link_checks.lookup(["https://down.example/"])["https://down.example/"]["status"]
        assert status.startswith("つながらない")

    def test_checks_once_until_it_gets_old(self, serve, tmp_path):
        serve({"https://shop.example/": page("店")})
        path = collection_db(tmp_path, [({"url": "https://shop.example/"}, [])])

        assert tick([path]) == 1
        assert tick([path]) == 0

    def test_skips_removed_docs_and_non_http(self, tmp_path):
        path = collection_db(tmp_path, [
            ({"url": "https://a.example/"}, [notes.REMOVED_TAG]),
            ({"url": "ftp://b.example/"}, []),
            ({"website": "https://c.example/"}, []),
        ])
        assert link_checks.collection_urls(path) == ["https://c.example/"]

    def test_caps_each_tick(self, serve, tmp_path, monkeypatch):
        monkeypatch.setattr(link_checks, "MAX_PER_TICK", 1)
        serve({"https://a.example/": page("a"), "https://b.example/": page("b")})
        path = collection_db(tmp_path, [({"url": "https://a.example/"}, []), ({"url": "https://b.example/"}, [])])

        assert tick([path]) == 1
        assert tick([path]) == 1
        assert tick([path]) == 0


def test_page_title_reads_the_charset_and_prefers_og_title():
    body = (
        '<html><head><meta charset="shift_jis"><title>題名</title>'
        '<meta property="og:title" content="店の名前"></head></html>'
    ).encode("shift_jis")
    assert link_checks.page_title(body, "text/html") == "店の名前"
    assert link_checks.page_title(b"<title> a\n b </title>", "text/html; charset=utf-8") == "a b"
