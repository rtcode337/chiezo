"""/mcp(MCP Streamable HTTP)のテスト。

トランスポートは stateless なので initialize のハンドシェイクなしに JSON-RPC を
1 発 POST できる。レスポンスは SSE フレームで返るので data: 行だけ拾う。
"""
import inspect
import json

import pytest
from fastapi.testclient import TestClient

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

# MCP ツール名 → 実体である REST エンドポイント関数名と、呼び出しに要る最小の引数。
# シグネチャ突き合わせテスト(下記)がこの表を使う。
TOOL_ENDPOINTS = {
    "sources": ("list_sources", {}),
    "search": ("search", {"source": "jawiki", "q": "東京"}),
    "doc": ("get_doc_by_title", {"source": "jawiki", "title": "東京都"}),
    "filter": ("filter_docs", {"source": "jawiki", "tag": "関東地方"}),
    "tags": ("list_tags", {"source": "jawiki"}),
    "titles": ("titles", {"source": "jawiki", "prefix": "東京"}),
    "links": ("links", {"source": "jawiki", "title": "東京都"}),
}


@pytest.fixture(scope="module")
def monkeypatch_module():
    mp = pytest.MonkeyPatch()
    yield mp
    mp.undo()


@pytest.fixture(scope="module")
def client(built_data_dir, monkeypatch_module):
    monkeypatch_module.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        yield c


def rpc(client, method: str, params: dict | None = None, rpc_id: int = 1) -> dict:
    """JSON-RPC を 1 回投げ、SSE フレームから result / error を取り出す。"""
    res = client.post(
        "/mcp/",
        headers=MCP_HEADERS,
        json={"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}},
    )
    assert res.status_code == 200, res.text
    for line in res.text.splitlines():
        if line.startswith("data: "):
            return json.loads(line[len("data: "):])
    raise AssertionError(f"no data frame in response: {res.text!r}")


def call_tool(client, name: str, arguments: dict) -> dict:
    """tools/call の戻り(content[0].text の JSON)と isError を返す。

    エラー時は FastMCP が "Error executing tool <name>: " を前置きするので、
    最初の `{` 以降を JSON として読む。
    """
    body = rpc(client, "tools/call", {"name": name, "arguments": arguments})
    result = body["result"]
    text = result["content"][0]["text"]
    try:
        payload = json.loads(text[text.index("{"):])
    except ValueError:
        payload = {"_text": text}
    return {"isError": result.get("isError", False), "payload": payload}


class TestToolListing:
    def test_lists_all_tools(self, client):
        tools = {t["name"]: t for t in rpc(client, "tools/list")["result"]["tools"]}
        assert set(tools) == set(TOOL_ENDPOINTS)

    def test_tools_have_descriptions_and_schemas(self, client):
        for tool in rpc(client, "tools/list")["result"]["tools"]:
            assert tool["description"], tool["name"]
            assert tool["inputSchema"]["type"] == "object"

    def test_search_tool_exposes_the_filter_parameters(self, client):
        tools = {t["name"]: t for t in rpc(client, "tools/list")["result"]["tools"]}
        props = tools["search"]["inputSchema"]["properties"]
        assert {"source", "q", "limit", "offset", "area", "feature", "bbox", "tag"} <= set(props)


class TestTools:
    def test_sources(self, client):
        out = call_tool(client, "sources", {})
        assert [s["name"] for s in out["payload"]["sources"]] == ["jawiki"]

    def test_search(self, client):
        out = call_tool(client, "search", {"source": "jawiki", "q": "浅草寺"})
        assert out["payload"]["mode"] == "fts"
        assert [r["title"] for r in out["payload"]["results"]] == ["浅草寺"]

    def test_doc_truncates_body_by_default(self, client):
        from app.mcp_server import MCP_DOC_MAX_CHARS

        out = call_tool(client, "doc", {"source": "jawiki", "title": "東京都"})
        assert out["payload"]["title"] == "東京都"
        # REST の既定は無制限だが、MCP はコンテキストに直接載るので既定で切る
        assert len(out["payload"]["body"]) <= MCP_DOC_MAX_CHARS

    def test_filter_by_tag(self, client):
        out = call_tool(client, "filter", {"source": "jawiki", "tag": "日本の都道府県"})
        assert out["payload"]["total"] == 2

    def test_tags(self, client):
        out = call_tool(client, "tags", {"source": "jawiki", "prefix": "日本の"})
        assert {t["tag"] for t in out["payload"]["tags"]} >= {"日本の山", "日本の鉄道"}

    def test_titles_and_links(self, client):
        titles = call_tool(client, "titles", {"source": "jawiki", "prefix": "浅草"})
        assert [t["title"] for t in titles["payload"]["titles"]] == ["浅草寺"]
        links = call_tool(client, "links", {"source": "jawiki", "title": "浅草寺"})
        assert "雷門" in links["payload"]["links"]


class TestErrors:
    """HTTPException の detail を握り潰さずツールエラーとして返すこと。

    404 の候補一覧や 409 の移行案内は、モデルが次の手を決めるのに要る情報なので、
    「エラーになりました」だけを返すと使い物にならない。
    """

    def test_unknown_source_reports_available_sources(self, client):
        out = call_tool(client, "search", {"source": "nosuch", "q": "x"})
        assert out["isError"] is True
        assert "unknown source" in out["payload"]["error"]
        assert out["payload"]["sources"] == ["jawiki"]

    def test_missing_doc_reports_candidates(self, client):
        out = call_tool(client, "doc", {"source": "jawiki", "title": "浅草"})
        assert out["isError"] is True
        assert "浅草寺" in out["payload"]["candidates"]

    def test_filter_without_condition_is_rejected(self, client):
        out = call_tool(client, "filter", {"source": "jawiki"})
        assert out["isError"] is True
        assert "at least one of" in out["payload"]["error"]


class TestStaysInSyncWithRest:
    """MCP は REST エンドポイント関数をそのまま呼ぶ薄いラッパである、という前提の担保。

    FastAPI のエンドポイントは既定値が `Query(...)` オブジェクトなので、Python から
    直接呼ぶときに引数を 1 つでも渡し忘れると、値として Query インスタンスが入り込む
    (`if tag:` が常に真になる等、例外にならず静かに壊れる)。REST 側にパラメータが
    増えたらこのテストが落ちるので、MCP 側の追従漏れに気づける。
    """

    @pytest.mark.parametrize("tool", sorted(TOOL_ENDPOINTS))
    def test_every_endpoint_parameter_is_passed(self, client, tool, monkeypatch):
        from app import main as api

        endpoint_name, arguments = TOOL_ENDPOINTS[tool]
        endpoint = getattr(api, endpoint_name)
        expected = set(inspect.signature(endpoint).parameters)

        passed: dict = {}

        def recorder(**kwargs):
            passed.update(kwargs)
            return {}

        monkeypatch.setattr(api, endpoint_name, recorder)
        call_tool(client, tool, arguments)

        missing = expected - set(passed)
        assert not missing, (
            f"MCP ツール {tool} が {endpoint_name} の引数 {sorted(missing)} を渡していない"
            "(渡し忘れると Query オブジェクトが値として入る)"
        )
        assert not set(passed) - expected

    def test_query_defaults_never_leak_into_endpoints(self, client, monkeypatch):
        """渡された値が Query オブジェクトでない(= 実値である)ことも確かめる。"""
        from fastapi import params

        from app import main as api

        seen: dict = {}

        def recorder(**kwargs):
            seen.update(kwargs)
            return {}

        monkeypatch.setattr(api, "search", recorder)
        call_tool(client, "search", {"source": "jawiki", "q": "東京"})
        leaked = [k for k, v in seen.items() if isinstance(v, params.Query)]
        assert not leaked, f"Query の既定値が値として渡っている: {leaked}"
