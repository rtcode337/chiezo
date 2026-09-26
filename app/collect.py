"""collect — AI に集めさせて、引ける形で溜めていく層。

## 何のためか

Wikipedia や OSM のような**まとまったダンプが無い**ことは知りたい、という穴を埋める。
たとえば直近のニュース、飲食店のような入れ替わりの速い情報、人物の関係。
どれも「1 回引いて終わり」ではなく、**繰り返し少しずつ溜まっていく**のが本質なので、
取り込み(ingest)のブルーグリーン(全件洗い替え)には乗らない。

## 置き方の決めごと

- **溜まる先は長期記憶(`corpus/`)。焼くのは ingest**。短期記憶と
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
  **同じ url の 1 件も足さない**(`url_key`)—— 見出しは書き換わるので、鍵が
  見出しだけだと同じ記事が二度入る。

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
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import HTTPException

from app import collect_log, db, feeds, jst, machine_store, notes, workers
from app import extract as extraction
from app import partition as partitioning
from app.jst import to_jst
from app.registry import TAG_MIN_SCHEMA_VERSION, generation_stamp, previous_generation

log = logging.getLogger("chiezo.app")

SOURCE_KIND = "collect"

# 定義をまとめて持つメモ(notes 側)。プロジェクトと同じく 1 件に配列で持つ
# 機械の置き場での置き所(`app/machine_store.py`)
DEFS_KIND = "collect"
DEFS_KEY = "definitions"
DEFS_BROKEN = "収集のメモが JSON として読めません"

# 収集ソースの名前に使える文字。**ファイル名とソース名とURLになる**ので狭く取る
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,30}$")

# 間隔の下限。AI を呼ぶので、分より短い間隔を許すと枠を焼くだけになる
MIN_INTERVAL_MINUTES = 5

# 時計を持たない巡回の「次に走る時刻」。**「無い」を日時で表す** ——
# 予定の早い順に並べるところ(`due_sweeps` / `Collection.due_at`)が 1 本道のままで済む
NEVER = datetime(9999, 12, 31, tzinfo=UTC)

# 巡回(`Sweep`)の名前。**書いていない収集は、定義そのものが 1 本の巡回**として振る舞う。
# こうしておくと、区画の記録も時計も「巡回ごと」の 1 本道になる(場合分けが増えない)。
DEFAULT_SWEEP_NAME = "既定"
# 割り込み用の巡回の名前(見本として作るときに使う)。**名前で選ばない** ——
# 選ぶときに見るのは `on_demand` のほうで、名前は画面に出る札でしかない
FOCUS_SWEEP_NAME = "割り込み"
# 1 つの収集に持てる巡回の数。**2〜3 本で足りる** —— ざっと全体を拾うものと、
# 少数をじっくり調べるもの。増やすほど同じ収集に対する AI の呼び出しが重なる
MAX_SWEEPS = 8
# 墓場に置ける見出しの数。**捨てたぶんはもう守らない**(足す回が連れ戻す)ので
# 1 件の脇に運べる事実の数と長さ(`_carried`)。**札であって記事ではない**ので、
# 際限なく運べるようにはしない
MAX_CARRIED_KEYS = 10
MAX_CARRIED_KEY_CHARS = 40
MAX_CARRIED_CHARS = 200
# 脇書きに置ける並びの長さ。**入れ子は通さないが、短い語の並びは通す** ——
# 「一緒に出てくる語」のように、1 件の脇に添える事実が並びになることがある
MAX_CARRIED_ITEMS = 20
# 1 件が持てる脇書きの数。**重ねる作りなので、天井はこちらに要る** ——
# 言われていないものを消さない以上、放っておくと回を重ねるだけ増える
MAX_EXTRA_KEYS = 20
# 墓標に添える理由の長さ。**1 行で足りる** —— なぜ外したかが読めればよく、
# 本文はそのまま残る(理由で上書きしない)ので、長く書かせる先はこちらではない
MAX_REMOVED_REASON_CHARS = 400
# 直す前の本文を控えておく長さ(`_before_of`)。この層の本文は数百字で、
# 実測でも 100〜200 字に収まる —— 天井は「切られずに全文が残る」側に置く。
# 1 件につき 1 回分しか持たないので、これだけあっても増え続けはしない
MAX_BEFORE_BODY_CHARS = 4000

# 1 回で見る区画の上限。**区画ごとに AI を 1 回呼ぶ**(素材をその区画のぶんに
# 絞るのが区画の意味なので、まとめて聞くと絞った意味が消える)ため、ここが
# そのまま「1 回の取り込みで AI を何回叩くか」になる。
#
# **5 に抑える。** 1 回が長いほど、途中で起きたことを取り返せない ——
# 本番で 7 区画の回が 29 分かかり、その途中で相手の枠が閉まって、
# 集めたぶんがまるごと消えた(いまは区画ごとに振り替えるが、長い回はそれでも
# 枠を読み違える幅が大きい)。**一周の目安より、こちらを優先する** ——
# 目安の日数を超えても回り続けるほうが、1 回に賭けるより確実に進む
MAX_PARTITIONS_PER_RUN = 5

# 割り込み(`Focus`)で名指しできる見出しの数。**名指しは訂正のためのもの**なので、
# 数十件も並べるなら区画を指すほうが早い
MAX_FOCUS_TITLES = 50
# 割り込みの指示文の長さ。プロンプトへそのまま入るので、本文ぶんの枠に収める
MAX_FOCUS_NOTE_CHARS = 2_000
# その回だけ差し込む依頼文の長さ。定義のプロンプトと同じ立場なので、同じだけ許す
MAX_PROMPT_CHARS = 20_000

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
# **数千件・数万件を集めたいことは普通にある**ので、件数の側に天井を作らない。
#
# **もうメモリの話ではない。** 素材は 1 行ずつ流すようになった(`bake_lines`)ので、
# ここが守っているのは「焼くのに現実的な大きさか」だけ —— 取り込み側はこれを
# ファイルへ落としてから舐めるので、置き場と時間のほうが効く。
# 実測では地図の名簿が 1 件 478 バイトで、68 万件でも 460 MB に収まる。
#
# **超えたら黙って切らずに断る**(`app/extract.py` と同じ判断)。切ったことは
# 返り値から分からないので、絞ったつもりの無い収集が「そこまでしか無い」ように見える
# —— 実測: 索引から 6,875 件に当たった抽出が 200 件で止まり、控えに残ったのは
# 「ok・200 件追加」だけで、当たった件数も切ったことも痕跡が無かった。
MAX_MATERIAL_BYTES = int(
    os.environ.get("CHIEZO_COLLECT_MAX_MATERIAL_BYTES", "") or 1024 * 1024 * 1024
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
#   **消すのは墓標で明示したときだけ**。
# 収集の**種類**。`mode` とは別の軸 —— あちらは「返ってきた 1 件で何ができるか」で、
# こちらは「その収集が何を集めているのか」。
#
# **流れ**(`KIND_FLOW`) —— 時とともに増える流れを追う。直近だけが対象で、
#   古いものは順に要らなくなる(ニュース、いま話題の映画、直近のイベント)。
# **網羅**(`KIND_STOCK`) —— ある括りの全部を集める。増減はしても、
#   **古いものが要らなくなることはなく**、端から端まで精査し続ける
#   (画家の関連図、全国の食事処)。
#
# 分かれるのは言い方だけではない —— 区画で全部を回るのは網羅だけ、
# 期限で落とすのは流れだけ、`{current}` と `{recent}` の使い分けもここで決まる。
KIND_FLOW = "flow"
KIND_STOCK = "stock"
KINDS = (KIND_FLOW, KIND_STOCK)

# 流れの収集が、何日ぶんを持つか。**既定は 30 日** —— ニュースなら十分に振り返れて、
# 量も抑えられる。0 なら期限では落とさない
DEFAULT_KEEP_DAYS = 30

# 作り直しのプロンプトに必ず入れてもらう印。ここに前世代の中身が差し込まれる。
# **無いまま作り直すと、AI は今ある内容を知らないまま「全体」を答える**ことになり、
# 育てたものが 1 回で消える。だから作るときに弾く
MATERIAL_PLACEHOLDER = "{current}"

# いま見る区画を差し込む場所。**`{cursor}` と役割が違う** ——
# あちらは「次はどこ」を AI に決めさせる 1 本、こちらは Chiezo が台帳から選んで渡す
# 1 区画。回る先を数え上げられるので、一周したかも取りこぼしも台帳の側で分かる。
PARTITION_PLACEHOLDER = "{partition}"

# 外向きの道具(`app/feeds.py`)が取ってきたものを差し込む場所。
# **素材であって情報源ではない** —— 何を溜めるかは AI が決める(自分でも調べる)。
# `{current}` が「いま手元にあるもの」なら、こちらは「外で拾ってきたもの」
FEED_PLACEHOLDER = "{feed}"

# 前回この巡回が走ってから後に入ったものを差し込む場所。
# **`{current}` と役割が違う** —— あちらは「いま手元にある全部」、こちらは「その差分」。
# 溜まっていく一方の収集(ニュースのような)で、要約や重要度付けを頼む回に要る:
# 全部を差し込むと入り切らないし、入ったとしても毎回同じものを読み直すことになる。
RECENT_PLACEHOLDER = "{recent}"

# **別のソースに溜まったもの**を差し込む場所(`material`)。
# **`{current}` とも `{feed}` とも役割が違う** —— あちらは「この収集の中身」と
# 「外の RSS が配ったもの」で、こちらは**Chiezo に既に溜まっている別の収集**。
#
# 集めたものを材料にして別の見方を育てる、という置き方のために要る ——
# 例えば「技術ニュースの収集」に溜まった記事を読んで、話題の網を別の収集に育てる。
# 同じ収集に混ぜると、育てたものが流れの期限で消えるうえ、`{current}` が記事で
# 埋まって、育てているものが差し込みから押し出される。
#
# **渡すのは前回この巡回が走ってから入ったぶんだけ**(`{recent}` と同じ読み方)。
# 全部を渡すと入り切らず、毎回同じものを読み直すことになる。
SOURCE_PLACEHOLDER = "{material}"

# **いま持っているものの見出しだけ**を差し込む場所。
# **`{current}` と役割が違う** —— あちらは本文まで見せるので、育ったものほど
# 1 件が重くなり、数百件で天井に当たる(そして**当たると新しいものから切れる**。
# 並びは古い順なので、いちばん見てほしい入ったばかりのものが落ちる)。
# こちらは見出しとタグだけなので、1 件がおよそ 1/6 になり、数千件でも入る。
#
# 使いどころは「**何を持っているか**」だけが要る回 —— 重なりを畳む、親子を決める、
# 同じものを二度足さない。本文の良し悪しを見る回(`{current}`)とは分けて持つ。
NAMES_PLACEHOLDER = "{names}"

# 見出しだけを差し込むときの上限。本文を持たないぶん、件数の天井は高くてよい
MAX_NAME_DOCS = 3_000

# 材料として 1 回に渡す上限。**多すぎると読み切れない**(そのぶん枠も食う)
DEFAULT_MATERIAL_LIMIT = 60
MAX_MATERIAL_LIMIT = 300

# いまの日時(日本時間)を差し込む場所。**AI はいまが何日の何時かを知らない** ——
# 学習した時点で止まっているので、聞けばそれらしい日付を作ってしまう。
# 「1 日に 2 回まとめる」のように、回ごとに違う見出しを付けさせたい場面で要る。
NOW_PLACEHOLDER = "{now}"

# 作り直しで、前世代の何割を下回ったら焼くのを断るか。
# **既定で守る側に倒す** —— AI が変な日に当たった 1 回で、育てた分類が消えるのは重い。
# 意図して減らすときは、この値を下げるか 0 にして守りを外す(画面から変えられる)。
DEFAULT_KEEP_RATIO = 0.5

# 作り直しのときにプロンプトへ差し込む前世代の上限。
# **収まらなければ切って、切ったことを AI に伝える** —— 黙って切ると、
# 見えなかったぶんを「無かったもの」として落とした答えが返る。
#
# **2 つの上限は釣り合っていないといけない。** 区画を切る側は件数(`target`)で
# 大きさを決めるので、文字数のほうが先に当たると**区画を件数どおりに切ったのに
# 中身が全部は載らない**という食い違いが起きる —— そのとき AI に届くのは
# 「載っているぶんだけを整理してください」なので、**漏れを問う前提
# (この区画の全部が並んでいる)が黙って崩れる**。
#
# 実測: 地図の名簿は機械で引いた直後が 1 行 83 文字でも、AI が説明を書いたあとは
# 249 文字になる(本文は `MATERIAL_BODY_CHARS` = 200 まで書かれ、タグも 5 つ付く)。
# 4 万文字だと 160 件で切れ、300 件という件数の上限は一度も効かなかった。
# **件数のほうが先に当たる高さに置く**(300 件 × 249 文字 ≒ 7.5 万)。
MAX_MATERIAL_DOCS = 300
MAX_MATERIAL_CHARS = 80_000
# 消したものを載せる枠。**生きているものとは別に持つ。** 同じ枠から取ると、
# **消すほど直す相手が見えなくなる** —— 精査を頼む収集ほど墓標が増えるので、
# 区画の中身が墓標で埋まり、生きているものが押し出される。
# 1 行は見出しと消した理由だけで本文を載せないぶん、同じ件数でも短い
MAX_REMOVED_DOCS = 300
MAX_REMOVED_CHARS = 20_000
# 差し込む 1 件の本文の長さ。全文を渡すと件数が入らない
MATERIAL_BODY_CHARS = 200

# 控えに残す見出しの数(足した・直した・消した、それぞれ)。**頭のほうだけ** ——
# 全部持つと定義のメモも変更履歴も太る(初期構築の 1 回で数千件が並ぶ)。
# 読むのは「何が動いたか」の手がかりであって、全件の一覧ではない
MAX_TITLE_SAMPLE = 20

# 読めなかった答えを、控えに添える長さ。**全文は AI の履歴にある** ——
# ここに要るのは「何が返ってきたか」が一目で分かるぶんだけ
MAX_SAID_CHARS = 120

# タグの確かめ方をいくつまで書けるか。**指定であって分類ではない**ので、少なくてよい
MAX_VERIFY_TAGS = 8
# 実在を確かめる問い合わせ 1 回ぶんの見出しの数(SQLite の上限に余裕を持たせる)
VERIFY_CHUNK = 400

# 収集を追加するときの下書き。**書き方が分からない人に、形を見せるためのもの**
# (画面の入力欄の placeholder)。この層はプロンプト次第でどうにでもなるぶん、
# 空の画面からは `{cursor}` の使い方を思いつけない。
#
# **ここから作られる収集は無い。** 止めた状態の見本を置いていた頃は、消しても
# 「まだ 1 件も無い」が成り立つ拍子に戻ってきた —— 消せない見本は見本ではない。
PROMPT_EXAMPLE = (
    "{cursor} 以降に出たニュースのうち、**日本で暮らす人が押さえておくべきもの**を10件、"
    "重要な順に。\n"
    "政治・経済・災害・事故・事件・国際情勢と、暮らしに影響する制度や価格の変更を優先する。"
    "芸能・ゴシップ・スポーツの勝敗・個人の炎上は入れない。\n"
    "title は見出し(同じ話題は同じ見出しにする)、"
    "body は3〜4文で「何が起きたか」と「なぜ押さえておくべきか」、"
    "tags は分野を1〜2個(政治 / 経済 / 災害 / 事件 / 国際 / 社会 / 科学 など)、"
    "url は出典。\n"
    "next_cursor には、いちばん新しいニュースの日付を YYYY-MM-DD で入れる。"
)


def require_enabled() -> None:
    """使える形になっていなければ断る。"""
    if not is_enabled():
        raise HTTPException(
            503,
            {
                "error": "collection is disabled",
                "hint": "CHIEZO_STATE_DIR(収集の定義の置き場)と CHIEZO_TRIGGER_URL"
                        "(取り込みを起こす相手)を設定すると有効になる。"
                        "集めたものは長期記憶へ焼かれるので、途中の置き場は要らない",
            },
        )


def is_enabled() -> bool:
    """定義の置き場(`state/chiezo_settings.db`)と、取り込みを起こす相手(chiezo-trigger)が
    揃っていること。

    **集めたものの置き場は持たない** —— 焼くときに作って ingest へ渡すだけ。

    trigger を条件に入れるのは、**それが無いと集める手段が 1 つも無い**から。
    集めるのは取り込みの中で起きるので、取り込みを起こせない面
    (corpus を持たないタスク専用の面など)では定義を置いても永遠に走らない。
    そこで見本の定義まで作ると、**使えない機能の設定が短期記憶に 1 件混ざる**だけになる。
    """
    return machine_store.is_enabled() and bool(os.environ.get("CHIEZO_TRIGGER_URL", "").strip())


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
    # 外向きの道具の指定(`app/feeds.py`)。RSS / Atom を機械的に取ってきて
    # `{feed}` へ差し込む。**取ってきたものをそのまま溜めるわけではない**
    feed: dict | None = None
    # 割り出した区画の台帳。1 件は `{"key", "count", "visited_at"}`。
    # **巡回の記録はここだけが持つ** —— 割り直しても引き継ぐ(`partitioning.refresh`)
    partitions: list[dict] = field(default_factory=list)
    # 巡回(`Sweep`)。**空なら定義そのものが 1 本の巡回**。
    # ざっと全体を拾うものと、少数をじっくり調べるものを別々の時計で回すために持つ
    sweeps: list[dict] = field(default_factory=list)
    # いま起こしてある取り込みが、どの巡回のものか。**取り込みは名前しか運べない**
    # (`GET /v1/collect/fetch?source=…`)ので、起こした側がここに書いて渡す
    pending_sweep: str = ""
    # **最後の 1 回を巻き戻すための控え**。設定を直してからやり直したい、が普通に
    # 起きる —— そのとき戻せるのは**定義の側だけ**(焼いた世代は 1 つ前までしか
    # 残らないので、対象の巡回が最後でなければ中身は戻せない)。
    # 戻すのは進み具合と区画の印だけで、**墓場は戻さない** ——
    # 消したのは意図してのことなので、やり直しで連れ戻さない。
    # 1 回ぶんしか持たない(「最後の 1 回」だけをやり直す口なので)
    last_undo: dict | None = None
    # 起こしてある割り込みの依頼(`Focus`)。**同じ理由でここに置く** ——
    # 取り込みは収集の名前しか運べないので、頼んだ側が書いて渡す
    pending_focus: dict | None = None
    # **次の 1 回だけの上書き**(区画の名指しと、頼む相手)。画面の「この区画で
    # 1 回走らせる」が書き、素材を組むところで読んで消える。**保存ではない** ——
    # 巡回の設定を書き換えずに 1 回だけ違う条件で走らせるためのもの。
    # **割り込み(`pending_focus`)とは別物**: あちらは進み具合も区画の印も
    # 動かさないが、こちらは**ふつうの回として走る**(印が付き、予定が進む)。
    # 枠が細いときに「直したコードを 1 区画だけ本番の形で試す」がこれ
    pending_run: dict | None = None
    # 作り直しで、前世代の何割を下回ったら断るか。0 なら守りを外す。
    # 足すほうでは使わない(そもそも減らないので)
    keep_ratio: float = DEFAULT_KEEP_RATIO
    # 最初の 1 回を機械的に埋める指定(`app/extract.py`)。無ければ毎回 AI に集めさせる。
    # **持つのは指定であって中身の知識ではない** —— どのソースのどのタグを引くかは
    # 依頼した側が書く。進み具合が空のときだけ使い、以降は AI が肉付けする
    # 収集の種類(`KINDS`)。**既定は網羅** —— 期限で落とすのは流れだけなので、
    # 知らないうちに消えるほうへは倒さない
    kind: str = KIND_STOCK
    # 流れの収集が持つ日数。0 なら期限では落とさない。**網羅では使わない**
    # (あちらは古いものが要らなくなることがない)
    keep_days: int = 0
    extract: dict | list[dict] | None = None
    # **材料に使う別のソース**(`{material}` で差し込む)。
    # `{"source": "tazuna_tech", "tag": "ニュース,記事", "limit": 60}` と書くと、
    # **前回この巡回が走ってから**そのソースに入ったものが渡る。
    # **中身は写さない** —— 読むだけで、この収集に溜まるのは AI が返したものだけ
    material: dict | None = None
    # **タグの値が実在するかを確かめる指定**。`[{"prefix": "代表作", "source": "jawiki"}]`
    # と書くと、`代表作:<見出し>` の見出しが jawiki に無いタグを**焼く前に落とす**。
    #
    # AI は「その画家の代表作」を挙げられても、**それが記事として存在するかは知らない**
    # —— 読む側は「タグがある = 押せば何か出る」と受け取るので、実在しない見出しが
    # 混ざると、押しても何も出ないものが並ぶ。**確かめられるのはこちら**(長期記憶を
    # 持っているのはこの層)なので、集める側で落とす
    verify_tags: list[dict] = field(default_factory=list)
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
    # **機械で引く巡回**(`extract` の指定を、AI を呼ばずにもう一度走らせる)。
    # **名簿を最新に保つための回**で、外のカテゴリは増えていくのに、機械で埋めるのは
    # 「進み具合が空の 1 回目だけ」だった —— そのあと増えたぶんは永遠に入らない。
    # **足すだけ(`only_new`)と組にして使う** —— 組にしないと、AI が肉付けしたぶんを
    # 名簿の薄い内容で上書きする(機械は影響関係も代表作も持っていない)
    use_extract: bool = False
    # **外の道具で引く巡回**(`app/feeds.py`)。RSS / Atom が配っている見出し・要約・
    # URL・配信日を、AI を呼ばずにそのまま溜める。取りこぼしも宣伝記事も引き受ける
    # 代わりに、**枠を使わずに毎時回せる** —— 重要度を付ける・まとめる・漏れを探す、
    # といった判断の要る仕事は別の巡回が AI に頼む(名簿と肉付けを分けるのと同じ形)
    use_feed: bool = False
    # **一度きりの巡回**。走ったら時計を持たなくなる(押せばまた走る)。
    # 名簿のように、元のデータが変わらない限り何度やっても同じ回のためのもの ——
    # 毎週回しても結果は変わらず、その 1 回ぶんの取り込みが無駄になる
    once: bool = False
    # **一周したら止まる巡回**。区画の材料を埋めるような、一度行き渡れば用の済む回。
    # 止めないと 2 周目 3 周目が回り続け、**同じことを何度も聞くために枠を使う** ——
    # そのあとの手入れは、区画ごとに回る回が引き継ぐ
    one_lap: bool = False
    # **先に一周してほしい巡回**。その巡回が全区画を一度でも見終えるまで走らない。
    # 区画の材料(分類や数のタグ)を埋める回が先に一周していないと、後の回は
    # 「この区画に居ない」を理由に見当違いのことをする —— 実際、名簿を作った直後の
    # 漏れ探しは、既に名簿に居る画家を 20 人挙げて終わった
    after: str = ""
    # **誰に頼むかを、名前で外へ出す**(`app/workers.py`)。書いてあれば、
    # 上の `backend` / `model` / `effort` ではなく**ワーカーの並びから選ぶ** ——
    # 枠に余裕のある先頭に頼み、どれも詰まっていればその回は走らせない。
    # 網羅の収集は端から端まで精査し続けるもので、**1 つの相手の枠で回し切れるとは
    # 限らない** —— 詰まったところで止まるのではなく、振り替えて回り続けてほしい
    worker: str = ""
    # **足すだけの巡回**。既にある見出しが返ってきても触らない ——
    # 「漏れているものを足す」を頼む回に要る印で、**AI の判断に頼らずに保証する**。
    # 見せられるのはその区画のぶんだけなので、AI には「もう居るかどうか」が分からない
    # (別の括りに入っていることも、タグが間違っていることもある)。触らせると、
    # 既にいる有名なものが薄い内容で上書きされ、持っていたタグごと落ちる。
    only_new: bool = False
    # **手で回す巡回**(`app/handoff.py`)。AI を呼ばずに、いつもどおり組んだ依頼文を
    # **ファイルとして人に渡す** —— web の画面から使う AI(Gemini など)は、
    # 鍵も API も無い代わりに調べものが速い。答えのファイルを読み込ませれば、
    # そこから先は AI に頼んだ回とまったく同じ道を通る(`collect.parse_response`)。
    # **時計では走らない** —— 人の手が入るので、次の束を作るのは「答えが入ったとき」
    # (区画を回る収集)か「押したとき」。予定で起こすと、答えの無い束が溜まる
    by_hand: bool = False
    # **時計を持たない巡回**(割り込み用)。定時には走らず、頼まれたときだけ動く。
    # 相手・モデル・考える量を**定時のものとは別に決めておく**ためにある ——
    # 割り込みは人が待っている場面なので、速い相手に頼みたい / 逆に 1 件を
    # じっくり調べさせたい、のどちらもあり、定時の巡回の設定を流用できない
    on_demand: bool = False
    next_run_at: str | None = None
    last_run_at: str | None = None
    last_status: str | None = None
    last_error: str | None = None

    def due_at(self) -> datetime:
        """次に走る時刻。持っていなければ「いますぐ」。

        **時計を持たない巡回は「来ない」**(`NEVER`)—— 予定が空なのを「いますぐ」と
        読む規則をそのまま当てると、割り込み用の巡回が毎周走ってしまう。
        """
        if self.on_demand or self.by_hand or (self.once and self.last_run_at):
            return NEVER
        return _parse(self.next_run_at) or _now()

    def is_due(self, at: datetime | None = None) -> bool:
        """**予定を持っていない巡回は、いますぐ走る。** 足したばかりの巡回がそれで、
        「いま」を取り直して比べると必ず未来になり、永遠に走らない(実際にそうなった)。

        **時計を持たない巡回は、定時には走らない**(頼まれたときだけ)。

        **手で回す巡回も走らない**(`by_hand`)—— 走らせる中身(人が持ち帰った
        答え)が揃うのは読み込んだときなので、予定で起こすと空振りする。
        次の束を組むのは「答えが焼けたとき」か「押したとき」。

        **一度きりの巡回は、走ったらもう走らない**(`once`)—— 元のデータが変わらない
        限り何度やっても同じなので、その 1 回ぶんの取り込みが無駄になる。
        押せばいつでも走る(口のほうでは断らない)。
        """
        if self.on_demand or self.by_hand or (self.once and self.last_run_at):
            return False
        at = at or _now()
        return self.enabled and (_parse(self.next_run_at) or at) <= at

    def walks_partitions(self) -> bool:
        """この回が区画を歩くか。**機械で引く回は歩かない**。

        指定を 1 本引いて全部を返すので、区画に印を付けない —— 付けると、
        見てもいない区画が「回り終えた」に混ざる(一周が嘘になる)。

        **一周を数える側はここを見る。** 歩かない回の一周は「1 回走ったこと」で、
        区画の印では永遠に数え終わらない。
        """
        return not (self.use_extract or self.use_feed)

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

    def cycle_days(self, total_partitions: int) -> float:
        """一周に**実際にかかる**日数。数えられないときは 0。

        **`cover_days` は希望であって結果ではない。** 1 回に見る区画には天井が
        あり(`MAX_PARTITIONS_PER_RUN`)、区画が多いとそこで頭打ちになる ——
        本番で「15 日で一周」と書いた巡回が、8,185 区画・60 分ごと・1 回 5 区画で
        実際には 68 日かかっていた。**画面に書いた日数をそのまま出していたので、
        4 倍のずれがどこにも出ていなかった。**

        **区画数はそのつど渡す。** 母集団が動けば区画も動くので、控えた値を
        持つと古くなる(減ったのに長いまま出る、が起きる)。
        """
        if self.on_demand or total_partitions <= 0 or self.interval_minutes <= 0:
            return 0.0
        runs_per_day = 24 * 60 / self.interval_minutes
        return total_partitions / (self.per_run(total_partitions) * runs_per_day)

    def applied_to(self, item: Collection, step=None) -> Collection:
        """この巡回の相手・モデル・深さを載せた定義(AI へ投げるときに使う)。

        **ワーカーが選んだ相手があればそちらが勝つ**(`step`)。巡回に書いてある
        相手は「ワーカーを使わないとき」の指定で、両方書いてあるときに
        どちらで走ったのか読めないほうが困る。
        """
        if step is not None:
            # **段は考える量を持たない**(モデルの名前に畳んである)。
            # 巡回側の指定も引き継がない —— 相手が変わっているので、
            # そちら向けに書かれた考える量は意味を持たない
            return replace(item, backend=step.backend, model=step.model or None, effort=None)
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
        on_demand=bool(raw.get("on_demand")),
        only_new=bool(raw.get("only_new")),
        use_extract=bool(raw.get("use_extract")),
        use_feed=bool(raw.get("use_feed")),
        by_hand=bool(raw.get("by_hand")),
        once=bool(raw.get("once")),
        one_lap=bool(raw.get("one_lap")),
        after=str(raw.get("after") or "").strip()[:40],
        worker=str(raw.get("worker") or "").strip()[:40],
        next_run_at=raw.get("next_run_at") or None,
        last_run_at=raw.get("last_run_at") or None,
        last_status=raw.get("last_status") or None,
        last_error=raw.get("last_error") or None,
    )


def normalize_kind(raw) -> str:
    """収集の種類を均す。**読めない値は網羅に倒す**(期限で消えないほう)。"""
    value = str(raw or "").strip()
    return value if value in KINDS else KIND_STOCK


def normalize_keep_days(raw, kind: str) -> int:
    """持つ日数を均す。**網羅では常に 0**(古いものが要らなくなることがない)。"""
    if kind != KIND_FLOW:
        return 0
    if raw in (None, ""):
        return DEFAULT_KEEP_DAYS
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_KEEP_DAYS


def expired_docs(item: Collection, docs: list[dict]) -> tuple[list[dict], int]:
    """期限を過ぎた文書を落とす。落とした数も返す。

    **流れの収集だけ**(`KIND_FLOW`)。あちらは時とともに増える流れを追っていて、
    古いものは順に要らなくなる —— 落とさないと、ニュースの収集は永久に増え続ける。
    **網羅では何もしない** —— そちらは古いものが要らなくなることがなく、
    端から端まで精査し続ける対象なので、期限で消すと穴が開く。

    **日付は記事のほう**(`extra.published_at`)を先に見る。集めた日だけで数えると、
    半年前の記事を今日拾ったものが 30 日生き残る。
    """
    if item.kind != KIND_FLOW or item.keep_days <= 0 or not docs:
        return docs, 0
    limit = _iso(_now() - timedelta(days=item.keep_days))
    kept = [d for d in docs if _doc_time(d) >= limit]
    return kept, len(docs) - len(kept)


def _doc_time(doc: dict) -> str:
    """その 1 件の日付。**読めなければ「いま」として扱う**(落とさない側へ倒す)。"""
    extra = doc.get("extra") or {}
    for value in (extra.get("published_at"), extra.get("collected_at"), doc.get("updated_at")):
        if isinstance(value, str) and value.strip():
            return value
    return _iso(_now())


def normalize_verify_tags(raw) -> list[dict]:
    """タグの確かめ方を均す。**読めない指定は断る**(黙って無視すると効かないまま回る)。"""
    if raw in (None, "", []):
        return []
    if not isinstance(raw, list):
        raise HTTPException(400, {"error": "verify_tags は配列で書いてください"})
    out = []
    for one in raw[:MAX_VERIFY_TAGS]:
        if not isinstance(one, dict):
            raise HTTPException(400, {"error": "verify_tags の中身はオブジェクトで書いてください"})
        prefix = str(one.get("prefix") or "").strip()
        source = str(one.get("source") or "").strip()
        if not prefix or not source:
            raise HTTPException(400, {
                "error": "verify_tags には prefix(タグの頭)と source(照らすソース名)が要ります",
            })
        out.append({"prefix": prefix, "source": source})
    return out


def verified_docs(item: Collection, docs: list[dict], sources: dict) -> tuple[list[dict], int]:
    """実在しない見出しを指すタグを落とす。落とした数も返す。

    **焼く前に、全部に対して掛ける**(集めたぶんだけではない)。既に入っているものにも
    同じ指定を当てないと、前に入った間違いが残り続ける —— 1 回焼き直せば揃う。

    **照らす先が無ければ何もしない**(ソースがまだ焼かれていない等)。
    知らないものを「無い」と読むと、正しいタグまで落ちる。
    """
    rules = normalize_verify_tags(item.verify_tags)
    if not rules or not docs:
        return docs, 0
    dropped = 0
    out = docs
    for rule in rules:
        src = sources.get(rule["source"])
        if src is None:
            continue
        head = rule["prefix"] + ":"
        wanted = {
            _tag_head(tag, head)
            for doc in out
            for tag in (doc.get("tags") or [])
            if str(tag).startswith(head)
        }
        wanted.discard("")
        if not wanted:
            continue
        alive = _existing_titles(src.path, wanted)
        kept = []
        for doc in out:
            tags = doc.get("tags") or []
            keep = [
                tag for tag in tags
                if not str(tag).startswith(head) or _tag_head(tag, head) in alive
            ]
            dropped += len(tags) - len(keep)
            kept.append({**doc, "tags": keep} if len(keep) != len(tags) else doc)
        out = kept
    return out, dropped


def _tag_head(tag, prefix: str) -> str:
    """`代表作:印象・日の出` → `印象・日の出`。**3 つ組のタグは 1 つ目だけを見る**
    (`影響元:名前:理由` のような形があるため)。"""
    return str(tag)[len(prefix):].split(":", 1)[0].strip()


def _existing_titles(path, titles: set[str]) -> set[str]:
    """そのソースに実在する見出しだけを返す。**問い合わせは分けて投げる**
    (SQLite が 1 文に取れる値の数に上限があるため)。"""
    found: set[str] = set()
    ordered = list(titles)
    for start in range(0, len(ordered), VERIFY_CHUNK):
        chunk = ordered[start:start + VERIFY_CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = db.query(path, f"SELECT title FROM docs WHERE title IN ({marks})", tuple(chunk))
        found.update(row["title"] for row in rows)
    return found


def normalize_sweeps(raw) -> list[dict]:
    """巡回の一覧を均す。**名前が鍵**なので、空や重複は落とす。

    区画の記録が名前で引かれる(`partitions[].visits`)ため、同じ名前が 2 本あると
    片方の進み具合がもう片方に化ける。

    **相手の欄に書かれたワーカーを取り出す**(`workers.OPTION_PREFIX`)。
    外のアプリは `/v1/ai/backends` の一覧から 1 つ選んで送ってくるので、
    **ワーカーがその一覧に混ざる以上、届く先も同じ欄**になる —— 欄を分けると、
    一覧から選んだ値を呼ぶ側が振り分けることになり、**どこに入れるかを知っている
    のが Chiezo だけ**という状態が残る。`worker` に直に書く形も今までどおり通る。
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
        made = {**item, "name": name, **_worker_picked(item)}
        if made.get("by_hand"):
            # **手で回す回にワーカーは持たせない。** 渡す先はそのときの枠で決まる
            # 仕組みなのに、この回は AI へ投げない —— 持たせると**ワーカーの
            # 待ち行列に積まれ、答えの無い取り込みが毎周走る**(本番で、
            # ワーカーを選んだままの巡回を手で回す回へ変えてそうなった)。
            # **相手とモデルも落とす**(機械で引く回と同じ理由で、読まれない)
            made.update(worker="", backend=None, model=None, effort=None)
        out.append(made)
    return out


def _worker_picked(raw: dict) -> dict:
    """相手の欄がワーカーを指していれば、その巡回に上書きする項目。

    **モデルと考える量も落とす。** どの相手に渡るかはそのときの枠で決まるので、
    ここに 1 つ書いても**どの相手に対する指定なのかが決まらない**
    (モデルの名前は相手ごとに違う)。段ごとの指定はワーカーの側が持つ。
    """
    if not (picked := workers.ref_in(str(raw.get("backend") or ""))):
        return {}
    return {"worker": picked, "backend": None, "model": None, "effort": None}


# 巡回が自分で持っている進み具合。**設定を送り直しても引き継ぐ** ——
# 設定を書く側(外のアプリや画面のフォーム)はこれらを持っていないので、
# 素直に置き換えると押すたびに時計が巻き戻る。
SWEEP_PROGRESS_FIELDS = ("next_run_at", "last_run_at", "last_status", "last_error")


def _keep_schedule(incoming: list[dict], current: list[dict]) -> list[dict]:
    """送られてきた巡回に、**同じ名前の巡回が持っていた進み具合を引き継ぐ**。

    **これが無いと、設定を更新するたびに全部の巡回がいますぐ走る。** 予定を
    持っていない巡回は「いますぐ」として扱う規則(`Sweep.is_due`)があるので、
    次回の予定を落とした瞬間に全部が due になる —— 押した人は設定を直しただけの
    つもりなのに、収集が 1 本走り出す(実際にそうなった)。前回の結果も同じ理由で
    引き継ぐ: 落とすと画面の「前回」が「まだ」に戻り、動いていないように見える。

    **名前が鍵**(区画の巡回記録と同じ)。名指しで書かれていればそちらが勝つので、
    予定を明示的に動かしたい呼び出しは今までどおり通る。
    """
    before = {raw.get("name"): raw for raw in (current or []) if isinstance(raw, dict)}
    out = []
    for raw in incoming:
        kept = before.get(raw["name"]) or {}
        carried = {
            key: kept[key]
            for key in SWEEP_PROGRESS_FIELDS
            if key not in raw and kept.get(key) is not None
        }
        out.append({**raw, **carried})
    return out


def sweep_named(item: Collection, name: str | None) -> Sweep:
    """名前で引く。**知らない名前なら、次に走るはずの巡回へ倒す** ——
    巡回を消したあとに走りかけの取り込みが素材を取りに来ることがある。
    """
    sweeps = sweeps_of(item)
    for sweep in sweeps:
        if sweep.name == name:
            return sweep
    # **倒す先は、走れる回だけ。** 時計を持たない回(割り込み)と手で回す回は
    # 自分から走らない —— そこへ倒すと、名前を取り違えた取り込みが
    # 「人が持ち帰った答え」を待つ回として走り、何も無いまま断られる
    due = [s for s in sweeps if s.enabled and not s.on_demand and not s.by_hand]
    return min(due or sweeps, key=lambda s: s.due_at())


def sweep_for_focus(item: Collection, focus: Focus | None = None) -> Sweep:
    """割り込みを走らせるときの設定(相手・モデル・考える量・指示文)。

    **割り込みも巡回として定義しておく**(`on_demand`)。定時のものと同じ書き方で
    置いておけるので、頼む相手を割り込みだけ別にできる —— 人が待っている場面なので
    速い相手に頼みたい / 1 件をじっくり調べさせたい、のどちらもあり、
    定時の巡回の設定を流用すると「どちらの都合で選んだ相手か」が言えなくなる。

    **名指しがあればそれを使う**(`focus.sweep`)—— 「じっくりの相手で、いま 1 回だけ」
    を頼めるようにするため。無ければ時計を持たない巡回、それも無ければ
    `sweep_named` と同じ倒し方をする(定義していない収集でも割り込みは頼める)。
    """
    sweeps = sweeps_of(item)
    named = (focus.sweep if focus else None) or None
    if named:
        for sweep in sweeps:
            # **手で回す回は割り込みに使えない。** あれは人が持ち帰った答えを
            # 焼く回で、その場で AI に聞く道を持たない —— 名指しされても、
            # 走れる回へ倒すほうがよい(割り込みは人が待っている場面)
            if sweep.name == named and not sweep.by_hand:
                return sweep
    for sweep in sweeps:
        if sweep.on_demand and not sweep.by_hand:
            return sweep
    return sweep_named(item, None)


@dataclass(frozen=True)
class Focus:
    """割り込み —— 「ここが間違っているから直して」を、巡回とは別の道で頼む。

    **定時の巡回に影響を出さない**のが約束。進み具合(`cursor`)も、どの巡回の予定も、
    区画の巡回記録も動かさない —— 動くのは中身だけ。そうでないと、割り込むたびに
    一周が伸びたり、見ていない区画に印が付いたりする。

    **名指しできる**(`titles`)。「この見出しのここが間違っている」を指せないと、
    訂正そのものが頼めない —— 区画を渡すだけでは、直してほしい 1 件が
    `{current}` に載る保証がない。

    **割り込みは必ず「直す」側で走る**(整理の約束で AI に頼む)。足すだけの収集でも、
    名指しで渡された 1 件を直せなければ割り込みの意味が無い。
    """

    note: str
    partition: str | None = None
    titles: list[str] = field(default_factory=list)
    # **その回だけの依頼文**(`{cursor}` などの差し込み口はいつもどおり効く)。
    # **保存しない** —— 定義のプロンプトは育てながら使うもので、1 回きりの頼みごとで
    # 書き換わると、次の定時の回が知らない文で走ることになる
    prompt: str | None = None
    # どの巡回の設定(相手・モデル・考える量)で走らせるか。書かなければ
    # 時計を持たない巡回(`on_demand`)
    sweep: str | None = None
    requested_by: str = ""
    requested_at: str = ""

    def to_json(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if v}


def normalize_focus(raw) -> Focus | None:
    """割り込みの依頼を均す。指示文が無ければ受け付けない。

    **指示文を必須にする** —— 何をどう直すかが書かれていない割り込みは、
    ただの 1 回ぶんの AI の呼び出しにしかならない(巡回でやれば済む)。
    """
    if not isinstance(raw, dict):
        return None
    note = str(raw.get("note") or "").strip()[:MAX_FOCUS_NOTE_CHARS]
    if not note:
        return None
    titles = [
        notes.title_key(t)
        for t in (raw.get("titles") or []) if str(t).strip()
    ]
    return Focus(
        note=note,
        partition=str(raw.get("partition") or "").strip() or None,
        titles=titles[:MAX_FOCUS_TITLES],
        prompt=str(raw.get("prompt") or "").strip()[:MAX_PROMPT_CHARS] or None,
        sweep=str(raw.get("sweep") or "").strip()[:40] or None,
        requested_by=str(raw.get("requested_by") or "").strip()[:80],
        requested_at=str(raw.get("requested_at") or "") or _iso(_now()),
    )


# **1 回の試し撃ちで選べる区画の数。** 区画 1 つにつき AI を 1 回呼ぶので、
# 選びすぎると枠が一息に消える —— 枠が細いときに使う口なので、ここで止める。
# 定時の巡回の天井(`MAX_PARTITIONS_PER_RUN`)とは別に持つ: あちらは無人で
# 回るぶんの目安で、こちらは人が選んで押すぶん(そのぶん少し広くてよい)。
MAX_TRIAL_PARTITIONS = int(os.environ.get("CHIEZO_MAX_TRIAL_PARTITIONS", "20") or 20)
# 名指しの回に添える補足の長さの上限(依頼文に足すので、長すぎると本題を押し流す)
MAX_RUN_NOTE_CHARS = 4000


def normalize_run_once(raw) -> dict | None:
    """**次の 1 回だけの上書き**を均す。中身が無ければ None。

    枠が細いときに「直したコードを、1 区画だけ、空いている相手で、**本番の形で**
    試したい」が普通に起きる。今までの道はどれも足りなかった:

    - **今すぐ実行** …… 区画も相手も選べず、1 回で `per_run` 区画ぶんの枠が消える
      (食事処なら 5 回の呼び出し)
    - **ドライラン** …… 焼かないので、**焼く側で落ちる回を再現しない**
    - **割り込み** …… 区画は選べるが、**進み具合も区画の印も動かない**
      (試したいのは「ふつうの回」なので、動いてくれないと確かめたことにならない)
    - **巡回の設定を直す** …… 直したまま定時の回が走り出す

    だからここは**ふつうの回として走る**(印が付き、予定も進み具合も進む)。
    上書きするのは「どこを見るか」と「誰に頼むか」の 2 つだけ。

    **相手を替えたらモデルと考える量は引き継がない**(`_asked_of`)——
    相手が変われば通る名前も違う(`sonnet` は codex には無い)。
    **ワーカーも指定できる** —— 空いている相手へ回したいときに、いちばん効くのが
    そこだから(枠を見て選ぶのはワーカーの仕事)。
    """
    if not isinstance(raw, dict):
        return None
    # **複数の区画を選べる。** 1 件ずつしか走らせられなかった頃は、直したところを
    # 何区画かまとめて確かめたいときに、区画の面を開き直して 1 回ずつ押すことに
    # なった。**1 つだけ書いた形も受ける**(区画の面からはそちらで飛んでくる)
    # **区画ごとの補足**(`notes`。区画の鍵 → その区画のぶんの補足)。1 回で何区画も
    # 見るとき、全部の区画に同じ補足を渡すと、**その区画を見ていない AI まで
    # 範囲の外の依頼に手を出す** —— 見出しで引き当てて直すので、見せていない 1 件の
    # 本文とタグを、中身を知らないまま書き換えられる。区画ごとに、その区画に入る
    # ぶんだけを渡す
    notes = {}
    raw_notes = raw.get("notes")
    if isinstance(raw_notes, dict):
        for key, text in raw_notes.items():
            key, text = str(key or "").strip(), str(text or "").strip()
            if key and text:
                notes[key] = text[:MAX_RUN_NOTE_CHARS]
    keys, seen = [], set()
    # **補足を書いた区画は、名指ししたことにする**(書き漏らしても黙って落とさない)
    for value in [*(raw.get("partitions") or []), raw.get("partition"), *notes]:
        key = str(value or "").strip()
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    keys = keys[:MAX_TRIAL_PARTITIONS]
    notes = {k: v for k, v in notes.items() if k in keys}
    out = {
        "partitions": keys,
        "backend": str(raw.get("backend") or "").strip()[:60],
        "model": str(raw.get("model") or "").strip()[:80],
        "effort": str(raw.get("effort") or "").strip()[:20],
        "worker": str(raw.get("worker") or "").strip()[:40],
        # **その回だけの補足**(外から届いた依頼など)。依頼文の後ろに足す
        # (`with_run_note`)。**保存しない** —— 定時の回が知らない話で走らないように
        "note": str(raw.get("note") or "").strip()[:MAX_RUN_NOTE_CHARS],
        "notes": notes,
    }
    kept = {k: v for k, v in out.items() if v}
    return kept or None


def with_run_note(sweep: Sweep, base_prompt: str, note: str | None) -> Sweep:
    """その 1 回だけ、依頼文の後ろに補足を足した巡回。**定義は書き換えない**。

    **いつもの仕事は変えない。** 割り込み(`render_focus`)は「ここに書かれていない
    ものは触らない」に絞るが、こちらはふつうの回 —— 区画をいつもどおり見たうえで、
    補足に書かれたことも確かめてもらう。**書かれていることを鵜呑みにしない**よう
    頼む(外から届いた依頼で、間違っていることもある)。

    **範囲の外には手を出させない。** 1 回で何区画も見るとき、AI は区画ごとに
    呼ばれ、見せるのはその区画の中身だけ —— 範囲の外の依頼に答えると、見ていない
    1 件を中身を知らないまま書き換えることになる(別の区画の回がそれを見ている)。
    区画ごとの補足(`notes`)ならそもそも範囲の外は渡らないが、全部に同じものを
    渡す補足(`note`)ではここが歯止めになる。

    **正しい値は AI に調べさせる。** 届く依頼はたいてい「ずれている」「違う」までで、
    正しい値を書いてこない。「確かめてから反映」だけだと、AI は確かめる材料が
    無いと読んで手元の値をそのまま返す —— 座標のずれを頼まれた 1 件が、同じ
    緯度経度のまま「直した」に数えられていた(本番で起きた)。確かめるとは
    正しい値を自分で調べることだ、と言い切る。
    """
    note = (note or "").strip()
    if not note:
        return sweep
    prompt = (sweep.prompt or base_prompt or "").rstrip()
    return replace(
        sweep,
        prompt=prompt
        + "\n\n【この回の補足】\n"
        + "この回では、いつもの仕事に加えて、次の依頼にも答えてください。"
        + "外から届いた依頼で、間違っていることもあります。**書かれていることを"
        + "確かめてから**反映し、確かめられなかったものは触らないでください。"
        + "**「ずれている」「違う」とだけ書かれていて正しい値が無いときは、"
        + "正しい値を自分で調べてください**(web 検索や Chiezo の辞典)。"
        + "場所なら、名前と住所から正しい緯度経度を求めて lat / lon に入れて返します。"
        + "手元の値をそのまま返しても直したことにはなりません。"
        + "**この回の範囲の外にあるものには答えず、触らないでください**"
        + "(別の回が見ます)。\n\n"
        + note,
    )


def asked_for_run(sweep: Sweep, override: dict | None) -> Sweep:
    """その 1 回だけ、頼む相手を差し替えた巡回。**定義は書き換えない**。

    書き換えると、試し撃ちのつもりが次の定時の回にも効く —— 枠が細いときに
    いちばん避けたい壊れ方(気づくのは枠が尽きてから)。
    """
    if not override:
        return sweep
    if worker := override.get("worker"):
        # **ワーカーに渡す回は、モデルも考える量も持たせない** —— どの相手に
        # 渡るかはそのときの枠で決まるので、ここで 1 つ書いても意味が決まらない
        return replace(sweep, worker=worker, backend=None, model=None, effort=None)
    if not override.get("backend"):
        return sweep
    return replace(
        sweep,
        worker="",
        backend=override["backend"],
        model=override.get("model") or None,
        effort=override.get("effort") or None,
    )


def _stored() -> str | None:
    """**まとめて 1 件に入れていた頃**の定義(JSON の文字列)。無ければ None。

    いまは 1 収集 = 1 件(`collect/<名前>`)。ここを読むのは移行のためだけ。
    """
    return machine_store.get(DEFS_KIND, DEFS_KEY)


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
        feed=feeds.to_json(feeds.normalize(item.get("feed"))),
        partitions=partitioning.normalize_ledger(item.get("partitions")),
        sweeps=normalize_sweeps(item.get("sweeps")),
        pending_sweep=str(item.get("pending_sweep") or ""),
        last_undo=item.get("last_undo") if isinstance(item.get("last_undo"), dict) else None,
        pending_focus=(
            focus.to_json() if (focus := normalize_focus(item.get("pending_focus"))) else None
        ),
        pending_run=normalize_run_once(item.get("pending_run")),
        keep_ratio=normalize_keep_ratio(item.get("keep_ratio")),
        extract=extraction.to_json(extraction.normalize(item.get("extract"))),
        material=normalize_material(item.get("material")),
        kind=normalize_kind(item.get("kind")),
        keep_days=normalize_keep_days(item.get("keep_days"), normalize_kind(item.get("kind"))),
        verify_tags=normalize_verify_tags(item.get("verify_tags")),
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


def normalize_material(raw) -> dict | None:
    """材料に使う別ソースの指定を均す。**読めない指定は断る**。

    黙って無視すると、差し込み口だけが残った依頼文で走り続ける ——
    AI からは「渡されるはずのものが空だった」としか見えない。

    **ソースがあるかはここでは見ない。** 定義を置く時点ではまだ 1 度も焼かれて
    いないことがあり(収集は作った直後が空)、存在で断ると鶏と卵になる。
    走るときに無ければ、その旨を差し込みへ書く。
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise HTTPException(400, {"error": "material はオブジェクトで書いてください"})
    source = str(raw.get("source") or "").strip()
    if not source:
        raise HTTPException(400, {
            "error": "material.source(材料に読むソース名)を入れてください",
            "hint": "例: {\"source\": \"tazuna_tech\", \"tag\": \"ニュース,記事\"}",
        })
    limit = raw.get("limit")
    try:
        limit = int(limit) if limit not in (None, "", 0) else DEFAULT_MATERIAL_LIMIT
    except (TypeError, ValueError):
        raise HTTPException(400, {"error": "material.limit は数で書いてください"}) from None
    return {
        "source": source[:80],
        # **タグで絞れる。** 1 つの収集に種類の違うものが混ざる(記事とまとめ、など)
        # ので、絞れないと材料にまとめが混ざる
        "tag": str(raw.get("tag") or "").strip()[:200],
        "limit": min(max(limit, 1), MAX_MATERIAL_LIMIT),
    }


def hidden_clause(src, include_hidden: bool = False) -> tuple[str, list]:
    """**読者に出さない印の付いたものを外す**条件(`notes.HIDDEN_TAGS`)。

    返すのは `AND` も `WHERE` も付かない条件だけ —— 呼ぶ側の組み立てに合わせる。

    **1 か所に持つ。** 印は増える(消えたもの・まだ AI が目を通していないもの)ので、
    読む場所ごとに書き写すと、次に足したときに片方だけ直すことになる。

    **タグの索引を持たない古いソースでは何もしない**(絞りようが無い)。
    """
    if include_hidden or src.schema_version < TAG_MIN_SCHEMA_VERSION:
        return "", []
    marks = ", ".join("?" * len(notes.HIDDEN_TAGS))
    return (
        f" doc_id NOT IN (SELECT dt.doc_id FROM doc_tags dt WHERE dt.tag IN ({marks}))",
        list(notes.HIDDEN_TAGS),
    )


def material_docs(spec: dict | None, sources: dict, since: str | None) -> list[dict]:
    """材料に読むソースから、**前回から入ったもの**を新しい順に。

    **まだ焼かれていないソースは空**(失敗ではない) —— 材料の側の収集がまだ 1 度も
    走っていないだけのことがある。

    **読者に出さない印の付いたものは渡さない**(`notes.HIDDEN_TAGS`)。材料に別の
    収集を選ぶのは「**そちらの AI が選り分けた後のもの**を読みたい」からで、
    配信元から引き直さない理由もそこにある —— 消したものやまだ読まれていないものを
    混ぜると、**整理が落とした宣伝や重複から次の収集が育つ**ことになる。
    """
    if not spec:
        return []
    src = sources.get(spec["source"])
    if src is None:
        return []
    tags = [t.strip() for t in (spec.get("tag") or "").split(",") if t.strip()]
    where = "WHERE updated_at > ?"
    args: list = [since or ""]
    if tags:
        where += (
            " AND doc_id IN (SELECT doc_id FROM doc_tags WHERE tag IN"
            f" ({','.join('?' * len(tags))}))"
        )
        args.extend(tags)
    hidden, marks = hidden_clause(src)
    if hidden:
        where += " AND" + hidden
        args.extend(marks)
    args.append(int(spec.get("limit") or DEFAULT_MATERIAL_LIMIT))
    rows = db.query(
        src.path,
        "SELECT title, opening, tags, updated_at, extra FROM docs"
        f" {where} ORDER BY updated_at DESC LIMIT ?",
        tuple(args),
    )
    return [
        {
            "title": row["title"],
            "opening": row["opening"],
            "tags": load_tags(row["tags"]),
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        }
        for row in rows
    ]


def render_source_material(spec: dict | None, docs: list[dict]) -> str:
    """材料をプロンプトへ差し込める形にする。

    **何のソースを読んだかを書く** —— 空だったときに、指定が違うのか、向こうに
    何も入っていないだけなのかが読めないと直しようがない。
    """
    if not spec:
        return "(この収集に材料のソースは指定されていません)"
    if not docs:
        return f"(「{spec['source']}」に、前回から新しく入ったものはありません)"
    lines = []
    used = 0
    for doc in docs:
        tags = "/".join(doc.get("tags") or [])
        body = (doc.get("opening") or "")[:MATERIAL_BODY_CHARS].replace("\n", " ")
        extra = doc.get("extra") or {}
        line = (
            f"- {doc['title']}"
            + (f" 【{tags}】" if tags else "")
            + (f" — {body}" if body else "")
            + (f" <{extra['url']}>" if extra.get("url") else "")
        )
        if used + len(line) > MAX_MATERIAL_CHARS:
            lines.append(f"…(ここまで。「{spec['source']}」にはこの続きもあります)")
            break
        lines.append(line)
        used += len(line)
    return f"「{spec['source']}」に前回から入ったもの:\n" + "\n".join(lines)


def normalize_keep_ratio(value) -> float:
    try:
        ratio = float(value)
    except (TypeError, ValueError):
        return DEFAULT_KEEP_RATIO
    return min(max(ratio, 0.0), 1.0)


def edits_what_is_there(prompt: str, only_new: bool = False) -> bool:
    """その回が、**既にあるものを直す回か**。

    収集ぜんたいの設定として持っていた頃(`mode`)は、巡回ごとに決められなかった。
    いまは巡回ごとに決まるので、収集の側に置く意味が無い —— **依頼文がそれを
    語っている**。今あるものを差し込んでいる(`{current}` / `{recent}` / `{names}`)なら、
    AI は「直すものと足すものだけ返す」仕事をしていて、墓標で消すこともできる。
    差し込んでいないなら、AI は今あるものを知らないので、消す力を持たせられない。

    **足すだけの回は、差し込んでいても直す回ではない**(`only_new`)——
    漏れを足す回は今あるものを見せるが、触れてはいけない。
    """
    if only_new:
        return False
    return any(
        p in (prompt or "")
        for p in (MATERIAL_PLACEHOLDER, RECENT_PLACEHOLDER, NAMES_PLACEHOLDER)
    )


def load() -> list[Collection]:
    """定義の一覧(並びは作った順)。**1 収集 = 1 件**(`collect/<名前>`)。

    **まとめて 1 件に入れない。** 区画の台帳は収集 1 つで MB 単位になりうるので、
    全部を 1 つの JSON に入れると、**どれか 1 つを直すだけで全部を読み書きする**
    ことになる —— 1 つが壊れれば全部が読めなくなり、1 つが太れば全部が重くなる。
    (**上限を置いて逃げない**ための前提でもある。台帳の天井は、この形にして
    初めて「1 つの収集の都合」に閉じられる。)

    **本文が壊れていたら黙って作り直さない** —— 中身ごと消えるので、読めないことを
    見せて人に直させる(プロジェクトと同じ判断)。**壊れているのはその 1 件だけ**
    だと分かるように、名前を添えて上げる。
    """
    _split_out_the_old_row()
    out = []
    for key in machine_store.keys(DEFS_KIND):
        if key == DEFS_KEY:
            continue
        body = machine_store.get(DEFS_KIND, key)
        if body is None:
            continue
        try:
            raw = json.loads(body)
            if not isinstance(raw, dict):
                raise TypeError("object を入れてください")
        except (ValueError, TypeError) as e:
            raise HTTPException(400, {"error": f"{DEFS_BROKEN}(「{key}」): {e}"}) from None
        out.append(_from_json(raw))
    # **並びは作った順**。1 件ずつ置くと配列の順を持てないので、作った時刻で並べ直す
    return sorted(out, key=lambda c: (c.created_at, c.name))


def _split_out_the_old_row() -> None:
    """まとめて 1 件に入れていた頃のものを、1 収集 1 件へ分ける。**1 度だけ**。

    **古い行が残っているあいだは、そちらが正**(分け終えてから消す)——
    途中で落ちても、次に読むときにもう一度分け直せる。
    """
    body = _stored()
    if body is None:
        return
    try:
        raw = json.loads(body or "{}")["collections"]
        if not isinstance(raw, list):
            raise TypeError("collections must be a list")
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, {"error": f"{DEFS_BROKEN}: {e}"}) from None
    for item in raw:
        if isinstance(item, dict) and (name := str(item.get("name") or "")):
            machine_store.put(DEFS_KIND, name, json.dumps(item, ensure_ascii=False, indent=2))
    machine_store.drop(DEFS_KIND, DEFS_KEY)
    log.info("split %d collection definitions into one record each", len(raw))


def save(items: list[Collection]) -> None:
    """一覧をそのまま書き込む。**消えたものはこの場で落とす**。

    1 件だけ直すなら `_replace_one` のほうが安い(そちらは 1 件しか書かない)。
    """
    _split_out_the_old_row()
    keep = {c.name for c in items}
    for item in items:
        _put_one(item)
    for key in machine_store.keys(DEFS_KIND):
        if key != DEFS_KEY and key not in keep:
            machine_store.drop(DEFS_KIND, key)


def _put_one(item: Collection) -> None:
    machine_store.put(
        DEFS_KIND, item.name, json.dumps(item.__dict__, ensure_ascii=False, indent=2),
    )


def get(name: str) -> Collection:
    """名前で 1 つ。**一覧を読まない** —— 台帳を抱えた他の収集まで読む理由が無い。"""
    _split_out_the_old_row()
    body = machine_store.get(DEFS_KIND, name) if name and name != DEFS_KEY else None
    if body is None:
        raise HTTPException(404, {"error": f"収集「{name}」がありません"})
    try:
        raw = json.loads(body)
        if not isinstance(raw, dict):
            raise TypeError("object を入れてください")
    except (ValueError, TypeError) as e:
        raise HTTPException(400, {"error": f"{DEFS_BROKEN}(「{name}」): {e}"}) from None
    return _from_json(raw)


def _read_one(name: str) -> tuple[Collection, str]:
    """1 件と、**そのとき置き場にあった文字列**。CAS の `expect` に要る。

    `get` と別に置いてあるのは、組み立て直した JSON では突き合わせにならないため ——
    並びも空白も同じである保証が無い(比べるのは読んだそのもの)。
    """
    _split_out_the_old_row()
    body = machine_store.get(DEFS_KIND, name) if name and name != DEFS_KEY else None
    if body is None:
        raise HTTPException(404, {"error": f"収集「{name}」がありません"})
    return get(name), body


def _replace_one_if(name: str, updated: Collection, expect: str) -> bool:
    """**読んだときのままなら**書き換える。書けたら True。

    取り合いに負けたほうを黙って通さないための口(`machine_store.put_if`)。
    """
    return machine_store.put_if(
        DEFS_KIND, name,
        json.dumps(updated.__dict__, ensure_ascii=False, indent=2), expect,
    )


def _replace_one(name: str, updated: Collection) -> None:
    """その 1 件だけを書き換える。**他の収集は読みも書きもしない**。"""
    get(name)
    if updated.name != name:
        machine_store.drop(DEFS_KIND, name)
    _put_one(updated)


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
    keep_ratio: float | None = None,
    extract_spec=None,
    kind: str = KIND_STOCK,
    keep_days=None,
    verify_tags=None,
    partition_spec=None,
    feed_spec=None,
    material_spec=None,
    sweeps=None,
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
        keep_ratio=(
            DEFAULT_KEEP_RATIO if keep_ratio is None else normalize_keep_ratio(keep_ratio)
        ),
        extract=extraction.to_json(extraction.normalize(extract_spec)),
        material=normalize_material(material_spec),
        kind=normalize_kind(kind),
        keep_days=normalize_keep_days(keep_days, normalize_kind(kind)),
        verify_tags=normalize_verify_tags(verify_tags),
        partition=partitioning.to_json(partitioning.normalize(partition_spec)),
        feed=feeds.to_json(feeds.normalize(feed_spec)),
        sweeps=normalize_sweeps(sweeps),
        requested_by=requested_by.strip()[:80],
        created_at=now,
        updated_at=now,
        # 有効にした時点で 1 回目が走るよう、予定は「いま」にしておく
        next_run_at=now,
    )
    save([*existing, item])
    return item


def update(name: str, **fields) -> Collection:
    """渡した項目だけを差し替える。間隔を変えたら次回の予定も引き直す。"""
    current = get(name)
    allowed = {
        "description", "prompt", "interval_minutes", "enabled",
        "backend", "model", "effort", "web", "cursor", "keep_ratio", "extract",
        "partition", "partitions", "sweeps", "feed", "verify_tags",
        "kind", "keep_days", "material",
    }
    patch = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "interval_minutes" in patch:
        patch["interval_minutes"] = max(int(patch["interval_minutes"]), MIN_INTERVAL_MINUTES)
    if "keep_ratio" in patch:
        patch["keep_ratio"] = normalize_keep_ratio(patch["keep_ratio"])
    if "partition" in patch:
        # 空のオブジェクトを渡したら区画を持たない収集に戻す(消す手段がここしかない)。
        # **割り方を変えたら台帳は捨てる** —— 鍵の意味が変わるので、引き継ぐと
        # 前の割り方で見た記録が新しい区画に付く。
        # **順番だけの違いでは捨てない**(`partitioning.same_cut`)—— `origin` は
        # 配る順を決めるだけで、どの文書がどの区画に入るかは動かない。捨てていた
        # 頃は、どこから広げるかを決め直すたびに一周の記録が巻き戻った
        # (本番で 10,457 区画ぶんが消えた)
        patch["partition"] = partitioning.to_json(partitioning.normalize(patch["partition"] or None))
        # **台帳を明示的に渡されていなければ捨てる。** 両方渡されたときは渡したほうが
        # 勝つ(割り出した結果を持ち込みたいのに、こちらが消してしまうため)
        if (
            not partitioning.same_cut(patch["partition"], current.partition)
            and "partitions" not in patch
        ):
            patch["partitions"] = []
    if "sweeps" in patch:
        patch["sweeps"] = _keep_schedule(normalize_sweeps(patch["sweeps"]), current.sweeps)
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
    if "feed" in patch:
        # 空のオブジェクトを渡したら道具を外す(消す手段がここしかない)
        patch["feed"] = feeds.to_json(feeds.normalize(patch["feed"] or None))
    if "extract" in patch:
        # 空のオブジェクトを渡したら「使わない」に戻す(消す手段がここしかない)
        patch["extract"] = extraction.to_json(extraction.normalize(patch["extract"] or None))
    if "material" in patch:
        # 空のオブジェクトを渡したら材料のソースを外す(消す手段がここしかない)
        patch["material"] = normalize_material(patch["material"] or None)
    if "verify_tags" in patch:
        # 空の配列を渡したら「確かめない」に戻す
        patch["verify_tags"] = normalize_verify_tags(patch["verify_tags"] or None)
    if "kind" in patch:
        patch["kind"] = normalize_kind(patch["kind"])
    # **種類を変えたら日数も引き直す**(網羅へ移したのに日数が残ると、
    # 次に流れへ戻したときに古い設定で消え始める)
    kind = patch.get("kind", current.kind)
    if "keep_days" in patch or "kind" in patch:
        # **流れへ移したときは既定を入れる。** 網羅の 0 は「使わない」の意味なので、
        # そのまま持ち越すと「期限では落とさない」になってしまう
        moved_in = kind == KIND_FLOW and current.kind != KIND_FLOW
        given = patch.get("keep_days", None if moved_in else current.keep_days)
        patch["keep_days"] = normalize_keep_days(given, kind)
    # 相手・モデル・深さは**空文字を「指定しない」に倒す**。画面のフォームは空欄を
    # 空文字で送ってくるが、持ち回るときは None でないと「未指定」の意味にならない
    # (読み直せば `_from_json` が同じことをするが、保存直後の値とずれる)
    for key in ("backend", "model", "effort"):
        if key in patch and not str(patch[key]).strip():
            patch[key] = None
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
    get(name)
    machine_store.drop(DEFS_KIND, name)
    collect_log.forget(name)
    # **待ち行列からも外す** —— 消えた収集を抱えたままだと、そのワーカーは
    # 起こそうとして 404 を踏み続ける
    workers.forget(name)


# ---- 溜め先(コアスキーマの DB。notes と同じ形)---------------------------------


SYSTEM_PROMPT = (
    "集めた情報を JSON だけで返す。前置き・説明・コードブロックの記号は付けない。"
    " 形式: {\"items\":[{\"title\":\"見出し\",\"body\":\"本文\","
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\"}],\"next_cursor\":\"次に進む印\"}"
    " title は重複の鍵になるので、同じものを指す見出しは同じ文字列にする。"
    " 出典が分かるものは url を必ず入れる。分からない項目は null。"
    " **同じ url の 1 件は既にあるものとみなし、足されない**(見出しが違っても同じ)。"
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
    "\"tags\":[\"タグ\"],\"url\":\"出典URL\",\"extra\":{\"鍵\":\"値\"}}],"
    "\"next_cursor\":\"次に進む印\"}"
    " **返すのは、直すものと新しく足すものだけでよい。**"
    " 触れなかったものはそのまま残るので、変えないものを返す必要はない。"
    " title は同一性の鍵。**同じ見出しで返すと、その 1 件が置き換わる**。"
    " **見出しの付け替えはできない** —— 既にあるものを別の見出しで返しても、"
    "同じ url の 1 件として弾かれる(元の見出しのまま残る)。"
    " **消したいものは、その見出しで tags に「" + notes.TOMBSTONE_TAG + "」を入れて返す**"
    "(墓標)。**そのとき本文に、なぜ消すのかを 1 行で書く** ——"
    "消したものは後から一覧でしか見えないので、理由が無いと消し間違いに気づけない。"
    "重複をまとめるときは、まとめた先を返し、元のものに墓標を付ける。"
    " **消したものは、tags に「" + notes.RESTORE_TAG + "」を入れて返したときだけ戻る**"
    "(消したのが間違いだと分かったとき)。入れずに返しても消えたまま変わらない。"
    " **脇書き(extra)は書いたものだけが変わる。** 触れなかった鍵はそのまま残るので、"
    "変えないものを書く必要は無い。**落としたい鍵だけ null を書く**。"
    " 分からない項目は null。"
)


def partition_counts(item: Collection, docs: list[dict]) -> dict[str, int]:
    """焼こうとしている世代を、区画ごとに数える。区画を持たない収集では空。

    **数えるのは生きているものだけ**(`living`)—— 区画の大きさは「この回に
    見てもらう量」なので、消したものを混ぜると実際より多く見える。
    """
    if not item.partition or not item.partitions:
        return {}
    return partitioning.counts_of(
        partitioning.normalize(item.partition),
        item.partitions,
        living({doc["title"]: doc for doc in docs}),
    )


def is_removed(doc: dict) -> bool:
    """消えたもの(`notes.REMOVED_TAG`)か。墓標(消してくれ、の指示)とは別物。"""
    return notes.REMOVED_TAG in (doc.get("tags") or [])


def living(docs: dict[str, dict]) -> dict[str, dict]:
    """消えたものを外した文書。**区画の母集団はこちら**。

    消したものは残り続ける(消えた印を付けて残すのがこの層の契約)ので、
    母集団に混ぜると**精査を頼むほど区画が太る** —— 実際には見るものが無い
    区画にも巡回の 1 回が割り当てられ、そのぶん AI の枠を捨てることになる。
    差し込みには別の一覧として渡すので、消えたものが見えなくなるわけではない
    (`render_material`)。
    """
    return {title: doc for title, doc in docs.items() if not is_removed(doc)}


def render_material(
    previous: dict[str, dict], scoped: bool = False, seen: set[str] | None = None,
) -> tuple[str, int]:
    """前世代を、プロンプトへ差し込める形にする。差し込んだ件数も返す。

    `seen` を渡すと、**実際に差し込んだ見出し**をそこへ書く(戻り値にできないため)。
    精査済みの印を付け替えるのに要る —— **切られたぶんまで「読んだ」ことにすると、
    AI が見ていないものが読者の画面へ出る**。

    **入り切らなければ切って、切ったことを本文に書く** —— 黙って切ると、AI は
    見えなかったぶんを「無かったもの」として落とし、歯止めが無ければそのまま消える。

    `scoped` は「今回の区画のぶんだけを渡している」の印。**区画で切ってあれば
    普通は全部入る**ので、切られたときの意味が変わる(区画が大きすぎる)。

    **消えたものは別の一覧にして後ろに置く**(`_render_removed`)。混ぜていた頃は
    同じ枠を取り合うので、**消すほど直す相手が見えなくなった** —— 精査を頼む収集
    ほど墓標が増えるため。返す件数は生きているものだけを数える。
    """
    if not previous:
        return (
            "(今回の対象には、まだ何も入っていません)" if scoped
            else "(まだ何も入っていません。最初の内容を作ってください)"
        ), 0
    alive = [doc for doc in previous.values() if not is_removed(doc)]
    gone = [doc for doc in previous.values() if is_removed(doc)]
    lines: list[str] = []
    used = 0
    for doc in alive[:MAX_MATERIAL_DOCS]:
        tags = doc.get("tags") or []
        body = (doc.get("body") or "")[:MATERIAL_BODY_CHARS].replace("\n", " ")
        line = (
            f"- {doc['title']}"
            + (f" 【{'/'.join(str(t) for t in tags)}】" if tags else "")
            + (f" — {body}" if body else "")
        )
        if used + len(line) > MAX_MATERIAL_CHARS:
            break
        lines.append(line)
        used += len(line)
        # **入ったぶんだけ控える**(天井で切れた行は「見せていない」)
        if seen is not None:
            seen.add(doc["title"])
    shown = len(lines)
    if alive:
        head = ("今回の対象にいま入っているもの(全 " if scoped else "いまの内容(全 ")
        head += f"{len(alive)} 件"
        head += f"。うち {shown} 件だけ載せています)" if shown < len(alive) else ")"
        if shown < len(alive):
            head += "\n※ 載っていないものは今回の対象外です。載っているぶんだけを整理してください。"
        text = head + ":\n" + "\n".join(lines)
    else:
        # 消えたものしか無い区画。**「何も入っていません」で終わらせない** ——
        # 下に消した一覧が続くので、空だと言い切ると食い違う
        text = (
            "今回の対象に、いま生きているものはありません" if scoped
            else "いま生きているものはありません"
        ) + "(下は消したものです)。"
    if gone:
        text += "\n\n" + _render_removed(gone)
    return text, shown


def _render_removed(docs: list[dict]) -> str:
    """**この回の対象で消したもの**の一覧。生きているものとは別に渡す。

    渡すのは「もう一度足させない」ため —— 黙って外すと、消したものを「抜けている」と
    読んで足し直され、次の回にまた消すことになる。**本文ではなく消した理由を載せる**
    (本文は戻すときのために残してあるが、ここで読ませたいのは、なぜもう一度
    足してはいけないか)。

    **印(`notes.REMOVED_TAG`)は 1 件ずつにも残す。** どれが消えたものかを
    行ごとに読めるようにするため。**戻すのは戻す印(`notes.RESTORE_TAG`)を
    付けたときだけ**(`material`)—— 消えた印を外して返すのを戻す操作にしていた頃は、
    タグを書き直した返りが印を写し忘れただけで戻っていた。
    """
    lines: list[str] = []
    used = 0
    for doc in docs[:MAX_REMOVED_DOCS]:
        why = removed_reason(doc)[:MATERIAL_BODY_CHARS].replace("\n", " ")
        line = f"- {doc['title']} 【{notes.REMOVED_TAG}】" + (f" — {why}" if why else "")
        if used + len(line) > MAX_REMOVED_CHARS:
            break
        lines.append(line)
        used += len(line)
    head = f"※ この対象で消したもの(全 {len(docs)} 件"
    head += f"。うち {len(lines)} 件だけ載せています)" if len(lines) < len(docs) else ")"
    head += (
        "。**もう一度足さないでください。**"
        f"消したのが間違いだと分かったときだけ、tags に「{notes.RESTORE_TAG}」を"
        "入れて同じ見出しで返せば戻ります(入れずに返しても消えたままです)"
    )
    return head + ":\n" + "\n".join(lines)


def render_names(previous: dict[str, dict], scoped: bool = False) -> str:
    """いま持っているものを、**見出しとタグだけ**の一覧にする。

    **本文を見せない代わりに、全部が入る。** `render_material` は 1 件に本文を
    200 字まで載せるので、育った収集では数百件で天井に当たり、しかも**切れるのは
    新しいほう**(並びは古い順)—— 重なりを畳む・親子を決めるといった仕事では、
    いちばん見てほしい入ったばかりのものが落ちることになる。

    **消えたものは出さない。** ここは「いま何を持っているか」を見せる場所で、
    何を外したかは別の話(`_render_removed` は `{current}` の側が出す)。
    """
    alive = [doc for doc in previous.values() if not is_removed(doc)]
    if not alive:
        return "(まだ何も入っていません)"
    lines = []
    used = 0
    for doc in alive[:MAX_NAME_DOCS]:
        tags = doc.get("tags") or []
        line = f"- {doc['title']}" + (f" 【{'/'.join(str(t) for t in tags)}】" if tags else "")
        if used + len(line) > MAX_MATERIAL_CHARS:
            break
        lines.append(line)
        used += len(line)
    head = ("今回の対象にいま入っているものの見出し(全 " if scoped else "いま持っているものの見出し(全 ")
    head += f"{len(alive)} 件"
    head += f"。うち {len(lines)} 件だけ載せています)" if len(lines) < len(alive) else ")"
    return head + ":\n" + "\n".join(lines)


def render_recent(
    previous: dict[str, dict], since: str | None, seen: set[str] | None = None,
) -> str:
    """前回から後に入ったものを、プロンプトへ差し込める形にする。

    **溜まっていく一方の収集のための差し込み**。全部を渡すと入り切らないうえ、
    毎回同じものを読み直すことになる —— 要約も重要度付けも、新しく入ったぶんにしか
    意味が無い。

    **時刻が読めないものは入れる。** 落とすと静かに 0 件になり、
    「何も入らなかった」のか「何も無かった」のかが読めなくなる。

    **基準が無ければ区切らない**(その巡回の 1 回目)。いま入っているものが
    まるごと差分になる —— 初めての要約が空で終わるのはおかしい。
    """
    cutoff = _at(since)
    fresh = [d for d in previous.values() if cutoff is None or _at(d.get("updated_at")) is None
             or _at(d.get("updated_at")) > cutoff]
    if not fresh:
        return "(前回から新しく入ったものはありません)"
    # **新しい順**。入り切らずに切れるときに、残るのが古いほうでは意味が無い
    fresh.sort(key=lambda d: d.get("updated_at") or "", reverse=True)
    lines: list[str] = []
    used = 0
    for doc in fresh[:MAX_MATERIAL_DOCS]:
        tags = "/".join(doc.get("tags") or [])
        body = (doc.get("body") or "")[:MATERIAL_BODY_CHARS].replace("\n", " ")
        extra = doc.get("extra") or {}
        when = (extra.get("published_at") or "")[:10]
        line = (
            f"- {doc['title']}"
            + (f" ({when})" if when else "")
            + (f" 【{tags}】" if tags else "")
            + (f" — {body}" if body else "")
            # **出典も見せる。** 見せずに「上に並んでいるものの URL を写して」と
            # 頼んでいた頃は、AI に写す先が無く「記事URLの記載なし」と書かれた
            # (実測。まとめの節が全部そうなった)
            + (f" 〈{url}〉" if (url := _source_url(extra)) else "")
        )
        if used + len(line) > MAX_MATERIAL_CHARS:
            break
        lines.append(line)
        used += len(line)
        # **入ったぶんだけ控える**(天井で切れた行は「見せていない」)
        if seen is not None:
            seen.add(doc["title"])
    head = f"前回から新しく入ったもの(全 {len(fresh)} 件"
    head += f"。うち {len(lines)} 件だけ載せています)" if len(lines) < len(fresh) else ")"
    return head + ":\n" + "\n".join(lines)


# 追跡用の飾り。**同じ記事でも配信元ごとに違うものが付く**ので、鍵にすると
# 同じ URL が別物に見える(実測で、同じ Qiita の記事が人気フィード経由だけ
# `utm_campaign=popular_items` を連れてきていた)。
#
# **落とすのはここに挙げたものと `utm_` で始まるものだけ。** 「? の後ろを丸ごと捨てる」
# にすると、`?id=123` のように**クエリが記事を指している**サイトで別々の記事が
# 1 つに潰れる —— 潰れたほうは足されないので、黙って入らなくなる。
_TRACKING_PARAMS = frozenset({
    "fbclid", "gclid", "dclid", "msclkid", "yclid", "ttclid", "twclid",
    "igshid", "mc_cid", "mc_eid",
})


def url_key(url) -> str:
    """同じ記事を指す URL を、同じ鍵にする。URL でなければ空。

    **`https` と `http`・`www.` の有無・末尾の `/`・並び順の違うクエリ**は同じ記事。
    追跡用の飾り(`utm_*` など)も落とす —— 配信元ごとに違うものが付くので、
    残すと同じ記事が別物に見える。

    **人に見せる URL は元のまま**(ここで作るのは突き合わせ専用の鍵)。
    """
    raw = str(url or "").strip()
    if not raw.lower().startswith(("http://", "https://")):
        return ""
    parts = urlsplit(raw)
    host = parts.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    path = parts.path.rstrip("/") or "/"
    if not parts.query:
        return host + path
    kept = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if not k.lower().startswith("utm_") and k.lower() not in _TRACKING_PARAMS
    )
    return host + path + ("?" + urlencode(kept) if kept else "")


def _raw_url(raw: dict) -> str:
    """集めた 1 件が名乗っている出典(`_to_doc` と同じ読み方)。"""
    if isinstance(raw, dict):
        url = (raw.get("url") or "").strip()
        if url:
            return url
        extra = raw.get("extra")
        if isinstance(extra, dict):
            return str(extra.get("url") or "").strip()
    return ""


def _said(text: str) -> str:
    """相手が言ったことの先頭。**空なら「何も返さなかった」と書く**(空文字は読めない)。"""
    head = " ".join((text or "").split())[:MAX_SAID_CHARS]
    return head or "(何も返ってきませんでした)"


def _incoming_urls(collected) -> set[str]:
    """その回に入ってくるぶんの URL の鍵。

    **持つのは入ってくるぶんだけ**。前世代ぜんたいの URL を持つと、数十万件の
    収集で名簿を丸ごとメモリに載せることになり、1 行ずつ流すようにした意味が消える
    (`stream_docs`)。突き合わせに要るのは「今から入るものと同じ URL」だけなので、
    前世代を流しながら、ここに載っている鍵だけを控える。

    **見出しで引ける形(抽出の名簿)では何もしない** —— あちらは数十万件が一時の
    SQLite に載っていて、全件を読み直すのは流している意味を打ち消す。
    """
    if hasattr(collected, "take"):
        return set()
    keys = {url_key(_raw_url(raw)) for raw in collected}
    keys.discard("")
    return keys


def _source_url(extra: dict) -> str:
    """その 1 件の出典。**差し込みにも見せる** —— 見せなければ写しようがない。"""
    url = str((extra or {}).get("url") or "").strip()
    return url if url.startswith(("http://", "https://")) else ""


def _at(raw) -> datetime | None:
    """控えの時刻を読む。**読めなければ None**(比べる相手にしない)。"""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def describe_partition(item: Collection, key: str, sources: dict) -> str:
    """区画の鍵を、人にも AI にも読める範囲の言い方にする。持たない収集では空。"""
    if not item.partition:
        return ""
    return partitioning.describe(
        partitioning.normalize(item.partition), key, sources, item.partitions
    )


def scoped_docs(
    item: Collection,
    previous: dict[str, dict],
    partition_key: str | None,
    focus: Focus | None = None,
) -> tuple[dict[str, dict], bool]:
    """今回の区画に入る文書だけに絞る。絞ったかどうかも返す。

    **これが区画のいちばんの利得。** 全体を差し込もうとすると入り切らず、切ったぶんは
    「今回の対象外」になる(`MAX_MATERIAL_CHARS`)。区画で切れば全部見せられるので、
    漏れているものを足させることも、重複をまとめることも初めて成り立つ。
    """
    if focus is not None and not partition_key:
        # **名指しだけの割り込みでは、名指しされたものが全体。** 区画を渡されていない
        # のに全件を差し込むと入り切らず、直す相手が切り落とされる
        return {t: previous[t] for t in focus.titles if t in previous}, True
    if not (item.partition and partition_key):
        return previous, False
    spec = partitioning.normalize(item.partition)
    # **鍵の範囲だけで判じない**(`partition_of`)。見出しで割った区画は、鍵が
    # 「最初の見出し〜最後の見出し」なので**区画と区画のあいだが誰のものでもなく**、
    # あとから足した見出しがそこへ落ちると、以後どの回にも出てこなくなる。
    # **索引は 1 回だけ組む**(`locator`)—— ここは全件を舐めるうえ、1 回の巡回で
    # 区画の数だけ呼ばれる
    find = partitioning.locator(spec, item.partitions)
    return {
        title: doc
        for title, doc in previous.items()
        if find(doc) == partition_key
    }, True


def render_focus(
    focus: Focus, item: Collection, previous: dict[str, dict], partition_key: str | None
) -> str:
    """割り込みの指示を、プロンプトの後ろに足す文にする。

    **いつもの回ではないと最初に言う。** 巡回のプロンプトをそのまま使うので、
    断らないと AI は「この範囲を一通り調べる」ほうへ引っ張られる。

    **名指しされたものは必ず載せる。** 区画に入っていなくても、`{current}` に
    載っていなくても載せる —— 直す相手が見えていない訂正は頼めない。
    """
    parts = [
        "【この回はいつもの巡回ではありません】",
        "次の指示にだけ従ってください。**範囲を広げず、ここに書かれていないものは"
        "触らないこと**(触れなかったものはそのまま残ります)。",
        f"\n指示: {focus.note}",
    ]
    if named := [previous[t] for t in focus.titles if t in previous]:
        lines = "\n".join(
            f"- {doc['title']}"
            + (f" 【{'/'.join(doc.get('tags') or [])}】" if doc.get("tags") else "")
            + (f" — {(doc.get('body') or '')[:MATERIAL_BODY_CHARS]}" if doc.get("body") else "")
            for doc in named
        )
        parts.append(f"\n直す対象:\n{lines}")
    # **手元に無い見出しも隠さない。** 名指しされたのに無いのは、見出しの書き方が
    # 違うか、そもそも入っていないか —— どちらも AI に伝わっていたほうが答えが良くなる
    if missing := [t for t in focus.titles if t not in previous]:
        parts.append(
            "\nまだ入っていない見出し(名指しされたが手元に無い): " + "、".join(missing)
        )
    return "\n".join(parts)


def build_messages(
    item: Collection,
    previous: dict[str, dict] | None = None,
    partition_key: str | None = None,
    sources: dict | None = None,
    sweep: Sweep | None = None,
    focus: Focus | None = None,
    feed: dict | None = None,
    seen: set[str] | None = None,
) -> list[dict]:
    """AI へ渡す本文。`{cursor}` を今のカーソルで、`{current}` を今ある内容で置き換える。

    `seen` を渡すと、**差し込んだ既存の見出し**をそこへ書く。**AI が目を通したのは
    ここに入ったものだけ** —— 精査済みの印を付け替えるのに要る(`notes.UNREVIEWED_TAG`)。

    **カーソルが空でも壊さない**(初回は空文字が入るだけ)。テンプレートに `{cursor}` が
    無い収集は、毎回同じことを聞く形になる。

    `{current}` は作り直し(整理)のためのもの。今ある内容を読ませて、分類をやり直す・
    重複をまとめる・言い回しを揃える、といった育て方をするときに使う。
    **区画を持つ収集では、その区画のぶんだけが入る**。

    `{names}` は**見出しとタグだけ**。本文を見せないぶん、育った収集でも全部が入る ——
    重なりを畳む・親子を決めるような、「何を持っているか」だけが要る回のための口。

    `{partition}` は**今回見る範囲**。Chiezo が台帳から選んで渡す(`app/partition.py`)。
    矩形だけでは AI にどこか分からないので、近くのものを数件添えた文になる。
    """
    # **その回だけの依頼文を差し込める**(`focus.prompt`)。保存はしないので、
    # 定義のプロンプトは育てたまま、1 回きりの頼みごとを別の文で走らせられる
    prompt = (focus.prompt if focus else None) or (sweep.prompt if sweep else "") or item.prompt
    user = prompt.replace("{cursor}", item.cursor or "(まだ無し。最初から)")
    spec = partitioning.normalize(item.partition) if item.partition else None
    if focus is not None:
        partition_key = focus.partition or partition_key
    if PARTITION_PLACEHOLDER in user:
        if spec and partition_key:
            where = partitioning.describe(spec, partition_key, sources or {}, item.partitions)
        elif focus is not None:
            # **「全体」と言わない。** 区画を渡されていない割り込みの対象は
            # 名指しされたものだけで、範囲ではない
            where = "(この回は範囲ではなく、下に名指しされたものが対象です)"
        else:
            where = "(全体)"
        user = user.replace(PARTITION_PLACEHOLDER, where)
    if FEED_PLACEHOLDER in user:
        # **道具を付けていなければ、その旨を入れて消す。** 差し込み口だけ残ると、
        # AI は「渡されるはずのものが空だった」と読んで待つ
        user = user.replace(
            FEED_PLACEHOLDER,
            feeds.render(feed) if feed else "(この収集に外向きの道具は付いていません)",
        )
    if SOURCE_PLACEHOLDER in user:
        # **基準はその巡回の前回**(`{recent}` と同じ読み方)。収集の前回に倒すと、
        # 別の巡回が走った時刻でこの窓が食われる
        since = sweep.last_run_at if sweep else None
        user = user.replace(
            SOURCE_PLACEHOLDER,
            render_source_material(item.material, material_docs(item.material, sources or {}, since)),
        )
    if MATERIAL_PLACEHOLDER in user:
        docs, scoped = scoped_docs(item, previous or {}, partition_key, focus)
        material_text, _shown = render_material(docs, scoped, seen)
        user = user.replace(MATERIAL_PLACEHOLDER, material_text)
    if NAMES_PLACEHOLDER in user:
        # **見出しとタグだけ。** 本文を見せないぶん全部が入る ——
        # 何を持っているかだけが要る回(畳む・親子を決める)のための差し込み口。
        # **目を通した印は付けない** —— 見出しだけでは読んだことにならない
        docs, scoped = scoped_docs(item, previous or {}, partition_key, focus)
        user = user.replace(NAMES_PLACEHOLDER, render_names(docs, scoped))
    if NOW_PLACEHOLDER in user:
        # **人が読むものは日本時間**(この文はそのまま見出しや本文へ写される)
        user = user.replace(NOW_PLACEHOLDER, jst.format(_now()))
    if RECENT_PLACEHOLDER in user:
        # **基準はその巡回の前回だけ**(収集ぜんたいの前回へは倒さない)。
        # 倒していたせいで、**その巡回の 1 回目が必ず空になった** —— 直前に別の巡回が
        # 走っていれば、基準はその数分前になる。実際、初めての要約と情報更新が
        # そろって 0 件で終わった(本番の履歴。見出しが 60 件足した 3 分後だった)。
        # **走ったことが無ければ区切らない** —— 1 回目は「いまあるもの全部」が差分
        since = sweep.last_run_at if sweep else None
        user = user.replace(RECENT_PLACEHOLDER, render_recent(previous or {}, since, seen))
    if focus is not None:
        user += "\n\n" + render_focus(focus, item, previous or {}, partition_key)
    # **割り込みは必ず「直す」側で頼む。** 足すだけの収集でも、名指しで渡された 1 件を
    # 直せなければ割り込みの意味が無い
    # **直す回かどうかは、その回の依頼文が語っている**(`edits_what_is_there`)。
    # 割り込みは必ず直す側 —— 名指しで渡された 1 件を直せなければ意味が無い
    edits = focus is not None or edits_what_is_there(prompt, sweep.only_new if sweep else False)
    system = REFINE_SYSTEM_PROMPT if edits else SYSTEM_PROMPT
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


def parse_response(content: str) -> tuple[list[dict], str | None, str]:
    """AI の答えから items と next_cursor を取り出す。3 つめは断り書き(無ければ空)。

    前置きやコードブロックが混ざっても拾えるように、`{` 〜 `}` を切り出してから読む
    (小型モデルでなくても、この手の付け足しは普通に起きる)。

    **途中で切れていたら、読めたところまでを拾う**(`_salvage`)。答えが長くなると
    相手の上限に当たって末尾が欠けることがあり、実際に本番で起きた
    (`JSONDecodeError: Expecting ',' delimiter: line 1 column 7949`)。
    そこで丸ごと捨てると、**その回に払った AI の呼び出しが全部無駄になる**うえ、
    同じ区画を次も同じ長さで聞くので繰り返し落ちる。

    **拾ったことは黙っていない。** 断り書きを返して、控えと画面に出す ——
    「集まりが少ない」のが世の中の都合なのか答えが切れたせいなのかで、次にすることが違う。
    """
    stripped = re.sub(r"```(?:json)?", "", content).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0:
        # **何が返ってきたかを添える。** 「見つかりません」だけだと、控えを見た人は
        # AI の履歴まで掘らないと理由が分からない —— 実際にそうなった(相手の agent が
        # 背景タスクを抱えたまま「待っています」と答えて終わり、JSON を返さなかった)。
        # **先頭だけ**にするのは、控えに答えを丸ごと写す場所ではないから
        raise ValueError(f"JSON オブジェクトが見つかりません: {_said(stripped)}")
    try:
        payload = json.loads(stripped[start : end + 1]) if end > start else None
    except ValueError:
        payload = None
    if payload is None:
        items = _salvage(stripped[start:])
        if not items:
            raise ValueError("JSON として読めず、拾えるものもありませんでした")
        # 切れているので次の印は読めない(半端な値を進めると、そこから先が飛ぶ)
        log.warning("collect response was cut off; salvaged %d items", len(items))
        return items, None, f"答えが途中で切れていたので、読めた {len(items)} 件だけ拾いました"
    if not isinstance(payload, dict):
        raise ValueError("トップレベルがオブジェクトではありません")
    items = payload.get("items")
    if not isinstance(items, list):
        raise ValueError("items が配列ではありません")
    next_cursor = payload.get("next_cursor")
    return (
        [i for i in items if isinstance(i, dict)],
        str(next_cursor) if isinstance(next_cursor, (str, int, float)) and next_cursor else None,
        "",
    )


def _salvage(text: str) -> list[dict]:
    """途中で切れた答えから、**そこまでに閉じている 1 件ずつ**を拾う。

    **`items` の中だけを見る。** いちばん外側の `{` から数えると、中の 1 件ずつを
    切り出せない(外側が閉じていないので、深さが 0 に戻らない)。

    文字列の中の `{` `}` を数えないよう、引用符と逃がし記号を見ながら進む
    (本文に「{」が入っていることは普通にある)。
    """
    head = re.search(r'"items"\s*:\s*\[', text)
    if head is None:
        return []
    found: list[dict] = []
    depth = 0
    begin = -1
    in_string = False
    escaped = False
    for index, ch in enumerate(text[head.end():], start=head.end()):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                begin = index
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and begin >= 0:
                with suppress(ValueError):
                    value = json.loads(text[begin : index + 1])
                    if isinstance(value, dict):
                        found.append(value)
            elif depth < 0:
                depth = 0
    return found


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
    # 答えが途中で切れた区画。**印は付けない**(その区画の残りは誰も見ていない)
    cut: list[str] | None = None,
    partitions: list[dict] | None = None,
    focus: bool = False,
) -> Collection:
    """1 回ぶんの結果を定義側へ書き戻し、次回の予定を入れる。

    **失敗しても次回の予定は入れる** —— 入れないと、一度こけた収集が二度と走らなくなる。

    **見た区画に印を付けるのは成功したときだけ。** 失敗した回に印を付けると、
    一度も見られていない区画が「回り終えた」に混ざり、一周が嘘になる。

    **消したものは消えた印を付けて残す**(`notes.REMOVED_TAG`)。消してしまうと、
    消す回と足す回が別々に走るせいで次の足す回が連れ戻す —— 足すほうは
    「いま名簿にいるか」しか見られず、なぜ居ないのか(一度も入っていないのか、
    調べたうえで外したのか)までは分からない。残せば見出しが既にいるので素通りする。

    **割り込みの回は、時計にも進み具合にも触らない**(`focus`)。進み具合と次の予定を
    動かすと、割り込むたびに一周が伸びる。**成否にかかわらず依頼は片付ける** ——
    残すと、次に走る定時の回が割り込みとして走ってしまう。

    **区画を名指しされた割り込みだけは、その区画に印を付ける。** 先に見てほしい
    ところを頼んだのだから、巡回が同じところをもう一度見る必要は無い。
    画家だけを名指しした割り込みには付けない(渡す `visited` が空になる)。
    """
    current = get(name)
    now = _now()
    this = sweep_named(current, sweep)
    ledger = partitions if partitions is not None else current.partitions
    # **区画を名指しされた割り込みは、その区画に印を付ける。** 先に見てほしいところを
    # 頼んだのだから、巡回が同じところをもう一度見る必要は無い。
    # **名指しが画家だけの割り込みは付けない** —— 1 人見ただけで区画を見終えたことに
    # すると、その区画の残りが誰にも見られなくなる(渡す `visited` が空になる)
    if visited and status == "ok":
        ledger = partitioning.clear_cuts(ledger, visited, this.name)
        ledger = partitioning.mark_visited(ledger, visited, this.name, _iso(now))
    # **切れた区画には印を付けない。** 返ってきたのは途中までで、その先は誰も
    # 見ていない —— 付けると一周が嘘になる。ただし**続けて切れたら諦める**
    # (`MAX_CUTS`)。粘り続けると、その 1 区画が一周を永久に止める
    if cut and status == "ok":
        ledger, give_up = partitioning.note_cut(ledger, cut, this.name)
        if give_up:
            ledger = partitioning.mark_visited(ledger, give_up, this.name, _iso(now))
    # **巻き戻せるだけの控えを残す。** 設定を直してからやり直したい、が普通に起きる。
    # 割り込みは何も動かさないので控えない(戻すものが無い)
    undo = current.last_undo if focus else {
        "sweep": this.name,
        "cursor": current.cursor,
        "visited": list(visited or []),
        "at": _iso(now),
    }
    updated = replace(
        current,
        last_undo=undo,
        cursor=(
            current.cursor if focus or next_cursor is None else next_cursor
        ),
        partitions=ledger,
        # **走り終えたので、どの巡回を起こしてあるかは忘れる**
        pending_sweep="",
        pending_focus=None if focus else current.pending_focus,
        # **1 回だけの上書きも忘れる**(次の定時の回まで残ると、頼んだ覚えのない
        # 相手で、頼んだ覚えのない区画だけを見る回が走る)
        pending_run=current.pending_run if focus else None,
        last_run_at=_iso(now),
        last_status=status,
        last_error=(error or "")[:500] or None,
        last_added=added,
        last_updated=updated,
        last_skipped=skipped,
        last_removed=removed,
        last_removed_titles=list(removed_titles or []),
        updated_at=_iso(now),
        **({} if focus else _advance(current, this, now, status=status, error=error)),
    )
    _replace_one(name, updated)
    return updated


def _advance(
    current: Collection,
    sweep: Sweep,
    now: datetime,
    *,
    status: str | None = None,
    error: str | None = None,
) -> dict:
    """走った巡回の次回の予定を進める。結果も渡されていれば一緒に控える。

    **起こしただけのときは結果を渡さない**(`status=None`)。渡すと、起こした時点で
    「走った」ことになる —— 収集ぜんたいの前回の状態を渡していたせいで、
    **ざっとが落ちた直後にじっくりを起こすと、じっくりにもその失敗が写っていた**
    (走っている最中なのに失敗と出る)。

    **巡回を書いていない収集では、定義そのものの欄が進む** —— 場合分けが要るのは
    ここだけで、呼ぶ側はどちらかを気にしなくてよい。
    """
    nxt = _iso(now + timedelta(minutes=sweep.interval_minutes))
    if not current.sweeps:
        return {"next_run_at": nxt}
    result = (
        {}
        if status is None
        else {
            "last_run_at": _iso(now),
            "last_status": status,
            "last_error": (error or "")[:500] or None,
        }
    )
    return {"sweeps": [
        {**raw, "next_run_at": nxt, **result} if raw.get("name") == sweep.name else raw
        for raw in current.sweeps
    ]}


def mark_pending(
    name: str, sweep: str | None = None, run_once: dict | None = None,
) -> Collection:
    """どの巡回のぶんを起こすかだけを控える。**予定は進めない**。

    **起こす前に書く。** 取り込みは収集の名前しか運べない
    (`GET /v1/collect/fetch?source=…`)ので、素材を作る側はここを読む ——
    起こしてから書くと、取り込みのほうが先に素材を取りに来たときに控えがまだ空で、
    「次に走るはずの巡回」へ倒れる(押した巡回ではないものが走る)。
    """
    current = get(name)
    this = sweep_named(current, sweep)
    # **1 回だけの上書きもここで書く。** `mark_started` は起こした後なので間に合わない
    # —— 取り込みのほうが先に素材を取りに来ると、上書きがまだ無いまま
    # ふつうの回として走る(名指しした区画ではないところを、別の相手が見る)
    updated = replace(
        current,
        pending_sweep=this.name,
        pending_run=normalize_run_once(run_once),
        updated_at=_iso(_now()),
    )
    _replace_one(name, updated)
    return updated


def restore_pending(name: str, sweep: str, run_once: dict | None = None) -> None:
    """控えた「次に起こす巡回」を元へ戻す(起こせなかったとき)。

    **書いてから起こす**作りなので、起こすのに失敗したぶんが残る ——
    残ると、**いま走っている取り込みがその巡回のつもりで素材を取りに来る**
    (押した覚えのない回が、押した覚えのない設定で走る)。

    **1 回だけの上書きも一緒に戻す** —— 片方だけ戻すと、次に走る回が
    名指しされた 1 区画だけを見て終わる(押した覚えのない回が、押した覚えの
    ない狭さで走る)。
    """
    with suppress(HTTPException):
        _replace_one(name, replace(get(name), pending_sweep=sweep, pending_run=run_once))


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
        # **結果は渡さない。** 起こしただけで「走った」ことにすると、直前に別の巡回が
        # 落ちていたときに、その失敗がこちらへ写る
        **_advance(current, this, now),
    )
    _replace_one(name, updated)
    return updated


def rewind(name: str) -> Sweep:
    """最後の 1 回を**やり直せる状態まで巻き戻す**。戻した先の巡回を返す。

    設定を直してからやり直したい、が普通に起きる。**戻せるのは定義の側だけ** ——
    焼いた世代は 1 つ前までしか残らないので、対象の巡回が最後でなければ中身は
    戻せない。だからここでは**進み具合と区画の印**だけを戻し、そのうえでもう一度
    走らせる(集め直した結果で上書きする)。

    **墓場は戻さない。** 消したのは意図してのことなので、やり直しで連れ戻さない。
    """
    current = get(name)
    undo = current.last_undo or {}
    sweep_name = str(undo.get("sweep") or "")
    if not sweep_name:
        raise HTTPException(409, {
            "error": f"収集「{name}」には、やり直せる回がありません",
            "hint": "1 回走ってからでないと、戻す先がない",
        })
    this = require_runnable(current, sweep_name)
    ledger = partitioning.forget_visits(
        current.partitions, list(undo.get("visited") or []), sweep_name
    )
    _replace_one(name, replace(
        current,
        cursor=str(undo.get("cursor") or ""),
        partitions=ledger,
        # **1 回ぶんしか持たない。** 戻したらもう使えない(同じ回を二度は戻せない)
        last_undo=None,
        updated_at=_iso(_now()),
    ))
    return this


def rewind_failed_bake(name: str, error: str, since: str, until: str) -> dict | None:
    """**焼くところで落ちた回**を、走る前まで戻す。戻したら控えを返す(でなければ None)。

    集める層は「素材を組んだ」までしか知らない。**控えを書いてから流し始める**
    作りなので(流し始めたらステータスは変えられない)、焼くところで落ちても
    定義の側には成功しか残らない —— 区画の印もカーソルも進んだままで、その区画は
    一周するまで誰も見に来ない。**枠を 1 回ぶん使って、成果だけが無い。**

    **戻してよいのは、その取り込みが運んだ回だけ。** 控えの時刻が取り込みの
    始まりと終わりのあいだに無ければ何もしない —— 別の回のものを戻すと、
    ちゃんと焼けている回の印まで落ちる。

    **時計は戻さない**(`next_run_at` / `last_run_at`)。すぐ焼き直させると、
    同じ理由で落ち続ける回が枠を食い続ける —— 次の予定で普通に走ればよい。

    **二度は戻らない。** 控えは 1 回ぶん(`last_undo`)で、戻したら消える ——
    後から来たほうは読む値が無い。**同時に来ても 1 回**(`_replace_one_if`)——
    読んだときのままなら書く形にしてあるので、取り合いに負けたほうは何もせず
    None を返す。
    """
    # **読んだそのものを控えておく**(`expect`)。時計はプロセスごとに立っている
    # (`--workers 2`)ので、同じ失敗を見て同じ判断をする道が 2 本ある ——
    # 読みと書きが離れていると**両方が「まだ誰も戻していない」と読んで二度戻す**
    # (本番で、履歴に同じ行が 2 つ並んだ)
    current, expect = _read_one(name)
    # **集める側が既に落ちていれば、戻すものは無い。** 素材を流す前に落ちた回
    # (AI が断った・相手が 502 を返した)は `record_result` が失敗として控えて
    # あり、印もカーソルも動いていない —— ここで重ねて触ると、**本当の理由が
    # 「焼くところで落ちました」に上書きされる**(本番で、相手がモデルを断った回が
    # 焼きの失敗として並んだ)。
    if current.last_status != "ok":
        return None
    undo = current.last_undo or {}
    sweep_name = str(undo.get("sweep") or "")
    at, start, end = _parse(str(undo.get("at") or "")), _parse(since), _parse(until)
    if not sweep_name or at is None or start is None or end is None:
        return None
    if not start <= at <= end:
        return None
    visited = [str(key) for key in (undo.get("visited") or [])]
    reason = f"焼くところで落ちました: {error}".strip()[:500]
    wrote = _replace_one_if(name, replace(
        current,
        cursor=str(undo.get("cursor") or ""),
        partitions=partitioning.forget_visits(current.partitions, visited, sweep_name),
        last_undo=None,
        last_status="error",
        last_error=reason,
        # **巡回の側にも書く。** 画面の「前回」は巡回ごとに出るので、
        # 収集ぜんたいの欄だけ直しても成功したように見えたままになる
        sweeps=[
            {**raw, "last_status": "error", "last_error": reason}
            if raw.get("name") == sweep_name else raw
            for raw in current.sweeps
        ],
        updated_at=_iso(_now()),
    ), expect)
    if not wrote:
        # 取り合いに負けた。**もう片方が同じことを済ませている**ので、
        # ここで控えを残すと履歴に同じ行が 2 つ並ぶ
        return None
    return {"sweep": sweep_name, "visited": visited, "at": str(undo.get("at") or "")}


def restart_cycle(name: str, sweep_name: str) -> Sweep:
    """その巡回の**一周をやり直す**(区画の印を台帳ぜんたいから外す)。戻した巡回を返す。

    **最後の 1 回を戻す口(`rewind`)とは別に要る。** あちらは直前の回のぶんだけで、
    母集団が入れ替わったあとには追いつかない —— 名簿を作り直すと区画は割り直され、
    割られた子は親の「見た」を写す(`partition._inherited`)ので、
    **中身が何倍になっても一周は終わったまま**になる。

    **中身も進み具合も動かさない。** 動かすのは「どこまで見たか」だけ ——
    やり直したいのは見る仕事であって、集めたものではない。
    """
    current = get(name)
    this = require_runnable(current, sweep_name)
    _replace_one(name, replace(
        current,
        partitions=partitioning.forget_all_visits(current.partitions, this.name),
        updated_at=_iso(_now()),
    ))
    return this


def require_runnable(item: Collection, name: str | None) -> Sweep:
    """名指しされた巡回を、**単独で走らせてよいか確かめてから**返す。

    **時計を持たない巡回は断る。** あれは割り込みで頼まれたときだけ動く 1 本で、
    自前の依頼文を持たない —— そのまま走らせると収集のプロンプトで普通の回が
    1 本増えるだけになり、「割り込み用に相手を分けておく」という置いた意味が消える。

    **画面でボタンを出さないだけにしない。** 口が受け付けるなら、いつか誰かが叩く。

    **知らない名前も断る。** `sweep_named` は次に走るはずの巡回へ倒すが、あれは
    取り込みが素材を取りに来たときの逃げ道 —— 名指しで押した側に別の巡回を
    走らせて返すのは、押した人の意図とは違うものが動いたことになる。
    """
    if name and not any(s.name == name for s in sweeps_of(item)):
        raise HTTPException(404, {"error": f"巡回「{name}」はありません"})
    sweep = sweep_named(item, name)
    if sweep.on_demand:
        raise HTTPException(400, {
            "error": f"巡回「{sweep.name}」は時計を持たないので、単独では走らせられません",
            "hint": "割り込み(POST /v1/collect/{name}/focus)で頼んでください"
                    " —— この巡回は、そのときの相手と考える量を決めておくためのものです",
        })
    return sweep


def require_focus(raw: dict) -> Focus:
    """割り込みの依頼を読む。**読めなければ断る**。

    **取り込みを起こす前に呼ぶ。** 起こしてから断ると、指示文の無い依頼のために
    1 本ぶんの取り込みが走る(そして何も直らない)。
    """
    focus = normalize_focus(raw)
    if focus is None:
        raise HTTPException(400, {
            "error": "note(どう直してほしいか)を書いてください",
            "reason": "何をどう直すかが書かれていない割り込みは、1 回ぶんの AI の"
                      "呼び出しにしかならない(巡回でやれば済む)",
        })
    return focus


def request_focus(name: str, focus: Focus) -> Focus:
    """割り込みの依頼を控える。**起こすのは呼んだ側**(`main.start_focus_bake`)。

    取り込みは収集の名前しか運べないので、素材を作る側が読めるところへ置いておく。

    **控えるのが先、起こすのが後。** 逆にすると、起こされた取り込みが素材を取りに来た
    ときにまだ依頼が書かれておらず、その回はふつうの巡回として走る —— 依頼は残るので、
    **次の定時の回を乗っ取る**。押した人からは「押した瞬間に巡回が前倒しで動いただけ」
    に見え、頼んだものはいつまでも走らない(実際にそうなった)。
    """
    _replace_one(name, replace(get(name), pending_focus=focus.to_json(), updated_at=_iso(_now())))
    return focus


def clear_focus(name: str) -> None:
    """控えた割り込みを取り下げる。**起こせなかったときに呼ぶ** ——
    残すと、次に走る定時の回が割り込みとして走ってしまう。
    """
    _replace_one(name, replace(get(name), pending_focus=None, updated_at=_iso(_now())))


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
        for sweep in sweeps_of(c)
        if sweep.is_due(now) and waited_for(c, sweep) and not lapped(c, sweep)
    ]
    return sorted(pairs, key=lambda pair: pair[1].due_at())


def lapped(item: Collection, sweep: Sweep) -> bool:
    """一周したら止まる巡回が、もう一周したか(`Sweep.one_lap`)。

    **区画の材料を埋めるような回は、行き渡れば用が済む。** 止めないと 2 周目 3 周目が
    回り続け、同じことを何度も聞くために枠を使う —— そのあとの手入れは、区画ごとに
    回る回が引き継ぐ。

    **区画を持たない収集では止めない**(一周という概念が無い。それは `once` の話)。
    押せばいつでも走る(口のほうでは断らない)。
    """
    if not sweep.one_lap or not item.partitions:
        return False
    if not sweep.walks_partitions():
        # **歩かない回の一周は 1 回**(`Sweep.walks_partitions`)。区画の印で数えると、
        # 印を付けない回は永遠に一周し終わらず、止まるはずの巡回が回り続ける
        return bool(sweep.last_run_at)
    visited, total = partitioning.progress(item.partitions, sweep.name)
    return bool(total) and visited >= total


def blocked_reason(item: Collection, sweep: Sweep) -> str:
    """定時には走らない理由。走る予定なら空。

    **予定を持っていないことと、走らないことは違う。** 予定が空なだけの巡回は
    「いますぐ」だが、先の回を待っている巡回や一周して止まった巡回は走らない ——
    どちらも予定が空なので、区別せずに出すと**いくら待っても動かないものを
    待ち続ける**ことになる(実際、3 日かかる一周を待っている回が「いますぐ」と
    出ていた)。

    **走らない理由を言うのはここだけにする。** 読む側(画面・別のアプリ)が同じ
    場合分けを書き写すと、片方だけが古くなる。
    """
    if not sweep.enabled:
        return "止めている"
    if sweep.on_demand:
        return "頼まれたとき"
    if sweep.once and sweep.last_run_at:
        return "一度きり(済み)"
    if lapped(item, sweep):
        return "一周して止まった"
    if not waited_for(item, sweep):
        return f"「{sweep.after}」の一周待ち"
    return ""


def waited_for(item: Collection, sweep: Sweep) -> bool:
    """先に一周してほしい巡回が、もう一周したか(`Sweep.after`)。

    **区画の材料を埋める回が先に一周していないと、後の回は見当違いのことをする** ——
    実際、名簿を作った直後の漏れ探しは、区画の外に居るだけの画家を「漏れ」として
    20 人挙げ、すべて既存だった。

    **区画を持たない収集では「1 回でも走ったか」で見る**(一周という概念が無い)。
    知らない名前を指していたら待たない —— 待つ相手が居ないのに永久に止まる方が悪い。
    """
    if not sweep.after:
        return True
    others = {s.name: s for s in sweeps_of(item)}
    before = others.get(sweep.after)
    if before is None:
        return True
    # **区画を歩かない回は、1 回走れば一周**(`Sweep.walks_partitions`)。
    # 機械で引く回は区画に印を付けないので、一周を印で数えると 0 / 全区画 のまま
    # 動かず、**その回を待つ巡回は二度と走らない** —— 実際、名簿を待つ「ざっと」が
    # 1 回目(まだ区画が無く、下の枝に落ちた)を最後に止まっていた。
    # 一度きりの回も同じ(`once`。もう走らないので、区画を回り切ることは無い)
    if not item.partitions or not before.walks_partitions() or before.once:
        return bool(before.last_run_at)
    visited, total = partitioning.progress(item.partitions, sweep.after)
    return bool(total) and visited >= total


def due_collections(at: datetime | None = None) -> list[Collection]:
    """いま走らせるべき収集(予定の早い順・重複なし)。"""
    seen: set[str] = set()
    out = []
    for item, _sweep in due_sweeps(at):
        if item.name not in seen:
            seen.add(item.name)
            out.append(item)
    return out


def derives_from(item: Collection, known: set[str] | None = None) -> str:
    """この収集が読んでいる**別の収集**の名前。読んでいなければ空。

    **溜めたものから作る収集**——記事のタグから話題の索引を作る、集めた店から系列を
    起こす——は、材料か抽出の指定で必ず別のソースを名指しする。その名前が収集で
    あれば、それが親になる。**新しい欄は持たない** —— どこから作られたかは既に
    指定に書いてあり、別に持つと 2 つがずれる。

    **見るのは先頭の 1 本だけ。** 指定は何本でも書けるが、親は 1 つに決まっていないと
    並べようがない(ソースをまたぐ名簿では、先頭が主たる材料)。

    **ダンプのソースは親にしない**(jawiki から引く名簿は「jawiki の子」ではない)。
    親子にして読めるのは、どちらもこの層が回している収集どうしのときだけ。
    """
    names = known if known is not None else {one.name for one in load()}
    for source in _material_sources(item):
        if source != item.name and source in names:
            return source
    return ""


def _material_sources(item: Collection) -> list[str]:
    """その収集が材料に読むソース(書いてある順)。"""
    out = []
    if isinstance(item.material, dict) and (source := item.material.get("source")):
        out.append(str(source))
    written = item.extract if isinstance(item.extract, list) else [item.extract]
    for one in written:
        if isinstance(one, dict) and (source := one.get("source")):
            out.append(str(source))
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
    # **どの収集から作られたか**。画面は親の下に並べるのに使う —— 溜めたものから
    # 作る収集が増えるほど、一覧が平らなままでは何と何が組なのか読めない
    data["derives_from"] = derives_from(item)
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
            "next_partition": partitioning.due(
                item.partitions, sweep.name, partitioning.normalize(item.partition)
            ),
            "partitions_per_run": sweep.per_run(len(item.partitions)),
            # **なぜ定時に走らないか**(走るなら空)。読む側が場合分けを
            # 書き写さずに済むよう、理由はこちらが言う
            "blocked": blocked_reason(item, sweep),
        }
        for sweep in sweeps
    ]
    if not with_partitions:
        data.pop("partitions", None)
    return data


# ---- 焼く素材を配る(ingest が取りに来る)------------------------------------
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


def recent(
    name: str, sources: dict, limit: int = 5, include_hidden: bool = False,
) -> list[dict]:
    """焼いてあるもののうち新しい順に何件か(何が集まっているかの手掛かり)。

    **見に行く先は長期記憶** —— 途中の置き場を持たないので、集めたものはここにしかない。
    まだ 1 度も焼いていなければ空(ソースそのものが無い)。

    **読者に出さない印の付いたものは既定で外す**(`notes.HIDDEN_TAGS`。消えたもの・
    まだ AI が目を通していないもの)。ここだけ外していなかったせいで、**読む側が
    自分で落とすことになっていた** —— 実際、外のアプリがその受け止めを持っていた。
    印が増えるたびに読み手を全部直して回ることになり、しかも**漏れるのは外側
    (読者の画面)だけ**なので気づくのが遅い。

    **絞るのは SQL の側**。読んでから落とすと、落としたぶんだけ件数が減る
    (5 件頼んだのに 2 件しか返らない)。

    **タグの索引を持たない古いソースでは何もしない**(絞りようが無い)。
    """
    src = sources.get(name)
    if src is None or limit <= 0:
        return []
    hidden, params = hidden_clause(src, include_hidden)
    where = " WHERE" + hidden if hidden else ""
    rows = db.query(
        src.path,
        "SELECT title, opening, tags, updated_at, extra FROM docs"
        f"{where} ORDER BY updated_at DESC LIMIT ?",
        (*params, limit),
    )
    return [
        {
            "title": row["title"],
            "opening": row["opening"],
            # **どんな 1 件かはタグにしか出ていない。** 同じ収集の中に種類の違うもの
            # (記事とまとめ、など)が混ざるので、無いと読む側が見分けられない
            "tags": load_tags(row["tags"]),
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        }
        for row in rows
    ]


def changed_here(name: str, sources: dict, sweep: str, limit: int = 100) -> list[dict]:
    """その回が最後に動かしたものを、新しい順に(`_stamped` の印で引く)。

    **変更履歴(`app/collect_log.py`)では答えられない問いのほう。** あちらは回ごとに
    1 行で、動いた見出しは頭の 20 件までしか残らず、しかも回ごとの間隔は桁違いなので
    短い回の行が長い回の行を押し流す。ここは**いま手元にあるものを文書の側から**引くので、
    その回が何回前に走っていようが読める。

    **読めるのは最後に動かした回だけ。** 後の回が同じ 1 件に触れば印はそちらに移る
    (印は 1 回分しか持たない)—— 「整理が直したあと見出しが触った」ものは
    見出しのほうに出る。
    """
    src = sources.get(name)
    if src is None or not sweep or limit <= 0:
        return []
    rows = db.query(
        src.path,
        "SELECT title, opening, tags, updated_at, extra FROM docs"
        f" WHERE json_extract(extra, '$.{CHANGED_BY_KEY}') = ?"
        " ORDER BY updated_at DESC LIMIT ?",
        (sweep, limit),
    )
    return [
        {
            "title": row["title"],
            "opening": row["opening"],
            "tags": load_tags(row["tags"]),
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        }
        for row in rows
    ]


def doc_versions(name: str, sources: dict, title: str) -> dict:
    """1 件の見出しについて、**いまの中身と、直す前の中身**を返す。

    「直近の変更」に並ぶのは動いた見出しの名前までで、**何がどう変わったのかは
    そこからは読めない** —— 件数と名前が分かっても、プロンプトを直す判断には
    「どう書き換わったか」が要る。

    **直す前の中身は 1 件の側が持っている**(`_before_of`)。持っていれば
    `kept` に入れて返す —— こちらは**その 1 件を最後に直した回の直前**なので、
    後から別の回が何度焼き直そうと残る。

    世代どうしの比較も一緒に返すが、**あちらは 1 つ前の焼き直しまで**
    (ブルーグリーンが残す世代がそこまで)。間隔の短い回が 1 度走れば比べる相手は
    入れ替わるので、控えを持たない 1 件のための補助として使う。読む人が取り違え
    ないよう、**どの世代どうしを比べたかも一緒に返す**。
    """
    src = sources.get(name)
    if src is None:
        return {"now": None, "before": None, "kept": {}, "now_stamp": "", "before_stamp": ""}
    before_path = previous_generation(src.path)
    now = _doc_at(src.path, title)
    return {
        "now": now,
        "before": _doc_at(before_path, title) if before_path else None,
        "kept": before_of(now) if now else {},
        "now_stamp": src.dump_date or "",
        "before_stamp": generation_stamp(before_path) if before_path else "",
    }


def _doc_at(path: Path, title: str) -> dict | None:
    """その世代の 1 件。**無ければ None**(足された前・消された後がこれに当たる)。"""
    with suppress(Exception):
        rows = db.query(
            path,
            "SELECT doc_id, title, body, tags, updated_at, extra FROM docs"
            " WHERE title = ? LIMIT 1",
            (title,),
        )
        if rows:
            row = rows[0]
            # **`load_json` は使えない**(あれは dict しか通さない。タグは配列)
            try:
                tags = json.loads(row["tags"] or "[]")
            except ValueError:
                tags = []
            return {
                # **世代ごとに違う番号**。いまの世代のものだけが、いまの中身を指せる
                "doc_id": row["doc_id"],
                "title": row["title"],
                "body": row["body"] or "",
                "tags": [str(t) for t in tags] if isinstance(tags, list) else [],
                "updated_at": row["updated_at"] or "",
                "extra": load_json(row["extra"]),
            }
    return None


def previous_docs(name: str, sources: dict) -> dict[str, dict]:
    """前世代(焼き上がっている `corpus/` 側)の全文書(見出し → 文書)。

    **これが「毎回焼き直すのに積み上がる」の要**。足すほうでは素材に前世代を混ぜるので、
    ブルーグリーンの全件作り直しに乗せたまま追記として振る舞う。

    **作り直しでも要る** —— プロンプトへ差し込む素材であり、消えたものを数える相手であり、
    残ったものの `doc_id` を引き継ぐ元でもある。取り込みの経路では 1 度だけ読んで
    使い回す(同じものを 3 回読みに行かない)。
    """
    return _docs_where(sources.get(name), "", ())


def partition_docs(item: Collection, sources: dict, key: str) -> dict[str, dict]:
    """その区画に入っているものだけ。**SQL で先に絞ってから判じる**。

    **区画を 1 つ開くのに全文書を読んでいた。** 本番の食事処(686,602 件)で、
    いちばん小さい区画を開くのに 48.9 秒かかった —— 中身の件数は関係なく、
    全部が「全表を本文ごと dict に読む」ぶんである(実測 1.8 GB 級の山が、
    区画を開くたびに立つ)。

    **絞りは速さのためだけで、正しさは `scoped_docs` が持つ。** 最後の判定は
    今までどおり `partitioning.locator` を通るので、**画面と AI が同じものを
    見る**という約束は動かない —— 絞りが広すぎても結果は変わらない
    (`partitioning.narrowing` が迷うところで絞らない側へ倒しているのはこのため)。
    """
    src = sources.get(item.name)
    if src is None:
        return {}
    where, args = partitioning.narrowing(
        partitioning.normalize(item.partition) if item.partition else None,
        item.partitions, key, getattr(src, "schema_version", 0) or 0,
    )
    members, _scoped = scoped_docs(item, _docs_where(src, where, args), key)
    return members


def _docs_where(src, where: str, args: tuple) -> dict[str, dict]:
    """`docs` から見出し → 文書の dict。**条件は呼ぶ側が持ってくる**。"""
    if src is None:
        return {}
    rows = db.query(
        src.path,
        "SELECT doc_id, title, body, tags, updated_at, extra FROM docs"
        f" WHERE 1=1{where}",
        args,
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
            # **冒頭は持たない**(本文の先頭の写し。焼く側が作る)
            "body": row["body"],
            "tags": [str(t) for t in tags],
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        }
    return out


def load_tags(raw) -> list[str]:
    """タグの列。**読めなければ空**(壊れた控えで落とさない)。"""
    try:
        value = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return [str(t) for t in value] if isinstance(value, list) else []


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

    **母集団に消えたものは入れない**(`living`)。区画の大きさは「この回に見て
    もらう量」なので、消したものを混ぜると実際より多く見える —— 精査を頼む収集は
    墓標が増え続けるので、中身が墓標だけの区画にも巡回の 1 回が回ってくる。

    **毎回は割り直さない。** 母集団を外のソースから取っているなら、こちらが何件
    集めようと点の数は変わらないので、区画は動かないほうがよい —— 動かすと
    巡回の記録が毎回リセットされ、一周が永遠に終わらない。
    自分自身を割っているときだけ、育って `target` を超えた区画や空になった区画が
    出たら割り直す。

    **割り直さない回でも、件数だけは取り直す**(`partitioning.counted`)。
    台帳の数は割ったときの写しなので、中身が別の区画へ移っても古い数が出続ける ——
    判定のためにどのみち数えているので、同じ数を書き戻す。

    **割り直しても巡回の記録は引き継ぐ**(`partitioning.refresh`)。

    **小さすぎる区画は、割り直さない回でも隣とまとめる**(`partitioning.merged`)。
    割り直しの引き金は「育った」と「空になった」しかないので、中身が別の区画へ
    移って痩せた帯は、痩せたまま回り続ける —— 1 人のために 1 回ぶんの枠を使う
    ことになる。まとめるのは周回の記録が同じ隣どうしだけなので、進み具合は動かない。

    **前世代は「呼ぶと流れてくるもの」でも受け取る。** 区画を割るのに要るのは
    見出しとタグと脇書きだけで、本文は要らない —— 数十万件の収集では、本文まで
    読むかどうかで必要なメモリが桁で変わる。
    """
    if not item.partition:
        return []
    spec = partitioning.normalize(item.partition)
    # **母集団は生きているものだけ**(`living`)。消したものは残り続けるので、
    # 混ぜると**精査を頼むほど区画が太り**、見るものが無い区画にも巡回の 1 回が
    # 割り当てられる。消えたものは差し込みには別の一覧として渡る
    alive = _light_docs(previous) if callable(previous) else living(previous)
    counts = partitioning.counts_of(spec, item.partitions, alive)
    if item.partitions and not partitioning.outgrown(spec, counts):
        return partitioning.merged(spec, partitioning.counted(item.partitions, counts))
    built = partitioning.build(spec, sources, alive)
    ledger = partitioning.merged(spec, partitioning.refresh(built, item.partitions, spec))
    log.info("partition %s: %d 区画(まとめる前 %d)", item.name, len(ledger), len(built))
    return ledger


class Edits:
    """その回に集めたもの。**見出しで引ける形**で持つ。

    前世代を 1 行ずつ流して重ねるようになったので、突き合わせの向きが逆になった
    —— 前世代を dict に読んで集めたものを重ねるのではなく、**前世代を流しながら、
    その見出しに来ている直しを引く**。引く側がここ。

    **小さいほうを持つ**のが眼目。定時の巡回で直すのは 1 回に数百件だが、前世代は
    数十万件になりうる(地図の名簿)。大きいほうを持つと、150 件直す回でも 2 GB 要る。
    """

    def __init__(self, collected):
        self._by_title: dict[str, dict] = {}
        self._order: list[str] = []
        self.count = 0
        for raw in collected:
            self.count += 1
            title = notes.title_key(raw.get("title"))
            if title not in self._by_title:
                self._order.append(title)
            self._by_title[title] = raw
        self._used: set[str] = set()

    def take(self, title: str) -> dict | None:
        """その見出しに来ている直し。**取ったら印を付ける**(あとで足す側から外す)。"""
        raw = self._by_title.get(title)
        if raw is not None:
            self._used.add(title)
        return raw

    def rest(self):
        """前世代に無かったぶん。**書いた順**で返す(並びが結果の並びになる)。"""
        for title in self._order:
            if title not in self._used:
                yield self._by_title[title]

    def reset(self) -> None:
        """印を消して、もう一度流せるようにする(数える周と流す周で 2 度使う)。"""
        self._used = set()


def repartition(name: str, sources: dict) -> list[dict]:
    """**台帳の割り直しだけを走らせる**(AI も焼きも動かさない)。組んだ台帳を返す。

    **焼くのと同じ回に乗っていたのが重かった。** 割り直しは母集団を丸ごと 1 周
    舐めて全点をメモリに載せるので、素材を流すのと同時に走ると、そこだけ
    山が二つ重なる —— 本番で、台帳が空の状態から 686,602 件を割り直す回が、
    素材を 280,270 件まで流したところで切れた。ふだんは台帳を使い回すので
    起きないが、**台帳が消えると次の 1 回に必ず乗る**(いちばん重い回が、
    いちばん条件の悪いときに来る)。

    切り離せると、**直す前に台帳だけ整えておける**。押しても:

    - **AI を呼ばない**(枠を使わない)
    - **焼かない**(長期記憶も世代も動かない)
    - **進み具合も次回の予定も動かさない** —— 動かすのは台帳だけ

    **巡回の記録は引き継ぐ**(`partitioning.refresh`)ので、一周は巻き戻らない。
    **割り直しが要らなければ数え直すだけ**(`plan_partitions` の判断そのまま)——
    ここだけ別の規則にすると、ボタンで組んだ台帳と巡回が組む台帳が食い違う。
    """
    item = get(name)
    if item is None:
        raise HTTPException(status_code=404, detail=f"収集 {name} がありません")
    if not item.partition:
        raise HTTPException(
            status_code=400,
            detail="この収集は区画で回っていません(割り直すものがありません)",
        )
    ledger = plan_partitions(item, sources, lambda: stream_previous(name, sources))
    _replace_one(name, replace(get(name), partitions=ledger, updated_at=_iso(_now())))
    log.info("repartition %s: %d 区画", name, len(ledger))
    return ledger


def plan_partitions_next(
    item: Collection, sources: dict, previous, collected,
    only_new: bool = False, edits: bool = False,
) -> list[dict]:
    """**これから焼く世代**で区画を割り直す(`plan_partitions` は前世代で割る)。

    機械で名簿を作り直した回のためのもの。名簿は 1 回で母集団ごと入れ替わるのに、
    台帳は次に走るまで古いまま —— **1 回に何区画を見るかは台帳から決まる**ので、
    **次の巡回が AI を何回叩くのかが、始まるまで誰にも見えなかった**
    (60 万件を 1 区画として持ったまま「1 回に 1 区画」と出る)。

    **毎回やらない。** 定時の巡回が動かすのは 1 回に数百件で、割り直しの引き金
    (育った・空になった)は次の回の頭で普通に効く —— そのために世代をもう 1 周
    舐めるのは高い。入れ替わるのは機械で引く回だけ。
    """
    rows = _rows_of(previous)
    return plan_partitions(
        item, sources,
        lambda: stream_docs(item, rows(), collected, only_new, edits),
    )


def uses_extract(item: Collection, sweep=None) -> bool:
    """この回は機械で引くか。**判断は 1 か所に持つ** ——
    条件を書き写すと、片方だけ直したときに「機械で引いたのに割り直さない」になる。

    **機械で引く回**(`Sweep.use_extract`)か、**進み具合が空の 1 回目**。
    前者は名簿を最新に保つための回 —— 外のカテゴリは増えていくのに、1 回目しか
    機械で埋めないと、そのあと増えたぶんは永遠に入らない。
    """
    if not item.extract:
        return False
    return bool((sweep is not None and sweep.use_extract) or not item.cursor)


def by_hand_sweep(item: Collection):
    """手で回す巡回(いちばん先に書いてあるもの)。無ければ None。

    **名指しされなければこれを使う** —— 手で回す回を 2 本持つ収集は、
    いまのところ考えなくてよい(束は 1 つしか預かれない)。
    """
    return next((s for s in sweeps_of(item) if s.by_hand and s.enabled), None)


HANDOFF_HEAD = """<!--
この束は Chiezo が組んだものです（収集「{name}」/ 巡回「{sweep}」）。
渡す相手は web の画面から使う AI（Gemini など）で、答えはファイルで受け取ります。
-->

# {name} —— {sweep}

**このファイルを最後まで読んでから作業してください。**
{scope}

答えは**説明を付けず、下の「返す形」のとおりの JSON だけ**を
`answer.json` というファイルにして返してください。
**範囲が複数あっても、答えは 1 つの `items` にまとめて**構いません
（どの範囲のものかは座標とタグから分かります）。
Chiezo はそのファイルをそのまま読み込みます（前置きや ```json の囲みが
混ざっていても拾いますが、**JSON 以外の中身は捨てます**）。

**触ったものと、新しく足すものだけを返してください。** 直すところが無かった
1 件は返さなくて構いません（返さなかったものはそのまま残ります）。

---

## 返す形

```json
{shape}
```

---

{body}"""

HANDOFF_SHAPE = (
    '{"items": [{"title": "見出し", "body": "本文", "url": "出典の URL",'
    ' "tags": ["タグ"], "lat": 35.0, "lon": 139.0}], "next_cursor": "次に続きを見る印"}'
)


def handoff_body(item: Collection, sweep, sections: list[list[dict]], keys: list[str]) -> str:
    """束の本文。**AI へ投げるのと同じ文**に、人とファイルのための前置きを足す。

    **前置きが要る。** 材料だけを渡すと、web の画面は要約や感想を返してくる ——
    「読んで、この形の JSON をファイルで返す」までを、材料より先に言い切る。

    **中身は書き換えない。** 依頼文そのものは `build_messages` が組んだものを
    そのまま載せる —— ここで言い換えると、AI に頼んだ回と手で回した回で
    違うことを頼むことになり、結果を比べられなくなる。

    **区画ごとに節を分ける**(`sections` は区画 1 つぶんの messages の並び)。
    まとめて 1 つの節にしていた頃は、**範囲が「(全体)」になり、しかも差し込みが
    天井(`MAX_MATERIAL_DOCS`)で切られた** —— 本番の 1 束目は 5 区画・497 件の
    うち 300 件しか載らず、範囲も書かれていないので、**主な仕事である
    「この範囲に足りない店を足す」が 1 件も返ってこなかった**(タグの手入れだけ)。
    節に分ければ、どの節も「その範囲の全部が並んでいる」ことを保てる。
    """
    scope = (
        f"今回の範囲は **{len(keys)} 区画**です。"
        "**節ごとに範囲が違います** —— それぞれの節で、その範囲の仕事をしてください。"
        if len(keys) > 1 else "今回の範囲は下に書いてあります。"
    )
    parts = []
    for index, messages in enumerate(sections, 1):
        head = (f"## 範囲 {index} / {len(sections)}\n\n" if len(sections) > 1 else "## 頼みごと\n\n")
        parts.append(head + "\n\n".join((m.get("content") or "") for m in messages))
    return HANDOFF_HEAD.format(
        name=item.name, sweep=sweep.name if sweep else DEFAULT_SWEEP_NAME,
        scope=scope, shape=HANDOFF_SHAPE, body="\n\n---\n\n".join(parts),
    ) + "\n"


def asks_ai(item: Collection, sweep=None) -> bool:
    """この回は AI に頼むか。**判断は 1 か所に持つ**(`uses_extract` と同じ理由)。

    機械で引く回も外の道具で引く回も AI を呼ばない。**呼ばない回の相手を控えに
    残さない**ために要る —— 残すと、後から読む人には「この回は AI で走っている」と
    見える(画面にも相手を出さない、と決めているのと同じ話)。
    """
    if uses_extract(item, sweep):
        return False
    # 手で回す回も AI を呼ばない —— 答えを書いたのは Chiezo が知らない相手で、
    # 既定の相手を控えに残すと「その相手に頼んだ回」として履歴に並ぶ
    return not (sweep is not None and (sweep.use_feed or sweep.by_hand))


def stream_docs(
    item: Collection,
    previous,
    collected,
    only_new: bool = False,
    edits: bool = False,
    diff: dict | None = None,
    sweep: str = "",
    unreviewed: bool = False,
    reviewed: set[str] | None = None,
    facts: bool = False,
):
    """焼く素材を 1 件ずつ返す。`material` の中身で、**丸ごとは持たない**。

    **前世代を外側にして回す。** 前世代は数十万件になりうるので、dict に読むと
    行の数だけメモリが要る —— 50 万件の地図の名簿で 1.8 GB になった(実測)。
    流しながら、その見出しに来ている直しを `collected` から引いて重ねる。

    **並びは前世代の `doc_id` 順、そのあとに新しいぶん。** 前は最後に並べ直して
    いたが、丸ごと持たないと並べ替えられない —— 前世代を `doc_id` 順に読めば
    同じ並びになる(新しいぶんは採番の順で後ろに付く)。

    `diff` を渡すと、数えた結果をそこへ書く(戻り値にできないため)。

    **動かした 1 件には、どの回が動かしたかを脇書きに残す**(`_stamped`)。
    直した 1 件には、**直す前の中身も 1 回分だけ**控える(`_before_of`)。

    `unreviewed` は「この回で入るものに、まだ AI が目を通していない印を付ける」
    (機械で引く回・外の道具で引く回)。`reviewed` は**この回で AI に差し込んだ見出し**で、
    その印を外す —— 差し込まれた時点で目は通っている。
    **返ってこなかったものも外す** —— 整理は触ったものしか返さないので、
    返りだけを見ていると「読んだうえで直す必要が無かった」が未精査のまま残る。

    **同じ URL の 1 件は足さない**(`url_key`)。見出しが重複の鍵だが、見出しは
    書き換わる —— 書き換えた側は新しい 1 件として通るので、同じ記事が 2 件並ぶ。
    弾いたぶんは `duplicates` に数えて見出しも残す(黙って落とさない)。

    `facts` は「機械が運んできた脇書きで入れ替える」(機械で引く回)。数えた値は
    回るたびに変わるので、足すだけの回でもそこだけは新しくする —— 本文とタグは
    AI のものなので触らない。
    """
    counts = diff if diff is not None else {}
    now = _iso(_now())
    # **見出しで引ける形なら、そのまま使う。** 抽出が返す名簿は一時の SQLite に
    # 載っている(`app/extract.py` の `Roster`)—— 数十万件を dict に積み直したら、
    # 逃がした意味が消える
    edits_of = collected if hasattr(collected, "take") else Edits(collected)
    edits_of.reset()
    # **同じ記事を二度入れない。** 1 件を指す鍵は見出しだが、見出しは書き換わる ——
    # AI は外から見つけた 1 件に自分の言葉で見出しを付けるし、配信元が違えば同じ
    # 記事が別の見出しで流れてくる(「記事名 - サイト名」と「記事名」)。
    # 鍵が見出しだけだと、そのどちらも新しい 1 件として通る(本番で、生きている
    # 記事 364 件のうち 22 件が同じ URL の複製だった)。
    incoming_urls = _incoming_urls(collected)
    known_urls: set[str] = set()
    # **足したぶんの見出し**。長期記憶は見出しに一意の索引を張るので、同じ見出しを
    # 2 度流すと**焼く段で索引が張れず、取り込みがまるごと落ちる**(世代は
    # 切り替わらないので、集めたぶんが静かに消えたように見える。本番で起きた)。
    # 候補の側は既に切り詰めた見出しで重複を畳んである(`Edits` / `extract.Roster`)
    # ので、ここは最後の歯止め —— 持つのは足したぶんだけで、前世代は数えない
    # (数十万件の見出しを抱えると、1 行ずつ流すようにした意味が消える)
    fresh_titles: set[str] = set()
    duplicate_titles: list[str] = []
    added = updated = skipped = seen = 0
    next_id = 0
    added_titles: list[str] = []
    updated_titles: list[str] = []
    removed_titles: list[str] = []

    for before in previous:
        seen += 1
        next_id = max(next_id, before["doc_id"])
        title = before["title"]
        # **消えた 1 件の URL も控える**(`_chiezo_removed`)—— 調べたうえで外した
        # ものが、書き換えた見出しで戻ってくるのを止める
        if incoming_urls:
            prev_extra = before.get("extra")
            key = url_key(prev_extra.get("url") if isinstance(prev_extra, dict) else "")
            if key in incoming_urls:
                known_urls.add(key)
        raw = edits_of.take(title)
        if raw is None:
            yield _reviewed(before) if reviewed and title in reviewed else before
            continue
        if only_new:
            # **足すだけの回。** 既にあるものには触らない(数えるだけ)。
            # ただし、まだ持っていない脇書きは受け取る。
            # **機械で引く回は、運んできた鍵を入れ替える**(`facts`)—— 数えた値は
            # 回るたびに変わるので、足さないと最初に拾った日の数が残り続ける
            skipped += 1
            yield _with_facts(before, raw) if facts else _with_new_facts(before, raw)
            continue
        if edits and _is_tombstone(raw):
            # 墓標。**消さずに印を付けて残す**
            removed_titles.append(title)
            updated += 1
            yield _stamped(
                _reviewed(_buried(before, raw, now)), sweep, "removed", by=signed_by(raw),
            )
            continue
        if is_removed(before) and not _is_restore(raw):
            # **消えたものは、戻す印を付けて返したときだけ戻す。** 印の無い返りは
            # 戻すつもりの無いもの(タグを書き直して、消えた印を写し忘れただけ)として
            # 触らない —— 閉店で消した店が、別の依頼の回に説明を書き直されて戻った
            skipped += 1
            yield before
            continue
        doc = _to_doc(raw, now, item.web)
        if doc is None:
            skipped += 1
            yield before
            continue
        if is_removed(before):
            # 戻す。消えた印を行ごと写して返してきても、戻す印のほうを優先する
            doc["tags"] = [t for t in doc["tags"] if t != notes.REMOVED_TAG]
        before_extra = before.get("extra")
        doc["extra"] = _merge_extra(
            before_extra if isinstance(before_extra, dict) else {}, doc["extra"]
        )
        if edits:
            updated += 1
            updated_titles.append(title)
        else:
            # 集めるほうで同じ見出しが来るのは「もう持っている」の意味
            skipped += 1
            yield {**doc, "doc_id": before["doc_id"]}
            continue
        yield _stamped(
            _reviewed({**doc, "doc_id": before["doc_id"]}), sweep, "updated", before,
            by=signed_by(raw),
        )

    next_id += 1
    for raw in edits_of.rest():
        title = notes.title_key(raw.get("title"))
        if only_new and edits and _is_tombstone(raw):
            skipped += 1
            continue
        if edits and _is_tombstone(raw):
            # **持っていないものへの墓標は数えない**(消すものが無い)
            skipped += 1
            continue
        doc = _to_doc(raw, now, item.web)
        if doc is None:
            skipped += 1
            continue
        doc["extra"] = _merge_extra({}, doc["extra"])
        key = url_key(doc["extra"].get("url"))
        if key and key in known_urls:
            # **同じ URL の 1 件が既にある。** 見出しが違っても同じ記事なので足さない
            # —— 足すと、読む人には同じ記事が 2 件並ぶ。
            # **書き換えた見出しは通らない**(その 1 件は先に入った見出しのまま残る)
            skipped += 1
            duplicate_titles.append(doc["title"])
            continue
        if key:
            # **同じ回の中の重複も止める**(配信元が 2 つ、同じ記事を別の見出しで配る)
            known_urls.add(key)
        if doc["title"] in fresh_titles:
            skipped += 1
            duplicate_titles.append(doc["title"])
            continue
        fresh_titles.add(doc["title"])
        added += 1
        added_titles.append(doc["title"])
        fresh = _unreviewed(doc) if unreviewed else doc
        yield _stamped({**fresh, "doc_id": next_id}, sweep, "added", by=signed_by(raw))
        next_id += 1

    counts.update({
        "previous": seen,
        "total": seen + added,
        "added": added,
        "updated": updated,
        "kept": seen,
        "removed": len(removed_titles),
        "skipped": skipped,
        # **黙って落とさない。** 弾いた件数と見出しを残す —— 数えないと、
        # 入るはずのものが入らないときに気づく手掛かりが無い
        "duplicates": len(duplicate_titles),
        "duplicate_titles": duplicate_titles[:MAX_TITLE_SAMPLE],
        "added_titles": added_titles[:MAX_TITLE_SAMPLE],
        "updated_titles": updated_titles[:MAX_TITLE_SAMPLE],
        "removed_titles": removed_titles[:MAX_TITLE_SAMPLE],
        "collected": edits_of.count,
    })


def material(
    item: Collection,
    previous: dict[str, dict],
    collected: list[dict],
    only_new: bool = False,
    edits: bool = False,
    sweep: str = "",
    unreviewed: bool = False,
    reviewed: set[str] | None = None,
    facts: bool = False,
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

    **`only_new` の回は、既にある見出しに触らない**(足すだけ)。「漏れているものを
    足して」と頼む回に要る印で、**AI の判断に頼らずにここで保証する** —— 見せられるのは
    その区画のぶんだけなので、AI には「もう居るかどうか」が分からない(別の括りに
    入っていることも、タグが間違っていることもある)。触らせると、既にいる有名なものが
    薄い内容で上書きされ、持っていたタグごと落ちる。**墓標も読まない** ——
    足すだけの回に消す力を持たせない。

    **直す回かどうか**(`edits`)で違うのは 2 つだけ:

    - **同じ見出しをどう数えるか。** 直しに来ていない回では「既に持っていた」ので
      追加に数えない。直す回では、置き換わったことを `updated` に数える。
    - **墓標を読むかどうか。** 直す回だけ、`_chiezo_tombstone` の付いた見出しを落とす
      **消すのは明示したときだけ**。

    直す回かどうかは**その回の依頼文が語っている**(`edits_what_is_there`)——
    今あるものを差し込んでいなければ、AI は今あるものを知らないので、
    消す力を持たせられない。

    **`doc_id` は前世代のものを引き継ぐ**。残った文書の URL が焼き直しで変わらないため。
    """
    counts: dict = {}
    rows = sorted(previous.values(), key=lambda d: d["doc_id"])
    docs = list(stream_docs(
        item, rows, collected, only_new, edits, counts, sweep, unreviewed, reviewed, facts,
    ))
    return docs, counts


def _is_tombstone(raw: dict) -> bool:
    """墓標か(消してほしい、の印)。"""
    tags = raw.get("tags") or []
    return any(str(t).strip() == notes.TOMBSTONE_TAG for t in tags)


def _is_restore(raw: dict) -> bool:
    """消えたものを戻す指示か(`notes.RESTORE_TAG`)。"""
    tags = raw.get("tags") or []
    return any(str(t).strip() == notes.RESTORE_TAG for t in tags)


def shrink_blocked(item: Collection, diff: dict, edits: bool = False) -> str | None:
    """墓標で消しすぎていたら、その理由の文。問題なければ None。

    **AI が変な日に当たった 1 回で、育てた分類が消えるのを止める**のがここ。
    返し忘れでは減らなくなったので、ここが止めるのは**明示的な大量削除**だけになった
    —— そのぶん、止まったときの意味が鋭い(AI が「全部要らない」と言っている)。
    足すほうには要らない(そもそも減らない)。`keep_ratio` を 0 にすると外れる。

    **数えるのは「印を付けた件数」**。消さずに印を付けて残すようになったので、
    件数そのものは減らない —— 結果の件数で見ていると、9 割に印が付いた回が
    素通りする(実際、この形にした直後の歯止めがそうなっていた)。
    """
    if not edits or item.keep_ratio <= 0 or not diff["previous"]:
        return None
    floor = diff["previous"] * item.keep_ratio
    left = diff["previous"] - diff["removed"]
    if left >= floor:
        return None
    sample = "、".join(diff["removed_titles"][:5])
    return (
        f"整理の結果、前の {diff['previous']} 件のうち {diff['removed']} 件に"
        f"消えた印が付き、残るのは {left} 件です(下限 {floor:.0f} 件)。"
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
    title = notes.title_key(raw.get("title"))
    body = (raw.get("body") or "").strip()[:MAX_BODY_CHARS]
    if not title or not body:
        return None
    # 戻す印は指示なので文書には残さない(墓標と同じ)
    tags = [
        str(t).strip() for t in (raw.get("tags") or [])
        if str(t).strip() and str(t).strip() != notes.RESTORE_TAG
    ]
    # **運ばれてきた事実を先に置く。** 集める側が元の記事から写した値(知名度など)が
    # ここに入る —— 下で入れるものが鍵を持っていたら、そちらを優先する。
    # **この時点では「消して」の印(null)も混じる**(重ねるときに解く)
    extra = {**_carried(raw.get("extra")), COLLECTED_AT_KEY: now, "web": bool(web)}
    if url := (raw.get("url") or "").strip():
        extra["url"] = url
    # **配信日は、集めた日と別に持つ**。フィードから機械的に溜めるときに入る ——
    # 集めた日だけだと、半年前の記事を今日拾ったのか、今日出たものなのかが読めない
    if at := _published(raw.get("at")):
        extra["published_at"] = at
    # **絵の URL**(画像そのものは持たない)。jawiki の代表画像と同じ鍵にしてある ——
    # 読む側は「この 1 件の絵」としてだけ見ればよく、どこから来たかは問わない
    if image := _http_url(raw.get("image")):
        extra["image"] = image
    # **座標は運ぶ**。矩形で区画を割る収集では、これが無いと集めたものがどの区画にも
    # 入らない(次に同じ区画を見たとき「まだ何も無い」と見えて、同じものを集め直す)。
    # ついでにコアスキーマの生成列に乗るので、`filter?bbox=` で普通のソースとして引ける
    lat, lon = _coords(raw)
    if lat is not None:
        extra["lat"], extra["lon"] = lat, lon
    return {
        "doc_id": 0,  # material が前世代から引き継ぐか、新しく振る
        "title": title,
        # **冒頭(`opening`)は持たせない。** 本文の先頭を切っただけの写しで、
        # 焼く側が同じものを作れる(`ingest/sources/remote.py`)—— 素材に乗せると
        # **1 件につき本文の先頭が 2 回流れる**(実測で 59.7 万件の収集の 1 割)
        "body": body,
        "tags": tags,
        "updated_at": now,
        "extra": extra,
    }


def _buried(doc: dict, raw: dict, now: str) -> dict:
    """消えた印を付けた 1 件。**本文も理由も、両方残す**。

    **持っていたタグは残す。** 外したあとも「どういう条件でここに入ったのか」が
    読めないと、消し間違いを確かめようがない。

    **理由は本文に書かせている**(`REFINE_SYSTEM_PROMPT`)が、**本文を理由で
    置き換えない** —— 置き換えていた頃は、消し間違いを戻すときに元の中身が
    もう無かった。印を外せば戻せる作りなのに、戻ってくるのが「なぜ消したか」の
    1 行だけでは、育てたぶんを集め直すことになる。理由は脇書きへ置く。

    **消す前に AI が目を通していたかも残す**(`REMOVED_AFTER_REVIEW_KEY`)。
    目を通したものは既に読み手へ出ている(未レビューは読み口が外す)ので、
    読み手の手元に写しがあるかもしれない —— 写しを消させるには、そちらだけを
    選び出せる必要がある。最初の目通しで弾かれたものは一度も出ていないので、
    消させる必要が無い。**消えた印を付けたあとでは見分けられない**(未レビューの印は
    消すのと同じ回で外れる)ので、印を付ける前のここで読む。
    既に消えていたものを消し直すときは、最初に消したときの値を引き継ぐ。
    """
    tags = [t for t in (doc.get("tags") or []) if t != notes.REMOVED_TAG]
    why = (raw.get("body") or "").strip()[:MAX_REMOVED_REASON_CHARS]
    extra = doc.get("extra") if isinstance(doc.get("extra"), dict) else {}
    after_review = (
        bool(extra.get(REMOVED_AFTER_REVIEW_KEY)) if is_removed(doc) else not is_unreviewed(doc)
    )
    return {
        **doc,
        "tags": [*tags, notes.REMOVED_TAG],
        "extra": {
            **extra,
            "removed_reason": why,
            "removed_at": now,
            REMOVED_AFTER_REVIEW_KEY: after_review,
        },
        "updated_at": now,
    }


CHANGE_ADDED = "added"
CHANGE_UPDATED = "updated"
CHANGE_REMOVED = "removed"

# 脇書きに残す「最後に動かした回」の鍵。
CHANGED_BY_KEY = "changed_by"
CHANGE_KEY = "change"
# 直す前の中身を控えておく鍵(`_before_of`)。
BEFORE_KEY = "before"
# その 1 件を最後に集めた時刻(`_to_doc`)。AI が触った回には必ず入り直す
COLLECTED_AT_KEY = "collected_at"
# **その 1 件を最後に動かした AI とモデル**(`antigravity / gemini-3.8-flash-medium`)。
# 回ごとの控え(`app/collect_log.py`)にも相手は残るが、あれは 1 回 1 行で流れていく
# うえ、**1 回の中で相手が振り替わる**(ワーカーは区画ごとに枠の空いた段を選ぶ)——
# 「この 1 件を書いたのは誰か」は、その 1 件に押しておくしか読む手が無い。
# **AI を呼ばない回には付かない**(機械で引く回・外の道具で引く回)。
CHANGED_AI_KEY = "changed_ai"
# 集めたものに載せて運ぶための鍵。**脇書きではなく素の側に置く** ——
# `_carried` は `extra` の中だけを見るので、ここに置けば AI の返した事実と混ざらない
SIGNED_BY_KEY = "_chiezo_by"
# 前と比べない脇書き。**こちらが回ごとに押す印なので、混ぜると毎回「変わった」に
# なる** —— 本当に動いた事実が埋もれる。控えに入れると、さらに控えの中に控えが入り、
# 焼き直すたびに入れ子が 1 段深くなる(1 件が際限なく伸びる)
MARGIN_KEYS = (CHANGE_KEY, CHANGED_BY_KEY, CHANGED_AI_KEY, BEFORE_KEY, COLLECTED_AT_KEY)


def signed(items: list[dict], backend: str, model: str) -> list[dict]:
    """集めた 1 件ずつに、**どの AI が書いたか**を載せる。

    **回ごとではなく 1 件ごとに載せる。** ワーカーを使う回は区画ごとに相手が
    振り替わる(枠の空いた段を選ぶ)ので、回の単位で 1 つに丸めると**半分の
    文書に嘘の署名が付く**。

    **機械で引く回には呼ばれない** —— あちらは AI を通さないので署名のしようがない。
    """
    if not backend:
        return items
    mark = f"{backend} / {model}" if model else backend
    return [{**raw, SIGNED_BY_KEY: mark} for raw in items]


def signed_by(raw) -> str:
    """その 1 件に載っている署名。無ければ空。"""
    return str((raw or {}).get(SIGNED_BY_KEY) or "")[:MAX_CARRIED_CHARS]


def _stamped(
    doc: dict, sweep: str, change: str, before: dict | None = None, by: str = "",
) -> dict:
    """動かした 1 件に、**どの回が・どう動かしたか**を脇書きとして押す。

    **変更履歴を別に持つだけでは、読みたいほうが読めない。** 控え
    (`app/collect_log.py`)は回ごとに 1 行で、動いた見出しは頭の 20 件しか
    残らない。しかも回ごとの間隔は桁違いなので、短い回の行が長い回の行を押し流す
    —— 「整理が何を直したのか」を引きに来ると、たいてい流れた後になる。
    文書の側に押しておけば、**いま手元にあるものについては回の間隔と関係なく読める**。

    **残すのは 1 回分だけ**(上書きする)。履歴を溜める場所ではない ——
    溜めると 1 件ごとに際限なく伸び、焼き直すたびに全件がその分だけ重くなる。
    「いつ」は既にある `updated_at` 列が持っているので、ここには持たない。

    **脇書きに置くのは、検索と本文精査の邪魔をしないため。** 全文検索が索引するのは
    見出しと本文だけ(`ingest/core.py` の `docs_fts`)、AI へ差し込む一覧に載るのも
    配信日と出典だけ(`_current_line`)なので、ここへ入れたものはどちらにも出てこない。
    本文やタグへ書くと、読む人にも AI にも「文書の中身」として見える。

    **鍵の数の天井(`MAX_EXTRA_KEYS`)より後に押す。** あの天井は AI が運んでくる
    事実が際限なく増えないためのもので、こちらが押す印まで落とすと、
    脇書きの多い 1 件だけ印が付かないことになる(いちばん読みたい 1 件がそれになる)。

    `before` を渡すと、**直す前の中身も 1 回分だけ一緒に控える**(`_before_of`)。
    """
    extra = doc.get("extra") if isinstance(doc.get("extra"), dict) else {}
    stamp = {CHANGE_KEY: change}
    if sweep:
        stamp[CHANGED_BY_KEY] = sweep
    # **AI を呼ばない回では消す。** 前に AI が触った 1 件を機械の回が動かしたとき、
    # 古い署名が残っていると「この内容を書いたのはこの AI」と読めてしまう
    if by:
        stamp[CHANGED_AI_KEY] = by
    else:
        extra = {k: v for k, v in extra.items() if k != CHANGED_AI_KEY}
    merged = {**extra, **stamp}
    # **印と控えは必ず揃える。** 前の回の控えを残したまま印だけ新しくすると、
    # 画面には「この回が直す前」として前の回の中身が出る
    if kept := _before_of(doc, before):
        merged[BEFORE_KEY] = kept
    else:
        merged.pop(BEFORE_KEY, None)
    return {**doc, "extra": merged}


def _before_of(doc: dict, before: dict | None) -> dict:
    """直す前の中身のうち、**この回で変わったところだけ**。

    世代の比較(`doc_versions` の `_previous_generation`)では、
    **その回が直したものを見に行った頃にはもう流れている** —— 残る世代は 1 つ前
    までで、巡回は回るたびに焼き直すので、間隔の短い回が 1 度走っただけで
    比べる相手が入れ替わる。1 件の側に控えておけば、次に同じ 1 件が動くまで残る。

    **本文だけではない。** この層の直しは、本文の書き換えと同じくらいタグの
    付け替え(分類そのもの)と脇書きの差し替え(出典・配信日・座標)で起きる ——
    本文しか控えないと、「何も変わっていないのに動いた 1 件」が並ぶ。

    **変わっていないものは入れない。** 丸ごと控えると 1 件の大きさが倍になり、
    焼き直すたびに全件がそのぶん重くなる。

    **見出しは控えない。** この層で 1 件を指す鍵は見出しで、直す回は見出しで
    引き当てている(`stream_docs`)—— 書き換えた見出しは別の 1 件として通るので、
    ここへ来る時点で見出しは必ず同じ。
    """
    if not isinstance(before, dict):
        return {}
    kept: dict = {}
    was = (before.get("body") or "").strip()
    if was != (doc.get("body") or "").strip():
        kept["body"] = was[:MAX_BEFORE_BODY_CHARS]
    if sorted(before.get("tags") or []) != sorted(doc.get("tags") or []):
        kept["tags"] = [str(t) for t in (before.get("tags") or [])]
    if (facts := facts_of(before)) != facts_of(doc):
        kept["extra"] = facts
    return kept


def facts_of(doc: dict) -> dict:
    """脇書きのうち、**その 1 件についての事実だけ**(こちらが押す印を外す)。

    印(`MARGIN_KEYS`)は回ごとに必ず書き換わるので、混ぜたまま前と比べると
    **毎回「脇書きが変わった」になる** —— 本当に変わった事実が埋もれる。
    """
    extra = doc.get("extra")
    if not isinstance(extra, dict):
        return {}
    return {k: v for k, v in extra.items() if k not in MARGIN_KEYS}


def changed_by(doc: dict) -> str:
    """その 1 件を最後に動かした回。持っていなければ空。"""
    extra = doc.get("extra")
    return str((extra or {}).get(CHANGED_BY_KEY) or "") if isinstance(extra, dict) else ""


def before_of(doc: dict) -> dict:
    """その 1 件を最後に動かした回が、**直す前の中身**(変わったところだけ)。

    持っていなければ空。入っているのは `body` / `tags` / `extra` のうち、
    その回で実際に動いたものだけ。
    """
    extra = doc.get("extra")
    kept = extra.get(BEFORE_KEY) if isinstance(extra, dict) else None
    return kept if isinstance(kept, dict) else {}


def _unreviewed(doc: dict) -> dict:
    """まだ AI が目を通していない印を付ける(`notes.UNREVIEWED_TAG`)。"""
    tags = [t for t in (doc.get("tags") or []) if t != notes.UNREVIEWED_TAG]
    return {**doc, "tags": [*tags, notes.UNREVIEWED_TAG]}


def _reviewed(doc: dict) -> dict:
    """印を外す。**持っていなければ何もしない**(同じ辞書を返す)。

    毎回新しい辞書を作らないのは、触っていない 1 件がそのまま流れる道だから ——
    数十万件を積み直すと、前世代を 1 行ずつ読んでいる意味が消える。
    """
    tags = doc.get("tags") or []
    if notes.UNREVIEWED_TAG not in tags:
        return doc
    return {**doc, "tags": [t for t in tags if t != notes.UNREVIEWED_TAG]}


def is_unreviewed(doc: dict) -> bool:
    """まだ AI が目を通していないか。"""
    return notes.UNREVIEWED_TAG in (doc.get("tags") or [])


# 消す前に AI が目を通していたか(`_buried`)。**目を通したものは読み手へ出ている**ので、
# 読み手が写しを消すべきかはこれで決まる。入れる前に消えたものには付いていない
REMOVED_AFTER_REVIEW_KEY = "removed_after_review"

# 墓標に残す脇書き。**これ以外は落とす**（毎回の素材に丸ごと乗るため）。
#
# - `removed_reason` / `removed_at` …… なぜ・いつ外したか（画面と AI が読む）
# - `removed_after_review` …… 消す前に目を通していたか(読み手が写しを消すかを決める)
# - `url` …… **同じものが別の見出しで戻ってくるのを止めている鍵**
#   (`stream_docs` の重複判定)。落とすと、調べたうえで外したものが
#   書き換えた見出しで足し直される
KEPT_ON_REMOVED = ("removed_reason", "removed_at", REMOVED_AFTER_REVIEW_KEY, "url")


def slim_removed(doc: dict) -> dict:
    """消えた 1 件から、**要らない脇書きを落とした**もの。生きている 1 件はそのまま。

    墓標は消えずに残り続けるので、**毎回の素材に丸ごと乗ります** —— 実測で
    1 件 268 文字の脇書き(座標・住所・電話・サイト・出典…)を持っており、
    一周ぶん溜まると素材の 2 割に達します。**墓標に要るのは「なぜ外したか」と
    「同じものを戻さないための鍵」だけ**で、座標も電話も読む人がいません。

    **本文は落としません。** 印を外せば元の中身のまま戻せる、が墓標の約束で、
    戻ってくるのが理由の 1 行だけでは育てたぶんを集め直すことになります。
    """
    if not is_removed(doc):
        return doc
    extra = doc.get("extra")
    if not isinstance(extra, dict):
        return doc
    kept = {k: v for k, v in extra.items() if k in KEPT_ON_REMOVED}
    return doc if kept == extra else {**doc, "extra": kept}


def removed_reason(doc: dict) -> str:
    """その 1 件を消した理由。持っていなければ空。"""
    extra = doc.get("extra")
    return (extra.get("removed_reason") or "").strip() if isinstance(extra, dict) else ""


def _with_new_facts(doc: dict, raw: dict) -> dict:
    """既にある 1 件に、**まだ持っていない脇書きだけ**を足す。

    足すだけの回は中身に触らないが、機械で運ぶ事実は別 —— 指定に鍵を足しても、
    既にいるものには届かないままになる(名簿を焼き直しても全員が飛ばされる)。
    **持っている値は上書きしない**ので、育てた中身も、先に入った事実も動かない。
    """
    extra = doc.get("extra") if isinstance(doc.get("extra"), dict) else {}
    # **消しの印は効かせない**(足すだけの回は、何も落とさない)
    fresh = {
        k: v for k, v in _carried(raw.get("extra")).items()
        if v is not None and k not in extra
    }
    return {**doc, "extra": _merge_extra(extra, fresh)} if fresh else doc


def _with_facts(doc: dict, raw: dict) -> dict:
    """既にある 1 件の脇書きを、**機械が運んできた値で入れ替える**。

    数えた値(件数・直近の件数・最後に付いた日)は回るたびに変わるので、
    **足すだけの回でもそこだけは新しくする** —— 入れ替えないと、最初に拾った日の
    数がそのまま残り、「いま動いているか」が永久に古いままになる。

    **触るのは運ばれてきた鍵だけ。** 本文もタグも、運ばれていない鍵も動かさない ——
    あちらは AI が育てるもので、機械が持ち主ではない。
    """
    extra = doc.get("extra") if isinstance(doc.get("extra"), dict) else {}
    fresh = {k: v for k, v in _carried(raw.get("extra")).items() if v is not None}
    return {**doc, "extra": _merge_extra(extra, fresh)} if fresh else doc


def _carried(raw) -> dict:
    """運ばれてきた事実。**そのまま載る値だけを通す**。

    載せるのは「元の長期記憶に書いてある事実」で、読む側が 1 件ずつ引き直さなくて
    済むようにするためのもの(知名度・座標など)。**入れ子は通さない** ——
    ここは 1 件の脇に添える札で、記事を丸ごと写す場所ではない。

    **短い語の並びは通す** —— 「一緒に出てくる語」のように、1 件の脇に添える事実が
    並びになることがある。通すのは**そのまま載る値の並び**だけで、入れ子の入れ子は
    やはり通さない(`MAX_CARRIED_ITEMS` で長さも切る)。

    **null はそのまま通す**(「この鍵を消して」の印。解くのは `_merge_extra`)。
    """
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, value in list(raw.items())[:MAX_CARRIED_KEYS]:
        name = str(key).strip()[:MAX_CARRIED_KEY_CHARS]
        if not name:
            continue
        if isinstance(value, list):
            if kept := _carried_list(value):
                out[name] = kept
            continue
        if value is None or isinstance(value, (str, int, float, bool)):
            out[name] = value[:MAX_CARRIED_CHARS] if isinstance(value, str) else value
    return out


def _carried_list(raw: list) -> list:
    """並びの脇書き。**そのまま載る値だけ**を、長さを切って通す。"""
    out = []
    for value in raw[:MAX_CARRIED_ITEMS]:
        if isinstance(value, str):
            out.append(value[:MAX_CARRIED_CHARS])
        elif isinstance(value, (int, float, bool)):
            out.append(value)
    return out


def _merge_extra(before: dict, now: dict) -> dict:
    """脇書きを重ねる。**言われていないものは消さない**。

    1 件は焼くたびに丸ごと置き換わるが、脇書きだけは別に扱う —— 機械で運んだ事実
    (知名度・座標)は、AI が手を入れる回には返ってこない。置き換えると最初の手入れで
    静かに消える(この層でいちばん起きやすい壊れ方で、1 件ずつ減るので
    消えすぎの歯止めもすり抜ける)。

    **`null` は「この鍵を消して」の印。** 書かなければ残る作りなので、
    間違って入った値を落とす口がどこかに要る。

    **数に天井を置く。** 消さない作りなので、置かないと回を重ねるだけ増える。
    """
    out = dict(before)
    for key, value in now.items():
        if value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return dict(list(out.items())[:MAX_EXTRA_KEYS])


def _http_url(raw) -> str:
    """http(s) の URL だけを通す。**それ以外は持たない**(画面がそのまま指すため)。"""
    value = str(raw or "").strip()
    return value if value.startswith(("http://", "https://")) else ""


def _published(raw) -> str:
    """配信日。**読めなければ持たない**(崩れた日付を素通ししない)。"""
    if not isinstance(raw, str) or not raw.strip():
        return ""
    try:
        value = datetime.fromisoformat(raw.strip())
    except ValueError:
        return ""
    return _iso(value if value.tzinfo else value.replace(tzinfo=UTC))


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
    (同じ名前だと切り替えが前世代を上書きして、戻り先が消える。実際に踏んだ)。
    """
    now = to_jst(datetime.now(UTC))
    stamp = now.strftime("%Y%m%d%H%M%S")
    current = sources.get(name)
    if current is not None and current.dump_date == stamp:
        stamp = (now + timedelta(seconds=1)).strftime("%Y%m%d%H%M%S")
    return stamp


def prompt_docs(item: Collection, previous, keys: list[str], focus=None) -> dict[str, dict]:
    """プロンプトへ差し込むぶんだけを取り出す。

    **区画で切ってあれば、その区画のぶんだけ**(数百件)。全部を持つと、区画で
    切った意味がメモリの側から消える —— 150 件見る回のために数十万件を読むことになる。
    区画を持たない収集(流れを追うもの)は、そもそも直近しか残らないので全部を持つ。

    **名指しされた見出しは、どの区画でも拾う**(割り込み)。区画の外に居ることが
    あるので、区画で絞ると名指しした 1 件が差し込みから消える。
    """
    if not callable(previous):
        return previous or {}
    named = set(focus.titles) if focus and focus.titles else set()
    if not (keys and item.partition):
        return {doc["title"]: doc for doc in previous()}
    spec = partitioning.normalize(item.partition)
    wanted = set(keys)
    find = partitioning.locator(spec, item.partitions)
    out: dict[str, dict] = {}
    for doc in previous():
        if doc["title"] in named:
            out[doc["title"]] = doc
            continue
        if find(doc) in wanted:
            out[doc["title"]] = doc
    return out


def _light_docs(previous) -> dict[str, dict]:
    """区画を割るためだけの、軽い写し(見出し・タグ・脇書き)。

    **本文を持たない。** 区画に要るのは座標か分類だけで、本文は 1 件の大半を
    占める —— 数十万件では、持つかどうかでメモリが桁で変わる。
    **消えたものは入れない**(`living` と同じ判断)。
    """
    out: dict[str, dict] = {}
    for doc in previous():
        if is_removed(doc):
            continue
        out[doc["title"]] = {
            "title": doc["title"],
            "tags": doc.get("tags") or [],
            "extra": doc.get("extra"),
        }
    return out


def stream_previous(name: str, sources: dict):
    """前世代を 1 行ずつ返す。`previous_docs` の流し込み版。

    **丸ごと持てないから分けてある。** 集める層は焼き直しのたびに前世代を全部
    舐めるので、dict に読むと行の数だけメモリが要る —— 50 万件の地図の名簿で
    1.8 GB(実測)。**`doc_id` の順**で返すので、焼いたあとの並びも前世代のまま。
    """
    src = sources.get(name)
    if src is None:
        return
    rows = db.stream(
        src.path,
        "SELECT doc_id, title, body, tags, updated_at, extra FROM docs ORDER BY doc_id",
    )
    for row in rows:
        yield slim_removed({
            "doc_id": row["doc_id"],
            "title": row["title"],
            # **冒頭は流さない**(本文の先頭の写しで、焼く側が作れる)
            "body": row["body"],
            "tags": load_tags(row["tags"]),
            "updated_at": row["updated_at"],
            "extra": load_json(row["extra"]),
        })


def _rows_of(previous):
    """前世代を**何度でも流せる形**にする。

    2 周するので、1 度きりの iterator は受け取れない —— dict をもらったら
    `doc_id` 順に並べて返し、呼べるものをもらったら呼ぶたびに新しく流させる。
    """
    if callable(previous):
        return previous
    rows = sorted((previous or {}).values(), key=lambda d: d["doc_id"])
    return lambda: iter(rows)


def bake_survey(item, sources: dict, previous, collected, only_new=False, edits=False) -> dict:
    """焼く前の 1 周目。**数えるだけで、何も持たない**。

    **流し始めたら断れない**(ステータスは 1 度しか送れない)。丸ごと組んでから
    測っていた頃はそれで良かったが、1 行ずつ流すならここで先に数えて、断るなら
    断ってから流し始める —— 前世代をもう 1 度読むことになるが、手元の SQLite を
    `doc_id` の索引順に舐めるだけなので安い。

    ついでに、**2 周目に要るものもここで用意する** —— 実在を確かめるタグの
    引き当て(`verified_docs` と同じ規則)と、区画ごとの件数と、見本の見出し。

    **大きさは絞り込む前の値で見る。** タグを落とすと縮むだけなので、天井の
    判断としては安全側に倒れる。
    """
    rows = _rows_of(previous)
    diff: dict = {}
    limit = _expiry_limit(item)
    rules = normalize_verify_tags(item.verify_tags)
    wanted: dict[int, set[str]] = {n: set() for n, _ in enumerate(rules)}
    spec = partitioning.normalize(item.partition) if item.partition else None
    # **索引は 1 回だけ組む**(`locator`)。ここも全件を舐めるところ
    find = partitioning.locator(spec, item.partitions) if spec and item.partitions else None
    counts: dict[str, int] = {}
    used = 0
    total = 0
    expired = 0
    first_title = None

    # **2 周目とまったく同じものを渡す。** ここだけ `Edits` に包んで渡していたが、
    # `_incoming_urls` は「見出しで引ける形」を抽出の名簿とみなして空を返すので、
    # **数える周だけ URL の重複判定が効かなかった** —— 同じ記事が別の見出しで
    # 流れてくるフィードでは、数える周が 1 件多く数え、その数が取り込み側の
    # 下限になる(`bake_lines` の meta)。**焼き上がった世代が「1 件足りない」で
    # 捨てられる**(本番で `validation failed: only 105 docs (< 106)`)
    for doc in stream_docs(item, rows(), collected, only_new, edits, diff):
        if limit is not None and _doc_time(doc) < limit:
            expired += 1
            continue
        total += 1
        if first_title is None:
            first_title = doc["title"]
        used += len(json.dumps(doc, ensure_ascii=False).encode()) + 1
        if used > MAX_MATERIAL_BYTES:
            raise HTTPException(409, {
                "error": f"収集「{item.name}」の素材が大きすぎます"
                         f"({total:,} 件目で"
                         f" {MAX_MATERIAL_BYTES / 1024 / 1024:.0f} MB を超えました)",
                "hint": "1 回に集める件数を減らすか、本文を短くしてください"
                        "(天井は CHIEZO_COLLECT_MAX_MATERIAL_BYTES で変えられます)。"
                        "焼いていないので、いまの内容はそのままです",
            })
        for n, rule in enumerate(rules):
            head = rule["prefix"] + ":"
            for tag in doc.get("tags") or []:
                if str(tag).startswith(head) and (found := _tag_head(tag, head)):
                    wanted[n].add(found)
        if find is not None and not is_removed(doc) and (key := find(doc)):
            counts[key] = counts.get(key, 0) + 1

    if not total:
        raise HTTPException(409, {
            "error": f"収集「{item.name}」は 1 件も集められませんでした",
            "hint": "プロンプトを見直すか、相手を替えてから試してください",
        })
    diff["expired"] = expired
    diff["partition_counts"] = counts
    # **前世代が読めなかったのか、本当に空なのかを分ける。** 読めないまま通すと、
    # その回の成果だけで新しい世代ができて**長期記憶が丸ごと入れ替わる** ——
    # 本番で 597,068 件の収集が 3 件の世代に差し替わった(`stream_previous` は
    # ソースを引けないと黙って 0 行を返す)。
    # **歯止めが 2 つとも効かない組み合わせ**だった: 取り込み側の `min_docs` は
    # 「これから流す行数」なので 3 行なら下限も 3 になり、`keep_ratio` のほうは
    # 前世代の件数を分母にするので 0 件では外れる。
    # **突き合わせる相手は長期記憶の件数**(`registry` が控えている `doc_count`)——
    # 台帳や控えの値と違って、いま配っているものそのものを数えた値。
    if not diff["previous"] and (baked := getattr(sources.get(item.name), "doc_count", 0)):
        raise HTTPException(409, {
            "error": f"収集「{item.name}」の前世代を読めませんでした"
                     f"(長期記憶には {baked:,} 件あるのに 0 件しか流れてきていません)",
            "hint": "世代の切り替え直後に起きます。焼いていないので、いまの内容は"
                    "そのままです —— 少し置いてからもう一度走らせてください",
        })
    if reason := shrink_blocked(item, diff, edits):
        raise HTTPException(409, {
            "error": f"収集「{item.name}」の整理を止めました: {reason}",
            "hint": "プロンプトを直すか、意図して減らすなら keep_ratio を下げてください"
                    "(0 で守りを外す)。焼いていないので、いまの内容はそのままです",
        })
    alive = {}
    for n, rule in enumerate(rules):
        src = sources.get(rule["source"])
        if src is not None and wanted[n]:
            alive[n] = _existing_titles(src.path, wanted[n])
    plan = {
        "diff": diff, "rules": rules, "alive": alive, "first_title": first_title,
        # **2 周目に流す行数**。取り込み側の検証の下限になる(`bake_lines` の meta)——
        # 流し始めたらステータスは変えられないので、途中で切れた素材と最後まで届いた
        # 素材は、受け取った側からは見分けが付かない。数だけが手掛かりになる。
        # 数えるのは期限で落としたあと(流すのもそのあと)
        "rows": total,
        # **期限の境目は 1 周目のものを使い回す。** 2 周目で測り直すと、境目の
        # 1 件が回を跨いだだけで数が食い違い、正しく焼けたものが弾かれる
        "limit": limit,
    }
    # **落としたタグの数も、流し始める前に数える。** 控えに残すのはここで record する
    # ためで、流しながら数えると「控えを書いたあとに分かる」ことになる。
    # **指定を持つ収集だけ**もう 1 周する(持たない収集では 1 件も落ちない)
    diff["tags_dropped"] = _count_dropped(item, plan, previous, collected, only_new, edits) \
        if alive else 0
    return plan


def _count_dropped(item, plan, previous, collected, only_new, edits) -> int:
    """実在しない見出しを指すタグを、いくつ落とすことになるか。"""
    rows = _rows_of(previous)
    limit = _expiry_limit(item)
    dropped = 0
    for doc in stream_docs(item, rows(), collected, only_new, edits):
        if limit is not None and _doc_time(doc) < limit:
            continue
        for n, rule in enumerate(plan["rules"]):
            if (known := plan["alive"].get(n)) is None:
                continue
            head = rule["prefix"] + ":"
            tags = doc.get("tags") or []
            dropped += sum(
                1 for tag in tags
                if str(tag).startswith(head) and _tag_head(tag, head) not in known
            )
    return dropped


def bake_lines(item, sources: dict, previous, collected, only_new=False, edits=False,
               survey: dict | None = None, sweep: str = "",
               unreviewed: bool = False, reviewed: set[str] | None = None,
               facts: bool = False):
    """焼く素材を 1 行ずつ返す。**2 周目**(数えるのは `bake_survey`)。

    **どの回が焼いたかはここで渡す。** 押すのは実際に焼かれる素材のほうで、
    数える 1 周目ではない(あちらは件数しか使わない)。
    """
    plan = survey or bake_survey(item, sources, previous, collected, only_new, edits)
    rows = _rows_of(previous)
    limit = plan.get("limit", _expiry_limit(item))

    yield json.dumps({
        "meta": {
            "dump_date": _dump_date(item.name, sources),
            # **冒頭の長さはこちらの決めごと。** 素材に乗せないので、焼く側が
            # 本文から作る —— 数字を 2 つのイメージに置くと、片方だけ動かした日に
            # 静かにずれる(読み口が返すのはそこなので、壊れ方が目に見えない)
            "opening_chars": notes.TITLE_MAX_CHARS * 4,
            # **これから流す行数をそのまま下限にする。** 流し始めたあとに落ちても
            # ステータスは変えられない(1 度しか送れない)ので、**途中で切れた素材は
            # 受け取る側から見ると「短いだけの正しい素材」**になる —— 焼けてしまうと
            # 前の世代は捨てられ、届かなかったぶんは消える。
            # 本番でこれが起きた(68 万件のうち 1.7 万件で焼き上がり、残りが消えた)。
            # 数が足りなければ取り込み側が検証で落とし、前の世代がそのまま残る
            "min_docs": max(1, int(plan.get("rows") or 1)),
            "sample_titles": [plan["first_title"]],
        }
    }, ensure_ascii=False)

    for doc in stream_docs(
        item, rows(), collected, only_new, edits, None, sweep, unreviewed, reviewed, facts,
    ):
        if limit is not None and _doc_time(doc) < limit:
            continue
        for n, rule in enumerate(plan["rules"]):
            if (known := plan["alive"].get(n)) is None:
                continue
            head = rule["prefix"] + ":"
            tags = doc.get("tags") or []
            keep = [
                tag for tag in tags
                if not str(tag).startswith(head) or _tag_head(tag, head) in known
            ]
            if len(keep) != len(tags):
                doc = {**doc, "tags": keep}
        yield json.dumps(doc, ensure_ascii=False)


def _expiry_limit(item: Collection) -> str | None:
    """期限で落とす境目。**流れの収集だけ**(網羅では穴が開く)。"""
    if item.kind != KIND_FLOW or item.keep_days <= 0:
        return None
    return _iso(_now() - timedelta(days=item.keep_days))


def ndjson(
    item: Collection,
    sources: dict,
    previous: dict[str, dict],
    collected: list[dict],
    only_new: bool = False,
    edits: bool = False,
) -> tuple[str, dict]:
    """取り込み側が読む素材(1 行目が meta、以降は 1 行 1 文書)と、前世代との差分。

    **丸ごと 1 本の文字列にする形**。本番はこれを使わず 1 行ずつ流す
    (`bake_lines`)—— 数十万件の収集では、繋いだだけで数百 MB になる。
    ここに残してあるのは、**一度に見たい側**(テストと下見)のため。

    断る条件は `bake_survey` が持つ(空・減りすぎ・大きすぎ)。**流し始める前に
    数える**ので、どちらの道でも同じところで同じ理由で止まる。
    """
    plan = bake_survey(item, sources, previous, collected, only_new, edits)
    lines = list(bake_lines(item, sources, previous, collected, only_new, edits, plan))
    return "\n".join(lines) + "\n", plan["diff"]
