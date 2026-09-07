"""collect — AI に集めさせて、引ける形で溜めていく層。

## 何のためか

Wikipedia や OSM のような**まとまったダンプが無い**ことは知りたい、という穴を埋める。
たとえば直近のニュース、飲食店のような入れ替わりの速い情報、人物の関係。
どれも「1 回引いて終わり」ではなく、**繰り返し少しずつ溜まっていく**のが本質なので、
取り込み(ingest)のブルーグリーン(全件洗い替え)には乗らない。

## 置き方の決めごと

- **溜まる先は長期記憶(`corpus/`)。焼くのは ingest**。固化(`app/memory.py`)と
  まったく同じ形で、素材を配るのはこちら、焼くのは向こう。
  **長期記憶へ書けるのは ingest だけ**という線を、この層のためにも崩さない
  (`chiezo-app` は `/data/corpus` を読み取り専用で重ねてマウントしている)。
- **集めるのは焼くとき**(`/v1/collect/fetch` が呼ばれた瞬間に AI へ聞く)。
  **待ち行列を持たない** —— 素材が「前世代 + いま集めたぶん」なので、
  **焼くこと自体が積み上げの仕組み**になっていて、集めた時点と焼く時点を分ける理由が無い。
  分けていた頃は置き場・掃除の口・画面・環境変数が要り、「待ち行列と溜め先」という
  二重の概念まで抱えていた。
- **払った AI の呼び出しは無駄にしない**。取り込み側は返した NDJSON を他のダンプと同じく
  `dumps/` へ置き、焼きに失敗しても**次はそのファイルを読み直す**
  (`ingest/sources/collect.py`)。ダンプを落として構築する他のソースと同じ振る舞い。
- **毎回焼き直すが、中身は積み上がる**。ブルーグリーン(全件の作り直し)に乗せたまま
  追記として振る舞い、世代は今と 1 つ前だけが残る(`ingest/main.py` の `switch_db`)。
- **定義は notes の 1 件に JSON でまとめて持つ**(`app/tasks.py` のプロジェクトと同じ流儀)。
  収集ごとに 1 メモにすると短期記憶に並んで邪魔になるうえ、並び順を持てない。
  **集めたものは notes に入れない** —— 短期記憶が収集物で埋まると `recall` も画面も
  使い物にならなくなる(そもそも通り道に置かない)。
- **時計は Chiezo が持つ**(`due_collections`)。ホストの cron に出さないのは、間隔を
  画面から変えられるようにするため。**時計が叩くのは ingest**(chiezo-trigger)で、
  「集めて焼く」が 1 つの操作になっている。
- **同じ見出しは前世代を置き換える**。**見出しが重複の鍵**。

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
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app import db, notes
from app.jst import to_jst

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

# 集め方。**足すのか、作り直すのか**で、素材の作り方も要る守りも変わる。
#
# - append(集める): 前世代 + 今回のぶん。外から新しいものを取ってきて積む。
#   同じ見出しは置き換わるだけなので、失敗しても既にあるものは壊れない。
# - rebuild(整理する): 今回返ってきたものが**そのまま新しい全体**になる。
#   既にある内容を AI に読ませて、分類をやり直す・重複をまとめる・言い回しを揃える、
#   といった育て方をするためのもの。**落とされたものは消える**ので、
#   append には要らなかった歯止め(`keep_ratio`)がここで要る。
MODE_APPEND = "append"
MODE_REBUILD = "rebuild"
MODES = (MODE_APPEND, MODE_REBUILD)

# 作り直しのプロンプトに必ず入れてもらう印。ここに前世代の中身が差し込まれる。
# **無いまま作り直すと、AI は今ある内容を知らないまま「全体」を答える**ことになり、
# 育てたものが 1 回で消える。だから作るときに弾く
MATERIAL_PLACEHOLDER = "{current}"

# 作り直しで、前世代の何割を下回ったら焼くのを断るか。
# **既定で守る側に倒す** —— AI が変な日に当たった 1 回で、育てた分類が消えるのは重い。
# 意図して減らすときは、この値を下げるか 0 にして守りを外す(画面から変えられる)。
DEFAULT_KEEP_RATIO = 0.5

# 作り直しのときにプロンプトへ差し込む前世代の上限。
# **収まらなければ切って、切ったことを AI に伝える** —— 黙って切ると、
# 見えなかったぶんを「無かったもの」として落とした答えが返る。
MAX_MATERIAL_DOCS = 300
MAX_MATERIAL_CHARS = 40_000
# 差し込む 1 件の本文の長さ。全文を渡すと件数が入らない
MATERIAL_BODY_CHARS = 200

# 控えに残す「消えた見出し」の数。全部持つと定義のメモが太るので頭だけ
MAX_REMOVED_SAMPLE = 10

# 最初から置いておく見本。**止めた状態で置く** —— 有効なものを黙って足すと、
# 設定した覚えのない AI の呼び出しが枠を食う。画面の「有効にする」で動き出す。
#
# 見本を 1 つ置くのは、この層がプロンプト次第でどうにでもなるぶん、
# **何をどう書けばよいかが分からないと始められない**ため(空の画面から
# `{cursor}` の使い方は思いつかない)。消したければ普通に削除できる。
# **見本と分かる名前にする。** `news` のような普通の名前だと、あとから同じものを
# 作ろうとしたときにぶつかる(名前はソース名なので 1 つしか持てない)。
# **ハイフンは使えない** —— 世代ファイル名 `<source>-<date>.db` の区切りと衝突するため
# `NAME_RE` が弾く。
SAMPLE_NAME = "sample_news"
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


def require_enabled() -> None:
    """使える形になっていなければ断る。"""
    if not is_enabled():
        raise HTTPException(
            503,
            {
                "error": "collection is disabled",
                "hint": "CHIEZO_NOTES_DIR(収集の定義の置き場)と CHIEZO_TRIGGER_URL"
                        "(取り込みを起こす相手)を設定すると有効になる。"
                        "集めたものは長期記憶へ焼かれるので、途中の置き場は要らない",
            },
        )


def is_enabled() -> bool:
    """定義の置き場(notes)と、取り込みを起こす相手(chiezo-trigger)が揃っていること。

    **専用の置き場は持たない** —— 集めたものは焼くときに作って ingest へ渡すだけ。

    trigger を条件に入れるのは、**それが無いと集める手段が 1 つも無い**から。
    集めるのは取り込みの中で起きるので、取り込みを起こせない面
    (corpus を持たないタスク専用の面など)では定義を置いても永遠に走らない。
    そこで見本の定義まで作ると、**使えない機能の設定が短期記憶に 1 件混ざる**だけになる。
    """
    return notes.is_enabled() and bool(os.environ.get("CHIEZO_TRIGGER_URL", "").strip())


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
    # 集め方(`MODES`)。既定は足すほう —— 既にある定義の意味を変えない
    mode: str = MODE_APPEND
    # 作り直しで、前世代の何割を下回ったら断るか。0 なら守りを外す。
    # 足すほうでは使わない(そもそも減らないので)
    keep_ratio: float = DEFAULT_KEEP_RATIO
    # 誰が置いたか。外のアプリが名乗った文字列で、**印であって認証ではない**
    # (LAN 内・認証なしの前提なので偽れる)。有効にするか決める人の手がかり
    requested_by: str = ""
    # ここから下は実行のたびに書き換わる控え
    last_run_at: str | None = None
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None
    last_added: int = 0
    last_skipped: int = 0
    # 作り直しで消えた件数と、その見出しの頭のほう。
    # **消えたものが見えないとプロンプトを直せない** —— 件数だけでは
    # 「何が落ちたのか」が分からない
    last_removed: int = 0
    last_removed_titles: list[str] = field(default_factory=list)
    next_run_at: str | None = None

    def is_rebuild(self) -> bool:
        return self.mode == MODE_REBUILD

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
        mode=normalize_mode(item.get("mode")),
        keep_ratio=normalize_keep_ratio(item.get("keep_ratio")),
        requested_by=str(item.get("requested_by") or ""),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
        last_run_at=item.get("last_run_at") or None,
        last_status=item.get("last_status") or None,
        last_error=item.get("last_error") or None,
        last_added=int(item.get("last_added") or 0),
        last_skipped=int(item.get("last_skipped") or 0),
        last_removed=int(item.get("last_removed") or 0),
        last_removed_titles=[str(t) for t in (item.get("last_removed_titles") or [])],
        next_run_at=item.get("next_run_at") or None,
    )


def normalize_mode(value) -> str:
    """知らない集め方は足すほうへ倒す。壊れた定義でいきなり作り直させない。"""
    return value if value in MODES else MODE_APPEND


def normalize_keep_ratio(value) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_RATIO
    return min(max(ratio, 0.0), 1.0)


def check_prompt(mode: str, prompt: str) -> None:
    """作り直しのプロンプトに素材の差し込み口があるかを確かめる。

    **無いまま作り直させない**。AI は今ある内容を知らないまま「全体」を答えることに
    なり、返ってこなかったものは全部消える。作る時点で弾くのがいちばん安い。
    """
    if mode == MODE_REBUILD and MATERIAL_PLACEHOLDER not in prompt:
        raise HTTPException(400, {
            "error": f"作り直しのプロンプトには {MATERIAL_PLACEHOLDER} を入れてください",
            "reason": "ここへ今ある内容が差し込まれる。無いと、AI は今の内容を"
                      "知らないまま全体を答えることになり、返らなかったものは消える",
        })


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
    mode: str = MODE_APPEND,
    keep_ratio: float | None = None,
) -> Collection:
    if not NAME_RE.match(name):
        raise HTTPException(400, {
            "error": "name は英小文字で始まる 2〜31 文字(英小文字・数字・_)にしてください",
            "reason": "ソース名・ファイル名・URL にそのまま使うため",
        })
    if not prompt.strip():
        raise HTTPException(400, {"error": "prompt must not be empty"})
    if mode not in MODES:
        raise HTTPException(400, {"error": f"mode は {' / '.join(MODES)} のどれかにしてください"})
    check_prompt(mode, prompt)
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
        mode=mode,
        keep_ratio=(
            DEFAULT_KEEP_RATIO if keep_ratio is None else normalize_keep_ratio(keep_ratio)
        ),
        requested_by=requested_by.strip()[:80],
        created_at=now,
        updated_at=now,
        # 有効にした時点で 1 回目が走るよう、予定は「いま」にしておく
        next_run_at=now,
    )
    save([*existing, item])
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
        "backend", "model", "effort", "web", "cursor", "mode", "keep_ratio",
    }
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "interval_minutes" in patch:
        patch["interval_minutes"] = max(int(patch["interval_minutes"]), MIN_INTERVAL_MINUTES)
    if "mode" in patch and patch["mode"] not in MODES:
        raise HTTPException(400, {"error": f"mode は {' / '.join(MODES)} のどれかにしてください"})
    if "keep_ratio" in patch:
        patch["keep_ratio"] = normalize_keep_ratio(patch["keep_ratio"])
    # 集め方かプロンプトのどちらを変えても、組み合わせで確かめ直す ——
    # 片方だけ見ていると、作り直しへ切り替えたときに素材の差し込み口が無いまま通る
    check_prompt(
        patch.get("mode", current.mode),
        patch.get("prompt", current.prompt),
    )
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


def remove(name: str) -> None:
    """定義を消す。**焼いたものは残る**(長期記憶のソースとして)。

    定義を消すのと、集まったものを捨てるのは別の意思決定だから —— 後者は
    ソースの削除(管理画面の長期記憶の側)でやる。
    """
    items = load()
    if not any(c.name == name for c in items):
        raise HTTPException(404, {"error": f"収集「{name}」がありません"})
    save([c for c in items if c.name != name])


# ---- 溜め先(コアスキーマの DB。notes と同じ形)---------------------------------


SYSTEM_PROMPT = (
    "集めた情報を JSON だけで返す。前置き・説明・コードブロックの記号は付けない。"
    " 形式: {\"items\":[{\"title\":\"見出し\",\"body\":\"本文\","
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\"}],\"next_cursor\":\"次に進む印\"}"
    " title は重複の鍵になるので、同じものを指す見出しは同じ文字列にする。"
    " 出典が分かるものは url を必ず入れる。分からない項目は null。"
)


REBUILD_SYSTEM_PROMPT = (
    "既にある内容を整理し直して JSON だけで返す。前置き・説明・コードブロックの記号は付けない。"
    " 形式: {\"items\":[{\"title\":\"見出し\",\"body\":\"本文\","
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\"}],\"next_cursor\":\"次に進む印\"}"
    " **返したものがそのまま新しい全体になる。返さなかったものは消える。**"
    " 残すものは、手を入れないものも含めて必ず返すこと。"
    " title は同一性の鍵。同じものを指す見出しは同じ文字列にする。"
    " 分からない項目は null。"
)


def render_material(previous: dict[str, dict]) -> tuple[str, int]:
    """前世代を、プロンプトへ差し込める形にする。差し込んだ件数も返す。

    **入り切らなければ切って、切ったことを本文に書く** —— 黙って切ると、AI は
    見えなかったぶんを「無かったもの」として落とし、歯止めが無ければそのまま消える。
    """
    if not previous:
        return "(まだ何も入っていません。最初の内容を作ってください)", 0
    lines: list[str] = []
    used = 0
    docs = list(previous.values())
    for doc in docs[:MAX_MATERIAL_DOCS]:
        tags = "/".join(doc.get("tags") or [])
        body = (doc.get("body") or "")[:MATERIAL_BODY_CHARS].replace("\n", " ")
        line = f"- {doc['title']}" + (f" 【{tags}】" if tags else "") + (f" — {body}" if body else "")
        if used + len(line) > MAX_MATERIAL_CHARS:
            break
        lines.append(line)
        used += len(line)
    shown = len(lines)
    head = f"いまの内容(全 {len(docs)} 件"
    head += f"。うち {shown} 件だけ載せています)" if shown < len(docs) else ")"
    if shown < len(docs):
        head += "\n※ 載っていないものは今回の対象外です。載っているぶんだけを整理してください。"
    return head + ":\n" + "\n".join(lines), shown


def build_messages(item: Collection, previous: dict[str, dict] | None = None) -> list[dict]:
    """AI へ渡す本文。`{cursor}` を今のカーソルで、`{current}` を今ある内容で置き換える。

    **カーソルが空でも壊さない**(初回は空文字が入るだけ)。テンプレートに `{cursor}` が
    無い収集は、毎回同じことを聞く形になる。

    `{current}` は作り直し(整理)のためのもの。今ある内容を読ませて、分類をやり直す・
    重複をまとめる・言い回しを揃える、といった育て方をするときに使う。
    """
    user = item.prompt.replace("{cursor}", item.cursor or "(まだ無し。最初から)")
    if MATERIAL_PLACEHOLDER in user:
        material_text, _shown = render_material(previous or {})
        user = user.replace(MATERIAL_PLACEHOLDER, material_text)
    system = REBUILD_SYSTEM_PROMPT if item.is_rebuild() else SYSTEM_PROMPT
    return [
        {"role": "system", "content": system},
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
    removed: int = 0,
    removed_titles: list[str] | None = None,
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
        last_removed=removed,
        last_removed_titles=list(removed_titles or []),
        next_run_at=_iso(now + timedelta(minutes=current.interval_minutes)),
        updated_at=_iso(now),
    )
    _replace_one(name, updated)
    return updated


def mark_started(name: str) -> Collection:
    """取り込みを起こしたので、次回の予定だけ進める。

    **結果は控えない** —— 実際に集められたかは、取り込みが素材を取りに来たとき
    (`ndjson`)に分かる。ここで「成功」と書くと、起こしただけのものが成功に見える。
    """
    current = get(name)
    updated = replace(
        current,
        next_run_at=_iso(_now() + timedelta(minutes=current.interval_minutes)),
        updated_at=_iso(_now()),
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
    """画面と REST に返す形。**次回の予定を必ず入れる**

    (「30 分ごと・次は 14:20」まで見えて初めて、動いているか判断できる。
    溜まった件数は長期記憶の側にあるので、画面はソース表から取る)。
    """
    return {**item.__dict__, "url": f"/search/{item.name}/"}


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


def recent(name: str, sources: dict, limit: int = 5) -> list[dict]:
    """焼いてあるもののうち新しい順に何件か(何が集まっているかの手掛かり)。

    **見に行く先は長期記憶** —— 途中の置き場を持たないので、集めたものはここにしかない。
    まだ 1 度も焼いていなければ空(ソースそのものが無い)。
    """
    src = sources.get(name)
    if src is None or limit <= 0:
        return []
    rows = db.query(
        src.path,
        "SELECT title, opening, updated_at, extra FROM docs ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )
    return [
        {
            "title": row["title"],
            "opening": row["opening"],
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        }
        for row in rows
    ]


def previous_docs(name: str, sources: dict) -> dict[str, dict]:
    """前世代(焼き上がっている `corpus/` 側)の全文書(見出し → 文書)。

    **これが「毎回焼き直すのに積み上がる」の要**。足すほうでは素材に前世代を混ぜるので、
    ブルーグリーンの全件作り直しに乗せたまま追記として振る舞う(固化と同じ)。

    **作り直しでも要る** —— プロンプトへ差し込む素材であり、消えたものを数える相手であり、
    残ったものの `doc_id` を引き継ぐ元でもある。取り込みの経路では 1 度だけ読んで
    使い回す(同じものを 3 回読みに行かない)。
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


def material(
    item: Collection, previous: dict[str, dict], collected: list[dict]
) -> tuple[list[dict], dict]:
    """焼く素材と、前世代との差分を組み立てる。

    集め方で作り方が変わる:

    - **足す(append)**: 前世代 + 今回のぶん。同じ見出しは今回のほうで置き換える
      (そちらが新しいので正しい)。**前世代は必ず残る**ので、失敗しても壊れない。
    - **作り直す(rebuild)**: 今回のぶん**だけ**が新しい全体になる。前世代にあって
      今回返ってこなかった見出しは消える。整理そのものが目的なので、
      消えること自体は正しい振る舞い —— 消えすぎを止めるのは `shrink_blocked`。

    **`doc_id` は前世代のものを引き継ぐ**。どちらの集め方でも、残った文書の URL が
    焼き直しで変わらないようにするため。
    """
    merged = dict(previous) if not item.is_rebuild() else {}
    next_id = max((d["doc_id"] for d in previous.values()), default=0) + 1
    now = _iso(_now())
    added = skipped = 0
    for raw in collected[:MAX_ITEMS_PER_RUN]:
        doc = _to_doc(raw, now, item.web)
        if doc is None:
            skipped += 1
            continue
        title = doc["title"]
        if item.is_rebuild() and title in merged:
            # 同じ答えの中に同じ見出しが 2 つ。後から来たほうは捨てる
            # (足すほうでは「前世代と同じ」を意味するので、ここには来ない)
            skipped += 1
            continue
        kept_before = previous.get(title)
        if kept_before is None:
            doc_id = next_id
            next_id += 1
            added += 1
        else:
            # 既に持っている見出し。**上書きはする**(今回のほうが新しい)。
            # 足すほうでは積み上がった件数が増えないので「追加」には数えない
            doc_id = kept_before["doc_id"]
            if not item.is_rebuild():
                skipped += 1
        merged[title] = {**doc, "doc_id": doc_id}
    removed_titles = [t for t in previous if t not in merged]
    diff = {
        "previous": len(previous),
        "total": len(merged),
        "added": added,
        "kept": len(merged) - added,
        "removed": len(removed_titles),
        "skipped": skipped,
        "removed_titles": removed_titles[:MAX_REMOVED_SAMPLE],
    }
    return sorted(merged.values(), key=lambda d: d["doc_id"]), diff


def shrink_blocked(item: Collection, diff: dict) -> str | None:
    """作り直しで減りすぎていたら、その理由の文。問題なければ None。

    **AI が変な日に当たった 1 回で、育てた分類が消えるのを止める**のがここ。
    足すほうには要らない(そもそも減らない)。`keep_ratio` を 0 にすると外れる ——
    意図して大きく減らすときのための逃げ道で、外したことが定義に残る。
    """
    if not item.is_rebuild() or item.keep_ratio <= 0 or not diff["previous"]:
        return None
    floor = diff["previous"] * item.keep_ratio
    if diff["total"] >= floor:
        return None
    sample = "、".join(diff["removed_titles"][:5])
    return (
        f"作り直しの結果が {diff['total']} 件で、前の {diff['previous']} 件から"
        f"{diff['removed']} 件減ります(下限 {floor:.0f} 件)。"
        + (f"消えるもの: {sample} ほか。" if sample else "")
    )


def _to_doc(raw: dict, now: str, web: bool) -> dict | None:
    """AI が返した 1 件を、焼ける形に整える。見出しか本文が無いものは捨てる。

    **いつ集めたかは必ず残す**(`extra.collected_at`)—— 読む側が古い情報かどうかを
    判断できるように。

    **web を開けて集めたかも残す**(`extra.web`)。手元に置くだけなら私的利用の範囲でも、
    公開リポジトリへ出すかどうかは別の判断になる。後から「これは外から取ったものか」を
    辿れないと、その判断ができない(出典 `url` と合わせて手掛かりにする)。
    """
    title = (raw.get("title") or "").strip()[:notes.TITLE_MAX_CHARS]
    body = (raw.get("body") or "").strip()[:MAX_BODY_CHARS]
    if not title or not body:
        return None
    tags = [str(t).strip() for t in (raw.get("tags") or []) if str(t).strip()]
    extra = {"collected_at": now, "web": bool(web)}
    if url := (raw.get("url") or "").strip():
        extra["url"] = url
    return {
        "doc_id": 0,  # material が前世代から引き継ぐか、新しく振る
        "title": title,
        "opening": body[:notes.TITLE_MAX_CHARS * 4],
        "body": body,
        "tags": tags,
        "updated_at": now,
        "extra": extra,
    }


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


def ndjson(
    item: Collection, sources: dict, previous: dict[str, dict], collected: list[dict]
) -> tuple[str, dict]:
    """取り込み側が読む素材(1 行目が meta、以降は 1 行 1 文書)と、前世代との差分。

    **空なら 409 で断る** —— 流し始めた後ではステータスを変えられないので、
    先に全部組み立ててから返す(固化と同じ判断。収集物はたかだか数万件)。

    **作り直しで減りすぎていても断る**。ここが後戻りできる最後の地点で、
    通してしまうと次の世代が焼き上がり、戻すには世代を巻き戻すしかなくなる。
    """
    docs, diff = material(item, previous, collected)
    if not docs:
        raise HTTPException(
            409,
            {
                "error": f"収集「{item.name}」は 1 件も集められませんでした",
                "hint": "プロンプトを見直すか、相手を替えてから試してください",
            },
        )
    if reason := shrink_blocked(item, diff):
        raise HTTPException(
            409,
            {
                "error": f"収集「{item.name}」の作り直しを止めました: {reason}",
                "hint": "プロンプトを直すか、意図して減らすなら keep_ratio を下げてください"
                        "(0 で守りを外す)。焼いていないので、いまの内容はそのままです",
            },
        )
    meta = {
        "meta": {
            "dump_date": _dump_date(item.name, sources),
            "min_docs": 1,
            "sample_titles": [docs[0]["title"]],
        }
    }
    lines = [json.dumps(meta, ensure_ascii=False)]
    lines.extend(json.dumps(d, ensure_ascii=False) for d in docs)
    return "\n".join(lines) + "\n", diff
