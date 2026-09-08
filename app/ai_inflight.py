"""いま走っている AI への依頼の控え（`state/ai_inflight.db`）。

なぜ要るか。 「AI への依頼」の表（`app/views/ai_history.py`）に行が立つのは往復が
終わってからで、成功は `usage_store`、失敗は `ai_log` に書かれる。CLI ブリッジ越しの
相手は数十秒から数分かかるので、そのあいだ画面には何も出ない。頼んだ本人が待っている
うちは分かるが、無人で回る層（収集の時計）が動かしているぶんは、終わるまで走って
いるのかどうかも読めない —— 遅いのか、止まっているのか、そもそも呼べていないのかの
区別が付かなかった。

決めごと:

- 中身は残さない。 `ai_log` と同じ理由（依頼文には呼んだ側の材料がそのまま入る）。
  残すのは相手・モデル・種類と依頼文の大きさ、それといつ始まったか。
- 終わったら消す。 ここは「いま走っているもの」だけの表で、済んだ依頼は
  `usage_store` と `ai_log` が引き受ける。残すと同じ往復が 2 か所に並ぶ。
- 期限は始めた側が書く。 待つ秒数は相手で桁が違う（CLI ブリッジは 900 秒、直に叩く
  相手は 120 秒）ので、掃除する側が 1 つの数字で切ると、粘っている相手を消すか、
  止まったものを何十分も残すかのどちらかになる（`media._reap_stale` が kind ごとに
  猶予を分けているのと同じ問題）。始めた側は自分の上限を知っているので、
  そのとき期限まで書いておけば掃除は 1 本の DELETE で済む。
- 走らせたまま落ちたぶんは期限で消える。 `--workers 2` で動くので、片方が再起動
  すれば走っていた往復は消え、行だけが残る。
- プロセスの中の変数に持たない。 ワーカーが 2 つあるので、管理画面を出したほうと
  実際に走らせているほうが別だと何も見えない。
- `CHIEZO_STATE_DIR` が機能フラグを兼ね、記録に失敗しても呼び出しは壊さない
  （`ai_log` と同じ流儀）。
- WAL は使わない（`/state` は CLI ブリッジが読み取り専用でマウントする場所）。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import settings_store
from app.ai_log import KIND_CHAT

log = logging.getLogger("chiezo.ai_inflight")

# 相手を待つ上限に足す猶予。上限ちょうどで消すと、時間切れの処理を書いている最中の
# 行が表から先に消える（`media.STALE_AFTER` と同じ足し方）。
STALE_GRACE_SECONDS = 60.0

SCHEMA = """
CREATE TABLE IF NOT EXISTS ai_inflight (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT    NOT NULL,
    -- この行を消してよくなる時刻。始めた側が「自分の上限 + 猶予」で書く。
    expires_at   TEXT    NOT NULL,
    backend      TEXT    NOT NULL,
    model        TEXT    NOT NULL,
    effort       TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'chat',
    -- 送った依頼文の大きさ（中身は残さない）。
    prompt_bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_ai_inflight_at ON ai_inflight(at DESC);
"""


def db_path() -> Path | None:
    d = settings_store.state_dir()
    return d / "ai_inflight.db" if d else None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    return conn


def _reap_stale(conn: sqlite3.Connection) -> None:
    """誰も面倒を見ていない行を捨てる。

    往復が終われば `end()` が消すが、ワーカーごと落ちた場合はそこを通らない。
    残ったままだと画面に「ずっと走っている依頼」が並び、本当に走っているものが
    埋もれる。
    """
    conn.execute(
        "DELETE FROM ai_inflight WHERE expires_at < ?",
        (datetime.now(UTC).isoformat(timespec="seconds"),),
    )


def begin(
    *,
    backend: str,
    model: str,
    effort: str,
    prompt_bytes: int,
    timeout: float,
    kind: str = KIND_CHAT,
) -> int | None:
    """走り始めたことを 1 件残し、`end()` に渡す札を返す。

    控えが取れなくても呼び出しは止めない（`None` を返す）—— 走っているものが
    見えないことと、AI が答えられないことは別の話。
    """
    path = db_path()
    if path is None:
        return None
    now = datetime.now(UTC)
    try:
        with _connect(path) as conn:
            _reap_stale(conn)
            cur = conn.execute(
                "INSERT INTO ai_inflight (at, expires_at, backend, model, effort, kind,"
                " prompt_bytes) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    now.isoformat(timespec="seconds"),
                    (
                        now + timedelta(seconds=timeout + STALE_GRACE_SECONDS)
                    ).isoformat(timespec="seconds"),
                    backend,
                    model,
                    effort,
                    kind or KIND_CHAT,
                    prompt_bytes,
                ),
            )
            return cur.lastrowid
    except sqlite3.Error as e:
        log.warning("ai inflight write failed: %r", e)
        return None


def end(token: int | None) -> None:
    """走り終えた行を消す。成功でも失敗でも同じように呼ぶ。"""
    if token is None:
        return
    path = db_path()
    if path is None:
        return
    try:
        with _connect(path) as conn:
            conn.execute("DELETE FROM ai_inflight WHERE id = ?", (token,))
    except sqlite3.Error as e:
        log.warning("ai inflight delete failed: %r", e)


def running(limit: int = 50) -> list[dict]:
    """いま走っている依頼を新しい順に返す。何も走っていなければ空。"""
    path = db_path()
    if path is None or not path.exists():
        return []
    rows: list[dict] = []
    with suppress(sqlite3.Error):
        conn = _connect(path)
        try:
            with conn:
                _reap_stale(conn)
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT at, expires_at, backend, model, effort, kind, prompt_bytes"
                    " FROM ai_inflight ORDER BY id DESC LIMIT ?",
                    (max(1, limit),),
                )
            ]
        finally:
            conn.close()
    return rows
