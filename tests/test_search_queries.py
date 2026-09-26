"""検索文を AI に考えさせてから外の道具で引く回(`app/search_queries.py`)のテスト。

**同じ検索文で引き直しても、同じ記事が返るだけ**なので、回ごとに新しい検索文を
考えさせる。見ているのは 4 つ —— 1 回目は最初の組を使う(AI を呼ばない)、
2 回目からはこれまでの検索文と件数を見せて考えさせる、使った検索文は二度使わない、
新しいものが出なければ前の検索文のまま引かずに断る。
"""
from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app import collect, feeds, search_queries

TEMPLATE = "https://example.com/search?q={query}&sort=new"


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    return tmp_path


FEED = {
    "urls": [{"url": TEMPLATE, "tags": ["例"]}, "https://example.com/static.rss"],
    "queries": ["技術書", "エンジニア 本"],
    "per_run": 3,
    "reuse_days": 60,
}

THINK = {
    "name": "機械収集",
    "use_feed": True,
    "interval_minutes": 1440,
    "backend": "codex",
    "prompt": "これまで:\n{queries}\n新しい検索文を考えて",
}


@pytest.fixture
def made(enabled):
    collect.create("posts", prompt="{cursor}", interval_minutes=60)
    collect.update("posts", feed=FEED, sweeps=[THINK, {"name": "そのまま", "use_feed": True}])
    return collect.get("posts")


class TestFeedTemplates:
    def test_the_slot_is_expanded_per_query_with_a_tag(self):
        """展開した 1 本に `<印>:<検索文>` を付ける(次の回に件数を数える)。"""
        spec = feeds.normalize(FEED)
        expanded = feeds.expand(spec, ["技術書 おすすめ", "SRE"])

        urls = [one["url"] for one in expanded["urls"]]
        assert urls[0] == "https://example.com/search?q=%E6%8A%80%E8%A1%93%E6%9B%B8%20%E3%81%8A%E3%81%99%E3%81%99%E3%82%81&sort=new"
        assert "https://example.com/static.rss" in urls
        assert expanded["urls"][0]["tags"] == ["例", "検索:技術書 おすすめ"]
        assert len(urls) == 3

    def test_the_seed_is_required(self):
        with pytest.raises(HTTPException):
            feeds.normalize({"urls": [TEMPLATE]})

    def test_the_count_per_run_fits_the_url_ceiling(self):
        """展開したあとの本数も `MAX_URLS` に収める。"""
        spec = feeds.normalize({"urls": [TEMPLATE, TEMPLATE + "&p=2"], "queries": ["a"], "per_run": 99})
        assert spec["per_run"] == feeds.MAX_URLS // 2

    def test_a_feed_without_the_slot_is_unchanged(self):
        spec = feeds.normalize({"urls": ["https://example.com/a.rss"]})
        assert not feeds.has_templates(spec)
        assert "queries" not in spec


class TestLedger:
    def test_record_current_and_forget(self, enabled):
        search_queries.record("posts", ["a", "b"])
        assert search_queries.current("posts") == ["a", "b"]
        assert search_queries.forget("posts", "a") == 1
        assert [r["query"] for r in search_queries.history("posts")] == ["b"]
        assert search_queries.forget("posts") == 1
        assert search_queries.history("posts") == []

    def test_recently_used_queries_are_not_offered_again(self, enabled):
        search_queries.record("posts", ["技術書"])
        assert search_queries.fresh("posts", ["技術書", " SRE  本 ", "SRE 本", "x"], 2, 60) == ["SRE 本", "x"]

    def test_old_queries_can_be_used_again(self, enabled):
        """二度と使えない形にはしない —— しばらく経てば同じ検索文でも新しい記事が当たる。"""
        search_queries.record("posts", ["技術書"])
        rows = search_queries.history("posts")
        rows[0]["used_at"] = "2026-01-01T00:00:00+00:00"
        search_queries._save("posts", rows)

        assert search_queries.fresh("posts", ["技術書"], 5, 60) == ["技術書"]
        assert "60 日が過ぎた検索文は、もう一度使えます" in search_queries.render(rows, 60)

    def test_a_reused_query_is_counted_only_for_its_own_run(self, enabled, tmp_path):
        """タグは回をまたいで同じ。前の回に入ったぶんを今回の手柄にしない。"""
        path = tmp_path / "posts.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE doc_tags (doc_id INTEGER, tag TEXT)")
            conn.executemany("INSERT INTO doc_tags VALUES (?, ?)", [(i, "検索:a") for i in range(5)])
        search_queries.record("posts", ["a"])
        rows = search_queries.history("posts")
        rows[0].update(found=3, kept=3, used_at="2026-01-01T00:00:00+00:00")
        search_queries._save("posts", rows)
        search_queries.record("posts", ["a"])

        search_queries.count("posts", {"posts": SimpleNamespace(path=path)}, "検索")
        assert [(r["found"], r["kept"]) for r in search_queries.history("posts")] == [(2, 2), (3, 3)]

    def test_the_answer_is_read_even_when_wrapped(self):
        assert search_queries.parse('案です:\n```json\n["a b", " c "]\n```') == ["a b", "c"]
        assert search_queries.parse("読めない") == []

    def test_counts_found_and_kept(self, enabled, tmp_path):
        """入った数だけでは、外されるものを大量に連れてくる検索文が当たりに見える。"""
        path = tmp_path / "posts.db"
        with sqlite3.connect(path) as conn:
            conn.execute("CREATE TABLE doc_tags (doc_id INTEGER, tag TEXT)")
            conn.executemany(
                "INSERT INTO doc_tags VALUES (?, ?)",
                [(1, "検索:a"), (2, "検索:a"), (2, "_chiezo_removed"), (3, "検索:b")],
            )
        search_queries.record("posts", ["a", "b", "c"])
        search_queries.count("posts", {"posts": SimpleNamespace(path=path)}, "検索")

        got = {r["query"]: (r["found"], r["kept"]) for r in search_queries.history("posts")}
        assert got == {"a": (2, 1), "b": (1, 1), "c": (0, 0)}
        assert "2 件入り、消されずに残ったのは 1 件" in search_queries.render(search_queries.history("posts"))


class TestThinking:
    def test_only_a_feed_sweep_with_the_slot_thinks(self, made):
        assert search_queries.thinks(collect.sweep_named(made, "機械収集"))
        assert not search_queries.thinks(collect.sweep_named(made, "そのまま"))

    def test_first_run_uses_the_seed_without_asking(self, made, monkeypatch):
        from app import main

        asked = []

        async def ask(item, messages):
            asked.append(messages)
            return "[]", "codex", "m"

        monkeypatch.setattr(main, "_ask_for_collection", ask)
        spec = feeds.normalize(made.feed)
        got = asyncio.run(main._search_queries(made, collect.sweep_named(made, "機械収集"), spec, {}, []))

        assert got == ["技術書", "エンジニア 本"]
        assert asked == []
        assert search_queries.current("posts") == got

    def test_later_runs_ask_with_the_history_and_drop_used(self, made, monkeypatch):
        from app import main

        search_queries.record("posts", ["技術書"])
        asked = []

        async def ask(item, messages):
            asked.append((item.backend, messages))
            return '["技術書", "SRE 本", "設計 本"]', "codex", "m"

        monkeypatch.setattr(main, "_ask_for_collection", ask)
        spec = feeds.normalize(made.feed)
        got = asyncio.run(main._search_queries(made, collect.sweep_named(made, "機械収集"), spec, {}, []))

        assert got == ["SRE 本", "設計 本"]
        backend, messages = asked[0]
        # 相手はその巡回のもの。これまでの検索文が差し込み口に入る
        assert backend == "codex"
        assert "- 技術書(0 件入り" in messages[1]["content"]
        assert search_queries.history("posts")[0]["by"] == "codex"

    def test_nothing_new_is_refused(self, made, monkeypatch):
        """前の検索文のまま引いても同じ記事が返るだけ。"""
        from app import main

        search_queries.record("posts", ["技術書"])

        async def ask(item, messages):
            return '["技術書"]', "codex", "m"

        monkeypatch.setattr(main, "_ask_for_collection", ask)
        spec = feeds.normalize(made.feed)
        with pytest.raises(HTTPException) as caught:
            asyncio.run(main._search_queries(made, collect.sweep_named(made, "機械収集"), spec, {}, []))
        assert caught.value.status_code == 409

    def test_other_runs_reuse_the_latest_set(self, made):
        """考えるのは検索文の巡回だけ。ほかの回は枠を使わず、いちばん新しい組で引く。"""
        from app import main

        search_queries.record("posts", ["SRE 本"])
        spec = feeds.normalize(made.feed)
        got = asyncio.run(main._search_queries(made, collect.sweep_named(made, "そのまま"), spec, {}, []))
        assert got == ["SRE 本"]


class TestDefinition:
    def test_a_thinking_sweep_needs_the_slot(self, enabled):
        """走るまで分からないのでは遅い(考えた検索文がどこにも使われない)。"""
        collect.create("posts", prompt="{cursor}", interval_minutes=60)
        with pytest.raises(HTTPException) as caught:
            collect.update("posts", feed={"urls": ["https://example.com/a.rss"]}, sweeps=[THINK])
        assert caught.value.status_code == 400

    def test_removing_the_collection_forgets_the_queries(self, made):
        """作り直した収集が、前の収集の検索文を「使ったことがある」として弾かない。"""
        search_queries.record("posts", ["技術書"])
        collect.remove("posts")
        assert search_queries.history("posts") == []


class TestAdmin:
    @pytest.fixture()
    def client(self, enabled, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_the_ledger_is_shown_and_can_be_forgotten(self, client, made):
        search_queries.record("posts", ["技術書", "SRE 本"], "codex")

        html = client.get("/admin/collect/posts").text
        assert 'id="search-queries"' in html
        assert "SRE 本" in html

        res = client.post("/admin/collect/posts/queries/forget", data={"query": "SRE 本"}, follow_redirects=False)
        assert res.status_code == 303
        assert [r["query"] for r in search_queries.history("posts")] == ["技術書"]

        client.post("/admin/collect/posts/queries/forget", data={"query": ""}, follow_redirects=False)
        assert search_queries.history("posts") == []
        assert "まだ 1 回も引いていません" in client.get("/admin/collect/posts").text
