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

    def test_too_many_requests_is_retried_later(self, serve):
        # 429 は一時的なもの。印を付けると二度と取りに行かず、絵が無いまま残っていた
        serve({"https://example.org/a.png": httpx.Response(429)})
        items = [{"title": "作品A", "image": "https://example.org/a.png"}]

        attach(True, items)
        assert "extra" not in items[0]

    def test_docs_marked_429_before_are_tried_again(self, serve):
        serve({"https://example.org/a.png": image_response()})
        items = [{"title": "作品A", "body": "直した"}]
        existing = {"作品A": {"image": "https://example.org/a.png", "thumb_failed": "HTTP 429"}}

        assert attach(True, items, existing) == 1
        assert items[0]["extra"]["thumb"]

    def test_wikimedia_images_are_taken_from_the_scaled_copy(self, serve):
        # 元の絵は数 MB あることがあり、続けて取ると 429 で断られやすい
        original = "https://upload.wikimedia.org/wikipedia/commons/5/52/Kakiage.jpg"
        scaled = (
            "https://upload.wikimedia.org/wikipedia/commons/thumb/5/52/Kakiage.jpg/330px-Kakiage.jpg"
        )
        calls = serve({scaled: image_response()})
        items = [{"title": "かき揚げ", "image": original}]

        assert attach(True, items) == 1
        assert calls == [scaled]
        # 置き場の名前は元の URL から(同じ絵は 1 度だけ)
        assert items[0]["extra"]["thumb"] == thumbs.public_path(
            __import__("hashlib").sha1(original.encode()).hexdigest() + ".webp"
        )

    def test_falls_back_to_the_original_when_there_is_no_scaled_copy(self, serve):
        # 元の絵が縮小版の幅より小さいと、縮小版は断られる
        original = "https://upload.wikimedia.org/wikipedia/commons/a/ab/Small.png"
        scaled = "https://upload.wikimedia.org/wikipedia/commons/thumb/a/ab/Small.png/330px-Small.png"
        calls = serve({scaled: httpx.Response(400), original: image_response()})
        items = [{"title": "小さい絵", "image": original}]

        assert attach(True, items) == 1
        assert calls == [scaled, original]

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


class TestAnyHost:
    def test_star_reads_pages_on_any_host(self, serve):
        # 1 件が公式サイトを指す収集(作品・イベント)は、ホストを名指しできない
        calls = serve({
            "https://anime.example/": httpx.Response(200, content=PAGE.encode()),
            "https://anime.example/img/event.png": image_response(),
        })
        items = [{"title": "作品A", "url": "https://anime.example/"}]

        assert attach({"pages_from": ["*"]}, items) == 1
        assert items[0]["extra"]["image"] == "https://anime.example/img/event.png"
        assert calls == ["https://anime.example/", "https://anime.example/img/event.png"]

    def test_star_leaves_feed_items_that_carry_an_image(self, serve):
        # 配信元が絵を配っているなら、ページまで取りに行かない
        calls = serve({})
        items = [{"title": "告知", "url": "https://a.example/1", "image": "https://a.example/1.png"}]

        assert attach({"pages_from": ["*"]}, items, from_feed=True) == 0
        assert calls == []

    def test_normalize_keeps_the_star(self):
        assert thumbs.normalize({"pages_from": ["*"]}) == {"pages_from": ["*"]}


def backfill(spec, items, waiting, from_feed=False):
    return asyncio.run(thumbs.backfill(thumbs.normalize(spec), items, waiting, from_feed))


class TestBackfill:
    def test_adds_extra_only_items_for_docs_not_returned(self, serve):
        serve({
            "https://anime.example/": httpx.Response(200, content=PAGE.encode()),
            "https://anime.example/img/event.png": image_response(),
        })
        items = [{"title": "今回の作品", "body": "本文"}]
        waiting = {
            "前からある作品": {"url": "https://anime.example/"},
            # 今回返ってきたものは attach の受け持ち
            "今回の作品": {"url": "https://anime.example/"},
        }

        assert backfill({"pages_from": ["*"]}, items, waiting) == 1

        added = items[1]
        assert added["title"] == "前からある作品"
        # 見出しと脇書きだけ(本文もタグも持たない)
        assert set(added) == {"title", "extra"}
        assert added["extra"]["thumb"].startswith("/v1/thumbs/")
        assert added["extra"]["image"] == "https://anime.example/img/event.png"

    def test_marks_what_it_could_not_make(self, serve):
        serve({"https://anime.example/": httpx.Response(404)})
        items: list = []

        assert backfill({"pages_from": ["*"]}, items, {"作品": {"url": "https://anime.example/"}}) == 0
        assert items[0]["title"] == "作品"
        assert "thumb_failed" in items[0]["extra"]

    def test_caps_each_run(self, serve, monkeypatch):
        monkeypatch.setattr(thumbs, "MAX_BACKFILL_PER_RUN", 1)
        calls = serve({
            "https://a.example/": httpx.Response(404),
            "https://b.example/": httpx.Response(404),
        })
        waiting = {"A": {"url": "https://a.example/"}, "B": {"url": "https://b.example/"}}

        backfill({"pages_from": ["*"]}, [], waiting)
        assert len(calls) == 1


def test_waiting_for_thumbs_picks_docs_without_a_thumb(tmp_path):
    """後から作る相手は、絵も作れなかった印も無く、取りに行く先を持つ 1 件だけ。"""
    import json
    import sqlite3
    from types import SimpleNamespace

    from app import collect

    path = tmp_path / "docs.sqlite"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE docs (doc_id INTEGER, title TEXT, body TEXT, tags TEXT,"
        " updated_at TEXT, extra TEXT)"
    )
    rows = [
        (1, "待っている", {"url": "https://a.example/"}, []),
        (2, "もう持っている", {"url": "https://a.example/", "thumb": "/v1/thumbs/x.webp"}, []),
        (3, "作れなかった", {"url": "https://a.example/", "thumb_failed": "HTTP 404"}, []),
        # 一時的な失敗(多すぎる)は取り直す
        (6, "断られた", {"url": "https://a.example/", "thumb_failed": "HTTP 429"}, []),
        (4, "行き先が無い", {}, []),
        (5, "消えた", {"url": "https://a.example/"}, [notes.REMOVED_TAG]),
    ]
    for doc_id, title, extra, tags in rows:
        con.execute(
            "INSERT INTO docs VALUES (?, ?, '', ?, '', ?)",
            (doc_id, title, json.dumps(tags), json.dumps(extra)),
        )
    con.commit()
    con.close()

    sources = {"x": SimpleNamespace(path=path)}
    assert sorted(collect.waiting_for_thumbs("x", sources, 10)) == ["待っている", "断られた"]


class TestHealth:
    """取れているかの控え(`thumbs.health`)。一時的な失敗は 1 件ごとの印に残らないので、ここで数える。"""

    def test_counts_failures_in_a_row_and_resets_when_one_is_taken(self, serve):
        serve({
            "https://example.org/a.png": httpx.Response(503),
            "https://example.org/b.png": httpx.Response(503),
            "https://example.org/c.png": httpx.ConnectError("つながらない"),
        })
        attach(True, [{"title": t, "image": f"https://example.org/{t}.png"} for t in "abc"])

        state = thumbs.health()
        assert state["failing"] == 3 and thumbs.failing(state)
        assert state["last_reason"] == "つながらない(ConnectError)"
        assert state["last_host"] == "example.org"

        serve({"https://example.org/d.png": image_response()})
        attach(True, [{"title": "d", "image": "https://example.org/d.png"}])
        state = thumbs.health()
        assert state["failing"] == 0 and state["last_ok_at"]
        assert not thumbs.failing(state)

    def test_a_missing_picture_is_not_a_failure_to_reach(self, serve):
        # 相手に届いたうえでの 404 は、その 1 枚の事情
        serve({"https://example.org/a.png": httpx.Response(404)})
        attach(True, [{"title": "a", "image": "https://example.org/a.png"}])
        assert thumbs.health().get("failing", 0) == 0

    def test_the_status_page_warns_when_it_keeps_failing(self, serve):
        from app.views import admin

        serve({f"https://example.org/{t}.png": httpx.Response(503) for t in "abc"})
        attach(True, [{"title": t, "image": f"https://example.org/{t}.png"} for t in "abc"])

        html = admin._thumbs_health_html()
        assert "3 回続けて" in html and "HTTP 503" in html

    def test_told_to_wait_stops_the_run_and_rests(self, serve):
        """「多すぎる」と言われたら、その回の残りは頼まず、その相手へはしばらく出ない。"""
        calls = serve({
            "https://example.org/a.png": httpx.Response(429, headers={"retry-after": "10"}),
            "https://example.org/b.png": image_response(),
        })
        items = [{"title": t, "image": f"https://example.org/{t}.png"} for t in "ab"]

        assert attach(True, items) == 0
        assert calls == ["https://example.org/a.png"]
        # 相手の言う 10 秒より長く休む
        until = thumbs.resting_until("example.org")
        assert until and "extra" not in items[1]

        # 休んでいる間は、次の回でも外へ出ない
        attach(True, [{"title": "b", "image": "https://example.org/b.png"}])
        assert calls == ["https://example.org/a.png"]

        # 1 回断られただけでも、休んでいる期限を状況の面に出す
        from app.views import admin
        assert "まで頼みません" in admin._thumbs_health_html()

        # 休み明けには頼む
        state = thumbs.health()
        state["resting"] = {"example.org": "2000-01-01T00:00:00+00:00"}
        thumbs.settings_store.set_flag(thumbs.HEALTH_FLAG, __import__("json").dumps(state))
        assert attach(True, [{"title": "b", "image": "https://example.org/b.png"}]) == 1

        from app.views import admin
        assert "まで頼みません" not in admin._thumbs_health_html()


class TestLog:
    """依頼の控え(`thumbs.recent_log`)。誰の頼みで・どの絵が・どうなったかを新しい順に残す。"""

    def test_keeps_who_asked_and_what_happened(self, serve):
        serve({
            "https://example.org/a.png": image_response(),
            "https://example.org/b.png": httpx.Response(404),
        })
        attach(True, [{"title": t, "image": f"https://example.org/{t}.png"} for t in "ab"])
        # 同じ絵をもう一度頼むと、置き場の写しを返すだけ(外へは出ない)
        asyncio.run(thumbs.attach(
            thumbs.normalize(True), [{"title": "c", "image": "https://example.org/a.png"}], {},
            False, caller="写真の依頼",
        ))

        entries = thumbs.recent_log()
        assert [(e["caller"], e["target"].rsplit("/", 1)[-1], e["result"]) for e in entries] == [
            ("写真の依頼", "a.png", "写しを返した"),
            ("不明", "b.png", "作れない(HTTP 404)"),
            ("不明", "a.png", "取った"),
        ]

        from app.views import admin
        html = admin._thumbs_log_html()
        assert "取った 1・オフライン版から 0・写しを返した 1・取れなかった 1" in html

    def test_keeps_only_the_newest(self, serve, monkeypatch):
        monkeypatch.setattr(thumbs, "MAX_LOG", 2)
        serve({f"https://example.org/{t}.png": image_response() for t in "abc"})
        attach(True, [{"title": t, "image": f"https://example.org/{t}.png"} for t in "abc"])

        assert [e["target"][-5:] for e in thumbs.recent_log()] == ["c.png", "b.png"]


def test_the_status_page_puts_pictures_under_ai(tmp_path, monkeypatch, built_data_dir):
    """外の絵の具合と依頼の控えは、状況の面の AI の節の下の「画像」の節に置く。"""
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as client:
        thumbs._log("写真の依頼", "https://example.org/a.png", "取った", ok=True)
        html = client.get("/admin/status").text

    ai, images = html.index('id="ai-head"'), html.index('id="images-head"')
    assert ai < images < html.index('id="thumbs-log"')
