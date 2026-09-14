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
- **中身はコアスキーマで持ち、普通のソースとして登録する**(`machine`)。
  人が触らない置き場だが、**見えないままでは確かめようがない** —— 収集が消えた・
  戻ってきたのような話を追うとき、まず知りたいのは「設定がどこに、いつの姿で
  残っているか」。コアスキーマなら `search` / `doc` / `filter` も画面も
  そのまま効くので、この置き場のためだけの読み口を作らずに済む
  (短期記憶が `notes` として登録されているのと同じ形)。
  **DDL は `app/notes.py` から借りる** —— app の中に写しを増やすと、
  ずれたときに「ここだけ filter が 409」のように静かに壊れる。
- **1 件 = `<種類>/<名前>` という見出しの文書**。種類はタグにも入れるので、
  `filter?tag=collect` で種類ごとに引ける。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException

from app import notes

SOURCE_NAME = "machine"
SOURCE_KIND = "machine"

# 見出しの組み立て方。`collect/definitions` のような形になる
KEY_SEP = "/"


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
    return conn


def ensure_db() -> Path | None:
    """置き場が無ければ作る。無効なら None。

    **コアスキーマで作る**ので、そのまま普通のソースとして登録できる
    (`registry`)。DDL は `app/notes.py` から借りる —— app の中に写しを増やすと、
    ずれたときに「ここだけ filter が 409」のように静かに壊れる。
    """
    path = db_path()
    if path is None:
        return None
    conn = _connect()
    try:
        with conn:
            if _has_table(conn, "docs"):
                return path
            conn.executescript(notes.SCHEMA_DDL)
            conn.executescript(notes.INDEX_DDL)
            conn.execute(
                "INSERT INTO meta (source, source_kind, lang, dump_date, schema_version,"
                " built_at) VALUES (?, ?, ?, ?, ?, ?)",
                (SOURCE_NAME, SOURCE_KIND, None, None, notes.SCHEMA_VERSION, _now()),
            )
    finally:
        conn.close()
    return path


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _title(kind: str, key: str) -> str:
    return f"{kind}{KEY_SEP}{key}"


def _write(conn: sqlite3.Connection, doc_id: int, title: str, kind: str,
           body: str, now: str) -> None:
    """1 件ぶんを書く。**索引も一緒に入れる**(external content は自動で追いつかない)。"""
    conn.execute(
        "INSERT INTO docs (doc_id, title, opening, body, tags, links, updated_at,"
        " rank_score, extra) VALUES (?, ?, ?, ?, ?, NULL, ?, 0.0, NULL)",
        (doc_id, title, body[:notes.TITLE_MAX_CHARS * 4], body,
         json.dumps([kind], ensure_ascii=False), now),
    )
    conn.execute(
        "INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)", (doc_id, title, body)
    )
    conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (kind, doc_id))
    conn.execute(
        "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
        " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
        (kind,),
    )


def _erase(conn: sqlite3.Connection, row) -> None:
    """1 件ぶんを消す。**索引も一緒に外す**(入れたときと同じ値を渡す)。"""
    conn.execute(
        "INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', ?, ?, ?)",
        (row["doc_id"], row["title"], row["body"]),
    )
    for tag in json.loads(row["tags"] or "[]"):
        conn.execute("UPDATE tag_counts SET docs = docs - 1 WHERE tag = ?", (tag,))
    conn.execute("DELETE FROM tag_counts WHERE docs <= 0")
    conn.execute("DELETE FROM doc_tags WHERE doc_id = ?", (row["doc_id"],))
    conn.execute("DELETE FROM docs WHERE doc_id = ?", (row["doc_id"],))


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def get(kind: str, key: str) -> str | None:
    """その 1 件の中身(JSON の文字列)。無ければ None。"""
    ensure_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT body FROM docs WHERE title = ?", (_title(kind, key),)
        ).fetchone()
    return row["body"] if row else None


def put(kind: str, key: str, body: str) -> None:
    """その 1 件を置く(あれば置き換える)。

    **`doc_id` は引き継ぐ** —— 文書の URL が書き換えのたびに変わらないようにする
    (固化や収集の焼き直しと同じ約束)。
    """
    ensure_db()
    title = _title(kind, key)
    with _connect() as conn, conn:
        row = conn.execute(
            "SELECT doc_id, title, body, tags FROM docs WHERE title = ?", (title,)
        ).fetchone()
        if row is None:
            (doc_id,) = conn.execute(
                "SELECT COALESCE(MAX(doc_id), 0) + 1 FROM docs"
            ).fetchone()
        else:
            doc_id = row["doc_id"]
            _erase(conn, row)
        _write(conn, doc_id, title, kind, body, _now())


def drop(kind: str, key: str) -> bool:
    """その 1 件を消す。消したら True。"""
    ensure_db()
    with _connect() as conn, conn:
        row = conn.execute(
            "SELECT doc_id, title, body, tags FROM docs WHERE title = ?",
            (_title(kind, key),),
        ).fetchone()
        if row is None:
            return False
        _erase(conn, row)
    return True


def records() -> list[dict]:
    """置いてあるもの全部の**目録**(中身は運ばない)。

    中身は 1 件ずつ引く(`get`)か、普通のソースとして開く —— まとめて返すと、
    区画の台帳を抱えた 1 件で数百 KB になる。
    """
    if not is_enabled():
        return []
    ensure_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT doc_id, title, LENGTH(body) AS bytes, updated_at FROM docs"
            " ORDER BY title"
        ).fetchall()
    return [dict(r) for r in rows]


def updated_at(kind: str, key: str) -> str:
    """いつ書き換わったか(無ければ空)。"""
    ensure_db()
    with _connect() as conn:
        row = conn.execute(
            "SELECT updated_at FROM docs WHERE title = ?", (_title(kind, key),)
        ).fetchone()
    return row["updated_at"] if row else ""
