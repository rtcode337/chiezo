"""Gemini API の考える量(`app/gemini_api.py`)。モデルの名前に畳んで選ばせ、取り次ぐときに戻す。"""
from __future__ import annotations

import asyncio
import json

import httpx

from app import gemini_api, providers


def run(transport: httpx.AsyncBaseTransport, method: str, url: str, body: dict | None = None):
    async def go():
        async with httpx.AsyncClient(transport=transport) as client:
            if method == "GET":
                return await client.get(url)
            return await client.post(url, json=body)

    return asyncio.run(go())


BASE = "https://generativelanguage.googleapis.com/v1beta/openai"


def test_the_list_gets_folded_names_only_for_models_that_take_levels():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [
            {"id": "models/gemini-3.8-flash"}, {"id": "models/gemini-3.5-flash-lite"}, {"id": "models/gemma-4-31b-it"},
        ]})

    got = run(gemini_api.Transport(httpx.MockTransport(handler)), "GET", f"{BASE}/models").json()
    ids = [e["id"] for e in got["data"]]
    # 3.8 は low / medium / high、3.5 の lite は minimal も。段を持たないモデルは素の名前だけ
    assert ids == [
        "models/gemini-3.8-flash",
        "models/gemini-3.8-flash-low", "models/gemini-3.8-flash-medium", "models/gemini-3.8-flash-high",
        "models/gemini-3.5-flash-lite",
        "models/gemini-3.5-flash-lite-minimal", "models/gemini-3.5-flash-lite-low",
        "models/gemini-3.5-flash-lite-medium", "models/gemini-3.5-flash-lite-high",
        "models/gemma-4-31b-it",
    ]


def test_a_folded_name_is_sent_as_the_model_and_reasoning_effort():
    sent = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": []})

    transport = gemini_api.Transport(httpx.MockTransport(handler))
    run(transport, "POST", f"{BASE}/chat/completions", {"model": "gemini-3.8-flash-high", "messages": []})
    run(transport, "POST", f"{BASE}/chat/completions", {"model": "gemini-3.8-flash", "messages": []})
    # 受け付けない段は畳んだ名前とみなさない(そのまま送って相手に断らせる)
    run(transport, "POST", f"{BASE}/chat/completions", {"model": "gemini-3.8-flash-minimal", "messages": []})

    assert sent[0]["model"] == "gemini-3.8-flash" and sent[0]["reasoning_effort"] == "high"
    assert sent[1] == {"model": "gemini-3.8-flash", "messages": []}
    assert sent[2]["model"] == "gemini-3.8-flash-minimal" and "reasoning_effort" not in sent[2]


def test_folded_names_do_not_also_send_a_separate_effort():
    # 名前に畳んだ回は、考える量を別に送らない(2 つ組で食い違わないように)
    assert providers.drops_effort("gemini", "gemini-3.8-flash-high")
    assert not providers.drops_effort("gemini", "gemini-3.8-flash")
    assert providers.effort_in_model("gemini", "gemini-3.8-flash-high") == "high"
    assert providers.selectable_efforts("gemini") == ()
