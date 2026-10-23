"""外の絵を確かめた結果を覚える置き場(`app/image_memory.py`・短期記憶の `chiezo_images`)。

表紙・写真・収集のサムネイルのどれでも、取れなかった絵は覚えて、確かめ直す日まで外へ出ない。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from app import image_memory, thumbs
from tests.test_thumbs import image_response, serve  # noqa: F401


@pytest.fixture
def remembered(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    return image_memory


def attach(items):
    return asyncio.run(thumbs.attach(thumbs.normalize(True), items, {}, False))


def test_a_missing_picture_is_not_asked_again(serve, remembered):  # noqa: F811
    calls = serve({"https://example.org/a.png": httpx.Response(404)})

    attach([{"title": "a", "image": "https://example.org/a.png"}])
    items = [{"title": "b", "image": "https://example.org/a.png"}]
    attach(items)

    assert calls == ["https://example.org/a.png"]
    assert "前に確かめて無かった" in items[0]["extra"]["thumb_failed"]
    assert remembered.recall("https://example.org/a.png")["found"] is False


def test_it_is_checked_again_after_a_while(serve, remembered, monkeypatch):  # noqa: F811
    calls = serve({"https://example.org/a.png": httpx.Response(404)})
    attach([{"title": "a", "image": "https://example.org/a.png"}])
    monkeypatch.setattr(remembered, "RECHECK_DAYS", 0)
    attach([{"title": "b", "image": "https://example.org/a.png"}])
    assert len(calls) == 2


def test_a_passing_failure_is_not_remembered(serve, remembered):  # noqa: F811
    """多すぎる(429)・つながらないは一時的なもの。覚えると、取れる絵を 30 日取りに行かなくなる。"""
    serve({"https://example.org/a.png": httpx.Response(429)})
    attach([{"title": "a", "image": "https://example.org/a.png"}])
    assert remembered.recall("https://example.org/a.png") is None


def test_a_taken_picture_is_remembered_with_where_it_came_from(serve, remembered):  # noqa: F811
    serve({"https://example.org/a.png": image_response()})
    items = [{"title": "a", "image": "https://example.org/a.png"}]
    attach(items)
    got = remembered.recall("https://example.org/a.png")
    assert got["found"] and got["thumb"] == items[0]["extra"]["thumb"] and got["how"] == "取った"


def test_it_is_a_short_term_source(tmp_path, monkeypatch, built_data_dir, serve):  # noqa: F811
    """短期記憶の名前付きの置き場として起動時に作られ、タグで引ける。"""
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    serve({"https://example.org/a.png": httpx.Response(404)})
    from app.main import app

    with TestClient(app) as client:
        attach([{"title": "a", "image": "https://example.org/a.png"}])
        got = client.get("/v1/chiezo_images/filter", params={"tag": "無かった"}).json()
        assert got["total"] == 1
        assert got["results"][0]["title"] == image_memory.key_of("https://example.org/a.png")
        hosts = client.get("/v1/chiezo_images/filter", params={"tag": "ホスト:example.org"}).json()
        assert hosts["total"] == 1
