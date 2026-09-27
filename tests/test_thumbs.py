"""サムネイル(`app/thumbs.py`)。外へは出ず、応答を差し替えて確かめる。"""
from __future__ import annotations

import asyncio
import io
import socket

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from app import feeds, notes, thumbs

# 外のアドレスに見える値(名前を引いた結果として返す)
PUBLIC = "93.184.216.34"


def png(width=800, height=600) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(out, "PNG")
    return out.getvalue()


PAGE = """<html><head>
<meta property="og:title" content="勉強会">
<meta property="og:image" content="/img/event.png">
</head><body>本文</body></html>"""


@pytest.fixture
def serve(monkeypatch, tmp_path):
    """外へ出ずに応答を差し替える。名前は外のアドレスとして引ける(`lan.` だけ手元)。"""
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(thumbs, "MIN_INTERVAL", 0.0)

    def fake_getaddrinfo(host, *_args, **_kwargs):
        address = "10.0.0.5" if host.startswith("lan.") else PUBLIC
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    def make(responses: dict):
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
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
        return calls

    return make


def image_response(data: bytes | None = None, content_type="image/png"):
    return httpx.Response(200, content=data or png(), headers={"content-type": content_type})


def attach(spec, items, existing=None, from_feed=False):
    return asyncio.run(thumbs.attach(thumbs.normalize(spec), items, existing or {}, from_feed))


class TestNormalize:
    def test_off_by_default(self):
        assert thumbs.normalize(None) is None
        assert thumbs.normalize(False) is None

    def test_true_means_ai_images_only(self):
        assert thumbs.normalize(True) == {"pages_from": []}

    def test_page_hosts_are_cleaned(self):
        assert thumbs.normalize({"pages_from": [" Connpass.com ", ".connpass.com"]}) == {
            "pages_from": ["connpass.com"]
        }

    def test_rejects_other_shapes(self):
        with pytest.raises(HTTPException):
            thumbs.normalize("connpass.com")


class TestAttach:
    def test_shrinks_the_image_the_ai_returned(self, serve):
        calls = serve({"https://example.org/poster.png": image_response()})
        items = [{"title": "作品A", "image": "https://example.org/poster.png"}]

        assert attach(True, items) == 1

        thumb = items[0]["extra"]["thumb"]
        assert thumb.startswith("/v1/thumbs/") and thumb.endswith(".webp")
        with Image.open(thumbs.resolve(thumb.rsplit("/", 1)[1])) as small:
            # 長辺を縮める(縦横比はそのまま)
            assert small.size == (thumbs.SIZE, 240)
            assert small.format == "WEBP"
        assert calls == ["https://example.org/poster.png"]

    def test_reads_og_image_only_from_named_hosts(self, serve):
        calls = serve({
            "https://group.connpass.com/event/1/": httpx.Response(200, content=PAGE.encode()),
            "https://group.connpass.com/img/event.png": image_response(),
        })
        items = [
            {"title": "勉強会", "url": "https://group.connpass.com/event/1/"},
            # 名指ししていないホストのページは読まない
            {"title": "別の告知", "url": "https://other.example/event/2"},
        ]

        assert attach({"pages_from": ["connpass.com"]}, items, from_feed=True) == 1

        assert items[0]["extra"]["image"] == "https://group.connpass.com/img/event.png"
        assert items[0]["extra"]["thumb"].startswith("/v1/thumbs/")
        assert "extra" not in items[1]
        assert calls == [
            "https://group.connpass.com/event/1/", "https://group.connpass.com/img/event.png",
        ]

    def test_feed_images_are_left_as_they_are(self, serve):
        # 配信元が自分で配っている絵は、読む側がそのまま指せばよい
        calls = serve({})
        items = [{"title": "告知", "image": "https://example.org/a.png"}]

        assert attach(True, items, from_feed=True) == 0
        assert calls == []

    def test_once_per_doc(self, serve):
        # 作ったものにも、作れなかったものにも、二度と取りに行かない
        calls = serve({})
        items = [
            {"title": "作品A", "image": "https://example.org/a.png"},
            {"title": "作品B", "image": "https://example.org/b.png"},
        ]
        existing = {"作品A": {"thumb": "/v1/thumbs/x.webp"}, "作品B": {"thumb_failed": "HTTP 404"}}

        assert attach(True, items, existing) == 0
        assert calls == []

    def test_uses_the_image_already_on_the_doc(self, serve):
        # 整理の回は絵を返さないことがある(持っている絵から作る)
        serve({"https://example.org/a.png": image_response()})
        items = [{"title": "作品A", "body": "口コミを足した"}]

        assert attach(True, items, {"作品A": {"image": "https://example.org/a.png"}}) == 1
        assert items[0]["extra"]["thumb"]

    def test_marks_what_is_not_an_image(self, serve):
        serve({"https://example.org/a.png": image_response(b"<html></html>", "text/html")})
        items = [{"title": "作品A", "image": "https://example.org/a.png"}]

        assert attach(True, items) == 0
        assert items[0]["extra"]["thumb_failed"].startswith("絵ではない")

    def test_marks_missing_pages(self, serve):
        serve({"https://example.org/a.png": httpx.Response(404)})
        items = [{"title": "作品A", "image": "https://example.org/a.png"}]

        attach(True, items)
        assert items[0]["extra"]["thumb_failed"] == "HTTP 404"

    def test_retries_later_when_the_other_side_is_down(self, serve):
        # 一時的なものには印を付けない(次の回に取り直す)
        serve({
            "https://example.org/a.png": httpx.Response(503),
            "https://example.org/b.png": httpx.ConnectError("つながらない"),
        })
        items = [
            {"title": "作品A", "image": "https://example.org/a.png"},
            {"title": "作品B", "image": "https://example.org/b.png"},
        ]

        attach(True, items)
        assert "extra" not in items[0] and "extra" not in items[1]

    def test_refuses_the_local_network(self, serve):
        # URL は AI や外のページから来る。そのまま叩くと LAN の機器を叩かされうる
        calls = serve({})
        items = [{"title": "作品A", "image": "http://lan.example/a.png"}]

        attach(True, items)
        assert items[0]["extra"]["thumb_failed"].startswith("手元のネットワーク")
        assert calls == []

    def test_checks_every_redirect(self, serve):
        calls = serve({
            "https://example.org/a.png": httpx.Response(
                302, headers={"location": "http://lan.example/secret.png"}
            ),
        })
        items = [{"title": "作品A", "image": "https://example.org/a.png"}]

        attach(True, items)
        assert items[0]["extra"]["thumb_failed"].startswith("手元のネットワーク")
        assert calls == ["https://example.org/a.png"]

    def test_skips_tombstones(self, serve):
        calls = serve({})
        items = [{"title": "作品A", "image": "https://example.org/a.png", "tags": [notes.TOMBSTONE_TAG]}]

        assert attach(True, items) == 0
        assert calls == []

    def test_caps_each_run(self, serve, monkeypatch):
        # 残りは次の回に回る(取り込みを待たせない)
        monkeypatch.setattr(thumbs, "MAX_PER_RUN", 1)
        serve({"https://example.org/a.png": image_response()})
        items = [
            {"title": "作品A", "image": "https://example.org/a.png"},
            {"title": "作品B", "image": "https://example.org/b.png"},
        ]

        assert attach(True, items) == 1
        assert "extra" not in items[1]


class TestResolve:
    def test_only_names_it_made(self, serve):
        serve({})
        for name in ("../settings.db", "x.webp", "a" * 40 + ".png"):
            with pytest.raises(HTTPException):
                thumbs.resolve(name)


def test_og_image_from_the_page():
    assert thumbs.page_image_url(PAGE, "https://a.example/event/1/") == "https://a.example/img/event.png"
    assert thumbs.page_image_url("<html></html>", "https://a.example/") == ""
