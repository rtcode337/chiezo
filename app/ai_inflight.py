"""いま走っている AI への依頼の控え（`state/ai_inflight.db`）。

なぜ要るか。 「AI への依頼」の表（`app/views/ai_history.py`）に行が立つのは往復が
終わってからで、成功は `usage_store`、失敗は `ai_log` に書かれる。CLI ブリッジ越しの
相手は数十秒から数分かかるので、そのあいだ画面には何も出ない。頼んだ本人が待っている
うちは分かるが、無人で回る層（収集の時計）が動かしているぶんは、終わるまで走って
いるのかどうかも読めない —— 遅いのか、止まっているのか、そもそも呼べていないのかの
区別が付かなかった。

決めごと:

- 依頼文は残す。ただしこの表にだけ。 `ai_log`（失敗の控え）が中身を持たないのは、
  500 件を溜め続ける表だから —— 依頼文には呼んだ側の材料がそのまま入る（銘柄の
  保有状況、家庭内の通信先…）ので、後から読める場所に積むものではない。
  **この表は違う。走っているあいだしか行が無く、終われば消える。**
  そして「いま何が走っているか」は、相手とモデルと大きさだけでは分からない ——
  同じ相手に同じくらいの大きさの依頼を 2 本投げていたら、どちらがどちらか読めない。
  止めるかどうかを決めるには中身が要る。長すぎるものは `PROMPT_MAX` で切る。
- 誰の代わりに走っているかを持つ（`job_id`）。 文章の生成（`media._run_text`）は
  中で会話の口を呼ぶので、**1 つの依頼がジョブと会話の 2 か所に並ぶ**。
  実際に走っているのは 1 本なので、紐を持たせて画面と数を 1 本に戻す。
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
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path

from app import settings_store
from app.ai_log import KIND_CHAT

log = logging.getLogger("chiezo.ai_inflight")

# 相手を待つ上限に足す猶予。上限ちょうどで消すと、時間切れの処理を書いている最中の
# 行が表から先に消える（`media.STALE_AFTER` と同じ足し方）。
STALE_GRACE_SECONDS = 60.0

# 残す依頼文の長さ。読んで「何が走っているか」が分かればよく、全文は要らない
# （実測で 300KB を超える依頼がある）。切ったことは画面側で示す。
PROMPT_MAX = 20_000

# いま走っている往復が、どの生成ジョブの代わりに呼ばれているか。
# **引数で引き回さない。** 会話の口までは何段も挟まっていて、途中の層は
# ジョブのことを知らなくてよい（知らせると、会話しか使わない経路まで
# ジョブの都合を持つことになる）。
_JOB: ContextVar[str] = ContextVar("chiezo_ai_inflight_job", default="")


@contextmanager
def on_behalf_of(job_id: str):
    """このかたまりの中で立つ控えに、抱えているジョブの id を付ける。

    付けておくと、画面はジョブの行と会話の行が同じ 1 本だと分かるので、
    2 件走っているように見せずに済む（`views/ai_history.running_rows`）。
    """
    token = _JOB.set(job_id or "")
    try:
        yield
    finally:
        _JOB.reset(token)

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
    -- 送った依頼文の大きさ。切り詰めた `prompt` と違い、これは元のままの数。
    prompt_bytes INTEGER NOT NULL,
    -- 送った依頼文（`PROMPT_MAX` で切る）。終われば行ごと消える。
    prompt       TEXT    NOT NULL DEFAULT '',
    -- この往復を抱えている生成ジョブ。直に呼ばれたものは空。
    job_id       TEXT    NOT NULL DEFAULT ''
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
    _add_missing_columns(conn)
    return conn


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """既にある表に、後から足した列を継ぎ足す。

    `CREATE TABLE IF NOT EXISTS` は既存の表には効かないので、列を足した版を
    古い `state/` に当てると `no such column` で読めなくなる。作り直しても
    困らない表（走っているものしか入っていない）だが、**動いている最中に
    入れ替わる**ので、消すのではなく足すほうが静かに済む。
    """
    have = {r["name"] for r in conn.execute("PRAGMA table_info(ai_inflight)")}
    for name in ("prompt", "job_id"):
        if name not in have:
            with suppress(sqlite3.Error):
                conn.execute(
                    f"ALTER TABLE ai_inflight ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")


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
    prompt: str = "",
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
                " prompt_bytes, prompt, job_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                    (prompt or "")[:PROMPT_MAX],
                    _JOB.get(""),
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
                    "SELECT at, expires_at, backend, model, effort, kind, prompt_bytes,"
                    " prompt, job_id FROM ai_inflight ORDER BY id DESC LIMIT ?",
                    (max(1, limit),),
                )
            ]
        finally:
            conn.close()
    return rows
