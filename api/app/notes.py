"""notes — Chiezo で唯一書き込めるソース(短期記憶の置き場)。

## なぜ Chiezo に置くのか

「AI に覚えておいてほしいこと」の置き場として、CLAUDE.md や Claude Code の記憶機能は
毎セッション全部がコンテキストに載る。件数が増えるほど、関係ない話のときにも
トークンを払い続けることになり、規模に対して破綻する。

Chiezo なら常駐するのは MCP のツール定義(数百字)だけで、中身は引いたときにしか
載らない。100 件でも 1000 件でも常駐コストは変わらない。「さっき話したあの件」
「1 か月前に覚えてもらったあの件」のときだけ探しに行く、という使い方はこの層の担当。

## 設計の要点

- `CHIEZO_NOTES_DIR` が機能フラグを兼ねる(未設定 = 丸ごと無効)。「使う」層と同じ流儀。
- 置き場は `/data` と分ける。`registry.data_dir_fingerprint()` が `/data/*.db` の
  mtime と size を 5 秒ごとに見ていて、変化があれば全ソースを再走査(`COUNT(*)` 込み)
  する。notes.db を `/data` に置くと、メモを 1 件書くたびに jawiki 150 万件の COUNT が走る。
  分けておけば干渉しないし、`/data` の read-only マウントも崩さずに済む。
- スキーマはコアスキーマそのもの。だから `search` / `doc` / `filter` / `tags` /
  ブラウズ画面 / MCP が、ソース種別を意識しない設計のおかげでそのまま効く。
  DDL は ingest の `core.py` にあるが、api は ingest を import しない(コンテナが別)
  ため写しを持つ。ずれると静かに壊れるので、`tests/test_notes.py` が ingest 側の
  `core.CORE_SCHEMA_DDL` から作った DB とスキーマを突き合わせて落とす。
- `docs_fts` は external content 方式(`content='docs'`)なので自動では同期しない。
  ingest が全件投入後に `INSERT INTO docs_fts(rowid, title, body) SELECT …` しているのと
  同じことを、1 件ずつの追記でも手でやる。削除は FTS の `'delete'` コマンドが要る。
- 読み手は `mode=ro`(`app/db.py` の `set_mutable_paths`)。追記される DB を
  `immutable=1` で開くと壊れたページを掴む。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import UTC, datetime
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

# recall は当たったメモの本文をまるごと返すので、20 件返れば 20 件分の全文が
# 会話のコンテキストに載る。他ソースが `search`(冒頭だけ)→ `doc`(全文)の二段に
# なっているのに合わせ、既定では先頭 400 文字に切って `truncated` を立てる
# (全文は `url` / `doc_id` から `/v1/notes/doc/{doc_id}` で取り直せる)。
# 0 を渡すと切らない —— `doc` / `filter` の `max_chars` と同じ流儀。
RECALL_MAX_CHARS_DEFAULT = 400

# recall が返せる項目。`fields` で選ぶと、当たりを付ける段では本文を載せずに済む。
RECALL_FIELDS = ("doc_id", "title", "text", "tags", "updated_at", "url")

# 名指ししたときだけ返る項目。`extra` を既定に入れると、持たないほとんどのメモにも
# `"extra": null` が並び、recall を読む AI のコンテキストを無駄に食う。
# 構造を持つのはタスク・ルールの画面だけなので、要る側が明示的に取りに来る。
RECALL_OPTIONAL_FIELDS = ("extra",)
RECALL_ALLOWED_FIELDS = RECALL_FIELDS + RECALL_OPTIONAL_FIELDS

# タイトルは本文の 1 行目から作る(`docs.title` は UNIQUE なので衝突時は doc_id を足す)
TITLE_MAX_CHARS = 60

# ingest の core.py と同じ形。変更したら向こうも直すこと(テストが突き合わせて落とす)。
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

# notes だけが持つ索引。想起の主役は全文検索ではなく時系列になる見込みのため
# (「さっき話したあの件」は語が一致しない)。他のソースには無いが、コアスキーマの
# 追加ではなく notes 固有の索引なので schema_version は上げない。
NOTES_INDEX_DDL = """
CREATE INDEX idx_docs_updated ON docs(updated_at DESC, doc_id DESC);
"""

# ingest の core.SCHEMA_VERSION と揃える(registry の対応範囲に入っている必要がある)
SCHEMA_VERSION = 4

# タグの定番語彙。**書き手(セッション・AI クライアント・人)が変わっても、同じ意味には
# 同じ表記が付く**ようにするための、サーバー側の 1 か所 —— クライアント側の CLAUDE.md に
# 写しを持たせると写しごとにずれていく(実際に NAS と nas、デプロイ と deploy に割れた)。
# 配り方は MCP の remember のツール定義(tag_guide())。タグ自体は自由入力のままで、
# ここに無い語を拒みはしない(語彙は絞り込みの実用が目的で、検閲ではない)。
CANONICAL_TAGS: dict[str, str] = {
    "todo": "作業。タスク画面に並ぶ。状態のタグが無いものは未着手",
    "着手中": "todo のうち依頼済みで動作確認待ちのもの",
    "完了": "todo のうち終わったもの",
    "難所": "todo のうち直すのが大変そうなもの(状態とは別軸)",
    "rule": "全環境に効かせる決まりごと。本文が Markdown 1 本ぶん",
    "無効": "rule のうち連結に含めないもの",
    "project": "タスクの入れ物の定義。本文にリポジトリの URL を書く",
    "アーカイブ": "project のうち片付いたもの",
    "決定": "決めたこと・方針",
    "runbook": "繰り返し使う手順",
    "環境": "開発マシン・LAN・ポートなど環境の事実",
    "本番": "本番運用の事実(URL・構成)",
    "設計メモ": "設計判断・検討の記録",
    "トラブルシュート": "ハマった症状と原因・回避策",
}


def tag_guide() -> str:
    """remember のツール定義に載せるタグの手引き。語彙の出どころは CANONICAL_TAGS の 1 か所。"""
    listed = " / ".join(f"{tag}={hint}" for tag, hint in CANONICAL_TAGS.items())
    return (
        "**同じ意味には同じ表記のタグ**を付けること。定番: "
        + listed
        + "。プロジェクトはリポジトリ名を小文字でそのまま(例 tech-antenna)。"
        "これ以外のタグを作る前に tags で既存の表記を確かめる"
        "(NAS と nas のような割れは絞り込みを壊す)。"
    )


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
    return datetime.now(UTC).isoformat(timespec="seconds")


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


def dump_extra(extra: dict | None) -> str | None:
    """`docs.extra` に入れる JSON。空の dict は「持たない」と同じに畳む。

    タグで表せる性質(種別・状態・所属)はタグに置き、ここに入れるのは
    タグにすると読めなくなるものだけ —— 今はタスク・ルールの並び順(`sort_order`)。
    """
    return json.dumps(extra, ensure_ascii=False) if extra else None


def load_extra(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("notes: could not parse extra as json: %r", raw)
        return None
    return value if isinstance(value, dict) else None


def add(
    text: str,
    title: str | None = None,
    tags: str | None = None,
    extra: dict | None = None,
) -> dict:
    """メモを 1 件足して、足したものを返す。

    `extra` はタグで表せない構造(並び順など)の置き場。詳しくは `dump_extra`。
    """
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
                    json.dumps(tag_list, ensure_ascii=False), None, now, 0.0,
                    dump_extra(extra),
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
    created = {
        "doc_id": doc_id,
        "title": final_title,
        "tags": tag_list,
        "updated_at": now,
        "url": doc_url(SOURCE_NAME, doc_id),
    }
    # 持たないメモに "extra": null を並べない(recall の既定項目と同じ考え方)
    if extra:
        created["extra"] = extra
    return created


def update(
    doc_id: int,
    text: str | None = None,
    title: str | None = None,
    tags: str | None = None,
    extra: dict | None = None,
) -> dict | None:
    """メモを 1 件書き換える。渡した項目だけを差し替え、None の項目は今のまま。

    - `tags` は丸ごと置き換え(カンマ区切り)。空文字を渡すと全部外れる ——
      「1 個だけ足す」はできない(部分編集を許すと、読み手が今の値を知らないまま
      消してしまう。置き換えなら送った値がそのまま結果になる)
    - `extra` も同じく丸ごと置き換え。空の dict を渡すと外れる
    - `updated_at` は現在時刻になる。recall は新しい順なので、**書き換えたメモは
      先頭に浮く**(「最新の判断が上に来る」は想起の用途では望ましい側)
    - タイトルの衝突は `add` と同じ規則で doc_id を足して逃がす
    - 見つからなければ None(HTTP 層が 404 にする)
    """
    if text is None and title is None and tags is None and extra is None:
        raise HTTPException(
            400, {"error": "nothing to update: pass text, title, tags or extra"}
        )
    if text is not None and not text.strip():
        raise HTTPException(400, {"error": "text must not be empty"})
    if title is not None and not title.strip():
        raise HTTPException(400, {"error": "title must not be empty"})

    path = require_path()
    conn = _connect(path)
    try:
        with conn:
            row = conn.execute(
                "SELECT title, body, tags, extra FROM docs WHERE doc_id = ?", (doc_id,)
            ).fetchone()
            if row is None:
                return None

            new_body = text.strip() if text is not None else row["body"]
            new_title = title.strip() if title is not None else row["title"]
            # docs.title は UNIQUE。他のメモと衝突したら add と同じ規則で一意にする
            if new_title != row["title"] and conn.execute(
                "SELECT 1 FROM docs WHERE title = ? AND doc_id != ?", (new_title, doc_id)
            ).fetchone():
                new_title = f"{new_title} ({doc_id})"

            old_tags = json.loads(row["tags"] or "[]")
            new_tags = split_tags(tags) if tags is not None else old_tags
            new_extra = extra if extra is not None else load_extra(row["extra"])

            now = _now()
            conn.execute(
                "UPDATE docs SET title = ?, opening = ?, body = ?, tags = ?, updated_at = ?,"
                " extra = ? WHERE doc_id = ?",
                (
                    new_title, new_body[:TITLE_MAX_CHARS * 4], new_body,
                    json.dumps(new_tags, ensure_ascii=False), now,
                    dump_extra(new_extra), doc_id,
                ),
            )
            # external content の FTS は自動では追従しない。'delete' に書き換え前の値を
            # 渡して消してから、新しい値を入れ直す(add / delete と同じ流儀)
            conn.execute(
                "INSERT INTO docs_fts(docs_fts, rowid, title, body) VALUES ('delete', ?, ?, ?)",
                (doc_id, row["title"], row["body"]),
            )
            conn.execute(
                "INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)",
                (doc_id, new_title, new_body),
            )

            if new_tags != old_tags:
                conn.execute("DELETE FROM doc_tags WHERE doc_id = ?", (doc_id,))
                for tag in old_tags:
                    conn.execute(
                        "UPDATE tag_counts SET docs = docs - 1 WHERE tag = ?", (tag,)
                    )
                conn.execute("DELETE FROM tag_counts WHERE docs <= 0")
                for tag in new_tags:
                    conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id))
                    conn.execute(
                        "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
                        " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
                        (tag,),
                    )
    finally:
        conn.close()
    updated = {
        "doc_id": doc_id,
        "title": new_title,
        "tags": new_tags,
        "updated_at": now,
        "url": doc_url(SOURCE_NAME, doc_id),
    }
    if new_extra:
        updated["extra"] = new_extra
    return updated


def set_extra(doc_id: int, extra: dict | None) -> bool:
    """`extra` だけを書き換える。**`updated_at` を動かさない**。見つからなければ False。

    並び替えのための口。`update()` を使うと `updated_at` が現在時刻になり、
    **カードを 1 枚ドラッグしただけで `recall` の先頭に浮いてしまう** ——
    並び替えはメモの内容が動いたわけではないので、時系列を乱すのは誤り
    (cc-tasks でも並び替えだけは `updated_at` を触らない実装にしてあった)。

    `extra` は FTS(title / body)にも `doc_tags` にも関わらないので、
    ここだけ直接書いても索引は食い違わない。
    """
    path = require_path()
    conn = _connect(path)
    try:
        with conn:
            cur = conn.execute(
                "UPDATE docs SET extra = ? WHERE doc_id = ?", (dump_extra(extra), doc_id)
            )
            return cur.rowcount > 0
    finally:
        conn.close()


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


def parse_recall_fields(fields: str | None) -> list[str]:
    """`fields` を項目の並びに直す。空なら全項目(これまでの応答と同じ)。"""
    if not fields:
        return list(RECALL_FIELDS)
    requested = [f.strip() for f in fields.split(",") if f.strip()]
    if not requested:
        return list(RECALL_FIELDS)
    unknown = [f for f in requested if f not in RECALL_ALLOWED_FIELDS]
    if unknown:
        raise HTTPException(
            400,
            {
                "error": f"unknown fields: {', '.join(unknown)}",
                "allowed_fields": list(RECALL_ALLOWED_FIELDS),
            },
        )
    return requested


def recall(
    q: str | None = None,
    since: str | None = None,
    until: str | None = None,
    tag: str | None = None,
    limit: int = RECALL_LIMIT_DEFAULT,
    offset: int = 0,
    fields: str | None = None,
    max_chars: int = RECALL_MAX_CHARS_DEFAULT,
) -> dict:
    """メモを思い出す。新しい順に返す。

    全文検索(`q`)は trigram なので「あの件」のような曖昧な問いには当たらない。
    そのため `q` を省くと時系列だけで引ける形にしてある(「さっき話したあの件」は
    語ではなく時刻で引くほうが確実)。`since` / `until` は `updated_at` との
    文字列比較なので、`2026-07-31` でも `2026-07-31T12:00:00+00:00` でも渡せる。

    本文は既定で `RECALL_MAX_CHARS_DEFAULT` 文字に切り、切ったものには
    `truncated: true` を立てる(黙って切ると「これで全部」と読まれる)。
    全文は `/v1/notes/doc/{doc_id}` で取り直す。`max_chars=0` で切らない。

    上限はここで担保する。REST の `Query(ge=1, le=…)` は HTTP の口にしか効かず、
    MCP(`app/mcp_server.py`)は api の関数を Python から直接呼ぶので通らない。
    SQLite は `LIMIT -1` を「無制限」と解釈するため、負の値がそのまま届くと
    全件返る(頁を送る意図の呼び出しが静かに全件取得になる)。同じ理由で
    `max_chars` の負値もここで 0 に丸める(負の添字は末尾を削る意味になる)。
    """
    path = require_path()
    ensure_db()
    limit = max(1, min(int(limit), RECALL_LIMIT_MAX))
    offset = max(0, int(offset))
    field_list = parse_recall_fields(fields)
    max_chars = max(0, int(max_chars))
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
        "SELECT d.doc_id, d.title, d.body, d.tags, d.extra, d.updated_at"
        f" FROM docs d{clause}"
        " ORDER BY d.updated_at DESC, d.doc_id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    return {
        "source": SOURCE_NAME,
        "total": total,
        "offset": offset,
        "notes": [_recall_note(r, field_list, max_chars) for r in rows],
    }


def _recall_note(row, fields: list[str], max_chars: int) -> dict:
    values = {
        "doc_id": row["doc_id"],
        "title": row["title"],
        "text": row["body"],
        "tags": json.loads(row["tags"] or "[]"),
        "extra": load_extra(row["extra"]),
        "updated_at": row["updated_at"],
        "url": doc_url(SOURCE_NAME, row["doc_id"]),
    }
    note: dict = {}
    truncated = False
    for name in fields:
        value = values[name]
        if name == "text" and max_chars and value and len(value) > max_chars:
            value = value[:max_chars]
            truncated = True
        note[name] = value
    if truncated:
        note["truncated"] = True
    return note
