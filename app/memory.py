"""記憶の固化(短期記憶 → 長期記憶)。

短期記憶(`app/notes.py`)に溜めたもののうち、**残す価値があると判断したもの**を
読み取り専用のソース 1 つ(`memory`)へ焼く層。人が眠っている間に海馬の内容を
大脳へ移すのと同じ役回りで、判断は 1 件ずつ行う:

    短期記憶に書く
      → 残す価値があるものに `固化対象` を付ける(人でも AI でもよい)
      → 固化を実行(普通の取り込みとして走る)
      → 焼き上がりを確かめて `固化` に付け替える

**判断の口は用意していない**。`固化対象` はただのタグなので、MCP の `update` で
付けられる —— AI に「短期記憶を順に見て、残す価値があるものに固化対象を付けて」と
頼めばそのまま回る。専用の口を足すと、同じことをする経路が 2 つになる。

## 焼くのは ingest、配るのがここ

素材を配る口は取り込み側のプラグイン契約(`ingest/sources/remote.py`)と同じ形にして
ある。**ソースの定義は ingest 側が持つ**(`ingest/sources/memory.py` が `ADAPTERS` に
入っている)ので、設定を足さなくても管理画面の一覧に出るし、`SOURCE=memory` で CLI
からも回せる。DB の構築・FTS・タグ転置表・世代切り替え・検証は本体の仕掛けがそのまま効く。

## 素材は「前世代 + 固化対象」

長期記憶も更新される —— 確定したつもりの知識は変わるし、消したくもなる。ところが
jawiki や geonames と違って、このソースには外に素材が無い(短期側を消した瞬間、
中身は焼いた DB の中にしか残らない)。そこで自分自身を素材に含める:

    前世代の `memory` の全文書 + `固化対象` のメモ(同じ見出しは短期側が勝つ)

こうすると 1 本のフローに追加・更新・削除が全部乗る:

- 追加 … 短期記憶に書いて `固化対象` を付ける
- 更新 … 長期側と同じ見出しのメモに `固化対象` を付ける(焼くとき短期側が勝つ)
- 削除 … そのメモに墓標のタグ(`notes.TOMBSTONE_TAG`)も付ける。対象ごと落ち、墓標も焼かない

その場で書き換えるのではなく毎回作り直すので、焼き損じてもブルーグリーンの前世代へ
戻せる。読み取り専用という約束はここでも崩れていない —— `immutable=1` が守るのは
「開いている間に変わらない」ことで、不変であることではない(jawiki も再構築で変わる)。
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

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


def pending() -> list[dict]:
    """次に焼かれるもの —— `固化対象` が付いていて、まだ `固化` でないメモ。"""
    path = notes.notes_path()
    if path is None or not path.exists():
        return []
    rows = db.query(
        path,
        "SELECT d.doc_id, d.title, d.body, d.tags, d.updated_at FROM docs d"
        " WHERE d.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " AND d.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " ORDER BY d.doc_id",
        (notes.CONSOLIDATE_TAG, notes.CONSOLIDATED_TAG),
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

    `固化対象` と `固化` は短期側の状態で、長期側では全員がそうなので意味を持たない。
    `削除` は指示であって知識ではない(そもそも墓標は焼かれない)。
    """
    dropped = (notes.CONSOLIDATE_TAG, notes.CONSOLIDATED_TAG, notes.TOMBSTONE_TAG)
    return [t for t in tags if t not in dropped]


def material(sources: dict) -> list[dict]:
    """焼く素材(前世代 + 固化対象)を doc_id 順に組み立てる。

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


def sweep(sources: dict) -> dict:
    """焼き上がりを確かめて、短期側の印を `固化対象` から `固化` に付け替える。

    印の条件は「意図どおり長期側へ反映されていること」。通常のメモは同じ見出しが
    長期側にあること、墓標は無くなっていること —— 焼く前に呼んでも何も起きない。
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


def status(sources: dict) -> dict:
    """画面と REST に出す状態。"""
    src = sources.get(SOURCE_NAME)
    return {
        "enabled": is_enabled(),
        "source": SOURCE_NAME,
        "pending": len(pending()),
        "consolidated": src is not None,
        "docs": src.doc_count if src is not None else 0,
        "built_at": src.built_at if src is not None else None,
        "tags": {"target": notes.CONSOLIDATE_TAG, "done": notes.CONSOLIDATED_TAG},
    }
