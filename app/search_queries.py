"""**検索文を AI に考えさせてから、外の道具で引く**回のための控え。

検索の RSS(`feed` の URL に `{query}` を書いたもの)は、**同じ検索文で引き直しても
同じ記事が返るだけ**になる —— 固定の検索文で回していた収集では、1 回目に 158 件
入ったあと、2 回目は 158 件すべてが既にあるもので弾かれた。そこで回ごとに

1. 前の回の検索文が、それぞれ何件の文書を連れてきたかを数える(文書に付く
   `<印>:<検索文>` のタグで数える。消されずに残った数も数える)
2. これまでの検索文と数を AI に見せて、**まだ試していない切り口**の検索文を考えさせる
3. その検索文で道具を回す(`feeds.expand`)

**AI の仕事は検索文を考えるだけ**で、記事を取ってくるのは機械。**使った検索文も
`reuse_days` が過ぎればまた使える** —— 二度と使えない形にすると、記事が増えたあとに
同じ切り口で拾い直す道が無くなる。どの相手に考えさせるかは
その巡回の相手・モデル・ワーカーで決まる。

**この回かどうかは依頼文が語る**(`thinks`)—— 機械で引く巡回の依頼文に `{queries}` が
あれば、そこへこれまでの検索文を差し込んで考えさせる。巡回の依頼文は空なら収集の
依頼文に倒れるので、「依頼文があるか」では見分けられない。

**控えは Chiezo が持つ**(`queries/<収集の名前>`。設定の置き場)。何を探しているかは
依頼文に書いてあり、控えは「どれを試してどれだけ入ったか」という事実だけなので、
呼ぶ側に持たせる理由が無い —— 持たせていた頃は、Chiezo で収集を作り直しても
呼ぶ側に前の収集の検索文が残り、新しい収集ではまだ走っていない検索文が
「0 件だった」と数えられて、最初の組が一度も走らないまま置き換わった。
**収集を消したら控えも消す**(`collect.remove`)。
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app import db, jst, machine_store, notes

KIND = "queries"

PLACEHOLDER = "{queries}"

# AI に見せるこれまでの検索文の数(新しい順)。**全部は見せない** ——
# 回を重ねるほど依頼文が伸びる。古いものは件数の傾向として十分伝わっている
MAX_SHOWN = 200
# 控えに残す数の天井。使った検索文を弾くのに要るので、見せる数より多く持つ
MAX_KEPT = 2000
# 1 つの検索文の長さ。**URL に入る**ので、長い文を許すと相手に断られる
MAX_QUERY_CHARS = 60

SYSTEM_PROMPT = (
    "外の検索に渡す検索文を考える。検索文だけを JSON の文字列の配列で返し、"
    "前置き・説明・コードブロックの記号は付けない。例: [\"検索文 1\", \"検索文 2\"]"
)


def thinks(sweep) -> bool:
    """この巡回は、検索文を AI に考えさせてから引く回か。"""
    return bool(sweep is not None and sweep.use_feed and PLACEHOLDER in (sweep.prompt or ""))


def history(name: str) -> list[dict]:
    """これまでの検索文(**新しい順**)。1 件は `query` / `used_at` / `found` / `kept` / `by`。

    読めなければ空 —— 控えは作り直せる(最初の組から始まるだけ)ので、
    定義と違って人に直させる価値が無い。
    """
    if not machine_store.is_enabled():
        return []
    try:
        rows = json.loads(machine_store.get(KIND, name) or "[]")
    except ValueError:
        return []
    return [r for r in rows if isinstance(r, dict) and isinstance(r.get("query"), str)]


def _save(name: str, rows: list[dict]) -> None:
    machine_store.put(KIND, name, json.dumps(rows[:MAX_KEPT], ensure_ascii=False, indent=2))


def current(name: str) -> list[str]:
    """いちばん新しい組(同じ時刻に使ったもの)。まだ無ければ空。"""
    rows = history(name)
    if not rows:
        return []
    last = rows[0].get("used_at")
    return [r["query"] for r in rows if r.get("used_at") == last]


def record(name: str, queries: list[str], by: str = "") -> None:
    """使った検索文を控える(新しいものを頭に)。"""
    now = datetime.now(UTC).isoformat(timespec="seconds")
    fresh = [{"query": q, "used_at": now, "found": None, "kept": None, "by": by} for q in queries]
    _save(name, fresh + history(name))


def forget(name: str, query: str | None = None) -> int:
    """控えを消す。`query` を渡せばその 1 つ、無ければ全部。消えた数を返す。

    **全部消すと、次の回は最初の組から始まる**(`plan` が控えの空を見る)。
    """
    rows = history(name)
    if query is None:
        if machine_store.is_enabled():
            machine_store.drop(KIND, name)
        return len(rows)
    kept = [r for r in rows if r["query"] != query]
    if len(kept) != len(rows):
        _save(name, kept)
    return len(rows) - len(kept)


def count(name: str, sources: dict, tag: str) -> None:
    """まだ数えていない検索文に、連れてきた文書の数を書き込む。

    **2 つ数える** —— 入った数(`found`)と、消されずに残った数(`kept`)。
    入った数だけを見せると、宣伝や的外れを大量に連れてくる検索文が「当たり」に見える
    (本文検索は語を含む記事をゆるく拾うので、実際に大半が外される回があった)。

    **ソースがまだ無ければ 0 件**(1 度も焼いていない)。**読めなければ数えない** ——
    読めないことと 0 件は別。
    """
    rows = history(name)
    if all(r.get("found") is not None for r in rows):
        return
    src = sources.get(name)
    counts: dict[str, tuple[int, int]] = {}
    if src is not None:
        head = tag + ":"
        try:
            found = db.query(
                src.path,
                "SELECT t.tag AS tag, COUNT(*) AS n,"
                " SUM(CASE WHEN t.doc_id IN (SELECT doc_id FROM doc_tags WHERE tag = ?)"
                " THEN 0 ELSE 1 END) AS kept"
                " FROM doc_tags t WHERE t.tag >= ? AND t.tag < ? GROUP BY t.tag",
                (notes.REMOVED_TAG, head, head + "\uffff"),
            )
        except Exception:  # 数えられなかった回は数えない(次の回でまた試す)
            return
        counts = {row["tag"][len(head):]: (row["n"], row["kept"]) for row in found}
    # **同じ検索文を何度か使ったら、前の回までに数えたぶんを引く**(タグは回をまたいで
    # 同じなので、そのまま数えると前の回に入った記事まで今回の手柄になる)。
    # 控えは新しい順なので、古いほうから数え直す
    for at in range(len(rows) - 1, -1, -1):
        row = rows[at]
        if row.get("found") is not None:
            continue
        n, kept = counts.get(row["query"], (0, 0))
        before = [r for r in rows[at + 1:] if r["query"] == row["query"]]
        row["found"] = max(0, n - sum(r.get("found") or 0 for r in before))
        row["kept"] = max(0, kept - sum(r.get("kept") or 0 for r in before))
    _save(name, rows)


def render(rows: list[dict], reuse_days: int | None = None) -> str:
    """`{queries}` に差し込む文。**新しい順・数つき**。

    **また使えるようになる日数も書く** —— 書かないと、AI は一覧にある検索文を
    すべて「もう使えない」と読み、当たった切り口を掘り直さない。
    """
    if not rows:
        return "(まだありません)"
    lines = []
    for row in rows[:MAX_SHOWN]:
        if row.get("found") is None:
            got = "まだ数えていない"
        else:
            got = f"{row['found']} 件入り、消されずに残ったのは {row.get('kept') or 0} 件"
        at = jst.parse(str(row.get("used_at") or ""))
        lines.append(f"- {row['query']}({got}" + (f"・{jst.format(at)}" if at else "") + ")")
    more = len(rows) - MAX_SHOWN
    text = "\n".join(lines) + (f"\n…(ほかに古いものが {more} 件)" if more > 0 else "")
    if reuse_days is not None:
        text += (
            f"\n(使ってから {reuse_days} 日が過ぎた検索文は、もう一度使えます。"
            f"{reuse_days} 日以内に使ったものを挙げても使われません)"
        )
    return text


def parse(content: str) -> list[str]:
    """答えから検索文の配列を拾う。**前置きや囲みがあっても拾う**(相手は守らないことがある)。"""
    found = re.search(r"\[.*\]", content or "", re.S)
    if not found:
        return []
    try:
        raw = json.loads(found.group(0))
    except ValueError:
        return []
    if not isinstance(raw, list):
        return []
    return [str(q).strip()[:MAX_QUERY_CHARS] for q in raw if isinstance(q, str | int) and str(q).strip()]


def fresh(name: str, proposed: list[str], limit: int, reuse_days: int = 0) -> list[str]:
    """**最近使っていない**検索文だけを、重ねず `limit` 個まで。

    `reuse_days` 日より前に使ったものは、また使える(0 なら使ったものもすぐ使える)。
    """
    cutoff = datetime.now(UTC) - timedelta(days=reuse_days)
    used = {
        r["query"] for r in history(name)
        if reuse_days > 0 and (at := jst.parse(str(r.get("used_at") or ""))) and at > cutoff
    }
    out: list[str] = []
    for q in proposed:
        q = " ".join(q.split())
        if q and q not in used and q not in out:
            out.append(q)
    return out[:limit]


def messages(prompt: str, name: str, reuse_days: int | None = None) -> list[dict]:
    """考えさせるときの本文(差し込み口を埋めたもの)。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt.replace(PLACEHOLDER, render(history(name), reuse_days))},
    ]


def nothing_new() -> HTTPException:
    """考えさせても使える検索文が出なかったとき。**最近の検索文のまま走らせない** ——
    同じ記事が返るだけで、取り込みを 1 本使って何も増えない。"""
    return HTTPException(409, {
        "error": "使える検索文が 1 つもありませんでした(最近使ったものか、読めない答えだけでした)",
        "hint": "依頼文を見直すか、相手を替えてから試してください",
    })
