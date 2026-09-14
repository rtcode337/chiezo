"""記憶の固化(短期記憶 → 長期記憶)。

短期記憶(`app/notes.py`)に溜めたもののうち、**残す価値があると判断したもの**を
読み取り専用のソース 1 つ(`memory`)へ焼く層。人が眠っている間に海馬の内容を
大脳へ移すのと同じ役回りで、判断は 1 件ずつ行う:

    短期記憶に書く
      → 残す価値があるものに `_chiezo_consolidate` を付ける(人でも AI でもよい)
      → 固化を実行(普通の取り込みとして走る)
      → 焼き上がりを確かめて `_chiezo_consolidated` に付け替える

**判断の口は用意していない**。`_chiezo_consolidate` はただのタグなので、MCP の `update` で
付けられる —— AI に「短期記憶を順に見て、残す価値があるものに印を付けて」と
頼めばそのまま回る。専用の口を足すと、同じことをする経路が 2 つになる。

## 焼くのは ingest、配るのがここ

素材を配る口は取り込み側のプラグイン契約(`ingest/sources/remote.py`)と同じ形にして
ある。**ソースの定義は ingest 側が持つ**(`ingest/sources/memory.py` が `ADAPTERS` に
入っている)ので、設定を足さなくても管理画面の一覧に出るし、`SOURCE=memory` で CLI
からも回せる。DB の構築・FTS・タグ転置表・世代切り替え・検証は本体の仕掛けがそのまま効く。

## 素材は「前世代 + 印の付いたメモ」

長期記憶も更新される —— 確定したつもりの知識は変わるし、消したくもなる。ところが
jawiki や geonames と違って、このソースには外に素材が無い(短期側を消した瞬間、
中身は焼いた DB の中にしか残らない)。そこで自分自身を素材に含める:

    前世代の `memory` の全文書 + `_chiezo_consolidate` のメモ(同じ見出しは短期側が勝つ)

こうすると 1 本のフローに追加・更新・削除が全部乗る:

- 追加 … 短期記憶に書いて `_chiezo_consolidate` を付ける
- 更新 … 長期側と同じ見出しのメモに `_chiezo_consolidate` を付ける(焼くとき短期側が勝つ)
- 削除 … そのメモに墓標のタグ(`notes.TOMBSTONE_TAG`)も付ける。対象ごと落ち、墓標も焼かない

その場で書き換えるのではなく毎回作り直すので、焼き損じてもブルーグリーンの前世代へ
戻せる。読み取り専用という約束はここでも崩れていない —— `immutable=1` が守るのは
「開いている間に変わらない」ことで、不変であることではない(jawiki も再構築で変わる)。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import HTTPException

from app import db, notes
from app.jst import to_jst

log = logging.getLogger("chiezo.app")

# 焼き先のソース名。**1 つだけ**にしてある —— タグごとに分けていた頃は、テーマの
# 定義と管理の口がその数だけ要ったが、引くときは結局まとめて引きたい
# (どのテーマに入れたかを覚えていないと探せないのでは、長期記憶として使えない)。
SOURCE_NAME = "memory"

# 検証の最低文書数。数十件から始まるので、ダンプ由来のソースのような「最低◯万件」は
# 課せない。1 件でも焼けることを許し、0 件は呼ぶ側で断る。
MIN_DOCS = 1

# 検証に使う代表タイトルの数(取り込み後にこの見出しが引けるかを ingest が確かめる)。
SAMPLE_TITLES = 3


def is_enabled() -> bool:
    """短期記憶があれば固化もできる。**専用の置き場を持たない**。

    テーマを持っていた頃は定義の置き場(`state/memory.db`)が要ったが、焼き先が
    1 つに決まったので設定そのものが無くなった。
    """
    return notes.is_enabled()


def long_term_path() -> Path | None:
    """焼いた長期記憶のファイル。**登録表を通さずに引く**。

    やること層は外に出す面(`chiezo-tasks`)からも動き、あちらは `/data` を走査しない
    (読むのは短期記憶だけ)。固化したタスクとルールを読むのにそこまで要らないので、
    ファイルを直に指す。**焼く前は None**(まだ 1 件も移していない)。
    """
    raw = os.environ.get("CHIEZO_DATA_DIR", "").strip()
    if not raw:
        return None
    path = Path(raw) / f"{SOURCE_NAME}.db"
    return path if path.exists() else None


def pending() -> list[dict]:
    """次に焼かれるもの —— 移す印が付いていて、まだ移し終えていないメモ。"""
    return _tagged(notes.CONSOLIDATE_TAG, without=notes.CONSOLIDATED_TAG)


def consolidated() -> list[dict]:
    """**移し終えたメモ**。長期側に同じ見出しがあるので、短期から消してよいもの。"""
    return _tagged(notes.CONSOLIDATED_TAG)


def _tagged(tag: str, without: str | None = None) -> list[dict]:
    path = notes.notes_path()
    if path is None or not path.exists():
        return []
    sql = (
        "SELECT d.doc_id, d.title, d.body, d.tags, d.updated_at FROM docs d"
        " WHERE d.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
    )
    args: list[str] = [tag]
    if without is not None:
        sql += " AND d.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        args.append(without)
    rows = db.query(path, sql + " ORDER BY d.doc_id", tuple(args))
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


def _previous(sources: dict) -> dict[str, dict]:
    """前世代の全文書(見出し → 文書)。まだ焼いていなければ空。"""
    src = sources.get(SOURCE_NAME)
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


def _burnable_tags(tags: list[str]) -> list[str]:
    """長期側へ持っていくタグ。段取りのための 3 つだけ落とす。

    `_chiezo_consolidate` と `_chiezo_consolidated` は短期側の状態で、長期側では全員がそうなので意味を持たない。
    `_chiezo_tombstone` は指示であって知識ではない(そもそも墓標は焼かれない)。
    """
    dropped = (notes.CONSOLIDATE_TAG, notes.CONSOLIDATED_TAG, notes.TOMBSTONE_TAG)
    return [t for t in tags if t not in dropped]


def material(sources: dict) -> list[dict]:
    """焼く素材(前世代 + 印の付いたメモ)を doc_id 順に組み立てる。

    `doc_id` は前世代のものを引き継ぐ。焼き直しても文書 URL
    (`/search/memory/doc/<id>`)が変わらないようにするため。
    """
    merged = _previous(sources)
    next_id = max((d["doc_id"] for d in merged.values()), default=0) + 1
    for note in pending():
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


def _dump_date(sources: dict) -> str:
    """世代ファイル名になる値(JST・秒まで)。

    日付だけだと、1 日に何度も焼く固化では 2 回目が同じファイル名になり、
    切り替えが前世代を上書きして戻り先が消える。秒まで入れてもまだ足りない ——
    現行世代と同じ値になるときは 1 秒進める(実際にテストが同じ秒で 2 回焼いて踏んだ)。
    """
    now = to_jst(datetime.now(UTC))
    stamp = now.strftime("%Y%m%d%H%M%S")
    current = sources.get(SOURCE_NAME)
    if current is not None and current.dump_date == stamp:
        stamp = (now + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    return stamp


def ndjson(sources: dict) -> str:
    """取り込み側が読む素材(1 行目が meta、以降は 1 行 1 文書)。

    ストリームにせず組み立ててから返す。素材が空なら 409 で断りたいが、流し始めた
    後ではステータスを変えられない(SSE と同じ理由)。長期記憶はたかだか数千件で、
    ダンプのように数十 GB を運ぶわけではないので、先に全部作って構わない。
    """
    docs = material(sources)
    if not docs:
        # 空になる理由は 2 つあり、次にすることが違う。素材が無いのか、
        # 残る文書が 1 件も無い(墓標で全部落ちた)のか。
        if pending():
            raise HTTPException(
                409,
                {
                    "error": "consolidating would empty the long-term memory",
                    "hint": "墓標で全部落ちる。丸ごと消すなら DB を消す",
                },
            )
        raise HTTPException(
            409,
            {
                "error": "nothing to consolidate",
                "hint": f"`{notes.CONSOLIDATE_TAG}` を付けたメモがない",
            },
        )
    meta = {
        "meta": {
            "dump_date": _dump_date(sources),
            "min_docs": MIN_DOCS,
            "sample_titles": [d["title"] for d in docs[:SAMPLE_TITLES]],
        }
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines += [json.dumps(doc, ensure_ascii=False) for doc in docs]
    return "\n".join(lines) + "\n"


def mark_consolidated(sources: dict) -> dict:
    """焼き上がりを確かめて、短期側の印を「移す」から「移した」へ付け替える。

    **固化が済んだ時点で自分から動く**(`main.refresh_sources` が長期側の変化に
    気づいたら呼ぶ)。人が押して回る手順にしていた頃は、焼けているのに印が
    「まだ移していない」のまま残り、次の固化で同じものをもう一度焼いていた。

    印の条件は変えない —— 「意図どおり長期側へ反映されていること」。通常のメモは
    同じ見出しが長期側にあること、墓標は無くなっていること。**焼く前に呼んでも
    何も起きない**ので、反映されていないのに印だけ付く事故は起きないまま。
    """
    src = sources.get(SOURCE_NAME)
    if src is None:
        raise HTTPException(
            409,
            {
                "error": "not consolidated yet",
                "hint": "先に固化(取り込み)を実行する。焼き上がる前に印だけ付けない",
            },
        )
    titles = {row["title"] for row in db.query(src.path, "SELECT title FROM docs")}
    marked: list[str] = []
    waiting: list[str] = []
    for note in pending():
        tombstone = notes.TOMBSTONE_TAG in note["tags"]
        reflected = (note["title"] not in titles) if tombstone else (note["title"] in titles)
        if not reflected:
            waiting.append(note["title"])
            continue
        tags = [t for t in note["tags"] if t != notes.CONSOLIDATE_TAG]
        tags.append(notes.CONSOLIDATED_TAG)
        notes.update(note["doc_id"], tags=",".join(tags))
        marked.append(note["title"])
    return {
        "source": SOURCE_NAME,
        "marked": len(marked),
        "titles": marked,
        # 焼かれていない(= 固化の後に付けた)ぶん。次に焼けば入る
        "pending": len(waiting),
    }


def catch_up(sources: dict) -> int:
    """固化が済んでいれば印を付け替える。**待っているものが無ければ何もしない**。

    長期側が変わるたびに呼ばれる(`main.refresh_sources`)ので、**ここが軽いことが
    要る** —— 付け替える相手が居ないうちから長期側の見出しを全部読むと、
    どのソースを焼き直しても数十万件の読み出しが 1 回増える。
    """
    if not is_enabled() or SOURCE_NAME not in sources or not pending():
        return 0
    try:
        return mark_consolidated(sources)["marked"]
    except HTTPException:
        return 0


def sweep(sources: dict) -> dict:
    """**移し終えたメモを、短期記憶から消す。**

    短期記憶は「思い出す」ための場所なので、長期側へ移したものが残り続けると、
    同じ内容が 2 か所にある状態が積み上がる —— 直すときにどちらが正か言えなくなるし、
    件数も見かけ上増え続ける。**移った先には同じ見出しの文書がある**ので、
    消えるのは控えのほうだけ。

    **消すのは印が付いているものだけ**(`mark_consolidated` が、長期側に反映されて
    いることを確かめてから付ける)。**取り消せない**ので、押す側には確認を出す。
    """
    if not is_enabled():
        raise HTTPException(503, {"error": "notes storage is disabled"})
    removed: list[str] = []
    for note in consolidated():
        if notes.delete(note["doc_id"]):
            removed.append(note["title"])
    return {
        "source": SOURCE_NAME,
        "removed": len(removed),
        "titles": removed,
        # まだ焼かれていないぶん(消す対象ではない)
        "pending": len(pending()),
    }


def status(sources: dict) -> dict:
    """画面と REST に出す状態。"""
    src = sources.get(SOURCE_NAME)
    return {
        "enabled": is_enabled(),
        "source": SOURCE_NAME,
        "pending": len(pending()),
        # 移し終えて、短期から消せるもの
        "swept": len(consolidated()),
        "consolidated": src is not None,
        "docs": src.doc_count if src is not None else 0,
        "built_at": src.built_at if src is not None else None,
        "tags": {"target": notes.CONSOLIDATE_TAG, "done": notes.CONSOLIDATED_TAG},
    }
