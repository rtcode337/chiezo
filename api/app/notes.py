"""notes — Chiezo で唯一書き込めるソース(短期記憶の置き場)。

## なぜ Chiezo に置くのか

「AI に覚えておいてほしいこと」の置き場として、CLAUDE.md や Claude Code の記憶機能は
**毎セッション全部がコンテキストに載る**。件数が増えるほど、関係ない話のときにも
トークンを払い続けることになり、規模に対して破綻する。

Chiezo なら**常駐するのは MCP のツール定義(数百字)だけ**で、中身は引いたときにしか
載らない。100 件でも 1000 件でも常駐コストは変わらない。「さっき話したあの件」
「1 か月前に覚えてもらったあの件」のときだけ探しに行く、という使い方はこの層の担当。

## 設計の要点

- **`CHIEZO_NOTES_DIR` が機能フラグを兼ねる**(未設定 = 丸ごと無効)。「答える」層と同じ流儀。
- **置き場は `/data` と分ける**。`registry.data_dir_fingerprint()` が `/data/*.db` の
  mtime と size を 5 秒ごとに見ていて、変化があれば**全ソースを再走査**(`COUNT(*)` 込み)
  する。notes.db を `/data` に置くと、メモを 1 件書くたびに jawiki 150 万件の COUNT が走る。
  分けておけば干渉しないし、`/data` の read-only マウントも崩さずに済む。
- **スキーマはコアスキーマそのもの**。だから `search` / `doc` / `filter` / `tags` /
  ブラウズ画面 / MCP が、ソース種別を意識しない設計のおかげでそのまま効く。
  DDL は ingest の `core.py` にあるが、**api は ingest を import しない**(コンテナが別)
  ため写しを持つ。ずれると静かに壊れるので、`tests/test_notes.py` が ingest 側の
  `core.CORE_SCHEMA_DDL` から作った DB とスキーマを突き合わせて落とす。
- **`docs_fts` は external content 方式**(`content='docs'`)なので自動では同期しない。
  ingest が全件投入後に `INSERT INTO docs_fts(rowid, title, body) SELECT …` しているのと
  同じことを、1 件ずつの追記でも手でやる。削除は FTS の `'delete'` コマンドが要る。
- **読み手は `mode=ro`**(`app/db.py` の `set_mutable_paths`)。追記される DB を
  `immutable=1` で開くと壊れたページを掴む。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from fastapi import HTTPException

from app import db
from app.fts import build_match_query

from app.pages import doc_url

log = logging.getLogger("chiezo.api")

SOURCE_NAME = "notes"
SOURCE_KIND = "notes"

RECALL_LIMIT_DEFAULT = 20
RECALL_LIMIT_MAX = 100

# タイトルは本文の 1 行目から作る(`docs.title` は UNIQUE なので衝突時は doc_id を足す)
TITLE_MAX_CHARS = 60

# ingest の core.py と同じ形。**変更したら向こうも直すこと**(テストが突き合わせて落とす)。
SCHEMA_DDL = """
CREATE TABLE meta (
    source        TEXT NOT NULL,
    source_kind   TEXT NOT NULL,
    lang          TEXT,
    dump_date     TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    built_at      TEXT NOT NULL
);
CREATE TABLE docs (
    doc_id      INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    opening     TEXT,
    body        TEXT,
    tags        TEXT,
    links       TEXT,
    updated_at  TEXT,
    rank_score  REAL DEFAULT 0,
    extra       TEXT,
    feature     TEXT GENERATED ALWAYS AS (json_extract(extra, '$.feature')) VIRTUAL,
    area        TEXT GENERATED ALWAYS AS (json_extract(extra, '$.area')) VIRTUAL,
    lat         REAL GENERATED ALWAYS AS (json_extract(extra, '$.lat')) VIRTUAL,
    lon         REAL GENERATED ALWAYS AS (json_extract(extra, '$.lon')) VIRTUAL,
    wikidata    TEXT GENERATED ALWAYS AS (json_extract(extra, '$.wikidata')) VIRTUAL
);
CREATE TABLE aliases (
    alias     TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);
CREATE TABLE doc_tags (
    tag       TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);
CREATE TABLE tag_counts (
    tag       TEXT PRIMARY KEY,
    docs      INTEGER NOT NULL
) WITHOUT ROWID;
CREATE TABLE doc_coords (
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    doc_id    INTEGER NOT NULL,
    PRIMARY KEY (lat, lon, doc_id)
) WITHOUT ROWID;
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title, body,
    content='docs',
    content_rowid='doc_id',
    tokenize='trigram'
);
"""

INDEX_DDL = """
CREATE UNIQUE INDEX idx_docs_title ON docs(title);
CREATE INDEX idx_aliases_alias ON aliases(alias);
CREATE INDEX idx_docs_feature_area ON docs(feature, area);
CREATE INDEX idx_docs_area_feature ON docs(area, feature);
CREATE INDEX idx_docs_lat_lon ON docs(lat, lon);
CREATE INDEX idx_docs_wikidata ON docs(wikidata);
CREATE INDEX idx_doc_tags_tag ON doc_tags(tag, doc_id);
CREATE INDEX idx_tag_counts_docs ON tag_counts(docs DESC, tag);
CREATE INDEX idx_docs_rank ON docs(rank_score DESC, title);
"""

# notes だけが持つ索引。想起の主役は全文検索ではなく**時系列**になる見込みのため
# (「さっき話したあの件」は語が一致しない)。他のソースには無いが、コアスキーマの
# 追加ではなく notes 固有の索引なので schema_version は上げない。
NOTES_INDEX_DDL = """
CREATE INDEX idx_docs_updated ON docs(updated_at DESC, doc_id DESC);
"""

# ingest の core.SCHEMA_VERSION と揃える(registry の対応範囲に入っている必要がある)
SCHEMA_VERSION = 4


def notes_dir() -> Path | None:
    raw = os.environ.get("CHIEZO_NOTES_DIR", "").strip()
    return Path(raw) if raw else None


def is_enabled() -> bool:
    return notes_dir() is not None


def notes_path() -> Path | None:
    directory = notes_dir()
    return directory / f"{SOURCE_NAME}.db" if directory else None


def require_path() -> Path:
    path = notes_path()
    if path is None:
        raise HTTPException(
            503,
            {
                "error": "notes are disabled",
                "hint": "書き込み可能なディレクトリを CHIEZO_NOTES_DIR に設定すると有効になる",
            },
        )
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    """書き込み用の接続。

    WAL にするのは、読み手(`mode=ro`)を止めずに追記できるようにするため。
    uvicorn は複数ワーカーで動くので、書き込みが競ったときは busy_timeout で待つ
    (メモの追記は頻度が低いので、これで十分)。
    """
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_db() -> Path | None:
    """notes の DB が無ければ作る。無効なら None。

    ingest を回さずに使い始められるようにするため、起動時にここで作る
    (メモを取るのに数時間の取り込みを待たせる理由がない)。
    """
    path = notes_path()
    if path is None:
        return None
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        with conn:
            conn.executescript(SCHEMA_DDL)
            conn.executescript(INDEX_DDL)
            conn.executescript(NOTES_INDEX_DDL)
            conn.execute(
                "INSERT INTO meta (source, source_kind, lang, dump_date, schema_version, built_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (SOURCE_NAME, SOURCE_KIND, None, None, SCHEMA_VERSION, _now()),
            )
    finally:
        conn.close()
    log.info("created notes db at %s", path)
    return path


def _make_title(text: str) -> str:
    """本文の 1 行目からタイトルを作る。"""
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:TITLE_MAX_CHARS]
    return "メモ"


def split_tags(tags: str | None) -> list[str]:
    return [t.strip() for t in (tags or "").split(",") if t.strip()]


def add(text: str, title: str | None = None, tags: str | None = None) -> dict:
    """メモを 1 件足して、足したものを返す。"""
    path = require_path()
    ensure_db()
    body = text.strip()
    if not body:
        raise HTTPException(400, {"error": "text must not be empty"})
    tag_list = split_tags(tags)
    now = _now()
    conn = _connect(path)
    try:
        with conn:
            (doc_id,) = conn.execute("SELECT COALESCE(MAX(doc_id), 0) + 1 FROM docs").fetchone()
            base = (title or "").strip() or _make_title(body)
            # docs.title は UNIQUE。同じ 1 行目のメモを何度も取ることはあるので、
            # 衝突したら doc_id を足して一意にする(呼び出し側に失敗を見せない)。
            final_title = base
            if conn.execute("SELECT 1 FROM docs WHERE title = ?", (base,)).fetchone():
                final_title = f"{base} ({doc_id})"
            conn.execute(
                "INSERT INTO docs (doc_id, title, opening, body, tags, links, updated_at,"
                " rank_score, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    doc_id, final_title, body[:TITLE_MAX_CHARS * 4], body,
                    json.dumps(tag_list, ensure_ascii=False), None, now, 0.0, None,
                ),
            )
            # external content なので FTS には手で入れる(core.py と同じ書き方)
            conn.execute(
                "INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)",
                (doc_id, final_title, body),
            )
            for tag in tag_list:
                conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id))
                conn.execute(
                    "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
                    " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
                    (tag,),
                )
    finally:
        conn.close()
    return {
        "doc_id": doc_id,
        "title": final_title,
        "tags": tag_list,
        "updated_at": now,
        "url": doc_url(SOURCE_NAME, doc_id),
    }


def delete(doc_id: int) -> bool:
    """メモを 1 件消す。消せたら True、元から無ければ False。"""
    path = require_path()
    conn = _connect(path)
    try:
        with conn:
            row = conn.execute(
                "SELECT title, body, tags FROM docs WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                return False
            # external content の FTS は 'delete' コマンドで、入れたときと同じ値を渡して消す
            conn.execute(
                "INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', ?, ?, ?)",
                (doc_id, row["title"], row["body"]),
            )
            for tag in json.loads(row["tags"] or "[]"):
                conn.execute(
                    "UPDATE tag_counts SET docs = docs - 1 WHERE tag = ?", (tag,)
                )
            conn.execute("DELETE FROM tag_counts WHERE docs <= 0")
            conn.execute("DELETE FROM doc_tags WHERE doc_id = ?", (doc_id,))
            conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))
    finally:
        conn.close()
    return True


def recall(
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tag: str | None = None,
    limit: int = RECALL_LIMIT_DEFAULT,
    offset: int = 0,
) -> dict:
    """メモを思い出す。**新しい順**に返す。

    全文検索(`q`)は trigram なので「あの件」のような曖昧な問いには当たらない。
    そのため `q` を省くと時系列だけで引ける形にしてある(「さっき話したあの件」は
    語ではなく時刻で引くほうが確実)。`since` / `until` は `updated_at` との
    文字列比較なので、`2026-07-31` でも `2026-07-31T12:00:00+00:00` でも渡せる。

    **上限はここで担保する**。REST の `Query(ge=1, le=…)` は HTTP の口にしか効かず、
    MCP(`app/mcp_server.py`)は api の関数を Python から直接呼ぶので通らない。
    SQLite は `LIMIT -1` を「無制限」と解釈するため、負の値がそのまま届くと
    全件返る(頁を送る意図の呼び出しが静かに全件取得になる)。
    """
    path = require_path()
    ensure_db()
    limit = max(1, min(int(limit), RECALL_LIMIT_MAX))
    offset = max(0, int(offset))
    where: list[str] = []
    params: list = []
    if since:
        where.append("d.updated_at >= ?")
        params.append(since)
    if until:
        where.append("d.updated_at <= ?")
        params.append(until)
    for name in split_tags(tag):
        where.append("d.doc_id IN (SELECT dt.doc_id FROM doc_tags dt WHERE dt.tag = ?)")
        params.append(name)

    match = build_match_query(q) if q else None
    if q and match is None:
        # trigram で扱えない短い語。本文の部分一致に落とす(件数が小さいので走査で足りる)
        where.append("(d.title LIKE ? OR d.body LIKE ?)")
        params.extend([f"%{q.strip()}%"] * 2)
    elif match is not None:
        where.append("d.doc_id IN (SELECT rowid FROM docs_fts WHERE docs_fts MATCH ?)")
        params.append(match)

    clause = (" WHERE " + " AND ".join(where)) if where else ""
    (total,) = db.query(path, f"SELECT COUNT(*) FROM docs d{clause}", tuple(params))[0]
    rows = db.query(
        path,
        "SELECT d.doc_id, d.title, d.body, d.tags, d.updated_at"
        f" FROM docs d{clause}"
        " ORDER BY d.updated_at DESC, d.doc_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "source": SOURCE_NAME,
        "total": total,
        "offset": offset,
        "notes": [
            {
                "doc_id": r["doc_id"],
                "title": r["title"],
                "text": r["body"],
                "tags": json.loads(r["tags"] or "[]"),
                "updated_at": r["updated_at"],
                "url": doc_url(SOURCE_NAME, r["doc_id"]),
            }
            for r in rows
        ],
    }
