"""記憶の固化(短期記憶 → 長期記憶)のテスト。

app が配る素材を、取り込み側(`ingest/sources/remote.py`)の仕掛けで実際に焼くところまで
通す。契約を挟んで両側が別のリポジトリ相当に分かれているので、片側だけのモックで
済ませると「配れてはいるが焼けない」が通ってしまう。
"""
import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def data_dir(tmp_path):
    """長期記憶の置き場(空から始める。固化ソースだけがここに増える)。"""
    d = tmp_path / "corpus"
    d.mkdir()
    return d


@pytest.fixture()
def client(tmp_path, data_dir, monkeypatch):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


def remember(client, text, tags, title=None):
    res = client.post("/v1/notes", json={"text": text, "tags": tags, "title": title})
    assert res.status_code == 200, res.text
    return res.json()


def add_theme(client, name="rules", tags="rule", label="決まりごと"):
    res = client.post("/v1/memory/themes", json={"name": name, "label": label, "tags": tags})
    assert res.status_code == 200, res.text
    return res.json()


def consolidate(client, data_dir, name="rules"):
    """app が配る素材を取り込み側で焼き、app に登録し直す(固化の 1 往復)。"""
    import main as ingest_main

    from app import main
    from sources.remote import RemotePluginAdapter, RemoteSource

    res = client.get("/v1/memory/fetch", params={"source": name})
    assert res.status_code == 200, res.text
    ndjson = data_dir / f"{name}.ndjson"
    ndjson.write_text(res.text, encoding="utf-8")

    adapter = RemotePluginAdapter(
        RemoteSource(base_url="http://chiezo-app:7010/v1/memory", name=name, kind="memory")
    )
    # 1 行目の meta から日付と検証条件を受け取るところも本番と同じ経路で通す
    dump_date = adapter._apply_meta(ndjson)
    building = data_dir / f"{name}-{dump_date}.db.building"
    ingest_main.build_db(adapter, ndjson, dump_date, building)
    ingest_main.validate_db(adapter, building)
    ingest_main.switch_db(data_dir, name, dump_date, building)

    # 本番は 5 秒ごとの再走査が拾う。テストでは待たずに反映させる
    main.refresh_sources(client.app)
    return dump_date


class TestPluginContract:
    def test_the_dump_date_carries_seconds(self, client, data_dir, tmp_path):
        """固化が渡す日付を取り込み側が受け取れること(8 桁固定だと落ちて当日に丸まる)。"""
        from sources.remote import RemotePluginAdapter, RemoteSource

        add_theme(client)
        remember(client, "何かの決まり", tags="rule")
        path = tmp_path / "rules.ndjson"
        path.write_text(client.get("/v1/memory/fetch", params={"source": "rules"}).text)

        adapter = RemotePluginAdapter(RemoteSource(base_url="x", name="rules", kind="memory"))
        assert len(adapter._apply_meta(path)) == 14

    def test_empty_catalog_is_accepted_by_the_ingest_side(self):
        """テーマが 1 つも無い状態は正常。

        取り込み側が空を弾いていた頃は、テーマを作るまでカタログ取得が丸ごと
        落ちていた(配るソースが実行時に決まるプラグインでは、空が起動直後の姿)。
        """
        from sources.remote import _parse_catalog

        assert _parse_catalog("http://plugin", {"sources": []}) == []

    def test_catalog_lists_themes(self, client):
        add_theme(client)
        payload = client.get("/v1/memory/sources").json()
        assert payload["sources"] == [
            {"name": "rules", "kind": "memory", "label": "決まりごと", "min_docs": 1}
        ]

    def test_fetch_is_readable_by_the_remote_adapter(self, client, data_dir):
        """配った NDJSON が取り込み側の Doc になること(契約の突き合わせ)。"""
        from sources.remote import RemotePluginAdapter, RemoteSource

        add_theme(client)
        remember(client, "コミット前に確認を取る", tags="rule", title="コミットの流儀")
        body = client.get("/v1/memory/fetch", params={"source": "rules"}).text
        path = data_dir / "rules.ndjson"
        path.write_text(body, encoding="utf-8")

        adapter = RemotePluginAdapter(RemoteSource(base_url="x", name="rules", kind="memory"))
        adapter._apply_meta(path)
        docs = list(adapter.iter_docs(path))
        assert [d.title for d in docs] == ["コミットの流儀"]
        assert docs[0].tags == ["rule"]

    def test_fetch_refuses_when_there_is_nothing_to_burn(self, client):
        """素材が空なら断る。0 件のソースを焼いても消えるだけなので、流し始める前に。"""
        add_theme(client)
        res = client.get("/v1/memory/fetch", params={"source": "rules"})
        assert res.status_code == 409
        assert "nothing to consolidate" in res.json()["error"]

    def test_unknown_theme_is_404(self, client):
        assert client.get("/v1/memory/fetch", params={"source": "nope"}).status_code == 404


class TestThemes:
    def test_name_must_be_usable_as_a_source_name(self, client):
        res = client.post("/v1/memory/themes", json={"name": "my-rules", "tags": "rule"})
        assert res.status_code == 400
        assert "invalid theme name" in res.json()["error"]

    def test_tags_are_required(self, client):
        res = client.post("/v1/memory/themes", json={"name": "rules", "tags": " "})
        assert res.status_code == 400

    def test_existing_source_name_is_refused(self, client, data_dir):
        """テーマ名はそのままソース名になる。既に使われている名前を通すと、
        焼いた瞬間に別のソースを置き換えることになる。"""
        add_theme(client)
        remember(client, "何かの決まり", tags="rule")
        consolidate(client, data_dir)
        res = client.post("/v1/memory/themes", json={"name": "jawiki", "tags": "rule"})
        # 登録済みソース(この時点では rules だけ)以外は素通しする
        assert res.status_code == 200
        client.delete("/v1/memory/themes/jawiki")

        client.delete("/v1/memory/themes/rules")
        res = client.post("/v1/memory/themes", json={"name": "rules", "tags": "rule"})
        assert res.status_code == 409

    def test_removing_a_theme_keeps_the_burned_source(self, client, data_dir):
        add_theme(client)
        remember(client, "何かの決まり", tags="rule")
        consolidate(client, data_dir)
        assert client.delete("/v1/memory/themes/rules").status_code == 200
        listed = [s["name"] for s in client.get("/v1/sources").json()["sources"]]
        assert "rules" in listed


class TestConsolidation:
    def test_burns_only_the_matching_notes(self, client, data_dir):
        add_theme(client)
        remember(client, "決まりごとの本文", tags="rule", title="決まり")
        remember(client, "これはタスク", tags="todo", title="やること")
        consolidate(client, data_dir)

        titles = [
            r["title"]
            for r in client.get("/v1/rules/search", params={"q": "決まり"}).json()["results"]
        ]
        assert titles == ["決まり"]

    def test_the_source_is_searchable_like_any_other(self, client, data_dir):
        """長期側に入れば、ほかのソースと同じ口で引けること。"""
        add_theme(client)
        remember(client, "コミット前に変更点を説明する", tags="rule", title="コミットの流儀")
        consolidate(client, data_dir)

        doc = client.get("/v1/rules/doc", params={"title": "コミットの流儀"}).json()
        assert "変更点を説明" in doc["body"]
        assert client.get("/v1/rules/tags").json()["tags"] == [{"tag": "rule", "docs": 1}]

    def test_an_update_wins_over_the_previous_generation(self, client, data_dir):
        """短期側で直すと長期側が置き換わり、doc_id は変わらないこと。

        直すのは固化済みのメモそのもの(短期側の見出しは UNIQUE なので、同じ見出しの
        メモを 2 つ持てない)。本文を直せば固化の印は自動で外れ、対象に戻る。
        """
        add_theme(client)
        note = remember(client, "古い内容", tags="rule", title="決まり")
        consolidate(client, data_dir)
        before = client.get(
            "/v1/rules/doc", params={"title": "決まり", "fields": "doc_id,body"}
        ).json()
        client.post("/v1/memory/themes/rules/sweep")

        assert client.patch(
            f"/v1/notes/{note['doc_id']}", json={"text": "新しい内容"}
        ).status_code == 200
        consolidate(client, data_dir)

        after = client.get(
            "/v1/rules/doc", params={"title": "決まり", "fields": "doc_id,body"}
        ).json()
        assert after["body"] == "新しい内容"
        assert after["doc_id"] == before["doc_id"]

    def test_a_tombstone_drops_the_document(self, client, data_dir):
        """墓標は対象を落とし、墓標そのものも焼かれないこと。"""
        add_theme(client)
        note = remember(client, "消される決まり", tags="rule", title="要らない決まり")
        remember(client, "残る決まり", tags="rule", title="残す決まり")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")

        # 固化済みのメモ自身を墓標にする(見出しは既に長期側と一致している)
        client.patch(f"/v1/notes/{note['doc_id']}", json={"tags": "rule,削除"})
        consolidate(client, data_dir)

        titles = {
            r["title"] for r in client.get("/v1/rules/filter", params={"tag": "rule"}).json()["results"]
        }
        assert titles == {"残す決まり"}

    def test_a_tombstone_works_after_the_note_is_gone(self, client, data_dir):
        """短期側から消した後でも、同じ見出しで書けば長期側から落とせること。

        固化の目的は短期側を空けることなので、消した後に「あれを消したい」と
        なる場面が本番になる(そのとき短期側に見出しは残っていない)。
        """
        add_theme(client)
        note = remember(client, "消される決まり", tags="rule", title="要らない決まり")
        remember(client, "残る決まり", tags="rule", title="残す決まり")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")
        client.delete(f"/v1/notes/{note['doc_id']}")

        remember(client, "もう要らない", tags="rule,削除", title="要らない決まり")
        consolidate(client, data_dir)

        assert client.get(
            "/v1/rules/doc", params={"title": "要らない決まり"}
        ).status_code == 404

    def test_a_second_burn_on_the_same_day_keeps_the_previous_generation(self, client, data_dir):
        """同じ日に焼き直しても前世代が残ること。

        世代ファイルは `<source>-<日付>.db` で、切り替えは 1 つ前を残す作り。
        日付までしか持たなかった頃は、2 回目が同じファイル名になって前世代を
        上書きしていた —— 固化は 1 日に何度も走るので、戻り先が消えていた。
        """
        add_theme(client)
        note = remember(client, "古い内容", tags="rule", title="決まり")
        first = consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")

        client.patch(f"/v1/notes/{note['doc_id']}", json={"text": "新しい内容"})
        second = consolidate(client, data_dir)

        assert first != second
        generations = sorted(p.name for p in data_dir.glob("rules-*.db"))
        assert generations == [f"rules-{first}.db", f"rules-{second}.db"]
        assert (data_dir / "rules.db").resolve().name == f"rules-{second}.db"

    def test_refuses_to_empty_the_source(self, client, data_dir):
        """墓標で全部落ちるときは断る。

        空の DB は取り込み側の検証も通らないし、通っても空箱が残るだけ。
        「素材がない」とは次にすることが違うので、文言も分けてある。
        """
        add_theme(client)
        note = remember(client, "唯一の決まり", tags="rule", title="決まり")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")
        client.patch(f"/v1/notes/{note['doc_id']}", json={"tags": "rule,削除"})

        res = client.get("/v1/memory/fetch", params={"source": "rules"})
        assert res.status_code == 409
        assert "would empty the source" in res.json()["error"]

    def test_the_previous_generation_survives_a_note_only_theme(self, client, data_dir):
        """短期側を固化済みにしても、長期側の中身は次の焼き直しで消えないこと。

        素材が「前世代 + 差分」でなければ、印を付けた次の固化で全部消える。
        """
        add_theme(client)
        remember(client, "残ってほしい", tags="rule", title="古株")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")

        remember(client, "あとから来た", tags="rule", title="新入り")
        consolidate(client, data_dir)

        titles = {
            r["title"] for r in client.get("/v1/rules/filter", params={"tag": "rule"}).json()["results"]
        }
        assert titles == {"古株", "新入り"}


class TestSweep:
    def test_refuses_before_anything_is_burned(self, client):
        """焼く前に印だけ付けない。"""
        add_theme(client)
        remember(client, "まだ焼いていない", tags="rule")
        res = client.post("/v1/memory/themes/rules/sweep")
        assert res.status_code == 409
        assert "not consolidated yet" in res.json()["error"]

    def test_marks_what_reached_the_long_term_side(self, client, data_dir):
        add_theme(client)
        note = remember(client, "焼かれる決まり", tags="rule", title="決まり")
        consolidate(client, data_dir)

        res = client.post("/v1/memory/themes/rules/sweep").json()
        assert res["marked"] == 1
        assert res["titles"] == ["決まり"]

        doc = client.get(f"/v1/notes/doc/{note['doc_id']}").json()
        assert "固化" in doc["tags"]

    def test_marks_a_tombstone_only_when_the_target_is_gone(self, client, data_dir):
        add_theme(client)
        note = remember(client, "消される決まり", tags="rule", title="要らない決まり")
        remember(client, "残る決まり", tags="rule", title="残す決まり")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")

        client.patch(f"/v1/notes/{note['doc_id']}", json={"tags": "rule,削除"})
        # まだ焼き直していないので、長期側には残っている = 反映されていない
        assert client.post("/v1/memory/themes/rules/sweep").json()["marked"] == 0

        consolidate(client, data_dir)
        assert client.post("/v1/memory/themes/rules/sweep").json()["marked"] == 1
        doc = client.get(f"/v1/notes/doc/{note['doc_id']}").json()
        assert "固化" in doc["tags"]

    def test_consolidated_notes_drop_out_of_recall(self, client, data_dir):
        """思い出す先が長期側に移ったら、短期側の想起からは外れること。"""
        add_theme(client)
        remember(client, "焼かれる決まり", tags="rule", title="決まり")
        remember(client, "まだ短期にいる", tags="決定", title="決めたこと")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")

        titles = [n["title"] for n in client.get("/v1/notes/recall").json()["notes"]]
        assert titles == ["決めたこと"]

        # 控えを見たいときは明示的に呼べる(消したわけではない)
        with_burned = client.get("/v1/notes/recall", params={"consolidated": "true"}).json()
        assert {n["title"] for n in with_burned["notes"]} == {"決まり", "決めたこと"}

    def test_search_still_sees_consolidated_notes(self, client, data_dir):
        """隠すのは時系列の想起だけ。検索やタグ絞り込みは今までどおり全部見せる。"""
        add_theme(client)
        remember(client, "焼かれる決まり", tags="rule", title="決まり")
        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")

        hits = client.get("/v1/notes/search", params={"q": "焼かれる決まり"}).json()["results"]
        assert [h["title"] for h in hits] == ["決まり"]


class TestPendingCount:
    def test_themes_report_what_is_waiting(self, client, data_dir):
        add_theme(client)
        remember(client, "まだ焼いていない", tags="rule")
        listed = client.get("/v1/memory/themes").json()["themes"]
        assert listed[0]["pending"] == 1
        assert listed[0]["consolidated"] is False

        consolidate(client, data_dir)
        client.post("/v1/memory/themes/rules/sweep")
        listed = client.get("/v1/memory/themes").json()["themes"]
        assert listed[0]["pending"] == 0
        assert listed[0]["consolidated"] is True
        assert listed[0]["docs"] == 1


class TestAdminScreen:
    def test_lists_themes_with_what_is_waiting(self, client):
        add_theme(client)
        remember(client, "まだ焼いていない決まり", tags="rule")
        html = client.get("/admin").text
        assert "短期記憶から移す(固化)" in html
        assert "決まりごと" in html

    def test_a_theme_can_be_added_from_the_form(self, client):
        res = client.post(
            "/admin/memory/themes",
            data={"name": "envs", "label": "環境の事実", "tags": "環境"},
            follow_redirects=False,
        )
        assert res.status_code == 303
        listed = client.get("/v1/memory/themes").json()["themes"]
        assert [(t["name"], t["tags"]) for t in listed] == [("envs", ["環境"])]

    def test_sweeping_from_the_form_marks_the_notes(self, client, data_dir):
        add_theme(client)
        note = remember(client, "焼かれる決まり", tags="rule", title="決まり")
        consolidate(client, data_dir)

        res = client.post("/admin/memory/rules/sweep", follow_redirects=False)
        assert res.status_code == 303
        assert "固化" in client.get(f"/v1/notes/doc/{note['doc_id']}").json()["tags"]

    def test_deleting_a_theme_from_the_form_keeps_the_source(self, client, data_dir):
        add_theme(client)
        remember(client, "何かの決まり", tags="rule")
        consolidate(client, data_dir)

        res = client.post("/admin/memory/rules/delete", follow_redirects=False)
        assert res.status_code == 303
        assert client.get("/v1/memory/themes").json()["themes"] == []
        assert "rules" in [s["name"] for s in client.get("/v1/sources").json()["sources"]]
