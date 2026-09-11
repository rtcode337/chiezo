"""外向きの道具(`app/feeds.py`)—— RSS / Atom を機械的に取ってきて、参考として渡す。

押さえているのは 2 つ。**取ってきたものをそのまま溜めないこと**(差し込むだけで、
何を溜めるかは AI が決める)と、**外へ出る以上の作法を守ること**
(本文を取りに行かない・レート制限・名乗りに個人情報を入れない)。
"""
from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi import HTTPException

from app import feeds

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>ためし新聞</title>
    <item>
      <title>新しいほう</title>
      <link>https://example.com/2</link>
      <description>要約のようなもの。</description>
      <pubDate>Wed, 10 Sep 2026 12:00:00 +0900</pubDate>
    </item>
    <item>
      <title>古いほう</title>
      <link>https://example.com/1</link>
      <description>むかしの話。</description>
      <pubDate>Mon, 01 Sep 2026 12:00:00 +0900</pubDate>
    </item>
  </channel>
</rss>
"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>ためしブログ</title>
  <entry>
    <title>記事のタイトル</title>
    <link href="https://example.org/a"/>
    <summary>まとめ。</summary>
    <updated>2026-09-09T03:00:00Z</updated>
  </entry>
</feed>
"""


@pytest.fixture
def serve(monkeypatch):
    """外へ出ずに応答を差し替える。**待たせない**(最小間隔はここでは見ない)。"""
    monkeypatch.setattr(feeds, "MIN_INTERVAL", 0.0)

    def make(bodies: dict[str, str | Exception]):
        calls: list[str] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(str(request.url))
            body = bodies[str(request.url)]
            if isinstance(body, Exception):
                raise body
            return httpx.Response(200, content=body.encode())

        def client():
            return httpx.AsyncClient(
                transport=httpx.MockTransport(handler),
                headers={"User-Agent": feeds.USER_AGENT},
            )

        monkeypatch.setattr(feeds, "_client", client)
        return calls

    return make


def run(spec, since=None):
    return asyncio.run(feeds.fetch(feeds.normalize(spec), since))


class TestTheSpec:
    def test_it_needs_at_least_one_url(self):
        """読めない指定を黙って無視すると、道具を付けたつもりの収集が道具なしで回る。"""
        with pytest.raises(HTTPException):
            feeds.normalize({"urls": []})

    def test_only_http_urls(self):
        """file:// や内部のスキームを踏みに行かせない。"""
        with pytest.raises(HTTPException):
            feeds.normalize({"urls": ["file:///etc/passwd"]})

    def test_too_many_urls_are_refused(self):
        """増やすほど 1 回の実行が遅くなる(順に取るため)。"""
        with pytest.raises(HTTPException):
            feeds.normalize({"urls": [f"https://example.com/{i}" for i in range(20)]})

    def test_nothing_means_no_tool(self):
        assert feeds.normalize(None) is None
        assert feeds.normalize({}) is None


class TestReading:
    def test_it_reads_rss(self, serve):
        serve({"https://example.com/feed": RSS})
        got = run({"urls": ["https://example.com/feed"]})
        assert [i["title"] for i in got["items"]] == ["新しいほう", "古いほう"]
        assert got["items"][0]["url"] == "https://example.com/2"
        assert got["items"][0]["summary"] == "要約のようなもの。"
        # どこから来たかは必ず出す
        assert got["items"][0]["from"] == "ためし新聞"

    def test_it_reads_atom(self, serve):
        serve({"https://example.org/atom": ATOM})
        got = run({"urls": ["https://example.org/atom"]})
        assert got["items"][0]["title"] == "記事のタイトル"
        assert got["items"][0]["url"] == "https://example.org/a"
        assert got["items"][0]["from"] == "ためしブログ"

    def test_the_newest_come_first(self, serve):
        """何本のフィードから来たかに関わらず、読む側に効くのは新しさ。"""
        serve({"https://example.com/feed": RSS, "https://example.org/atom": ATOM})
        got = run({"urls": ["https://example.com/feed", "https://example.org/atom"]})
        assert [i["title"] for i in got["items"]] == ["新しいほう", "記事のタイトル", "古いほう"]

    def test_since_the_last_run_drops_the_old_ones(self, serve):
        serve({"https://example.com/feed": RSS})
        got = run(
            {"urls": ["https://example.com/feed"], "since": "last_run"},
            "2026-09-05T00:00:00+00:00",
        )
        assert [i["title"] for i in got["items"]] == ["新しいほう"]

    def test_something_without_a_date_survives_the_filter(self, serve):
        """日付を持たないフィードは普通にある。絞り込みで静かに 0 件にしない。"""
        undated = RSS.replace("<pubDate>Mon, 01 Sep 2026 12:00:00 +0900</pubDate>", "")
        serve({"https://example.com/feed": undated})
        got = run(
            {"urls": ["https://example.com/feed"], "since": "last_run"},
            "2026-09-05T00:00:00+00:00",
        )
        assert "古いほう" in [i["title"] for i in got["items"]]

    def test_a_broken_date_does_not_drop_the_entry(self, serve):
        serve({"https://example.com/feed": RSS.replace("Mon, 01 Sep 2026 12:00:00 +0900", "きのう")})
        got = run({"urls": ["https://example.com/feed"]})
        assert len(got["items"]) == 2

    def test_the_limit_cuts_from_the_newest(self, serve):
        serve({"https://example.com/feed": RSS})
        got = run({"urls": ["https://example.com/feed"], "limit": 1})
        assert [i["title"] for i in got["items"]] == ["新しいほう"]


class TestGoingOutside:
    def test_a_feed_that_is_down_does_not_stop_the_collection(self, serve):
        """参考の素材なので、1 本の不調で収集ごと止めるほうが損。"""
        serve({
            "https://example.com/feed": RSS,
            "https://down.example/feed": httpx.ConnectError("nope"),
        })
        got = run({"urls": ["https://example.com/feed", "https://down.example/feed"]})
        assert got["failed"] == 1
        assert len(got["items"]) == 2

    def test_how_many_failed_is_told_not_hidden(self, serve):
        """少ないのが世の中の都合か、道具の不調かで意味が違う。"""
        serve({"https://down.example/feed": httpx.ConnectError("nope")})
        text = feeds.render(run({"urls": ["https://down.example/feed"]}))
        assert "1 件は取れませんでした" in text

    def test_xml_with_a_dtd_is_not_read(self, serve):
        """実体参照の展開でメモリを食い尽くす細工に付き合わない。"""
        bomb = '<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "x">]><rss><channel/></rss>'
        serve({"https://example.com/feed": bomb})
        got = run({"urls": ["https://example.com/feed"]})
        assert got["failed"] == 1

    def test_it_names_the_project_and_nothing_else(self):
        """相手のログに残るので、連絡先も個人名も入れない。"""
        assert feeds.USER_AGENT == "chiezo (local knowledge server)"
        assert "@" not in feeds.USER_AGENT

    def test_it_waits_between_calls(self, monkeypatch, serve):
        """無人で回る層なので、こちらが加減しないと等間隔の足跡が延々と残る。"""
        monkeypatch.setattr(feeds, "MIN_INTERVAL", 0.05)
        serve({"https://example.com/feed": RSS, "https://example.org/atom": ATOM})
        import time

        started = time.monotonic()
        run({"urls": ["https://example.com/feed", "https://example.org/atom"]})
        assert time.monotonic() - started >= 0.05


class TestWhatTheAiSees:
    def test_it_says_the_list_is_not_the_whole_world(self, serve):
        """情報源として扱わせると、フィードが拾わなかったものは永遠に入らない。"""
        serve({"https://example.com/feed": RSS})
        text = feeds.render(run({"urls": ["https://example.com/feed"]}))
        assert "参考です" in text
        assert "これが全部ではありません" in text
        assert "自分でも調べてください" in text
        assert "新しいほう" in text

    def test_an_empty_harvest_says_so(self, serve):
        serve({"https://example.com/feed": RSS.replace("<item>", "<skip>")})
        assert "1 件も取れませんでした" in feeds.render(run({"urls": ["https://example.com/feed"]}))
