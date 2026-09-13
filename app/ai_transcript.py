"""AI へ渡したものと返ってきたものの控え(`state/ai_transcripts.db` と `state/ai-transcripts/`)。

なぜ要るか
----------
**CLI ブリッジ越しの相手にはシェルを渡している**(`--dangerously-skip-permissions` /
`danger-full-access`)。絵を描く内蔵ツールを使わせるために必要な権限だが、同じ権限で
何でもできる —— 実際、頼んでいない生成を `curl` や Python で立てられた。
**何をしたのかを後から読めないと、気づきようがない。**

`app/ai_log.py` は「中身は残さない」を決めごとにしている(依頼文には呼んだ側の材料が
そのまま入るため)。**そちらは変えない。** ここは目的が違う別の控えで、
`CHIEZO_AI_TRANSCRIPT_DAYS=0` で丸ごと止められる。

決めごと
--------
- **3 つ残す**: 渡した依頼文・相手の生の出力(CLI の stdout/stderr)・返ってきた応答。
  途中経過を落とすと「何をしたか」が読めなくなるので、ここがいちばん効く
- **一覧は軽く、全文はファイル。** 先頭 `HEAD_MAX` 字だけ列に入れ、全文は日付ごとの
  ディレクトリへ置く。DB に何十 KB も積むと、一覧を引くたびに運ぶことになる
- **溜め続けない**(`KEEP_DAYS`)。行もファイルも同じ日数で捨てる
- 記録に失敗しても呼び出しは壊さない。控えが取れないことと、AI が答えられないことは別
- WAL は使わない(`/state` は CLI ブリッジが読み取り専用でマウントする。`ai_log` と同じ)
"""
from __future__ import annotations

import logging
import os
import shutil
import sqlite3
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import settings_store

log = logging.getLogger("chiezo.ai_transcript")

# 何日で捨てるか。**0 で丸ごと止まる**(記録しないし、画面にも出ない)。
KEEP_DAYS = int(os.environ.get("CHIEZO_AI_TRANSCRIPT_DAYS", "14") or 0)

# 列に入れる長さ。一覧はこれだけを運ぶ。
HEAD_MAX = 2000

SCHEMA = """
CREATE TABLE IF NOT EXISTS transcripts (
    id           TEXT    PRIMARY KEY,
    at           TEXT    NOT NULL,
    backend      TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    kind         TEXT    NOT NULL,
    caller       TEXT    NOT NULL,
    ok           INTEGER NOT NULL,
    -- 先頭だけ。全文は同じ id のファイルにある
    prompt       TEXT    NOT NULL DEFAULT '',
    trace        TEXT    NOT NULL DEFAULT '',
    reply        TEXT    NOT NULL DEFAULT '',
    -- 元の大きさ。切り詰めた先頭と違い、これは元のままの数
    prompt_bytes INTEGER NOT NULL DEFAULT 0,
    reply_bytes  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_transcripts_at ON transcripts(at DESC);
"""


def is_enabled() -> bool:
    return KEEP_DAYS > 0 and settings_store.state_dir() is not None


def _db_path() -> Path | None:
    base = settings_store.state_dir()
    return None if base is None else base / "ai_transcripts.db"


def _text_dir() -> Path | None:
    base = settings_store.state_dir()
    return None if base is None else base / "ai-transcripts"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> datetime:
    return datetime.now(UTC)


def _head(text: str) -> str:
    return (text or "")[:HEAD_MAX]


def record(
    *,
    backend: str,
    model: str = "",
    kind: str = "chat",
    caller: str = "",
    ok: bool = True,
    prompt: str = "",
    trace: str = "",
    reply: str = "",
) -> str:
    """1 件残して id を返す。**失敗しても例外にしない**(呼び出しを止めないため)。"""
    if not is_enabled() or not backend:
        return ""
    ident = uuid.uuid4().hex
    now = _now()
    try:
        _write_full(ident, now, backend, model, kind, caller, prompt, trace, reply)
        with _connect() as conn:
            conn.execute(
                "INSERT INTO transcripts (id, at, backend, model, kind, caller, ok,"
                " prompt, trace, reply, prompt_bytes, reply_bytes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (ident, now.isoformat(timespec="seconds"), backend, model or "", kind,
                 caller or "", 1 if ok else 0,
                 _head(prompt), _head(trace), _head(reply),
                 len((prompt or "").encode()), len((reply or "").encode())),
            )
        prune()
    except (OSError, sqlite3.Error) as e:
        log.warning("could not record transcript: %s", e)
        return ""
    return ident


def _write_full(
    ident: str, at: datetime, backend: str, model: str, kind: str, caller: str,
    prompt: str, trace: str, reply: str,
) -> None:
    """全文を 1 ファイルに。**節に分けて置く** —— 3 つを別ファイルにすると、
    1 件を読むのに 3 回開くことになる(掃除する数も 3 倍になる)。"""
    root = _text_dir()
    if root is None:
        return
    day = root / at.strftime("%Y%m%d")
    day.mkdir(parents=True, exist_ok=True)
    (day / f"{ident}.txt").write_text(
        f"# {at.isoformat(timespec='seconds')} / {backend} / {model or '-'}"
        f" / {kind} / {caller or '-'}\n\n"
        f"## 依頼文\n\n{prompt}\n\n"
        f"## 相手の出力（途中経過）\n\n{trace}\n\n"
        f"## 応答\n\n{reply}\n",
        encoding="utf-8",
    )


def full_text(ident: str) -> str | None:
    """全文。掃除で消えていれば None。

    **行が無ければ控えも無い、として扱う。** ファイルは日付ごとにまとめて捨てるので
    行より少し長く残るが、読む側から見れば「消えたもの」が 2 通りあるのは事故のもと。
    """
    root = _text_dir()
    if root is None or not ident or get(ident) is None:
        return None
    for day in sorted(root.iterdir(), reverse=True) if root.exists() else []:
        path = day / f"{ident}.txt"
        if path.is_file():
            with suppress(OSError):
                return path.read_text(encoding="utf-8")
    return None


def recent(limit: int = 50, offset: int = 0) -> list[dict]:
    """新しい順に。**一覧が運ぶのは先頭だけ**(全文は開いたときに読む)。"""
    if not is_enabled():
        return []
    with suppress(sqlite3.Error):
        with _connect() as conn:
            rows = conn.execute(
                "SELECT * FROM transcripts ORDER BY at DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
        return [dict(r) for r in rows]
    return []


def get(ident: str) -> dict | None:
    if not is_enabled() or not ident:
        return None
    with suppress(sqlite3.Error):
        with _connect() as conn:
            row = conn.execute(
                "SELECT * FROM transcripts WHERE id = ?", (ident,)
            ).fetchone()
        return dict(row) if row else None
    return None


def prune(keep_days: int | None = None) -> None:
    """古い行とファイルを捨てる。**どちらも同じ日数**で揃える ——
    片方だけ残ると、一覧に出るのに開けない行ができる。"""
    days = KEEP_DAYS if keep_days is None else keep_days
    if days < 0:
        return
    limit = _now() - timedelta(days=days)
    with suppress(sqlite3.Error), _connect() as conn:
        # **同じ秒に入った行も落とす。** `at` は秒までなので、`<` だと
        # 「0 日ぶん残す」(= 全部捨てる)が効かない
        conn.execute(
            "DELETE FROM transcripts WHERE at <= ?",
            (limit.isoformat(timespec="seconds"),),
        )
    root = _text_dir()
    if root is None or not root.exists():
        return
    for day in root.iterdir():
        if not day.is_dir() or len(day.name) != 8 or not day.name.isdigit():
            continue
        with suppress(ValueError, OSError):
            when = datetime.strptime(day.name, "%Y%m%d").replace(tzinfo=UTC)
            if when <= limit - timedelta(days=1):
                shutil.rmtree(day)
