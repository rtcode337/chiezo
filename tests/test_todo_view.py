"""管理画面のルールの節(記憶の面のいちばん下。書き込みの口は `/admin/todo/rules/...`)。

ToDo の層のうち、画面に出すのはルールだけ。プロジェクトとタスクは置き場と REST
(`/v1/todo/...`。`tests/test_todo_api.py`)だけを持ち、操作は LAN の中のタスクと予定の
アプリに一本化している。ここで押さえるのは 3 つ:

- **画面にはルールだけが出る** —— プロジェクトとタスクの節と、その書き込みの口が残っていないこと
- **書いたら押した場所へ戻り、断られた理由は帯に出る** —— ドメイン層は
  `HTTPException` を投げるので、素通しすると画面の代わりに JSON が出る
- **外に出す面をもう持たない** —— `/api/**` も SPA の置き場も残っていないこと
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """短期記憶だけを持つ本体。長期記憶は要らない(ルールは notes の上にある)。"""
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    data = tmp_path / "data"
    data.mkdir()
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(data))
    from app.main import app

    with TestClient(app) as c:
        yield c


def _post(client, path, **data):
    return client.post(f"/admin/todo{path}", data=data, follow_redirects=False)


def _notice(res) -> str:
    """303 の戻り先に載せた一言(`?notice=`)。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(res.headers["location"]).query)
    return (query.get("notice") or query.get("error") or [""])[0]


class TestOnlyRulesAreShown:
    def test_rules_are_the_last_section_of_the_memory_page(self, client):
        """ルールは短期記憶に載っているだけなので、記憶の面の中に置く。"""
        from app.views.admin import PAGES

        assert "/admin/todo" not in [path for path, _label, _note in PAGES]
        _post(client, "/rules", title="日本語で書く", body="応答は日本語。")
        html = client.get("/admin/memory").text
        assert html.index('id="init"') < html.index('<h2 id="rules">ルール</h2>')
        assert "日本語で書く" in html

    def test_projects_and_tasks_are_not_on_the_page(self, client):
        """操作はタスクと予定のアプリに一本化した。画面に同じ操作を残さない。"""
        from app import tasks

        tasks.create_project("sample-game")
        tasks.create_task("盤面の当たり判定を直す", project="sample-game")

        html = client.get("/admin/memory").text
        assert "盤面の当たり判定を直す" not in html
        for gone in ('id="tasks"', 'id="done"', 'id="backup"', "プロジェクトとタスク", "✳"):
            assert gone not in html, gone

    def test_the_front_door_counts_only_rules(self, client):
        from app import tasks

        tasks.create_task("盤面の当たり判定を直す")
        _post(client, "/rules", title="日本語で書く", body="応答は日本語。")
        html = client.get("/admin").text
        assert "ルール 1 本(有効 1 本)" in html
        assert "未完了" not in html

    def test_the_task_and_project_forms_are_gone(self, client):
        """書き込みの口も残さない(残すと画面の外から同じ操作ができてしまう)。"""
        for path in (
            "/tasks", "/tasks/1", "/tasks/1/status", "/tasks/1/flag", "/tasks/1/move",
            "/tasks/1/delete", "/projects", "/projects/1", "/projects/1/archive",
            "/projects/1/move", "/projects/1/delete", "/import",
        ):
            assert _post(client, path).status_code in (404, 405), path
        assert client.get("/admin/todo/export").status_code == 404

    def test_the_rest_for_apps_is_still_there(self, client):
        """外したのは画面だけ。アプリが読み書きする口は残す。"""
        assert client.get("/v1/todo/tasks").status_code == 200
        assert client.get("/v1/todo/projects").status_code == 200


class TestRules:
    def test_only_enabled_rules_are_combined(self, client):
        from app import tasks

        _post(client, "/rules", title="効くほう", body="本文 A")
        _post(client, "/rules", title="効かないほう", body="本文 B")
        off = next(r for r in tasks.list_rules() if r.title == "効かないほう")
        _post(client, f"/rules/{off.doc_id}/toggle", enabled="0")

        combined = tasks.combined()
        assert "## 効くほう" in combined
        assert "効かないほう" not in combined

    def test_moving_changes_the_order(self, client):
        from app import tasks

        _post(client, "/rules", title="あ", body="本文 A")
        _post(client, "/rules", title="い", body="本文 B")
        second = tasks.list_rules()[1]
        _post(client, f"/rules/{second.doc_id}/move", dir="up")
        assert [r.title for r in tasks.list_rules()] == ["い", "あ"]

    def test_importing_shows_what_lands_before_writing(self, client):
        """入れ替えは取り消せないので、**下見を挟む**。"""
        from app import tasks

        markdown = "## 足すほう\n\nここが本文。\n"
        preview = client.post(
            "/admin/todo/rules/import", data={"markdown": markdown}, follow_redirects=False
        )
        assert preview.status_code == 200
        assert "足すほう" in preview.text and "この内容で取り込む" in preview.text
        assert tasks.list_rules() == []

        client.post(
            "/admin/todo/rules/import",
            data={"markdown": markdown, "confirm": "yes"},
            follow_redirects=False,
        )
        assert [r.title for r in tasks.list_rules()] == ["足すほう"]

    def test_importing_with_replace_clears_the_old_ones(self, client):
        from app import tasks

        _post(client, "/rules", title="古いほう", body="本文")
        client.post(
            "/admin/todo/rules/import",
            data={"markdown": "## 新しいほう\n\n本文。\n", "replace": "1", "confirm": "yes"},
            follow_redirects=False,
        )
        assert [r.title for r in tasks.list_rules()] == ["新しいほう"]

    def test_the_rules_repository_is_remembered(self, client):
        from app import tasks

        _post(client, "/rules/repo", url="https://github.com/owner/kiyaku")
        assert tasks.rules_repo_url() == "https://github.com/owner/kiyaku"
        assert 'value="https://github.com/owner/kiyaku"' in client.get("/admin/memory").text

    def test_deleting_asks_first_even_with_a_quote_in_the_title(self, client):
        """見出しに `'` があっても、確認の JS の文字列が途中で閉じないこと。"""
        _post(client, "/rules", title="it's", body="本文")
        html = client.get("/admin/memory").text
        assert "ルール「it\\&#x27;s」を消します" in html or "ルール「it\\&#39;s」を消します" in html


class TestRefusalsStayOnThePage:
    def test_a_refused_write_comes_back_as_a_banner(self, client):
        """ドメイン層の `HTTPException` を素通しすると、画面の代わりに JSON が出る。"""
        res = _post(client, "/rules", title="  ", body="本文")
        assert res.status_code == 303
        assert res.headers["location"].endswith("#rules")
        assert "title は必須です" in _notice(res)
        assert "⚠️" in client.get("/admin/memory?error=title は必須です").text

    def test_an_unknown_rule_is_not_a_500(self, client):
        res = _post(client, "/rules/999/toggle", enabled="0")
        assert res.status_code == 303
        assert "ルールが見つかりません" in _notice(res)


class TestNoPublicFace:
    """Chiezo は安全なネットワークの中からしか触らせない。**外に出す面を持たない**。"""

    def test_the_old_rest_layer_is_gone(self, client):
        for path in ("/api/tasks", "/api/rules", "/api/notes", "/api/media/groups", "/api/me"):
            assert client.get(path).status_code == 404, path

    def test_the_google_login_is_gone(self, client):
        for path in ("/login/oauth2/code/google", "/oauth2/authorization/google"):
            assert client.get(path, follow_redirects=False).status_code == 404, path

    def test_the_spa_modules_are_gone(self):
        import importlib

        for name in ("app.tasks_app", "app.tasks_auth", "app.tasks_api", "app.tasks_static"):
            with pytest.raises(ModuleNotFoundError):
                importlib.import_module(name)
