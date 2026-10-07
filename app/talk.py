"""会話の口(`/v1/talk/...`)—— キャラクターと続きものの会話をする。

`/v1/ai/complete` は 1 回 1 往復で、履歴は呼ぶ側が毎回積んで送る。これはアプリが
自分のプロンプトで頼みごとをするには向くが、**キャラクターと話し続ける**のには
2 つ合わない —— 相手(CLI)を毎回起こすので 1 往復が遅く、キャラ設定と履歴を毎回
送り直すので長く話すほど重くなる。

そこで会話の間だけ、相手を起動したままにする。持つのは会話ブリッジ
(`bridge/talk_bridge.py`。CLI を起動したまま、会話ごとにスレッドを持つ)で、
**ここは状態を持たない** —— 会話の id はブリッジのスレッドの id に相手の名前を
付けただけのもので、どのワーカーが受けても同じブリッジへ流せる。

キャラ設定は短期記憶(`chiezo_memory`)に置いたものを見出しで指す。
会話を始めるときに 1 回だけ読んで相手に渡し、あとは新しい発言だけを送る。
相手は会話の途中で Chiezo の知識(MCP の読む口)を自分で引ける。

口:
    POST   /v1/talk/sessions                 会話を始める
    POST   /v1/talk/sessions/{id}/turns      1 回ぶん話す
    DELETE /v1/talk/sessions/{id}            会話を終える
    GET    /v1/talk/backends                 話せる相手(会話ブリッジ)と、動いているか
"""
from __future__ import annotations

import logging
import os
import time

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app import ai_inflight, ai_log, db, notes, usage_store

log = logging.getLogger("chiezo.talk")

router = APIRouter()

# 会話ブリッジを立てられる相手。足すときはブリッジ側(`TalkBackend`)を実装してからここに並べる
BACKENDS = ("codex",)
DEFAULT_BACKEND = "codex"
# 会話ブリッジの待ち受け(compose のサービス名 + ポート)。相手ごとに環境変数で差し替えられる
DEFAULT_PORT = 7016
# 1 往復の上限。知識を何度も引く回は 30 秒ほどかかる(実測)ので、ブリッジの上限
# (`CHIEZO_TALK_TURN_TIMEOUT`、既定 180 秒)より少し長くして、切れたときに
# ブリッジの 504(理由つき)が届くようにする
TURN_TIMEOUT = float(os.environ.get("CHIEZO_TALK_TIMEOUT", "200") or 200)

# 相手へ渡す会話の決まりごと。キャラ設定の後ろに付ける。
# **キャラの人柄はここに書かない**(それは呼ぶ側が短期記憶に置くもの)。
# ここに置くのは、Chiezo の道具の引き方 —— どのアプリから話しても同じこと
TALK_RULES = """\
# 会話の進め方
- これは続いている会話です。前のやり取りを覚えたうえで返事をしてください。
- 事実を確かめたいときは Chiezo の道具(search / doc / recall など)で調べられます。
  調べるのは、知らないと答えられないときだけにしてください。調べるのは 1 回の返事につき
  多くても 2 回までです(相手は返事を待っています)。
- 調べても分からないことは、分からないと言ってください。推測で事実を作らないでください。
- 調べていないことを「確かめた」「載っている」と言わないでください。"""


# ブリッジへの繋ぎ方。テストが偽のブリッジ(`httpx.MockTransport`)に差し替える
_transport: httpx.AsyncBaseTransport | None = None


def bridge_url(backend: str) -> str:
    """相手の会話ブリッジの URL。`CHIEZO_TALK_<相手>_URL` で差し替えられる。"""
    env = os.environ.get(f"CHIEZO_TALK_{backend.upper()}_URL", "").strip()
    return (env or f"http://chiezo-talk-{backend}:{DEFAULT_PORT}").rstrip("/")


def _require_backend(backend: str) -> str:
    name = (backend or DEFAULT_BACKEND).strip().lower()
    if name not in BACKENDS:
        raise HTTPException(400, {"error": f"unknown talk backend: {name}", "backends": list(BACKENDS)})
    return name


def _split_session(session_id: str) -> tuple[str, str]:
    backend, sep, thread_id = session_id.partition(":")
    if not sep or not thread_id:
        raise HTTPException(404, {"error": "session not found"})
    return _require_backend(backend), thread_id


def character_text(title: str) -> str:
    """短期記憶からキャラ設定を見出しで引く。無ければ 404。"""
    path = notes.require_path()
    notes.ensure_db()
    rows = db.query(path, "SELECT body FROM docs WHERE title = ?", (notes.title_key(title),))
    if not rows:
        raise HTTPException(404, {
            "error": "character not found",
            "hint": f"短期記憶(chiezo_memory)に見出し「{title}」のメモを置いてください",
        })
    return rows[0]["body"]


def build_instructions(character: str, extra: str) -> str:
    """相手に渡す指示。キャラ設定 → 会話の決まりごと → 呼ぶ側の補足 の順。"""
    parts = [character.strip(), TALK_RULES]
    if extra.strip():
        parts.append(extra.strip())
    return "\n\n".join(parts)


async def _call(method: str, url: str, timeout: float, body: dict | None = None) -> dict:
    """ブリッジへ投げる。繋がらない・断られたは、理由を付けて同じ状態で返す。"""
    try:
        async with httpx.AsyncClient(timeout=timeout, transport=_transport) as client:
            res = await client.request(method, url, json=body)
    except httpx.TimeoutException:
        raise HTTPException(504, {"error": "talk bridge timed out", "reason": "TimeoutException"}) from None
    except httpx.HTTPError as e:
        log.warning("会話ブリッジに繋がらない(%s): %r", url, e)
        raise HTTPException(502, {
            "error": "talk bridge is not reachable",
            "reason": type(e).__name__,
            "hint": "会話ブリッジ(chiezo-talk-<相手>)を立ててください。docs/ai.md の「会話の口」",
        }) from None
    try:
        payload = res.json()
    except ValueError:
        payload = {}
    if res.status_code >= 400:
        detail = payload.get("detail", payload) if isinstance(payload, dict) else {}
        raise HTTPException(res.status_code, detail if isinstance(detail, dict) else {"error": str(detail)})
    return payload if isinstance(payload, dict) else {}


class StartBody(BaseModel):
    # 短期記憶に置いたキャラ設定の見出し
    character: str = Field(min_length=1)
    # 相手(会話ブリッジ)。空なら既定
    backend: str = ""
    model: str = ""
    # キャラ設定の後ろに足す補足(場面・相手の人数など、呼ぶ側の事情)
    instructions: str = ""
    # 相手自身の web 検索を開けるか(既定は閉じる。開けると 1 回が十数秒延びる)
    web: bool = False
    # 誰が頼んだか(`/v1/ai/complete` と同じ。必須にはしない)
    requested_by: str = ""


class TurnBody(BaseModel):
    text: str = Field(min_length=1)
    # 返事の形を縛る JSON スキーマ(任意)。縛ると content はその形の JSON 文字列になる
    output_schema: dict | None = Field(None, alias="schema")
    # 考える量(相手の語彙。codex なら low / medium / high)。空ならブリッジの既定
    effort: str = ""
    requested_by: str = ""

    model_config = {"populate_by_name": True}


@router.get("/v1/talk/backends")
async def talk_backends() -> dict:
    """話せる相手と、その会話ブリッジが動いているか(サインインまでは聞かない)。"""
    found = []
    for name in BACKENDS:
        entry = {"id": name, "url": bridge_url(name), "up": False}
        try:
            health = await _call("GET", f"{bridge_url(name)}/health", 3.0)
            entry.update(up=True, threads=health.get("threads"))
        except HTTPException:
            pass
        found.append(entry)
    return {"default": DEFAULT_BACKEND, "backends": found}


@router.post("/v1/talk/sessions")
async def start_session(body: StartBody) -> dict:
    backend = _require_backend(body.backend)
    character = await run_in_threadpool(character_text, body.character)
    instructions = build_instructions(character, body.instructions)
    started = await _call(
        "POST", f"{bridge_url(backend)}/v1/talk/threads", 60.0,
        {"instructions": instructions, "model": body.model, "web": body.web},
    )
    thread_id = started.get("thread_id")
    if not thread_id:
        raise HTTPException(502, {"error": "talk bridge returned no thread"})
    return {
        "session_id": f"{backend}:{thread_id}",
        "backend": backend,
        "model": started.get("model", ""),
        "character": body.character,
        "web": body.web,
    }


@router.post("/v1/talk/sessions/{session_id}/turns")
async def talk_turn(session_id: str, body: TurnBody) -> dict:
    backend, thread_id = _split_session(session_id)
    payload: dict = {"text": body.text, "effort": body.effort}
    if body.output_schema:
        payload["schema"] = body.output_schema
    caller = ai_inflight.caller_of("api", body.requested_by)
    started = time.monotonic()
    try:
        result = await _call(
            "POST", f"{bridge_url(backend)}/v1/talk/threads/{thread_id}/turns", TURN_TIMEOUT, payload,
        )
    except HTTPException as e:
        # 会話が失われた(404)のは呼ぶ側が始め直せば済むので、失敗の控えには残さない
        if e.status_code != 404:
            detail = e.detail if isinstance(e.detail, dict) else {}
            ai_log.record(
                backend=backend, model="", effort=body.effort, status=e.status_code,
                reason=f"会話: {detail.get('error', '')}", prompt_bytes=len(body.text.encode()),
            )
        raise
    content = result.get("content", "")
    usage = result.get("usage") or {}
    # 使ったぶんは他の依頼と同じ表に残す(同じサブスクの枠を食う)
    usage_store.record(
        backend,
        model=result.get("model", ""),
        effort=body.effort,
        kind="chat",
        caller=caller,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        cached_tokens=usage.get("cached_tokens"),
        prompt_bytes=len(body.text.encode()),
        reply_bytes=len(content.encode()),
        ms=int((time.monotonic() - started) * 1000),
    )
    return {
        "content": content,
        "backend": backend,
        "model": result.get("model", ""),
        # 知識を引いた回(返事が遅れた理由が読めるように)
        "tools": result.get("tools", []),
        "ms": result.get("ms"),
    }


@router.delete("/v1/talk/sessions/{session_id}")
async def end_session(session_id: str) -> dict:
    backend, thread_id = _split_session(session_id)
    return await _call("DELETE", f"{bridge_url(backend)}/v1/talk/threads/{thread_id}", 10.0)
