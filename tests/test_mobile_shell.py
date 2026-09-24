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


class TestWideTablesBecomeCards:
    """**表は 1 行 1 枚の札に起こす**(`pages.as_cards` と `PAGE_STYLE`)。

    管理画面の表は 7 列あるものもあり、スマホでは横に延々とスクロールすることに
    なっていた。**横スクロールは「どの列を見ているか」を覚えていないと読めない**
    ので、列の多い表ほど破綻する。

    表は画面ごとに手で組んである(31 か所)ので、**出来上がった HTML に 1 回だけ
    掛ける** —— 1 つずつ見出しを書き写すと、書き漏らしと食い違いが必ず出る。
    """

    def test_the_column_name_goes_onto_each_cell(self):
        from app import pages

        out = pages.as_cards(
            "<table><thead><tr><th>巡回</th><th>間隔</th></tr></thead>"
            "<tr><td>ざっと</td><td>60 分ごと</td></tr></table>"
        )

        assert '<table class="as-cards">' in out
        assert '<td data-label="巡回">ざっと</td>' in out
        assert '<td data-label="間隔">60 分ごと</td>' in out

    def test_a_table_without_headings_is_left_alone(self):
        """**見出しが無い表は札にしない。** `thead` を隠すと、名前の付かない値が
        並ぶだけになる。
        """
        from app import pages

        plain = "<table><tr><td>あ</td><td>い</td></tr></table>"

        assert pages.as_cards(plain) == plain

    def test_a_table_inside_a_script_is_left_alone(self):
        """会話の画面は表を JavaScript の文字列として持っている
        (`app/views/chat.py`)—— そこを書き換えると台本そのものが壊れる。
        """
        from app import pages

        script = (
            "<script>out.push('<table><thead><tr><th>名</th></tr></thead>"
            "<tr><td>' + v + '</td></tr></table>')</script>"
        )

        assert pages.as_cards(script) == script

    def test_a_cell_that_spans_columns_gets_no_name(self):
        """またがる欄は、名前が 1 つに決まらない。**数えるほうは進める**ので、
        その次の欄に正しい名前が付く。
        """
        from app import pages

        out = pages.as_cards(
            "<table><thead><tr><th>名</th><th>数</th><th>後</th></tr></thead>"
            '<tr><td colspan="2">またがる</td><td>ここ</td></tr></table>'
        )

        assert 'data-label' not in out.split("またがる")[0].split("<td")[-1]
        assert '<td data-label="後">ここ</td>' in out

    def test_a_heading_that_spans_the_whole_table_is_not_a_column_name(self):
        """`<th colspan="3">` は表そのものの札で、列の名前ではない。
        配ると 1 列目だけに見当違いの名前が付く —— **間違った名前を付けるほうが、
        名前が無いより読めない**ので、その表は見送る。
        """
        from app import pages

        caption = (
            '<table><thead><tr><th colspan="2">AI に頼めること</th></tr></thead>'
            "<tr><td>会話</td><td>画像</td></tr></table>"
        )

        assert pages.as_cards(caption) == caption

    def test_an_existing_class_is_kept(self):
        from app import pages

        out = pages.as_cards(
            '<table class="ai-usage"><thead><tr><th>相手</th></tr></thead>'
            "<tr><td>codex</td></tr></table>"
        )

        assert '<table class="as-cards ai-usage">' in out

    def test_nothing_but_attributes_is_added(self, client):
        """**中身は 1 文字も動かさない。** 表の切れ目を読み違えても、最悪
        「名前が付かない欄がある」で済む(壊れた HTML にはならない)。
        """
        import re

        from app import pages

        body = client.get("/admin").text
        out = pages.as_cards(body)
        back = re.sub(r'<td data-label="[^"]*"', "<td", out)
        back = back.replace('<table class="as-cards">', "<table>")
        back = back.replace('<table class="as-cards ', '<table class="')

        assert back == body

    def test_the_narrow_screen_shows_the_names_beside_the_values(self, client):
        """札の見た目は殻の style が持つ(画面ごとに書き写さない)。"""
        html = client.get("/admin").text

        assert "table.as-cards td[data-label]::before" in html
        assert "content: attr(data-label)" in html
