"""会話の口(app/talk.py)と会話ブリッジ(bridge/talk_bridge.py)のテスト。

CLI は起動しない。本体の側は偽のブリッジ(`httpx.MockTransport`)を相手にし、
ブリッジの側は CLI との接ぎ目(`TalkBackend`)を偽物に替えて、
「何を渡し、何を返し、失敗をどう伝えるか」を確かめる。
"""
import asyncio
import importlib
import json

import httpx
import pytest
from fastapi.testclient import TestClient

CHARACTER = "あなたは「テスト子」。明るく話す。"


# ---- 本体の口(app/talk.py)---------------------------------------------------


class FakeBridge:
    """会話ブリッジの偽物。受けた要求を控え、決めた応答を返す。"""

    def __init__(self):
        self.requests: list[tuple[str, str, dict]] = []
        self.turn_status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/v1/talk/threads":
            return httpx.Response(200, json={"thread_id": "th1", "cli": "codex", "model": "gpt-test"})
        if path.endswith("/turns"):
            if self.turn_status != 200:
                return httpx.Response(self.turn_status, json={"detail": {"error": "something broke"}})
            return httpx.Response(200, json={
                "content": '{"text":"やっほー"}', "model": "gpt-test",
                "tools": [{"tool": "search", "arguments": "{}"}],
                "usage": {"input_tokens": 100, "output_tokens": 5, "cached_tokens": 80},
                "ms": 1234,
            })
        if request.method == "DELETE":
            return httpx.Response(200, json={"ended": True})
        if path == "/health":
            return httpx.Response(200, json={"ok": True, "threads": 2})
        return httpx.Response(404, json={})


@pytest.fixture()
def fake_bridge(monkeypatch):
    from app import talk

    fake = FakeBridge()
    monkeypatch.setattr(talk, "_transport", httpx.MockTransport(fake.handler))
    return fake


@pytest.fixture()
def client(tmp_path, built_data_dir, monkeypatch, fake_bridge):
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    from app.main import app

    with TestClient(app) as c:
        c.post("/v1/chiezo_memory", json={"text": CHARACTER, "title": "キャラ設定: テスト子"})
        yield c


class TestStart:
    def test_character_comes_from_short_term_memory(self, client, fake_bridge):
        res = client.post("/v1/talk/sessions", json={
            "character": "キャラ設定: テスト子", "instructions": "相手は 1 人。",
        })
        assert res.status_code == 200
        assert res.json()["session_id"] == "codex:th1"
        body = fake_bridge.requests[-1][2]
        # キャラ設定 → 会話の決まりごと → 呼ぶ側の補足 の順で 1 つの指示になる
        text = body["instructions"]
        assert text.index("テスト子") < text.index("会話の進め方") < text.index("相手は 1 人。")
        # web は頼まれなければ閉じる
        assert body["web"] is False

    def test_missing_character_is_404_before_reaching_the_bridge(self, client, fake_bridge):
        res = client.post("/v1/talk/sessions", json={"character": "居ないキャラ"})
        assert res.status_code == 404
        assert "居ないキャラ" in res.json()["hint"]
        assert fake_bridge.requests == []

    def test_unknown_backend_is_400(self, client):
        res = client.post("/v1/talk/sessions", json={"character": "キャラ設定: テスト子", "backend": "nope"})
        assert res.status_code == 400

    def test_unreachable_bridge_says_how_to_set_it_up(self, client, monkeypatch):
        from app import talk

        def refuse(request):
            raise httpx.ConnectError("no route")

        monkeypatch.setattr(talk, "_transport", httpx.MockTransport(refuse))
        res = client.post("/v1/talk/sessions", json={"character": "キャラ設定: テスト子"})
        assert res.status_code == 502
        assert "chiezo-talk" in res.json()["hint"]


class TestTurn:
    def test_forwards_the_text_and_the_shape_and_records_usage(self, client, fake_bridge):
        from app import usage_store

        schema = {"type": "object", "properties": {"text": {"type": "string"}}}
        res = client.post("/v1/talk/sessions/codex:th1/turns", json={
            "text": "やっほー", "schema": schema, "requested_by": "myapp",
        })
        assert res.status_code == 200
        assert res.json()["content"] == '{"text":"やっほー"}'
        assert res.json()["tools"][0]["tool"] == "search"
        _, path, body = fake_bridge.requests[-1]
        assert path == "/v1/talk/threads/th1/turns"
        assert body["schema"] == schema
        # 他の依頼と同じ表に、誰が頼んだかつきで残る
        calls = usage_store.recent_calls(5)
        assert calls[0]["backend"] == "codex"
        assert calls[0]["caller"] == "api:myapp"

    def test_lost_session_is_404_and_not_a_failure(self, client, fake_bridge):
        from app import ai_log

        fake_bridge.turn_status = 404
        res = client.post("/v1/talk/sessions/codex:th1/turns", json={"text": "まだいる?"})
        assert res.status_code == 404
        assert ai_log.recent(5) == []

    def test_bridge_failure_is_recorded(self, client, fake_bridge):
        from app import ai_log

        fake_bridge.turn_status = 502
        res = client.post("/v1/talk/sessions/codex:th1/turns", json={"text": "やっほー"})
        assert res.status_code == 502
        assert ai_log.recent(5)[0]["status"] == 502

    def test_malformed_session_id_is_404(self, client):
        assert client.post("/v1/talk/sessions/th1/turns", json={"text": "x"}).status_code == 404

    def test_end_is_forwarded(self, client, fake_bridge):
        assert client.delete("/v1/talk/sessions/codex:th1").json() == {"ended": True}
        assert fake_bridge.requests[-1][:2] == ("DELETE", "/v1/talk/threads/th1")


def test_bridges_cannot_start_another_conversation():
    """会話ブリッジの相手がさらに会話を始めないよう、AI を使う口として数える。"""
    from app.main import _asks_an_ai

    assert _asks_an_ai("/v1/talk/sessions")
    assert _asks_an_ai("/v1/talk/sessions/codex:th1/turns")


# ---- 会話ブリッジ(bridge/talk_bridge.py)---------------------------------------


class FakeBackend:
    def __init__(self):
        self.generation = 1
        self.started: list[tuple[str, str, bool]] = []
        self.turns: list[tuple[str, str, dict | None, str]] = []
        self.ended: list[str] = []

    async def start_thread(self, instructions, model, web):
        self.started.append((instructions, model, web))
        return f"t{len(self.started)}", model or "gpt-test"

    async def run_turn(self, thread_id, text, schema, effort):
        from talk_bridge import TurnResult

        self.turns.append((thread_id, text, schema, effort))
        return TurnResult(content=f"echo:{text}", model="gpt-test")

    async def end_thread(self, thread_id):
        self.ended.append(thread_id)

    async def check(self):
        return True, ""

    async def close(self):
        pass


@pytest.fixture()
def talk(monkeypatch):
    for key in ("CHIEZO_TALK_CLI", "CHIEZO_TALK_MODEL", "CHIEZO_TALK_EFFORT", "CHIEZO_TALK_MAX_THREADS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("CHIEZO_TALK_MAX_THREADS", "3")
    import talk_bridge

    module = importlib.reload(talk_bridge)
    module.backend = FakeBackend()
    module.threads.clear()
    return module


@pytest.fixture()
def bridge_client(talk):
    with TestClient(talk.app) as c:
        yield c


class TestBridge:
    def test_a_conversation_keeps_its_thread(self, bridge_client, talk):
        thread_id = bridge_client.post("/v1/talk/threads", json={"instructions": "x"}).json()["thread_id"]
        res = bridge_client.post(f"/v1/talk/threads/{thread_id}/turns", json={"text": "やっほー", "schema": {"a": 1}})
        assert res.json()["content"] == "echo:やっほー"
        # 返事の形は `schema` で受け取る。考える量は頼まれなければ既定(軽い)
        assert talk.backend.turns[-1] == (thread_id, "やっほー", {"a": 1}, "low")
        assert bridge_client.delete(f"/v1/talk/threads/{thread_id}").json() == {"ended": True}
        assert talk.backend.ended == [thread_id]

    def test_web_is_closed_unless_asked(self, bridge_client, talk):
        bridge_client.post("/v1/talk/threads", json={"instructions": "x"})
        bridge_client.post("/v1/talk/threads", json={"instructions": "x", "web": True})
        assert [web for _, _, web in talk.backend.started] == [False, True]

    def test_unknown_thread_is_404(self, bridge_client):
        assert bridge_client.post("/v1/talk/threads/nope/turns", json={"text": "x"}).status_code == 404

    def test_threads_from_before_a_restart_are_404(self, bridge_client, talk):
        thread_id = bridge_client.post("/v1/talk/threads", json={"instructions": "x"}).json()["thread_id"]
        # CLI が落ちて起動し直すと、前のスレッドは CLI の側にもう無い
        talk.backend.generation += 1
        assert bridge_client.post(f"/v1/talk/threads/{thread_id}/turns", json={"text": "x"}).status_code == 404

    def test_oldest_idle_thread_makes_room(self, bridge_client, talk):
        ids = [bridge_client.post("/v1/talk/threads", json={"instructions": "x"}).json()["thread_id"]
               for _ in range(4)]
        # 上限 3 本。4 本目を始めるとき、いちばん長く話していない 1 本目を片付ける
        assert ids[0] not in talk.threads
        assert talk.backend.ended == [ids[0]]


def _feed(messages: list[dict]) -> asyncio.Queue:
    queue: asyncio.Queue = asyncio.Queue()
    for m in messages:
        queue.put_nowait(m)
    return queue


class TestCodexNotifications:
    def test_collects_answer_tools_and_usage(self, talk):
        queue = _feed([
            {"method": "item/completed", "params": {"item": {"type": "mcpToolCall", "tool": "search",
                                                             "arguments": {"q": "富士山"}}}},
            {"method": "thread/tokenUsage/updated", "params": {"tokenUsage": {"last": {
                "inputTokens": 10, "outputTokens": 2, "cachedInputTokens": 8}}}},
            {"method": "item/completed", "params": {"item": {"type": "agentMessage", "text": "3776m"}}},
            {"method": "turn/completed", "params": {"turn": {"status": "completed"}}},
        ])
        result = asyncio.run(talk.CodexAppServer._collect(queue))
        assert result.content == "3776m"
        assert result.tools[0]["tool"] == "search"
        assert result.usage == {"input_tokens": 10, "output_tokens": 2, "cached_tokens": 8}

    def test_signed_out_is_401(self, talk):
        queue = _feed([{"method": "error", "params": {
            "error": {"message": "auth", "codexErrorInfo": "unauthorized"}, "willRetry": False}}])
        with pytest.raises(talk.TalkError) as e:
            asyncio.run(talk.CodexAppServer._collect(queue))
        assert e.value.status == 401

    def test_failed_turn_carries_the_reason(self, talk):
        queue = _feed([{"method": "turn/completed", "params": {
            "turn": {"status": "failed", "error": {"message": "overloaded"}}}}])
        with pytest.raises(talk.TalkError) as e:
            asyncio.run(talk.CodexAppServer._collect(queue))
        assert "overloaded" in e.value.message

    def test_mcp_is_attached_with_auto_approval(self, talk):
        cmd = talk.CodexAppServer()._command()
        assert cmd[:2] == ["codex", "app-server"]
        assert any("mcp_servers.chiezo.url" in c and c.endswith('/mcp/knowledge/"') for c in cmd)
        assert 'mcp_servers.chiezo.default_tools_approval_mode="auto"' in cmd
