"""会話ブリッジ — CLI を起動したまま持ち、続きものの会話を受け持つ。

`cli_bridge.py` は「1 回聞いたら答えが返る」口で、要求のたびに CLI を起こして捨てる。
これは調べものには向くが、キャラクターと話すような**往復の多い会話**には 2 つ合わない。

- 遅い。起動とキャラ設定の読み込みを毎回やり直すので、短い返事にも数秒〜十数秒かかる
- 文脈が切れる。前のやり取りを毎回プロンプトに積み直すことになり、長くなるほど重い

そこで CLI を起動したままにして、会話ごとに CLI 側のスレッドを 1 本ずつ持つ。
キャラ設定は会話を始めるときに 1 回だけ渡し、あとは新しい発言だけを送る。

別のコンテナ・別のサインインにしてある理由:

- `cli_bridge.py` は CLI を 1 本ずつしか動かさない(`cli_slot`。認証情報が回る相手では、
  2 本同時に更新をかけると権限ごと失効する)。会話をここに同居させると、話している間ずっと
  枠を握るか、1 往復ごとに他の依頼の後ろへ並ぶことになる
- 認証情報も分ける。同じ auth.json を 2 つのプロセスが回すと上と同じ失効が起きる。
  このブリッジは CLI のプロセスを 1 つしか持たない(会話はその中のスレッドとして持つ)ので、
  自分のサインインを持てば、回転はそのプロセスの中で閉じる。サインインはコンテナの中で 1 回:
      docker compose exec chiezo-talk-codex codex login --device-auth

受け持てる CLI は今のところ codex だけ(`codex app-server`。1 つのプロセスが複数の
スレッドを持て、1 回ごとに返事の形を JSON スキーマで縛れる)。
CLI の差は `TalkBackend` の裏に閉じてあり、足すときはそこを実装する。

口:
    POST   /v1/talk/threads               会話を始める。{instructions, model?, effort?}
    POST   /v1/talk/threads/{id}/turns    1 回ぶん話す。{text, schema?, effort?}
    DELETE /v1/talk/threads/{id}          会話を終える
    GET    /health                        生きているか・サインイン済みか
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from itertools import count
from typing import Any, Protocol

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

log = logging.getLogger("chiezo.talk")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CLI = os.environ.get("CHIEZO_TALK_CLI", "codex").strip().lower()
# Chiezo の MCP。会話の途中で知識ベースを引かせる。空なら繋がない。
# 末尾のスラッシュは必須(cli_bridge.py の MCP_URL と同じ理由)。
MCP_URL = os.environ.get("CHIEZO_TALK_MCP_URL", "http://chiezo-app:7010/mcp/knowledge/").strip()
if MCP_URL and not MCP_URL.endswith("/"):
    MCP_URL += "/"
# 既定のモデルと考える量。会話は速さが命なので、考える量は既定で軽くする
MODEL = os.environ.get("CHIEZO_TALK_MODEL", "").strip()
EFFORT = os.environ.get("CHIEZO_TALK_EFFORT", "low").strip()
# 1 回の上限秒数。知識ベースを何度も引くと 30 秒ほどかかる(実測)ので余裕を持たせる
TURN_TIMEOUT = float(os.environ.get("CHIEZO_TALK_TURN_TIMEOUT", "180") or 180)
# 話しかけられないまま、この秒数が経った会話は片付ける。呼ぶ側が終えずに去っても、
# スレッドが CLI のメモリに溜まり続けないように
IDLE_SEC = float(os.environ.get("CHIEZO_TALK_IDLE_SEC", "3600") or 3600)
# 同時に持つ会話の上限。超えたら、いちばん長く話していないものから片付ける
MAX_THREADS = int(os.environ.get("CHIEZO_TALK_MAX_THREADS", "16") or 16)
# CLI の作業場所。会話にファイルは要らないので、空の場所で動かす
WORKDIR = os.environ.get("CHIEZO_TALK_WORKDIR", "/tmp")


class TalkError(Exception):
    """CLI が断った・落ちた。`status` は呼ぶ側へ返す HTTP の状態。"""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass
class TurnResult:
    content: str
    model: str = ""
    # 知識ベースなどの道具を引いた回(名前と引数の頭)。返事が遅れた理由を見えるようにする
    tools: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


class TalkBackend(Protocol):
    """CLI ごとの差を閉じ込める。スレッドは CLI 側の id で指す。"""

    async def start_thread(self, instructions: str, model: str, web: bool) -> tuple[str, str]:
        """(スレッドの id, 実際のモデル)を返す。`web` は CLI 自身の web 検索を開けるか。"""
        ...

    async def run_turn(self, thread_id: str, text: str, schema: dict | None, effort: str) -> TurnResult: ...

    async def end_thread(self, thread_id: str) -> None: ...

    async def check(self) -> tuple[bool, str]:
        """サインイン済みか。(使えるか, 理由)"""
        ...

    async def close(self) -> None: ...


class CodexAppServer:
    """`codex app-server`(標準入出力の JSON-RPC)を 1 つ起動したまま使う。

    やり取りは initialize → thread/start → turn/start の順。turn/start の応答は
    「受け付けた」だけで、中身は通知(item/completed, turn/completed)で後から届く。
    スレッドは 1 本ずつ別の turn を走らせてよい(呼ぶ側の 1 会話は `Thread.lock` で 1 本にする)。
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._ids = count(1)
        self._pending: dict[int, asyncio.Future] = {}
        # スレッドごとの通知の行き先(走っている turn が受け取る)
        self._listeners: dict[str, asyncio.Queue] = {}
        self._start_lock = asyncio.Lock()
        # CLI が落ちたら前のスレッドはすべて消える。番号で見分けて、古いものを断る
        self.generation = 0

    def _command(self) -> list[str]:
        cmd = ["codex", "app-server"]
        if MCP_URL:
            # 設定ファイルを書き換えず、起動の引数で繋ぐ。道具を引くたびの確認は外す
            # (`approval_policy="never"` は「聞かない」であって「許す」ではないので、
            # これが無いと道具の呼び出しが断られる。entrypoint.sh の codex の節と同じ)
            cmd += [
                "-c", f'mcp_servers.chiezo.url="{MCP_URL}"',
                "-c", 'mcp_servers.chiezo.default_tools_approval_mode="auto"',
            ]
        return cmd

    async def _ensure(self) -> None:
        async with self._start_lock:
            if self._proc is not None and self._proc.returncode is None:
                return
            self.generation += 1
            self._proc = await asyncio.create_subprocess_exec(
                *self._command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                cwd=WORKDIR,
                # 1 行が長い(道具の結果がそのまま載る)ので、読み取りの上限を広げる
                limit=16 * 1024 * 1024,
            )
            self._reader = asyncio.create_task(self._read_loop(self._proc))
            await self._request("initialize", {"clientInfo": {"name": "chiezo-talk", "version": "1"}})
            log.info("codex app-server を起動した(pid=%s, 世代 %d)", self._proc.pid, self.generation)

    async def _read_loop(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while line := await proc.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._dispatch(message)
        finally:
            # 落ちた。待っている人には全員、落ちたと伝える
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(TalkError(502, "codex app-server が終了しました"))
            self._pending.clear()
            for queue in self._listeners.values():
                queue.put_nowait({"method": "_exited"})
            log.warning("codex app-server が終了した(code=%s)", proc.returncode)

    async def _dispatch(self, message: dict) -> None:
        if "method" in message and "id" in message:
            # 向こうからの問い合わせ(承認など)。会話では何も許さない
            await self._send({"jsonrpc": "2.0", "id": message["id"],
                              "error": {"code": -32601, "message": "not supported"}})
            return
        if "id" in message:
            future = self._pending.pop(message["id"], None)
            if future is not None and not future.done():
                if "error" in message:
                    error = message["error"] or {}
                    future.set_exception(TalkError(502, str(error.get("message") or error)))
                else:
                    future.set_result(message.get("result") or {})
            return
        params = message.get("params") or {}
        thread_id = params.get("threadId")
        if thread_id and thread_id in self._listeners:
            self._listeners[thread_id].put_nowait(message)

    async def _send(self, message: dict) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
        await self._proc.stdin.drain()

    async def _request(self, method: str, params: dict, timeout: float = 60) -> dict:
        request_id = next(self._ids)
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        await self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)

    async def start_thread(self, instructions: str, model: str, web: bool) -> tuple[str, str]:
        await self._ensure()
        params: dict[str, Any] = {
            "developerInstructions": instructions,
            # 会話の記録をディスクに残さない(キャラとの雑談を溜め込む理由が無い)
            "ephemeral": True,
            "approvalPolicy": "never",
            # 書き込みもシェルも要らない。知識ベースは MCP で引く
            "sandbox": "read-only",
            "cwd": WORKDIR,
            # **web 検索は頼まれたときだけ開ける。** codex は既定で開いていて、
            # 道具の一覧に出ないまま外を調べて答える(実測: 知識ベースを 1 度も
            # 引かずに公式サイトを出典に挙げた)。会話では 1 回が十数秒延びるうえ、
            # 呼ぶ側は「Chiezo の知識で答えた」つもりで受け取る
            "config": {"web_search": "live" if web else "disabled"},
        }
        if model:
            params["model"] = model
        result = await self._request("thread/start", params)
        thread_id = (result.get("thread") or {}).get("id")
        if not thread_id:
            raise TalkError(502, f"thread/start がスレッドを返さなかった: {result}")
        return thread_id, str(result.get("model") or model)

    async def run_turn(self, thread_id: str, text: str, schema: dict | None, effort: str) -> TurnResult:
        await self._ensure()
        queue: asyncio.Queue = asyncio.Queue()
        self._listeners[thread_id] = queue
        try:
            params: dict[str, Any] = {"threadId": thread_id, "input": [{"type": "text", "text": text}]}
            if schema:
                params["outputSchema"] = schema
            if effort:
                params["effort"] = effort
            try:
                turn = await self._request("turn/start", params)
            except TalkError as e:
                # 落ちて作り直した後の古いスレッドなど。CLI がもう知らない
                if "not found" in e.message.lower():
                    raise TalkError(404, "この会話は CLI の側で失われました。始め直してください") from e
                raise
            turn_id = (turn.get("turn") or {}).get("id", "")
            try:
                return await asyncio.wait_for(self._collect(queue), TURN_TIMEOUT)
            except TimeoutError:
                with suppress(Exception):
                    await self._request("turn/interrupt", {"threadId": thread_id, "turnId": turn_id}, timeout=5)
                raise TalkError(504, f"{TURN_TIMEOUT:.0f} 秒で返事が来なかった") from None
        finally:
            self._listeners.pop(thread_id, None)

    @staticmethod
    async def _collect(queue: asyncio.Queue) -> TurnResult:
        result = TurnResult(content="")
        last_error = ""
        while True:
            message = await queue.get()
            method = message.get("method", "")
            params = message.get("params") or {}
            if method == "_exited":
                raise TalkError(502, "返事の途中で codex app-server が終了しました")
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") not in ("agentMessage", "userMessage", "reasoning"):
                    log.info("turn item: %s %s", item.get("type"), item.get("tool") or item.get("command") or "")
                if item.get("type") == "agentMessage":
                    result.content = item.get("text", "")
                elif item.get("type") == "mcpToolCall":
                    result.tools.append({
                        "tool": item.get("tool", ""),
                        "arguments": json.dumps(item.get("arguments"), ensure_ascii=False)[:200],
                    })
                elif item.get("type") == "webSearch":
                    # web を開けた会話だけ。どこを調べたかも道具の 1 回として見せる
                    result.tools.append({"tool": "web_search", "arguments": str(item.get("query", ""))[:200]})
            elif method == "thread/tokenUsage/updated":
                last = (params.get("tokenUsage") or {}).get("last") or {}
                result.usage = {
                    "input_tokens": last.get("inputTokens"),
                    "output_tokens": last.get("outputTokens"),
                    "cached_tokens": last.get("cachedInputTokens"),
                }
            elif method == "error":
                error = params.get("error") or {}
                last_error = str(error.get("message") or error)
                if not params.get("willRetry"):
                    info = error.get("codexErrorInfo")
                    if info == "unauthorized":
                        raise TalkError(401, f"codex のサインインが切れています: {last_error}")
                    if info == "usageLimitExceeded":
                        raise TalkError(429, f"codex の枠を使い切りました: {last_error}")
            elif method == "turn/completed":
                turn = params.get("turn") or {}
                status = turn.get("status")
                if status == "completed":
                    return result
                reason = ((turn.get("error") or {}).get("message")) or last_error or str(status)
                raise TalkError(502, f"codex が返事を書き終えられなかった: {reason}")

    async def end_thread(self, thread_id: str) -> None:
        if self._proc is None or self._proc.returncode is not None:
            return
        with suppress(Exception):
            await self._request("thread/unsubscribe", {"threadId": thread_id}, timeout=5)

    async def check(self) -> tuple[bool, str]:
        try:
            await self._ensure()
            result = await self._request("account/read", {"refreshToken": False}, timeout=10)
        except (TalkError, OSError, TimeoutError) as e:
            return False, str(e)
        if not result.get("account"):
            return False, "codex にサインインしていません(コンテナの中で codex login --device-auth)"
        return True, ""

    async def close(self) -> None:
        if self._proc is not None and self._proc.returncode is None:
            self._proc.terminate()
            with suppress(Exception):
                await asyncio.wait_for(self._proc.wait(), 5)


@dataclass
class Thread:
    backend_id: str
    generation: int
    model: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_used: float = field(default_factory=time.monotonic)
    turns: int = 0


def make_backend(cli: str) -> TalkBackend:
    if cli == "codex":
        return CodexAppServer()
    raise SystemExit(f"未対応の CHIEZO_TALK_CLI: {cli}(codex)")


backend: TalkBackend = make_backend(CLI)
threads: dict[str, Thread] = {}


async def _forget(thread_id: str) -> None:
    thread = threads.pop(thread_id, None)
    if thread is not None:
        await backend.end_thread(thread.backend_id)


async def _sweep() -> None:
    """長く話していない会話と、上限を超えたぶんを片付ける。"""
    now = time.monotonic()
    for thread_id, thread in list(threads.items()):
        if now - thread.last_used > IDLE_SEC and not thread.lock.locked():
            log.info("話しかけられない会話を片付けた: %s", thread_id)
            await _forget(thread_id)
    idle = sorted((t.last_used, i) for i, t in threads.items() if not t.lock.locked())
    while len(threads) >= MAX_THREADS and idle:
        await _forget(idle.pop(0)[1])


@asynccontextmanager
async def lifespan(_: FastAPI):
    yield
    await backend.close()


app = FastAPI(title="chiezo talk bridge", lifespan=lifespan)


def _fail(e: TalkError) -> HTTPException:
    return HTTPException(e.status, {"error": e.message, "cli": CLI})


class StartRequest(BaseModel):
    # キャラ設定と会話の決まりごと。CLI には「開発者の指示」として渡り、会話の間ずっと効く
    instructions: str
    model: str = ""
    # CLI 自身の web 検索を開けるか(既定は閉じる)
    web: bool = False


class TurnRequest(BaseModel):
    text: str
    # 返事の形を縛る JSON スキーマ(任意)。縛ると content はその形の JSON 文字列になる
    # (`schema` は BaseModel の名前と重なるので、受け取る名前だけを別名で当てる)
    output_schema: dict | None = Field(None, alias="schema")
    effort: str = ""


@app.get("/health")
async def health(check: bool = False) -> dict:
    body: dict[str, Any] = {"ok": True, "cli": CLI, "threads": len(threads), "mcp": bool(MCP_URL)}
    if check:
        ok, reason = await backend.check()
        body["auth"] = ok
        if reason:
            body["reason"] = reason
    return body


@app.post("/v1/talk/threads")
async def start(body: StartRequest) -> dict:
    if not body.instructions.strip():
        raise HTTPException(400, {"error": "instructions must not be empty"})
    await _sweep()
    try:
        backend_id, model = await backend.start_thread(body.instructions, body.model or MODEL, body.web)
    except TalkError as e:
        raise _fail(e) from e
    generation = getattr(backend, "generation", 0)
    threads[backend_id] = Thread(backend_id=backend_id, generation=generation, model=model)
    log.info("会話を始めた: %s(model=%s)", backend_id, model or "既定")
    return {"thread_id": backend_id, "cli": CLI, "model": model}


@app.post("/v1/talk/threads/{thread_id}/turns")
async def turn(thread_id: str, body: TurnRequest) -> dict:
    thread = threads.get(thread_id)
    if thread is None or thread.generation != getattr(backend, "generation", 0):
        threads.pop(thread_id, None)
        raise HTTPException(404, {"error": "thread not found",
                                  "reason": "会話が終わったか、CLI が再起動しました。始め直してください"})
    if not body.text.strip():
        raise HTTPException(400, {"error": "text must not be empty"})
    # 同じ会話の中では 1 回ずつ(前の返事が出る前に次を送ると、CLI 側で順序が崩れる)
    async with thread.lock:
        started = time.monotonic()
        try:
            result = await backend.run_turn(thread.backend_id, body.text, body.output_schema, body.effort or EFFORT)
        except TalkError as e:
            if e.status == 404:
                threads.pop(thread_id, None)
            raise _fail(e) from e
        finally:
            thread.last_used = time.monotonic()
        thread.turns += 1
    if not result.content:
        raise HTTPException(502, {"error": "empty response", "cli": CLI})
    return {
        "content": result.content,
        "cli": CLI,
        "model": thread.model,
        "tools": result.tools,
        "usage": result.usage,
        "ms": int((time.monotonic() - started) * 1000),
    }


@app.delete("/v1/talk/threads/{thread_id}")
async def end(thread_id: str) -> dict:
    existed = thread_id in threads
    await _forget(thread_id)
    return {"ended": existed}
