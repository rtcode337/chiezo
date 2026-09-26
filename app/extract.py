"""抽出の指定 —— 集める層の最初の 1 回を、AI ではなく決まった手順で埋める。

**Chiezo はここで何を集めているかを知らない。** 「どのソースの・どのタグを・何件・
タグをどう読み替えるか」は依頼する側(外のアプリ)が書いた指定として渡ってきて、
こちらはそれを機械的に実行するだけ。画家も作曲家も、この層から見れば同じ形をしている。

**AI が間違えるところと、間違えないところを分ける**のが狙い。名前・年代・出典の
ような「既に手元の長期記憶に載っている事実」は引いてくれば済むのに、AI に書かせると
存在しない人物や合わない年代が混ざる。逆に、影響関係や代表作のような**書いていない
ことは引けない**ので、そちらは AI に任せる(この層は触らない)。

**指定は 1 度書けば残る。** 依頼文から指定を組み立てるのに AI を使っても、その後は
指定のほうを回すので毎回結果が変わらない。決定的なのは指定であって AI ではない。

指定の形(すべての鍵は省略可、`source` と `tag` だけ必須):

    {
      "source": "jawiki",
      "tag": "ロマン派の作曲家",
      "limit": 30,   ← 書かなければ全部。AI に読ませる側の都合で絞るときだけ書く
      "body": "opening",
      "url": "https://ja.wikipedia.org/wiki/{title}",
      "extra": ["pageviews_month"],   ← 元の記事の extra から、この鍵だけ写す
      "tags": [
        {"const": "作曲家"},
        {"patterns": ["^(\\\\d{3,4})年生$", "^(\\\\d{3,4})年没$"], "format": "年代:{1}-{2}"},
        {"pattern": "^(.+)出身の人物$", "format": "出身:{1}"}
      ]
    }

**指定は配列でも書ける**(ソースをまたいで 1 つの名簿にする)。同じ見出しが
複数のソースに居たときは、**先に書いたほうが勝つ**——後ろのソースは、前が
埋めなかったところだけを埋める。

    "extract": [
      {"source": "jawiki",         "tag": "東京都の飲食店", "extra": []},
      {"source": "osm_japan",      "tag": "amenity=restaurant", "extra": ["lat", "lon"]},
      {"source": "overture_japan", "tag": "restaurant", "extra": ["lat", "lon", "website"]}
    ]

**項目ごとの優先は `extra` の書き分けで表す。** 上の例なら、説明は先頭の Wikipedia が
勝ち、座標は「Wikipedia が持ち込まない」ので次の OSM が勝つ。項目ごとの順位を別に
書けるようにはしない —— 同じことを 2 通りで書けるだけになり、どちらが効くのかを
読む人が指定から判断できなくなる。

**並び順は指定できない。** 引く先のソースが持っている順(`rank_score` の降順 ——
Wikipedia ならページビュー、地名なら人口)をそのまま使う。「有名なほうから N 件」は
ここで既に満たされているので、指定側で並べ替えを書けるようにすると、同じことを
2 通りで書けるだけになる。

**順の無いソースに `limit` を書くと、切り口は見出し順になる。** 同じ種別の地物に
同じ点しか付かないソースがあり(OSM の飲食店はどれも 0.4)、そこでは
`rank_score` が並びを決めないので、**残るのは名前が先に来るものだけ**になる ——
数字と英字の名前がまず入り、仮名の途中で切れる。**それを黙ってやらない**のが
この層の流儀なので、そういうソースからは切らずに全部取る(取れない量なら断る)。
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import suppress

from fastapi import HTTPException

from app import notes

log = logging.getLogger("chiezo.app")

# **件数は既定では絞らない。** 引くのは手元の索引を 1 本引くだけで、AI の枠も時間も
# 使わない —— 30 件に絞る理由は取る側には無い(絞りたいのは AI に読ませる側)。
#
# 上限は「当たりすぎ」を止めるためだけに置く。**黙って切らずに断る** ——
# 切ったことは返り値から分からないので、絞ったつもりのない指定が
# 「そこまでしか無い」ように見えてしまう(実際に 30 件で止まっているのを、
# 機械抽出の限界だと受け取られた)。
# **本当の天井は素材の大きさのほう**(`collect.MAX_MATERIAL_BYTES`)。ここは
# 「当たりすぎ」を止めるためだけの数で、**大きさの見張りではない** ——
# 1 件のバイト数は運ぶ項目で倍以上違い(見出しと本文だけの名簿で 256 バイト、
# 住所・電話・サイト・座標まで運ぶ地図の名簿で 474 バイト。どちらも実測)、
# 件数から素材の大きさは決められない。低く置くと、**順の無いソースが見出し順で
# 切られる**(`limit` を書かせると黙って切れるので、切らずに済む高さにしておく)。
#
# **網羅を頼まれる層なので、地図辞典 1 国ぶんが丸ごと入る高さに置く** ——
# ここで切ると「全部を集めて精査する」という収集の種類そのものが嘘になる。
# **1 国ぶんは思うより大きい。** タグの索引には親のカテゴリも入っているので、
# 地図辞典の飲食店はタグ 1 つで 41.9 万件、ジャンルのタグを並べると 60.7 万件に
# なる(実測)。50 万件で置いていた頃は、ちょうどこれが入らなかった
MAX_ROWS = 1_000_000

# 1 つの収集に書ける抽出の本数。**ソースをまたいで名簿を作るため**のもの。
# 優先は書いた順なので、本数が増えるほど「どの本が勝つか」を読むのが難しくなる ——
# 上限はそこで置いている(大きさを見張るのは素材の側。`collect.MAX_MATERIAL_BYTES`)
MAX_SPECS = 5

# 名簿を引くときの持ち時間。**読み口の 5 秒(`db.QUERY_TIMEOUT_SECONDS`)には収まらない**
# —— あれは人が待っている問い合わせを守るための数で、ここは取り込みの中で動く
# 背景の仕事(誰も応答を待っていない)。地図の名簿のように数十万件に当たる指定は、
# 並べ替えだけで数秒かかる —— 5 秒で打ち切ると **QueryTimeout だけが控えに残り、
# どの指定のどこが重かったのかは誰にも分からない**(実際にそうなった)
EXTRACT_TIMEOUT_SECONDS = 120.0

# その本が「勝ちにいく」と書ける項目(`provides`)。書かなければ全部を取りにいく。
# **タグはここに入れない** —— タグは競争ではなく足し算で、どの本のタグも残る
PROVIDED_FIELDS = ("body", "url", "extra")
# 末尾一致で広げられるタグの数。**黙って切らずに断る**(このファイルの流儀)。
# SQLite の変数の上限(既定 32,766)には遠いが、ここまで来たら指定のほうが広すぎる。
# 実測: 日本語版 Wikipedia の「〜の画家」で 323 件
MAX_SUFFIX_TAGS = 900
# 末尾一致に要る長さ。短い語は何にでも当たる(「家」だけで数万のカテゴリが並ぶ)
MIN_SUFFIX_CHARS = 3
# タグの読み替えの上限。指定が肥大すると、1 件あたりの正規表現の回数がそのまま伸びる
MAX_RULES = 20
# 元の記事の `extra` から写せる鍵の数。**写すのは事実だけ** —— 知名度や座標のような、
# 既に長期記憶に載っていて AI に書かせる意味の無い値を運ぶための口。
# 際限なく写せるようにすると、集めた 1 件が元の記事の丸写しになる
MAX_CARRIED_KEYS = 10
MAX_PATTERNS_PER_RULE = 4
MAX_PATTERN_CHARS = 200
# 1 件から作るタグの上限(読み替えが総当たりで当たったときの歯止め)
MAX_TAGS_PER_DOC = 30
# 別の文書を引いてくる読み替えで、1 件から取れる数の上限
MAX_TAKE = 5
# つながりの見方。**相互は片側よりずっと強い** —— 有名なものどうしは互いに名前が
# 出てくるので、片側だと 12% が繋がって毛玉になった。相互は 7% まで落ちたうえ、
# 残った組は実際に関係のある相手だった(人物 30 件で実測)
LINK_KINDS = ("mutual", "out")
# 本文に使える欄。**長い本文は取らない** —— 焼くのは要点で、全文は元のソースにある
BODY_FIELDS = ("opening", "body")
DEFAULT_BODY_FIELD = "opening"
# 抽出し終えた印。次の実行は「進み具合が入っている」ほうへ進む(= AI が肉付けする)
DEFAULT_CURSOR = "抽出済み"

# 何を名簿にするか。**文書か、タグか。**
#
# `docs` は「そのタグが付いた文書を 1 件ずつ」、`tags` は「そのソースのタグを 1 語ずつ」。
# 後者は**溜めたものの索引を作る**ための口で、集めた記事から話題の語を起こすような
# 収集がこれに当たる —— 語を AI に思いつかせると、記事に出てこない語が混ざるうえ、
# 同じ語が回ごとに違う表記で増える。**語そのものは機械で拾えるのだから拾う**。
ROSTER_KINDS = ("docs", "tags")
DEFAULT_ROSTER_KIND = "docs"

# タグの名簿で「いま動いているか」を見る窓(日)。**時間軸はここに入る** ——
# 件数だけだと、昔よく出てきた語がいつまでも大きいままになる
DEFAULT_RECENT_DAYS = 7
MAX_RECENT_DAYS = 365

# 1 語につき運ぶ「一緒に出てくる語」の数。**多いと図が毛玉になる**(人物の名簿で
# 実測した相互リンクと同じ話で、上位だけ残すと実際に関係のある相手が残る)
DEFAULT_LINK_COUNT = 6
MAX_LINK_COUNT = 20

# 外す語を書ける数。**書くのは頼む側** —— 配信元の名前も種別の語も、決めているのは
# 頼む側のアプリで、Chiezo には「どれが分野で、どれが媒体名か」を知る手立てが無い
MAX_SKIP_RULES = 30
# 頼んだ件数のこれを下回ったときだけ、実在するタグを見せて選び直させる。
# **少し足りないだけで投げ直さない** —— 件数を満たそうとして条件のほうが広がる
# (「西洋近代絵画」と頼んだのに 14 世紀からの欧州全体になった)
RETRY_BELOW = 0.5
# 件数を書いていないときに「痩せている」と見なす数。**0 件だけが失敗ではない** ——
# 広い語のタグは実在しても数件しか付いておらず、欲しいものは時代や地域で
# 絞った名前の側にあることが多い(広いほうを書かれて 7 件しか取れなかった)
THIN_ROWS = 10


def looks_thin(spec, got: int) -> bool:
    """取れた数が、頼んだものに対して少なすぎないか。

    件数を書いていれば、その数に対して。書いていなければ、絶対数で見る
    (書いていないときは「全部でこれだけ」なので、少ないのは選んだタグのせい)。

    **1 本でも件数を書いていなければ、絶対数で見る** —— 書いていない本は
    「全部」を頼んでいるので、頼んだ数を足し合わせようがない。
    """
    written = specs(spec)
    limits = [one["limit"] for one in written]
    if written and all(limits):
        return got < sum(limits) * RETRY_BELOW
    return got < THIN_ROWS


def normalize(raw) -> dict | list[dict] | None:
    """指定を確かめて、実行できる形に整える。空なら None(抽出は使わない)。

    **書いた形のまま返す** —— 1 本なら 1 つ、配列なら配列。畳んで返すと、
    定義に控えたものが書いた人の書いたものと違う形になる(送り直すたびに揺れる)。
    束ねて扱いたいところは `specs/1` を通す。

    **壊れた指定は作る時点で断る**。実行時に落ちると、無人で回っている最中に
    「集められなかった」だけが残り、どこが悪いのかは誰も見ていない。
    """
    if raw in (None, "", {}, []):
        return None
    if isinstance(raw, list):
        if len(raw) > MAX_SPECS:
            raise _bad(f"抽出の指定は {MAX_SPECS} 本までです")
        written = [normalize_one(one) for one in raw]
        if any(one is None for one in written):
            raise _bad("空の抽出の指定が混ざっています")
        _reject_same_source(written)
        return written
    return normalize_one(raw)


def specs(written) -> list[dict]:
    """束ねて扱うための形。**書いた順がそのまま優先の順**。"""
    if written is None:
        return []
    return written if isinstance(written, list) else [written]


def _reject_same_source(written: list[dict]) -> None:
    """同じソースを 2 度書かせない。

    先に書いたほうが勝つ規則なので、2 本目は「1 本目が埋めなかったところ」しか
    埋められない —— タグ違いで 2 度引きたいなら `tag` にカンマで書き並べれば済む。
    **書けるが効かない指定**を残すと、効いていないことに気づけない。
    """
    seen = set()
    for one in written:
        if one["source"] in seen:
            raise _bad(f"同じソース「{one['source']}」を 2 度書いています")
        seen.add(one["source"])


def normalize_one(raw) -> dict | None:
    """1 本ぶんの指定を整える。"""
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("抽出の指定はオブジェクトで書いてください")

    source = str(raw.get("source") or "").strip()
    tag = str(raw.get("tag") or "").strip()
    tag_suffix = str(raw.get("tag_suffix") or "").strip()
    not_tag = str(raw.get("not_tag") or "").strip()
    of = str(raw.get("of") or DEFAULT_ROSTER_KIND).strip()
    if not source:
        raise _bad("source(引くソース名)を入れてください")
    if of not in ROSTER_KINDS:
        raise _bad(f"of は {' / '.join(ROSTER_KINDS)} のどちらかにしてください")
    # **タグの名簿には「どのタグを拾うか」を書かない。** 拾うのはそのソースのタグ
    # 全部で、絞るのは「外す語」と「何件以上付いているか」のほう
    if of == DEFAULT_ROSTER_KIND and not tag and not tag_suffix:
        raise _bad("tag(絞り込むタグ)か tag_suffix(タグの末尾)を入れてください")
    # **末尾は書き並べられる**(`tag` と同じくカンマ区切り)。同じものを指す
    # カテゴリの呼び方が 1 つとは限らない —— 画家の名簿では「〜の画家」だけを
    # 書いていたせいで「〜の女性画家」が丸ごと落ちていた(実測で 44 カテゴリ・
    # 1,877 記事。草間彌生もそこにいた)
    for one in split_tags(tag_suffix):
        if len(one) < MIN_SUFFIX_CHARS:
            raise _bad(
                f"tag_suffix は {MIN_SUFFIX_CHARS} 文字以上にしてください"
                "(短い語は何にでも当たります)"
            )

    # **書かなければ全部**。書いたときだけ、その数で切る
    limit = raw.get("limit")
    if limit in (None, "", 0):
        limit = None
    else:
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            raise _bad("limit は数で書いてください") from None
        limit = min(max(limit, 1), MAX_ROWS)

    body_field = str(raw.get("body") or DEFAULT_BODY_FIELD).strip()
    if body_field not in BODY_FIELDS:
        raise _bad(f"body は {' / '.join(BODY_FIELDS)} のどれかにしてください")

    rules = raw.get("tags") or []
    if not isinstance(rules, list):
        raise _bad("tags は配列で書いてください")
    if len(rules) > MAX_RULES:
        raise _bad(f"tags の読み替えは {MAX_RULES} 個までです")

    provided = raw.get("provides")
    if provided is None:
        provided = list(PROVIDED_FIELDS)
    if not isinstance(provided, list):
        raise _bad("provides は項目名の配列で書いてください")
    provided = [str(f).strip() for f in provided if str(f).strip()]
    if unknown := [f for f in provided if f not in PROVIDED_FIELDS]:
        raise _bad(f"provides に書けるのは {' / '.join(PROVIDED_FIELDS)} です: {unknown[0]}")

    carried = raw.get("extra") or []
    if not isinstance(carried, list):
        raise _bad("extra は写したい鍵の配列で書いてください")
    if len(carried) > MAX_CARRIED_KEYS:
        raise _bad(f"extra に書ける鍵は {MAX_CARRIED_KEYS} 個までです")
    carried = [str(k).strip() for k in carried if str(k).strip()]

    spec = {
        "source": source,
        "of": of,
        **(_tags_mode(raw) if of == "tags" else {}),
        "tag": tag,
        # **カテゴリの「族」をそのまま指せるようにする。** 1 つずつ書き並べる形だと、
        # 書く側が名前を思い出しで補うことになり、抜けても気づけない —— 実際、
        # 画家の一覧で「アメリカ合衆国」が丸ごと落ち、「イギリス」と書いたせいで
        # 中身の大半がある「イングランド」が取れていなかった。末尾で指せば
        # **推測が要らず、あとからカテゴリが増えても勝手に入る**
        "tag_suffix": tag_suffix,
        "not_tag": not_tag,
        "limit": limit,
        "body": body_field,
        "url": str(raw.get("url") or "").strip(),
        # **この本が勝ちにいく項目。** 書いてない項目でも、どの本も埋めなかった
        # ところは埋める(順位を譲るだけで、穴を空けたままにはしない)
        "provides": tuple(provided),
        "tags": [_normalize_rule(rule) for rule in rules],
        # **元の記事に載っている事実を、そのまま運ぶ。** 知名度(月次ページビュー)の
        # ような値は既に長期記憶にあるので、読む側が 1 件ずつ引き直す理由が無い
        "extra": carried,
        "cursor": str(raw.get("cursor") or DEFAULT_CURSOR).strip() or DEFAULT_CURSOR,
    }
    return spec


def _tags_mode(raw) -> dict:
    """タグの名簿だけが持つ指定を整える。

    **外す語は頼む側が書く。** 配信元の名前も種別の語も、決めているのは頼む側の
    アプリで、Chiezo には「どれが分野で、どれが媒体名か」を知る手立てが無い ——
    推測させると、増えた配信元が黙って話題として並ぶ。
    """
    skip = raw.get("skip") or []
    if not isinstance(skip, list):
        raise _bad("skip は外す語の並び(正規表現)で書いてください")
    if len(skip) > MAX_SKIP_RULES:
        raise _bad(f"skip に書けるのは {MAX_SKIP_RULES} 個までです")
    return {
        "skip": [_compile(str(one)).pattern for one in skip if str(one).strip()],
        # **何件以上付いている語を拾うか。** 1 件しか付いていない語は、まだ話題か
        # どうかも分からない(次の回に増えていれば入る)
        "min_docs": _counted(raw.get("min_docs"), 1, 1),
        "recent_days": _counted(raw.get("recent_days"), DEFAULT_RECENT_DAYS, 1,
                                MAX_RECENT_DAYS),
        "links": _counted(raw.get("links"), DEFAULT_LINK_COUNT, 0, MAX_LINK_COUNT),
    }


def _counted(raw, fallback: int, low: int, high: int | None = None) -> int:
    """数の指定。**読めない値は断る** —— 指定は書いて残すものなので、黙って既定に
    落とすと、書いたつもりの値が効かないまま回り続ける。"""
    if raw in (None, ""):
        return fallback
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise _bad(f"数で書いてください: {raw!r}") from None
    value = max(value, low)
    return min(value, high) if high is not None else value


def _normalize_rule(raw) -> dict:
    """タグの読み替え 1 つぶん。

    3 通りある:
    - `const` —— 固まった 1 つ。「これは何の一覧か」を見分ける目印を付ける
    - `pattern` —— **当たったタグごとに 1 つ**。1 件に複数付きうるもの向け
    - `patterns` —— **全部当たったときだけ 1 つ**。生年と没年から年代を作るような、
      2 つ以上のタグを 1 つにまとめる読み方
    """
    if not isinstance(raw, dict):
        raise _bad("tags の要素はオブジェクトで書いてください")

    if const := str(raw.get("const") or "").strip():
        return {"kind": "const", "value": const}

    fmt = str(raw.get("format") or "").strip()
    if not fmt:
        raise _bad("読み替えには format(作るタグの形)が要ります")

    # 別の文書を引いてくる読み替え。**この文書のタグではなく、他の文書の見出し**を
    # 入れる(「〈その見出し〉の作品」を引いて代表作を出す、のような)
    if from_tag := str(raw.get("from_tag") or "").strip():
        try:
            take = min(max(int(raw.get("take") or 1), 1), MAX_TAKE)
        except (TypeError, ValueError):
            raise _bad("take は数で書いてください") from None
        return {"kind": "from_tag", "tag": from_tag, "format": fmt, "take": take}

    # 記事どうしのつながり。**この抽出に入っているものだけ**を相手にする
    if linked := str(raw.get("linked") or "").strip():
        if linked not in LINK_KINDS:
            raise _bad(f"linked は {' / '.join(LINK_KINDS)} のどれかにしてください")
        return {"kind": "linked", "how": linked, "format": fmt}

    patterns = raw.get("patterns")
    if patterns is None:
        single = str(raw.get("pattern") or "").strip()
        if not single:
            raise _bad("読み替えには pattern か patterns が要ります")
        return {"kind": "each", "patterns": [_compile(single)], "format": fmt,
                "fallback": _fallback_of(raw)}

    if not isinstance(patterns, list) or not patterns:
        raise _bad("patterns は 1 つ以上の配列で書いてください")
    if len(patterns) > MAX_PATTERNS_PER_RULE:
        raise _bad(f"patterns は {MAX_PATTERNS_PER_RULE} 個までです")
    return {
        "kind": "all",
        "patterns": [_compile(str(p or "")) for p in patterns],
        "format": fmt,
        "fallback": _fallback_of(raw),
    }


def _fallback_of(raw: dict) -> bool:
    """`fallback` の読み方。**前の規則が同じ接頭辞のタグを作っていれば、この規則は作らない**。

    同じ軸を何通りかの手掛かりから作るときに要る —— 生年と没年の両方 → 生年だけ →
    世紀だけ、のように手掛かりの確かな順に並べると、並べただけでは**当たった全部が
    足される**(両方を持つ人に年代のタグが 3 本付く)。読む側は最初の 1 本しか見ないので、
    残りは食い違いの種になるだけ。**確かな手掛かりが当たったら、そこで止める**。
    """
    value = raw.get("fallback")
    if value is None or value is False:
        return False
    if value is True:
        return True
    raise _bad("fallback は true か false で書いてください")


def _prefix_of(fmt: str) -> str:
    """作るタグの接頭辞(`年代:{1}-{2}` → `年代:`)。**`:` が無ければ全体**を比べる。"""
    head, sep, _rest = fmt.partition(":")
    return head + sep if sep else fmt


def _compile(pattern: str) -> re.Pattern:
    if not pattern:
        raise _bad("pattern が空です")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise _bad(f"pattern は {MAX_PATTERN_CHARS} 文字までです")
    try:
        return re.compile(pattern)
    except re.error as e:
        raise _bad(f"pattern が正規表現として読めません: {e}") from None


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": message})


def resolve_tags(spec: dict, src) -> list[str]:
    """指定に当たるタグ名を実体で返す。

    **末尾一致は `tag_counts` で展開する。** あれはタグ名 → 文書数の集計表(重複を
    畳んだ 29 万行)なので、転置表(764 万行)を舐めずに済む。展開してから普段どおり
    `IN (...)` で引くので、doc_tags 側は今までと同じ索引の使い方になる。

    **当たりすぎたら黙って切らずに断る**(このファイルの流儀)。切ると、広すぎる
    指定が「そこまでしか無い」ように見える。
    """
    from app import db

    tags = split_tags(spec["tag"])
    for suffix in split_tags(spec["tag_suffix"]):
        # LIKE のメタ文字は素通しにしない(`_` は 1 文字に当たる)
        pattern = "%" + suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = db.query(
            src.path,
            "SELECT tag FROM tag_counts WHERE tag LIKE ? ESCAPE '\\'"
            " ORDER BY docs DESC, tag LIMIT ?",
            (pattern, MAX_SUFFIX_TAGS + 1),
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
        found = [row["tag"] for row in rows]
        if len(found) > MAX_SUFFIX_TAGS:
            raise HTTPException(409, {
                "error": f"tag_suffix「{suffix}」は {MAX_SUFFIX_TAGS:,} 件を超えるタグに当たります",
                "hint": "もっと長い末尾にするか、tag に書き並べてください",
            })
        seen = set(tags)
        tags += [t for t in found if t not in seen]
    return tags


def to_json(spec) -> dict | list[dict] | None:
    """定義に持たせる形(正規表現は書いた文字列のまま残す)。

    **書いた形のまま返す** —— 1 本なら 1 つ、配列なら配列。
    """
    if not spec:
        return None
    if isinstance(spec, list):
        return [to_json(one) for one in spec]
    rules = []
    for rule in spec["tags"]:
        if rule["kind"] == "const":
            rules.append({"const": rule["value"]})
        elif rule["kind"] == "from_tag":
            rules.append({
                "from_tag": rule["tag"], "format": rule["format"], "take": rule["take"]
            })
        elif rule["kind"] == "linked":
            rules.append({"linked": rule["how"], "format": rule["format"]})
        elif rule["kind"] == "each":
            rules.append({"pattern": rule["patterns"][0].pattern, "format": rule["format"],
                          **({"fallback": True} if rule.get("fallback") else {})})
        else:
            rules.append({
                "patterns": [p.pattern for p in rule["patterns"]],
                "format": rule["format"],
                **({"fallback": True} if rule.get("fallback") else {}),
            })
    written = {
        "source": spec["source"],
        "tag": spec["tag"],
        "tag_suffix": spec["tag_suffix"],
        "not_tag": spec["not_tag"],
        "limit": spec["limit"],
        "body": spec["body"],
        "url": spec["url"],
        "tags": rules,
        "extra": spec["extra"],
        "cursor": spec["cursor"],
    }
    # **既定のままなら書かない。** 1 本しか書いていない指定に順位の話は無いので、
    # 控えに出すと「これは何を譲っているのか」を毎回読ませることになる
    # (`app/partition.py` の `other` と同じ判断)
    if spec["provides"] != PROVIDED_FIELDS:
        written["provides"] = list(spec["provides"])
    # **タグの名簿だけが持つものは、そのときだけ書く。** 文書の名簿に `min_docs` が
    # 並んでいると、効かない指定を読ませることになる
    if spec["of"] != DEFAULT_ROSTER_KIND:
        written["of"] = spec["of"]
        written["skip"] = list(spec["skip"])
        written["min_docs"] = spec["min_docs"]
        written["recent_days"] = spec["recent_days"]
        written["links"] = spec["links"]
    return written


def _doc_ids(spec: dict, sources: dict):
    """指定に当たる doc_id を返す SELECT を組む。"""
    from app.main import build_doc_id_set

    src = sources.get(spec["source"])
    if src is None:
        raise HTTPException(404, {
            "error": f"抽出できません: ソース「{spec['source']}」がありません",
            "hint": "まだ焼いていないか、名前が違う(/v1/sources で確かめられる)",
        })

    tags = resolve_tags(spec, src)
    if not tags:
        raise HTTPException(409, {
            "error": "この指定に当たるタグが 1 つもありません",
            "hint": f"tag_suffix「{spec['tag_suffix']}」で終わるタグが"
                    f"ソース「{spec['source']}」にない。/v1/<source>/tags で確かめられる",
        })
    id_set = build_doc_id_set(src, tags=tags)
    if id_set is None:
        raise HTTPException(409, {
            "error": f"ソース「{spec['source']}」はタグで絞り込めません",
            "hint": "タグの転置表が入る前のスキーマで焼かれている。取り込み直すと使える",
        })
    set_sql, params = id_set
    # 外したいタグは EXCEPT で引く。**カテゴリは持ち主の職業を選ばない** ——
    # 仕事で括ったカテゴリにも、それで知られていない人が入っている。人気の順に
    # 取ると本業の人より先にそちらが並ぶ(上位 30 件のうち 3 件がそうだった)
    if excluded := split_tags(spec["not_tag"]):
        set_sql += (
            f" EXCEPT SELECT doc_id FROM doc_tags WHERE tag IN ({','.join('?' * len(excluded))})"
        )
        params = [*params, *excluded]
    return src, set_sql, list(params)


def count(spec, sources: dict, retired: set[str] | None = None) -> int:
    """指定が当たる件数。**取れた数ではなく、当たっている数**。

    取った数だけを見せると、絞られていることに気づけない。

    **何本あっても合計で返す。** 同じ見出しが複数のソースに居れば畳まれるので、
    実際に溜まる数はこれより少ない —— それでも「当たっている数」としては足した数が
    正しい(どれか 1 本の数を見せると、他の本が当たっていないように見える)。
    """
    from app import db

    total = 0
    for one in specs(spec):
        if one["of"] == "tags":
            total += len(_tag_rows(one, sources, retired))
            continue
        src, set_sql, params = _doc_ids(one, sources)
        (matched,) = db.query(
            src.path,
            f"SELECT COUNT(*) FROM ({set_sql})",
            tuple(params),
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )[0]
        total += matched
    return total


def run(spec, sources: dict, retired: set[str] | None = None) -> tuple[list[dict], str]:
    """指定どおりに引いて、集める層が読む形(items)にして返す。

    `retired` は**その収集で既に外された見出し**(墓標の付いたもの)。渡すと、
    タグの名簿はその語を拾い直さない —— 墓標は「これは話題ではない」という判断なので、
    拾う側が知らないと毎回同じ語を並べ直すことになる。

    返す形は AI に書かせたときとまったく同じ(`title` / `body` / `tags` / `url`)。
    後ろの工程から見れば、誰が作ったものかは区別が付かない。

    **何本書いてあっても 1 つの名簿にして返す。** 同じ見出しが複数のソースに居たら
    畳む。どの本の値を採るかは**項目ごと**に決まる ——

    1. まず、その項目を**勝ちにいくと書いた本**(`provides`)のうち、いちばん先に
       書いてあるものが入る
    2. それでも空いたところは、**書いた順にどの本からでも**埋める

    2 周目が要るのは、**順位を譲ることと、穴を空けたままにすることは別**だから。
    百科事典は座標で勝たせたくないが、地図に載っていない 1 軒の座標は百科事典に
    しか無い —— 譲った本の値を捨てると、その 1 軒はどの区画にも入らず、
    AI から永遠に見えなくなる(区画は座標で決まる)。

    **タグは競争ではなく足し算**なので `provides` に入れない。どの本のタグも残り、
    **前のものを先頭に残したまま**足す —— 読む側は先頭のタグを代表として使うので、
    後ろの本が並びを変えると意味が変わる。

    **進み具合は先頭の本のものを返す。** 本ごとに別々に持たせても、収集が持てる進み
    具合は 1 つしかない —— どれかを選ぶなら、優先の先頭にするのがいちばん読める。
    """
    written = specs(spec)
    cursor = DEFAULT_CURSOR
    roster = Roster()
    try:
        # 1 周目。**その項目を勝ちにいくと書いた本だけ**が、先に書いた順で入る
        for index, one in enumerate(written):
            items, cursor_of = _timed_run(one, sources, retired)
            if index == 0:
                cursor = cursor_of
            for item in items:
                roster.merge(item, one["provides"])
        # 2 周目。**譲った本の値でも、空いているところは埋める**
        for one in written:
            items, _cursor = _timed_run(one, sources, retired)
            for item in items:
                roster.merge(item, PROVIDED_FIELDS)
    except BaseException:
        roster.close()
        raise
    return roster, cursor


def _timed_run(spec: dict, sources: dict, retired: set[str] | None = None) -> tuple[list[dict], str]:
    """1 本ぶんを引いて、かかった時間を控える。

    **どの本で詰まったかを言える形にする。** 打ち切りがそのまま上がると控えに
    残るのは「QueryTimeout」の一語だけで、**何本も書いてある指定のどれが重かったのかを
    後から辿れない**(無人で回る層なので、そのとき見ている人はいない)。
    """
    from app import db

    started = time.monotonic()

    def refused():
        return HTTPException(504, {
            "error": f"ソース「{spec['source']}」の抽出が"
                     f"{EXTRACT_TIMEOUT_SECONDS:.0f} 秒で終わりませんでした",
            "hint": "tag を絞るか limit に取る件数を書いてください"
                    "(当たっている件数は収集の画面で確かめられます)",
        })

    try:
        items, cursor = _run_one(spec, sources, retired)
    except db.QueryTimeout:
        raise refused() from None

    def guarded():
        # **打ち切りは回している最中に来る。** 1 行ずつ返すようになったので、
        # 呼んだ瞬間ではなく、読み進めたところで切れる
        seen = 0
        try:
            for item in items:
                seen += 1
                yield item
        except db.QueryTimeout:
            raise refused() from None
        log.info(
            "extract %s: %d items in %.1fs", spec["source"], seen, time.monotonic() - started
        )

    return guarded(), cursor


class Roster:
    """引いてきた名簿の置き場。**一時の SQLite に書く**。

    dict で持っていた頃は、引いた件数ぶんのメモリが要った —— 日本の飲食店を
    3 つの辞典から引くと 68 万件で、2 GB を超える(実測)。焼く側も 1 行ずつ
    受け取るようになったので、ここも持たずに渡せる形にする。

    **畳むのに見出しで引く必要がある**(同じ店が複数の辞典に居る)ので、ただの
    ファイルではなく索引の要る置き場になる。SQLite ならその索引がただで付く。

    **焼く層が読む約束**(`take` / `rest` / `reset` / `count`)に合わせてある ——
    向こうは「前世代を流しながら、その見出しに来ている直しを引く」形で回る。
    """

    def __init__(self) -> None:
        handle, self.path = tempfile.mkstemp(prefix="chiezo-roster-", suffix=".db")
        os.close(handle)
        # **スレッドを跨いで読む。** 引くのは別スレッド(`asyncio.to_thread`)で、
        # 読むのは流し込みの最中 —— 触るのは一度に 1 つなので、見張りを外してよい
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        # **同期を切る。** 一時の置き場なので、落ちたら作り直せばよい
        self.conn.executescript(
            "PRAGMA journal_mode=OFF; PRAGMA synchronous=OFF;"
            " CREATE TABLE items ("
            "  title TEXT PRIMARY KEY, seq INTEGER, body TEXT, url TEXT,"
            "  tags TEXT, extra TEXT, used INTEGER NOT NULL DEFAULT 0);"
            " CREATE INDEX idx_items_rest ON items (used, seq);"
        )
        self._seq = 0

    def merge(self, item: dict, claims) -> None:
        """1 件を名簿へ入れる。**既に入っている値は上書きしない**。

        **鍵にするのは切り詰めた見出し**(`notes.TITLE_MAX_CHARS`)。焼く側は
        見出しを切ってから書き、長期記憶は見出しに一意の索引を張る —— ここで
        生のまま鍵にすると、**先頭 60 字が同じ 2 件が別物として通り、焼く段で
        索引が張れずに取り込みがまるごと落ちる**(世代は切り替わらないので、
        集めたぶんが静かに消えたように見える)。
        AI が返したぶんを持つ側(`collect.Edits`)は初めから切ってある ——
        こちらだけ生だったのが食い違いの元だった。
        """
        title = item["title"] = notes.title_key(item.get("title"))
        row = self.conn.execute(
            "SELECT seq, body, url, tags, extra FROM items WHERE title = ?", (title,)
        ).fetchone()
        if row is None:
            self._seq += 1
            self.conn.execute(
                "INSERT INTO items (title, seq, body, url, tags, extra)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    title, self._seq,
                    item.get("body") if "body" in claims else None,
                    item.get("url") if "url" in claims else None,
                    json.dumps(item.get("tags") or [], ensure_ascii=False),
                    json.dumps(item.get("extra") or {}, ensure_ascii=False)
                    if "extra" in claims else "{}",
                ),
            )
            return

        body = row["body"] or (item.get("body") if "body" in claims else None)
        url = row["url"] or (item.get("url") if "url" in claims else None)
        tags = json.loads(row["tags"])
        added = [t for t in (item.get("tags") or []) if t not in tags]
        if added:
            tags = (tags + added)[:MAX_TAGS_PER_DOC]
        extra = json.loads(row["extra"])
        if "extra" in claims:
            for key, value in (item.get("extra") or {}).items():
                extra.setdefault(key, value)
        self.conn.execute(
            "UPDATE items SET body = ?, url = ?, tags = ?, extra = ? WHERE title = ?",
            (body, url, json.dumps(tags, ensure_ascii=False),
             json.dumps(extra, ensure_ascii=False), item["title"]),
        )

    def _to_item(self, row) -> dict:
        item = {"title": row["title"], "tags": json.loads(row["tags"])}
        if row["body"]:
            item["body"] = row["body"]
        if row["url"]:
            item["url"] = row["url"]
        if extra := json.loads(row["extra"]):
            item["extra"] = extra
        return item

    @property
    def count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    def take(self, title: str) -> dict | None:
        row = self.conn.execute(
            "SELECT title, body, url, tags, extra FROM items WHERE title = ?", (title,)
        ).fetchone()
        if row is None:
            return None
        self.conn.execute("UPDATE items SET used = 1 WHERE title = ?", (title,))
        return self._to_item(row)

    def rest(self) -> Iterator[dict]:
        rows = self.conn.execute(
            "SELECT title, body, url, tags, extra FROM items WHERE used = 0 ORDER BY seq"
        )
        for row in rows:
            yield self._to_item(row)

    def reset(self) -> None:
        self.conn.execute("UPDATE items SET used = 0")

    def __iter__(self) -> Iterator[dict]:
        rows = self.conn.execute(
            "SELECT title, body, url, tags, extra FROM items ORDER BY seq"
        )
        for row in rows:
            yield self._to_item(row)

    def __len__(self) -> int:
        return self.count

    def close(self) -> None:
        """置き場を片づける。**残すと一時ファイルが溜まる**。"""
        with suppress(Exception):
            self.conn.close()
        with suppress(Exception):
            os.unlink(self.path)


def _merge_item(merged: dict[str, dict], item: dict, claims) -> None:
    """1 件を名簿へ入れる。**既に入っている値は上書きしない**。

    `claims` はこの回に書き込んでよい項目。タグと見出しはいつでも入る。
    """
    existing = merged.get(item["title"])
    if existing is None:
        existing = merged[item["title"]] = {"title": item["title"], "tags": []}

    for key in ("body", "url"):
        if key in claims and not existing.get(key) and item.get(key):
            existing[key] = item[key]

    if added := [t for t in item.get("tags", []) if t not in existing["tags"]]:
        existing["tags"] += added
        del existing["tags"][MAX_TAGS_PER_DOC:]

    # **鍵ごとに見る。** まるごと見ると、前の本が 1 つでも持っていた時点で
    # 後ろの本の持つ別の鍵(電話やサイト)が入らない
    if "extra" in claims and (carried := item.get("extra")):
        into = existing.setdefault("extra", {})
        for key, value in carried.items():
            into.setdefault(key, value)


def _run_one(spec: dict, sources: dict, retired: set[str] | None = None) -> tuple[Iterator[dict], str]:
    """1 本ぶんを引く。

    **件数を書いていなければ全部取る。** 当たりすぎているときは黙って切らずに断る ——
    切ったことは返り値から分からないので、絞ったつもりのない指定が「そこまでしか無い」
    ように見えてしまう。
    """
    from app import db

    if spec["of"] == "tags":
        return _run_tags(spec, sources, retired)

    src, set_sql, params = _doc_ids(spec, sources)
    limit = spec["limit"]
    if limit is None:
        matched = count(spec, sources)
        if matched > MAX_ROWS:
            raise HTTPException(409, {
                "error": f"この指定は {matched:,} 件に当たります"
                         f"(一度に取れるのは {MAX_ROWS:,} 件まで)",
                "hint": "タグを絞るか、limit に取る件数を書いてください",
            })
        limit = MAX_ROWS

    sql = (
        f"SELECT title, {spec['body']} AS body, tags, extra, links FROM docs"
        f" WHERE doc_id IN ({set_sql}) ORDER BY rank_score DESC, title LIMIT ?"
    )
    args = (*params, limit)

    # つながりは**この抽出に入っているものだけ**が相手なので、先に全部読んでから作る。
    # **その規則を書いていなければ読まない** —— 数十万件の名簿では、見出しと
    # リンク先を持つだけでメモリが要る(地図の名簿はそもそもリンクを持たない)
    links = {}
    if any(rule["kind"] == "linked" for rule in spec["tags"]):
        for row in db.stream(src.path, sql, args, timeout=EXTRACT_TIMEOUT_SECONDS):
            links[(row["title"] or "").strip()] = _link_set(row["links"])
    context = {"src": src, "links": links}

    def items():
        seen = 0
        for row in db.stream(src.path, sql, args, timeout=EXTRACT_TIMEOUT_SECONDS):
            seen += 1
            if item := _to_item(dict(row), spec, context):
                yield item
        log.info("extract %s tag=%r: %d docs", spec["source"], spec["tag"], seen)

    return items(), spec["cursor"]


def _tag_rows(spec: dict, sources: dict, retired: set[str] | None = None) -> list[dict]:
    """そのソースのタグを、**件数・直近の件数・最後に付いた日**つきで数える。

    **読者に出さない印の付いた文書は数えない**(`notes.HIDDEN_TAGS`)。消したものや
    まだ精査していないものから語を起こすと、外したはずの宣伝が話題として並ぶ。

    **日付は配信日があればそちら。** 集めた日で数えると、古い記事をまとめて取り込んだ
    日に、その語が急に動き出したように見える。

    **切るのは外す語を落としてから。** 先に切ると、外す語が上位を埋めているぶんだけ
    拾える語が減る(配信元の名前はたいてい上位に来る)。

    **一度外された語は拾い直さない**(`retired`)。墓標は「これは話題ではない」という
    判断で、拾う側が知らないと毎回同じ語を並べ直す —— 足す側で弾かれるので中身は
    増えないが、**一緒に出てくる語の枠を食う**(実測で、上位 6 のうち 2 つが外した
    媒体名だった)。
    """
    from app import db

    src = sources.get(spec["source"])
    if src is None:
        raise HTTPException(404, {
            "error": f"抽出できません: ソース「{spec['source']}」がありません",
            "hint": "まだ焼いていないか、名前が違う(/v1/sources で確かめられる)",
        })
    hidden = ", ".join("?" * len(notes.HIDDEN_TAGS))
    rows = db.query(
        src.path,
        "WITH live AS ("
        " SELECT d.doc_id,"
        " substr(COALESCE(json_extract(d.extra, '$.published_at'), d.updated_at), 1, 10) AS day"
        " FROM docs d"
        f" WHERE d.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag IN ({hidden}))"
        ")"
        " SELECT t.tag AS tag, COUNT(*) AS docs,"
        " SUM(CASE WHEN l.day >= ? THEN 1 ELSE 0 END) AS docs_recent,"
        " MAX(l.day) AS last_seen"
        " FROM doc_tags t JOIN live l ON l.doc_id = t.doc_id"
        " GROUP BY t.tag HAVING docs >= ?"
        " ORDER BY docs_recent DESC, docs DESC, t.tag",
        (*notes.HIDDEN_TAGS, _days_ago(spec["recent_days"]), spec["min_docs"]),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    skip = [re.compile(one) for one in spec["skip"]]
    gone = retired or set()
    kept = [
        dict(row) for row in rows
        if row["tag"] not in gone
        and not any(pattern.search(row["tag"]) for pattern in skip)
    ]
    return kept[: spec["limit"]] if spec["limit"] else kept


def _days_ago(days: int) -> str:
    """直近の窓の始まり(日付)。**日付で比べる** —— 突き合わせる相手が日付までの
    文字列なので、時刻まで持つと境目の 1 日が落ちる。"""
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def _tag_links(spec: dict, sources: dict, tags: list[str]) -> dict[str, list[dict]]:
    """同じ文書に一緒に付いた回数。**名簿に入っている語どうしだけ**を数える。

    **つながりはここから引く。** AI に「関連するもの」を書かせると思いついた相手が
    並ぶので、一度も一緒に出ていない組が線になる —— 一緒に出た回数は手元で数えられる
    のだから数える。
    """
    from app import db

    if not tags or spec["links"] <= 0:
        return {}
    src = sources[spec["source"]]
    marks = ", ".join("?" * len(tags))
    hidden = ", ".join("?" * len(notes.HIDDEN_TAGS))
    rows = db.query(
        src.path,
        "SELECT a.tag AS one, b.tag AS other, COUNT(*) AS n"
        " FROM doc_tags a JOIN doc_tags b ON a.doc_id = b.doc_id AND a.tag < b.tag"
        f" WHERE a.tag IN ({marks}) AND b.tag IN ({marks})"
        f" AND a.doc_id NOT IN (SELECT doc_id FROM doc_tags WHERE tag IN ({hidden}))"
        " GROUP BY a.tag, b.tag ORDER BY n DESC, a.tag, b.tag",
        (*tags, *tags, *notes.HIDDEN_TAGS),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    linked: dict[str, list[dict]] = {}
    for row in rows:
        for tag, other in ((row["one"], row["other"]), (row["other"], row["one"])):
            partners = linked.setdefault(tag, [])
            if len(partners) < spec["links"]:
                partners.append({"tag": other, "n": row["n"]})
    return linked


def _run_tags(
    spec: dict, sources: dict, retired: set[str] | None = None
) -> tuple[Iterator[dict], str]:
    """タグの名簿を、集める層が読む形にする。

    **本文には数えたことだけを書く。** その語が何を指すのかは AI の仕事で、精査の回が
    書き直したら次の機械の回はそれを残す(足すだけの回なので本文には触らない)。
    **数のほうは毎回入れ替わる**(`collect.stream_docs` の `facts`)。
    """
    rows = _tag_rows(spec, sources, retired)
    linked = _tag_links(spec, sources, [row["tag"] for row in rows])
    context = {"src": sources[spec["source"]], "links": {}}

    def items():
        for row in rows:
            extra = {
                "docs": row["docs"],
                "docs_recent": row["docs_recent"],
                "last_seen": row["last_seen"] or "",
            }
            if partners := linked.get(row["tag"]):
                # **脇書きに入れ子は置かない**(`collect._carried` の決まり)ので、
                # 「語:回数」の並びにする。読む側は右端の `:` で割る ——
                # 語そのものに `:` が入ることがある(「CodeZine:新着一覧」)
                extra["links"] = [f"{one['tag']}:{one['n']}" for one in partners]
            yield {
                "title": row["tag"],
                "body": (
                    f"この語が付いているのは {row['docs']} 件"
                    f"(直近 {spec['recent_days']} 日で {row['docs_recent']} 件)。"
                    + (f"最後に付いたのは {row['last_seen']}。" if row["last_seen"] else "")
                ),
                # **読み替えの規則はそのまま効く**({"const": "トピック"} で目印を
                # 付ける、など)。元のタグは無い —— この 1 件そのものがタグなので
                "tags": _apply_rules(spec["tags"], [], row["tag"], context),
                "extra": extra,
            }
        log.info("extract %s of=tags: %d tags", spec["source"], len(rows))

    return items(), spec["cursor"]


def split_tags(raw: str) -> list[str]:
    return [t.strip() for t in (raw or "").split(",") if t.strip()]


def _link_set(raw) -> set[str]:
    """記事から出ているリンク先。節への飛び先(`記事名#節名`)は記事名まで畳む。"""
    return {str(link).split("#", 1)[0].strip() for link in _json_list(raw)}


def _to_item(row: dict, spec: dict, context: dict) -> dict | None:
    """1 行を 1 件にする。本文が空のものは落とす(焼く側でも落ちる)。"""
    title = (row.get("title") or "").strip()
    body = (row.get("body") or "").strip()
    if not title or not body:
        return None

    tags = [str(t) for t in _json_list(row.get("tags"))]
    extra = _json_map(row.get("extra"))

    item = {
        "title": title,
        "body": body,
        "tags": _apply_rules(spec["tags"], tags, title, context),
    }
    if template := spec["url"]:
        item["url"] = _fill(template, {"title": title, **{
            k: str(v) for k, v in extra.items() if isinstance(v, (str, int, float))
        }})
    # **元の記事の事実をそのまま運ぶ。** 無い鍵は黙って飛ばす —— 記事によって
    # 持っている値が違う(ページビューを持たない記事もある)
    if carried := {k: extra[k] for k in spec["extra"] if k in extra}:
        item["extra"] = carried
    return item


def _json_list(raw) -> list:
    """`docs.tags` は JSON の配列を文字列で持っている。読めなければ空。"""
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _json_map(raw) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _apply_rules(rules: list[dict], tags: list[str], title: str, context: dict) -> list[str]:
    """引いてきたタグを、依頼した側が読める形に読み替える。

    **元のタグは持ち越さない。** カテゴリはソースの都合で付いているもので、
    そのまま渡すと読む側が「どれが意味のあるタグか」を選ぶことになる。
    """
    out: list[str] = []
    for rule in rules:
        # **前の規則が同じ接頭辞のタグを作っていれば飛ばす**(`_fallback_of`)
        if rule.get("fallback") and any(
            t.startswith(_prefix_of(rule["format"])) for t in out
        ):
            continue
        if rule["kind"] == "const":
            out.append(rule["value"])
        elif rule["kind"] == "each":
            pattern = rule["patterns"][0]
            for tag in tags:
                if found := pattern.match(tag):
                    out.append(_fill_groups(rule["format"], [found]))
                    # 手掛かりの代わりに使う規則は 1 本で足りる(2 つ当たっても 2 本作らない)
                    if rule.get("fallback"):
                        break
        elif rule["kind"] == "from_tag":
            out.extend(_from_tag(rule, title, context))
        elif rule["kind"] == "linked":
            out.extend(_linked(rule, title, context))
        else:
            found = [_first_match(pattern, tags) for pattern in rule["patterns"]]
            # 1 つでも当たらなければ作らない(「1840-」のような半端を残さない)
            if all(found):
                out.append(_fill_groups(rule["format"], found))

    seen, unique = set(), []
    for tag in out:
        if tag and tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique[:MAX_TAGS_PER_DOC]


def _from_tag(rule: dict, title: str, context: dict) -> list[str]:
    """この文書の見出しから作ったタグで、別の文書を引く。

    「<その人>の作品」「<その人>の楽曲」のようなカテゴリを持つソースなら、そこから
    人気の順に取れば代表作になる(人物 30 件で実測したとき 20 件が持っていた)。
    **無ければ何も作らない**。
    """
    from app import db

    src = context["src"]
    tag = _fill(rule["tag"], {"title": title})
    rows = db.query(
        src.path,
        "SELECT title FROM docs WHERE doc_id IN"
        " (SELECT doc_id FROM doc_tags WHERE tag = ?)"
        " ORDER BY rank_score DESC, title LIMIT ?",
        (tag, rule["take"]),
        timeout=EXTRACT_TIMEOUT_SECONDS,
    )
    return [_fill(rule["format"], {"1": row[0]}) for row in rows]


def _linked(rule: dict, title: str, context: dict) -> list[str]:
    """この抽出に入っている文書のうち、記事がリンクしている相手。

    **相互リンクを既定にする。** 片側だけだと、有名なものどうしが軒並み繋がって
    毛玉になる(実測で 12% が繋がった)。相互は 7% まで落ちて、しかも残った組は
    実際に関係のある相手だった。
    """
    links = context["links"]
    mine = links.get(title, set())
    others = [
        other
        for other in links
        if other != title and other in mine
        and (rule["how"] == "out" or title in links.get(other, set()))
    ]
    return [_fill(rule["format"], {"1": other}) for other in others]


def _first_match(pattern: re.Pattern, tags: list[str]):
    for tag in tags:
        if found := pattern.match(tag):
            return found
    return None


def _fill_groups(template: str, matches: list) -> str:
    """`{1}` `{2}` … を、捕獲した中身で埋める。

    当たった順に、それぞれの捕獲を並べたものを 1・2・3… と数える
    (`patterns` を 2 つ書いても、1 つの pattern に括弧を 2 つ書いても同じ番号になる)。
    括弧を書いていなければ、当たった部分そのものを 1 つとして数える。
    """
    values = {}
    for found in matches:
        for group in found.groups() or (found.group(0),):
            values[str(len(values) + 1)] = group or ""
    return _fill(template, values)


def _fill(template: str, values: dict[str, str]) -> str:
    """`{名前}` を置き換える。知らない名前はそのまま残す(消して詰めない)。"""
    def swap(found: re.Match) -> str:
        return values.get(found.group(1), found.group(0))

    return re.sub(r"\{([^{}]+)\}", swap, template)

# ---- 依頼文から指定を書かせる ------------------------------------------------

SPEC_GUIDE = """依頼を読んで、**まず「手元の索引から機械的に引けるか」を決めてください**。

引けるなら、抽出の指定を書いてください。引けないなら
`{"extract": null, "reason": "引けない理由"}` を返してください。指定さえ書ければ、
そのあとは AI を呼ばずに何万件でも一度に取れます(1000 件を 10 件ずつ AI に書かせると
100 回かかりますが、指定なら 1 秒で終わります)。

**決める前に、道具で確かめてください。** あなたには Chiezo の道具が渡してあります ——
どんなタグが実在するか(tags)、そのタグに何件当たるか(filter)、記事から何が
リンクされているか(links)、1 件がどんなタグを持っているか(doc)。
**タグ名は当てずっぽうでは当たりません**(完全一致でしか引けないので、それらしい
名前を書くと静かな 0 件になります)。実在する名前を引いてから書いてください。

**同じ形の名前がずらりと並ぶなら、書き並べずに `tag_suffix` を使ってください。**
「〜の画家」「〜の作曲家」のように**族をなすカテゴリ**は、数が多いうえに名前の付き方が
揃っていません —— 書き並べると必ず取りこぼします(実例: 地域を書き並べた指定で
「アメリカ合衆国」が丸ごと落ち、「イギリス」と書いたせいで中身の大半がある
「イングランド」が取れていなかった)。末尾で指せば**推測が要らず、あとから
カテゴリが増えても勝手に入ります**。要らないものは `not_tag` で外してください。

**同じものの呼び方が 1 つとは限りません。** 末尾はカンマ区切りで何個でも書けるので、
別の呼び方のカテゴリも一緒に指してください(実例: 画家の名簿で「の画家」だけを
書いていたせいで「〜の女性画家」が丸ごと落ちていた —— 44 カテゴリ・1,877 記事)。

抽出の指定は次の形の JSON です。

{
  "source": "引くソース名",
  "tag": "絞り込むタグ(完全一致。カンマ区切りで複数書くと、そのどれかを持つもの)",
  "tag_suffix": "タグの末尾(これで終わるタグ全部。カンマ区切りで何個でも書ける。"
               "tag と併用でき、全部の和になる)",
  "not_tag": "外すタグ(カンマ区切り。これを持つものは、tag に当たっていても取らない)",
  "limit": 30,   ← 書かなければ全部。AI に読ませる側の都合で絞るときだけ書く
  "body": "opening(冒頭。既定) か body(全文)",
  "url": "出典の作り方。{title} と、そのソースが extra に持っている値を差し込める",
  "tags": [
    {"const": "そのまま付ける固定のタグ"},
    {"pattern": "^(.+)出身の人物$", "format": "出身:{1}"},
    {"patterns": ["^(\\d{3,4})年生$", "^(\\d{3,4})年没$"], "format": "年代:{1}-{2}"},
    {"pattern": "^(\\d{3,4})年生$", "format": "年代:{1}-", "fallback": true},
    {"from_tag": "{title}の楽曲", "format": "代表曲:{1}", "take": 1},
    {"linked": "mutual", "format": "関連:{1}"}
  ],
  "cursor": "抽出し終えた印(次の実行はここから肉付けになる)"
}

タグの読み替えは 5 通りです。
- const: 固定の 1 つ。読む側が「これは何の一覧か」を見分ける目印に使う
- pattern: **当たったタグごとに 1 つ**作る。1 件に複数付きうるもの向け
- patterns: **全部当たったときだけ 1 つ**作る。2 つのタグを 1 つにまとめる読み方。
  1 つでも当たらなければ作らない(半端なタグを残さない)
- from_tag: **この文書の見出しから作ったタグで、別の文書を引く**(`{title}` が入る)。
  「<その人>の楽曲」「<その人>の作品」のようなカテゴリがあるソースでは、人気の順に
  取れば代表作になる。`take` で何件取るか(既定 1)。そのカテゴリが無ければ何も作らない
- linked: **この抽出に入っている文書のうち、記事がリンクしている相手**を出す。
  `mutual`(相互にリンクしているものだけ)と `out`(こちらから張っているもの)。
  片側だと有名なものどうしが軒並み繋がって毛玉になるので、**まず mutual を使う**
`{1}` `{2}` は、当たった順に括弧で捕まえた中身が入ります。

pattern と patterns には `"fallback": true` を書けます。**前の規則が同じ接頭辞の
タグ(`年代:` など)を作っていれば、その規則は作りません**。同じ軸を確かな手掛かりの
順に並べるときに使います(生年と没年の両方 → 生年だけ、のように。書かないと、
当たった全部が足されて同じ軸のタグが何本も付きます)。

**書いていないことは引けませんが、書いてあることは全部引けます。** 関係や代表作も、
カテゴリやリンクの形で書いてあれば取れます。AI に書かせるのは、そのどれでも
取れないものだけにしてください。

守ること。
- source は実在するソース名。tag は**そのソースに実在するタグ**(前方一致や部分一致
  ではなく完全一致で引くので、それらしい名前を作ると 0 件になる)
- **カテゴリは持ち主の職業を選ばない。** 仕事で括ったカテゴリにも、それで知られて
  いない人が入っている。人気の順に取るとそちらが先に並ぶので、混ぜたくないものが
  はっきりしているときは `not_tag` で外す
- 並び順は書けない。ソースが持っている順(ページビューや人口の多い順)で上から取る
- **件数は絞らなくてよい。** 引くのは手元の索引を 1 本引くだけで、AI の枠も時間も
  使わない。「有名なほうから N 件」と頼まれたときだけ limit を書く
- 元のタグは持ち越さない。読む側が要るタグだけを読み替えで作る

出力は JSON だけ。前置き・説明・コードブロックの記号は付けない。"""


def build_draft_messages(want: str, sources: dict, current=None) -> list[dict]:
    """依頼文から指定を書かせるときの本文。

    **どのソースがあるかは渡す**(名前を知らなければ実在しないソースを書く)。
    タグまでは渡さない —— 1 つのソースに数十万のタグがあり、渡しきれない。
    書かせた指定は実際に引いてみて、0 件なら候補を添えて返す(`similar_tags`)。

    **いまの指定は書いてある形のまま見せる**(配列なら配列)。畳んで見せると、
    直してと頼まれた AI が 1 本に書き戻してしまい、他のソースが黙って落ちる。
    """
    catalog = ", ".join(
        f"{name}({src.kind})" for name, src in sorted(sources.items())
    ) or "(まだ 1 つも焼かれていません)"
    parts = [f"集めたいもの: {want.strip()}", f"\n引けるソース: {catalog}"]
    if current:
        parts.append("\nいまの指定:\n" + json.dumps(current, ensure_ascii=False, indent=2))
    parts.append("\nこれを踏まえた抽出の指定を書いて。")
    return [
        {"role": "system", "content": SPEC_GUIDE},
        {"role": "user", "content": "\n".join(parts)},
    ]


def parse_draft(content: str) -> dict:
    """AI の答えから指定を取り出す。前置きやコードブロックが混ざっても拾う。

    **「機械では引けない」も答えのうち**(`{"extract": null, "reason": …}`)。
    引けないものを無理に指定へ落とすと、当たらないタグで静かな 0 件になる。
    """
    stripped = re.sub(r"```(?:json)?", "", content or "").strip()
    start, end = stripped.find("{"), stripped.rfind("}")
    if start < 0 or end <= start:
        raise HTTPException(502, {"error": "AI が抽出の指定を返しませんでした"})
    try:
        value = json.loads(stripped[start : end + 1])
    except ValueError as e:
        raise HTTPException(502, {
            "error": "AI の答えを JSON として読めませんでした",
            "reason": type(e).__name__,
        }) from None
    if not isinstance(value, dict):
        raise HTTPException(502, {"error": "AI の答えがオブジェクトではありません"})
    return value


def similar_tags(spec: dict, sources: dict, limit: int = 15) -> list[dict]:
    """書かれたタグに似た、**実在するタグ**を文書数つきで返す。

    **書いた本人には確かめようがない** —— タグは完全一致でしか引けないので、
    それらしい名前を書いた瞬間に静かな 0 件になる。広い語も同じで、実在はするが
    数件しか付いておらず、欲しいものは時代や地域で絞った名前の側にある。
    実在する名前を数と一緒に返して、選び直せるようにする。

    **見るのは先頭の 1 本だけ。** これを使うのは AI に指定を書かせる道で、
    そこで書かせるのは 1 本(`/v1/collect/draft-extract`)——
    束ねて渡されたときに全部の候補を混ぜると、どの本のための候補なのかが消える。
    """
    from app import db

    written = specs(spec)
    if not written:
        return []
    spec = written[0]
    src = sources.get(spec["source"])
    if src is None:
        return []
    # 書かれたタグを部分一致で探す。長い語ほど当たらないので、短くしながら試す
    wanted = (spec["tag"].split(",")[0] or spec["tag_suffix"]).strip()
    if not wanted:
        return []
    for length in range(len(wanted), 1, -1):
        rows = db.query(
            src.path,
            "SELECT tag, docs FROM tag_counts WHERE tag LIKE ? ORDER BY docs DESC LIMIT ?",
            (f"%{wanted[:length]}%", limit),
        )
        if rows:
            return [{"tag": row[0], "docs": row[1]} for row in rows]
    return []


def build_retry_messages(want: str, spec: dict, total: int, candidates: list[dict]) -> list[dict]:
    """取れた数が足りなかったときに、**実在するタグを見せて選び直させる**本文。

    1 度だけ投げ直す。タグ名は手元にしか無く、書く側は当てるしかない ——
    当てさせるより、実在するものを見せたほうが早いし確かめられる。
    """
    listed = "\n".join(f"- {c['tag']}({c['docs']} 件)" for c in candidates)
    return [
        {"role": "system", "content": SPEC_GUIDE},
        {
            "role": "user",
            "content": (
                f"集めたいもの: {want.strip()}\n\n"
                "さきほどの指定:\n"
                + json.dumps(to_json(spec), ensure_ascii=False, indent=2)
                + f"\n\nこの指定では {total} 件しか取れませんでした"
                f"(頼まれた件数は {spec['limit']} 件)。\n"
                "実在するタグは次のとおりです(文書数つき)。この中から選び直してください。\n"
                "1 つで足りなければ、カンマ区切りで複数書けます(そのどれかを持つものが取れます)。\n"
                f"{listed}\n\n"
                "**頼まれた範囲から外れるタグは足さないでください。**"
                "件数を満たすことより、頼まれたものだけが入っていることのほうが大事です"
                "(足りなければ足りないままで構いません)。\n"
                "選び直した指定を JSON で返してください。"
            ),
        },
    ]
