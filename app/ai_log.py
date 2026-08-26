"""AI への問い合わせが失敗したときの控え（`state/ai_failures.db`）。

なぜ要るか。 CLI ブリッジ越しの相手はときどき理由を書かずに落ちる（実測: 307KB の
プロンプトで 6 秒後に `claude exited 1`、stderr も空）。呼んだ側のログには
`llm error 502 / claude failed` しか残らず、その場に居合わせないと何も分からない。
朝の定期実行のような無人の呼び出しでは、気づいたときには手がかりが消えている。

決めごと:

- 中身は残さない。 記録するのは相手・モデル・状態・理由と、プロンプトのバイト数だけ。
  プロンプトと応答には呼んだ側の材料がそのまま入る（銘柄の保有状況、家庭内の通信先…）ので、
  トラブルシュートのために持つ物ではない。大きさは残す —— 失敗が大きさに寄っているのかを
  後から見分けられるようにするため。
- `CHIEZO_STATE_DIR` が機能フラグを兼ねる（未設定なら何も記録しない）。
  設定・notes と同じ流儀。記録に失敗しても呼び出しは壊さない —— 控えが取れないことと、
  AI が答えられないことは別の話。
- `settings.db` とは別のファイルにする。 あちらは消してはいけない設定で、こちらは
  消してよい観測。混ぜると、片方を捨てたいときに両方を抱えることになる。
- 溜め続けない（`MAX_ROWS`）。読むのは「最近なにが落ちたか」だけで、
  半年前の失敗は要らない。
- WAL は使わない。 `/state` は CLI ブリッジが読み取り専用でマウントする場所で、
  WAL の読み手は -shm への書き込みを要求する（`settings_store` と同じ理由。
  このファイル自体は読まれないが、同じ約束で揃えておく）。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from app import settings_store

log = logging.getLogger("chiezo.ai_log")

# 残す件数。1 回の失敗が 1 行で、読むのは直近だけ。
MAX_ROWS = 500

# 理由の長さの上限。相手の応答をそのまま抱え込まないため。
REASON_MAX = 500

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_failures (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT    NOT NULL,
    backend      TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    effort       TEXT    NOT NULL,
    -- 相手が返した HTTP の状態。0 は「そもそも繋がらなかった」。
    status       INTEGER NOT NULL,
    reason       TEXT    NOT NULL,
    -- 送ったプロンプトの大きさ（中身は残さない）。
    prompt_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_failures_at ON ai_failures(at DESC);
"""


def db_path() -> Path | None:
    d = settings_store.state_dir()
    return d / "ai_failures.db" if d else None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    return conn


def record(
    *,
    backend: str,
    model: str,
    effort: str,
    status: int,
    reason: str,
    prompt_bytes: int,
) -> None:
    """失敗を 1 件残す。呼び出し側の失敗にはしない（控えが取れなくても答えは返す）。"""
    path = db_path()
    if path is None:
        return
    try:
        with _connect(path) as conn:
            conn.execute(
                "INSERT INTO ai_failures (at, backend, model, effort, status, reason, prompt_bytes)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    backend,
                    model,
                    effort,
                    status,
                    (reason or "")[:REASON_MAX],
                    prompt_bytes,
                ),
            )
            # 古いものから捨てる。件数で切るのは、失敗の頻度が読めないため
            # （静かな週も荒れた日もあり、日数で切ると多い日に全部消える）。
            conn.execute(
                "DELETE FROM ai_failures WHERE id <= "
                "(SELECT MAX(id) FROM ai_failures) - ?",
                (MAX_ROWS,),
            )
    except sqlite3.Error as e:
        log.warning("ai failure log write failed: %r", e)


def recent(limit: int = 50) -> list[dict]:
    """直近の失敗を新しい順に返す。記録が無ければ空。"""
    path = db_path()
    if path is None or not path.exists():
        return []
    rows: list[dict] = []
    with suppress(sqlite3.Error):
        conn = _connect(path)
        try:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT at, backend, model, effort, status, reason, prompt_bytes"
                    " FROM ai_failures ORDER BY id DESC LIMIT ?",
                    (max(1, min(limit, MAX_ROWS)),),
                )
            ]
        finally:
            conn.close()
    return rows
