"""手元(スマホ)で足りないものを補う殻(`app/pages.py`)。

管理画面は JS を持たない流儀だが、ここは見た目の話ではなく**戻る手段が無い**
という話 —— ホーム画面から開くとブラウザの帯ごと消えるので、戻るも進むも
読み直しもできなくなる。あわせて、引っ張って読み直せるようにする。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(data))
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestTheShellCarriesIt:
    def test_every_page_gets_it(self, client):
        """殻に入れてあるので、面を増やしても付け忘れない。"""
        for path in ("/admin", "/admin/memory", "/admin/todo", "/admin/ai"):
            html = client.get(path).text
            assert "pull-mark" in html, path
            assert "app-nav" in html, path

    def test_it_reads_nothing_from_outside(self, client):
        """LAN 内・オフラインで動く前提。外の部品を読みに行かない。"""
        html = client.get("/admin").text
        assert "http://" not in html.split("<script>")[-1]
        assert "cdn" not in html.lower()


class TestPullingToRefresh:
    def test_it_only_grabs_at_the_very_top(self, client):
        """途中で掴むと、下へスクロールしたいだけの指が読み直しになる。"""
        html = client.get("/admin").text
        assert "window.scrollY <= 0" in html

    def test_it_does_not_grab_inside_an_input(self, client):
        """選択やカーソル移動を邪魔しない。"""
        html = client.get("/admin").text
        assert "'INPUT'" in html and "'TEXTAREA'" in html

    def test_it_needs_a_real_pull(self, client):
        """触れただけで読み直すと、書きかけの入力が消える。"""
        html = client.get("/admin").text
        assert "PULL = 70" in html

    def test_it_does_nothing_without_touch(self, client):
        assert "'ontouchstart' in window" in client.get("/admin").text


class TestTheNavigationBar:
    def test_it_shows_only_when_the_browser_bar_is_gone(self, client):
        html = client.get("/admin").text
        assert "(display-mode: standalone)" in html
        assert "navigator.standalone" in html

    def test_it_has_back_reload_forward(self, client):
        html = client.get("/admin").text
        for call in ("history.back()", "location.reload()", "history.forward()"):
            assert call in html, call

    def test_it_makes_room_so_the_footer_is_not_covered(self, client):
        html = client.get("/admin").text
        assert "has-app-nav" in html
        assert "body.has-app-nav { padding-bottom" in html


class TestBeingAddedToTheHomeScreen:
    def test_the_manifest_says_to_drop_the_browser_bar(self, client):
        res = client.get("/manifest.webmanifest")

        assert res.status_code == 200
        assert res.headers["content-type"].startswith("application/manifest+json")
        got = json.loads(res.text)
        assert got["display"] == "standalone"
        assert got["start_url"] == "/admin"

    def test_the_icons_are_served(self, client):
        for path, kind in (("/icon.svg", "image/svg+xml"),
                           ("/apple-touch-icon.png", "image/png")):
            res = client.get(path)
            assert res.status_code == 200, path
            assert res.headers["content-type"].startswith(kind), path

    def test_every_icon_in_the_manifest_exists(self, client):
        """並べたのに配っていないと、ホーム画面のアイコンが白く抜ける。"""
        for icon in json.loads(client.get("/manifest.webmanifest").text)["icons"]:
            assert client.get(icon["src"]).status_code == 200, icon["src"]

    def test_the_shell_points_at_it(self, client):
        html = client.get("/admin").text
        assert 'rel="manifest" href="/manifest.webmanifest"' in html
        # iOS はマニフェストだけでは帯を消さない
        assert 'name="apple-mobile-web-app-capable" content="yes"' in html

    def test_there_is_no_service_worker(self, client):
        """抱え込むと、更新しても古い版が出続ける(やること画面で踏んだ)。"""
        html = client.get("/admin").text
        assert "serviceWorker" not in html
        assert client.get("/sw.js").status_code == 404
