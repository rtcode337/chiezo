"""収集の名前を変える(`collect.rename` / 管理画面の「名前を変える」)のテスト。

収集の名前は焼いた先のソース名でもある。**変えたあとも新しい名前で全部が続き、
元の名前は残らない**(別名は持たない)。焼いたファイルを動かすのは取り込み側なので、
ここでは `move` に差し込んだものが呼ばれたかと、設定の側が付いてきたかを見る
(ファイルの側は `tests/test_ingest_collect.py`)。
"""
from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi import HTTPException

from app import collect, collect_log, handoff, ingest_queue, repartition_job, search_queries, workers


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    return tmp_path


@pytest.fixture
def news(enabled):
    return collect.create("news", prompt="{cursor} 以降", interval_minutes=60)


def _moves():
    calls = []
    return calls, lambda name, to: calls.append((name, to))


def _status(fn) -> int:
    with pytest.raises(HTTPException) as got:
        fn()
    return got.value.status_code


class TestRenaming:
    def test_the_definition_moves_with_its_progress(self, news):
        """進み具合(カーソル・区画の台帳・前回の結果)ごと新しい名前へ移る。"""
        collect._put_one(replace(collect.get("news"), cursor="2026-10-01", last_added=7))
        calls, move = _moves()

        item = collect.rename("news", "daily_news", move=move)

        assert calls == [("news", "daily_news")]
        assert item.name == "daily_news"
        moved = collect.get("daily_news")
        assert (moved.cursor, moved.last_added) == ("2026-10-01", 7)
        # **元の名前は残らない**(別名は持たない)
        assert _status(lambda: collect.get("news")) == 404
        assert [c.name for c in collect.load()] == ["daily_news"]

    def test_references_from_other_collections_follow(self, news):
        """材料・抽出・区画の母集団・タグの確かめで指しているところを書き換える。"""
        collect.create(
            "topics", prompt="{material}", interval_minutes=60,
            material_spec={"source": "news", "tag": "記事"},
            extract_spec=[{"source": "news", "tag": "記事"}, {"source": "jawiki", "tag": "x"}],
            partition_spec={"by": "tag", "prefix": "地域:", "source": "news"},
            verify_tags=[{"prefix": "出典", "source": "news"}, {"prefix": "人", "source": "jawiki"}],
        )

        collect.rename("news", "daily_news", move=lambda *_: None)

        topics = collect.get("topics")
        assert topics.material["source"] == "daily_news"
        assert [s["source"] for s in topics.extract] == ["daily_news", "jawiki"]
        assert topics.partition["source"] == "daily_news"
        assert [v["source"] for v in topics.verify_tags] == ["daily_news", "jawiki"]
        # 親子の並び(`derives_from`)も新しい名前で辿れる
        assert collect.derives_from(topics) == "daily_news"

    def test_a_self_reference_follows_too(self, news):
        collect.update("news", partition={"by": "tag", "prefix": "地域:", "source": "news"})

        collect.rename("news", "daily_news", move=lambda *_: None)

        assert collect.get("daily_news").partition["source"] == "daily_news"

    def test_side_records_follow(self, news):
        """検索文の控え・手で回す束・割り直しの控え・変更履歴を持ち越す。"""
        search_queries.record("news", ["地震 速報"])
        handoff.put("news", sweep="既定", keys=[], shown=[], body="# 束")
        handoff.answered("news", [{"title": "見出し"}])
        repartition_job.finish("news", 12)
        collect_log.record("news", status=collect_log.STATUS_OK, diff={"added": 3})

        collect.rename("news", "daily_news", move=lambda *_: None)

        assert search_queries.current("daily_news") == ["地震 速報"]
        assert search_queries.history("news") == []
        assert handoff.get("news") is None
        assert handoff.body_of("daily_news") == "# 束"
        assert handoff.ready("daily_news")
        assert handoff.take("daily_news")[0] == [{"title": "見出し"}]
        assert repartition_job.state("daily_news")["partitions"] == 12
        assert repartition_job.state("news") == {}
        assert [r["name"] for r in collect_log.recent(name="daily_news")] == ["daily_news"]
        assert collect_log.recent(name="news") == []


class TestRefusing:
    """**断るときは何も動かさない**(`move` が呼ばれない)。"""

    def _refused(self, new_name="daily_news", **kwargs) -> int:
        calls, move = _moves()
        status = _status(lambda: collect.rename("news", new_name, move=move, **kwargs))
        assert calls == []
        assert collect.get("news").name == "news"
        return status

    def test_an_enabled_collection(self, news):
        collect.update("news", enabled=True)
        assert self._refused() == 409

    def test_one_in_the_worker_queue(self, news):
        workers.enqueue("w1", "news", "既定", "2026-10-10T00:00:00+00:00")
        assert self._refused() == 409

    def test_one_in_the_ingest_queue(self, news):
        ingest_queue.add("news", "既定", origin="manual")
        assert self._refused() == 409

    @pytest.mark.parametrize("field,value", [
        ("pending_sweep", "既定"),
        ("pending_focus", {"note": "座標を直す", "titles": ["見出し"]}),
        ("pending_run", {"partitions": ["東京"]}),
    ])
    def test_one_with_something_pending(self, news, field, value):
        collect._put_one(replace(collect.get("news"), **{field: value}))
        assert self._refused() == 409

    def test_a_collection_that_is_being_repartitioned(self, news):
        repartition_job.start("news")
        assert self._refused() == 409

    def test_a_taken_collection_name(self, news):
        collect.create("other", prompt="x", interval_minutes=60)
        assert self._refused("other") == 409

    @pytest.mark.parametrize("name", ["chiezo_memory", "chiezo_settings", "memory"])
    def test_a_system_source_name(self, news, name):
        assert self._refused(name) == 409

    def test_a_source_that_already_exists(self, news):
        """焼いてあるソース・取り込みのカタログに載っているものは呼ぶ側が渡す。"""
        assert self._refused("jawiki", taken={"jawiki"}) == 409

    @pytest.mark.parametrize("name", ["", "News", "x", "a-b", "../etc", "news"])
    def test_an_invalid_or_same_name(self, news, name):
        assert self._refused(name) == 400

    def test_an_unknown_collection(self, enabled):
        assert _status(lambda: collect.rename("nosuch", "daily_news")) == 404

    def test_a_collection_read_by_identity(self, news):
        """**元の記録の鍵(`identity`)で引かれている収集は断る** —— 鍵の頭にソース名が
        入って焼かれているので、名前を変えると突き合わせが外れる。"""
        collect.create(
            "shops", prompt="x", interval_minutes=60,
            extract_spec={"source": "news", "tag": "店", "identity": ["id"]},
        )
        calls, move = _moves()

        with pytest.raises(HTTPException) as got:
            collect.rename("news", "daily_news", move=move)

        assert got.value.status_code == 409
        assert "shops" in got.value.detail["error"]
        assert calls == []

    def test_a_failed_move_changes_nothing(self, news):
        """取り込みが断ったら、設定の側は何も変えない。"""
        def move(_name, _to):
            raise HTTPException(409, {"error": "ingest is running: news"})

        assert _status(lambda: collect.rename("news", "daily_news", move=move)) == 409
        assert collect.get("news").name == "news"
        assert _status(lambda: collect.get("daily_news")) == 404

    def test_a_failure_after_the_move_says_so(self, news, monkeypatch):
        """ファイルが動いたあとで落ちたら、黙らずにそう言う。"""
        def broken(*_):
            raise OSError("disk full")

        monkeypatch.setattr(collect_log, "rename", broken)

        with pytest.raises(HTTPException) as got:
            collect.rename("news", "daily_news", move=lambda *_: None)

        assert got.value.status_code == 500
        assert "daily_news" in got.value.detail["error"]
        assert "disk full" not in str(got.value.detail)


class TestTheRoute:
    @pytest.fixture
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        from app.views import admin

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.test")
        monkeypatch.setattr(admin, "initializable_sources", lambda: {"osm_japan": {}})
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_it_renames_through_the_trigger(self, client, news, monkeypatch):
        from app.views import admin

        calls, move = _moves()
        monkeypatch.setattr(admin, "_rename_source", move)

        res = _rename(client, "daily_news")

        assert res.status_code == 303
        assert collect.get("daily_news").name == "daily_news"
        assert calls == [("news", "daily_news")]

    @pytest.mark.parametrize("taken", ["jawiki", "osm_japan"])
    def test_names_of_existing_sources_are_refused(self, client, news, monkeypatch, taken):
        """焼いてあるソース(jawiki)も、カタログにだけ載っているソースも使えない。"""
        from app.views import admin

        calls, move = _moves()
        monkeypatch.setattr(admin, "_rename_source", move)

        res = _rename(client, taken)

        assert res.status_code == 409
        assert calls == []

    def test_a_trigger_refusal_comes_back(self, client, news, monkeypatch):
        from app.views import admin

        def refuse(name, _to):
            raise HTTPException(409, {"error": f"ingest is running: {name}"})

        monkeypatch.setattr(admin, "_rename_source", refuse)

        res = _rename(client, "daily_news")

        assert res.status_code == 409
        assert collect.get("news").name == "news"

    def test_the_admin_form_redirects_to_the_new_page(self, client, news, monkeypatch):
        from app.views import admin

        monkeypatch.setattr(admin, "_rename_source", lambda *_: None)

        res = client.post(
            "/admin/collect/news/rename", data={"to": "daily_news"}, follow_redirects=False,
        )

        assert res.status_code == 303
        assert res.headers["location"] == "/admin/collect/daily_news"

    def test_the_form_shows_only_on_a_stopped_collection(self, client, news):
        assert "/admin/collect/news/rename" in client.get("/admin/collect/news").text
        collect.update("news", enabled=True)
        html = client.get("/admin/collect/news").text
        assert "/admin/collect/news/rename" not in html
        assert "止めると名前を変えられます" in html


def _rename(client, to):
    """管理画面の「名前を変える」を押す(名前の変更は管理画面にしか無い)。"""
    return client.post("/admin/collect/news/rename", data={"to": to}, follow_redirects=False)


def test_there_is_no_rest_route(tmp_path, monkeypatch):
    """ファイルごと動く操作なので、外のアプリからは呼べないこと。"""
    from app.main import app

    paths = {getattr(r, "path", "") for r in app.routes}
    assert "/v1/collect/{name}/rename" not in paths
