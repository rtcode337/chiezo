"""ToDo の REST(`/v1/todo/...`)。LAN の中のアプリがタスクとプロジェクトを読み書きする口。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import tasks


@pytest.fixture()
def client(tmp_path, built_data_dir, monkeypatch):
    # タスクはメモの置き場、プロジェクトは設定の置き場に入るので両方が要る
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


def test_projects_can_be_listed_and_created(client: TestClient):
    r = client.post("/v1/todo/projects", json={"name": "sample-app", "description": "見本"})
    assert r.status_code == 201
    assert r.json()["name"] == "sample-app"

    names = [p["name"] for p in client.get("/v1/todo/projects").json()["projects"]]
    assert "sample-app" in names


def test_a_task_round_trips_with_app_data(client: TestClient):
    client.post("/v1/todo/projects", json={"name": "sample-app"})
    r = client.post("/v1/todo/tasks", json={
        "title": "ログを直す", "body": "詳しくは issue", "project": "sample-app",
        "flagged": True, "data": {"due_on": "2026-10-24"},
    })
    assert r.status_code == 201
    created = r.json()
    assert created["title"] == "ログを直す"
    assert created["body"] == "詳しくは issue"
    assert created["project"] == "sample-app"
    assert created["flagged"] is True
    assert created["status"] == "todo"
    assert created["data"] == {"due_on": "2026-10-24"}

    listed = client.get("/v1/todo/tasks", params={"project": "sample-app"}).json()["tasks"]
    assert [t["id"] for t in listed] == [created["id"]]


def test_patch_changes_only_what_was_sent_and_keeps_other_extra(client: TestClient):
    created = client.post("/v1/todo/tasks", json={"title": "考える", "data": {"a": 1}}).json()
    before = tasks._extra_of(tasks._row(created["id"]))
    assert "created_at" in before

    r = client.patch(f"/v1/todo/tasks/{created['id']}", json={"status": "done"})
    assert r.json()["status"] == "done"
    assert r.json()["data"] == {"a": 1}

    r = client.patch(f"/v1/todo/tasks/{created['id']}", json={"data": {"a": 2}})
    assert r.json()["data"] == {"a": 2}
    assert r.json()["status"] == "done"
    after = tasks._extra_of(tasks._row(created["id"]))
    assert after["created_at"] == before["created_at"]


def test_unassigned_tasks_and_unlinking(client: TestClient):
    client.post("/v1/todo/projects", json={"name": "sample-app"})
    created = client.post("/v1/todo/tasks", json={"title": "移す", "project": "sample-app"}).json()

    client.patch(f"/v1/todo/tasks/{created['id']}", json={"project": ""})
    unassigned = client.get("/v1/todo/tasks", params={"project": ""}).json()["tasks"]
    assert [t["id"] for t in unassigned] == [created["id"]]


def test_delete_and_not_found(client: TestClient):
    created = client.post("/v1/todo/tasks", json={"title": "消す"}).json()
    assert client.delete(f"/v1/todo/tasks/{created['id']}").status_code == 204
    assert client.get(f"/v1/todo/tasks/{created['id']}").status_code == 404
    assert client.patch(f"/v1/todo/tasks/{created['id']}", json={"data": {}}).status_code == 404


def test_an_unknown_project_is_refused(client: TestClient):
    r = client.post("/v1/todo/tasks", json={"title": "どこか", "project": "ないプロジェクト"})
    assert r.status_code == 404
