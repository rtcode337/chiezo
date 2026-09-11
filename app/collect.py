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
import math
import os
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException

from app import collect_log, db, notes
from app import extract as extraction
from app import partition as partitioning
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

# 巡回(`Sweep`)の名前。**書いていない収集は、定義そのものが 1 本の巡回**として振る舞う。
# こうしておくと、区画の記録も時計も「巡回ごと」の 1 本道になる(場合分けが増えない)。
DEFAULT_SWEEP_NAME = "既定"
# 1 つの収集に持てる巡回の数。**2〜3 本で足りる** —— ざっと全体を拾うものと、
# 少数をじっくり調べるもの。増やすほど同じ収集に対する AI の呼び出しが重なる
MAX_SWEEPS = 8
# 1 回で見る区画の上限。**区画ごとに AI を 1 回呼ぶ**(素材をその区画のぶんに
# 絞るのが区画の意味なので、まとめて聞くと絞った意味が消える)ため、
# 1 回の取り込みが何十分にもならないようにここで止める
MAX_PARTITIONS_PER_RUN = 20

# 1 回で焼ける素材の大きさ(バイト)。**件数ではなく大きさで縛る。**
#
# かつては受け取る件数を 200 で切っていた。AI の暴走した答えを止めるためだったが、
# あれは守るべきものを何も守っていなかった:
#
# - プロンプトが膨らむ心配は `render_material` が文字数で見ており(`MAX_MATERIAL_CHARS`)、
#   しかも切ったことを AI に伝える。収集が何件持っていても効く
# - 応答時間は相手ごとのタイムアウトが縛っている(`app/answer.py` の `Settings.timeout`)
# - 件数は大きさの代理にならない。1 件は `MAX_BODY_CHARS` まで許すので、
#   200 件でも 4 MB になりうるし、短い数千件は 1 MB に収まる
#
# 実際に危ないのは `ndjson` が素材を 1 本の文字列で組むところで、そこはバイト数でしか
# 測れない。**数千件・数万件を集めたいことは普通にある**ので、件数の側に天井を作らない。
#
# **超えたら黙って切らずに断る**(`app/extract.py` と同じ判断)。切ったことは
# 返り値から分からないので、絞ったつもりの無い収集が「そこまでしか無い」ように見える
# —— 実測: 索引から 6,875 件に当たった抽出が 200 件で止まり、控えに残ったのは
# 「ok・200 件追加」だけで、当たった件数も切ったことも痕跡が無かった。
MAX_MATERIAL_BYTES = int(
    os.environ.get("CHIEZO_COLLECT_MAX_MATERIAL_BYTES", "") or 64 * 1024 * 1024
)

# 本文の上限。1 件がこれを超えるものは切る(引くための索引であって全文の保管庫ではない)
MAX_BODY_CHARS = 20_000

# 集め方。**外から取ってくるのか、既にあるものを育てるのか**で、
# 素材の作り方も要る守りも変わる。**どちらも前世代を消さない**。
#
# - append(集める): 前世代 + 今回のぶん。外から新しいものを取ってきて積む。
#   AI には今の内容を見せない(見せる必要が無く、そのぶん安い)。
# - refine(整理する): 前世代を**読ませたうえで**、直すものと足すものだけを返させる。
#   分類をやり直す・重複をまとめる・言い回しを揃える、といった育て方のためのもの。
#   **返さなかったものはそのまま残る**。かつては「返ったものが新しい全体」に
#   していたが、それだと**返し忘れが黙って消える** —— 無人で毎日回る層でいちばん
#   起きやすい壊れ方で、しかも歯止めは半分を切るまで働かないので、1 件ずつ削れて
#   いくのは毎回すり抜けた。そもそも前世代は入り切るぶんしか見せられない
#   (`MAX_MATERIAL_CHARS`)ので、育つほど「全部返す」自体が成り立たなくなる。
#   **消すのは墓標で明示したときだけ**(固化とまったく同じ契約)。
MODE_APPEND = "append"
MODE_REFINE = "refine"
MODES = (MODE_APPEND, MODE_REFINE)

# 「作り直し」と呼んでいた頃の値。**育てる側へ読み替える** —— 当時の意図は
# 「整理したい」で、置き換えはその実現手段でしかなかった
LEGACY_MODES = {"rebuild": MODE_REFINE}

# 作り直しのプロンプトに必ず入れてもらう印。ここに前世代の中身が差し込まれる。
# **無いまま作り直すと、AI は今ある内容を知らないまま「全体」を答える**ことになり、
# 育てたものが 1 回で消える。だから作るときに弾く
MATERIAL_PLACEHOLDER = "{current}"

# いま見る区画を差し込む場所。**`{cursor}` と役割が違う** ——
# あちらは「次はどこ」を AI に決めさせる 1 本、こちらは Chiezo が台帳から選んで渡す
# 1 区画。回る先を数え上げられるので、一周したかも取りこぼしも台帳の側で分かる。
PARTITION_PLACEHOLDER = "{partition}"

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

# 控えに残す見出しの数(足した・直した・消した、それぞれ)。**頭のほうだけ** ——
# 全部持つと定義のメモも変更履歴も太る(初期構築の 1 回で数千件が並ぶ)。
# 読むのは「何が動いたか」の手がかりであって、全件の一覧ではない
MAX_TITLE_SAMPLE = 20

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
    # 回る先の割り方(`app/partition.py`)。無ければ区画を持たない収集。
    #
    # **`cursor` が「次はどこ」を AI に決めさせるのに対し、こちらは Chiezo が
    # 台帳から選んで渡す。** 回る先を数え上げられるので、一周したかも取りこぼしも
    # こちら側で分かる。**割るのは対象としている空間であって、集まったものではない**
    # —— 集まった点だけから作ると、まだ 1 件も集めていない範囲に区画が生まれない。
    partition: dict | None = None
    # 割り出した区画の台帳。1 件は `{"key", "count", "visited_at"}`。
    # **巡回の記録はここだけが持つ** —— 割り直しても引き継ぐ(`partitioning.refresh`)
    partitions: list[dict] = field(default_factory=list)
    # 巡回(`Sweep`)。**空なら定義そのものが 1 本の巡回**。
    # ざっと全体を拾うものと、少数をじっくり調べるものを別々の時計で回すために持つ
    sweeps: list[dict] = field(default_factory=list)
    # いま起こしてある取り込みが、どの巡回のものか。**取り込みは名前しか運べない**
    # (`GET /v1/collect/fetch?source=…`)ので、起こした側がここに書いて渡す
    pending_sweep: str = ""
    # 集め方(`MODES`)。既定は足すほう —— 既にある定義の意味を変えない
    mode: str = MODE_APPEND
    # 作り直しで、前世代の何割を下回ったら断るか。0 なら守りを外す。
    # 足すほうでは使わない(そもそも減らないので)
    keep_ratio: float = DEFAULT_KEEP_RATIO
    # 最初の 1 回を機械的に埋める指定(`app/extract.py`)。無ければ毎回 AI に集めさせる。
    # **持つのは指定であって中身の知識ではない** —— どのソースのどのタグを引くかは
    # 依頼した側が書く。進み具合が空のときだけ使い、以降は AI が肉付けする
    extract: dict | None = None
    # 誰が置いたか。外のアプリが名乗った文字列で、**印であって認証ではない**
    # (LAN 内・認証なしの前提なので偽れる)。有効にするか決める人の手がかり
    requested_by: str = ""
    # ここから下は実行のたびに書き換わる控え
    last_run_at: str | None = None
    last_status: str | None = None  # "ok" | "error"
    last_error: str | None = None
    last_added: int = 0
    # 育てるほうで置き換わった件数。**足した件数と別に持つ** —— 整理の回は
    # 足すものが無くても大量に直っていることがあり、追加だけ見ていると
    # 「何もしなかった」ように読める
    last_updated: int = 0
    last_skipped: int = 0
    # 作り直しで消えた件数と、その見出しの頭のほう。
    # **消えたものが見えないとプロンプトを直せない** —— 件数だけでは
    # 「何が落ちたのか」が分からない
    last_removed: int = 0
    last_removed_titles: list[str] = field(default_factory=list)
    next_run_at: str | None = None

    def is_refine(self) -> bool:
        return self.mode == MODE_REFINE

    def due_at(self) -> datetime:
        """次に走る時刻。**巡回のうちいちばん早いもの**(持っていなければ「いますぐ」)。

        巡回を書いていない収集では、自分の `next_run_at` がそのまま出る。
        """
        return min((s.due_at() for s in sweeps_of(self)), default=_now())

    def is_due(self, at: datetime | None = None) -> bool:
        return self.enabled and self.due_at() <= (at or _now())


@dataclass(frozen=True)
class Sweep:
    """1 本の巡回 —— 「どれくらいの頻度で、1 回にどれだけ見るか」。

    **1 つの収集に 2 種類の直し方が要る。** ざっと全体を拾って訂正するもの
    (1 週間で一周するくらいの頻度と量)と、少数をじっくり調べるもの。
    進み方も、頼む相手も、1 回に食べる量も違うので、`interval_minutes` 1 本では表せない。

    **区画の巡回記録は巡回ごとに持つ**(`partitions[].visits`)。ざっとが一周した区画を
    じっくりはまだ見ていない、が普通に起きる。

    書いていない収集では、定義そのものが 1 本の巡回になる(`DEFAULT_SWEEP_NAME`)。
    """

    name: str
    prompt: str
    interval_minutes: int
    enabled: bool
    backend: str | None
    model: str | None
    effort: str | None
    # 一周にかける日数。**書けば 1 回に見る区画数を Chiezo が計算する** ——
    # 手で書かせると、区画が増えた日に一周が静かに伸びる(そして誰も気づかない)
    cover_days: float | None = None
    # 1 回に見る区画数を直に決める。`cover_days` より優先
    partitions_per_run: int | None = None
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None

    def due_at(self) -> datetime:
        """次に走る時刻。持っていなければ「いますぐ」。"""
        return _parse(self.next_run_at) or _now()

    def is_due(self, at: datetime | None = None) -> bool:
        """**予定を持っていない巡回は、いますぐ走る。** 足したばかりの巡回がそれで、
        「いま」を取り直して比べると必ず未来になり、永遠に走らない(実際にそうなった)。
        """
        at = at or _now()
        return self.enabled and (_parse(self.next_run_at) or at) <= at

    def per_run(self, total_partitions: int) -> int:
        """1 回に見る区画の数。

        **`cover_days` から計算するのが本命。** 「7 日で一周」とだけ書けば、区画が
        増えても 1 回あたりが自動で増える —— 区画数を手で追いかけなくてよくなる。
        """
        if self.partitions_per_run:
            return max(1, min(self.partitions_per_run, MAX_PARTITIONS_PER_RUN))
        if self.cover_days and total_partitions:
            runs = self.cover_days * 24 * 60 / self.interval_minutes
            if runs >= 1:
                return max(1, min(math.ceil(total_partitions / runs), MAX_PARTITIONS_PER_RUN))
        return 1

    def applied_to(self, item: Collection) -> Collection:
        """この巡回の相手・モデル・深さを載せた定義(AI へ投げるときに使う)。"""
        return replace(item, backend=self.backend, model=self.model, effort=self.effort)

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v is not None}


def sweeps_of(item: Collection) -> list[Sweep]:
    """この収集の巡回。**書いていなければ定義そのものが 1 本**。

    場合分けを外へ漏らさないための形 —— 呼ぶ側はいつでも「巡回の一覧」を相手にする。
    """
    if not item.sweeps:
        return [Sweep(
            name=DEFAULT_SWEEP_NAME,
            prompt=item.prompt,
            interval_minutes=item.interval_minutes,
            enabled=True,
            backend=item.backend,
            model=item.model,
            effort=item.effort,
            next_run_at=item.next_run_at,
            last_run_at=item.last_run_at,
            last_status=item.last_status,
            last_error=item.last_error,
        )]
    return [_sweep_from_json(raw, item) for raw in item.sweeps[:MAX_SWEEPS]]


def _sweep_from_json(raw: dict, item: Collection) -> Sweep:
    """**書いていない項目は収集のものを使う。** 巡回ごとに全部書かせない
    (違うところだけ書けば済むほうが、2 本目を足すときに間違えにくい)。
    """
    return Sweep(
        name=str(raw.get("name") or DEFAULT_SWEEP_NAME).strip()[:40],
        prompt=str(raw.get("prompt") or "") or item.prompt,
        interval_minutes=max(
            int(raw.get("interval_minutes") or item.interval_minutes), MIN_INTERVAL_MINUTES
        ),
        enabled=bool(raw.get("enabled", True)),
        backend=raw.get("backend") or item.backend,
        model=raw.get("model") or item.model,
        effort=raw.get("effort") or item.effort,
        cover_days=float(raw["cover_days"]) if raw.get("cover_days") else None,
        partitions_per_run=(
            int(raw["partitions_per_run"]) if raw.get("partitions_per_run") else None
        ),
        next_run_at=raw.get("next_run_at") or None,
        last_run_at=raw.get("last_run_at") or None,
        last_status=raw.get("last_status") or None,
        last_error=raw.get("last_error") or None,
    )


def normalize_sweeps(raw) -> list[dict]:
    """巡回の一覧を均す。**名前が鍵**なので、空や重複は落とす。

    区画の記録が名前で引かれる(`partitions[].visits`)ため、同じ名前が 2 本あると
    片方の進み具合がもう片方に化ける。
    """
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw[:MAX_SWEEPS]:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()[:40]
        if not name or name in seen:
            continue
        seen.add(name)
        out.append({**item, "name": name})
    return out


def sweep_named(item: Collection, name: str | None) -> Sweep:
    """名前で引く。**知らない名前なら、次に走るはずの巡回へ倒す** ——
    巡回を消したあとに走りかけの取り込みが素材を取りに来ることがある。
    """
    sweeps = sweeps_of(item)
    for sweep in sweeps:
        if sweep.name == name:
            return sweep
    due = [s for s in sweeps if s.enabled]
    return min(due or sweeps, key=lambda s: s.due_at())


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
        partition=partitioning.to_json(partitioning.normalize(item.get("partition"))),
        partitions=partitioning.normalize_ledger(item.get("partitions")),
        sweeps=normalize_sweeps(item.get("sweeps")),
        pending_sweep=str(item.get("pending_sweep") or ""),
        mode=normalize_mode(item.get("mode")),
        keep_ratio=normalize_keep_ratio(item.get("keep_ratio")),
        extract=extraction.to_json(extraction.normalize(item.get("extract"))),
        requested_by=str(item.get("requested_by") or ""),
        created_at=str(item.get("created_at") or ""),
        updated_at=str(item.get("updated_at") or ""),
        last_run_at=item.get("last_run_at") or None,
        last_status=item.get("last_status") or None,
        last_error=item.get("last_error") or None,
        last_added=int(item.get("last_added") or 0),
        last_updated=int(item.get("last_updated") or 0),
        last_skipped=int(item.get("last_skipped") or 0),
        last_removed=int(item.get("last_removed") or 0),
        last_removed_titles=[str(t) for t in (item.get("last_removed_titles") or [])],
        next_run_at=item.get("next_run_at") or None,
    )


def normalize_mode(value) -> str:
    """知らない集め方は足すほうへ倒す。壊れた定義でいきなり消す側へ寄せない。"""
    if value in MODES:
        return value
    return LEGACY_MODES.get(value, MODE_APPEND)


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
    if mode == MODE_REFINE and MATERIAL_PLACEHOLDER not in prompt:
        raise HTTPException(400, {
            "error": f"整理のプロンプトには {MATERIAL_PLACEHOLDER} を入れてください",
            "reason": "ここへ今ある内容が差し込まれる。無いと、AI は今あるものを"
                      "知らないまま書くことになり、直すことも重複をまとめることもできない",
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
    extract_spec=None,
    partition_spec=None,
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
        extract=extraction.to_json(extraction.normalize(extract_spec)),
        partition=partitioning.to_json(partitioning.normalize(partition_spec)),
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
        "backend", "model", "effort", "web", "cursor", "mode", "keep_ratio", "extract",
        "partition", "partitions", "sweeps",
    }
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "interval_minutes" in patch:
        patch["interval_minutes"] = max(int(patch["interval_minutes"]), MIN_INTERVAL_MINUTES)
    if "mode" in patch and patch["mode"] not in MODES:
        raise HTTPException(400, {"error": f"mode は {' / '.join(MODES)} のどれかにしてください"})
    if "keep_ratio" in patch:
        patch["keep_ratio"] = normalize_keep_ratio(patch["keep_ratio"])
    if "partition" in patch:
        # 空のオブジェクトを渡したら区画を持たない収集に戻す(消す手段がここしかない)。
        # **割り方を変えたら台帳は捨てる** —— 鍵の意味が変わるので、引き継ぐと
        # 前の割り方で見た記録が新しい区画に付く
        patch["partition"] = partitioning.to_json(partitioning.normalize(patch["partition"] or None))
        # **台帳を明示的に渡されていなければ捨てる。** 両方渡されたときは渡したほうが
        # 勝つ(割り出した結果を持ち込みたいのに、こちらが消してしまうため)
        if patch["partition"] != current.partition and "partitions" not in patch:
            patch["partitions"] = []
    if "sweeps" in patch:
        patch["sweeps"] = normalize_sweeps(patch["sweeps"])
        # **巡回の名前が消えたら、その巡回の記録も台帳から落とす** ——
        # 残しておくと、同じ名前で作り直したとき前の進み具合が引き継がれる
        names = {raw["name"] for raw in patch["sweeps"]}
        if names:
            patch["partitions"] = [
                {**p, "visits": {k: v for k, v in (p.get("visits") or {}).items() if k in names}}
                for p in patch.get("partitions", current.partitions)
            ]
    if "partitions" in patch:
        # **空の配列を渡せば最初から回り直せる**(消す手段がここしかない)。
        # 2 周目を粗いまま繰り返させず、一度リセットして精度を上げ直したいときに使う
        patch["partitions"] = partitioning.normalize_ledger(patch["partitions"])
    if "extract" in patch:
        # 空のオブジェクトを渡したら「使わない」に戻す(消す手段がここしかない)
        patch["extract"] = extraction.to_json(extraction.normalize(patch["extract"] or None))
    # 相手・モデル・深さは**空文字を「指定しない」に倒す**。画面のフォームは空欄を
    # 空文字で送ってくるが、持ち回るときは None でないと「未指定」の意味にならない
    # (読み直せば `_from_json` が同じことをするが、保存直後の値とずれる)
    for key in ("backend", "model", "effort"):
        if key in patch and not str(patch[key]).strip():
            patch[key] = None
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

    **変更履歴は落とす**(`app/collect_log.py`)。名前がそのままソース名なので、
    同じ名前で作り直すのは普通に起きる —— 残しておくと、前の収集が足した・消した
    ものが新しい収集の履歴に混ざって見える。
    """
    items = load()
    if not any(c.name == name for c in items):
        raise HTTPException(404, {"error": f"収集「{name}」がありません"})
    save([c for c in items if c.name != name])
    collect_log.forget(name)


# ---- 溜め先(コアスキーマの DB。notes と同じ形)---------------------------------


SYSTEM_PROMPT = (
    "集めた情報を JSON だけで返す。前置き・説明・コードブロックの記号は付けない。"
    " 形式: {\"items\":[{\"title\":\"見出し\",\"body\":\"本文\","
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\"}],\"next_cursor\":\"次に進む印\"}"
    " title は重複の鍵になるので、同じものを指す見出しは同じ文字列にする。"
    " 出典が分かるものは url を必ず入れる。分からない項目は null。"
)


# 矩形で区画を割っている収集にだけ足す。**座標が無いと、集めたものがどの区画にも
# 入らない** —— 次にその区画を見るとき「まだ何も無い」と見えて、同じものを集め直す。
GEO_SYSTEM_NOTE = (
    " **1 件ごとに lat(緯度)と lon(経度)を数で入れる。**"
    " 分からないものは入れなくてよいが、入っていないものは今回の範囲に属さない扱いになる。"
)


REFINE_SYSTEM_PROMPT = (
    "既にある内容を育てる。JSON だけで返し、前置き・説明・コードブロックの記号は付けない。"
    " 形式: {\"items\":[{\"title\":\"見出し\",\"body\":\"本文\","
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\"}],\"next_cursor\":\"次に進む印\"}"
    " **返すのは、直すものと新しく足すものだけでよい。**"
    " 触れなかったものはそのまま残るので、変えないものを返す必要はない。"
    " title は同一性の鍵。**同じ見出しで返すと、その 1 件が置き換わる**。"
    " **消したいものは、その見出しで tags に「" + notes.TOMBSTONE_TAG + "」を入れて返す**"
    "(墓標。本文は空でよい)。重複をまとめるときは、まとめた先を返し、"
    "元のものに墓標を付ける。"
    " 分からない項目は null。"
)


def render_material(previous: dict[str, dict], scoped: bool = False) -> tuple[str, int]:
    """前世代を、プロンプトへ差し込める形にする。差し込んだ件数も返す。

    **入り切らなければ切って、切ったことを本文に書く** —— 黙って切ると、AI は
    見えなかったぶんを「無かったもの」として落とし、歯止めが無ければそのまま消える。

    `scoped` は「今回の区画のぶんだけを渡している」の印。**区画で切ってあれば
    普通は全部入る**ので、切られたときの意味が変わる(区画が大きすぎる)。
    """
    if not previous:
        return (
            "(この範囲には、まだ何も入っていません)" if scoped
            else "(まだ何も入っていません。最初の内容を作ってください)"
        ), 0
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
    head = ("この範囲にいま入っているもの(全 " if scoped else "いまの内容(全 ") + f"{len(docs)} 件"
    head += f"。うち {shown} 件だけ載せています)" if shown < len(docs) else ")"
    if shown < len(docs):
        head += "\n※ 載っていないものは今回の対象外です。載っているぶんだけを整理してください。"
    return head + ":\n" + "\n".join(lines), shown


def scoped_docs(
    item: Collection, previous: dict[str, dict], partition_key: str | None
) -> tuple[dict[str, dict], bool]:
    """今回の区画に入る文書だけに絞る。絞ったかどうかも返す。

    **これが区画のいちばんの利得。** 全体を差し込もうとすると入り切らず、切ったぶんは
    「今回の対象外」になる(`MAX_MATERIAL_CHARS`)。区画で切れば全部見せられるので、
    漏れているものを足させることも、重複をまとめることも初めて成り立つ。
    """
    if not (item.partition and partition_key):
        return previous, False
    spec = partitioning.normalize(item.partition)
    return {
        title: doc
        for title, doc in previous.items()
        if partitioning.belongs(spec, partition_key, doc)
    }, True


def build_messages(
    item: Collection,
    previous: dict[str, dict] | None = None,
    partition_key: str | None = None,
    sources: dict | None = None,
    sweep: Sweep | None = None,
) -> list[dict]:
    """AI へ渡す本文。`{cursor}` を今のカーソルで、`{current}` を今ある内容で置き換える。

    **カーソルが空でも壊さない**(初回は空文字が入るだけ)。テンプレートに `{cursor}` が
    無い収集は、毎回同じことを聞く形になる。

    `{current}` は作り直し(整理)のためのもの。今ある内容を読ませて、分類をやり直す・
    重複をまとめる・言い回しを揃える、といった育て方をするときに使う。
    **区画を持つ収集では、その区画のぶんだけが入る**。

    `{partition}` は**今回見る範囲**。Chiezo が台帳から選んで渡す(`app/partition.py`)。
    矩形だけでは AI にどこか分からないので、近くのものを数件添えた文になる。
    """
    prompt = (sweep.prompt if sweep else "") or item.prompt
    user = prompt.replace("{cursor}", item.cursor or "(まだ無し。最初から)")
    spec = partitioning.normalize(item.partition) if item.partition else None
    if PARTITION_PLACEHOLDER in user:
        user = user.replace(
            PARTITION_PLACEHOLDER,
            partitioning.describe(spec, partition_key, sources or {})
            if (spec and partition_key) else "(全体)",
        )
    if MATERIAL_PLACEHOLDER in user:
        docs, scoped = scoped_docs(item, previous or {}, partition_key)
        material_text, _shown = render_material(docs, scoped)
        user = user.replace(MATERIAL_PLACEHOLDER, material_text)
    system = REFINE_SYSTEM_PROMPT if item.is_refine() else SYSTEM_PROMPT
    if spec and spec["by"] == partitioning.BY_GEO:
        system += GEO_SYSTEM_NOTE
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
    "- **端から端まで舐めていく collection では {partition} を入れる**。"
    "そこへ「今回見る範囲」が差し込まれる(Chiezo が対象の空間を区画に割って、"
    "順に配る)。指示文は「この範囲について調べて」「この範囲に足すべきものが"
    "無いか確かめて」のように書く。範囲の選び方を AI に決めさせてはいけない\n"
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
    updated: int = 0,
    skipped: int = 0,
    removed: int = 0,
    removed_titles: list[str] | None = None,
    error: str | None = None,
    next_cursor: str | None = None,
    sweep: str | None = None,
    visited: list[str] | None = None,
    partitions: list[dict] | None = None,
) -> Collection:
    """1 回ぶんの結果を定義側へ書き戻し、次回の予定を入れる。

    **失敗しても次回の予定は入れる** —— 入れないと、一度こけた収集が二度と走らなくなる。

    **見た区画に印を付けるのは成功したときだけ。** 失敗した回に印を付けると、
    一度も見られていない区画が「回り終えた」に混ざり、一周が嘘になる。
    """
    current = get(name)
    now = _now()
    this = sweep_named(current, sweep)
    ledger = partitions if partitions is not None else current.partitions
    if visited and status == "ok":
        ledger = partitioning.mark_visited(ledger, visited, this.name, _iso(now))
    updated = replace(
        current,
        cursor=next_cursor if next_cursor is not None else current.cursor,
        partitions=ledger,
        # **走り終えたので、どの巡回を起こしてあるかは忘れる**
        pending_sweep="",
        last_run_at=_iso(now),
        last_status=status,
        last_error=(error or "")[:500] or None,
        last_added=added,
        last_updated=updated,
        last_skipped=skipped,
        last_removed=removed,
        last_removed_titles=list(removed_titles or []),
        updated_at=_iso(now),
        **_advance(current, this, now, status=status, error=error),
    )
    _replace_one(name, updated)
    return updated


def _advance(
    current: Collection, sweep: Sweep, now: datetime, *, status: str, error: str | None
) -> dict:
    """走った巡回の次回の予定と控えを進める。

    **巡回を書いていない収集では、定義そのものの欄が進む** —— 場合分けが要るのは
    ここだけで、呼ぶ側はどちらかを気にしなくてよい。
    """
    nxt = _iso(now + timedelta(minutes=sweep.interval_minutes))
    if not current.sweeps:
        return {"next_run_at": nxt}
    return {"sweeps": [
        {
            **raw,
            "next_run_at": nxt,
            "last_run_at": _iso(now),
            "last_status": status,
            "last_error": (error or "")[:500] or None,
        }
        if raw.get("name") == sweep.name else raw
        for raw in current.sweeps
    ]}


def mark_started(name: str, sweep: str | None = None) -> Collection:
    """取り込みを起こしたので、次回の予定だけ進める。

    **どの巡回を起こしたかを控える**(`pending_sweep`)。取り込みは収集の名前しか
    運べない(`GET /v1/collect/fetch?source=…`)ので、素材を作る側はここを読む。

    **結果は控えない** —— 実際に集められたかは、取り込みが素材を取りに来たとき
    (`ndjson`)に分かる。ここで「成功」と書くと、起こしただけのものが成功に見える。
    """
    current = get(name)
    this = sweep_named(current, sweep)
    now = _now()
    updated = replace(
        current,
        pending_sweep=this.name,
        updated_at=_iso(now),
        **_advance(current, this, now, status=current.last_status, error=current.last_error),
    )
    _replace_one(name, updated)
    return updated


def due_sweeps(at: datetime | None = None) -> list[tuple[Collection, Sweep]]:
    """いま走らせるべき(収集, 巡回)の組を、予定の早い順に。

    **止めている収集は 1 本も出さない** —— 巡回ごとの `enabled` は、有効な収集の中で
    どの巡回を回すかの話で、収集そのものの可否とは別の段。
    """
    if not is_enabled():
        return []
    now = at or _now()
    pairs = [
        (c, sweep)
        for c in load() if c.enabled
        for sweep in sweeps_of(c) if sweep.is_due(now)
    ]
    return sorted(pairs, key=lambda pair: pair[1].due_at())


def due_collections(at: datetime | None = None) -> list[Collection]:
    """いま走らせるべき収集(予定の早い順・重複なし)。"""
    seen: set[str] = set()
    out = []
    for item, _sweep in due_sweeps(at):
        if item.name not in seen:
            seen.add(item.name)
            out.append(item)
    return out


def to_public(item: Collection, *, with_partitions: bool = True) -> dict:
    """画面と REST に返す形。**次回の予定を必ず入れる**

    (「30 分ごと・次は 14:20」まで見えて初めて、動いているか判断できる。
    溜まった件数は長期記憶の側にあるので、画面はソース表から取る)。

    **一覧では台帳そのものを載せない**(`with_partitions=False`)——
    上限まで割ると 1 件で数百 KB になり、収集の数だけ倍になる。進み具合だけ載せて、
    中身は 1 件ぶんの口(`GET /v1/collect/{name}`)で取ってもらう。
    """
    data = {**item.__dict__, "url": f"/search/{item.name}/"}
    sweeps = sweeps_of(item)
    # **一覧に出す予定は、巡回のうちいちばん早いもの。** 巡回を書いている収集では
    # 定義側の `next_run_at` が進まないので、そのまま出すと止まって見える
    data["next_run_at"] = _iso(item.due_at()) if item.enabled else item.next_run_at
    data["partitions_total"] = len(item.partitions)
    # 進み具合と次の行き先は**巡回ごと**。ざっとが一周した区画を、じっくりは
    # まだ見ていない、が普通に起きる
    data["sweeps"] = [
        {
            **sweep.to_json(),
            "partitions_visited": partitioning.progress(item.partitions, sweep.name)[0],
            "next_partition": partitioning.due(item.partitions, sweep.name),
            "partitions_per_run": sweep.per_run(len(item.partitions)),
        }
        for sweep in sweeps
    ]
    if not with_partitions:
        data.pop("partitions", None)
    return data


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


def plan_partitions(item: Collection, sources: dict, previous: dict[str, dict]) -> list[dict]:
    """この回に使う区画の台帳。必要なら割り直す(`app/partition.py`)。

    **毎回は割り直さない。** 母集団を外のソースから取っているなら、こちらが何件
    集めようと点の数は変わらないので、区画は動かないほうがよい —— 動かすと
    巡回の記録が毎回リセットされ、一周が永遠に終わらない。
    自分自身を割っているときだけ、育って `target` を超えた区画が出たら割り直す。

    **割り直しても巡回の記録は引き継ぐ**(`partitioning.refresh`)。
    """
    if not item.partition:
        return []
    spec = partitioning.normalize(item.partition)
    if item.partitions and not partitioning.outgrown(spec, item.partitions, previous):
        return item.partitions
    built = partitioning.build(spec, sources, previous)
    log.info("partition %s: %d 区画", item.name, len(built))
    return partitioning.refresh(built, item.partitions)


def material(
    item: Collection, previous: dict[str, dict], collected: list[dict]
) -> tuple[list[dict], dict]:
    """焼く素材と、前世代との差分を組み立てる。

    **受け取る件数は絞らない。** 大きさの天井は `ndjson` がバイト数で見る
    (`MAX_MATERIAL_BYTES` に理由)。ここで件数を切ると、集まった件数と焼けた件数が
    黙って食い違う。

    **どちらの集め方でも前世代から始める。** 返ってこなかったものは残る ——
    集める(append)は外から積むだけなので当然だが、育てる(refine)でも同じにしてある。
    かつては refine を「返ったものが新しい全体」にしていたが、それだと**返し忘れが
    黙って消える**。無人で回る層でいちばん起きやすい壊れ方で、しかも 1 件ずつ削れて
    いくのは歯止め(`shrink_blocked`)をすり抜ける。

    違うのは 2 つだけ:

    - **同じ見出しをどう数えるか。** 集めるほうは「既に持っていた」ので追加に数えない。
      育てるほうは**直しに来ている**ので、置き換わったことを `updated` に数える。
    - **墓標を読むかどうか。** 育てるほうだけ、`削除` の付いた見出しを落とす
      (固化とまったく同じ契約)。**消すのは明示したときだけ**。

    **`doc_id` は前世代のものを引き継ぐ**。残った文書の URL が焼き直しで変わらないため。
    """
    merged = dict(previous)
    next_id = max((d["doc_id"] for d in previous.values()), default=0) + 1
    now = _iso(_now())
    added = updated = skipped = 0
    added_titles: list[str] = []
    updated_titles: list[str] = []
    removed_titles: list[str] = []
    for raw in collected:
        title = (raw.get("title") or "").strip()[:notes.TITLE_MAX_CHARS]
        if item.is_refine() and title and _is_tombstone(raw):
            # 墓標。**持っていないものへの墓標は数えない**(消すものが無い)
            if merged.pop(title, None) is not None:
                removed_titles.append(title)
            else:
                skipped += 1
            continue
        doc = _to_doc(raw, now, item.web)
        if doc is None:
            skipped += 1
            continue
        title = doc["title"]
        kept_before = previous.get(title)
        if kept_before is None:
            doc_id = next_id
            next_id += 1
            added += 1
            added_titles.append(title)
        else:
            doc_id = kept_before["doc_id"]
            if item.is_refine():
                updated += 1
                updated_titles.append(title)
            else:
                # 集めるほうで同じ見出しが来るのは「もう持っている」の意味。
                # 中身は新しいほうで置き換えるが、積み上がった件数は増えない
                skipped += 1
        merged[title] = {**doc, "doc_id": doc_id}
    diff = {
        "previous": len(previous),
        "total": len(merged),
        "added": added,
        "updated": updated,
        "kept": len(merged) - added,
        "removed": len(removed_titles),
        "skipped": skipped,
        # 動いた見出しの頭のほう。**件数だけでは何が起きたか読めない** ——
        # 「10 件消えた」と「この 10 件が消えた」では、プロンプトを直せるかが違う
        "added_titles": added_titles[:MAX_TITLE_SAMPLE],
        "updated_titles": updated_titles[:MAX_TITLE_SAMPLE],
        "removed_titles": removed_titles[:MAX_TITLE_SAMPLE],
        # 集めた側が返した件数。**焼ける件数(`total`)とは別に出す** —— 一致しない
        # ときに、捨てたのか前世代と重なったのかを読み分けられるようにするため
        "collected": len(collected),
    }
    return sorted(merged.values(), key=lambda d: d["doc_id"]), diff


def _is_tombstone(raw: dict) -> bool:
    """墓標か(消してほしい、の印)。固化と同じタグを使う。"""
    tags = raw.get("tags") or []
    return any(str(t).strip() == notes.TOMBSTONE_TAG for t in tags)


def shrink_blocked(item: Collection, diff: dict) -> str | None:
    """墓標で減りすぎていたら、その理由の文。問題なければ None。

    **AI が変な日に当たった 1 回で、育てた分類が消えるのを止める**のがここ。
    返し忘れでは減らなくなったので、ここが止めるのは**明示的な大量削除**だけになった
    —— そのぶん、止まったときの意味が鋭い(AI が「全部要らない」と言っている)。
    足すほうには要らない(そもそも減らない)。`keep_ratio` を 0 にすると外れる。
    """
    if not item.is_refine() or item.keep_ratio <= 0 or not diff["previous"]:
        return None
    floor = diff["previous"] * item.keep_ratio
    if diff["total"] >= floor:
        return None
    sample = "、".join(diff["removed_titles"][:5])
    return (
        f"整理の結果が {diff['total']} 件で、前の {diff['previous']} 件から"
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
    # **座標は運ぶ**。矩形で区画を割る収集では、これが無いと集めたものがどの区画にも
    # 入らない(次に同じ区画を見たとき「まだ何も無い」と見えて、同じものを集め直す)。
    # ついでにコアスキーマの生成列に乗るので、`filter?bbox=` で普通のソースとして引ける
    lat, lon = _coords(raw)
    if lat is not None:
        extra["lat"], extra["lon"] = lat, lon
    return {
        "doc_id": 0,  # material が前世代から引き継ぐか、新しく振る
        "title": title,
        "opening": body[:notes.TITLE_MAX_CHARS * 4],
        "body": body,
        "tags": tags,
        "updated_at": now,
        "extra": extra,
    }


def _coords(raw: dict) -> tuple[float | None, float | None]:
    """AI が返した 1 件から座標を取る。**数でなければ持たない**(文字列を素通ししない)。"""
    try:
        lat, lon = float(raw["lat"]), float(raw["lon"])
    except (KeyError, TypeError, ValueError):
        return None, None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None, None
    return lat, lon


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
    先に全部組み立ててから返す(固化と同じ判断)。

    **作り直しで減りすぎていても断る**。ここが後戻りできる最後の地点で、
    通してしまうと次の世代が焼き上がり、戻すには世代を巻き戻すしかなくなる。

    **大きすぎても断る**。素材を 1 本の文字列で組む場所なので、ここだけは
    実際のバイト数でしか測れない(`MAX_MATERIAL_BYTES`)。積みながら見て、
    超えた時点で止める —— 全部組んでから測ると、測るために膨らませることになる。
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
                "error": f"収集「{item.name}」の整理を止めました: {reason}",
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
    used = len(lines[0].encode())
    for n, doc in enumerate(docs, 1):
        line = json.dumps(doc, ensure_ascii=False)
        used += len(line.encode()) + 1
        if used > MAX_MATERIAL_BYTES:
            raise HTTPException(
                409,
                {
                    "error": f"収集「{item.name}」の素材が大きすぎます"
                             f"({len(docs):,} 件のうち {n:,} 件目で"
                             f" {MAX_MATERIAL_BYTES / 1024 / 1024:.0f} MB を超えました)",
                    "hint": "1 回に集める件数を減らすか、本文を短くしてください"
                            "(天井は CHIEZO_COLLECT_MAX_MATERIAL_BYTES で変えられます)。"
                            "焼いていないので、いまの内容はそのままです",
                },
            )
        lines.append(line)
    return "\n".join(lines) + "\n", diff
