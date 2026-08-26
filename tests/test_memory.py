"""記憶の固化(短期記憶 → 長期記憶)のテスト。

配信側が配る素材を、取り込み側(`ingest/sources/remote.py`)の仕掛けで実際に焼くところ
まで通す。契約を挟んで両側が分かれているので、片側だけのモックで済ませると
「配れてはいるが焼けない」が通ってしまう。
"""
import pytest
from fastapi.testclient import TestClient

TARGET = "固化対象"
DONE = "固化"
TOMB = "削除"


@pytest.fixture()
def data_dir(tmp_path):
    """長期記憶の置き場(空から始める。固化したソースだけがここに増える)。"""
    d = tmp_path / "corpus"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


def remember(client, text, tags=None, title=None):
    res = client.post("/v1/notes", json={"text": text, "tags": tags, "title": title})
    assert res.status_code == 200, res.text
    return res.json()


def mark(client, doc_id, tags):
    """`固化対象` を付ける。専用の口は無く、ただのタグ。"""
    res = client.patch(f"/v1/notes/{doc_id}", json={"tags": tags})
    assert res.status_code == 200, res.text
    return res.json()


def consolidate(client, data_dir):
    """配信側が配る素材を取り込み側で焼き、登録し直す(固化の 1 往復)。"""
    import main as ingest_main

    from app import main, memory
    from sources.remote import RemotePluginAdapter, RemoteSource

    res = client.get("/v1/memory/fetch")
    assert res.status_code == 200, res.text
    ndjson = data_dir / "memory.ndjson"
    ndjson.write_text(res.text, encoding="utf-8")

    adapter = RemotePluginAdapter(
        RemoteSource(base_url="http://chiezo-app:7010/v1/memory",
                     name=memory.SOURCE_NAME, kind="memory")
    )
    # 1 行目の meta から日付と検証条件を受け取るところも本番と同じ経路で通す
    dump_date = adapter._apply_meta(ndjson)
    building = data_dir / f"{memory.SOURCE_NAME}-{dump_date}.db.building"
    ingest_main.build_db(adapter, ndjson, dump_date, building)
    ingest_main.validate_db(adapter, building)
    ingest_main.switch_db(data_dir, memory.SOURCE_NAME, dump_date, building)

    # 本番は 5 秒ごとの再走査が拾う。テストでは待たずに反映させる
    main.refresh_sources(client.app)
    return dump_date


class TestItIsABuiltInSource:
    """固化は取り込み側の組み込みソース。設定を足さなくても使える。"""

    def test_the_ingest_side_knows_it(self):
        from sources import ADAPTERS

        assert "memory" in ADAPTERS

    def test_it_points_at_the_app_without_configuration(self, monkeypatch):
        """URL は決まっている(プラグインと違って相手が必ず chiezo-app なので)。"""
        from sources import ADAPTERS

        adapter = ADAPTERS["memory"]()
        assert adapter.src.base_url == "http://chiezo-app:7010/v1/memory"

        monkeypatch.setenv("CHIEZO_APP_URL", "http://elsewhere:7010/")
        assert ADAPTERS["memory"]().src.base_url == "http://elsewhere:7010/v1/memory"

    def test_the_cli_can_run_it(self):
        """`SOURCE=memory` で CLI から回せること(get_adapter が解決できる)。"""
        from sources import get_adapter

        assert get_adapter("memory").source == "memory"


class TestMaterial:
    def test_only_marked_notes_are_burned(self, client, data_dir):
        keep = remember(client, "残す価値のある決まり", title="決まり", tags="rule")
        remember(client, "まだ判断していないメモ", title="そのうち")
        mark(client, keep["doc_id"], f"rule,{TARGET}")
        consolidate(client, data_dir)

        titles = [
            r["title"] for r in
            client.get("/v1/memory/search", params={"q": "決まり"}).json()["results"]
        ]
        assert titles == ["決まり"]

    def test_the_source_is_searchable_like_any_other(self, client, data_dir):
        note = remember(client, "コミット前に変更点を説明する", title="コミットの流儀")
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)

        doc = client.get("/v1/memory/doc", params={"title": "コミットの流儀"}).json()
        assert "変更点を説明" in doc["body"]

    def test_the_marks_themselves_are_not_burned(self, client, data_dir):
        """段取りのタグは長期側では意味を持たない(全員がそうなので)。"""
        note = remember(client, "本文", title="決まり", tags="rule")
        mark(client, note["doc_id"], f"rule,{TARGET}")
        consolidate(client, data_dir)

        doc = client.get("/v1/memory/doc", params={"title": "決まり", "fields": "tags"}).json()
        assert doc["tags"] == ["rule"]

    def test_refuses_when_nothing_is_marked(self, client):
        remember(client, "印の付いていないメモ")
        res = client.get("/v1/memory/fetch")
        assert res.status_code == 409
        assert "nothing to consolidate" in res.json()["error"]

    def test_an_unknown_source_name_is_refused(self, client):
        assert client.get("/v1/memory/fetch", params={"source": "nope"}).status_code == 404


class TestUpdateAndDelete:
    def test_an_update_wins_over_the_previous_generation(self, client, data_dir):
        note = remember(client, "古い内容", title="決まり")
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)
        before = client.get(
            "/v1/memory/doc", params={"title": "決まり", "fields": "doc_id,body"}
        ).json()
        client.post("/v1/memory/sweep")

        # 本文を直すと固化の印が外れるので、もう一度 `固化対象` を付けて焼き直す
        client.patch(f"/v1/notes/{note['doc_id']}", json={"text": "新しい内容"})
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)

        after = client.get(
            "/v1/memory/doc", params={"title": "決まり", "fields": "doc_id,body"}
        ).json()
        assert after["body"] == "新しい内容"
        assert after["doc_id"] == before["doc_id"]

    def test_a_tombstone_drops_the_document(self, client, data_dir):
        gone = remember(client, "消される決まり", title="要らない決まり")
        stay = remember(client, "残る決まり", title="残す決まり")
        for note in (gone, stay):
            mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")

        mark(client, gone["doc_id"], f"{TARGET},{TOMB}")
        consolidate(client, data_dir)

        titles = {
            r["title"] for r in
            client.get("/v1/memory/search", params={"q": "決まり"}).json()["results"]
        }
        assert titles == {"残す決まり"}

    def test_refuses_to_empty_the_long_term_memory(self, client, data_dir):
        note = remember(client, "唯一の決まり", title="決まり")
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")
        mark(client, note["doc_id"], f"{TARGET},{TOMB}")

        res = client.get("/v1/memory/fetch")
        assert res.status_code == 409
        assert "would empty" in res.json()["error"]

    def test_the_previous_generation_survives(self, client, data_dir):
        """印を付け替えた後の焼き直しで、前に入れたものが消えないこと。"""
        old = remember(client, "残ってほしい", title="古株")
        mark(client, old["doc_id"], TARGET)
        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")

        new = remember(client, "あとから来た", title="新入り")
        mark(client, new["doc_id"], TARGET)
        consolidate(client, data_dir)

        # 3 文字未満の語は全文検索に載らないので、見出しで直に引く
        for title in ("古株", "新入り"):
            assert client.get(
                "/v1/memory/doc", params={"title": title}
            ).status_code == 200, title

    def test_a_second_burn_on_the_same_day_keeps_the_previous_generation(self, client, data_dir):
        """世代ファイルは日付だけだと 2 回目が前世代を上書きしてしまう。"""
        note = remember(client, "古い内容", title="決まり")
        mark(client, note["doc_id"], TARGET)
        first = consolidate(client, data_dir)
        client.post("/v1/memory/sweep")

        client.patch(f"/v1/notes/{note['doc_id']}", json={"text": "新しい内容"})
        mark(client, note["doc_id"], TARGET)
        second = consolidate(client, data_dir)

        assert first != second
        generations = sorted(p.name for p in data_dir.glob("memory-*.db"))
        assert generations == [f"memory-{first}.db", f"memory-{second}.db"]


class TestSweep:
    def test_refuses_before_anything_is_burned(self, client):
        note = remember(client, "まだ焼いていない")
        mark(client, note["doc_id"], TARGET)
        res = client.post("/v1/memory/sweep")
        assert res.status_code == 409
        assert "not consolidated yet" in res.json()["error"]

    def test_the_mark_moves_from_target_to_done(self, client, data_dir):
        note = remember(client, "焼かれる決まり", title="決まり", tags="rule")
        mark(client, note["doc_id"], f"rule,{TARGET}")
        consolidate(client, data_dir)

        assert client.post("/v1/memory/sweep").json()["marked"] == 1
        tags = client.get(f"/v1/notes/doc/{note['doc_id']}").json()["tags"]
        assert DONE in tags and TARGET not in tags
        # メモとして付けていたタグは残す
        assert "rule" in tags

    def test_a_tombstone_is_marked_only_when_the_target_is_gone(self, client, data_dir):
        gone = remember(client, "消される決まり", title="要らない決まり")
        stay = remember(client, "残る決まり", title="残す決まり")
        for note in (gone, stay):
            mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")

        mark(client, gone["doc_id"], f"{TARGET},{TOMB}")
        # まだ焼き直していないので、長期側には残っている = 反映されていない
        assert client.post("/v1/memory/sweep").json()["marked"] == 0

        consolidate(client, data_dir)
        assert client.post("/v1/memory/sweep").json()["marked"] == 1

    def test_consolidated_notes_drop_out_of_recall(self, client, data_dir):
        note = remember(client, "焼かれる決まり", title="決まり")
        remember(client, "まだ短期にいる", title="決めたこと")
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")

        titles = [n["title"] for n in client.get("/v1/notes/recall").json()["notes"]]
        assert titles == ["決めたこと"]

    def test_search_still_sees_consolidated_notes(self, client, data_dir):
        """隠すのは時系列の想起だけ。検索は今までどおり全部見せる。"""
        note = remember(client, "焼かれる決まり", title="決まり")
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")

        hits = client.get("/v1/notes/search", params={"q": "焼かれる決まり"}).json()["results"]
        assert [h["title"] for h in hits] == ["決まり"]


class TestStatus:
    def test_it_reports_the_queue(self, client, data_dir):
        note = remember(client, "まだ焼いていない")
        mark(client, note["doc_id"], TARGET)

        state = client.get("/v1/memory/status").json()
        assert state["pending"] == 1 and state["consolidated"] is False

        consolidate(client, data_dir)
        client.post("/v1/memory/sweep")
        state = client.get("/v1/memory/status").json()
        assert state["pending"] == 0 and state["consolidated"] is True
        assert state["docs"] == 1

    def test_it_names_the_tags(self, client):
        """画面と AI が同じ語彙を見るように、状態から引ける。"""
        tags = client.get("/v1/memory/status").json()["tags"]
        assert tags == {"target": TARGET, "done": DONE}


class TestAdminScreen:
    def test_it_shows_the_queue(self, client):
        note = remember(client, "まだ焼いていない")
        mark(client, note["doc_id"], TARGET)
        html = client.get("/admin").text
        assert "短期記憶から移す(固化)" in html
        assert "固化を待っているメモ" in html

    def test_the_burn_button_is_disabled_with_an_empty_queue(self, client):
        """焼くものが無いときは押せないこと。

        押せると取り込みが始まってすぐ 409 で落ち、画面には HTTP のステータスしか
        残らない(何をすれば直るのかが読めない)。
        """
        remember(client, "印の付いていないメモ")
        html = client.get("/admin").text
        assert "焼くものが無いので" in html

    def test_sweeping_from_the_form_moves_the_mark(self, client, data_dir):
        note = remember(client, "焼かれる決まり", title="決まり")
        mark(client, note["doc_id"], TARGET)
        consolidate(client, data_dir)

        res = client.post("/admin/memory/sweep", follow_redirects=False)
        assert res.status_code == 303
        assert DONE in client.get(f"/v1/notes/doc/{note['doc_id']}").json()["tags"]
