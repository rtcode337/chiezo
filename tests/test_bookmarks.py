"""読む側に外を叩かせないための口: はてなブックマーク数(`GET /v1/bookmarks`)と、
Wikimedia の絵の縮小版(`POST /v1/thumbs`)。外へは出ず、応答を差し替えて確かめる。"""
from __future__ import annotations

import io
import socket
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import bookmarks, feeds, thumbs


@pytest.fixture
def client(built_data_dir, monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def hatena(monkeypatch):
    """はてなの `count/entries` を演じる。呼ばれた URL の並びを控える。"""
    bookmarks.clear()
    monkeypatch.setattr(bookmarks, "MIN_INTERVAL", 0.0)
    calls: list[list[str]] = []
    known = {"https://example.com/a": 12, "https://example.com/b": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        urls = parse_qs(urlparse(str(request.url)).query)["url"]
        calls.append(urls)
        assert request.headers["user-agent"] == feeds.USER_AGENT
        return httpx.Response(200, json={u: known[u] for u in urls if u in known})

    monkeypatch.setattr(
        bookmarks, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler),
                                  headers={"User-Agent": feeds.USER_AGENT}),
    )
    yield calls
    bookmarks.clear()


class TestBookmarks:
    def test_counts_and_leaves_out_what_could_not_be_read(self, client, hatena):
        """0 は「誰もブックマークしていない」。引けなかった URL は返さない(0 と書かない)。"""
        got = client.get("/v1/bookmarks", params=[
            ("url", "https://example.com/a"), ("url", "https://example.com/b"),
            ("url", "https://example.com/unknown"),
        ]).json()
        assert got == {"counts": {"https://example.com/a": 12, "https://example.com/b": 0}}

    def test_remembers_for_an_hour(self, client, hatena):
        """同じ URL を続けて聞かれても、はてなへは 1 度しか行かない。"""
        for _ in range(2):
            client.get("/v1/bookmarks", params={"url": "https://example.com/a"})
        assert hatena == [["https://example.com/a"]]

    def test_asks_fifty_at_a_time(self, client, hatena):
        urls = [f"https://example.com/{i}" for i in range(120)]
        client.get("/v1/bookmarks", params=[("url", u) for u in urls])
        assert [len(batch) for batch in hatena] == [50, 50, 20]

    def test_refuses_too_many(self, client, hatena):
        urls = [f"https://example.com/{i}" for i in range(bookmarks.MAX_URLS + 1)]
        assert client.get("/v1/bookmarks", params=[("url", u) for u in urls]).status_code == 400
        assert hatena == []


def png() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (800, 600), (30, 120, 200)).save(out, "PNG")
    return out.getvalue()


@pytest.fixture
def commons(monkeypatch):
    """Wikimedia の置き場を演じる(名前は外のアドレスとして引ける)。"""
    monkeypatch.setattr(thumbs, "MIN_INTERVAL", 0.0)
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=png(), headers={"content-type": "image/png"})

    monkeypatch.setattr(
        thumbs, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
    )
    return calls


class TestThumbs:
    URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Sample.jpg/120px-Sample.jpg"

    def test_makes_once_and_serves_from_chiezo(self, client, commons):
        """同じ絵は 1 度だけ取り、2 回目からは置き場の写しの場所を返す。"""
        first = client.post("/v1/thumbs", json={"url": self.URL}).json()
        second = client.post("/v1/thumbs", json={"url": self.URL}).json()

        assert first == second
        assert first["thumb"].startswith("/v1/thumbs/") and first["thumb"].endswith(".webp")
        assert commons == [self.URL]
        served = client.get(first["thumb"])
        assert served.status_code == 200
        assert Image.open(io.BytesIO(served.content)).format == "WEBP"

    def test_says_what_the_other_side_answered(self, client, monkeypatch):
        """断られたら、相手が何と返したか(「HTTP 429」)を 502 に添える。取れているかの口にも出る。"""
        monkeypatch.setattr(thumbs, "MIN_INTERVAL", 0.0)
        monkeypatch.setattr(
            socket, "getaddrinfo",
            lambda host, *_a, **_k: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))],
        )

        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429)

        monkeypatch.setattr(
            thumbs, "_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False),
        )
        got = client.post("/v1/thumbs", json={"url": self.URL})
        assert got.status_code == 502
        assert got.json()["reason"] == "HTTP 429"

        # 断られたあとは外へ出ずに断り、休んでいることを返す
        again = client.post("/v1/thumbs", json={"url": self.URL})
        assert again.status_code == 502 and "まで頼みません" in again.json()["reason"]

        health = client.get("/v1/thumbs/health").json()
        assert health["last_reason"] == "HTTP 429"
        assert health["resting"]["upload.wikimedia.org"]

    def test_only_wikimedia(self, client, commons):
        """読む側から URL を受け取って外へ出る口なので、決めたホストの絵しか取りに行かない。"""
        for url in ("https://example.com/a.png", "http://upload.wikimedia.org/a.png",
                    "https://upload.wikimedia.org.example.com/a.png"):
            assert client.post("/v1/thumbs", json={"url": url}).status_code == 400
        assert commons == []
