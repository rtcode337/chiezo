"""notes(唯一書き込めるソース)のテスト。"""
import sqlite3

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


@pytest.fixture()
def disabled_client(built_data_dir, monkeypatch):
    monkeypatch.delenv("CHIEZO_NOTES_DIR", raising=False)
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


class TestDisabled:
    def test_remember_returns_503(self, disabled_client):
        res = disabled_client.post("/v1/notes", json={"text": "覚えて"})
        assert res.status_code == 503
        assert res.json()["error"] == "notes are disabled"
        assert "CHIEZO_NOTES_DIR" in res.json()["hint"]

    def test_recall_returns_503(self, disabled_client):
        assert disabled_client.get("/v1/notes/recall").status_code == 503

    def test_update_returns_503(self, disabled_client):
        assert disabled_client.patch("/v1/notes/1", json={"text": "直す"}).status_code == 503

    def test_notes_is_not_registered_as_a_source(self, disabled_client):
        names = [s["name"] for s in disabled_client.get("/v1/sources").json()["sources"]]
        assert "notes" not in names


class TestRemember:
    def test_creates_the_db_on_startup(self, client, notes_dir):
        """ingest を回さずに使い始められること。"""
        assert (notes_dir / "notes.db").exists()

    def test_registers_itself_as_a_source(self, client):
        listed = {s["name"]: s for s in client.get("/v1/sources").json()["sources"]}
        assert listed["notes"]["kind"] == "notes"
        assert listed["notes"]["schema_version"] == 4

    def test_remembers_and_recalls(self, client):
        res = client.post(
            "/v1/notes",
            json={"text": "devcontainer をやめて WSL2 へ移行する", "tags": "環境,決定"},
        )
        assert res.status_code == 200
        created = res.json()
        assert created["title"] == "devcontainer をやめて WSL2 へ移行する"
        assert created["tags"] == ["環境", "決定"]
        assert created["url"] == f"/search/notes/doc/{created['doc_id']}"

        got = client.get("/v1/notes/recall").json()
        assert got["total"] == 1
        assert got["notes"][0]["text"] == "devcontainer をやめて WSL2 へ移行する"

    def test_title_is_taken_from_the_first_line(self, client):
        created = client.post(
            "/v1/notes", json={"text": "一行目が見出し\n\n二行目以降は本文"}
        ).json()
        assert created["title"] == "一行目が見出し"

    def test_duplicate_titles_are_disambiguated(self, client):
        """docs.title は UNIQUE。同じ書き出しのメモを何度も取れないと困る。"""
        first = client.post("/v1/notes", json={"text": "TODO"}).json()
        second = client.post("/v1/notes", json={"text": "TODO"}).json()
        assert first["title"] == "TODO"
        assert second["title"] == f"TODO ({second['doc_id']})"

    def test_empty_text_is_rejected(self, client):
        assert client.post("/v1/notes", json={"text": "   "}).status_code == 400

    def test_doc_count_follows_writes(self, client):
        """走査は /data の変化でしか走らないので、書いた側で件数を直している。"""
        for i in range(3):
            client.post("/v1/notes", json={"text": f"メモ {i}"})
        listed = {s["name"]: s for s in client.get("/v1/sources").json()["sources"]}
        assert listed["notes"]["docs"] == 3


class TestRecall:
    @pytest.fixture()
    def filled(self, client):
        client.post("/v1/notes", json={"text": "浅草寺に行った話", "tags": "旅行"})
        client.post("/v1/notes", json={"text": "Chiezo のスキーマを 4 に上げた", "tags": "開発"})
        client.post("/v1/notes", json={"text": "WSL2 へ移行すると決めた", "tags": "開発,環境"})
        return client

    def test_newest_first(self, filled):
        titles = [n["title"] for n in filled.get("/v1/notes/recall").json()["notes"]]
        assert titles[0] == "WSL2 へ移行すると決めた"

    def test_full_text_search(self, filled):
        got = filled.get("/v1/notes/recall", params={"q": "スキーマ"}).json()
        assert [n["title"] for n in got["notes"]] == ["Chiezo のスキーマを 4 に上げた"]

    def test_short_query_falls_back_to_substring(self, filled):
        """trigram は 3 文字未満を引けないので、件数の小さい notes では走査に落とす。"""
        got = filled.get("/v1/notes/recall", params={"q": "旅"}).json()
        assert got["total"] == 0  # 本文に「旅」は無い(タグにはある)
        got = filled.get("/v1/notes/recall", params={"q": "went"}).json()
        assert got["total"] == 0

    def test_tag_filter(self, filled):
        got = filled.get("/v1/notes/recall", params={"tag": "開発"}).json()
        assert got["total"] == 2

    def test_multiple_tags_are_and(self, filled):
        got = filled.get("/v1/notes/recall", params={"tag": "開発,環境"}).json()
        assert [n["title"] for n in got["notes"]] == ["WSL2 へ移行すると決めた"]

    def test_time_range(self, filled):
        got = filled.get("/v1/notes/recall", params={"since": "2999-01-01"}).json()
        assert got["total"] == 0
        got = filled.get("/v1/notes/recall", params={"since": "2000-01-01"}).json()
        assert got["total"] == 3

    def test_paging(self, filled):
        page = filled.get("/v1/notes/recall", params={"limit": 2}).json()
        assert page["total"] == 3 and len(page["notes"]) == 2
        rest = filled.get("/v1/notes/recall", params={"limit": 2, "offset": 2}).json()
        assert len(rest["notes"]) == 1

    def test_limit_is_clamped_for_direct_callers(self, filled, monkeypatch):
        """MCP は app の関数を直接呼ぶので、FastAPI の Query 検証を通らない。"""
        from app import notes

        monkeypatch.setattr(notes, "RECALL_LIMIT_MAX", 2)
        assert len(notes.recall(limit=10_000)["notes"]) == 2

    def test_body_is_truncated_by_default(self, client):
        """既定で全文を返すと、当たった件数ぶんの本文がまるごとコンテキストに載る。"""
        from app import notes

        long_text = "あ" * (notes.RECALL_MAX_CHARS_DEFAULT + 50)
        client.post("/v1/notes", json={"text": long_text})
        note = client.get("/v1/notes/recall").json()["notes"][0]
        assert len(note["text"]) == notes.RECALL_MAX_CHARS_DEFAULT
        assert note["truncated"] is True

    def test_truncated_notes_can_be_fetched_in_full(self, client):
        """切った本文の取り直し先。doc_id が残っていないと全文へ辿れない。"""
        from app import notes

        long_text = "い" * (notes.RECALL_MAX_CHARS_DEFAULT + 50)
        client.post("/v1/notes", json={"text": long_text})
        note = client.get("/v1/notes/recall").json()["notes"][0]
        full = client.get(f"/v1/notes/doc/{note['doc_id']}").json()
        assert full["body"] == long_text

    def test_short_notes_are_not_marked_truncated(self, filled):
        assert all(
            "truncated" not in n for n in filled.get("/v1/notes/recall").json()["notes"]
        )

    def test_max_chars_zero_returns_everything(self, client):
        from app import notes

        long_text = "う" * (notes.RECALL_MAX_CHARS_DEFAULT + 50)
        client.post("/v1/notes", json={"text": long_text})
        note = client.get("/v1/notes/recall", params={"max_chars": 0}).json()["notes"][0]
        assert note["text"] == long_text and "truncated" not in note

    def test_fields_selects_and_orders_the_response(self, filled):
        got = filled.get(
            "/v1/notes/recall", params={"fields": "title,updated_at"}
        ).json()
        assert list(got["notes"][0]) == ["title", "updated_at"]

    def test_unknown_field_is_rejected(self, filled):
        res = filled.get("/v1/notes/recall", params={"fields": "title,body"})
        assert res.status_code == 400
        body = res.json()
        assert "body" in body["error"] and "text" in body["allowed_fields"]

    def test_max_chars_is_clamped_for_direct_callers(self, filled):
        """MCP は app の関数を直接呼ぶ。負の添字は末尾を削る意味になってしまう。"""
        from app import notes

        note = notes.recall(max_chars=-3)["notes"][0]
        assert note["text"] == "WSL2 へ移行すると決めた"

    def test_negative_limit_does_not_return_everything(self, filled):
        """SQLite の LIMIT -1 は「無制限」なので、素通しすると全件返る。"""
        from app import notes

        got = notes.recall(limit=-1, offset=-5)
        assert got["total"] == 3 and len(got["notes"]) == 1
        assert got["offset"] == 0


class TestUpdate:
    @pytest.fixture()
    def created(self, client):
        return client.post(
            "/v1/notes", json={"text": "浅草寺に行った話", "tags": "旅行,寺"}
        ).json()

    def test_replaces_only_what_was_passed(self, client, created):
        res = client.patch(f"/v1/notes/{created['doc_id']}", json={"text": "泉岳寺に行った話"})
        assert res.status_code == 200
        got = client.get("/v1/notes/doc", params={"title": created["title"]}).json()
        assert got["body"] == "泉岳寺に行った話"
        # 渡していない項目は今のまま(タイトルもタグも変わらない)
        assert got["tags"] == ["旅行", "寺"]

    def test_fts_follows_the_new_body(self, client):
        # タイトルは本文と別に持つ(1 行目由来のタイトルは text を変えても残る)ので、
        # 本文だけに入る語で確かめる
        created = client.post(
            "/v1/notes", json={"text": "参拝の記録\n\n浅草寺に行った", "tags": "旅行"}
        ).json()
        client.patch(f"/v1/notes/{created['doc_id']}", json={"text": "参拝の記録\n\n泉岳寺に行った"})
        # external content の FTS を手で入れ替えないと、古い本文で当たり続ける
        assert client.get("/v1/notes/recall", params={"q": "泉岳寺"}).json()["total"] == 1
        assert client.get("/v1/notes/recall", params={"q": "浅草寺"}).json()["total"] == 0

    def test_tags_are_replaced_wholesale_and_counts_follow(self, client, created):
        client.patch(f"/v1/notes/{created['doc_id']}", json={"tags": "旅行,御朱印"})
        assert client.get("/v1/notes/tags").json()["tags"] == [
            {"tag": "御朱印", "docs": 1},
            {"tag": "旅行", "docs": 1},
        ]

    def test_empty_tags_clears_them(self, client, created):
        client.patch(f"/v1/notes/{created['doc_id']}", json={"tags": ""})
        assert client.get("/v1/notes/tags").json()["tags"] == []

    def test_update_bumps_updated_at_to_the_front_of_recall(self, client, created, monkeypatch):
        client.post("/v1/notes", json={"text": "あとから書いた別のメモ"})
        # updated_at は秒精度なので、同じ秒に書くと doc_id の若い側が後ろに沈む。
        # 「書き換えで浮く」を確かめたいテストなので、時刻を進めて書き換える
        from app import notes

        monkeypatch.setattr(notes, "_now", lambda: "2999-01-01T00:00:00+00:00")
        client.patch(f"/v1/notes/{created['doc_id']}", json={"text": "書き換えた本文"})
        got = client.get("/v1/notes/recall").json()
        assert got["notes"][0]["text"] == "書き換えた本文"

    def test_title_collision_is_disambiguated(self, client, created):
        other = client.post("/v1/notes", json={"text": "別のメモ"}).json()
        res = client.patch(f"/v1/notes/{other['doc_id']}", json={"title": created["title"]}).json()
        assert res["title"] == f"{created['title']} ({other['doc_id']})"

    def test_unknown_id_is_404(self, client):
        assert client.patch("/v1/notes/999", json={"text": "無い"}).status_code == 404

    def test_nothing_to_update_is_400(self, client, created):
        assert client.patch(f"/v1/notes/{created['doc_id']}", json={}).status_code == 400

    def test_empty_text_is_rejected(self, client, created):
        assert client.patch(
            f"/v1/notes/{created['doc_id']}", json={"text": "   "}
        ).status_code == 400


class TestExtra:
    """タグで表せない構造(並び順など)の置き場。タスク・ルールの画面が使う。"""

    @pytest.fixture()
    def created(self, client):
        return client.post(
            "/v1/notes",
            json={"text": "浅草寺に行く", "tags": "todo", "extra": {"sort_order": 30}},
        ).json()

    def test_stored_and_returned_when_named(self, client, created):
        assert created["extra"] == {"sort_order": 30}
        got = client.get(
            "/v1/notes/recall", params={"fields": "doc_id,extra"}
        ).json()["notes"][0]
        assert got == {"doc_id": created["doc_id"], "extra": {"sort_order": 30}}

    def test_absent_from_the_default_recall(self, client, created):
        """既定に入れると、持たないメモにも "extra": null が並んでコンテキストを食う。"""
        note = client.get("/v1/notes/recall").json()["notes"][0]
        assert "extra" not in note

    def test_notes_without_extra_report_none_when_named(self, client):
        client.post("/v1/notes", json={"text": "ただのメモ"})
        note = client.get(
            "/v1/notes/recall", params={"fields": "extra"}
        ).json()["notes"][0]
        assert note == {"extra": None}

    def test_extra_is_listed_as_an_allowed_field(self, client, created):
        res = client.get("/v1/notes/recall", params={"fields": "nope"})
        assert res.status_code == 400
        assert "extra" in res.json()["allowed_fields"]

    def test_replaced_wholesale(self, client, created):
        res = client.patch(
            f"/v1/notes/{created['doc_id']}", json={"extra": {"sort_order": 10}}
        )
        assert res.json()["extra"] == {"sort_order": 10}

    def test_empty_dict_clears_it(self, client, created):
        res = client.patch(f"/v1/notes/{created['doc_id']}", json={"extra": {}})
        assert "extra" not in res.json()
        note = client.get(
            "/v1/notes/recall", params={"fields": "extra"}
        ).json()["notes"][0]
        assert note == {"extra": None}

    def test_survives_an_update_that_does_not_mention_it(self, client, created):
        client.patch(f"/v1/notes/{created['doc_id']}", json={"text": "泉岳寺に行く"})
        note = client.get(
            "/v1/notes/recall", params={"fields": "extra"}
        ).json()["notes"][0]
        assert note == {"extra": {"sort_order": 30}}

    def test_extra_alone_is_enough_to_update(self, client, created):
        """並び替えは本文もタグも変えないので、extra だけの更新が通らないと使えない。"""
        res = client.patch(
            f"/v1/notes/{created['doc_id']}", json={"extra": {"sort_order": 1}}
        )
        assert res.status_code == 200

    def test_mcp_remember_still_works_without_extra(self, client):
        """MCP は FastAPI を通さずハンドラを直接呼ぶので、Body(...) の既定値が
        そのまま引数に入る。省略した extra を素通しすると FieldInfo が保存側へ流れる。
        """
        from tests.test_mcp import call_tool

        stored = call_tool(client, "remember", {"text": "extra を渡さない経路"})
        assert stored["isError"] is False


class TestTagGuide:
    """定番タグの語彙はサーバー側の 1 か所(CANONICAL_TAGS)から配る。"""

    def test_every_canonical_tag_is_in_the_guide(self):
        from app import notes

        guide = notes.tag_guide()
        for tag, hint in notes.CANONICAL_TAGS.items():
            assert tag in guide and hint in guide

    def test_remember_tool_description_carries_the_guide(self):
        """語彙を書き換えたら MCP のツール定義にそのまま載ること(写しを作らない)。"""
        import inspect

        from app import mcp_server

        assert "tag_guide" in inspect.getsource(mcp_server._register_memory_tools)


class TestForget:
    def test_deletes_from_docs_and_fts(self, client):
        created = client.post("/v1/notes", json={"text": "消す予定のメモ", "tags": "一時"}).json()
        assert client.delete(f"/v1/notes/{created['doc_id']}").status_code == 200
        assert client.get("/v1/notes/recall").json()["total"] == 0
        # FTS からも消えていること(external content は手で消さないと残る)
        assert client.get("/v1/notes/recall", params={"q": "予定"}).json()["total"] == 0
        # タグの集計も戻っていること
        assert client.get("/v1/notes/tags").json()["tags"] == []

    def test_unknown_id_is_404(self, client):
        assert client.delete("/v1/notes/999").status_code == 404


class TestWorksWithTheGenericEndpoints:
    """コアスキーマなので、ソース種別を意識しない既存の口がそのまま効く。"""

    @pytest.fixture()
    def filled(self, client):
        client.post("/v1/notes", json={"text": "浅草寺の最寄り駅を調べた", "tags": "調査"})
        return client

    def test_search(self, filled):
        got = filled.get("/v1/notes/search", params={"q": "最寄り駅"}).json()
        assert [r["title"] for r in got["results"]] == ["浅草寺の最寄り駅を調べた"]

    def test_doc(self, filled):
        got = filled.get("/v1/notes/doc", params={"title": "浅草寺の最寄り駅を調べた"}).json()
        assert got["tags"] == ["調査"]

    def test_filter_by_tag(self, filled):
        got = filled.get("/v1/notes/filter", params={"tag": "調査"}).json()
        assert got["total"] == 1

    def test_tags_listing(self, filled):
        assert filled.get("/v1/notes/tags").json()["tags"] == [{"tag": "調査", "docs": 1}]

    def test_browse_page(self, filled):
        assert filled.get("/search/notes/").status_code == 200
        assert "調べた" in filled.get("/search/notes/", params={"q": "調べた"}).text


class TestReaderIsNotImmutable:
    """追記される DB を immutable で開くと壊れたページを掴む。開き方が分かれていること。"""

    def test_notes_source_is_marked_mutable(self, client):
        from app import db

        src = client.app.state.sources["notes"]
        assert src.mutable is True
        assert db.is_mutable(src.path) is True

    def test_data_sources_stay_immutable(self, client):
        from app import db

        src = client.app.state.sources["jawiki"]
        assert src.mutable is False
        assert db.is_mutable(src.path) is False

    def test_writes_are_visible_to_readers_without_restart(self, client):
        """書いた直後に、読み取り側の接続からそのまま見えること。"""
        client.get("/v1/notes/recall")  # 先に読み取り接続を張らせる
        client.post("/v1/notes", json={"text": "あとから書いたメモ"})
        got = client.get("/v1/notes/recall").json()
        assert [n["title"] for n in got["notes"]] == ["あとから書いたメモ"]


class TestSchemaStaysInSyncWithIngest:
    """app は ingest を import しないので DDL の写しを持っている。ずれたら落とす。

    ずれると「notes だけ filter が 409」「tags が空」のように静かに壊れるため、
    ここで ingest の core.py から作った DB と実際に突き合わせる。
    """

    def _schema(self, conn) -> set[str]:
        return {
            f"{row['type']} {row['name']}"
            for row in conn.execute(
                "SELECT type, name FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            )
        }

    def test_tables_and_indexes_match_core(self, notes_dir, tmp_path):
        import core  # ingest 側(conftest が sys.path に入れている)
        from app import notes

        notes.ensure_db()
        theirs = sqlite3.connect(tmp_path / "core.db")
        theirs.row_factory = sqlite3.Row
        theirs.executescript(core.CORE_SCHEMA_DDL)
        theirs.executescript(core.CORE_INDEX_DDL)

        ours = sqlite3.connect(notes.notes_path())
        ours.row_factory = sqlite3.Row
        try:
            missing = self._schema(theirs) - self._schema(ours)
            assert not missing, f"notes.py の DDL に足りないもの: {sorted(missing)}"
            # notes 固有の索引(時系列の想起用)だけが増えている分には構わない
            extra = self._schema(ours) - self._schema(theirs)
            assert extra == {"index idx_docs_updated"}, f"想定外の追加: {sorted(extra)}"
        finally:
            theirs.close()
            ours.close()

    def test_schema_version_matches_core(self):
        import core
        from app import notes

        assert notes.SCHEMA_VERSION == core.SCHEMA_VERSION

    def test_docs_columns_match_core(self, notes_dir, tmp_path):
        import core
        from app import notes

        notes.ensure_db()
        theirs = sqlite3.connect(tmp_path / "core.db")
        theirs.executescript(core.CORE_SCHEMA_DDL)
        ours = sqlite3.connect(notes.notes_path())
        try:
            cols = lambda c: [(r[1], r[2]) for r in c.execute("PRAGMA table_info(docs)")]  # noqa: E731
            assert cols(ours) == cols(theirs)
        finally:
            theirs.close()
            ours.close()


class TestMcpTools:
    """ツール定義は常時コンテキストに載るので、使えないときは出さない。"""

    def _tool_names(self, client) -> set[str]:
        from tests.test_mcp import rpc

        return {t["name"] for t in rpc(client, "tools/list")["result"]["tools"]}

    def test_memory_tools_are_offered_when_enabled(self, client):
        assert {"remember", "recall"} <= self._tool_names(client)

    def test_memory_tools_are_absent_when_disabled(self, disabled_client):
        names = self._tool_names(disabled_client)
        assert not ({"remember", "recall"} & names)
        assert "search" in names  # 他の道具は出たまま

    def test_remember_and_recall_round_trip_over_mcp(self, client):
        from tests.test_mcp import call_tool

        stored = call_tool(client, "remember", {"text": "MCP 経由で覚えた話", "tags": "検証"})
        assert stored["isError"] is False
        got = call_tool(client, "recall", {"tag": "検証"})["payload"]
        assert got["total"] == 1
        assert got["notes"][0]["text"] == "MCP 経由で覚えた話"


class TestClaudeConfig:
    """CLAUDE.md ブロックにメモの中身を引き写さないこと(app/claude_config.py)。

    例示のタイトル・タグは DB の実データから採るが、notes はユーザーが手元で書いたメモで
    機密が混じりうる。ブロックはリポジトリ側(`--project`)にも生成できるので、
    見出しやタグが載るとコミットされて意図せず共有される。
    """

    SECRET_TITLE = "社外に出せない相談ごと"
    SECRET_TAG = "機密"

    @pytest.fixture()
    def block(self, client):
        client.post(
            "/v1/notes",
            json={"text": f"{self.SECRET_TITLE}\n\n本文も出さない", "tags": self.SECRET_TAG},
        )
        return client.get("/admin/claude-config.txt").text

    def _section(self, block: str) -> str:
        """notes の行だけを切り出す(次のソースの見出しまで)。"""
        lines = block.splitlines()
        start = next(i for i, line in enumerate(lines) if line.startswith("- **notes**"))
        rest = lines[start + 1:]
        end = next((i for i, line in enumerate(rest) if line.startswith("- **")), len(rest))
        return "\n".join(lines[start:start + 1 + end])

    def test_notes_is_listed(self, block):
        assert "- **notes**(kind=notes)" in block

    def test_the_memo_is_not_quoted(self, block):
        assert self.SECRET_TITLE not in block
        assert self.SECRET_TAG not in block

    def test_examples_use_placeholders(self, block):
        section = self._section(block)
        assert '--data-urlencode "q=<検索語>"' in section
        assert '--data-urlencode "title=<タイトル>"' in section
        assert '--data-urlencode "tag=<タグ名>"' in section

    def test_public_sources_still_quote_real_titles(self, block):
        """公開ダンプ由来のソースは実在タイトルで例示したまま(引用を止めるのは notes だけ)。

        プレースホルダーは「その API をどう呼ぶか」しか伝えない。実在タイトルは
        `多摩川 (relation:32007)` のようなそのソース特有の表記まで見せられるので、
        載せてよいソースでは落とさない。
        """
        others = block.replace(self._section(block), "")
        assert "- **jawiki**" in others
        assert "<タイトル>" not in others


class TestShortTermIsNotARebuildableSource:
    """短期記憶は長期記憶と同じ扱いにしない(管理画面・件数)。

    知識を 2 層に分けた結果として、notes は「取り込みで焼くソース」の枠に
    入らなくなった。混ぜていた頃の壊れ方を両側から固定する。
    """

    def test_admin_does_not_offer_rebuild(self, client):
        """再構築ボタンを出さないこと。

        出していた頃は、押しても trigger が unknown source を返すだけなのに、
        確認ダイアログだけが「ダンプの取得からやり直します」と言っていた
        —— 唯一書き込めるソースで、消えたと読める文言が出ていた。
        """
        html = client.get("/admin").text
        assert 'action="/admin/rebuild/notes"' not in html
        # 長期側にはボタンが出たままであること(消しすぎていない)
        assert 'action="/admin/rebuild/jawiki"' in html

    def test_rebuild_is_refused(self, client, monkeypatch):
        """URL を直に叩かれても trigger まで行かせない。"""
        monkeypatch.setattr("app.views.admin.TRIGGER_URL", "http://example.invalid")
        res = client.post("/admin/rebuild/notes")
        assert res.status_code == 409
        assert res.json()["error"] == "source is not rebuildable: notes"

    def test_admin_shows_the_short_term_section(self, client):
        from app import notes

        notes.add(text="短期記憶の節に出る", tags="決定")
        html = client.get("/admin").text
        assert "短期記憶" in html
        assert "覚えていること: 1 件" in html
        assert "決定 1" in html

    def test_source_count_follows_writes_that_skip_the_rest_api(self, client):
        """REST の口を通らない書き込みでも件数が追いつくこと。

        やること層(chiezo-tasks)は別プロセスから app/notes.py を直接呼ぶので、
        書いた側で数え直す形では追いつかなかった(件数が 12 のまま実体 42 になった)。
        """
        from app import notes

        def listed() -> int:
            sources = {s["name"]: s for s in client.get("/v1/sources").json()["sources"]}
            return sources["notes"]["docs"]

        before = listed()
        notes.add(text="別プロセスからの書き込み")
        assert listed() == before + 1
