"""管理画面の ToDo(`/admin/todo`)。

やること層は別プロセスの SPA(旧 chiezo-tasks・Google のログイン付き)をやめて、
管理画面の 1 面に畳んである。ここで押さえるのは 3 つ:

- **タスクとルールが 1 画面に出る**(行き来でページを読み直さない)
- **書いたら押した場所へ戻り、断られた理由は帯に出る** —— ドメイン層は
  `HTTPException` を投げるので、素通しすると画面の代わりに JSON が出る
- **外に出す面をもう持たない** —— `/api/**` も SPA の置き場も残っていないこと
"""
import json

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """短期記憶だけを持つ本体。長期記憶は要らない(ToDo は notes の上にある)。"""
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


def _project(client, name="arrow-puzzle", **kw):
    _post(client, "/projects", name=name, **kw)
    from app import tasks

    return tasks.require_project_by_name(name)


def _task(client, title="盤面の当たり判定を直す", **kw):
    _post(client, "/tasks", title=title, **kw)
    from app import tasks

    return next(t for t in tasks.list_active_tasks() if t.title == title)


def _notice(res) -> str:
    """303 の戻り先に載せた一言(`?notice=`)。"""
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(res.headers["location"]).query)
    return (query.get("notice") or query.get("error") or [""])[0]


class TestOnePage:
    def test_tasks_and_rules_are_on_the_same_page(self, client):
        """タスクとルールは同じ日に触るもの。**画面を分けない**。"""
        _project(client)
        _task(client)
        _post(client, "/rules", title="日本語で書く", body="応答は日本語。")

        html = client.get("/admin/todo").text
        assert "盤面の当たり判定を直す" in html
        assert "日本語で書く" in html
        assert 'id="tasks"' in html and 'id="rules"' in html

    def test_the_nav_lists_it_right_after_memory(self, client):
        """帯の並びは記憶の次。タスクもルールも短期記憶に載っているだけなので。"""
        from app.views.admin import PAGES

        paths = [path for path, _label, _note in PAGES]
        labels = {path: label for path, label, _note in PAGES}
        assert labels["/admin/todo"] == "ToDo"
        assert paths.index("/admin/todo") == paths.index("/admin/memory") + 1

    def test_the_front_door_shows_how_much_is_left(self, client):
        _project(client)
        _task(client)
        assert "未完了 1 件" in client.get("/admin").text

    def test_notes_and_compare_are_gone_from_this_layer(self, client):
        """メモの一覧は廃止(短期記憶は記憶の面とブラウズで読む)。

        見比べは管理画面に同じものがあるので、こちらには持ち込まない。
        """
        from app import tasks

        assert not hasattr(tasks, "list_notes")
        html = client.get("/admin/todo").text
        assert "/admin/media" not in html.split('<nav class="todo-index">')[1].split("</nav>")[0]


class TestTasks:
    def test_a_task_lands_in_its_project(self, client):
        _project(client)
        task = _task(client, project="arrow-puzzle")
        assert task.project == "arrow-puzzle"

    def test_a_task_without_a_project_falls_into_the_unsorted_group(self, client):
        task = _task(client, title="どこにも属さない")
        assert task.project is None
        assert "(未分類)" in client.get("/admin/todo").text

    def test_status_moves_without_opening_an_edit_screen(self, client):
        """一覧から直に状態を変えられること(押す場所と見る場所を分けない)。"""
        from app import tasks

        task = _task(client)
        _post(client, f"/tasks/{task.doc_id}/status", status="in_progress")
        assert tasks.require_task(task.doc_id).status == "in_progress"
        _post(client, f"/tasks/{task.doc_id}/status", status="done")
        assert tasks.require_task(task.doc_id).status == "done"
        # 完了したものは未完了の一覧から消え、畳んだ「完了」に移る
        assert "片付いたもの(1 件)" in client.get("/admin/todo").text

    def test_the_hard_one_mark_is_its_own_axis(self, client):
        from app import tasks

        task = _task(client)
        _post(client, f"/tasks/{task.doc_id}/flag", flagged="1")
        assert tasks.require_task(task.doc_id).flagged is True
        _post(client, f"/tasks/{task.doc_id}/flag", flagged="0")
        assert tasks.require_task(task.doc_id).flagged is False

    def test_editing_keeps_the_title_out_of_the_body(self, client):
        """本文はタイトル行を除いた残り。**両方を持つと、どちらを直すのか分からない**。"""
        from app import tasks

        task = _task(client, title="あ", body="中身")
        _post(client, f"/tasks/{task.doc_id}", title="い", body="別の中身", project="")
        updated = tasks.require_task(task.doc_id)
        assert updated.title == "い"
        assert tasks.body_rest(updated) == "別の中身"

    def test_moving_only_touches_the_group_it_is_in(self, client):
        """↑↓ は同じかたまりの中だけで動く(別のプロジェクトのタスクは混ぜない)。"""
        from app import tasks

        _project(client)
        first = _task(client, title="いちばん上", project="arrow-puzzle")
        second = _task(client, title="その次", project="arrow-puzzle")
        # 放り込んだ順ではなく、新しいものが上に積まれる
        assert [t.doc_id for t in tasks.list_active_tasks("arrow-puzzle")] == [
            second.doc_id, first.doc_id
        ]
        _post(client, f"/tasks/{second.doc_id}/move", dir="down")
        assert [t.doc_id for t in tasks.list_active_tasks("arrow-puzzle")] == [
            first.doc_id, second.doc_id
        ]

    def test_deleting_says_so(self, client):
        from app import tasks

        task = _task(client)
        res = _post(client, f"/tasks/{task.doc_id}/delete")
        assert res.status_code == 303
        assert tasks.list_active_tasks() == []


class TestHandoffToClaudeCode:
    """タスクを Claude Code に渡すリンク。**やること層の主目的がこれ**。"""

    def test_it_prefills_the_title_and_the_body(self, client):
        from app.views import todo

        _project(client)
        task = _task(client, title="あ", body="中身")
        url = todo.claude_code_url(task, [], None)
        assert url.startswith("https://claude.ai/code?")
        assert "prompt=" in url
        from urllib.parse import parse_qs, urlparse

        prompt = parse_qs(urlparse(url).query)["prompt"][0]
        assert prompt == "あ\n\n中身"

    def test_it_carries_the_repositories_as_owner_repo(self, client):
        from urllib.parse import parse_qs, urlparse

        from app.views import todo

        task = _task(client)
        url = todo.claude_code_url(
            task, ["https://github.com/owner/arrow-puzzle", "https://example.com/not-github"], None
        )
        repos = parse_qs(urlparse(url).query)["repositories"][0]
        assert repos == "owner/arrow-puzzle"

    def test_the_rules_repository_is_always_included(self, client):
        """規約リポジトリはセッションに含めるだけでなく、従う対象だと本文で名指す。"""
        from urllib.parse import parse_qs, urlparse

        from app.views import todo

        task = _task(client)
        url = todo.claude_code_url(task, [], "https://github.com/owner/kiyaku")
        query = parse_qs(urlparse(url).query)
        assert query["repositories"][0] == "owner/kiyaku"
        assert query["prompt"][0].startswith("まず、セッションに含まれる規約リポジトリ owner/kiyaku")

    def test_a_long_task_is_cut_before_the_prefill_limit(self, client):
        from app.views import todo

        task = _task(client, title="長い", body="あ" * 9000)
        url = todo.claude_code_url(task, [], None)
        from urllib.parse import parse_qs, urlparse

        prompt = parse_qs(urlparse(url).query)["prompt"][0]
        assert len(prompt) < 9000
        assert prompt.endswith("(以下略。全文は ToDo の画面で)")


class TestProjects:
    def test_archiving_is_refused_while_tasks_are_left(self, client):
        """片付いていないタスクごと一覧から消えると、放り込んだものを取りこぼす。"""
        project = _project(client)
        _task(client, project="arrow-puzzle")
        res = _post(client, f"/projects/{project.id}/archive", archived="1")
        assert res.status_code == 303
        assert "未完了のタスクが 1 件" in _notice(res)

    def test_renaming_moves_the_tag_on_every_task(self, client):
        """名前がそのまま紐づけなので、変えたらタスク側のタグも付け替わる。"""
        from app import tasks

        project = _project(client)
        task = _task(client, project="arrow-puzzle")
        _post(client, f"/projects/{project.id}", name="arrow-puzzle2", description="", repo_urls="")
        assert tasks.require_task(task.doc_id).project == "arrow-puzzle2"

    def test_only_an_archived_project_can_be_deleted(self, client):
        project = _project(client)
        res = _post(client, f"/projects/{project.id}/delete")
        assert "アーカイブしてからでないと削除できません" in _notice(res)
        _post(client, f"/projects/{project.id}/archive", archived="1")
        _post(client, f"/projects/{project.id}/delete")
        from app import tasks

        assert tasks.list_projects() == []

    def test_the_order_shown_is_the_order_reordered(self, client):
        """↑↓ は台帳の並びの上で動く。**画面がアーカイブを後ろへ寄せると押した行がずれる**。"""
        from app import tasks

        first = _project(client, "arrow-puzzle")
        second = _project(client, "travel-log")
        third = _project(client, "pupai")
        _post(client, f"/projects/{second.id}/archive", archived="1")

        html = client.get("/admin/todo").text
        table = html.split('<h2 id="projects">')[1]
        shown = [n for n in ("arrow-puzzle", "travel-log", "pupai") if n in table]
        assert shown == [p.name for p in tasks.list_projects()]

        _post(client, f"/projects/{third.id}/move", dir="up")
        assert [p.name for p in tasks.list_projects()] == [
            first.name, third.name, second.name
        ]

    def test_tasks_left_in_an_archived_project_still_show(self, client):
        """アーカイブは未完了 0 件が条件なので普通は起きないが、**起きたら消さない**。"""
        from app import tasks

        project = _project(client)
        _post(client, f"/projects/{project.id}/archive", archived="1")
        tasks.create_task("あとから紐づけ直した", project="arrow-puzzle")

        html = client.get("/admin/todo").text
        assert "あとから紐づけ直した" in html
        assert "アーカイブ済みですが、未完了のタスクが残っています" in html

    def test_repositories_are_one_per_line(self, client):
        project = _project(
            client, repo_urls="https://github.com/owner/a\n\nhttps://github.com/owner/b\n"
        )
        assert project.repo_urls == ["https://github.com/owner/a", "https://github.com/owner/b"]


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


class TestBackup:
    def test_what_is_written_out_can_be_read_back(self, client):
        """書き出したものがそのまま取り込みの入力になること(テキストで持てば控えになる)。"""
        _project(client)
        _task(client, project="arrow-puzzle")

        res = client.get("/admin/todo/export")
        assert res.status_code == 200
        assert res.headers["content-disposition"].startswith('attachment; filename="chiezo-todo-')
        payload = res.text

        # 下見では書き込まない
        preview = client.post(
            "/admin/todo/import", data={"payload": payload}, follow_redirects=False
        )
        assert preview.status_code == 200 and "この内容で読み込む" in preview.text

        # 二度読んでも増えない((プロジェクト名, タイトル)で照合して飛ばす)
        for _ in range(2):
            client.post(
                "/admin/todo/import",
                data={"payload": payload, "confirm": "yes"},
                follow_redirects=False,
            )
        from app import tasks

        assert len(tasks.list_active_tasks()) == 1

    def test_broken_json_is_told_on_the_page(self, client):
        res = _post(client, "/import", payload="{ここは JSON ではない")
        assert res.status_code == 303
        assert "JSON として読めません" in _notice(res)

    def test_the_export_is_the_shape_the_import_expects(self, client):
        _project(client)
        payload = json.loads(client.get("/admin/todo/export").text)
        assert payload["version"] == 1
        assert [p["name"] for p in payload["projects"]] == ["arrow-puzzle"]


class TestRefusalsStayOnThePage:
    def test_a_refused_write_comes_back_as_a_banner(self, client):
        """ドメイン層の `HTTPException` を素通しすると、画面の代わりに JSON が出る。"""
        res = _post(client, "/tasks", title="  ")
        assert res.status_code == 303
        assert "title は必須です" in _notice(res)
        assert "⚠️" in client.get("/admin/todo?error=title は必須です").text

    def test_an_unknown_task_is_not_a_500(self, client):
        res = _post(client, "/tasks/999/status", status="done")
        assert res.status_code == 303
        assert "タスクが見つかりません" in _notice(res)


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

    def test_old_bookmarks_land_on_the_new_page(self, client):
        """`/tasks` は SPA の置き場だった。**迷子にしない**ぶんだけ残す。"""
        for path in ("/tasks", "/tasks/", "/tasks/rules"):
            res = client.get(path, follow_redirects=False)
            assert res.status_code == 308, path
            assert res.headers["location"] == "/admin/todo"
