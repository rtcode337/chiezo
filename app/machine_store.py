"""機械が書き換えるものの置き場(`state/machine.db`)。

**人が書くものと混ぜない。** 収集の定義のような「機械が毎回書き換えるもの」は、
これまで短期記憶(`app/notes.py`)に 1 件のメモとして入れていた。あそこは人と AI が
読み書きする場所なので、混ぜると 3 つ困る:

- 人が消せてしまう(消すと収集がまるごと消える)
- 固化(`app/memory.py`)の対象に紛れる。長期記憶に設定が焼かれても意味が無い
- **1 件のメモに収める都合で、中身に上限が要る**。実際、消したものの控え(墓場)は
  2,000 件で頭打ちにしてあり、溢れると古いものから静かに戻ってくる

置き場を分ければ、どれも消える。

**`state/settings.db` にも相乗りしない。** あちらは「ユーザーが決めるものだけ」
(どの相手を使うか・認証情報・既定のモデル)で、人が入れた値と機械が書いた値が
同じファイルに並ぶと、消してよいものの線が引けなくなる。

設計の要点:

- `CHIEZO_STATE_DIR` が機能フラグを兼ねる(未設定なら機械の置き場が無い)。
  「使う」層・「覚える」層・設定の置き場と同じ流儀。
- **中身は JSON のまま持つ。** 収集の定義も進み具合も形が変わり続けるので、
  列に割ると変えるたびに移行が要る。引きたいのは「その名前のもの 1 つ」だけ。
- 種類(`kind`)で分ける。収集の定義とその墓場を同じ表に並べても、互いに干渉しない。
- WAL は使わない(`settings.db` と同じ理由。ブリッジが読み取り専用でマウントしうる)。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    kind       TEXT NOT NULL,
    key        TEXT NOT NULL,
    body       TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (kind, key)
);
"""


def state_dir() -> Path | None:
    raw = os.environ.get("CHIEZO_STATE_DIR", "").strip()
    return Path(raw) if raw else None


def is_enabled() -> bool:
    return state_dir() is not None


def db_path() -> Path | None:
    d = state_dir()
    return d / "machine.db" if d else None


def require_path() -> Path:
    path = db_path()
    if path is None:
        raise HTTPException(
            503,
            {
                "error": "machine storage is disabled",
                "hint": "書き込み可能なディレクトリを CHIEZO_STATE_DIR に設定すると、"
                        "収集の定義を置けるようになる",
            },
        )
    return path


def _connect() -> sqlite3.Connection:
    path = require_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    # WAL を使わない(`settings.db` と同じ理由 —— 読み取り専用でマウントされうる)
    conn.execute("PRAGMA journal_mode=DELETE")
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get(kind: str, key: str) -> str | None:
    """その 1 件の中身(JSON の文字列)。無ければ None。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT body FROM records WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
    return row["body"] if row else None


def put(kind: str, key: str, body: str) -> None:
    """その 1 件を置く(あれば置き換える)。"""
    with _connect() as conn:
        conn.execute(
            "INSERT INTO records (kind, key, body, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT (kind, key) DO UPDATE SET body = excluded.body,"
            " updated_at = excluded.updated_at",
            (kind, key, body, _now()),
        )


def drop(kind: str, key: str) -> bool:
    """その 1 件を消す。消したら True。"""
    with _connect() as conn:
        return conn.execute(
            "DELETE FROM records WHERE kind = ? AND key = ?", (kind, key)
        ).rowcount > 0


def updated_at(kind: str, key: str) -> str:
    """いつ書き換わったか(無ければ空)。"""
    with _connect() as conn:
        row = conn.execute(
            "SELECT updated_at FROM records WHERE kind = ? AND key = ?", (kind, key)
        ).fetchone()
    return row["updated_at"] if row else ""
