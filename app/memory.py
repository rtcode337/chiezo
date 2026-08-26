"""記憶の固化(短期記憶 → 長期記憶)。

短期記憶(`app/notes.py`)に溜めたもののうち、確定したものをテーマごとに読み取り専用の
ソースへ焼く層。焼くのは ingest で、ここがするのは素材を配ることだけ ——
別コンテナのプラグイン(`ingest/sources/remote.py`)とまったく同じ 2 つの口
(`GET {base}/sources` と `GET {base}/fetch?source=`)を話すので、DB の構築・FTS・
タグ転置表・世代切り替え・検証は本体の仕掛けがそのまま効く。新しい仕掛けを起こさない。

## 素材は「前世代 + 短期記憶の差分」

長期記憶も更新される —— 確定したつもりの知識は変わるし、消したくもなる。ところが
jawiki や geonames と違って、固化ソースには外に素材が無い(短期側を消した瞬間、
中身は焼いた DB の中にしか残らない)。そこで固化ソース自身を素材に含める:

    前世代の固化ソースの全文書 + 短期記憶の未固化ぶん(同じ見出しは短期側が勝つ)

こうすると 1 本のフローに追加・更新・削除が全部乗る:

- 追加 … 短期記憶に書く
- 更新 … 長期側と同じ見出しで短期記憶に書く(焼くとき短期側が勝って上書きされる)
- 削除 … 同じ見出し + 墓標のタグ(`notes.TOMBSTONE_TAG`)。対象ごと落ち、墓標も焼かない

その場で書き換えるのではなく毎回作り直すので、焼き損じてもブルーグリーンの前世代へ
戻せる。読み取り専用という約束はここでも崩れていない —— `immutable=1` が守るのは
「開いている間に変わらない」ことで、不変であることではない(jawiki も再構築で変わる)。

## 印を付けるのは焼いた後

焼き上がりを確かめてから短期側に `notes.CONSOLIDATED_TAG` を付ける(`sweep`)。
条件は「意図どおり長期側へ反映されていること」—— 通常のメモは同じ見出しが長期側に
あること、墓標は無くなっていること。焼く前に押しても何も起きないので、実際には
移っていないのに印だけが付く事故が起きない。印が付いたものは `recall` の既定から
外れる(思い出す先が長期側に移ったため)。短期側から実際に消すのはその後の話で、
消しても素材は長期側にある。
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from app import db, notes, settings_store
from app.jst import to_jst

log = logging.getLogger("chiezo.app")

STATE_FILE = "memory.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS themes (
    name       TEXT PRIMARY KEY,
    label      TEXT NOT NULL,
    tags       TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# テーマ名はそのままソース名(と世代ファイル名)になるので、取り込み側と同じ制限。
# `-` は世代ファイル名 `<source>-<date>.db` の区切りと衝突する。
_SOURCE_NAME = re.compile(r"^[A-Za-z0-9_]+$")

# 検証の最低文書数。固化ソースは数十件から始まるので、ダンプ由来のソースのような
# 「最低◯万件」は課せない。1 件でも焼けることを許し、0 件は呼ぶ側で断る。
MIN_DOCS = 1

# 検証に使う代表タイトルの数(取り込み後にこの見出しが引けるかを ingest が確かめる)。
SAMPLE_TITLES = 3


@dataclass
class Theme:
    """固化の単位。1 テーマ = 1 つの読み取り専用ソース。"""

    name: str
    label: str
    tags: list[str] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""


def db_path() -> Path | None:
    d = settings_store.state_dir()
    return d / STATE_FILE if d else None


def is_enabled() -> bool:
    return db_path() is not None


def require_path() -> Path:
    path = db_path()
    if path is None:
        raise HTTPException(
            503,
            {
                "error": "memory consolidation is disabled",
                "hint": "書き込み可能なディレクトリを CHIEZO_STATE_DIR に設定すると有効になる",
            },
        )
    return path


def _connect() -> sqlite3.Connection:
    """テーマの置き場への接続。

    `settings.db` に相乗りしないのは、あちらを CLI ブリッジのコンテナが読み取り専用で
    マウントして読むため(使用量・失敗ログを別ファイルにしてあるのと同じ判断)。
    """
    path = require_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _row_to_theme(row: sqlite3.Row) -> Theme:
    return Theme(
        name=row["name"],
        label=row["label"],
        tags=notes.split_tags(row["tags"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def themes() -> list[Theme]:
    """定義済みのテーマ。無効なら空(呼ぶ側で 503 にしない)。"""
    if not is_enabled():
        return []
    conn = _connect()
    try:
        rows = conn.execute("SELECT * FROM themes ORDER BY name").fetchall()
    finally:
        conn.close()
    return [_row_to_theme(r) for r in rows]


def get_theme(name: str) -> Theme | None:
    if not is_enabled():
        return None
    conn = _connect()
    try:
        row = conn.execute("SELECT * FROM themes WHERE name = ?", (name,)).fetchone()
    finally:
        conn.close()
    return _row_to_theme(row) if row else None


def require_theme(name: str) -> Theme:
    theme = get_theme(name)
    if theme is None:
        raise HTTPException(404, {"error": f"unknown theme: {name}"})
    return theme


def add_theme(name: str, label: str | None, tags: str | None, taken: set[str] | None = None) -> Theme:
    """テーマを 1 つ足す。

    `taken` には登録済みのソース名を渡す。テーマ名はそのままソース名になるので、
    先に取られている名前(jawiki など)を弾く —— 通してしまうと、焼いた瞬間に
    別のソースを置き換えることになる。
    """
    name = (name or "").strip()
    if not _SOURCE_NAME.match(name):
        raise HTTPException(
            400,
            {
                "error": f"invalid theme name: {name}",
                "hint": "使えるのは英数字とアンダースコアだけ(世代ファイル名の区切りと衝突するため)",
            },
        )
    tag_list = notes.split_tags(tags)
    if not tag_list:
        raise HTTPException(
            400,
            {
                "error": "tags must not be empty",
                "hint": "そのテーマへ焼くメモを選ぶ条件。複数を渡すと全部を持つメモだけが対象になる",
            },
        )
    if taken and name in taken and get_theme(name) is None:
        raise HTTPException(409, {"error": f"source name is already taken: {name}"})
    now = _now()
    conn = _connect()
    try:
        with conn:
            conn.execute(
                "INSERT INTO themes (name, label, tags, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(name) DO UPDATE SET label = excluded.label,"
                " tags = excluded.tags, updated_at = excluded.updated_at",
                (name, (label or "").strip() or name, ",".join(tag_list), now, now),
            )
    finally:
        conn.close()
    return require_theme(name)


def remove_theme(name: str) -> bool:
    """テーマの定義を消す。焼いた DB はそのまま残る(消すのは取り込み側の仕事)。"""
    if not is_enabled():
        return False
    conn = _connect()
    try:
        with conn:
            cur = conn.execute("DELETE FROM themes WHERE name = ?", (name,))
    finally:
        conn.close()
    return cur.rowcount > 0


# ---- プラグインとしての口(ingest/sources/remote.py の契約) --------------------


def catalog() -> dict:
    """`GET {base}/sources` の中身。

    テーマが 1 つも無ければ空で返す。取り込み側は空を正常として扱う
    (テーマを作るまで何も配らないのが起動直後の普通の姿)。
    """
    return {
        "sources": [
            {
                "name": t.name,
                "kind": "memory",
                "label": t.label,
                "min_docs": MIN_DOCS,
            }
            for t in themes()
        ]
    }


def _previous(name: str, sources: dict) -> dict[str, dict]:
    """前世代の固化ソースの全文書(見出し → 文書)。まだ焼いていなければ空。"""
    src = sources.get(name)
    if src is None:
        return {}
    rows = db.query(
        src.path, "SELECT doc_id, title, opening, body, tags, updated_at FROM docs"
    )
    out: dict[str, dict] = {}
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except ValueError:
            tags = []
        out[row["title"]] = {
            "doc_id": row["doc_id"],
            "title": row["title"],
            "opening": row["opening"],
            "body": row["body"],
            "tags": [str(t) for t in tags],
            "updated_at": row["updated_at"],
        }
    return out


def pending(theme: Theme) -> list[dict]:
    """短期記憶のうち、そのテーマの対象でまだ固化していないもの。

    次に焼けば長期側へ入るぶん。画面が「焼く候補が何件あるか」を出すのにも使う。
    """
    path = notes.notes_path()
    if path is None or not path.exists() or not theme.tags:
        return []
    clause = " AND ".join(
        "d.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)" for _ in theme.tags
    )
    rows = db.query(
        path,
        "SELECT d.doc_id, d.title, d.body, d.tags, d.updated_at FROM docs d"
        f" WHERE {clause}"
        " AND d.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " ORDER BY d.doc_id",
        (*theme.tags, notes.CONSOLIDATED_TAG),
    )
    out: list[dict] = []
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except ValueError:
            tags = []
        out.append({
            "doc_id": row["doc_id"],
            "title": row["title"],
            "body": row["body"] or "",
            "tags": [str(t) for t in tags],
            "updated_at": row["updated_at"],
        })
    return out


def _burnable_tags(tags: list[str]) -> list[str]:
    """長期側へ持っていくタグ。段取りのための 2 つだけ落とす。

    `固化` は短期側の状態で、長期側では全員がそうなので意味を持たない。
    `削除` は指示であって知識ではない(そもそも墓標は焼かれない)。
    """
    return [t for t in tags if t not in (notes.CONSOLIDATED_TAG, notes.TOMBSTONE_TAG)]


def material(theme: Theme, sources: dict) -> list[dict]:
    """焼く素材(前世代 + 短期記憶の差分)を doc_id 順に組み立てる。

    `doc_id` は前世代のものを引き継ぐ。焼き直しても文書 URL
    (`/search/<source>/doc/<id>`)が変わらないようにするため。
    """
    merged = _previous(theme.name, sources)
    next_id = max((d["doc_id"] for d in merged.values()), default=0) + 1
    for note in pending(theme):
        title = note["title"]
        if notes.TOMBSTONE_TAG in note["tags"]:
            merged.pop(title, None)
            continue
        previous = merged.get(title)
        if previous is None:
            doc_id = next_id
            next_id += 1
        else:
            doc_id = previous["doc_id"]
        body = note["body"]
        merged[title] = {
            "doc_id": doc_id,
            "title": title,
            "opening": body[:notes.TITLE_MAX_CHARS * 4],
            "body": body,
            "tags": _burnable_tags(note["tags"]),
            "updated_at": note["updated_at"],
        }
    return sorted(merged.values(), key=lambda d: d["doc_id"])


def _dump_date(theme: Theme, sources: dict) -> str:
    """世代ファイル名になる値(JST・秒まで)。

    日付だけだと、1 日に何度も焼く固化では 2 回目が同じファイル名になり、
    切り替えが前世代を上書きして戻り先が消える。秒まで入れてもまだ足りない ——
    現行世代と同じ値になるときは 1 秒進める(実際にテストが同じ秒で 2 回焼いて踏んだ)。
    """
    now = to_jst(datetime.now(UTC))
    stamp = now.strftime("%Y%m%d%H%M%S")
    current = sources.get(theme.name)
    if current is not None and current.dump_date == stamp:
        stamp = (now + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    return stamp


def ndjson(theme: Theme, sources: dict) -> str:
    """`GET {base}/fetch?source=` の中身(1 行目が meta、以降は 1 行 1 文書)。

    ストリームにせず組み立ててから返す。素材が空なら 409 で断りたいが、流し始めた
    後ではステータスを変えられない(SSE と同じ理由)。固化ソースはたかだか数千件で、
    ダンプのように数十 GB を運ぶわけではないので、先に全部作って構わない。
    """
    docs = material(theme, sources)
    if not docs:
        # 空になる理由は 2 つあり、次にすることが違う。素材が無いのか、
        # 残る文書が 1 件も無い(墓標で全部落ちた)のか。
        if pending(theme):
            raise HTTPException(
                409,
                {
                    "error": f"consolidating would empty the source: {theme.name}",
                    "hint": "墓標で全部落ちる。丸ごと不要ならテーマの定義ごと消す",
                },
            )
        raise HTTPException(
            409,
            {
                "error": f"nothing to consolidate for theme: {theme.name}",
                "hint": f"タグ {'/'.join(theme.tags)} を持つ未固化のメモがない",
            },
        )
    meta = {
        "meta": {
            "dump_date": _dump_date(theme, sources),
            "min_docs": MIN_DOCS,
            "sample_titles": [d["title"] for d in docs[:SAMPLE_TITLES]],
        }
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines += [json.dumps(doc, ensure_ascii=False) for doc in docs]
    return "\n".join(lines) + "\n"


# ---- 焼いた後の片付け --------------------------------------------------------


def sweep(theme: Theme, sources: dict) -> dict:
    """焼き上がりを確かめて、短期側に固化の印を付ける。

    印の条件は「意図どおり長期側へ反映されていること」。通常のメモは同じ見出しが
    長期側にあること、墓標は無くなっていること —— 焼く前に呼んでも何も起きない。
    """
    src = sources.get(theme.name)
    if src is None:
        raise HTTPException(
            409,
            {
                "error": f"not consolidated yet: {theme.name}",
                "hint": "先に固化(取り込み)を実行する。焼き上がる前に印だけ付けない",
            },
        )
    titles = {row["title"] for row in db.query(src.path, "SELECT title FROM docs")}
    marked: list[str] = []
    waiting: list[str] = []
    for note in pending(theme):
        tombstone = notes.TOMBSTONE_TAG in note["tags"]
        reflected = (note["title"] not in titles) if tombstone else (note["title"] in titles)
        if not reflected:
            waiting.append(note["title"])
            continue
        tags = [*note["tags"], notes.CONSOLIDATED_TAG]
        notes.update(note["doc_id"], tags=",".join(tags))
        marked.append(note["title"])
    return {
        "theme": theme.name,
        "source": theme.name,
        "marked": len(marked),
        "titles": marked,
        # 焼かれていない(= 固化の後に書かれた)ぶん。次に焼けば入る
        "pending": len(waiting),
    }
