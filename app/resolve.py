"""引き当て —— AI が返した店の手がかりから、**辞典の 1 件を機械で引き当てる**。

## 何のためか

AI に店の座標を書かせると、**それらしい数字を作る**か、どこかの地図で見た別の店舗の
位置を写す。名前だけで辞典を引いても外れる —— 同じ名前の店は辞典に何件もあり
(本店・支店・閉じた旧店舗・取り込みの都合で重なった別の記録)、**素の見出しが
本店とは限らない**。実際に、ある本店の名前で店舗辞典を引くと同じ名前が 3 件あり、
番号の付かない素の見出しは本店から 26 km 離れた別の町の店で、もう 1 件は住所が
本店と同じなのに座標が隣の県だった。本店に当たるのは番号付きの 1 件で、地図辞典の
点とは数 m の差で一致した。

そこで役目を分ける。**AI は店の名前と手がかり(住所・頁の URL・辞典の見出し)だけを
返し、座標は焼くときに Chiezo が辞典から引く**。引き当ての条件は手がかりと辞典の
両方から確かめられる事実だけで組む:

- **名前**が一致する(空白の有無は寄せる。「本店」「店」の付け外しは寄せるが、
  そのときは住所かもう 1 つの辞典で確かめられたものだけを採る)
- **県**が一致する(手がかりの住所の頭、無ければ 1 件の県のタグ。辞典の点の県と比べる)
- **市区町村**が一致する(辞典の町の名前が、手がかりの住所に入っている)
- **番地**が一致する(町の名前と、丁目・番地の数の並び)
- **サイト**が一致する(手がかりの URL と、辞典が持つ店のサイト)
- **辞典どうしが一致する**(別々の辞典の同じ名前の点が、近く(`agree_meters`)で重なる)

県・市区町村・番地のどれかが**食い違う候補は捨てる**(同名の別店舗はここで落ちる)。
残った候補を近さで束ね、**確かな束(市区町村・番地・サイトのどれかが合う、または
辞典どうしが一致する)がちょうど 1 つ**なら引き当てる。確かな束が無く、県だけが
合う候補がその県にただ 1 つなら、それを採る(採った根拠は `basis` に残るので、
読む側は「県と名前だけ」で採ったものを見分けられる)。**2 つ以上残ったら決めない**
(どれを選んでも、外れたときに気づく手立てが無い)。

## 引き当てたものは AI の回で動かさない

運んだ座標・住所・元の記録の鍵は、手がかりが変わらない限りそのまま残す。
AI が次の回に同じ店をそのまま返してきても(引き当てた名前や見出しを写してきても)、
引き直さずに前のものを使う —— 辞典を取り込み直すと点がわずかに動くことがあり、
動くたびに「直した」に数えると、何も起きていない 1 件が履歴に並ぶ。
**AI が返した座標は捨てる**(`ai_coords` を立てたときだけ残す)。

**人が現地で確かめた位置(`locks`)とは別物**。あちらは焼く最後に当て直す固定で、
引き当ての結果より強い(引き当てても、固定した 1 件の座標は固定のまま)。

## Chiezo は何を引き当てているかを知らない

どの脇書きが手がかりか(`clue`)・どの辞典を照らすか・元の記録をどの鍵で名指すか・
出どころのタグを何と書くか・引き当てられなかった 1 件に何のタグを付けるかは、
収集の定義(`resolve`)に書く。店でなくても、名前と住所で辞典の 1 件を指せる
ものなら同じ形で使える。
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

from fastapi import HTTPException

from app import db, notes
from app import extract as extraction

log = logging.getLogger("chiezo.app")

# 機械が運んだ値を、AI の返した 1 件に載せて焼く側へ渡す鍵(素の側に置く)。
# **`extra` の中に置かない** —— あちらは AI が運ぶ事実の札で、入れ子を落とし
# (`collect._carried`)、鍵の数にも天井がある。辞典から運ぶ値は入れ子(店の中身)で、
# AI の札の数とは関係なく載る必要がある
FACTS_KEY = "_chiezo_facts"

MAX_SOURCES = 4
MAX_CARRY_KEYS = 10
MAX_TO_EXTRA = 5
MAX_TAG_CHARS = 40
MAX_CLUE_CHARS = 200
DEFAULT_AGREE_METERS = 100
MIN_AGREE_METERS = 10
MAX_AGREE_METERS = 2000
# 辞典から店に写す既定の鍵。**辞典ごとに持っている鍵は違う**(地図辞典は住所と
# 営業時間、店舗辞典は住所とサイトと電話)ので、持っていないものは黙って飛ばす
DEFAULT_CARRY = ("address", "phone", "website", "opening_hours")
# 1 つの名前で 1 つの辞典から拾う候補の上限。**チェーンの名前は数百件当たる**ので、
# 天井を置いたうえで、当たりすぎたことを理由に書く
MAX_CANDIDATES = 200
# 引き当てで辞典を引くときの持ち時間。**人が待っている問い合わせではない**(焼く前の
# 背景の仕事)ので、読み口の 5 秒より長く取る
QUERY_TIMEOUT_SECONDS = 30.0
# 点の県を周りの地物から読むときの範囲(度)。**狭いほうから広げる** —— 県境の近くで
# 広く取ると、隣の県の地物が多数を占めることがある
AREA_PROBES = (0.003, 0.01, 0.03)
# 引き当てられなかった理由に添える候補の数
MAX_SHOWN_CANDIDATES = 5
# 名前に寄せる揺れ。**付け外しで採るのは、住所か辞典どうしで確かめられたときだけ**
LOOSE_SUFFIXES = ("本店", "店")
# 名前の飾りの記号(辞典によって付いたり付かなかったりする)。比べるときは落とす
DECORATIONS = "♪♫☆★♡♥❤"
# 伸ばす音の書き分け(「〜」と「ー」)。比べるときは「ー」にそろえる
WAVES = "〜～~"
# 名前の 1 か所に空白を挟んだ形も引く上限の長さ(地図の見出しは「店名 支店名」の
# ように空白で区切ることが多いのに、手がかりは詰めて書かれる)
MAX_SPACED_NAME_CHARS = 24

NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,39}")

BASIS_NAME = "名前"
BASIS_AREA = "県"
BASIS_CITY = "市区町村"
BASIS_BLOCK = "番地"
BASIS_SITE = "サイト"
BASIS_AGREE = "辞典の一致"
BASIS_LOOSE = "名前の揺れ"
BASIS_REF = "指した見出し"
BASIS_ADDRESS = "住所"
# **AI が指した見出しを、名前の書き方が違っても採る**ときの、名前の重なりの下限
# (`_named_alike`)。共通の続いた字がこの字数以上、かつ短いほうの名前のこの割合以上。
# 短いほうがまるごと長いほうに入っていれば字数は問わない。運営の名前を頭に付けた
# 正式名と看板の名前だけの見出しは通り、屋号の頭 2 字だけが同じ名前は通らない
MIN_SHARED_NAME_CHARS = 3
MIN_SHARED_NAME_RATIO = 0.6


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": f"resolve が読めません: {message}"})


# ---- 指定 ------------------------------------------------------------------------


def normalize(raw) -> dict | None:
    """引き当ての指定を均す。無ければ None。**読めない指定は断る**。

        {
          "clue": "shop",
          "sources": [
            {"source": "osm_japan", "identity": ["osm_type", "osm_id"], "tag": "出典:OSM"},
            {"source": "overture_japan", "identity": ["overture_id"], "tag": "出典:Overture"}
          ],
          "area_tag": "地域:",
          "agree_meters": 100,
          "carry": ["address", "phone", "website", "opening_hours"],
          "to_extra": {"opening_hours": "opening_hours"},
          "failed_tag": "状態:代表店未定",
          "failed_when_missing": false,
          "none_tag": "状態:代表店なし",
          "ai_coords": false
        }

    - `failed_when_missing` —— **手がかりがまだ無い 1 件にも** `failed_tag` を付ける
      (既定は引き当てに失敗した 1 件だけ)。「まだ決まっていないもの」を 1 つの印で
      数えたい・区画の足止め(巡回の `hold_tag`)に使いたい収集のため
    - `none_tag` —— **「探したが無かった」の印**。これが付いた 1 件には `failed_tag` を
      付けない(付いていれば外す)。引き当てた 1 件からはこの印も外す
    """
    if raw in (None, "", {}):
        return None
    if not isinstance(raw, dict):
        raise _bad("オブジェクトで書いてください")
    clue = str(raw.get("clue") or "").strip()
    if not NAME_RE.fullmatch(clue):
        raise _bad("clue(手がかりを書く脇書きの鍵。英字・数字・_)が要ります")
    if clue in ("lat", "lon", "url"):
        raise _bad(f"clue に {clue} は使えません(座標と出典の鍵なので)")
    sources = raw.get("sources")
    if not isinstance(sources, list) or not sources:
        raise _bad("sources(照らす辞典)を 1 つ以上書いてください")
    if len(sources) > MAX_SOURCES:
        raise _bad(f"sources は {MAX_SOURCES} つまでです")
    seen: set[str] = set()
    written = []
    for one in sources:
        if isinstance(one, str):
            one = {"source": one}
        if not isinstance(one, dict):
            raise _bad("sources の中身はソース名かオブジェクトで書いてください")
        name = str(one.get("source") or "").strip()
        if not name:
            raise _bad("sources の各項目に source(ソース名)が要ります")
        if name in seen:
            raise _bad(f"同じ辞典を 2 度は書けません: {name}")
        seen.add(name)
        identity = one.get("identity") or []
        if not isinstance(identity, list) or len(identity) > extraction.MAX_IDENTITY_KEYS:
            raise _bad(f"identity は {extraction.MAX_IDENTITY_KEYS} 個までの鍵の配列で書いてください")
        tag = str(one.get("tag") or "").strip()
        if len(tag) > MAX_TAG_CHARS or tag.startswith("_chiezo_"):
            raise _bad(f"tag は {MAX_TAG_CHARS} 文字までで、Chiezo の印(_chiezo_)は使えません")
        written.append({
            "source": name,
            "identity": [str(k).strip() for k in identity if str(k).strip()],
            "tag": tag,
        })
    try:
        agree = int(raw.get("agree_meters") or DEFAULT_AGREE_METERS)
    except (TypeError, ValueError):
        raise _bad("agree_meters は数で書いてください") from None
    if not MIN_AGREE_METERS <= agree <= MAX_AGREE_METERS:
        raise _bad(f"agree_meters は {MIN_AGREE_METERS}〜{MAX_AGREE_METERS} のあいだ")
    carry = raw.get("carry")
    if carry is None:
        carry = list(DEFAULT_CARRY)
    if not isinstance(carry, list) or len(carry) > MAX_CARRY_KEYS:
        raise _bad(f"carry は {MAX_CARRY_KEYS} 個までの鍵の配列で書いてください")
    to_extra = raw.get("to_extra") or {}
    if not isinstance(to_extra, dict) or len(to_extra) > MAX_TO_EXTRA:
        raise _bad(f"to_extra は {MAX_TO_EXTRA} 個までの「辞典の鍵 → 1 件の鍵」で書いてください")
    lifted = {}
    for key, into in to_extra.items():
        into = str(into or "").strip()
        if not NAME_RE.fullmatch(into) or into in ("lat", "lon", "url", clue):
            raise _bad(f"to_extra の写し先に使えない鍵です: {into!r}")
        lifted[str(key).strip()] = into
    failed = str(raw.get("failed_tag") or "").strip()
    if len(failed) > MAX_TAG_CHARS or failed.startswith("_chiezo_"):
        raise _bad(f"failed_tag は {MAX_TAG_CHARS} 文字までで、Chiezo の印(_chiezo_)は使えません")
    none = str(raw.get("none_tag") or "").strip()
    if len(none) > MAX_TAG_CHARS or none.startswith("_chiezo_"):
        raise _bad(f"none_tag は {MAX_TAG_CHARS} 文字までで、Chiezo の印(_chiezo_)は使えません")
    if none and none == failed:
        raise _bad("none_tag と failed_tag は別のタグにしてください")
    address = _address_spec(raw.get("address"), seen)
    when_missing = bool(raw.get("failed_when_missing", False))
    if when_missing and not failed:
        # 付けるタグが無いのに「付ける」と書いても何も起きない。黙って通すと、
        # 書いた人は効いていると思ったまま回る
        raise _bad("failed_when_missing を使うときは failed_tag も書いてください")
    return {
        "clue": clue,
        "sources": written,
        "area_tag": str(raw.get("area_tag") or "").strip(),
        "agree_meters": agree,
        "carry": [str(k).strip() for k in carry if str(k).strip()],
        "to_extra": lifted,
        "failed_tag": failed,
        "failed_when_missing": when_missing,
        "none_tag": none,
        "ai_coords": bool(raw.get("ai_coords", False)),
        "address": address,
    }


def _address_spec(raw, taken: set[str]) -> dict | None:
    """住所から座標を引く辞典の指定(`address`)。無ければ None。

        {"source": "address_jp", "tag": "出典:位置参照情報"}

    **店の辞典で引き当てられなかったときだけ使う最後の段**(`_by_address`)。
    """
    if raw in (None, "", {}):
        return None
    if isinstance(raw, str):
        raw = {"source": raw}
    if not isinstance(raw, dict) or not str(raw.get("source") or "").strip():
        raise _bad("address は {\"source\": 住所の辞典, \"tag\": 出どころのタグ} で書いてください")
    name = str(raw["source"]).strip()
    if name in taken:
        raise _bad(f"address の辞典は sources と別のものにしてください: {name}")
    tag = str(raw.get("tag") or "").strip()
    if len(tag) > MAX_TAG_CHARS or tag.startswith("_chiezo_"):
        raise _bad(f"tag は {MAX_TAG_CHARS} 文字までで、Chiezo の印(_chiezo_)は使えません")
    return {"source": name, "tag": tag}


def to_json(spec: dict | None) -> dict | None:
    """定義に持たせる形。**既定のままの鍵は落とす**(決めたのか任せたのかが読めるように)。"""
    if not spec:
        return None
    out = {"clue": spec["clue"], "sources": [
        {k: v for k, v in one.items() if v} for one in spec["sources"]
    ]}
    for key in ("area_tag", "failed_tag", "none_tag"):
        if spec.get(key):
            out[key] = spec[key]
    if spec.get("failed_when_missing"):
        out["failed_when_missing"] = True
    if spec["agree_meters"] != DEFAULT_AGREE_METERS:
        out["agree_meters"] = spec["agree_meters"]
    if tuple(spec["carry"]) != DEFAULT_CARRY:
        out["carry"] = list(spec["carry"])
    if spec["to_extra"]:
        out["to_extra"] = dict(spec["to_extra"])
    if spec["ai_coords"]:
        out["ai_coords"] = True
    if spec.get("address"):
        out["address"] = {k: v for k, v in spec["address"].items() if v}
    return out


def source_tags(spec: dict) -> set[str]:
    """出どころのタグの全部(機械が付け替えるタグ)。"""
    return {one["tag"] for one in _tagged_sources(spec) if one["tag"]}


def _tagged_sources(spec: dict) -> list[dict]:
    """出どころのタグを持つ辞典の全部(店の辞典と、住所の辞典)。"""
    return [*spec["sources"], *([spec["address"]] if spec.get("address") else [])]


# ---- 文字の均し ----------------------------------------------------------------------

_SPACE = re.compile(r"\s+")
# 辞典が同名の見出しに付ける見分けの札(「名前 (番号)」「名前 (node:123)」「名前 (町)」)
_NUMBERED = re.compile(r"^(.*\S)\s*\([^()]*\)$")
_POSTCODE = re.compile(r"^〒?\d{3}-?\d{4}")
# 日本語の字(かな・漢字)。町の名前が日本語で書かれているかを見る
_JAPANESE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff]")
_KANJI_DIGITS = {c: i for i, c in enumerate("〇一二三四五六七八九")}


def _nfkc(text) -> str:
    return unicodedata.normalize("NFKC", str(text or "")).strip()


def _plain(text: str) -> str:
    """飾りの記号を落とし、伸ばす音を「ー」にそろえた形。"""
    text = "".join(c for c in text if c not in DECORATIONS)
    return re.sub(f"[{WAVES}]", "ー", text)


def name_key(text) -> str:
    """名前を比べる形(NFKC・空白を詰める・英字は小文字・飾りの記号と伸ばす音をそろえる)。"""
    return _plain(_SPACE.sub("", _nfkc(text))).casefold()


def base_name(title: str) -> str:
    """辞典の見出しから見分けの札を外した、店そのものの名前。"""
    found = _NUMBERED.match(title or "")
    return found.group(1).strip() if found else (title or "").strip()


def _kanji_number(found: re.Match) -> str:
    text = found.group(1)
    if "十" in text:
        head, _, tail = text.partition("十")
        value = (_KANJI_DIGITS.get(head, 1) if head else 1) * 10
        value += _KANJI_DIGITS.get(tail, 0) if tail else 0
        return str(value) + found.group(2)
    return "".join(str(_KANJI_DIGITS[c]) for c in text) + found.group(2)


def address_key(text) -> str:
    """住所を比べる形。NFKC・空白を詰める・郵便番号を落とす・丁目/番地/番/号を `-` に。"""
    value = _SPACE.sub("", _nfkc(text))
    value = _POSTCODE.sub("", value)
    value = re.sub(r"([〇一二三四五六七八九十]+)(丁目)", _kanji_number, value)
    value = re.sub(r"(\d)の(\d)", r"\1-\2", value)
    value = re.sub(r"(丁目|番地|番|号)", "-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def _site_key(url) -> str:
    text = _nfkc(url).casefold()
    text = re.sub(r"^https?://", "", text)
    text = re.sub(r"^www\.", "", text)
    return text.split("#", 1)[0].rstrip("/")


# ---- 候補 ---------------------------------------------------------------------------


@dataclass
class Candidate:
    source: str
    title: str
    lat: float
    lon: float
    extra: dict
    area: str | None = None
    by_ref: bool = False
    loose: bool = False
    # 確かめた結果(None は「確かめられない」)
    area_ok: bool | None = None
    city_ok: bool | None = None
    block_ok: bool | None = None
    site_ok: bool | None = None
    why_not: str = ""
    agrees: bool = False

    @property
    def strong(self) -> bool:
        return bool(self.city_ok or self.block_ok or self.site_ok)

    def label(self) -> str:
        where = self.extra.get("locality") or self.area or "所在不明"
        text = f"{self.source}:{self.title}({where}"
        return text + (f"・{self.why_not})" if self.why_not else ")")


@dataclass
class Result:
    shop: dict
    lat: float | None = None
    lon: float | None = None

    @property
    def ok(self) -> bool:
        return self.lat is not None


def _variants(name: str) -> tuple[set[str], set[str]]:
    """辞典を引く名前の形。`(そのままの形, 付け外しの形)`。

    **地図の見出しは空白で区切ることが多い**(「店名 支店名」)のに、手がかりは
    詰めて書かれる —— 詰めた形の 1 か所に空白を挟んだ形も引く(索引で引ける
    完全一致のまま、空白の揺れを拾うため)。
    """
    exact: set[str] = set()
    loose: set[str] = set()
    forms = {name.strip(), _nfkc(name)}
    forms |= {_plain(v).strip() for v in forms}
    # **支店名を外した形も付け外しで引く**(「店名 ◯◯店」と書かれても、辞典の
    # 見出しは店名だけのことがある)。外したあとは住所で確かめるので、名前だけでは採らない
    for value in list(forms):
        head, _, tail = value.rpartition(" ")
        if head.strip() and tail.endswith("店"):
            loose |= {head.strip(), _plain(head).strip()}
    for value in forms:
        if not value:
            continue
        exact.add(value)
        tight = _SPACE.sub("", value)
        exact.add(tight)
        if len(tight) <= MAX_SPACED_NAME_CHARS:
            for at in range(1, len(tight)):
                exact.add(tight[:at] + " " + tight[at:])
                exact.add(tight[:at] + "\u3000" + tight[at:])
        for suffix in LOOSE_SUFFIXES:
            if tight.endswith(suffix) and len(tight) > len(suffix):
                loose.add(tight[: -len(suffix)])
            else:
                loose |= {tight + suffix, tight + " " + suffix}
        # 「◯◯本店」と書かれた支店の見出しが「◯◯店」のことがある
        if tight.endswith("本店") and len(tight) > 2:
            branch = tight[:-2] + "店"
            loose.add(branch)
            if len(branch) <= MAX_SPACED_NAME_CHARS:
                loose |= {branch[:at] + " " + branch[at:] for at in range(1, len(branch))}
    return exact, {v for v in loose if v} - exact


def _rows_named(src, names: set[str]) -> list:
    """その名前(見出しか別名)を持つ文書。**辞典の都合で付いた見分けの札は別名で引ける**。"""
    if not names:
        return []
    ordered = sorted(names)
    rows: dict[int, object] = {}
    for at in range(0, len(ordered), 200):
        chunk = ordered[at:at + 200]
        marks = ",".join("?" * len(chunk))
        found = db.query(
            src.path,
            f"SELECT doc_id, title, extra FROM docs WHERE title IN ({marks})"
            f" UNION SELECT d.doc_id, d.title, d.extra FROM aliases a"
            f" JOIN docs d ON d.doc_id = a.doc_id WHERE a.alias IN ({marks})"
            f" LIMIT {MAX_CANDIDATES + 1}",
            (*chunk, *chunk),
            timeout=QUERY_TIMEOUT_SECONDS,
        )
        for row in found:
            rows.setdefault(row["doc_id"], row)
    return list(rows.values())


def _candidate(source: str, row, **flags) -> Candidate | None:
    extra = _json(row["extra"])
    try:
        lat, lon = float(extra["lat"]), float(extra["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    area = extra.get("area")
    return Candidate(
        source=source, title=row["title"], lat=lat, lon=lon, extra=extra,
        area=str(area) if area else None, **flags,
    )


def _json(raw) -> dict:
    import json

    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# **県の名前の一覧は辞典から読む**(題材の知識を持たない)。地図辞典の `area` は
# 都道府県相当の行政区で、その値の集まりが「住所の頭に来うる県の名前」になる。
# 焼き直さない限り変わらないので、ファイルの指紋で控える
_AREAS: dict[tuple, frozenset[str]] = {}
# **県として数える値は、いちばん多い値のこの割合以上の文書に付いているものだけ。**
# 店舗辞典の `area` は自由記述が混ざる(実データで「〒343-0828」「東京都渋」「東京都/日本」が
# それぞれ 1〜数件)。拾うと、比べる形で空になる「〒…」がどの住所の頭にも当たり、長い順に
# 選ぶので**すべての候補が「県が違う」で落ちた**(東京都の 8 件が全滅した)。
# 県なら数千〜数万件ある(最多の東京都が 9 万件・崩れた値は 1 桁)。1% にしても最少の県まで残る。件数を決め打ちに
# しないのは、小さな辞典(試験用・地域を絞った辞典)で県まで落とさないため
MIN_AREA_SHARE = 0.01


def known_areas(src) -> frozenset[str]:
    try:
        stat = src.path.stat()
        mark = (str(src.path), stat.st_ino, stat.st_mtime_ns)
    except (AttributeError, OSError):
        mark = (str(getattr(src, "path", "")),)
    if mark not in _AREAS:
        try:
            rows = db.query(
                src.path,
                "SELECT area, COUNT(*) AS n FROM docs WHERE area IS NOT NULL GROUP BY area",
                timeout=QUERY_TIMEOUT_SECONDS,
            )
            floor = max((r["n"] for r in rows), default=0) * MIN_AREA_SHARE
            # 比べる形で空になる値(空白だけ・郵便番号だけ)は、どの住所の頭にも当たるので入れない
            _AREAS[mark] = frozenset(
                str(r["area"]) for r in rows
                if r["area"] and r["n"] >= floor and address_key(r["area"])
            )
        except Exception as e:  # 読めなければ、県の名前は使わない(落とさない側へ倒す)
            log.warning("resolve: %s の県の一覧を読めませんでした: %s", src.path, type(e).__name__)
            _AREAS[mark] = frozenset()
    return _AREAS[mark]


def area_at(src, lat: float, lon: float) -> str | None:
    """その点の県を、**周りの地物の県**から読む(県を持たない辞典の点のため)。

    狭いほうから広げ、いちばん多い県を採る。**同じ数で並んだら決めない**
    (県境の上の点で、どちらかに倒すと取り違えが通る)。
    """
    for span in AREA_PROBES:
        try:
            rows = db.query(
                src.path,
                "SELECT d.area AS area FROM doc_coords c JOIN docs d ON d.doc_id = c.doc_id"
                " WHERE c.lat BETWEEN ? AND ? AND c.lon BETWEEN ? AND ?"
                " AND d.area IS NOT NULL LIMIT 200",
                (lat - span, lat + span, lon - span, lon + span),
                timeout=QUERY_TIMEOUT_SECONDS,
            )
        except Exception as e:
            log.warning("resolve: 点の県を読めませんでした: %s", type(e).__name__)
            return None
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["area"]] = counts.get(row["area"], 0) + 1
        if counts:
            ranked = sorted(counts.items(), key=lambda kv: -kv[1])
            if len(ranked) > 1 and ranked[0][1] == ranked[1][1]:
                return None
            return ranked[0][0]
    return None


def meters(a: Candidate, b: Candidate) -> float:
    """2 点の距離(m)。国 1 つぶんの距離では球の近似で足りる。"""
    rad = math.radians
    dlat = rad(b.lat - a.lat)
    dlon = rad(b.lon - a.lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(rad(a.lat)) * math.cos(rad(b.lat)) * math.sin(dlon / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(h))


# ---- 1 件を引き当てる -----------------------------------------------------------------


def clue_of(raw) -> dict:
    """AI が書いた手がかり(名前・住所・URL・辞典の見出し)。文字列だけなら名前とみなす。"""
    if isinstance(raw, str):
        raw = {"name": raw}
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key in ("name", "address", "url", "ref"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()[:MAX_CLUE_CHARS]
    return out


def resolve(spec: dict, clue: dict, area_hint: str | None, sources: dict, now: str) -> Result:
    """手がかり 1 つを引き当てる。引き当てられなければ、理由を持った店を返す。

    **店の辞典で引き当てられなければ、住所の辞典で引く**(`spec["address"]`。`_by_address`)。
    店の辞典に載っていない店(小さな店・直売所)を AI が見つけても、座標の置き場が無いまま
    だった —— AI が確かめた住所から、座標は機械で引く。引けなければ店の辞典の理由を残す。
    """
    result = _by_shops(spec, clue, area_hint, sources, now)
    if result.ok or not spec.get("address"):
        return result
    return _by_address(spec, clue, area_hint, sources, now) or result


def _by_shops(spec: dict, clue: dict, area_hint: str | None, sources: dict, now: str) -> Result:
    """店の辞典(`spec["sources"]`)で引き当てる。"""
    name = clue.get("name") or ""
    if not name:
        return _failed(clue, "店の名前がありません", [], now)
    readable = [(one, sources.get(one["source"])) for one in spec["sources"]]
    readable = [(one, src) for one, src in readable if src is not None]
    if not readable:
        return _failed(clue, "照らす辞典がまだありません(取り込まれていない)", [], now)

    exact, loose = _variants(name)
    want = name_key(name)
    ref_source, _, ref_title = (clue.get("ref") or "").partition(":")
    found: list[Candidate] = []
    crowded = []
    for one, src in readable:
        rows = _rows_named(src, exact | loose)
        if len(rows) > MAX_CANDIDATES:
            crowded.append(one["source"])
            rows = rows[:MAX_CANDIDATES]
        by_ref = set()
        if ref_source == one["source"] and ref_title:
            ref_rows = db.query(
                src.path, "SELECT doc_id, title, extra FROM docs WHERE title = ?", (ref_title,),
                timeout=QUERY_TIMEOUT_SECONDS,
            )
            by_ref = {r["doc_id"] for r in ref_rows}
            known = {r["doc_id"] for r in rows}
            rows += [r for r in ref_rows if r["doc_id"] not in known]
        for row in rows:
            key = name_key(base_name(row["title"]))
            same = key == want or name_key(row["title"]) == want
            close = not same and key in {name_key(v) for v in loose}
            if not (same or close or row["doc_id"] in by_ref):
                continue
            if (cand := _candidate(
                one["source"], row, by_ref=row["doc_id"] in by_ref, loose=not same,
            )) is not None:
                found.append(cand)
    if not found:
        return _failed(clue, "辞典に同じ名前がありません", [], now)

    # **県を確かめる相手**。手がかりの住所の頭(辞典が知っている県の名前で読む)、
    # 無ければ 1 件の県のタグ。**代表の場所が 1 件の県の外にあることはある**ので、
    # 住所が書いてあればそちらを信じる
    area_sources = [src for _, src in readable if known_areas(src)]
    areas = frozenset().union(*(known_areas(src) for src in area_sources)) if area_sources else frozenset()
    address = address_key(clue.get("address") or "")
    target = next((a for a in sorted(areas, key=len, reverse=True)
                   if address.startswith(address_key(a))), None) if address else None
    target = target or area_hint
    for cand in found:
        _check(cand, target, address, clue, area_sources)

    alive = [c for c in found if not c.why_not]
    clusters = _clusters(alive, spec["agree_meters"])
    strong = [g for g in clusters if _is_strong(g)]
    if len(strong) > 1:
        pinned = [g for g in strong if any(c.by_ref for c in g)]
        if len(pinned) == 1:
            strong = pinned
    if len(strong) == 1:
        return _resolved(spec, strong[0], clue, now)
    if len(strong) > 1:
        return _failed(
            clue, f"同じ名前の確かな候補が {len(strong)} か所あり、決めきれません",
            [g[0] for g in strong], now,
        )
    # **確かな束が無いときは、県だけで採れるのは「名前の合う場所がそこだけ」のとき**。
    # 束が 2 つ以上あれば(県の分からない束も含めて)、どれが本物かを言えない
    if len(clusters) == 1 and any(c.area_ok for c in clusters[0]) \
            and not all(c.loose for c in clusters[0]):
        return _resolved(spec, clusters[0], clue, now)
    # **AI が辞典を見て指した見出し(`ref`)は、名前の書き方が違っても採る** —— 候補が
    # その 1 か所だけで、県が合い、名前が大きく重なるとき。辞典の側に住所の無い点
    # (地図辞典の直売所・小さな店に多い)は、名前を寄せると住所でも辞典どうしでも
    # 確かめようがなく、**AI が辞典で見つけて指した店まで落ちていた**(運営の名前を
    # 頭に付けた正式名と、看板の名前だけの見出し)
    if len(clusters) == 1 and any(
        c.by_ref and c.area_ok and _named_alike(base_name(c.title), name) for c in clusters[0]
    ):
        return _resolved(spec, clusters[0], clue, now, pointed=True)
    more = f"(候補が多すぎて {'・'.join(crowded)} は先頭の {MAX_CANDIDATES} 件だけ見ました)" \
        if crowded else ""
    if len(clusters) > 1:
        return _failed(
            clue, f"同じ名前の候補が {len(clusters)} か所あり、住所で絞れません{more}",
            [g[0] for g in clusters], now,
        )
    rejected = [c for c in found if c.why_not]
    if not alive:
        return _failed(clue, f"名前の合う候補が、どれも住所と合いません{more}", rejected, now)
    return _failed(
        clue, f"住所が無いか読めず、辞典どうしも一致しないので確かめられません{more}",
        alive + rejected, now,
    )


def _check(cand: Candidate, target: str | None, address: str, clue: dict, area_sources) -> None:
    """候補 1 つを手がかりと突き合わせる(食い違えば `why_not` に書く)。"""
    if target:
        # 候補の県が一覧に無い崩れた値(自由記述の「東京都/日本」など)なら、持っていないものとして
        # 周りの地物から読み直す —— そのまま比べると、同じ県の店が「県が違う」で落ちる
        known = {address_key(a) for src in area_sources for a in known_areas(src)}
        if cand.area is not None and known and address_key(cand.area) not in known:
            cand.area = None
        if cand.area is None:
            # 県を持たない辞典の点は、周りの地物の県で読む
            for src in area_sources:
                if area := area_at(src, cand.lat, cand.lon):
                    cand.area = area
                    break
        if cand.area:
            cand.area_ok = address_key(cand.area) == address_key(target)
            if not cand.area_ok:
                cand.why_not = f"県が違う({cand.area})"
                return
    if address:
        locality = str(cand.extra.get("locality") or "")
        if locality and not _JAPANESE.search(locality):
            # **ローマ字の町の名前(「Nagoya-shi」)は持っていないものとして扱う。** 手がかりの
            # 住所は日本語なので比べようがなく、比べると同じ町の店が「市区町村が違う」で落ちる
            # (店舗辞典の 1 つは、一部の県で町の名前をローマ字で持っている)
            locality = ""
        if locality:
            here = address_key(locality)
            # 郡を省いて書く住所がある(「〇〇郡〇〇町」を「〇〇町」と書く)
            short = re.sub(r"^.+?郡", "", here)
            cand.city_ok = here in address or bool(short and short in address)
            if not cand.city_ok:
                cand.why_not = f"市区町村が違う({locality})"
                return
        if theirs := cand.extra.get("address"):
            cand.block_ok = _same_block(address_key(theirs), address, locality)
            if cand.block_ok is False:
                cand.why_not = f"番地が違う({theirs})"
                return
    if (url := clue.get("url")) and (site := cand.extra.get("website")):
        cand.site_ok = _site_key(url) == _site_key(site) or None


def _same_block(theirs: str, ours: str, locality: str) -> bool | None:
    """番地が同じか。読めなければ None(「違う」とは言わない)。

    **町の名前で位置を合わせてから、丁目・番地の数の並びを比べる** —— 数だけを
    比べると、別の町の同じ番地が通る。辞典の住所の頭(町の名前まで)が手がかりの
    住所に入っていなければ、比べようがないので None。
    """
    if locality:
        theirs = theirs.replace(address_key(locality), "")
    numbers = re.findall(r"\d+", theirs)
    head = re.match(r"\D+", theirs)
    town = head.group(0).strip("-") if head else ""
    if len(numbers) < 2 or not town or town not in ours:
        return None
    mine = re.findall(r"\d+", ours.split(town, 1)[1])
    common = min(len(mine), len(numbers))
    if common < 2:
        return None
    # **3 つ目(丁目-番-号 の号)の違いは許す。** 同じ番の区画の中で、建物の入口や
    # 区画の割り方で号を書き分けることがあり、数十 m の内に収まる
    common = min(common, 2) if common >= 3 else common
    return mine[:common] == numbers[:common]


def _clusters(cands: list[Candidate], within: float) -> list[list[Candidate]]:
    """近い候補を束ねる(つながったものは 1 つの束)。束は「同じ場所」の意味。"""
    groups: list[list[Candidate]] = []
    for cand in cands:
        joined = [g for g in groups if any(meters(cand, other) <= within for other in g)]
        merged = [cand]
        for g in joined:
            merged.extend(g)
            groups.remove(g)
        groups.append(merged)
    for g in groups:
        if len({c.source for c in g}) > 1:
            for c in g:
                c.agrees = True
    return groups


def _is_strong(group: list[Candidate]) -> bool:
    """確かな束か。市区町村・番地・サイトのどれかが合うか、辞典どうしが一致するか。
    **付け外しで寄せた名前だけの束は、辞典どうしの一致だけでは採らない**。"""
    if any(c.strong for c in group):
        return True
    if any(c.agrees for c in group):
        return not all(c.loose for c in group)
    return False


def _named_alike(theirs: str, ours: str) -> bool:
    """2 つの名前が大きく重なるか(続いた共通の字で見る。`MIN_SHARED_NAME_*`)。"""
    a, b = name_key(theirs), name_key(ours)
    if not a or not b:
        return False
    short, long_ = sorted((a, b), key=len)
    # 短いほうがまるごと入っていれば重なりの字数は問わない(「鷹屋」と「鷹屋製菓」)
    if len(short) >= 2 and short in long_:
        return True
    match = SequenceMatcher(None, a, b, autojunk=False).find_longest_match(0, len(a), 0, len(b))
    return match.size >= MIN_SHARED_NAME_CHARS and match.size >= MIN_SHARED_NAME_RATIO * len(short)


def _resolved(
    spec: dict, group: list[Candidate], clue: dict, now: str, pointed: bool = False,
) -> Result:
    order = {one["source"]: n for n, one in enumerate(spec["sources"])}
    group = sorted(group, key=lambda c: (order.get(c.source, 99), not c.by_ref, not c.strong))
    chosen = group[0]
    identity_of = {one["source"]: one for one in spec["sources"]}
    basis = [BASIS_NAME]
    if any(c.area_ok for c in group):
        basis.append(BASIS_AREA)
    if any(c.city_ok for c in group):
        basis.append(BASIS_CITY)
    if any(c.block_ok for c in group):
        basis.append(BASIS_BLOCK)
    if any(c.site_ok for c in group):
        basis.append(BASIS_SITE)
    if any(c.agrees for c in group):
        basis.append(BASIS_AGREE)
    if all(c.loose for c in group):
        basis.append(BASIS_LOOSE)
    if pointed:
        basis.append(BASIS_REF)
    shop: dict = {
        "name": base_name(chosen.title),
        "ref": f"{chosen.source}:{chosen.title}",
        "source": chosen.source,
    }
    keys = []
    contributors = [chosen.source]
    for cand in group:
        one = identity_of.get(cand.source) or {}
        key = extraction.record_key(
            {"source": cand.source, "identity": one.get("identity")}, cand.extra,
        )
        if key and key not in keys:
            keys.append(key)
    if keys:
        shop["identity"] = keys
    for key in spec["carry"]:
        for cand in group:
            value = cand.extra.get(key)
            if isinstance(value, (str, int, float)) and not isinstance(value, bool) and str(value).strip():
                shop[key] = value
                if cand.source not in contributors:
                    contributors.append(cand.source)
                break
    if chosen.area:
        shop["area"] = chosen.area
    # **値を写した辞典を並べる**(出どころのタグはここから付ける)。辞典ごとに
    # ライセンスが違うので、座標の辞典だけでなく、電話やサイトを写した辞典も残す
    shop.update({"sources": contributors, "basis": basis, "clue": clue, "resolved_at": now})
    return Result(shop=shop, lat=chosen.lat, lon=chosen.lon)


def _by_address(
    spec: dict, clue: dict, area_hint: str | None, sources: dict, now: str,
) -> Result | None:
    """住所の辞典で、手がかりの住所から街区の代表点を引く。引けなければ None。

    **街区(番・地番)まで当たったときだけ採る** —— 町の名前までしか当たらない住所は、
    郊外では数 km 離れた点になりうる。鍵の末尾の数を 1 つずつ落としながら、いちばん
    細かく当たる鍵を探す(`…-1-31-12` の号は辞典に無いので `…-1-31` で当たる)。
    **1 つの街区に決まらなければ採らない**(同じ鍵が 2 つの街区を指すとき)。
    住所に都道府県が無ければ、1 件の県を頭に付けて引く。
    """
    address = str(clue.get("address") or "")
    one = spec["address"]
    src = sources.get(one["source"])
    if not address or src is None:
        return None
    key = address_block_key(address)
    if area_hint and not key.startswith(address_block_key(area_hint)) and not _has_prefecture(key):
        key = address_block_key(area_hint) + key
    found = re.match(r"^(.*?[^\d-])((?:-\d+)+)", key)
    if not found:
        return None
    town, numbers = found.group(1), found.group(2).strip("-").split("-")
    for size in range(len(numbers), 0, -1):
        wanted = f"{town}-{'-'.join(numbers[:size])}"
        rows = _rows_named(src, {wanted})
        if len(rows) > 1:
            return None
        if rows and (cand := _candidate(one["source"], rows[0])) is not None:
            shop = {
                "name": clue.get("name") or "",
                "ref": f"{one['source']}:{cand.title}",
                "source": one["source"],
                "address": cand.title,
                "area": cand.extra.get("area") or "",
                "sources": [one["source"]],
                "basis": [BASIS_ADDRESS],
                "clue": clue,
                "resolved_at": now,
            }
            if version := cand.extra.get("version"):
                shop["version"] = version
            return Result(shop={k: v for k, v in shop.items() if v != ""}, lat=cand.lat, lon=cand.lon)
    return None


_PREFECTURE_HEAD = re.compile(r"^.{2,3}?[都道府県]")


def _has_prefecture(key: str) -> bool:
    return bool(_PREFECTURE_HEAD.match(key))


# 住所の鍵(**`ingest/sources/isj.py` の `block_key` と同じ形にすること**。ingest と app は
# 別のイメージで互いを import できない。食い違うと引けなくなるので、両方を通すテストがある)
_DASHES = "‐‑‒–—―−ーｰ"


def address_block_key(text) -> str:
    """住所を街区の鍵の形にする(`東京都千代田区丸の内一丁目9番` → `東京都千代田区丸の内-1-9`)。"""
    value = _SPACE.sub("", _nfkc(text))
    value = _POSTCODE.sub("", value)
    value = value.replace("ヶ", "ケ").replace("ヵ", "カ")
    value = re.sub(r"([〇一二三四五六七八九十]+)(丁目)", _kanji_number, value)
    value = re.sub(rf"(\d)[{_DASHES}](?=\d)", r"\1-", value)
    value = re.sub(r"(\d)の(?=\d)", r"\1-", value)
    value = re.sub(r"(丁目|番地|番|号)", "-", value)
    value = re.sub(r"([^\d-])(?=\d)", r"\1-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def _failed(clue: dict, why: str, cands: list[Candidate], now: str) -> Result:
    shop = {"clue": clue, "unresolved": why, "checked_at": now}
    if cands:
        shop["candidates"] = [c.label() for c in cands[:MAX_SHOWN_CANDIDATES]]
    if clue.get("name"):
        shop["name"] = clue["name"]
    return Result(shop=shop)


# ---- 集めた 1 件ずつに掛ける -----------------------------------------------------------


def is_resolved(shop) -> bool:
    return isinstance(shop, dict) and bool(shop.get("resolved_at")) and not shop.get("unresolved")


def _same_clue(clue: dict, before: dict) -> bool:
    """AI が返した手がかりが、前に引き当てたものと同じ店を指しているか。

    **引き当てた名前・見出し・住所を写して返してきたときも同じとみなす** —— AI は
    渡された中身をそのまま返すことが多く、それを「手がかりが変わった」と読むと、
    何も起きていない回に引き直すことになる。
    """
    if not clue.get("name"):
        return False
    prev = before.get("clue") if isinstance(before.get("clue"), dict) else {}
    names = {name_key(before.get("name")), name_key(prev.get("name"))} - {""}
    if name_key(clue["name"]) not in names:
        return False
    if (ref := clue.get("ref")) and ref not in {before.get("ref"), prev.get("ref")}:
        return False
    if address := clue.get("address"):
        known = {address_key(before.get("address")), address_key(prev.get("address"))} - {""}
        if known and address_key(address) not in known:
            return False
    return True


def apply(
    spec: dict, items: list, previous: dict[str, dict], sources: dict, now: str,
    valid=None,
) -> tuple[list, dict]:
    """AI が返した 1 件ずつの手がかりを引き当て、焼く側へ渡す形にする。控えも返す。

    - `previous` は見出し → 今の脇書き(直す回で、前に引き当てた店を引き継ぐため)
    - `valid(鍵, 値)` は、辞典から写す値(`to_extra`)が書式に合うか(`app/formats.py`)。
      **合わない辞典の値は写さない**(AI の書いた値が残る)

    1 件ごとに:

    - **AI の座標は捨てる**(`ai_coords` を立てていなければ)
    - 手がかりが書いてあれば引き当てる。前と同じ店なら引き当て直さない
    - 手がかりに null が書いてあれば、店と座標を外す(代表の店を選び直すとき)
    - 書いてなければ、前の店のまま(座標も動かない)
    - 出どころのタグ・引き当てられなかった印のタグは、**店の状態から付け直す**
      (AI がタグを書き直して落としても戻る)
    """
    clue_key = spec["clue"]
    marks = source_tags(spec)
    failed_tag = spec["failed_tag"]
    report = {"resolved": 0, "unresolved": 0, "kept": 0, "cleared": 0, "failures": []}
    out = []
    for raw in items:
        if not isinstance(raw, dict) or any(
            str(t).strip() == notes.TOMBSTONE_TAG for t in (raw.get("tags") or [])
        ):
            out.append(raw)
            continue
        raw = dict(raw)
        extra = dict(raw["extra"]) if isinstance(raw.get("extra"), dict) else {}
        if not spec["ai_coords"]:
            raw.pop("lat", None)
            raw.pop("lon", None)
            extra.pop("lat", None)
            extra.pop("lon", None)
        title = notes.title_key(raw.get("title"))
        before_extra = previous.get(title) or {}
        before = before_extra.get(clue_key)
        before = before if isinstance(before, dict) else None
        facts: dict = {}
        state = before
        changed = False
        if clue_key in extra:
            given = extra.pop(clue_key)
            clue = clue_of(given)
            if given is None:
                # **null は「店を外す」**。座標も一緒に外す(店を外した 1 件に、前の店の
                # 座標が残ると、地図には前の店の場所に立ち続ける)
                facts = {clue_key: None, "lat": None, "lon": None}
                state, changed = None, True
                report["cleared"] += 1
            elif not clue:
                # 手がかりとして読めないもの(空のオブジェクトなど)は書かなかったのと同じ。
                # **外す印にはしない** —— 崩れた答えの回に、引き当てた店まで消えないように
                pass
            elif before is not None and is_resolved(before) and _same_clue(clue, before):
                report["kept"] += 1
            else:
                result = resolve(spec, clue, _area_hint(spec, raw), sources, now)
                state, changed = result.shop, True
                facts = {clue_key: result.shop, "lat": result.lat, "lon": result.lon}
                if result.ok:
                    report["resolved"] += 1
                else:
                    report["unresolved"] += 1
                    report["failures"].append({"title": title, "reason": result.shop["unresolved"]})
        for source_key, into in spec["to_extra"].items():
            value = state.get(source_key) if is_resolved(state) else None
            if value is not None and (valid is None or valid(into, value)):
                # **辞典の値が先**。AI の書いた値は、辞典に無いときだけ残る
                facts[into] = value
            elif changed and before is not None and before.get(source_key) is not None \
                    and before_extra.get(into) == before.get(source_key) and into not in extra:
                # 前の店から写した値は、店が変わったら外す(別の店の営業時間が残らないように)
                facts[into] = None
        tags = raw.get("tags")
        if isinstance(tags, list) or state is not None or changed:
            raw["tags"] = _machine_tags(list(tags or []), state, marks, failed_tag, spec)
        raw["extra"] = extra
        if facts:
            raw[FACTS_KEY] = facts
        out.append(raw)
    return out, report


def _area_hint(spec: dict, raw: dict) -> str | None:
    head = spec["area_tag"]
    if not head:
        return None
    for tag in raw.get("tags") or []:
        text = str(tag).strip()
        if text.startswith(head) and text[len(head):].strip():
            return text[len(head):].strip()
    return None


def _machine_tags(tags: list, state, marks: set[str], failed_tag: str, spec: dict) -> list:
    """出どころのタグと、引き当てられなかった印のタグを、店の状態から付け直す。"""
    out = [t for t in tags if str(t).strip() not in marks]
    if is_resolved(state):
        by_source = {one["source"]: one["tag"] for one in _tagged_sources(spec)}
        for tag in [by_source.get(s) for s in (state.get("sources") or [state.get("source")])]:
            if tag and tag not in out:
                out.append(tag)
    return _state_tags(out, state, spec)


def _state_tags(tags: list, state, spec: dict) -> list:
    """`failed_tag` と `none_tag` を、店の状態と「無かった」の印から付け直す。

    - 引き当てた 1 件 —— どちらも外す(店が見つかったのに「無い」が残ると食い違う)
    - 「無かった」の印が付いた 1 件 —— `failed_tag` を外す(まだ決まっていないのではなく、
      探したうえで無かった。数える側が「まだ」と取り違えない)
    - 引き当てに失敗した 1 件 —— `failed_tag` を付ける
    - 手がかりがまだ無い 1 件 —— `failed_when_missing` のときだけ付ける

    **それ以外の形は触らない**(どちらの指定も無い収集で、AI が書いた印を動かさない)。
    """
    failed_tag = spec["failed_tag"]
    none_tag = spec.get("none_tag") or ""
    out = list(tags)
    if is_resolved(state):
        drop = {failed_tag, none_tag} - {""}
        return [t for t in out if str(t).strip() not in drop]
    if none_tag and none_tag in out:
        return [t for t in out if str(t).strip() != failed_tag] if failed_tag else out
    missing = not isinstance(state, dict) or not (state.get("unresolved") or state.get("clue"))
    if failed_tag and failed_tag not in out and (
        (isinstance(state, dict) and state.get("unresolved"))
        or (missing and spec.get("failed_when_missing"))
    ):
        out.append(failed_tag)
    return out


def settle(spec: dict, doc: dict) -> dict:
    """焼く 1 件ずつに、`failed_tag` と `none_tag` を状態から付け直す(`collect.stream_docs`)。

    `apply` が触るのは **AI がその回に返した 1 件だけ**なので、返らなかった 1 件の印は
    前のまま残る —— 手がかりのまだ無い 1 件に `failed_tag` を付ける指定
    (`failed_when_missing`)を後から足すと、名簿の大半は印を持たないまま、
    数える側と区画の足止めから漏れる。**全部の 1 件に同じ決まりを当てる**。

    どちらの指定も無ければ何もしない(今までの収集の焼き上がりを動かさない)。
    **消えたものには触らない**(地図にも数にも出ないので、印を付け直す意味が無い)。
    """
    if not (spec.get("failed_when_missing") or spec.get("none_tag")):
        return doc
    tags = doc.get("tags") or []
    if notes.REMOVED_TAG in tags:
        return doc
    extra = doc.get("extra") if isinstance(doc.get("extra"), dict) else {}
    settled = _state_tags(list(tags), extra.get(spec["clue"]), spec)
    return doc if settled == list(tags) else {**doc, "tags": settled}


def unresolved_of(extra) -> str:
    """その 1 件の店が引き当てられなかった理由(引き当てていれば空)。"""
    if not isinstance(extra, dict):
        return ""
    for value in extra.values():
        if isinstance(value, dict) and value.get("unresolved") and "clue" in value:
            return str(value["unresolved"])
    return ""
