"""会話(/v1/chat)と web 検索の道具のテスト。

`/v1/ask` が 1 問 1 答なのに対し、`/v1/chat` は **messages をまるごと受け取る**
(サーバーは会話の状態を持たず、履歴はクライアントが毎回送る)。ここで確かめるのは
その約束と、履歴がちゃんとモデルまで届いていること。

web 検索は `websearch._client` を差し替えて偽の検索サーバを演じさせる
(外に出ずに、道具の出方・出典の付き方・無効時の振る舞いを通せる)。
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from test_agent import ANSWER, SEARCH_ASAKUSA, ToolLLM, make_client  # noqa: F401


@pytest.fixture()
def monkeypatch_env(monkeypatch, built_data_dir):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.delenv("CHIEZO_WEB_SEARCH_URL", raising=False)
    monkeypatch.delenv("CHIEZO_ASK_DEFAULT_MODE", raising=False)
    monkeypatch.delenv("CHIEZO_ASK_DEFAULT_GROUNDED", raising=False)
    return monkeypatch


PLAN_OK = json.dumps({"queries": [{"source": "jawiki", "q": "浅草寺"}]}, ensure_ascii=False)


def chat(client: TestClient, messages, **body):
    return client.post("/v1/chat", json={"messages": messages, **body})


class TestChatContract:
    def test_last_user_message_is_the_question(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = chat(client, [{"role": "user", "content": "浅草寺はどこ?"}], mode="agent")
        assert res.status_code == 200
        assert res.json()["answer"] == ANSWER
        assert res.json()["question"] == "浅草寺はどこ?"

    def test_history_reaches_the_model(self, monkeypatch_env):
        """「じゃあ京都のほうは?」が通じるのは、直前のやり取りを毎回送るから。"""
        fake = ToolLLM(ANSWER)
        history = [
            {"role": "user", "content": "浅草寺について教えて"},
            {"role": "assistant", "content": "東京都台東区の寺院です。"},
            {"role": "user", "content": "じゃあ京都のほうは?"},
        ]
        with make_client(monkeypatch_env, fake) as client:
            chat(client, history, mode="agent")
        sent = fake.requests[0]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[-1]["content"] == "じゃあ京都のほうは?"
        assert sent[2]["content"] == "東京都台東区の寺院です。"

    def test_rag_mode_sees_the_history_when_building_queries(self, monkeypatch_env):
        """rag はクエリ生成の段でも履歴が要る(指示語を検索語に直せない)。"""
        fake = ToolLLM(PLAN_OK, ANSWER)
        history = [
            {"role": "user", "content": "浅草寺について教えて"},
            {"role": "assistant", "content": "東京都台東区の寺院です。"},
            {"role": "user", "content": "その最寄り駅は?"},
        ]
        with make_client(monkeypatch_env, fake) as client:
            res = chat(client, history, mode="rag")
        assert res.status_code == 200
        plan_prompt = fake.requests[0]["messages"][-1]["content"]
        assert "これまでのやり取り:" in plan_prompt
        assert "東京都台東区の寺院です。" in plan_prompt

    def test_empty_or_assistant_tail_is_400(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM(ANSWER)) as client:
            assert chat(client, []).status_code == 400
            assert chat(client, [{"role": "assistant", "content": "はい"}]).status_code == 400

    def test_streaming_uses_the_same_events(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = client.post(
                "/v1/chat?stream=1",
                json={"messages": [{"role": "user", "content": "浅草寺は?"}], "mode": "agent"},
            )
        events = [
            line[len("event: "):]
            for line in res.text.splitlines() if line.startswith("event: ")
        ]
        assert events == ["meta", "step", "references", "delta", "done"]

    def test_disabled_layer_is_503(self, monkeypatch_env):
        monkeypatch_env.delenv("CHIEZO_LLM_URL", raising=False)
        from app.main import app

        with TestClient(app) as client:
            res = chat(client, [{"role": "user", "content": "こんにちは"}])
        assert res.status_code == 503


class TestDefaults:
    """既定は環境変数で決める(GPU の機械と CPU だけの機械で妥当な既定が違うため)。"""

    def test_rag_and_grounded_are_the_plain_defaults(self, monkeypatch_env):
        fake = ToolLLM(PLAN_OK, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = chat(client, [{"role": "user", "content": "浅草寺は?"}]).json()
        assert "queries" in body and body["grounded"] is True

    def test_env_can_flip_them(self, monkeypatch_env):
        fake = ToolLLM(ANSWER)
        with make_client(
            monkeypatch_env, fake,
            CHIEZO_ASK_DEFAULT_MODE="agent", CHIEZO_ASK_DEFAULT_GROUNDED="0",
        ) as client:
            body = chat(client, [{"role": "user", "content": "こんにちは"}]).json()
        assert body["mode"] == "agent" and body["grounded"] is False
        # 道具を呼ばずに答えてよい(= 雑談がそのまま通る)
        assert body["steps"] == []

    def test_page_follows_the_env_default(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM(), CHIEZO_ASK_DEFAULT_MODE="agent") as client:
            res = client.get("/ask")
        assert '<option value="agent" selected>' in res.text


class FakeSearch:
    """偽の検索サーバ(SearXNG の JSON 形式)。"""

    def __init__(self, *titles):
        self.titles = titles
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={
            "results": [
                {"title": t, "content": f"{t} の要約", "url": f"https://example.com/{i}"}
                for i, t in enumerate(self.titles)
            ]
        })


@pytest.fixture()
def web(monkeypatch_env):
    from app import websearch

    fake = FakeSearch("最新のニュース", "続報")
    monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searx.test/search")
    monkeypatch_env.setattr(websearch, "MIN_INTERVAL", 0.0)
    monkeypatch_env.setattr(
        websearch, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


class TestWebSearch:
    def test_tool_is_hidden_while_disabled(self, monkeypatch_env):
        """無効なら道具ごと出さない(使えないものを文脈に並べない)。"""
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            chat(client, [{"role": "user", "content": "こんにちは"}], mode="agent")
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "web_search" not in names

    def test_tool_appears_when_enabled(self, monkeypatch_env, web):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            chat(client, [{"role": "user", "content": "こんにちは"}], mode="agent")
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "web_search" in names
        # chiezo が先、という順番はプロンプト側で固定する
        assert "まず chiezo を引く" in fake.requests[0]["messages"][0]["content"]

    def test_results_become_web_references(self, monkeypatch_env, web):
        fake = ToolLLM([("web_search", {"q": "最新のニュース"})], "web で調べた限り…")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "最近のニュースは?"}], mode="agent"
            ).json()
        assert body["steps"][0]["ok"] is True
        # 出典は web と分かる形で並ぶ(chiezo の文書と混ざっても区別できる)
        assert [r["source"] for r in body["references"]] == ["web", "web"]
        assert body["references"][0]["url"].startswith("https://example.com/")
        assert len(web.requests) == 1

    def test_user_agent_carries_no_personal_information(self, monkeypatch_env):
        """名乗るのはプロジェクト名だけ。連絡先や個人名を相手のログに残さない。"""
        from app import websearch

        monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searx.test/search")
        ua = websearch._client().headers["user-agent"]
        assert ua == "chiezo (local knowledge server)"
        assert "@" not in ua

    def test_web_results_count_as_evidence_when_grounded(self, monkeypatch_env, web):
        """grounded は「取ってきた根拠に限る」で、その根拠源に web も含む。"""
        fake = ToolLLM([("web_search", {"q": "最新のニュース"})], "web によれば…")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "最近のニュースは?"}],
                mode="agent", grounded=True,
            ).json()
        assert body["answer"] == "web によれば…"

    def test_calling_it_while_disabled_is_refused(self, monkeypatch_env):
        fake = ToolLLM([("web_search", {"q": "何か"})], "はい")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "何か"}], mode="agent", grounded=False
            ).json()
        assert body["steps"][0]["ok"] is False
        assert "disabled" in body["steps"][0]["summary"]

    def test_failures_are_handed_back_to_the_model(self, monkeypatch_env, monkeypatch):
        from app import websearch

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searx.test/search")
        monkeypatch_env.setattr(websearch, "MIN_INTERVAL", 0.0)
        monkeypatch_env.setattr(
            websearch, "_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        )
        fake = ToolLLM([("web_search", {"q": "何か"})], "web は使えませんでした")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "何か"}], mode="agent", grounded=False
            ).json()
        assert body["steps"][0]["ok"] is False
        assert "unreachable" in body["steps"][0]["summary"]
        assert body["answer"] == "web は使えませんでした"
