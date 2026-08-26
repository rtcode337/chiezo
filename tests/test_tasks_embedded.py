"""やること層を本体(chiezo-app)に埋め込む面のテスト。

外に出す面(`chiezo-tasks`)は認証で守るが、こちらは LAN 内・認証なしの本体に
同じ REST と同じ画面を素通しで載せる。**両方の面が同じ成果物を配る**ので、
配り方の違い(埋め込み側は `/tasks` の下・PWA にしない)だけをここで押さえる。
"""
import pytest
from fastapi.testclient import TestClient

SHELL = """<!doctype html>
<html lang="ja">
  <head>
    <meta charset="UTF-8" />
    <link rel="icon" href="/icons/icon-192.png" />
    <script type="module" crossorigin src="/assets/index-abc.js"></script>
  <link rel="manifest" href="/manifest.webmanifest">
  <script id="vite-plugin-pwa:register-sw" src="/registerSW.js"></script></head>
  <body><div id="app"></div></body>
</html>
"""


@pytest.fixture()
def static_dir(tmp_path, monkeypatch):
    root = tmp_path / "tasks-static"
    (root / "assets").mkdir(parents=True)
    (root / "icons").mkdir()
    (root / "index.html").write_text(SHELL, encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("console.log(1)", encoding="utf-8")
    (root / "icons" / "icon-192.png").write_bytes(b"\x89PNG")
    monkeypatch.setenv("CHIEZO_TASKS_STATIC_DIR", str(root))
    return root


@pytest.fixture()
def client(static_dir, tmp_path, built_data_dir, monkeypatch):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestScreen:
    def test_the_shell_is_served_under_tasks(self, client):
        res = client.get("/tasks/")
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/html")
        assert '<div id="app">' in res.text

    def test_the_shell_tells_the_router_where_it_lives(self, client):
        """SPA のルーターは `<base>` を読んで自分の居場所を決める。"""
        assert '<base href="/tasks/">' in client.get("/tasks/").text

    def test_it_is_not_a_pwa_here(self, client):
        """Service Worker のスコープはルート直下なので、本体を巻き込ませない。"""
        html = client.get("/tasks/").text
        assert "registerSW" not in html
        assert 'rel="manifest"' not in html

    def test_unknown_paths_fall_back_to_the_shell(self, client):
        """画面のルーティングは SPA 側が持つ。"""
        assert '<div id="app">' in client.get("/tasks/rules").text

    def test_bare_tasks_redirects_to_the_slash(self, client):
        """`<base>` の解決が `/tasks/` を基準になるようにする。"""
        res = client.get("/tasks", follow_redirects=False)
        assert res.status_code == 307
        assert res.headers["location"] == "/tasks/"

    def test_assets_are_served_from_the_root(self, client):
        """殻は絶対パスで資材を参照する(`<base>` は絶対パスに効かない)。"""
        assert client.get("/assets/index-abc.js").status_code == 200
        assert client.get("/icons/icon-192.png").status_code == 200

    def test_it_does_not_swallow_the_rest_of_the_app(self, client):
        """総取りにしない。本体の機械向けの口を画面に落とさないこと。"""
        assert client.get("/v1/sources").status_code == 200
        assert client.get("/v1/nosuch/search", params={"q": "x"}).status_code == 404
        assert '<div id="app">' not in client.get("/v1/nosuch/search", params={"q": "x"}).text

    def test_missing_build_is_reported(self, client, monkeypatch, tmp_path):
        monkeypatch.setenv("CHIEZO_TASKS_STATIC_DIR", str(tmp_path / "nope"))
        res = client.get("/tasks/")
        assert res.status_code == 503
        assert "やること画面" in res.json()["error"]


class TestApi:
    def test_it_works_without_logging_in(self, client):
        """本体は LAN 内・認証なしの面なので素通し。"""
        assert client.get("/api/tasks").status_code == 200
        created = client.post("/api/tasks", json={"title": "埋め込みから足す"})
        assert created.status_code == 201
        assert created.json()["title"] == "埋め込みから足す"

    def test_me_reports_that_it_is_embedded(self, client):
        """画面はこれを見て、ログインの代わりに管理画面への戻り口を出す。"""
        me = client.get("/api/me").json()
        assert me["embedded"] is True

    def test_errors_keep_the_shape_the_screen_reads(self, client):
        """やること層のエラーは `{"error": {"code", "message"}}`。"""
        body = client.get("/api/tasks/999999").json()
        assert set(body["error"]) == {"code", "message"}

    def test_the_rest_of_the_app_keeps_its_own_error_shape(self, client):
        """本体は平たい `{"error": "..."}` のまま(例外ハンドラを共有しても混ざらない)。"""
        body = client.get("/v1/nosuch/search", params={"q": "x"}).json()
        assert isinstance(body["error"], str)


class TestAdminLink:
    def test_the_short_term_section_links_to_it(self, client):
        assert '<a href="/tasks/">' in client.get("/admin").text


class TestOtherNotes:
    """そのほかのメモ(タスク・プロジェクト・ルールのどれでもないもの)。"""

    def _fill(self, client):
        from app import notes

        notes.add(text="WSL2 へ移行すると決めた", title="移行の決定", tags="決定,環境")
        notes.add(text="復旧の手順", title="復旧手順", tags="runbook")
        notes.add(text="これはタスク", title="やること", tags="task")
        notes.add(text="これはルール", title="決まり", tags="rule")
        notes.add(text="これはプロジェクト", title="ぷろじぇくと", tags="project")

    def test_it_lists_only_what_no_other_page_shows(self, client):
        self._fill(client)
        listed = client.get("/api/notes").json()
        assert listed["total"] == 2
        assert {n["title"] for n in listed["items"]} == {"移行の決定", "復旧手順"}

    def test_newest_first(self, client):
        self._fill(client)
        titles = [n["title"] for n in client.get("/api/notes").json()["items"]]
        assert titles == ["復旧手順", "移行の決定"]

    def test_it_can_be_narrowed_by_tag(self, client):
        self._fill(client)
        listed = client.get("/api/notes", params={"tag": "決定"}).json()
        assert [n["title"] for n in listed["items"]] == ["移行の決定"]

    def test_tags_count_only_these_notes(self, client):
        """タスクやルールに付いたタグまで混ぜない(絞り込みの候補にならないため)。"""
        self._fill(client)
        counts = {t["tag"]: t["docs"] for t in client.get("/api/notes/tags").json()}
        assert counts == {"決定": 1, "環境": 1, "runbook": 1}

    def test_each_note_links_to_the_browse_screen(self, client):
        """全文と生の項目は本体のブラウズ画面で見る(画面側は抜粋まで)。"""
        self._fill(client)
        note = client.get("/api/notes").json()["items"][0]
        assert note["url"] == f"/search/notes/doc/{note['id']}"

    def test_paging(self, client):
        self._fill(client)
        page = client.get("/api/notes", params={"limit": 1}).json()
        assert page["total"] == 2 and len(page["items"]) == 1
        rest = client.get("/api/notes", params={"limit": 1, "offset": 1}).json()
        assert len(rest["items"]) == 1
        assert rest["items"][0]["title"] != page["items"][0]["title"]
