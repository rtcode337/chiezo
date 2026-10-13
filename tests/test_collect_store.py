"""置き場へ書く収集(`Collection.store`)のテスト。

**押さえているのは、この形でしか起きないこと**:
①作るときに、置き場へ書く収集で意味を持たない指定を断る、
②依頼元が起こす巡回の 1 回が、取り込みを通さずに置き場へ書く、
③同じ見出しを書き直さない(墓標でも消さない)、
④止めていれば断り、走っている最中なら 409、
⑤`{names}` に入るのは、その回のタグを全部持つものだけ。

置き場には会話で覚えたこと(アプリが直接書く)も並ぶので、範囲の外を読んだり
書き換えたりしないことが、この形の芯。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import collect, notes

STORE = "matoi_hina_memory"


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """定義の置き場・名前付きの置き場・取り込みの相手を用意する(収集が有効になる条件)。"""
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    notes.create_store(STORE, set())
    return tmp_path


def _create(**extra):
    """キャラ 1 人ぶんの収集(収集のタグ「学習」、興味ごとに巡回 1 本)。"""
    fields = {
        "name": "hina_learning",
        "prompt": "覚えていること:\n{names}\n\n新しく調べて覚えることを返して",
        "interval_minutes": 60,
        "store": STORE,
        "store_tags": ["学習"],
        "sweeps": [{"name": "天文", "by_caller": True, "store_tags": ["興味:天文"]}],
        **extra,
    }
    return collect.create(**fields)


def _remember(title: str, tags: list[str], body: str = "本文") -> None:
    notes.add(body, title=title, tags=",".join(tags), store=STORE)


class TestCreating:
    def test_a_store_collection_keeps_its_target(self, enabled):
        item = _create()

        assert item.store == STORE
        assert item.store_tags == ["学習"]
        assert collect.sweeps_of(item)[0].store_tags == ["興味:天文"]
        # **焼く先を持たない**ので、取り込みのカタログに出さない
        assert "hina_learning" not in {c["name"] for c in collect.catalog()}

    def test_the_store_must_exist(self, enabled):
        """書いた拍子には作らない(打ち間違いが黙って新しい置き場を生む)。"""
        with pytest.raises(HTTPException) as e:
            _create(store="matoi_nobody_memory")
        assert e.value.status_code == 400

    def test_the_short_term_memory_is_not_a_target(self, enabled):
        notes.ensure_db()
        with pytest.raises(HTTPException) as e:
            _create(store=notes.SOURCE_NAME)
        assert e.value.status_code == 400

    @pytest.mark.parametrize("extra", [
        {"partition_spec": {"by": "title", "target": 10}},
        {"feed_spec": {"urls": ["https://example.com/feed.xml"]}},
        {"links": True},
        {"verify_tags": [{"prefix": "代表作", "source": "jawiki"}]},
        {"kind": collect.KIND_FLOW},
        {"keep_days": 7},
        {"locked_doubt_tag": "要確認"},
    ])
    def test_settings_that_mean_nothing_here_are_refused(self, enabled, extra):
        """黙って無視すると、書いた人は効くと思ったまま回る。"""
        with pytest.raises(HTTPException) as e:
            _create(**extra)
        assert e.value.status_code == 400
        assert "置き場へ書く収集では使えない" in e.value.detail["error"]

    @pytest.mark.parametrize("sweep", [
        {"name": "天文", "by_caller": True, "use_feed": True},
        {"name": "天文", "by_caller": True, "cover_days": 7},
        {"name": "天文", "by_caller": True, "unreviewed_per_run": 5},
        {"name": "天文", "by_caller": True, "by_hand": True},
    ])
    def test_sweep_settings_that_mean_nothing_here_are_refused(self, enabled, sweep):
        with pytest.raises(HTTPException) as e:
            _create(sweeps=[sweep])
        assert e.value.status_code == 400

    def test_every_sweep_is_woken_by_the_caller(self, enabled):
        """時計で回すと取り込みの待ち行列に乗り、焼く先の無い取り込みが起きる。"""
        with pytest.raises(HTTPException) as e:
            _create(sweeps=[{"name": "天文", "store_tags": ["興味:天文"]}])
        assert "時計で回る" in e.value.detail["error"]
        with pytest.raises(HTTPException):
            _create(sweeps=None)

    def test_a_sweep_needs_some_tag(self, enabled):
        """タグが無いと、会話で覚えたことまで含めた置き場の全部が範囲になる。"""
        with pytest.raises(HTTPException) as e:
            _create(store_tags=[], sweeps=[{"name": "何でも", "by_caller": True}])
        assert "タグがありません" in e.value.detail["error"]

    def test_store_tags_need_a_store(self, enabled):
        with pytest.raises(HTTPException):
            collect.create(name="plain", prompt="集めて", interval_minutes=60, store_tags=["学習"])

    def test_the_clock_cannot_be_put_back(self, enabled):
        _create()
        with pytest.raises(HTTPException):
            collect.set_sweep_by_caller("hina_learning", "天文", False)
        assert collect.sweeps_of(collect.get("hina_learning"))[0].by_caller is True

    def test_it_cannot_switch_to_baking(self, enabled):
        """置き場へ書くかどうかは、作ったあとでは変えない。"""
        _create()
        with pytest.raises(HTTPException):
            collect.update("hina_learning", store="")

    def test_a_stopped_collection_can_be_toggled_after_its_store_is_gone(self, enabled):
        """空の置き場は画面から消せる。消えたあとに止める・有効にするまで断らない。"""
        _create()
        notes.delete_store(STORE)
        collect.update("hina_learning", enabled=True)
        assert collect.get("hina_learning").enabled is True


class TestTheScope:
    def test_names_holds_only_what_has_all_the_tags(self, enabled):
        """`{names}` に入るのは、その回のタグ(収集 + 巡回)を全部持つものだけ。"""
        item = _create()
        _remember("木星の衛星", ["学習", "興味:天文"])
        _remember("昨日の夕飯", ["会話"])
        _remember("特撮の歴史", ["学習", "興味:特撮"])
        sweep = collect.sweeps_of(item)[0]
        tags = collect.store_tags_for(item, sweep)

        previous = collect.store_docs(STORE, tags)
        messages = collect.build_store_messages(item, sweep, previous)

        assert tags == ["学習", "興味:天文"]
        assert "木星の衛星" in messages[-1]["content"]
        assert "昨日の夕飯" not in messages[-1]["content"]
        assert "特撮の歴史" not in messages[-1]["content"]
        # 直すことも消すこともできない回なので、整理の回の約束は渡さない
        assert messages[0]["content"] == collect.STORE_SYSTEM_PROMPT


class TestRunning:
    @pytest.fixture
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    @pytest.fixture
    def answering(self, monkeypatch):
        """AI の代わり(`main._ask_for_collection`)。誰の名前で呼ばれたかも控える。"""
        from app import ai_inflight, main

        heard: list[dict] = []
        reply = {"content": "{}"}

        async def fake(_item, messages):
            heard.append({"caller": ai_inflight.current_caller(), "messages": messages})
            return reply["content"], "codex", "gpt-6"

        monkeypatch.setattr(main, "_ask_for_collection", fake)
        return heard, reply

    def test_a_run_writes_into_the_store(self, client, answering):
        heard, reply = answering
        _create()
        collect.update("hina_learning", enabled=True)
        reply["content"] = (
            '{"items": [{"title": "土星の環", "body": "氷の粒でできている",'
            ' "tags": ["惑星"], "url": "https://example.com/saturn"}]}'
        )

        res = client.post("/v1/collect/hina_learning/run", params={"sweep": "天文"})

        assert res.status_code == 200, res.text
        assert res.json()["running"] is True and res.json()["queued"] is False
        got = notes.recall(tag="学習", store=STORE, fields="title,tags,extra")["notes"]
        assert [n["title"] for n in got] == ["土星の環"]
        assert got[0]["tags"] == ["学習", "興味:天文", "惑星"]
        assert got[0]["extra"] == {"url": "https://example.com/saturn"}
        # **依頼の履歴・使用量に収集の名前で残る**
        assert heard[0]["caller"] == "collect:hina_learning"
        # **回の結果は焼く収集と同じ控えに残る**(走り終えたら印も外れる)
        item = collect.get("hina_learning")
        assert (item.last_status, item.last_added) == ("ok", 1)
        assert collect.store_running("hina_learning") == {}
        from app import collect_log

        assert collect_log.recent("hina_learning")[0]["added"] == 1

    def test_the_same_title_is_not_written_again(self, client, answering):
        """覚え直さない。墓標が返っても、置き場の中身は消さない。"""
        _heard, reply = answering
        _create()
        collect.update("hina_learning", enabled=True)
        _remember("木星の衛星", ["学習", "興味:天文"], body="前に覚えたこと")
        reply["content"] = (
            '{"items": [{"title": "木星の衛星", "body": "書き換えたい"},'
            ' {"title": "火星の季節", "body": "新しく覚えたこと"},'
            ' {"title": "火星の季節", "body": "同じ回の 2 度目"},'
            f' {{"title": "木星の衛星", "body": "消して", "tags": ["{notes.TOMBSTONE_TAG}"]}}]}}'
        )

        client.post("/v1/collect/hina_learning/run", params={"sweep": "天文"})

        got = {n["title"]: n["text"] for n in notes.recall(store=STORE)["notes"]}
        assert got == {"木星の衛星": "前に覚えたこと", "火星の季節": "新しく覚えたこと"}
        item = collect.get("hina_learning")
        assert (item.last_added, item.last_skipped) == (1, 2)

    def test_a_stopped_collection_is_refused(self, client, answering):
        heard, _reply = answering
        _create()

        res = client.post("/v1/collect/hina_learning/run", params={"sweep": "天文"})

        assert res.status_code == 403
        assert heard == []

    def test_a_running_collection_is_refused(self, client, answering):
        """同じ範囲を 2 本が読むと、同じ見出しが `(番号)` 付きで並ぶ。"""
        heard, _reply = answering
        _create()
        collect.update("hina_learning", enabled=True)
        assert collect.begin_store_run("hina_learning", "天文") is True

        res = client.post("/v1/collect/hina_learning/run", params={"sweep": "天文"})

        assert res.status_code == 409
        assert heard == []

    def test_it_never_goes_through_the_ingest(self, client, enabled):
        """待ち行列から流れてきても、取り込みは起こさない(焼く先が無い)。"""
        from app import main

        _create()
        with pytest.raises(HTTPException) as e:
            main.start_collection_bake("hina_learning", "天文")
        assert e.value.status_code == 400

    def test_a_failure_is_kept_and_the_mark_comes_off(self, client, answering):
        _heard, reply = answering
        _create()
        collect.update("hina_learning", enabled=True)
        reply["content"] = "JSON ではない答え"

        res = client.post("/v1/collect/hina_learning/run", params={"sweep": "天文"})

        assert res.status_code == 200
        item = collect.get("hina_learning")
        assert item.last_status == "error"
        assert collect.store_running("hina_learning") == {}
        assert notes.recall(store=STORE)["total"] == 0

    def test_the_page_shows_where_it_writes(self, client, answering):
        _create()
        _remember("木星の衛星", ["学習", "興味:天文"])
        _remember("昨日の夕飯", ["会話"])

        page = client.get("/admin/collect/hina_learning").text
        listing = client.get("/admin/collect").text

        assert "置き場 <a" in page and STORE in page
        assert "1 件" in page and "1 件" in listing
        # 区画・焼き直しの口は出さない
        assert "/admin/collect/hina_learning/preview" not in page
        assert "/rollback" not in page and "/admin/collect/hina_learning/repartition" not in page


def test_the_sweep_form_keeps_the_caller_mark_and_tags():
    """画面から保存しても、依頼元が起こす印とタグが落ちない(落ちると時計で回り始める)。"""
    from starlette.datastructures import FormData

    from app.views import admin

    parsed = admin._parse_sweeps_form(FormData([
        ("sweep_name", "天文"), ("sweep_clock", "by_caller"), ("sweep_enabled", "1"),
        ("sweep_store_tags", "興味:天文, 星座"),
    ]))

    assert parsed[0]["by_caller"] is True
    assert collect.normalize_sweeps(parsed)[0]["store_tags"] == ["興味:天文", "星座"]
