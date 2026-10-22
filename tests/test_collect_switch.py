"""外のアプリが自分の収集を止める・動かす(`collect.switch_from_outside` / `POST /v1/collect/{name}/enabled`)。

動かすかどうかは Chiezo 側が決める —— **動かせるのは、管理画面で「外部からの有効化」を許可したときだけ**で、
それも依頼元が自分で依頼した収集に限る。止めるのは許可しなくてもできる(止めて AI が勝手に動き出すことは無い)。
"""
from __future__ import annotations

import pytest

from app import collect


@pytest.fixture
def client(tmp_path, monkeypatch, built_data_dir):
    from fastapi.testclient import TestClient

    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        collect.create("app_news", prompt="{cursor} 以降", interval_minutes=60, requested_by="someapp")
        yield c


def switch(client, enabled: bool, who: str = "someapp"):
    return client.post("/v1/collect/app_news/enabled", json={"enabled": enabled, "requested_by": who})


def test_enabling_needs_to_be_allowed_in_chiezo(client):
    res = switch(client, True)
    assert res.status_code == 403
    assert "外部からの有効化を許可していません" in res.json()["error"]
    assert not collect.get("app_news").enabled

    # 管理画面の収集の面のボタン 1 つで許可する(依頼元の名前は並べない)
    page = client.get("/admin/collect").text
    assert "外部からの有効化を許可する</button>" in page
    assert 'value="someapp"' not in page
    client.post("/admin/collect/outside-switch", data={"allowed": "1"})
    assert collect.outside_switch_allowed()
    assert "外部からの有効化をやめる</button>" in client.get("/admin/collect").text

    res = switch(client, True)
    assert res.status_code == 200 and res.json()["enabled"] is True
    assert collect.get("app_news").enabled

    # やめれば、また動かせなくなる(止めるのはできる)
    client.post("/admin/collect/outside-switch", data={"allowed": "0"})
    assert switch(client, False).status_code == 200
    assert switch(client, True).status_code == 403


def test_stopping_needs_no_handover(client):
    collect.update("app_news", enabled=True)
    assert switch(client, False).status_code == 200
    assert not collect.get("app_news").enabled


def test_only_the_requester_can_switch(client):
    collect.set_outside_switch(True)
    collect.update("app_news", enabled=True)
    assert switch(client, False, who="otherapp").status_code == 403
    assert switch(client, True, who="otherapp").status_code == 403
    assert collect.get("app_news").enabled


def test_stopped_collections_stand_out(client):
    """止めてある収集は、一覧の行に色を付けて「停止中」と出し、収集の面の頭に帯を出す。"""
    listing = client.get("/admin/collect").text
    assert "collect-list" in listing
    assert '<tr class="off">' in listing and "停止中" in listing
    page = client.get("/admin/collect/app_news").text
    assert 'class="stopped-banner"' in page

    collect.update("app_news", enabled=True)
    assert 'class="stopped-banner"' not in client.get("/admin/collect/app_news").text
