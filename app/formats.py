"""脇書きの値の書式 —— AI が書く・辞典から運ぶ値を、**決まった形のものだけ通す**。

## 何のためか

集めた 1 件の脇書き(`extra`)は、読む側がそのまま機械で読む値を持つことがある ——
提供される月(「いまは時期外か」を書き出すときに機械で決める)や営業時間がそれ。
**AI は書式を守ったつもりで崩す**(「11月〜3月」「冬季」「11時〜14時」)。崩れた値が
読む側まで届くと、読む側ごとに受け止めを書くことになり、しかも読めなかった 1 件は
黙って「通年」や「営業時間なし」に化ける。**確かめられるのは焼く側**なので、
ここで書式に合わないものを落とし、落とした理由を控えに残す。

## Chiezo は値の意味を知らない

どの鍵をどの書式で読むかは収集の定義(`formats`)が書く —— `{"available_months":
"months", "opening_hours": "opening_hours"}`。こちらが持つのは書式の種類だけで、
鍵の名前も、その値を何に使うかも知らない(`app/extract.py` と同じ線)。

書式の種類:

- `months` …… 月の並び。`11-3`(11 月から 3 月まで。年をまたいでよい)・`6,7,8`・
  `6-8,12` のように、1〜12 の数とその範囲をカンマで並べる。全角の数字・`〜`・`、`・
  「月」は寄せてから読む。**空(と「通年」)は「書かない」の意味**で、鍵を落とす
- `opening_hours` …… OpenStreetMap の `opening_hours` の書式(`Mo-Fr 11:00-14:00;
  Sa off` のような)。**文法を全部は追わない** —— 使われている語(曜日・月・祝日・
  `off` など)と記号と時刻だけでできているか、時刻が読める値か、を見る。
  日本語の説明文(「11時〜14時」)や、時刻の形が崩れたものが落ちる
"""
from __future__ import annotations

import re
import unicodedata

from fastapi import HTTPException

KIND_MONTHS = "months"
KIND_OPENING_HOURS = "opening_hours"
KINDS = (KIND_MONTHS, KIND_OPENING_HOURS)

# 書式を確かめる鍵の数。読む側が機械で読む値は数個で足りる
MAX_FORMAT_KEYS = 10
MAX_KEY_CHARS = 40
# OSM の値の長さの上限と同じ(タグの値は 255 文字まで)
MAX_OPENING_HOURS_CHARS = 255
# 控えに残す、落とした値の長さ
MAX_SHOWN_VALUE_CHARS = 60


def _bad(message: str) -> HTTPException:
    return HTTPException(400, {"error": f"formats が読めません: {message}"})


def normalize(raw) -> dict[str, str]:
    """`{"脇書きの鍵": "書式の種類"}` を均す。**読めない指定は断る**
    (黙って無視すると、確かめているつもりの値が確かめられないまま入る)。"""
    if raw in (None, "", {}):
        return {}
    if not isinstance(raw, dict):
        raise _bad("「脇書きの鍵 → 書式の種類」のオブジェクトで書いてください")
    if len(raw) > MAX_FORMAT_KEYS:
        raise _bad(f"書ける鍵は {MAX_FORMAT_KEYS} 個までです")
    out: dict[str, str] = {}
    for key, kind in raw.items():
        name = str(key or "").strip()
        if not name or len(name) > MAX_KEY_CHARS:
            raise _bad(f"鍵は 1〜{MAX_KEY_CHARS} 文字で書いてください: {key!r}")
        if kind not in KINDS:
            raise _bad(f"書式は {' / '.join(KINDS)} のどれか(「{name}」はいま {kind!r})")
        out[name] = kind
    return out


def check(kind: str, value) -> tuple[str | None, str]:
    """1 つの値を確かめる。`(通す値, 落とした理由)` を返す。

    - 通るなら `(均した値, "")`
    - **「書かない」の意味の値(空・「通年」)なら `(None, "")`**(鍵を落とす)
    - 書式に合わなければ `(None, 理由)`
    """
    if value is None:
        return None, ""
    if kind == KIND_MONTHS:
        return months(value)
    if kind == KIND_OPENING_HOURS:
        return opening_hours(value)
    return None, f"知らない書式です: {kind}"


# ---- 月の並び ------------------------------------------------------------------

_MONTH_PART = re.compile(r"(\d{1,2})(?:-(\d{1,2}))?")
# 書いたままでは読めない言い方。**「通年」だけは意味が決まっている**(鍵を落とす)
_ALL_YEAR = {"通年", "全年", "年中"}


def months(value) -> tuple[str | None, str]:
    """提供される月の並び(`11-3` / `6,7,8`)。均した値か、落とした理由を返す。

    **並べた順は保つ**(`11-3` を `11,12,1,2,3` に開かない)—— 書いた側が
    「冬の間」と読める形のまま残すため。読む側は範囲を開いて月の集合として使う。
    数として読めないものは通さない(「冬季」は月を言っていないので、読む側には
    時期外かどうかが決められない)。
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None, "文字列で書いてください"
    text = unicodedata.normalize("NFKC", str(value)).strip()
    for wave in ("〜", "~", "–", "—", "ー"):
        text = text.replace(wave, "-")
    text = text.replace("、", ",").replace("月", "")
    text = re.sub(r"\s+", "", text)
    if not text or text in _ALL_YEAR:
        return None, ""
    parts = []
    for part in text.split(","):
        found = _MONTH_PART.fullmatch(part)
        if not found:
            return None, f"月の並びとして読めません({_shown(value)})"
        first = int(found.group(1))
        last = int(found.group(2)) if found.group(2) else None
        if not 1 <= first <= 12 or (last is not None and not 1 <= last <= 12):
            return None, f"月は 1〜12 で書いてください({_shown(value)})"
        parts.append(f"{first}-{last}" if last is not None and last != first else str(first))
    return ",".join(parts), ""


def month_set(value: str) -> set[int]:
    """均した月の並びを、月の集合に開く(`11-3` → {11, 12, 1, 2, 3})。"""
    out: set[int] = set()
    for part in (value or "").split(","):
        found = _MONTH_PART.fullmatch(part.strip())
        if not found:
            continue
        first = int(found.group(1))
        last = int(found.group(2)) if found.group(2) else first
        month = first
        while True:
            out.add(month)
            if month == last:
                break
            month = month % 12 + 1
    return out


# ---- 営業時間(OSM の opening_hours) ---------------------------------------------

# 書式の中に出てくる語。**ここに無い英字の語は通さない**(説明文が混ざった値を落とす)
_OH_WORDS = {
    # 曜日・祝日・学校の休み
    "mo", "tu", "we", "th", "fr", "sa", "su", "ph", "sh",
    # 月
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    # 状態
    "off", "closed", "open", "unknown",
    # 日の出入りと周期
    "sunrise", "sunset", "dawn", "dusk", "week", "easter", "day", "days",
}
_OH_TOKEN = re.compile(
    r"""
    (?P<space>\s+)
  | (?P<comment>"[^"]*")
  | (?P<always>24/7)
  | (?P<time>\d{1,2}:\d{2}\+?)
  | (?P<number>\d+)
  | (?P<word>[A-Za-z]+)
  | (?P<mark>\|\||[;,\-:\[\]+/()])
    """,
    re.VERBOSE,
)


def opening_hours(value) -> tuple[str | None, str]:
    """OSM の `opening_hours` の書式か。均した値(前後の空白を落とすだけ)か、落とした理由。

    **文法を全部は追わない。** 規格は大きく、正しく読むには専用の読み手が要る ——
    ここで止めたいのは**書式の外の値**(日本語の説明文・`11時〜14時`・時刻の崩れ)で、
    それは使える語と記号と時刻だけでできているかで十分に見分けられる。
    時刻は 0:00〜48:00(翌日にかかる営業を書ける上限)で、分は 59 まで。
    """
    if not isinstance(value, str):
        return None, "文字列で書いてください"
    text = value.strip()
    if not text:
        return None, ""
    if len(text) > MAX_OPENING_HOURS_CHARS:
        return None, f"{MAX_OPENING_HOURS_CHARS} 文字を超えています"
    at = 0
    meaningful = False
    while at < len(text):
        found = _OH_TOKEN.match(text, at)
        if not found:
            return None, f"営業時間の書式(OSM)で読めない文字があります({_shown(value)})"
        kind = found.lastgroup
        token = found.group(0)
        if kind == "word" and token.lower() not in _OH_WORDS:
            return None, f"営業時間の書式(OSM)に無い語です: {token}"
        if kind == "time":
            hour, minute = token.rstrip("+").split(":")
            if int(hour) > 48 or int(minute) > 59 or (int(hour) == 48 and int(minute)):
                return None, f"時刻として読めません: {token}"
        if kind in ("time", "always", "word"):
            meaningful = True
        at = found.end()
    if not meaningful:
        return None, f"曜日・時刻・状態のどれも書かれていません({_shown(value)})"
    return text, ""


def _shown(value) -> str:
    text = str(value)
    return text if len(text) <= MAX_SHOWN_VALUE_CHARS else text[:MAX_SHOWN_VALUE_CHARS] + "…"


# ---- 集めた 1 件に掛ける ----------------------------------------------------------


def apply(formats: dict[str, str], items: list, facts_key: str) -> tuple[list, list[dict]]:
    """AI が返した 1 件ずつの脇書きを確かめる。**合わない値は落とし、理由を返す**。

    確かめるのは AI が書いた `extra` と、機械が運んだ値(`facts_key` の下。辞典から
    運んだ営業時間など)の両方。**墓標は確かめない**(消すための 1 件で、値を読まない)。
    落とした値は次の焼きで入らないだけで、**前から持っている値は消さない** ——
    AI が崩した値を書いてきた回に、前の正しい値まで消すと一歩後退になる。
    """
    if not formats:
        return items, []
    dropped: list[dict] = []
    out = []
    for raw in items:
        if not isinstance(raw, dict):
            out.append(raw)
            continue
        title = str(raw.get("title") or "")
        changed = dict(raw)
        for holder in ("extra", facts_key):
            extra = raw.get(holder)
            if not isinstance(extra, dict):
                continue
            kept = dict(extra)
            for key, kind in formats.items():
                if key not in kept or kept[key] is None:
                    continue
                value, why = check(kind, kept[key])
                if value is not None:
                    kept[key] = value
                elif why:
                    dropped.append({
                        "title": title, "key": key, "value": _shown(kept[key]), "reason": why,
                        "from": "辞典" if holder == facts_key else "AI",
                    })
                    kept.pop(key)
                else:
                    # 「書かない」の意味の値は、鍵を消す印(null)にする
                    kept[key] = None
            changed[holder] = kept
        out.append(changed)
    return out, dropped
