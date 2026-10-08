"""Claude API を OpenAI 互換の口に見せる変換(`app/anthropic_api.py`)。

相手(Anthropic)へは出ない。SDK のクライアントを差し替えて、往復の形だけを確かめる。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import anthropic
import httpx
from anthropic.types import Message

from app import anthropic_api


def _message(content: list[dict], *, stop: str = "end_turn", model: str = "claude-opus-5", **extra) -> Message:
    return Message.model_validate({
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop,
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5,
                  "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
        **extra,
    })


class TestToRequest:
    def test_system_is_lifted_out_of_the_conversation(self):
        kwargs = anthropic_api.to_request({
            "model": "claude-opus-5",
            "messages": [
                {"role": "system", "content": "簡潔に答える"},
                {"role": "user", "content": "こんにちは"},
            ],
        })
        assert kwargs["system"] == "簡潔に答える"
        assert kwargs["messages"] == [{"role": "user", "content": [{"type": "text", "text": "こんにちは"}]}]

    def test_sampling_parameters_are_not_sent(self):
        # Claude Opus 5 以降は temperature / top_p を受け付けず 400 になる
        kwargs = anthropic_api.to_request({
            "model": "claude-opus-5", "temperature": 0.2, "top_p": 0.9,
            "messages": [{"role": "user", "content": "x"}],
        })
        assert "temperature" not in kwargs and "top_p" not in kwargs

    def test_effort_goes_to_output_config_except_for_haiku(self):
        opus = anthropic_api.to_request({
            "model": "claude-opus-5", "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "x"}],
        })
        haiku = anthropic_api.to_request({
            "model": "claude-haiku-4-5", "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "x"}],
        })
        assert opus["output_config"] == {"effort": "low"}
        assert "output_config" not in haiku

    def test_folded_name_carries_the_effort(self):
        kwargs = anthropic_api.to_request({
            "model": "claude-opus-5-xhigh", "reasoning_effort": "low",
            "messages": [{"role": "user", "content": "x"}],
        })
        # 名前に畳んだ段が勝つ(素の名前と 2 つ組で頼まれた回の値ではない)
        assert (kwargs["model"], kwargs["output_config"]) == ("claude-opus-5", {"effort": "xhigh"})
        # 版の数字は段ではない
        assert anthropic_api.split_model("claude-opus-5-5") == ("claude-opus-5-5", "")

    def test_fallbacks_only_on_models_that_take_them(self):
        opus = anthropic_api.to_request({"model": "claude-opus-5", "messages": [{"role": "user", "content": "x"}]})
        sonnet = anthropic_api.to_request({"model": "claude-sonnet-5", "messages": [{"role": "user", "content": "x"}]})
        assert opus["fallbacks"] == "default" and opus["betas"] == [anthropic_api.FALLBACK_BETA]
        assert "fallbacks" not in sonnet and "betas" not in sonnet

    def test_tool_round_trip_becomes_tool_use_and_one_result_message(self):
        kwargs = anthropic_api.to_request({
            "model": "claude-opus-5",
            "tools": [{"type": "function", "function": {
                "name": "search", "description": "探す",
                "parameters": {"type": "object", "properties": {"q": {"type": "string"}}},
            }}],
            "messages": [
                {"role": "user", "content": "東京駅は?"},
                {"role": "assistant", "content": None, "tool_calls": [
                    {"id": "c1", "type": "function", "function": {"name": "search", "arguments": '{"q": "東京駅"}'}},
                    {"id": "c2", "type": "function", "function": {"name": "search", "arguments": '{"q": "丸の内"}'}},
                ]},
                {"role": "tool", "tool_call_id": "c1", "content": "東京駅の結果"},
                {"role": "tool", "tool_call_id": "c2", "content": ""},
            ],
        })
        assert kwargs["tools"] == [{
            "name": "search", "description": "探す",
            "input_schema": {"type": "object", "properties": {"q": {"type": "string"}}},
        }]
        assistant, results = kwargs["messages"][1], kwargs["messages"][2]
        assert [b["input"] for b in assistant["content"]] == [{"q": "東京駅"}, {"q": "丸の内"}]
        # 並列の呼び出しの結果は 1 つの user の発言にまとめる
        assert results["role"] == "user"
        assert [b["tool_use_id"] for b in results["content"]] == ["c1", "c2"]
        assert results["content"][1]["content"]  # 空の結果も空のまま渡さない

    def test_raw_blocks_are_sent_back_as_is(self):
        raw = [{"type": "thinking", "thinking": "", "signature": "sig"},
               {"type": "tool_use", "id": "c1", "name": "search", "input": {"q": "x"}}]
        kwargs = anthropic_api.to_request({
            "model": "claude-opus-5",
            "messages": [
                {"role": "user", "content": "x"},
                {"role": "assistant", "content": "", "tool_calls": [], anthropic_api.RAW_KEY: raw},
            ],
        })
        assert kwargs["messages"][1]["content"] == raw

    def test_images_in_user_content(self):
        kwargs = anthropic_api.to_request({
            "model": "claude-opus-5",
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "これは何?"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
            ]}],
        })
        blocks = kwargs["messages"][0]["content"]
        assert blocks[1]["source"] == {"type": "base64", "media_type": "image/png", "data": "AAAA"}
        assert blocks[2]["source"] == {"type": "url", "url": "https://example.com/a.png"}


class TestToCompletion:
    def test_text_and_usage(self):
        body = anthropic_api.to_completion(_message([{"type": "text", "text": "こんにちは"}]))
        assert body["model"] == "claude-opus-5"
        assert body["choices"][0]["message"]["content"] == "こんにちは"
        assert body["choices"][0]["finish_reason"] == "stop"
        # 入力はキャッシュのぶんも足し、読んだぶんは内訳で言う
        assert body["usage"] == {"prompt_tokens": 130, "completion_tokens": 5,
                                 "prompt_tokens_details": {"cached_tokens": 100}}

    def test_tool_calls_carry_the_raw_blocks(self):
        body = anthropic_api.to_completion(_message([
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "tool_use", "id": "c1", "name": "search", "input": {"q": "東京駅"}},
        ], stop="tool_use"))
        reply = body["choices"][0]["message"]
        assert reply["tool_calls"][0]["function"] == {"name": "search", "arguments": '{"q": "東京駅"}'}
        assert body["choices"][0]["finish_reason"] == "tool_calls"
        assert [b["type"] for b in reply[anthropic_api.RAW_KEY]] == ["thinking", "tool_use"]
        assert reply[anthropic_api.RAW_KEY][0]["signature"] == "sig"


def _client_with(create=None, models=None) -> httpx.AsyncClient:
    transport = anthropic_api.Transport("sk-test", 30.0)
    if create is not None:
        transport._client.messages.create = create
        transport._client.beta.messages.create = create
    if models is not None:
        transport._client.models.list = models
    return httpx.AsyncClient(transport=transport, base_url=anthropic_api.API_URL)


class TestTransport:
    def test_chat_completions_round_trip(self):
        async def run():
            create = AsyncMock(return_value=_message([{"type": "text", "text": "2"}]))
            async with _client_with(create=create) as client:
                res = await client.post("/v1/chat/completions", json={
                    "model": "claude-opus-5", "messages": [{"role": "user", "content": "1+1は?"}],
                })
            assert res.status_code == 200
            assert res.json()["choices"][0]["message"]["content"] == "2"
            assert create.await_args.kwargs["model"] == "claude-opus-5"
        asyncio.run(run())

    def test_refusal_is_a_failure_not_an_empty_answer(self):
        async def run():
            create = AsyncMock(return_value=_message(
                [], stop="refusal", stop_details={"type": "refusal", "category": "cyber", "explanation": None},
            ))
            async with _client_with(create=create) as client:
                res = await client.post("/v1/chat/completions", json={
                    "model": "claude-opus-5", "messages": [{"role": "user", "content": "x"}],
                })
            assert res.status_code == 422
            assert "cyber" in res.json()["error"]["message"]
        asyncio.run(run())

    def test_api_errors_keep_their_status(self):
        async def run():
            request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
            error = anthropic.AuthenticationError(
                "invalid x-api-key",
                response=httpx.Response(401, request=request),
                body={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}},
            )
            async with _client_with(create=AsyncMock(side_effect=error)) as client:
                res = await client.post("/v1/chat/completions", json={
                    "model": "claude-opus-5", "messages": [{"role": "user", "content": "x"}],
                })
            assert res.status_code == 401
            assert "invalid x-api-key" in json.dumps(res.json())
        asyncio.run(run())

    def test_models_are_listed_with_the_efforts_each_one_takes(self):
        async def run():
            def _model(id, levels):
                effort = {"supported": bool(levels),
                          **{lv: {"supported": lv in levels} for lv in anthropic_api.EFFORTS}}
                return SimpleNamespace(id=id, capabilities=SimpleNamespace(
                    effort=SimpleNamespace(supported=effort["supported"], **{
                        lv: SimpleNamespace(**effort[lv]) for lv in anthropic_api.EFFORTS
                    }),
                ))

            async def _pages(**_):
                yield _model("claude-opus-5", ("low", "max"))
                yield _model("claude-haiku-4-5", ())

            async with _client_with(models=lambda **kw: _pages(**kw)) as client:
                res = await client.get("/v1/models")
            assert [m["id"] for m in res.json()["data"]] == [
                "claude-opus-5", "claude-opus-5-low", "claude-opus-5-max", "claude-haiku-4-5",
            ]
        asyncio.run(run())


class TestThroughTheAnswerLayer:
    """会話の層(`answer.complete_message`)から Claude API の相手まで通る。

    呼ぶ側は相手が Claude API かどうかを知らない —— `_llm_client` が変換を挟むだけで、
    使用量の控え(入力・出力・キャッシュ)も他の相手と同じ道で残る。
    """

    def test_complete_message_reaches_claude_and_records_usage(self, monkeypatch, tmp_path):
        from app import answer, settings_store, usage_store

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        create = AsyncMock(return_value=_message([{"type": "text", "text": "はい"}]))
        real = anthropic_api.Transport

        def _transport(api_key, timeout):
            transport = real(api_key, timeout)
            transport._client.messages.create = create
            transport._client.beta.messages.create = create
            return transport

        monkeypatch.setattr(anthropic_api, "Transport", _transport)
        settings_store.set_credential("anthropic", "sk-test")
        settings_store.set_enabled("anthropic", True)
        cfg = answer.require_settings("anthropic")

        message = asyncio.run(answer.complete_message(cfg, [{"role": "user", "content": "やあ"}]))

        assert message["content"] == "はい"
        # 選ばなかったときは控えの先頭(既定のモデル)で頼む
        assert create.await_args.kwargs["model"] == "claude-opus-5"
        assert cfg.ran_model == "claude-opus-5"
        row = usage_store.recent_calls()[0]
        # 入力はキャッシュの読み書きのぶんも足した全体(10 + 100 + 20)
        assert (row["backend"], row["input_tokens"], row["output_tokens"]) == ("anthropic", 130, 5)
