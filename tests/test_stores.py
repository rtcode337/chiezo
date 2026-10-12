"""名前付きの置き場(外のアプリが自分の記録・設定を預ける)のテスト。"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def notes_dir(tmp_path, monkeypatch):
    directory = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(directory))
    return directory


@pytest.fixture()
def client(notes_dir, built_data_dir, monkeypatch):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


def _sources(client) -> dict:
    return {s["name"]: s for s in client.get("/v1/sources").json()["sources"]}


class TestCreate:
    def test_creates_a_store_and_registers_it_as_a_source(self, client, notes_dir):
        res = client.post("/v1/stores", json={"name": "myapp"})

        assert res.status_code == 201
        assert (notes_dir / "myapp.db").exists()
        # 作った時点で普通のソースとして読める(次の再走査を待たない)
        assert _sources(client)["myapp"]["kind"] == "chiezo_memory"
        assert [s["name"] for s in client.get("/v1/stores").json()["stores"]] == ["myapp"]

    def test_the_same_name_is_refused(self, client):
        """後から来たアプリが「作れた」と思い込んで、前の中身に書き足すのを防ぐ。"""
        assert client.post("/v1/stores", json={"name": "myapp"}).status_code == 201
        assert client.post("/v1/stores", json={"name": "myapp"}).status_code == 409

    def test_names_of_existing_sources_are_refused(self, client):
        assert client.post("/v1/stores", json={"name": "chiezo_memory"}).status_code == 409
        taken = next(n for n in _sources(client) if n != "chiezo_memory")
        assert client.post("/v1/stores", json={"name": taken}).status_code == 409

    def test_names_that_do_not_fit_a_source_name_are_refused(self, client):
        assert client.post("/v1/stores", json={"name": "Myapp"}).status_code == 400
        assert client.post("/v1/stores", json={"name": "myapp/x"}).status_code == 400


class TestReadWrite:
    @pytest.fixture()
    def store(self, client):
        client.post("/v1/stores", json={"name": "myapp"})
        return client

    def test_write_and_recall_with_extra(self, store):
        created = store.post(
            "/v1/stores/myapp",
            json={"text": "見た: 葛飾北斎", "tags": "見た記録,画家", "extra": {"seen_on": "2026-10-09"}},
        ).json()

        # 普通のソースなので、文書の URL もその名前で引ける
        assert "/myapp/" in created["url"]
        got = store.get("/v1/stores/myapp/recall", params={"tag": "画家", "fields": "doc_id,title,extra"}).json()
        assert got["source"] == "myapp"
        assert got["notes"] == [
            {"doc_id": created["doc_id"], "title": "見た: 葛飾北斎", "extra": {"seen_on": "2026-10-09"}}
        ]

    def test_it_does_not_mix_with_the_short_term_memory(self, store):
        """短期記憶を探した AI に、アプリの設定が混ざって出ないこと(置き場を分けた理由)。"""
        store.post("/v1/stores/myapp", json={"text": "興味トピック: LLM"})

        assert store.get("/v1/chiezo_memory/recall").json()["notes"] == []
        assert len(store.get("/v1/stores/myapp/recall").json()["notes"]) == 1

    def test_update_and_forget(self, store):
        doc_id = store.post("/v1/stores/myapp", json={"text": "読んだ: 本 A"}).json()["doc_id"]

        updated = store.patch(f"/v1/stores/myapp/{doc_id}", json={"tags": "読んだ記録"}).json()
        assert updated["tags"] == ["読んだ記録"]
        assert store.delete(f"/v1/stores/myapp/{doc_id}").json() == {"deleted": doc_id}
        assert store.get("/v1/stores/myapp/recall").json()["notes"] == []

    def test_a_store_that_was_not_created_is_404(self, store):
        """打ち間違えた名前で書いても、黙って新しい置き場ができないこと。"""
        assert store.post("/v1/stores/myappi", json={"text": "x"}).status_code == 404
        assert store.get("/v1/stores/myappi/recall").status_code == 404

    def test_a_name_outside_the_rule_cannot_point_at_other_files(self, store, notes_dir):
        """置き場の名前は URL から来る。規則に合わない名前でほかのファイルを指させない。"""
        from app import notes

        # 置き場の並びの外に、それらしい DB を置いておく
        (notes_dir.parent / "outside.db").write_bytes(b"")
        for name in ("..", "Myapp", "myapp-x", "%2E%2E"):
            assert store.get(f"/v1/stores/{name}/recall").status_code == 404, name
            assert store.post(f"/v1/stores/{name}", json={"text": "x"}).status_code == 404, name
        assert not notes.is_store("../outside")

    def test_a_store_cannot_be_deleted(self, store):
        """取り込みで焼き直せないので、消したら中身がどこにも無くなる。"""
        from app import registry

        assert registry.blocked_from_deleting("myapp")

    def test_a_collection_cannot_take_the_name(self, store, tmp_path, monkeypatch):
        from fastapi import HTTPException

        from app import collect

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        with pytest.raises(HTTPException) as e:
            collect.create("myapp", prompt="p", interval_minutes=60)
        assert e.value.status_code == 400


class TestManyWorkers:
    """本体は複数のプロセスで動く。置き場を作ったのとは別のプロセスからも、最新の中身が読めること。"""

    def test_a_store_made_elsewhere_is_read_fresh(self, client):
        from app import db, notes

        # 別のプロセスが作って書いた(こちらの登録は再走査の前のまま)
        notes.ensure_db("myapp")
        db.set_mutable_paths([])
        assert client.get("/v1/stores/myapp/recall").json()["total"] == 0
        notes.add(text="一つ目", store="myapp")
        db.set_mutable_paths([])

        # 開いた時点の中身を読み続けない
        assert client.get("/v1/stores/myapp/recall").json()["total"] == 1

    def test_a_new_store_makes_the_other_workers_rescan(self, client):
        from app import notes
        from app.main import app, refresh_sources

        refresh_sources(app)
        notes.ensure_db("myapp")  # 別のプロセスが作った
        assert refresh_sources(app) is True
        assert "myapp" in _sources(client)
        # 書き込みだけでは走査し直さない(メモ 1 件ごとに全ソースを数え直さない)
        notes.add(text="一つ目", store="myapp")
        assert refresh_sources(app) is False


class TestAdmin:
    def test_named_stores_are_listed_with_the_short_term_memory(self, client):
        """アプリが作った置き場も記憶の面の表に並べる(並べないと、何をどこに預けているか画面から見えない)。"""
        client.post("/v1/stores", json={"name": "myapp"})
        client.post("/v1/stores/myapp", json={"text": "預けたメモ"})
        page = client.get("/admin/memory").text
        assert ">myapp</a>" in page
        assert "アプリが作って預けている置き場" in page

    def test_an_empty_store_can_be_deleted_from_the_page(self, client, notes_dir):
        """空の置き場には削除の口が出て、押すと一覧からもファイルからも消える。"""
        client.post("/v1/stores", json={"name": "oldapp"})
        page = client.get("/admin/memory").text
        assert 'action="/admin/stores/oldapp/delete"' in page

        res = client.post("/admin/stores/oldapp/delete", follow_redirects=False)
        assert res.status_code == 303
        assert not list(notes_dir.glob("oldapp.db*"))
        assert "oldapp" not in _sources(client)
        assert client.get("/v1/stores").json()["stores"] == []

    def test_a_store_with_notes_cannot_be_deleted(self, client, notes_dir):
        """中身のある置き場は消せない(焼き直せないので、消したら中身がどこにも無くなる)。"""
        client.post("/v1/stores", json={"name": "myapp"})
        client.post("/v1/stores/myapp", json={"text": "預けたメモ"})
        assert 'action="/admin/stores/myapp/delete"' not in client.get("/admin/memory").text

        assert client.post("/admin/stores/myapp/delete").status_code == 409
        assert (notes_dir / "myapp.db").exists()

    def test_the_short_term_memory_itself_cannot_be_deleted(self, client):
        assert client.post("/admin/stores/chiezo_memory/delete").status_code == 404
        assert client.post("/admin/stores/nothere/delete").status_code == 404
