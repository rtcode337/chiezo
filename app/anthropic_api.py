"""Claude API(Anthropic の Messages API)を、OpenAI 互換の口に見せる。

Chiezo の会話の層(`app/answer.py`・`app/agent.py`)は、どの相手も OpenAI 互換の
`/chat/completions` と `/models` で話す作りになっている。Claude API はその形を
持たないので、**HTTP の出入口(httpx のトランスポート)で形を相互に変換する**。
呼ぶ側は相手が Claude API かどうかを知らずに済み、往復の数え方・止め方・
引き直し・使用量の控えも、ほかの相手と同じ経路を通る。

Anthropic も OpenAI 互換の口を出しているが、あちらは試すためのもので、
考える量(effort)や拒否されたときの切り替え(fallbacks)が使えない。
ここでは公式の SDK で Messages API を直に呼ぶ。

変換の要点:

- `system` の発言は Messages API の `system` にまとめる(会話の中に置けない)
- 道具の呼び出し(`tool_calls`)は `tool_use`、道具の結果(`tool`)は `tool_result` にする。
  続けて来た結果は 1 つの user の発言にまとめる(分けると並列の呼び出しが崩れる)
- 道具を呼んだ回の応答には、**Claude が返したブロックをそのまま `RAW_KEY` に添える**。
  agent はその発言を丸ごと積み直すので、次の往復で考えた中身(thinking)の署名ごと
  送り返せる —— 文字と道具の呼び出しだけに崩すと、考えた中身が落ちる
- `temperature` / `top_p` は送らない。Claude Opus 5 以降は受け付けず 400 になる
- 考える量は `output_config.effort` にする。**選ばせ方は他の相手と同じくモデルの名前に
  畳む**(`claude-opus-5-high`。`app/providers.py` の `folds_effort`)—— 一覧(`/models`)が
  モデルごとに受け付ける段を名乗るので、通らない組み合わせは並ばない。
  素の名前で頼まれた回は `reasoning_effort` を見る。Haiku 4.5 は受け付けないので送らない
- 拒否(`stop_reason: "refusal"`)は、本文が無ければ失敗として返す。空の答えを
  「答えた」と扱うと、呼ぶ側は理由も分からず空の結果を保存する
"""
from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic
import httpx

log = logging.getLogger(__name__)

# 相手の URL(`app/providers.py` に置く値)。SDK が自分で宛先を持つので、
# こちらは「どの相手か」の印と、使用量の控えの鍵にしか使わない。
API_URL = "https://api.anthropic.com/v1"

# 応答の上限。Chiezo の答えは長くても数千字なので、考える量を足してもこれで足りる。
# SDK は逐次で返さない往復に大きすぎる値を渡すと断る(10 分を超えそうな値)ので、
# その手前に置く。
DEFAULT_MAX_TOKENS = 16000

# 道具を呼んだ回の応答に、Claude が返したブロックをそのまま添える鍵。
# **OpenAI 互換の形の外側に置く**(他の相手の応答には入らない)。
RAW_KEY = "chiezo_anthropic_content"

# 拒否されたとき、サーバー側で別のモデルに回させる(`fallbacks: "default"`)。
# 回し先は拒否の種類ごとに Anthropic が選ぶ。受け付けるモデルが決まっているので、
# それ以外のモデルには付けない(付けると 400 になる)。
FALLBACK_BETA = "server-side-fallback-2026-07-01"
FALLBACK_MODELS = ("claude-opus-5", "claude-fable-5-1")

# 考える量の段(軽い順)。モデルの名前の末尾に畳んで選ばせる(`claude-opus-5-high`)
EFFORTS = ("low", "medium", "high", "xhigh", "max")

# 考える量を受け付けないモデルの頭(送ると 400 になる)。
NO_EFFORT_PREFIXES = ("claude-haiku-",)

_FINISH = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "pause_turn": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
    "refusal": "content_filter",
}


class Transport(httpx.AsyncBaseTransport):
    """OpenAI 互換の `/models` と `/chat/completions` を、Claude API へ取り次ぐ。

    `app/answer.py` の `_llm_client` が、Claude API の相手のときだけこれを挿す。
    """

    def __init__(self, api_key: str | None, timeout: float) -> None:
        # 引き直しは SDK に任せない。混雑(429/503)の引き直しは `answer._post_with_retry`
        # が全部の相手で同じように持っているので、二重に待たせない。
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key or "", max_retries=0, timeout=timeout,
        )

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.rstrip("/")
        try:
            if request.method == "GET" and path.endswith("/models"):
                return await self._models(request)
            if request.method == "POST" and path.endswith("/chat/completions"):
                body = json.loads((await request.aread()) or b"{}")
                if body.get("stream"):
                    return await self._stream(request, body)
                return await self._complete(request, body)
        except anthropic.APIStatusError as e:
            return _error_response(e.status_code, e.message, request)
        except anthropic.APITimeoutError as e:
            raise httpx.ReadTimeout(str(e), request=request) from None
        except anthropic.APIConnectionError as e:
            raise httpx.ConnectError(str(e), request=request) from None
        return _error_response(404, f"{request.method} {path} は Claude API に取り次げません", request)

    async def aclose(self) -> None:
        await self._client.close()

    async def _models(self, request: httpx.Request) -> httpx.Response:
        ids: list[str] = []
        async for model in self._client.models.list(limit=100):
            ids.append(model.id)
            # 受け付ける段だけ畳んだ名前を並べる(段の数はモデルで違う。Haiku は持たない)
            ids.extend(f"{model.id}-{level}" for level in _supported_efforts(model))
        return httpx.Response(200, json={"data": [{"id": i} for i in ids]}, request=request)

    def _messages_api(self, kwargs: dict[str, Any]):
        """拒否の切り替えを付ける回だけ beta の口を使う(付けない回は素の口)。"""
        return self._client.beta.messages if "fallbacks" in kwargs else self._client.messages

    async def _complete(self, request: httpx.Request, body: dict) -> httpx.Response:
        kwargs = to_request(body)
        message = await self._messages_api(kwargs).create(**kwargs)
        if refused := _refusal_reason(message):
            return _error_response(422, refused, request)
        return httpx.Response(200, json=to_completion(message), request=request)

    async def _stream(self, request: httpx.Request, body: dict) -> httpx.Response:
        kwargs = to_request(body)
        manager = self._messages_api(kwargs).stream(**kwargs)
        # **流し始める前に入っておく。** 認証の失敗や混雑はここで例外になるので、
        # 状態コードを付けた応答として返せる(流し始めてからでは 200 を返した後になる)。
        stream = await manager.__aenter__()
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_EventStream(manager, stream, request),
            request=request,
        )


class _EventStream(httpx.AsyncByteStream):
    """Claude の逐次の出来事を、OpenAI 互換の SSE(`data: {...}`)に直して流す。"""

    def __init__(self, manager, stream, request: httpx.Request) -> None:
        self._manager = manager
        self._stream = stream
        self._request = request
        self._closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        try:
            model = ""
            async for event in self._stream:
                if event.type == "message_start":
                    model = event.message.model
                elif event.type == "content_block_delta" and event.delta.type == "text_delta":
                    yield _sse({"model": model, "choices": [
                        {"index": 0, "delta": {"content": event.delta.text}},
                    ]})
            final = await self._stream.get_final_message()
            if (refused := _refusal_reason(final)) and not _text_of_blocks(final.content):
                # 何も流さないまま断られた。空の答えで終わらせず、読み手に失敗として渡す
                raise httpx.ReadError(refused, request=self._request)
            yield _sse({
                "model": final.model,
                "choices": [{"index": 0, "delta": {},
                             "finish_reason": _FINISH.get(final.stop_reason or "", "stop")}],
                "usage": _usage(final.usage),
            })
            yield b"data: [DONE]\n\n"
        except anthropic.APIError as e:
            # 流している途中の失敗(混雑など)。httpx の例外に直さないと、
            # 呼ぶ側の「相手との往復の失敗」の受け口を素通りしてしまう
            raise httpx.ReadError(str(e), request=self._request) from None
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            await self._manager.__aexit__(None, None, None)


def _supported_efforts(model: Any) -> list[str]:
    """そのモデルが受け付けると名乗る考える量の段。名乗らなければ空。"""
    effort = getattr(getattr(model, "capabilities", None), "effort", None)
    if effort is None or not getattr(effort, "supported", False):
        return []
    return [
        level for level in EFFORTS
        if getattr(getattr(effort, level, None), "supported", False)
    ]


def split_model(name: str) -> tuple[str, str]:
    """畳んだ名前(`claude-opus-5-high`)を、モデルと考える量に分ける。畳んでいなければ段は空。"""
    head, sep, tail = name.rpartition("-")
    if sep and head and tail in EFFORTS:
        return head, tail
    return name, ""


# ---- OpenAI 互換 → Messages API ---------------------------------------------


def to_request(body: dict) -> dict[str, Any]:
    """OpenAI 互換の `/chat/completions` の本文を、`messages.create` の引数にする。"""
    model, folded = split_model(str(body.get("model") or ""))
    system, messages = _convert_messages(body.get("messages") or [])
    fmt = body.get("response_format")
    if isinstance(fmt, dict) and fmt.get("type") in ("json_object", "json_schema"):
        # JSON で返させたい回(クエリ生成)。型を渡されていないので、指示として伝える
        system = "\n\n".join(p for p in (system, "JSON のオブジェクトだけを出力すること。") if p)
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": int(body.get("max_tokens") or body.get("max_completion_tokens")
                          or DEFAULT_MAX_TOKENS),
        "messages": messages,
        # 前回までと同じ頭の部分をキャッシュから読ませる。agent は道具の往復のたびに
        # 会話をまるごと送り直すので、2 往復目からの入力がほぼキャッシュの読み出しになる
        "cache_control": {"type": "ephemeral"},
    }
    if system:
        kwargs["system"] = system
    if stop := body.get("stop"):
        kwargs["stop_sequences"] = [stop] if isinstance(stop, str) else list(stop)
    if tools := [t for t in (_convert_tool(t) for t in body.get("tools") or []) if t]:
        kwargs["tools"] = tools
        if body.get("tool_choice") == "none":
            kwargs["tool_choice"] = {"type": "none"}
    effort = folded or str(body.get("reasoning_effort") or "")
    if effort and not model.startswith(NO_EFFORT_PREFIXES):
        kwargs["output_config"] = {"effort": effort}
    if model in FALLBACK_MODELS:
        kwargs["betas"] = [FALLBACK_BETA]
        kwargs["fallbacks"] = "default"
    return kwargs


def _convert_messages(raw: list[dict]) -> tuple[str, list[dict]]:
    system: list[str] = []
    out: list[dict] = []
    for m in raw:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        if role in ("system", "developer"):
            if text := _text_of(m.get("content")):
                system.append(text)
            continue
        if role == "tool":
            # 結果が空だと受け付けないので、空であることを書いて渡す
            _append(out, "user", [{
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id") or ""),
                "content": _text_of(m.get("content")) or "(結果は空)",
            }])
            continue
        if role == "assistant":
            raw_blocks = m.get(RAW_KEY)
            if isinstance(raw_blocks, list) and raw_blocks:
                _append(out, "assistant", list(raw_blocks))
                continue
            blocks: list[dict] = []
            if text := _text_of(m.get("content")):
                blocks.append({"type": "text", "text": text})
            for call in m.get("tool_calls") or []:
                fn = call.get("function") or {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(call.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": _arguments(fn.get("arguments")),
                })
            if blocks:
                _append(out, "assistant", blocks)
            continue
        if blocks := _user_blocks(m.get("content")):
            _append(out, "user", blocks)
    return "\n\n".join(system), out


def _append(out: list[dict], role: str, blocks: list[dict]) -> None:
    """同じ役の発言が続いたら 1 つにまとめる(道具の結果は 1 つの発言に揃える)。"""
    if out and out[-1]["role"] == role:
        out[-1]["content"].extend(blocks)
    else:
        out.append({"role": role, "content": blocks})


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [p.get("text") or "" for p in content if isinstance(p, dict) and p.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _user_blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content.strip() else []
    blocks: list[dict] = []
    for part in content if isinstance(content, list) else []:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text" and (part.get("text") or "").strip():
            blocks.append({"type": "text", "text": part["text"]})
        elif part.get("type") == "image_url":
            ref = part.get("image_url")
            url = ref.get("url") if isinstance(ref, dict) else ref
            if isinstance(url, str) and (image := _image_block(url)):
                blocks.append(image)
    return blocks


def _image_block(url: str) -> dict | None:
    if url.startswith("data:"):
        head, _, data = url.partition(",")
        media = head.removeprefix("data:").split(";")[0]
        if not data or ";base64" not in head:
            return None
        return {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}
    if url.startswith(("http://", "https://")):
        return {"type": "image", "source": {"type": "url", "url": url}}
    return None


def _convert_tool(tool: Any) -> dict | None:
    fn = tool.get("function") if isinstance(tool, dict) else None
    if not isinstance(fn, dict) or not fn.get("name"):
        return None
    schema = fn.get("parameters") or {"type": "object", "properties": {}}
    out = {"name": fn["name"], "input_schema": schema}
    if fn.get("description"):
        out["description"] = fn["description"]
    return out


def _arguments(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ---- Messages API → OpenAI 互換 ---------------------------------------------


def to_completion(message: Any) -> dict:
    """`messages.create` の応答を、OpenAI 互換の `chat.completion` の形にする。"""
    blocks = _after_fallback(list(message.content))
    calls = [
        {
            "id": b.id,
            "type": "function",
            "function": {"name": b.name, "arguments": json.dumps(b.input, ensure_ascii=False)},
        }
        for b in blocks if b.type == "tool_use"
    ]
    reply: dict[str, Any] = {"role": "assistant", "content": _text_of_blocks(message.content)}
    if calls:
        reply["tool_calls"] = calls
        reply[RAW_KEY] = [_dump(b) for b in _echoable(list(message.content))]
    return {
        "object": "chat.completion",
        "model": message.model,
        "choices": [{
            "index": 0,
            "message": reply,
            "finish_reason": _FINISH.get(message.stop_reason or "", "stop"),
        }],
        "usage": _usage(message.usage),
    }


def _text_of_blocks(blocks: list) -> str:
    return "".join(getattr(b, "text", "") or "" for b in blocks if b.type == "text")


def _last_fallback(blocks: list) -> int:
    """最後の `fallback` ブロック(別のモデルに回った境目)の位置。無ければ -1。"""
    marks = [i for i, b in enumerate(blocks) if b.type == "fallback"]
    return marks[-1] if marks else -1


def _after_fallback(blocks: list) -> list:
    """境目より後ろ(回った先のモデルが出したもの)。道具の呼び出しはここからだけ拾う ——
    境目より前の呼び出しは断られた側のもので、実行してはいけない。"""
    return blocks[_last_fallback(blocks) + 1:]


# 境目より前にあったら送り返さないブロック(断られた側の考えた中身と道具の呼び出し)
_DROP_BEFORE_FALLBACK = {"thinking", "redacted_thinking", "tool_use"}


def _echoable(blocks: list) -> list:
    """次の往復で送り返してよいブロック。境目より前の考えた中身・道具の呼び出しを落とす。"""
    cut = _last_fallback(blocks)
    return [
        b for i, b in enumerate(blocks)
        if not (i < cut and b.type in _DROP_BEFORE_FALLBACK)
    ]


def _dump(block: Any) -> dict:
    """SDK のブロックを、送り返せる素の dict にする(考えた中身の署名も残す)。"""
    return block.to_dict(exclude_none=True) if hasattr(block, "to_dict") else dict(block)


def _usage(usage: Any) -> dict:
    """トークン数を OpenAI 互換の名前にする。**入力にはキャッシュのぶんも足す** ——
    Claude は入力をキャッシュの読み書きと残りに分けて言うが、互換の `prompt_tokens` は
    全体を指し、キャッシュから読んだぶんは内訳(`cached_tokens`)で言う。"""
    if usage is None:
        return {}
    read = getattr(usage, "cache_read_input_tokens", None) or 0
    written = getattr(usage, "cache_creation_input_tokens", None) or 0
    return {
        "prompt_tokens": (usage.input_tokens or 0) + read + written,
        "completion_tokens": usage.output_tokens or 0,
        "prompt_tokens_details": {"cached_tokens": read},
    }


def _refusal_reason(message: Any) -> str:
    """断られたなら、その理由の一文。断られていなければ空。"""
    if getattr(message, "stop_reason", None) != "refusal":
        return ""
    details = getattr(message, "stop_details", None)
    category = getattr(details, "category", None) if details else None
    return "Claude が答えるのを断りました" + (f"(種類: {category})" if category else "")


def _error_response(status: int, message: str, request: httpx.Request) -> httpx.Response:
    """失敗を OpenAI 互換のエラーの形で返す(`answer._upstream_reason` が読める形)。"""
    return httpx.Response(status, json={"error": {"message": message}}, request=request)


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode()
