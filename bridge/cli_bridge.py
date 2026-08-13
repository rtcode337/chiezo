"""CLI ブリッジ — Claude Code / Codex CLI を OpenAI 互換の口に見せる。

(ファイル名が `server.py` でないのは `ingest/server.py` と衝突するため。テストは
リポジトリ共通の pythonpath で api / ingest / bridge をまとめて読むので、名前は全体で一意にする。)

Chiezo の「使う」層(`app/answer.py`)が要求するのは OpenAI 互換の `/chat/completions` だけ
なので、ローカルの推論サーバでも Gemini でも同じ 1 本の口で扱える。ところが Claude Code と
Codex は **HTTP ではなく CLI** で、サブスクの枠で使うにはその CLI を通すしかない
(API キー経路は従量課金になり、定額で試すという目的から外れる)。
そこを埋めるのがこのブリッジで、受けた OpenAI 形式のリクエストを CLI の起動に変換する。

設計の要点:

- **別コンテナに置く**。Chiezo 本体(`chiezo-api`)は数百 MB で動く前提があり、CLI を
  同居させるとその前提が崩れる。推論を同居させないのと同じ理由。
- **道具は CLI 自身に引かせる**。Chiezo の MCP(`/mcp`)を CLI に繋ぐので、
  「検索して答える」の段取りはブリッジ側で組まない —— Claude Code も Codex も
  道具を自分で回すのが本業で、そこは任せたほうが上手い。Chiezo 側から見ると
  「1 回聞いたら答えが返る」ので、`rag` / `agent` の区別は関係なくなる。
- **組み込みの道具は全部切る**。ファイルの読み書きやシェルは、知識ベースに答えるのに
  要らないうえ危ない。CLI に渡すのは Chiezo の MCP だけにする。
- **認証情報はイメージに焼かない**。環境変数で受け取り、Codex の認証ファイルだけは
  起動時に書き出してコンテナと一緒に捨てる(`entrypoint.sh`)。
- **ストリーミングは 1 チャンク**。CLI の応答を待ち切ってから SSE に載せる。
  差分で流すには CLI ごとに `--output-format stream-json` / `--json` の解析が要り、
  2 つ分の解析を抱えるだけの価値がまだ無い(Chiezo 側は差分の粒度を問わない)。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

log = logging.getLogger("chiezo.bridge")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

# どの CLI を包むか。イメージは 1 つで、立ち上げるときにこれで役割を決める
# (compose では chiezo-bridge-claude / chiezo-bridge-codex の 2 サービスに分ける)。
CLI = os.environ.get("CHIEZO_BRIDGE_CLI", "claude").strip().lower()
# Chiezo の MCP の URL。CLI はここから search / doc / filter … を引く。
MCP_URL = os.environ.get("CHIEZO_BRIDGE_MCP_URL", "http://chiezo-api:7010/mcp").strip()
# CLI に渡すモデル。空なら CLI の既定(サブスクの枠を無駄に食わないよう明示するのが望ましい)。
MODEL = os.environ.get("CHIEZO_BRIDGE_MODEL", "").strip()
# 1 回の呼び出しの上限秒数。CLI は道具を何度も引くので推論サーバより長くなる。
TIMEOUT = float(os.environ.get("CHIEZO_BRIDGE_TIMEOUT", "300") or 300)
# 名乗るモデル名(`/v1/models` と応答の model に載る。Chiezo の見出しがこれを出す)。
MODEL_LABEL = os.environ.get("CHIEZO_BRIDGE_MODEL_LABEL", "").strip() or (
    MODEL or {"claude": "Claude Code", "codex": "Codex CLI"}.get(CLI, CLI)
)
# CLI に許す道具。既定は Chiezo の MCP だけ。書き込み(remember)まで止めたいときは
# ここを `mcp__chiezo__search mcp__chiezo__doc …` のように絞る。
ALLOWED_TOOLS = os.environ.get("CHIEZO_BRIDGE_ALLOWED_TOOLS", "mcp__chiezo").strip()

MCP_CONFIG_PATH = "/tmp/chiezo-mcp.json"

@asynccontextmanager
async def lifespan(_: FastAPI):
    # Claude Code に渡す MCP 設定は起動時に書く(Codex は entrypoint.sh が config へ入れる)。
    if CLI == "claude":
        _write_mcp_config()
    log.info("bridge ready: cli=%s model=%s mcp=%s", CLI, MODEL_LABEL, MCP_URL)
    yield


app = FastAPI(title="Chiezo CLI bridge", lifespan=lifespan)


class Message(BaseModel):
    role: str
    content: str | None = None


class ChatRequest(BaseModel):
    messages: list[Message]
    model: str | None = None
    stream: bool = False
    # OpenAI 互換の相手が送ってくる他のフィールドは受け取って捨てる
    # (CLI に渡せる対応物が無いため)。知らない項目で 422 にしない。
    model_config = {"extra": "allow"}


def build_prompt(messages: list[Message]) -> str:
    """OpenAI の messages を CLI に渡す 1 本のテキストにする。

    CLI に「会話」の器は無いので、役割を見出しにして 1 本に畳む。system は先頭にまとめる
    —— Chiezo は抜粋(根拠)を system に載せるので、そこが埋もれると答えが根拠から離れる。
    """
    system = [m.content.strip() for m in messages if m.role == "system" and (m.content or "").strip()]
    turns = []
    for m in messages:
        text = (m.content or "").strip()
        if not text or m.role == "system":
            continue
        label = {"user": "ユーザー", "assistant": "あなた（過去の発言）"}.get(m.role, m.role)
        turns.append(f"## {label}\n{text}")

    parts = []
    if system:
        parts.append("\n\n".join(system))
    if turns:
        parts.append("\n\n".join(turns))
    return "\n\n---\n\n".join(parts) or "(空の依頼)"


def _write_mcp_config() -> None:
    """Claude Code に渡す MCP 設定を書く(Codex は entrypoint.sh が config に入れる)。"""
    config = {"mcpServers": {"chiezo": {"type": "http", "url": MCP_URL}}}
    with open(MCP_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False)


def build_command(out_path: str) -> list[str]:
    """CLI の起動コマンドを組む。プロンプトは標準入力から渡す。

    引数ではなく標準入力にするのは、Linux の単一引数の長さ上限
    (MAX_ARG_STRLEN = 128KiB)を超えると実行前に E2BIG で落ちるため。
    Chiezo が積む抜粋は簡単にこの桁へ届く。
    """
    if CLI == "claude":
        cmd = [
            "claude", "-p",
            # 組み込みの道具(Bash・Edit・Write…)は全部切る。知識ベースに答えるのに要らず、
            # 使えると危ない。残るのは --mcp-config で渡した Chiezo の道具だけ。
            "--tools", "",
            "--mcp-config", MCP_CONFIG_PATH,
            # 手元の ~/.claude.json 等に入っている別の MCP を拾わせない
            # (コンテナは使い捨てなので普通は無いが、意図しない相手に繋がないための保険)。
            "--strict-mcp-config",
            "--allowed-tools", ALLOWED_TOOLS,
            # 非対話なので確認を出されると待ち続けて固まる。渡した道具は Chiezo のものだけ。
            "--permission-mode", "bypassPermissions",
            "--output-format", "text",
        ]
        if MODEL:
            cmd += ["--model", MODEL]
        return cmd

    if CLI == "codex":
        cmd = [
            "codex", "exec",
            # /srv は git リポジトリではないので、既定の拒否を外す
            "--skip-git-repo-check",
            # セッションをディスクに残さない(コンテナは使い捨て)
            "--ephemeral",
            # 生成されたシェルコマンドを実行させない
            "-s", "read-only",
            "-c", 'approval_policy="never"',
            # 最後の発言だけをファイルへ。標準出力には進捗も混ざるので、本文はこちらから取る
            "-o", out_path,
        ]
        if MODEL:
            cmd += ["-m", MODEL]
        cmd.append("-")  # プロンプトは標準入力から
        return cmd

    raise RuntimeError(f"未対応の CHIEZO_BRIDGE_CLI: {CLI}")


async def run_cli(prompt: str) -> str:
    """CLI を 1 回起動して本文を返す。失敗は HTTPException にする。"""
    out_path = f"/tmp/chiezo-answer-{uuid.uuid4().hex}.txt"
    cmd = build_command(out_path)
    log.info("running %s (prompt %d bytes)", cmd[0], len(prompt.encode("utf-8")))
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(prompt.encode("utf-8")), timeout=TIMEOUT
        )
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise HTTPException(504, {"error": f"{CLI} timed out after {TIMEOUT:.0f}s"}) from None

    if proc.returncode != 0:
        # 中身(プロンプト・応答)はログに出さない。出すのは終了コードと CLI の stderr の頭だけ。
        detail = stderr.decode("utf-8", "replace").strip()[:500]
        log.error("%s exited %s: %s", CLI, proc.returncode, detail)
        raise HTTPException(502, {"error": f"{CLI} failed", "exit_code": proc.returncode, "stderr": detail})

    text = ""
    if CLI == "codex":
        try:
            with open(out_path, encoding="utf-8") as f:
                text = f.read().strip()
        except OSError:
            text = ""
        finally:
            # 消せなくても答えは返す(コンテナは使い捨てで、残っても次の起動で消える)。
            with suppress(OSError):
                os.unlink(out_path)
    if not text:
        text = stdout.decode("utf-8", "replace").strip()
    if not text:
        raise HTTPException(502, {"error": f"{CLI} returned an empty answer"})
    return text


def _completion(text: str) -> dict:
    return {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MODEL_LABEL,
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
        ],
    }


async def _sse(text: str) -> AsyncIterator[str]:
    """SSE で返す。差分は 1 つだけ(CLI を待ち切ってから流すため)。

    受け手(app/answer.py)は差分を順に足すだけなので、粒度は問われない。
    """
    head = {
        "id": f"chatcmpl-{uuid.uuid4().hex}",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": MODEL_LABEL,
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
    }
    yield f"data: {json.dumps(head, ensure_ascii=False)}\n\n"
    tail = dict(head, choices=[{"index": 0, "delta": {}, "finish_reason": "stop"}])
    yield f"data: {json.dumps(tail, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "cli": CLI, "model": MODEL_LABEL}


@app.get("/v1/models")
async def models() -> dict:
    """Chiezo の `model_label()` がここを引いて見出しの名前を決める。"""
    return {"object": "list", "data": [{"id": MODEL_LABEL, "object": "model", "owned_by": "chiezo-bridge"}]}


@app.post("/v1/chat/completions")
async def chat_completions(body: ChatRequest):
    if not body.messages:
        raise HTTPException(400, {"error": "messages must not be empty"})
    text = await run_cli(build_prompt(body.messages))
    if body.stream:
        return StreamingResponse(
            _sse(text),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return JSONResponse(_completion(text))
