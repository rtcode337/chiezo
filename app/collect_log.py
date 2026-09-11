"""収集が 1 回走るたびの変更履歴(`state/collect_runs.db`)。

## 何のためか

**どこに修正が入ったかを、後から読めるようにする。** 収集は無人で回る層なので、
その場に居合わせない人が「最近この収集は何を足して何を消したのか」を追えないと、
プロンプトを直す判断ができない。

定義のメモ側にも控えはあるが(`last_added` / `last_removed_titles`)、**最新の 1 回
ぶんだけ**で上書きされる。6 時間ごとに回る収集なら、朝見たときには昨日の夜の 1 回しか
残っていない。減り続けているのか、ある日だけ荒れたのかは、並べないと分からない。

## 決めごと

- **見出しは残す**(`app/ai_log.py` は中身を残さないが、こちらは残す)。あちらが
  残さないのは、プロンプトに呼んだ側の材料がそのまま入るため。こちらが記録するのは
  **長期記憶へ焼かれて誰でも引ける文書の見出し**で、隠す意味が無い。むしろ件数だけでは
  「何が落ちたのか」が分からず、記録する意味がほとんど消える。
- **`CHIEZO_STATE_DIR` が機能フラグを兼ねる**(未設定なら何も記録しない)。
  設定・notes・失敗の控えと同じ流儀。
- **記録に失敗しても収集は壊さない。** 履歴が取れないことと、集められないことは別の話。
- **溜め続けない**(`MAX_ROWS`)。読むのは「最近どこが動いたか」だけ。
- **WAL は使わない。** `/state` は CLI ブリッジが読み取り専用でマウントする場所で、
  WAL の読み手は -shm への書き込みを要求する(`app/settings_store.py` と同じ理由)。
- **失敗も 1 行として残す。** 「その日は走ったが何も入らなかった」と「そもそも走って
  いない」は別物で、成功だけ記録すると両方が同じ「空白」に見える。
"""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from app import settings_store

log = logging.getLogger("chiezo.app")

# 残す行数。1 回の実行が 1 行。6 時間ごとの収集を 10 本抱えても半年ぶん残る。
MAX_ROWS = 2_000

# 1 回ぶんに残す見出しの数。**頭のほうだけ** —— 全部持つと、初期構築の 1 回で
# 数千件の見出しが 1 行に入る(読むのは「何が動いたか」の手がかりで、全件ではない)。
MAX_TITLES = 20

# 理由の長さの上限。相手の応答をそのまま抱え込まないため。
REASON_MAX = 500

STATUS_OK = "ok"
STATUS_ERROR = "error"

SCHEMA = """
CREATE TABLE IF NOT EXISTS collect_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    at             TEXT    NOT NULL,
    name           TEXT    NOT NULL,
    status         TEXT    NOT NULL,
    -- 焼いた後の総件数。0 は失敗した回（素材を組めていない）。
    total          INTEGER NOT NULL DEFAULT 0,
    added          INTEGER NOT NULL DEFAULT 0,
    updated        INTEGER NOT NULL DEFAULT 0,
    removed        INTEGER NOT NULL DEFAULT 0,
    skipped        INTEGER NOT NULL DEFAULT 0,
    -- それぞれ見出しの配列（JSON）。頭のほうだけ。
    added_titles   TEXT    NOT NULL DEFAULT '[]',
    updated_titles TEXT    NOT NULL DEFAULT '[]',
    removed_titles TEXT    NOT NULL DEFAULT '[]',
    error          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_collect_runs_at ON collect_runs(at DESC);
CREATE INDEX IF NOT EXISTS ix_collect_runs_name ON collect_runs(name, id DESC);
"""


def db_path() -> Path | None:
    d = settings_store.state_dir()
    return d / "collect_runs.db" if d else None


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    return conn


def _titles(values) -> str:
    """見出しの配列を JSON にする。頭のほうだけ・1 件の長さも切る。"""
    picked = [str(v).strip()[:200] for v in (values or []) if str(v).strip()]
    return json.dumps(picked[:MAX_TITLES], ensure_ascii=False)


def _read_titles(raw) -> list[str]:
    with suppress(ValueError, TypeError):
        value = json.loads(raw or "[]")
        if isinstance(value, list):
            return [str(v) for v in value]
    return []


def record(name: str, *, status: str, diff: dict | None = None, error: str = "") -> None:
    """1 回ぶんを残す。**呼び出し側の失敗にはしない**(控えが取れなくても収集は続く)。

    `diff` は `app/collect.py` の `material` が返すもの。失敗した回は None で呼ぶ。
    """
    path = db_path()
    if path is None:
        return
    d = diff or {}
    try:
        with _connect(path) as conn:
            conn.execute(
                "INSERT INTO collect_runs (at, name, status, total, added, updated, removed,"
                " skipped, added_titles, updated_titles, removed_titles, error)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    name,
                    status,
                    int(d.get("total") or 0),
                    int(d.get("added") or 0),
                    int(d.get("updated") or 0),
                    int(d.get("removed") or 0),
                    int(d.get("skipped") or 0),
                    _titles(d.get("added_titles")),
                    _titles(d.get("updated_titles")),
                    _titles(d.get("removed_titles")),
                    (error or "")[:REASON_MAX],
                ),
            )
            # 古いものから捨てる。件数で切るのは、実行の頻度が収集ごとに違うため
            # （5 分ごとのものと週 1 のものが同じ表に並ぶので、日数では揃わない）。
            conn.execute(
                "DELETE FROM collect_runs WHERE id <= (SELECT MAX(id) FROM collect_runs) - ?",
                (MAX_ROWS,),
            )
    except sqlite3.Error as e:
        log.warning("collect run log write failed: %r", e)


def recent(name: str | None = None, limit: int = 50) -> list[dict]:
    """直近の実行を新しい順に。`name` を渡すとその収集だけ。記録が無ければ空。"""
    path = db_path()
    if path is None or not path.exists():
        return []
    sql = (
        "SELECT at, name, status, total, added, updated, removed, skipped,"
        " added_titles, updated_titles, removed_titles, error FROM collect_runs"
    )
    args: list = []
    if name:
        sql += " WHERE name = ?"
        args.append(name)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(max(1, min(limit, MAX_ROWS)))
    rows: list[dict] = []
    with suppress(sqlite3.Error):
        conn = _connect(path)
        try:
            for r in conn.execute(sql, tuple(args)):
                row = dict(r)
                for key in ("added_titles", "updated_titles", "removed_titles"):
                    row[key] = _read_titles(row[key])
                rows.append(row)
        finally:
            conn.close()
    return rows


def forget(name: str) -> None:
    """収集を消したときに、その履歴も落とす。

    残しておくと、同じ名前で作り直したときに前の収集の変更が混ざって見える
    （名前がソース名なので、作り直しは普通に起きる）。
    """
    path = db_path()
    if path is None or not path.exists():
        return
    with suppress(sqlite3.Error), _connect(path) as conn:
        conn.execute("DELETE FROM collect_runs WHERE name = ?", (name,))
