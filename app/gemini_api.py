"""Gemini API の考える量を、モデルの名前に畳んで選ばせる(Claude API・codex と同じ形)。

Gemini の OpenAI 互換の口(`…/v1beta/openai`)は `reasoning_effort` を受け付けるが、
**受け付ける段はモデルごとに違う**(`gemini-3.8-flash` は low / medium / high、
`gemini-3.5-flash-lite` は minimal も受ける)。Chiezo は考える量の欄を出さず、
一覧に `gemini-3.8-flash-high` のような畳んだ名前を並べて選ばせる
(`app/providers.py` の `folds_effort`)。ここは HTTP の出入口(httpx のトランスポート)で:

- `GET /models` の答えに、段を受け付けるモデルの畳んだ名前を足す(`LEVELS` にあるモデルだけ。
  知らないモデルに畳んだ名前を並べると、通らない組み合わせを作れてしまう)
- `POST /chat/completions` の `model` が畳んだ名前なら、素の名前と `reasoning_effort` に戻す

段の表は Google の文書(ai.google.dev/gemini-api/docs/thinking の「Controlling thinking」。
2026-10 時点)の写し。表に無いモデルは素の名前だけが並ぶ(考える量は相手の既定)。
"""
from __future__ import annotations

import json

import httpx

# モデル → 受け付ける段(軽い順)。Google の文書の表の写し
LEVELS: dict[str, tuple[str, ...]] = {
    "gemini-4-argon": ("low", "medium", "high"),
    "gemini-3.8-flash": ("low", "medium", "high"),
    "gemini-3.6-flash": ("minimal", "low", "medium", "high"),
    "gemini-3.5-flash-lite": ("minimal", "low", "medium", "high"),
    "gemini-3.1-pro-preview": ("low", "medium", "high"),
    "gemini-3-flash-preview": ("minimal", "low", "medium", "high"),
    "gemini-3-pro-preview": ("low", "high"),
    "gemini-2.5-pro": ("low", "medium", "high"),
    "gemini-2.5-flash": ("low", "medium", "high"),
    "gemini-2.5-flash-lite": ("low", "medium", "high"),
}
# 段の名前の全部(`app/providers.py` の Gemini の `efforts` もこれ)
EFFORTS = ("minimal", "low", "medium", "high")


def split(model: str) -> tuple[str, str]:
    """畳んだ名前を `(素の名前, 段)` に分ける。畳んでいなければ `(model, "")`。

    `models/` の付いた名前も受ける(Gemini の一覧はそれを付けて返す)。
    """
    prefix = "models/" if model.startswith("models/") else ""
    bare = model.removeprefix("models/")
    head, sep, tail = bare.rpartition("-")
    if sep and tail in LEVELS.get(head, ()):
        return prefix + head, tail
    return model, ""


def folded(model_id: str) -> list[str]:
    """そのモデルの畳んだ名前(受け付ける段のぶん)。段を持たないモデルは空。"""
    prefix = "models/" if model_id.startswith("models/") else ""
    bare = model_id.removeprefix("models/")
    return [f"{prefix}{bare}-{level}" for level in LEVELS.get(bare, ())]


class Transport(httpx.AsyncBaseTransport):
    """畳んだ名前を Gemini が分かる形に直して取り次ぐ。それ以外はそのまま通す。"""

    def __init__(self, inner: httpx.AsyncBaseTransport | None = None) -> None:
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/chat/completions"):
            request = _unfolded(request)
        response = await self._inner.handle_async_request(request)
        if request.method == "GET" and path.endswith("/models") and response.status_code == 200:
            return await _with_folded(response, request)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()


def _unfolded(request: httpx.Request) -> httpx.Request:
    try:
        body = json.loads(request.content or b"{}")
    except ValueError:
        return request
    model, level = split(str(body.get("model") or ""))
    if not level:
        return request
    body["model"] = model
    body["reasoning_effort"] = level
    headers = {k: v for k, v in request.headers.items() if k.lower() != "content-length"}
    return httpx.Request(request.method, request.url, headers=headers, content=json.dumps(body).encode())


async def _with_folded(response: httpx.Response, request: httpx.Request) -> httpx.Response:
    await response.aread()
    try:
        data = response.json()
    except ValueError:
        return httpx.Response(response.status_code, headers=response.headers, content=response.content,
                              request=request)
    entries = []
    for entry in data.get("data") or []:
        entries.append(entry)
        model_id = str(entry.get("id") or "") if isinstance(entry, dict) else ""
        entries.extend({**entry, "id": name} for name in folded(model_id))
    data["data"] = entries
    return httpx.Response(200, json=data, request=request)
