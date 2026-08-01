"""agent モード(/v1/ask?mode=agent)のテスト。

test_answer.py と同じく推論サーバは立てず、`answer._llm_client` を差し替えて偽の
OpenAI 互換サーバを演じさせる。違いは**この偽サーバが tool_calls を返せる**ことで、
「モデルが道具を呼ぶ → Chiezo が実行して結果を返す → モデルが答える」の全経路を、
実データもネットワークも GPU も無しで通せる。
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import CHAT_PATH


@pytest.fixture()
def monkeypatch_env(monkeypatch, built_data_dir):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    return monkeypatch


class ToolLLM:
    """偽の OpenAI 互換サーバ。ターンごとに「道具を呼ぶ」か「答える」かを返す。

    ターンの書き方:
      - 文字列                       … その内容で答える(道具は呼ばない)
      - [(道具名, 引数dict), ...]    … その道具を呼ぶ
    """

    def __init__(self, *turns):
        self.turns = list(turns)
        self.requests: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        turn = self.turns.pop(0) if self.turns else "分かりませんでした"
        if isinstance(turn, str):
            message = {"role": "assistant", "content": turn}
        else:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call_{i}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": args if isinstance(args, str)
                            else json.dumps(args, ensure_ascii=False),
                        },
                    }
                    for i, (name, args) in enumerate(turn)
                ],
            }
        return httpx.Response(200, json={"choices": [{"message": message}]})

    @property
    def calls(self) -> int:
        return len(self.requests)


def make_client(monkeypatch, fake: ToolLLM | None, **env) -> TestClient:
    from app import answer
    from app.main import app

    monkeypatch.setenv("CHIEZO_LLM_URL", "http://llm.test:8080/v1")
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    if fake is not None:
        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
    return TestClient(app)


def ask(client, **params):
    return client.get("/v1/ask", params={"mode": "agent", **params})


SEARCH_ASAKUSA = [("search", {"source": "jawiki", "q": "浅草寺"})]
DOC_ASAKUSA = [("doc", {"source": "jawiki", "title": "浅草寺"})]
ANSWER = "浅草寺は東京都台東区にある寺院です。"


class TestToolsComeFromMcp:
    """道具の定義は MCP のものをそのまま使う(書き写さない)。"""

    def test_agent_tools_all_exist_in_mcp(self, monkeypatch_env):
        import asyncio

        from app import agent
        from app.main import app

        with make_client(monkeypatch_env, None):
            names = {t.name for t in asyncio.run(app.state.mcp.list_tools())}
        missing = set(agent.AGENT_TOOLS) - names
        assert not missing, f"MCP に無い道具を agent に渡そうとしている: {missing}"

    def test_write_tools_are_separated_from_the_knowledge_tools(self):
        """書き込み(remember)は常時渡す群には入れない(切れる側に置く)。"""
        from app import agent

        assert "remember" not in agent.KNOWLEDGE_TOOLS
        assert "remember" in agent.NOTE_TOOLS

    def test_definitions_are_sent_in_openai_function_form(self, monkeypatch_env):
        from app import agent

        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            ask(client, q="浅草寺はどこ?")
        tools = fake.requests[0]["tools"]
        by_name = {t["function"]["name"]: t["function"] for t in tools}
        assert set(by_name) == set(agent.AGENT_TOOLS)
        # 説明文もスキーマも MCP のものが載っている(agent 用に書き直していない)
        assert "全文検索" in by_name["search"]["description"]
        assert by_name["search"]["parameters"]["properties"]["source"]["type"] == "string"

    def test_the_prompt_carries_the_mcp_usage_notes_and_the_catalog(self, monkeypatch_env):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            ask(client, q="浅草寺はどこ?")
        system = fake.requests[0]["messages"][0]["content"]
        assert "まず `search` で当たりを付け" in system  # MCP の instructions
        assert "- jawiki(wikipedia" in system  # ソース一覧


class TestToolLoop:
    def test_calls_tools_then_answers(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, DOC_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = ask(client, q="浅草寺はどこにある?")
        assert res.status_code == 200
        body = res.json()
        assert body["answer"] == ANSWER
        assert body["mode"] == "agent"
        assert [s["tool"] for s in body["steps"]] == ["search", "doc"]
        assert all(s["ok"] for s in body["steps"])
        assert fake.calls == 3  # 道具 2 回 + 最後の回答

    def test_tool_results_are_fed_back_as_tool_messages(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            ask(client, q="浅草寺はどこにある?")
        messages = fake.requests[1]["messages"]
        assert messages[-2]["role"] == "assistant" and messages[-2]["tool_calls"]
        tool_message = messages[-1]
        assert tool_message["role"] == "tool"
        assert tool_message["tool_call_id"] == "call_0"
        assert "浅草寺" in tool_message["content"]

    def test_documents_seen_become_references(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="浅草寺はどこにある?").json()
        (ref,) = [r for r in body["references"] if r["title"] == "浅草寺"]
        assert ref["source"] == "jawiki"
        assert ref["url"] == f"/search/jawiki/doc/{ref['doc_id']}"

    def test_counting_questions_can_use_filter_total(self, monkeypatch_env):
        """rag では原理的に答えられなかった問い(件数)に届くこと。"""
        turns = [
            [("tags", {"source": "jawiki", "contains": "都道府県"})],
            [("filter", {"source": "jawiki", "tag": "日本の都道府県"})],
            "2 件です。",
        ]
        fake = ToolLLM(*turns)
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="日本の都道府県の記事は何件ある?").json()
        assert [s["tool"] for s in body["steps"]] == ["tags", "filter"]
        assert body["steps"][1]["summary"] == "total=2"  # 東京都・大阪府
        assert body["answer"] == "2 件です。"

    def test_tool_errors_are_handed_back_to_the_model(self, monkeypatch_env):
        """道具の失敗でループを落とさない(404 の candidates は次の手の材料になる)。"""
        fake = ToolLLM([("doc", {"source": "jawiki", "title": "存在しない記事"})], ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="存在しない記事について", grounded=0).json()
        (step,) = body["steps"]
        assert step["ok"] is False
        assert "document not found" in step["summary"]
        returned = json.loads(fake.requests[1]["messages"][-1]["content"])
        assert "candidates" in returned  # 前置きを剥がして中身が届いている
        assert body["answer"] == ANSWER

    def test_broken_arguments_do_not_crash_the_loop(self, monkeypatch_env):
        fake = ToolLLM([("search", "{壊れた JSON")], ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="浅草寺はどこ?", grounded=0).json()
        assert body["steps"][0]["ok"] is False
        assert "JSON" in body["steps"][0]["summary"]
        assert body["answer"] == ANSWER

    def test_repeating_the_same_call_reuses_the_result(self, monkeypatch_env):
        """モデルは同じ呼び出しを 2 度出してくる。実行し直さず前回の結果を返す。

        ここでエラーを返すと、手元に結果があるのに「失敗した」と受け取って別の検索を
        足しに行き、ステップを空費する(実測)。
        """
        fake = ToolLLM(SEARCH_ASAKUSA, SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="浅草寺はどこ?").json()
        assert body["steps"][1]["ok"] is True
        assert body["steps"][1]["repeated"] is True
        assert "前回と同じ" in body["steps"][1]["summary"]
        # モデルには前回の結果が渡る(繰り返しであることを添えて)
        returned = json.loads(fake.requests[2]["messages"][-1]["content"])
        assert "浅草寺" in json.dumps(returned, ensure_ascii=False)
        assert "同じ引数で既に呼ばれた" in returned["note"]

    def test_a_repeat_in_the_same_turn_hits_the_tool_only_once(self, monkeypatch_env):
        """1 回の応答に同じ呼び出しが 2 つ並んでも、実行は 1 回だけ。"""
        twice = [
            ("search", {"source": "jawiki", "q": "浅草寺"}),
            ("search", {"source": "jawiki", "q": "浅草寺"}),
        ]
        fake = ToolLLM(twice, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            steps = ask(client, q="浅草寺はどこ?").json()["steps"]
        assert [s["repeated"] for s in steps] == [False, True]
        assert all(s["ok"] for s in steps)

    def test_step_budget_forces_a_final_answer(self, monkeypatch_env):
        """予算を使い切っても、調べただけで終わらせない(道具なしでもう 1 回聞く)。"""
        fake = ToolLLM(
            SEARCH_ASAKUSA,
            [("search", {"source": "jawiki", "q": "東京都"})],
            "調べた範囲では東京都台東区です。",
        )
        with make_client(monkeypatch_env, fake, CHIEZO_AGENT_MAX_STEPS=2) as client:
            body = ask(client, q="浅草寺はどこ?").json()
        assert len(body["steps"]) == 2
        assert body["answer"] == "調べた範囲では東京都台東区です。"
        last = fake.requests[-1]
        assert "tools" not in last  # 最後の 1 回は道具を渡さない
        assert "これ以上道具は使えません" in last["messages"][-1]["content"]

    def test_long_tool_results_are_truncated(self, monkeypatch_env):
        fake = ToolLLM(DOC_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake, CHIEZO_AGENT_TOOL_CHARS=300) as client:
            ask(client, q="浅草寺について")
        content = fake.requests[1]["messages"][-1]["content"]
        assert "300 字で切った" in content
        assert len(content) < 300 + 100


class TestGrounding:
    def test_grounded_without_any_evidence_returns_the_fixed_answer(self, monkeypatch_env):
        """道具が何も返さないまま答えさせない(rag 側の判断と同じ)。"""
        fake = ToolLLM(
            [("search", {"source": "jawiki", "q": "存在しない語句ですよこれは"})],
            "私の知識では…",
        )
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="存在しない語句ですよこれは").json()
        assert body["references"] == []
        assert "Chiezo からは分かりません" in body["answer"]

    def test_open_mode_keeps_the_models_own_answer(self, monkeypatch_env):
        fake = ToolLLM(
            [("search", {"source": "jawiki", "q": "存在しない語句ですよこれは"})],
            "私の知識では…",
        )
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="存在しない語句ですよこれは", grounded=0).json()
        assert body["answer"] == "私の知識では…"

    def test_grounded_accepts_counts_as_evidence(self, monkeypatch_env):
        """文書を返さない道具(件数)でも根拠として認めること。"""
        fake = ToolLLM([("filter", {"source": "jawiki", "tag": "日本の都道府県"})], "2 件です。")
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="日本の都道府県の記事は何件?").json()
        assert body["answer"] == "2 件です。"

    def test_policy_shows_up_in_the_system_prompt(self, monkeypatch_env):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            ask(client, q="浅草寺はどこ?", grounded=0)
        assert "自分の知識で補ってよい" in fake.requests[0]["messages"][0]["content"]


class TestSourcePinning:
    def test_pinned_source_is_stated_in_the_prompt(self, monkeypatch_env):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            ask(client, q="浅草寺はどこ?", source="jawiki")
        system = fake.requests[0]["messages"][0]["content"]
        assert "**このやり取りでは jawiki だけを引くこと**" in system

    def test_unknown_source_is_404(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM(ANSWER)) as client:
            res = ask(client, q="x", source="nosuch")
        assert res.status_code == 404

    def test_unknown_source_is_404_even_when_streaming(self, monkeypatch_env):
        """SSE はヘッダ送出後にステータスを変えられないので、検査は流す前に済ませる。"""
        with make_client(monkeypatch_env, ToolLLM(ANSWER)) as client:
            res = ask(client, q="x", source="nosuch", stream=1)
        assert res.status_code == 404


class TestStreaming:
    def test_sse_reports_steps_then_references_then_the_answer(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = ask(client, q="浅草寺はどこ?", stream=1)
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [
            line[len("event: "):]
            for line in res.text.splitlines() if line.startswith("event: ")
        ]
        assert events == ["meta", "step", "references", "delta", "done"]
        assert ANSWER in res.text

    def test_upstream_failure_mid_loop_becomes_an_error_event(self, monkeypatch_env):
        from app import answer

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        )
        with make_client(monkeypatch_env, None) as client:
            res = ask(client, q="浅草寺はどこ?", stream=1)
        assert res.status_code == 200  # 流し始めた後なのでステータスは変えられない
        assert "event: error" in res.text
        assert "llm unreachable" in res.text

    def test_unreachable_llm_is_502_without_streaming(self, monkeypatch_env):
        from app import answer

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        )
        with make_client(monkeypatch_env, None) as client:
            res = ask(client, q="浅草寺はどこ?")
        assert res.status_code == 502


class TestAskPage:
    def test_page_offers_the_mode_switch(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            res = client.get(CHAT_PATH)
        assert 'name="mode"' in res.text
        assert "モデルに道具を引かせる" in res.text

    def test_chat_page_preselects_the_mode(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            res = client.get(CHAT_PATH, params={"q": "浅草寺はどこ?", "mode": "agent"})
        assert '<option value="agent" selected>' in res.text
        assert 'data-first="浅草寺はどこ?"' in res.text

    def test_nojs_page_shows_the_trace(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get(
                CHAT_PATH, params={"q": "浅草寺はどこ?", "mode": "agent", "nojs": 1}
            )
        assert "調べた手順" in res.text
        assert "search" in res.text
        assert ANSWER in res.text

    def test_rag_stays_the_default(self, monkeypatch_env):
        """既定は rag のまま(agent は明示的に選んだときだけ)。"""
        fake = ToolLLM(
            json.dumps({"queries": [{"source": "jawiki", "q": "浅草寺"}]}, ensure_ascii=False),
            ANSWER,
        )
        with make_client(monkeypatch_env, fake) as client:
            body = client.get("/v1/ask", params={"q": "浅草寺はどこ?"}).json()
        assert "queries" in body and "steps" not in body
        assert "tools" not in fake.requests[0]


class TestReasoningLeftovers:
    """思考タグの残骸を本文に出さない(実測: Qwen3 + 思考オフで `</think>` が残った)。"""

    def test_orphan_think_tag_is_stripped(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, "</think>\n\n浅草寺は東京都台東区にあります。")
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="浅草寺はどこ?").json()
        assert body["answer"] == "浅草寺は東京都台東区にあります。"

    def test_think_block_is_stripped(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, "<think>まず検索する</think>台東区です。")
        with make_client(monkeypatch_env, fake) as client:
            body = ask(client, q="浅草寺はどこ?").json()
        assert body["answer"] == "台東区です。"


class TestStepNumbering:
    def test_parallel_calls_in_one_turn_get_distinct_numbers(self, monkeypatch_env):
        """1 ターンで 2 つ呼ばれても番号は通し(同じ番号が並ぶと手順を追えない)。"""
        both = [
            ("search", {"source": "jawiki", "q": "浅草寺"}),
            ("search", {"source": "jawiki", "q": "雷門"}),
        ]
        fake = ToolLLM(both, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            steps = ask(client, q="浅草寺と雷門は?").json()["steps"]
        assert [s["step"] for s in steps] == [1, 2]
        assert [s["turn"] for s in steps] == [1, 1]
