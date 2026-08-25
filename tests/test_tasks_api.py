"""やること層の REST(`/api/**`)のテスト。

画面(cc-tasks から移した Vue)が話す形そのものなので、**フィールド名と
エラーの形**を重点的に押さえる。ここがずれると画面側が黙って壊れる。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def api(tmp_path, monkeypatch):
    """ログイン済みのクライアント。

    開発モード(`CHIEZO_TASKS_DEV`)で素通しするのではなく、**本物のセッションを 1 件
    作って Cookie を持たせる** —— そうしないと認証と CSRF のミドルウェアを通らず、
    画面が実際に踏む経路を試したことにならない。
    """
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "test-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "test-secret")
    monkeypatch.setenv("ALLOWED_EMAIL", "someone@example.com")
    from app import tasks_auth
    from app.tasks_app import create_app

    session_id = tasks_auth._store(
        "user", tasks_auth.SESSION_TTL, email="someone@example.com", name="Someone"
    )
    csrf = "test-csrf-token"
    with TestClient(create_app(), headers={tasks_auth.CSRF_HEADER: csrf}) as c:
        c.cookies.set(tasks_auth.SESSION_COOKIE, session_id)
        c.cookies.set(tasks_auth.CSRF_COOKIE, csrf)
        yield c


def _project(api, name="arrow-puzzle", **kw):
    return api.post("/api/projects", json={"name": name, **kw}).json()


def _task(api, title="あ", **kw):
    return api.post("/api/tasks", json={"title": title, **kw}).json()


class TestShape:
    def test_task_fields_are_camel_case(self, api):
        task = _task(api)
        assert set(task) == {"id", "title", "status", "flagged", "sortOrder", "createdAt", "updatedAt"}

    def test_project_id_is_absent_when_unassigned(self, api):
        """画面の型が「未分類なら欠落する」前提。null を入れると分岐が増える。"""
        assert "projectId" not in _task(api)

    def test_project_id_is_the_project_doc_id(self, api):
        project = _project(api)
        task = _task(api, projectId=project["id"])
        assert task["projectId"] == project["id"]

    def test_project_fields_are_camel_case(self, api):
        project = _project(api, repoUrls=["https://example.com/a"], description="説明")
        assert project["repoUrls"] == ["https://example.com/a"]
        assert project["description"] == "説明" and project["sortOrder"] == 1

    def test_snake_case_is_accepted_too(self, api):
        project = api.post(
            "/api/projects", json={"name": "b", "repo_urls": ["https://example.com/b"]}
        ).json()
        assert project["repoUrls"] == ["https://example.com/b"]

    def test_error_carries_a_message_the_screen_can_show(self, api):
        """画面は error.message をそのまま出す。平たくすると案内が消える。"""
        _project(api, "arrow-puzzle")
        res = api.post("/api/projects", json={"name": "arrow-puzzle"})
        assert res.status_code == 409
        assert res.json()["error"]["code"] == "conflict"
        assert "同名のプロジェクト" in res.json()["error"]["message"]

    def test_not_found_shape(self, api):
        res = api.get("/api/tasks/9999")
        assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"


class TestRouteOrder:
    """`/api/tasks/{id}` を先に宣言すると order / export / import が id と解釈される。"""

    def test_task_order_is_not_parsed_as_an_id(self, api):
        project = _project(api)
        a = _task(api, "あ", projectId=project["id"])
        b = _task(api, "い", projectId=project["id"])
        res = api.put("/api/tasks/order", json={"projectId": project["id"], "ids": [b["id"], a["id"]]})
        assert res.status_code == 200
        assert [t["id"] for t in res.json()] == [b["id"], a["id"]]

    def test_export_is_not_parsed_as_an_id(self, api):
        assert api.get("/api/tasks/export").status_code == 200

    def test_project_order_is_not_parsed_as_an_id(self, api):
        a, b = _project(api, "a"), _project(api, "b")
        res = api.put("/api/projects/order", json={"ids": [b["id"], a["id"]]})
        assert [p["id"] for p in res.json()] == [b["id"], a["id"]]

    def test_rule_settings_is_not_parsed_as_an_id(self, api):
        assert api.get("/api/rules/settings").status_code == 200


class TestTasks:
    def test_list_filters_by_done(self, api):
        keep = _task(api, "残る")
        gone = _task(api, "片付く")
        api.patch(f"/api/tasks/{gone['id']}", json={"status": "done"})
        active = api.get("/api/tasks", params={"done": "false"}).json()
        assert [t["id"] for t in active] == [keep["id"]]

    def test_done_is_paged_with_total_pages(self, api):
        for i in range(3):
            t = _task(api, f"済み {i}")
            api.patch(f"/api/tasks/{t['id']}", json={"status": "done"})
        page = api.get("/api/tasks", params={"done": "true", "page": 0, "size": 2}).json()
        assert page["total"] == 3 and page["totalPages"] == 2 and len(page["items"]) == 2

    def test_detail_carries_the_project_name(self, api):
        project = _project(api)
        task = _task(api, "あ", projectId=project["id"])
        assert api.get(f"/api/tasks/{task['id']}").json()["projectName"] == "arrow-puzzle"

    def test_project_id_zero_unlinks(self, api):
        """0 は「紐づけを外す」。null は「変更しない」なので分ける必要がある。"""
        project = _project(api)
        task = _task(api, "あ", projectId=project["id"])
        updated = api.patch(f"/api/tasks/{task['id']}", json={"projectId": 0}).json()
        assert "projectId" not in updated

    def test_null_project_id_leaves_it_alone(self, api):
        project = _project(api)
        task = _task(api, "あ", projectId=project["id"])
        updated = api.patch(f"/api/tasks/{task['id']}", json={"projectId": None, "flagged": True}).json()
        assert updated["projectId"] == project["id"] and updated["flagged"] is True

    def test_delete_returns_204(self, api):
        task = _task(api)
        assert api.delete(f"/api/tasks/{task['id']}").status_code == 204
        assert api.get("/api/tasks").json() == []

    def test_unknown_status_is_400(self, api):
        assert api.get("/api/tasks", params={"status": "なにか"}).status_code == 400

    def test_an_empty_flag_means_no_filter(self, api):
        """画面は「全件」を `?archived=` / `?done=`(値だけ空)で送る。

        FastAPI に bool として宣言すると空文字を変換できず 422 になり、
        一覧がまるごと出なくなる(実ブラウザで踏んだ)。
        """
        _project(api, "a")
        _task(api, "あ")
        assert api.get("/api/projects?archived=").status_code == 200
        assert len(api.get("/api/projects?archived=").json()) == 1
        assert len(api.get("/api/tasks?done=").json()) == 1

    def test_a_garbage_flag_is_400_not_500(self, api):
        assert api.get("/api/projects", params={"archived": "たぶん"}).status_code == 400


class TestProjects:
    def test_archived_filter(self, api):
        live = _project(api, "a")
        archived = _project(api, "b")
        api.patch(f"/api/projects/{archived['id']}", json={"archived": True})
        assert [p["id"] for p in api.get("/api/projects", params={"archived": "false"}).json()] == [live["id"]]

    def test_archive_is_blocked_while_tasks_remain(self, api):
        project = _project(api)
        _task(api, "残っている", projectId=project["id"])
        res = api.patch(f"/api/projects/{project['id']}", json={"archived": True})
        assert res.status_code == 400 and "未完了" in res.json()["error"]["message"]

    def test_delete_requires_archive_first(self, api):
        project = _project(api)
        assert api.delete(f"/api/projects/{project['id']}").status_code == 400


class TestRules:
    def test_crud_and_combined(self, api):
        api.post("/api/rules", json={"title": "日本語で書く", "body": "- 応答も日本語"})
        rules = api.get("/api/rules").json()
        assert rules[0]["title"] == "日本語で書く" and rules[0]["enabled"] is True
        assert "## 日本語で書く" in api.get("/api/rules/combined").json()["markdown"]

    def test_disable_drops_it_from_combined(self, api):
        rule = api.post("/api/rules", json={"title": "あ", "body": "本文"}).json()
        api.patch(f"/api/rules/{rule['id']}", json={"enabled": False})
        assert api.get("/api/rules/combined").json()["markdown"] == ""

    def test_import_round_trip(self, api):
        api.post("/api/rules", json={"title": "あ", "body": "本文 A"})
        api.post("/api/rules", json={"title": "い", "body": "本文 B"})
        markdown = api.get("/api/rules/combined").json()["markdown"]
        result = api.post("/api/rules/import", json={"markdown": markdown, "replace": True}).json()
        assert result["titles"] == ["あ", "い"]
        assert api.get("/api/rules/combined").json()["markdown"] == markdown

    def test_import_dry_run_does_not_write(self, api):
        result = api.post(
            "/api/rules/import", json={"markdown": "## あ\n\n本文\n", "dryRun": True}
        ).json()
        assert result["titles"] == ["あ"] and result["rules"] == []
        assert api.get("/api/rules").json() == []

    def test_settings_round_trip(self, api):
        assert api.get("/api/rules/settings").json() == {"rulesRepoUrl": None}
        api.patch("/api/rules/settings", json={"rulesRepoUrl": "https://example.com/kiyaku"})
        assert api.get("/api/rules/settings").json()["rulesRepoUrl"] == "https://example.com/kiyaku"
        api.patch("/api/rules/settings", json={"rulesRepoUrl": ""})
        assert api.get("/api/rules/settings").json() == {"rulesRepoUrl": None}


class TestTransfer:
    def test_export_then_import_is_idempotent(self, api):
        project = _project(api, "arrow-puzzle", repoUrls=["https://example.com/a"])
        _task(api, "やること", projectId=project["id"])
        _task(api, "未分類のやること")
        exported = api.get("/api/tasks/export").json()
        assert exported["version"] == 1
        assert exported["projects"][0]["name"] == "arrow-puzzle"
        assert exported["unassignedTasks"][0]["title"] == "未分類のやること"

        # 二度読んでも増えない(復元は繰り返し試すことがある)
        result = api.post("/api/tasks/import", json=exported).json()
        assert result["createdTasks"] == []
        assert sorted(result["skippedTasks"]) == sorted(
            ["arrow-puzzle / やること", "未分類のやること"]
        )
        assert len(api.get("/api/tasks").json()) == 2

    def test_import_creates_missing_projects(self, api):
        data = {
            "version": 1,
            "projects": [{"name": "new-repo", "repoUrls": [], "tasks": [{"title": "あ"}]}],
            "unassignedTasks": [],
        }
        result = api.post("/api/tasks/import", json=data).json()
        assert result["createdProjects"] == ["new-repo"]
        assert [p["name"] for p in api.get("/api/projects").json()] == ["new-repo"]

    def test_dry_run_does_not_write(self, api):
        data = {"version": 1, "projects": [], "unassignedTasks": [{"title": "あ"}]}
        result = api.post("/api/tasks/import", params={"dryRun": "true"}, json=data).json()
        assert result["createdTasks"] == ["あ"] and api.get("/api/tasks").json() == []

    def test_flag_survives_the_round_trip(self, api):
        task = _task(api, "大掛かり")
        api.patch(f"/api/tasks/{task['id']}", json={"flagged": True})
        exported = api.get("/api/tasks/export").json()
        assert exported["unassignedTasks"][0]["flagged"] is True

    def test_newer_format_is_rejected(self, api):
        data = {"version": 99, "projects": [], "unassignedTasks": [{"title": "あ"}]}
        res = api.post("/api/tasks/import", json=data)
        assert res.status_code == 400 and "対応していない形式" in res.json()["error"]["message"]

    def test_empty_import_is_400(self, api):
        assert api.post("/api/tasks/import", json={"version": 1}).status_code == 400


class TestHealth:
    def test_healthz(self, api):
        assert api.get("/healthz").json() == {"ok": True, "notes": True}


class TestSpa:
    """画面(Vue の成果物)の配信。"""

    @pytest.fixture()
    def served(self, api, tmp_path, monkeypatch):
        root = tmp_path / "static"
        (root / "assets").mkdir(parents=True)
        (root / "index.html").write_text("<!doctype html>殻", encoding="utf-8")
        (root / "assets" / "app-abc123.js").write_text("console.log(1)", encoding="utf-8")
        (root / "manifest.webmanifest").write_text("{}", encoding="utf-8")
        monkeypatch.setenv("CHIEZO_TASKS_STATIC_DIR", str(root))
        return api

    def test_serves_the_shell_for_unknown_paths(self, served):
        """画面のルーティングは SPA 側が持つので、/done も /rules も殻を返す。"""
        for path in ["/", "/done", "/rules", "/archived"]:
            res = served.get(path)
            assert res.status_code == 200 and "殻" in res.text, path

    def test_serves_real_files(self, served):
        assert served.get("/assets/app-abc123.js").text == "console.log(1)"

    def test_hashed_assets_are_cached_but_the_shell_is_not(self, served):
        assert "immutable" in served.get("/assets/app-abc123.js").headers["cache-control"]
        assert served.get("/").headers["cache-control"] == "no-cache"

    def test_the_manifest_is_typed_so_it_gets_read(self, served):
        assert "manifest+json" in served.get("/manifest.webmanifest").headers["content-type"]

    def test_unknown_api_paths_do_not_fall_through_to_the_shell(self, served):
        """殻が 200 で返ると、綴りを間違えた呼び出しの原因を追えなくなる。"""
        res = served.get("/api/nope")
        assert res.status_code == 404 and res.json()["error"]["code"] == "not_found"

    def test_it_will_not_serve_outside_the_directory(self, served):
        res = served.get("/../../etc/passwd")
        assert "passwd" not in res.text and "root:" not in res.text

    def test_without_a_build_it_says_so(self, api, tmp_path, monkeypatch):
        monkeypatch.setenv("CHIEZO_TASKS_STATIC_DIR", str(tmp_path / "missing"))
        res = api.get("/")
        assert res.status_code == 503 and res.json()["error"]["code"] == "no_static"
