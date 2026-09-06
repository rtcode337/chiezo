"""collect — AI に集めさせて、引ける形で溜めていく層。

## 何のためか

Wikipedia や OSM のような**まとまったダンプが無い**ことは知りたい、という穴を埋める。
たとえば直近のニュース、飲食店のような入れ替わりの速い情報、人物の関係。
どれも「1 回引いて終わり」ではなく、**繰り返し少しずつ溜まっていく**のが本質なので、
取り込み(ingest)のブルーグリーン(全件洗い替え)には乗らない。

## 置き方の決めごと

- **溜まる先は長期記憶(`corpus/`)。焼くのは ingest**。固化(`app/memory.py`)と
  まったく同じ形にしてある —— 素材を配るのはこちら、焼くのは向こう。
  **長期記憶へ書けるのは ingest だけ**という線を、この層のためにも崩さない
  (`chiezo-app` は `/data/corpus` を読み取り専用で重ねてマウントしている)。
- **毎回焼き直すが、中身は積み上がる**。素材が「**前世代 + 新しく集めたぶん**」なので、
  ブルーグリーン(全件の作り直し)に乗せたまま追記として振る舞う。固化と同じ仕掛けで、
  世代は今と 1 つ前だけが残る(`ingest/main.py` の `switch_db`)。
- **集めたものは焼くまで待ち行列に置く**(`CHIEZO_COLLECT_DIR`。既定 `/data/collect`)。
  notes には混ぜない —— 短期記憶が収集物で何千件も埋まると `recall` も画面も
  使い物にならなくなる。待ち行列は**ソースとして登録しない**(引けるのは焼いた後)。
- **待ち行列のスキーマもコアスキーマ**(notes と同じ写し)。焼く素材をそのまま
  組み立てられるようにするため。読み手は `mode=ro`。
- **同じ見出しが再び来たら飛ばす**(重複を増やさない)。**見出しが重複の鍵**で、
  焼くときも前世代の同じ見出しを置き換える。
- **定義は notes の 1 件に JSON でまとめて持つ**(`app/tasks.py` のプロジェクトと同じ流儀)。
  収集ごとに 1 メモにすると短期記憶に並んで邪魔になるうえ、並び順を持てない。
- **時計は Chiezo が持つ**(`due_collections` を叩く常駐タスク)。ホストの cron に
  出さないのは、間隔を画面から変えられるようにするため。**次にいつ走るかも持つ**ので、
  画面は「30 分ごと / 次は 14:20」と出せる。
- **焼くのは ingest。素材を配るのがこちら**(`catalog` / `ndjson` / `sweep`)。
  契約は固化(`app/memory.py`)と同じで、違うのは**1 つではなく可変**なところ ——
  収集は実行時に増えるので、ingest 側は `ADAPTERS` に固定で並べずカタログを聞きに来る。
- **片付けは焼き上がりを確かめてから**(`sweep`)。先に消すと、焼きに失敗したときに
  集めたものが失われる。

## 収集の 1 回

`prompt` の `{cursor}` を今のカーソルで置き換えて AI に投げ、返った JSON を溜める。

    {"items": [{"title": …, "body": …, "tags": […], "url": …}], "next_cursor": "…"}

**カーソルが要**。これがあるから「実行ごとに先へ進む」が表せる:

| 例 | カーソルに入るもの |
|---|---|
| ニュース | 前回集めた時点(AI に「それ以降」を頼む) |
| 飲食店 | 次に回る地域名(東京 → 隣接県 → …) |
| 人物の関係 | 次に調べる人の待ち行列 |

`next_cursor` を返さない収集(毎回同じことを聞く)はカーソルが動かないだけで、
仕組みとしては同じ。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from app import db, notes
from app.jst import to_jst
from app.pages import doc_url

log = logging.getLogger("chiezo.app")

SOURCE_KIND = "collect"

# 定義をまとめて持つメモ(notes 側)。プロジェクトと同じく 1 件に配列で持つ
DEFS_TITLE = "収集"
DEFS_TAG = "収集"
DEFS_BROKEN = "収集のメモが JSON として読めません"

# 収集ソースの名前に使える文字。**ファイル名とソース名とURLになる**ので狭く取る
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

# 間隔の下限。AI を呼ぶので、分より短い間隔を許すと枠を焼くだけになる
MIN_INTERVAL_MINUTES = 5

# 1 回の収集で受け取る件数の上限。多すぎる答えは切って捨てる(次の回で続けられる)
MAX_ITEMS_PER_RUN = 200

# 本文の上限。1 件がこれを超えるものは切る(引くための索引であって全文の保管庫ではない)
MAX_BODY_CHARS = 20_000

# 最初から置いておく見本。**止めた状態で置く** —— 有効なものを黙って足すと、
# 設定した覚えのない AI の呼び出しが枠を食う。画面の「有効にする」で動き出す。
#
# 見本を 1 つ置くのは、この層がプロンプト次第でどうにでもなるぶん、
# **何をどう書けばよいかが分からないと始められない**ため(空の画面から
# `{cursor}` の使い方は思いつかない)。消したければ普通に削除できる。
SAMPLE_NAME = "news"
SAMPLE = {
    "name": SAMPLE_NAME,
    "description": "ニュース(見本。有効にすると6時間ごとに、押さえておくべきものを集める)",
    "prompt": (
        "{cursor} 以降に出たニュースのうち、**日本で暮らす人が押さえておくべきもの**を10件、"
        "重要な順に。\n"
        "政治・経済・災害・事故・事件・国際情勢と、暮らしに影響する制度や価格の変更を優先する。"
        "芸能・ゴシップ・スポーツの勝敗・個人の炎上は入れない。\n"
        "title は見出し(同じ話題は同じ見出しにする)、"
        "body は3〜4文で「何が起きたか」と「なぜ押さえておくべきか」、"
        "tags は分野を1〜2個(政治 / 経済 / 災害 / 事件 / 国際 / 社会 / 科学 など)、"
        "url は出典。\n"
        "next_cursor には、いちばん新しいニュースの日付を YYYY-MM-DD で入れる。"
    ),
    "interval_minutes": 360,
}


def collect_dir() -> Path | None:
    """溜め先。**未設定なら機能ごと無効**(notes の `CHIEZO_NOTES_DIR` と同じ流儀)。"""
    raw = os.environ.get("CHIEZO_COLLECT_DIR", "").strip()
    return Path(raw) if raw else None


def is_enabled() -> bool:
    # 定義の置き場が notes なので、notes が無効なら収集も成り立たない
    return collect_dir() is not None and notes.is_enabled()


def require_dir() -> Path:
    directory = collect_dir()
    if directory is None or not notes.is_enabled():
        raise HTTPException(
            503,
            {
                "error": "collection is disabled",
                "hint": "CHIEZO_COLLECT_DIR(溜め先)と CHIEZO_NOTES_DIR(定義の置き場)"
                        "の両方を設定すると有効になる",
            },
        )
    return directory


def db_path(name: str) -> Path:
    return require_dir() / f"{name}.db"


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# ---- 定義(notes の 1 件に JSON で持つ)----------------------------------------


@dataclass(frozen=True)
class Collection:
    """1 つの収集の定義と、その進み具合。"""

    name: str
    description: str
    prompt: str
    interval_minutes: int
    enabled: bool
    # 話す相手。未指定なら Chiezo の既定(`/v1/ai/backends` の先頭)
    backend: str | None
    model: str | None
    effort: str | None
    # web 検索を開けるか。**既定は開ける** —— 外の情報を集めるための層なので
    web: bool
    # 実行ごとに進む印。`prompt` の `{cursor}` に入り、AI が `next_cursor` で返す
    cursor: str
    created_at: str
    updated_at: str
    # 誰が置いたか。外のアプリが名乗った文字列で、**印であって認証ではない**
    # (LAN 内・認証なしの前提なので偽れる)。有効にするか決める人の手がかり
    requested_by: str = ""
    # ここから下は実行のたびに書き換わる控え
    last_run_at: str | None = None
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None
    last_added: int = 0
    last_skipped: int = 0
    next_run_at: str | None = None

    def due_at(self) -> datetime:
        """次に走る時刻。持っていなければ「いますぐ」。"""
        return _parse(self.next_run_at) or _now()

    def is_due(self, at: datetime | None = None) -> bool:
        return self.enabled and self.due_at() <= (at or _now())


def _defs_row():
    """定義をまとめたメモ。まだ 1 件も作っていなければ None。

    タグで引くのは `app/tasks.py` と同じ形。あちらの `_rows_tagged` を借りずに
    ここで書いているのは、collect が tasks(やること層)に依存する理由が無いため。
    """
    path = notes.require_path()
    notes.ensure_db()
    rows = db.query(
        path,
        "SELECT doc_id, title, body FROM docs"
        " WHERE doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)",
        (DEFS_TAG,),
    )
    for row in rows:
        if row["title"] == DEFS_TITLE:
            return row
    return None


def _from_json(item: dict) -> Collection:
    return Collection(
        name=str(item.get("name") or ""),
        description=str(item.get("description") or ""),
        prompt=str(item.get("prompt") or ""),
        interval_minutes=max(int(item.get("interval_minutes") or 60), MIN_INTERVAL_MINUTES),
        enabled=bool(item.get("enabled", True)),
        backend=item.get("backend") or None,
        model=item.get("model") or None,
        effort=item.get("effort") or None,
        web=bool(item.get("web", True)),
        cursor=str(item.get("cursor") or ""),
        requested_by=str(item.get("requested_by") or ""),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
        last_run_at=item.get("last_run_at") or None,
        last_status=item.get("last_status") or None,
        last_error=item.get("last_error") or None,
        last_added=int(item.get("last_added") or 0),
        last_skipped=int(item.get("last_skipped") or 0),
        next_run_at=item.get("next_run_at") or None,
    )


def _to_json(items: list[Collection]) -> str:
    return json.dumps(
        {"collections": [c.__dict__ for c in items]},
        ensure_ascii=False,
        indent=2,
    )


def load() -> list[Collection]:
    """定義の一覧(並びは配列の順)。

    **本文が壊れていたら黙って作り直さない** —— 中身ごと消えるので、読めないことを
    見せて人に直させる(プロジェクトと同じ判断)。
    """
    row = _defs_row()
    if row is None:
        return []
    try:
        payload = json.loads(row["body"] or "{}")
        raw = payload["collections"]
        if not isinstance(raw, list):
            raise ValueError("collections must be a list")
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, {"error": f"{DEFS_BROKEN}: {e}"}) from None
    return [_from_json(i) for i in raw if isinstance(i, dict)]


def save(items: list[Collection]) -> None:
    body = _to_json(items)
    row = _defs_row()
    if row is None:
        notes.add(text=body, title=DEFS_TITLE, tags=DEFS_TAG)
        return
    notes.update(row["doc_id"], text=body, title=DEFS_TITLE, tags=DEFS_TAG)


def get(name: str) -> Collection:
    for item in load():
        if item.name == name:
            return item
    raise HTTPException(404, {"error": f"収集「{name}」がありません"})


def _replace_one(name: str, updated: Collection) -> None:
    items = load()
    save([updated if c.name == name else c for c in items])


def create(
    name: str,
    prompt: str,
    interval_minutes: int,
    description: str = "",
    backend: str | None = None,
    model: str | None = None,
    effort: str | None = None,
    web: bool = True,
    requested_by: str = "",
) -> Collection:
    if not NAME_RE.match(name):
        raise HTTPException(400, {
            "error": "name は英小文字で始まる 2〜31 文字(英小文字・数字・_)にしてください",
            "reason": "ソース名・ファイル名・URL にそのまま使うため",
        })
    if not prompt.strip():
        raise HTTPException(400, {"error": "prompt must not be empty"})
    existing = load()
    if any(c.name == name for c in existing):
        raise HTTPException(409, {"error": f"収集「{name}」はすでにあります"})
    if name in {notes.SOURCE_NAME, "memory"}:
        raise HTTPException(400, {"error": f"「{name}」は既存のソース名なので使えません"})
    now = _iso(_now())
    item = Collection(
        name=name,
        description=description,
        prompt=prompt,
        interval_minutes=max(interval_minutes, MIN_INTERVAL_MINUTES),
        # **必ず止めた状態で作る**。作るのは「依頼」で、動かすかは Chiezo 側が決める ——
        # 外のアプリが作った収集がその場で走り出すと、頼んでいない AI の呼び出しが
        # 枠を食う(悪意が無くても、試しに叩いただけで走ってしまう)。
        # 有効にできるのは管理画面だけ(`enabled` は REST から触れない)
        enabled=False,
        backend=backend,
        model=model,
        effort=effort,
        web=web,
        cursor="",
        requested_by=requested_by.strip()[:80],
        created_at=now,
        updated_at=now,
        # 有効にした時点で 1 回目が走るよう、予定は「いま」にしておく
        next_run_at=now,
    )
    save([*existing, item])
    ensure_db(name)
    return item


def ensure_sample() -> None:
    """見本の収集を、まだ 1 件も無いときだけ置く。

    **止めた状態で置く**(`enabled=False`)。有効なものを黙って足すと、設定した
    覚えのない AI の呼び出しが枠を食う。画面の「有効にする」で動き出す。

    **一度でも定義があれば触らない** —— 見本を消した人に、起動のたびに
    押し付け直すことになるため(消せない見本は見本ではない)。
    """
    if not is_enabled():
        return
    try:
        if load():
            return
    except HTTPException:
        # 定義が壊れているときは触らない(直すのは人の仕事)
        return
    now = _iso(_now())
    item = Collection(
        name=SAMPLE["name"],
        description=SAMPLE["description"],
        prompt=SAMPLE["prompt"],
        interval_minutes=SAMPLE["interval_minutes"],
        enabled=False,
        backend=None,
        model=None,
        effort=None,
        web=True,
        cursor="",
        requested_by="見本",
        created_at=now,
        updated_at=now,
        next_run_at=now,
    )
    save([item])
    log.info("collect: placed the sample collection (%s, disabled)", item.name)


def update(name: str, **fields) -> Collection:
    """渡した項目だけを差し替える。間隔を変えたら次回の予定も引き直す。"""
    current = get(name)
    allowed = {
        "description", "prompt", "interval_minutes", "enabled",
        "backend", "model", "effort", "web", "cursor",
    }
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "interval_minutes" in patch:
        patch["interval_minutes"] = max(int(patch["interval_minutes"]), MIN_INTERVAL_MINUTES)
    updated = replace(current, **patch, updated_at=_iso(_now()))
    if ("interval_minutes" in patch or "enabled" in patch) and current.last_run_at:
        # 間隔を縮めたのに次回が遠いままだと、変えた実感が出ない。前回から測り直す。
        # **一度も走っていないものは動かさない** —— 作った直後は「すぐ 1 回目」が
        # 入っていて、間隔を直しただけで初回が先送りになるのは意図に反する
        base = _parse(current.last_run_at) or _now()
        updated = replace(
            updated,
            next_run_at=_iso(base + timedelta(minutes=updated.interval_minutes)),
        )
    _replace_one(name, updated)
    return updated


def remove(name: str, drop_data: bool = False) -> None:
    """定義を消す。**溜めたものは既定で残す** —— 定義を消すのと、集めたものを

    捨てるのは別の意思決定だから(間違えて消したときに取り返しがつかない)。
    """
    items = load()
    if not any(c.name == name for c in items):
        raise HTTPException(404, {"error": f"収集「{name}」がありません"})
    save([c for c in items if c.name != name])
    if drop_data:
        path = collect_dir() / f"{name}.db" if collect_dir() else None
        if path and path.exists():
            path.unlink()
            log.info("collect: dropped data for %s", name)


# ---- 溜め先(コアスキーマの DB。notes と同じ形)---------------------------------


def _connect(path: Path) -> sqlite3.Connection:
    """書き込み用の接続(notes と同じ。WAL で読み手を止めずに追記する)。"""
    conn = sqlite3.connect(path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=10000")
    return conn


def ensure_db(name: str) -> Path:
    """収集先の DB が無ければ作る。スキーマは notes と同じコアスキーマ。"""
    path = db_path(name)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = _connect(path)
    try:
        with conn:
            conn.executescript(notes.SCHEMA_DDL)
            conn.executescript(notes.INDEX_DDL)
            # 時系列で引きたい(最近集めたものから見たい)ので notes と同じ索引を張る
            conn.executescript(notes.NOTES_INDEX_DDL)
            conn.execute(
                "INSERT INTO meta (source, source_kind, lang, dump_date, schema_version, built_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (name, SOURCE_KIND, None, None, notes.SCHEMA_VERSION, _iso(_now())),
            )
    finally:
        conn.close()
    log.info("collect: created db at %s", path)
    return path


def append(name: str, items: list[dict]) -> tuple[int, int]:
    """集めたものを追記する。返すのは (足した数, 飛ばした数)。

    **同じ見出しが既にあれば飛ばす**。notes は衝突したら `(doc_id)` を足して別物として
    残すが、こちらは繰り返し同じことを聞く前提なので、それでは同じニュースが実行のたびに
    増える。**見出しが重複の鍵**という割り切り。
    """
    path = ensure_db(name)
    now = _iso(_now())
    added = skipped = 0
    conn = _connect(path)
    try:
        with conn:
            for item in items[:MAX_ITEMS_PER_RUN]:
                title = (item.get("title") or "").strip()[:notes.TITLE_MAX_CHARS]
                body = (item.get("body") or "").strip()[:MAX_BODY_CHARS]
                if not title or not body:
                    skipped += 1
                    continue
                if conn.execute("SELECT 1 FROM docs WHERE title = ?", (title,)).fetchone():
                    skipped += 1
                    continue
                (doc_id,) = conn.execute(
                    "SELECT COALESCE(MAX(doc_id), 0) + 1 FROM docs"
                ).fetchone()
                tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()]
                extra = {}
                url = (item.get("url") or "").strip()
                if url:
                    extra["url"] = url
                # いつ集めたかは必ず残す(古い情報かどうかを読む側が判断できるように)
                extra["collected_at"] = now
                conn.execute(
                    "INSERT INTO docs (doc_id, title, opening, body, tags, links, updated_at,"
                    " rank_score, extra) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id, title, body[:notes.TITLE_MAX_CHARS * 4], body,
                        json.dumps(tags, ensure_ascii=False), None, now, 0.0,
                        json.dumps(extra, ensure_ascii=False),
                    ),
                )
                # external content なので FTS には手で入れる(notes と同じ)
                conn.execute(
                    "INSERT INTO docs_fts(rowid, title, body) VALUES (?, ?, ?)",
                    (doc_id, title, body),
                )
                for tag in tags:
                    conn.execute(
                        "INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id)
                    )
                    conn.execute(
                        "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
                        " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
                        (tag,),
                    )
                added += 1
    finally:
        conn.close()
    return added, skipped


def count(name: str) -> int:
    """溜まっている件数。まだ 1 件も無ければ 0。

    **走査時の値ではなく数え直す**(notes と同じ)。収集の DB は `/data` の指紋に
    入らないので、走査のきっかけが起きず `doc_count` が古いまま残る。
    """
    path = collect_dir() / f"{name}.db" if collect_dir() else None
    if path is None or not path.exists():
        return 0
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error:
        return 0
    try:
        (total,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        return int(total)
    except sqlite3.Error:
        return 0
    finally:
        conn.close()


def sample(name: str, limit: int = 5) -> list[dict]:
    """直近に集めたもの(新しい順)。画面が「何が入ったか」を見せるのに使う。"""
    path = collect_dir() / f"{name}.db" if collect_dir() else None
    if path is None or not path.exists():
        return []
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return []
    try:
        rows = conn.execute(
            "SELECT doc_id, title, updated_at FROM docs ORDER BY updated_at DESC, doc_id DESC"
            " LIMIT ?",
            (max(1, min(limit, 50)),),
        ).fetchall()
        return [
            {
                "doc_id": r["doc_id"],
                "title": r["title"],
                "updated_at": r["updated_at"],
                "url": doc_url(name, r["doc_id"]),
            }
            for r in rows
        ]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


# ---- 走らせる ------------------------------------------------------------------


SYSTEM_PROMPT = (
    "集めた情報を JSON だけで返す。前置き・説明・コードブロックの記号は付けない。"
    " 形式: {\"items\":[{\"title\":\"見出し\",\"body\":\"本文\","
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\"}],\"next_cursor\":\"次に進む印\"}"
    " title は重複の鍵になるので、同じものを指す見出しは同じ文字列にする。"
    " 出典が分かるものは url を必ず入れる。分からない項目は null。"
)


def build_messages(item: Collection) -> list[dict]:
    """AI へ渡す本文。`{cursor}` を今のカーソルで置き換える。

    **カーソルが空でも壊さない**(初回は空文字が入るだけ)。テンプレートに `{cursor}` が
    無い収集は、毎回同じことを聞く形になる。
    """
    user = item.prompt.replace("{cursor}", item.cursor or "(まだ無し。最初から)")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


DRAFT_SYSTEM = (
    "あなたは、情報収集の指示文(プロンプト)を書く手伝いをする。"
    "書いた指示文は、web 検索のできる AI に定期的に投げられ、返った JSON がデータベースに"
    "溜まっていく。次の決まりを必ず守る指示文にすること。\n"
    "- 返させる形は {\"items\":[{\"title\",\"body\",\"tags\",\"url\"}],\"next_cursor\"}\n"
    "- title は重複の鍵。同じものを指す見出しは同じ文字列になるよう頼む\n"
    "- 指示文には {cursor} を入れる。**実行のたびに前回の続きへ進む印**で、"
    "AI が next_cursor で次の値を返す(「前回以降の日付」「次に回る地域」"
    "「次に調べる人」など、集めるものに合う進み方を決める)\n"
    "- 1回に集める件数を書く\n"
    "出力は**指示文そのものだけ**。前置き・見出し・コードブロックの記号・"
    "「以下が指示文です」のような説明は一切付けない。"
)


def build_draft_messages(
    want: str, current: str = "", feedback: str = ""
) -> list[dict]:
    """プロンプトを相談するときの本文。

    **前の案と追加の注文を渡す**ので、何度でも詰められる(1 往復ずつだが、
    前の案を持ち回るぶん会話として続く)。相談は Chiezo が状態を持たない作りに
    合わせて、毎回まるごと渡す —— `/v1/chat` が履歴を毎回受け取るのと同じ流儀。
    """
    parts = [f"集めたいもの: {want.strip()}"]
    if current.strip():
        parts.append(f"\nいまの指示文:\n{current.strip()}")
    if feedback.strip():
        parts.append(f"\n直してほしいところ: {feedback.strip()}")
    parts.append("\nこれを踏まえた指示文を書いて。")
    return [
        {"role": "system", "content": DRAFT_SYSTEM},
        {"role": "user", "content": "\n".join(parts)},
    ]


def clean_draft(content: str) -> str:
    """相談の答えを指示文として使える形に整える。

    **コードブロックの記号だけ外す**。中身は直さない —— 手を入れると、画面に出るものと
    実際に投げるものが食い違う(そのまま貼れることが相談の価値)。
    """
    text = re.sub(r"^\s*```(?:\w+)?\s*|\s*```\s*$", "", content.strip())
    return text.strip()


def parse_response(content: str) -> tuple[list[dict], str | None]:
    """AI の答えから items と next_cursor を取り出す。

    前置きやコードブロックが混ざっても拾えるように、`{` 〜 `}` を切り出してから読む
    (小型モデルでなくても、この手の付け足しは普通に起きる)。
    """
    stripped = re.sub(r"```(?:json)?", "", content).strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("JSON オブジェクトが見つかりません")
    payload = json.loads(stripped[start : end + 1])
    if not isinstance(payload, dict):
        raise ValueError("トップレベルがオブジェクトではありません")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("items が配列ではありません")
    next_cursor = payload.get("next_cursor")
    return (
        [i for i in items if isinstance(i, dict)],
        str(next_cursor) if isinstance(next_cursor, (str, int, float)) and next_cursor else None,
    )


def record_result(
    name: str,
    *,
    status: str,
    added: int = 0,
    skipped: int = 0,
    error: str | None = None,
    next_cursor: str | None = None,
) -> Collection:
    """1 回ぶんの結果を定義側へ書き戻し、次回の予定を入れる。

    **失敗しても次回の予定は入れる** —— 入れないと、一度こけた収集が二度と走らなくなる。
    """
    current = get(name)
    now = _now()
    updated = replace(
        current,
        cursor=next_cursor if next_cursor is not None else current.cursor,
        last_run_at=_iso(now),
        last_status=status,
        last_error=(error or "")[:500] or None,
        last_added=added,
        last_skipped=skipped,
        next_run_at=_iso(now + timedelta(minutes=current.interval_minutes)),
        updated_at=_iso(now),
    )
    _replace_one(name, updated)
    return updated


def due_collections(at: datetime | None = None) -> list[Collection]:
    """いま走らせるべき収集(予定の早い順)。無効なものは含まない。"""
    if not is_enabled():
        return []
    now = at or _now()
    return sorted(
        (c for c in load() if c.is_due(now)),
        key=lambda c: c.due_at(),
    )


def to_public(item: Collection) -> dict:
    """画面と REST に返す形。**溜まっている件数と次回の予定を必ず入れる**

    (「30 分ごと・次は 14:20・いま 128 件」まで見えて初めて、動いているか判断できる)。
    """
    return {
        **item.__dict__,
        "docs": count(item.name),
        "url": f"/search/{item.name}/",
    }


# ---- 焼く素材を配る(ingest が取りに来る。固化とまったく同じ契約)--------------
#
# `ingest/sources/remote.py` が読む形:
#   GET {base}/sources           → {"sources": [...]}
#   GET {base}/fetch?source=NAME → NDJSON(1 行目が meta、以降 1 行 1 文書)


def catalog() -> list[dict]:
    """焼ける収集の一覧(ingest のカタログに出る形)。

    **定義があれば、まだ 1 件も集めていなくても出す** —— 一覧に無いと
    「取り込めるソース」として管理画面に並ばず、焼く導線が出ない。
    """
    if not is_enabled():
        return []
    try:
        items = load()
    except HTTPException:
        return []
    return [
        {
            "name": c.name,
            "kind": SOURCE_KIND,
            "lang": "ja",
            "label": c.description or c.name,
            # 集め始めは数件なので、ダンプ由来のような下限は課せない
            "min_docs": 1,
            # 1 行ずつ流し込むだけ。件数に関わらずメモリは増えない
            "memory_gb": 0.5,
        }
        for c in items
    ]


def _previous(name: str, sources: dict) -> dict[str, dict]:
    """前世代(焼き上がっている `corpus/` 側)の全文書(見出し → 文書)。

    **これが「毎回焼き直すのに積み上がる」の要**。素材に前世代を混ぜるので、
    ブルーグリーンの全件作り直しに乗せたまま追記として振る舞う(固化と同じ)。
    """
    src = sources.get(name)
    if src is None:
        return {}
    rows = db.query(
        src.path,
        "SELECT doc_id, title, opening, body, tags, updated_at, extra FROM docs",
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
            "extra": load_json(row["extra"]),
        }
    return out


def load_json(raw) -> dict | None:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) else None


def staged(name: str) -> list[dict]:
    """待ち行列に溜まっている文書(焼く前のもの)。"""
    path = collect_dir() / f"{name}.db" if collect_dir() else None
    if path is None or not path.exists():
        return []
    rows = db.query(
        path,
        "SELECT doc_id, title, opening, body, tags, updated_at, extra FROM docs"
        " ORDER BY doc_id",
    )
    out = []
    for row in rows:
        try:
            tags = json.loads(row["tags"] or "[]")
        except ValueError:
            tags = []
        out.append({
            "title": row["title"],
            "opening": row["opening"],
            "body": row["body"],
            "tags": [str(t) for t in tags],
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        })
    return out


def material(name: str, sources: dict) -> list[dict]:
    """焼く素材(前世代 + 待ち行列)を doc_id 順に組み立てる。

    **`doc_id` は前世代のものを引き継ぐ** —— 焼き直しても文書の URL が変わらないように
    (固化と同じ)。同じ見出しは待ち行列の側で置き換える(新しく集めたほうが正しい)。
    """
    merged = _previous(name, sources)
    next_id = max((d["doc_id"] for d in merged.values()), default=0) + 1
    for doc in staged(name):
        title = doc["title"]
        previous = merged.get(title)
        if previous is None:
            doc_id = next_id
            next_id += 1
        else:
            doc_id = previous["doc_id"]
        merged[title] = {**doc, "doc_id": doc_id}
    return sorted(merged.values(), key=lambda d: d["doc_id"])


def _dump_date(name: str, sources: dict) -> str:
    """世代ファイル名になる値(JST・秒まで)。

    1 日に何度も焼くので日付だけでは足りず、**現行世代と同じ秒なら 1 秒進める**
    (同じ名前だと切り替えが前世代を上書きして、戻り先が消える。固化で踏んだ罠)。
    """
    now = to_jst(datetime.now(UTC))
    stamp = now.strftime("%Y%m%d%H%M%S")
    current = sources.get(name)
    if current is not None and current.dump_date == stamp:
        stamp = (now + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    return stamp


def ndjson(name: str, sources: dict) -> str:
    """取り込み側が読む素材(1 行目が meta、以降は 1 行 1 文書)。

    **空なら 409 で断る** —— 流し始めた後ではステータスを変えられないので、
    先に全部組み立ててから返す(固化と同じ判断。収集物はたかだか数万件)。
    """
    get(name)  # 知らない収集は 404
    docs = material(name, sources)
    if not docs:
        raise HTTPException(
            409,
            {
                "error": f"収集「{name}」にはまだ焼くものがありません",
                "hint": "「いま走らせる」で 1 回集めてから焼いてください",
            },
        )
    meta = {
        "meta": {
            "dump_date": _dump_date(name, sources),
            "min_docs": 1,
            "sample_titles": [docs[0]["title"]],
        }
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines.extend(json.dumps(d, ensure_ascii=False) for d in docs)
    return "\n".join(lines) + "\n"


def sweep(name: str, sources: dict) -> dict:
    """焼き上がりを確かめて、待ち行列から片付ける。

    **焼く前に押しても何も起きない**(長期側に入っていないものは残す)。
    固化の `sweep` と同じ考え方で、**移せたことを確かめてから**消す ——
    先に消すと、焼きに失敗したときに集めたものが失われる。
    """
    src = sources.get(name)
    if src is None:
        return {"source": name, "cleared": 0, "remaining": count(name)}
    baked = {row["title"] for row in db.query(src.path, "SELECT title FROM docs")}
    path = db_path(name)
    cleared = 0
    conn = _connect(path)
    try:
        with conn:
            for row in conn.execute("SELECT doc_id, title FROM docs").fetchall():
                if row["title"] not in baked:
                    continue
                doc_id = row["doc_id"]
                conn.execute("DELETE FROM docs WHERE doc_id = ?", (doc_id,))
                # external content なので FTS からも手で落とす(notes と同じ)
                conn.execute(
                    "INSERT INTO docs_fts(docs_fts, rowid, title, body)"
                    " VALUES ('delete', ?, ?, ?)",
                    (doc_id, row["title"], ""),
                )
                conn.execute("DELETE FROM doc_tags WHERE doc_id = ?", (doc_id,))
                cleared += 1
            # タグの集計は残った行から作り直す(1 件ずつ引くより確実)
            conn.execute("DELETE FROM tag_counts")
            conn.execute(
                "INSERT INTO tag_counts (tag, docs)"
                " SELECT tag, COUNT(*) FROM doc_tags GROUP BY tag"
            )
    finally:
        conn.close()
    return {"source": name, "cleared": cleared, "remaining": count(name)}


def register_mutable(paths: list) -> None:
    """待ち行列の DB を「追記される」と db 側へ伝える(`scan_all` から呼ぶ)。

    **ソースとしては登録しない** —— 引けるのは ingest が焼いた後の `corpus/` 側で、
    待ち行列は焼く前の置き場でしかない。ただし `db.query` で読むので、
    `immutable=1` で開かれないようにする必要はある(notes と同じ理由)。
    """
    directory = collect_dir()
    if directory is None:
        return
    paths.extend(sorted(directory.glob("*.db")))
