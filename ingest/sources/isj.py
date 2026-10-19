"""日本の住所の位置アダプタ(国土交通省「街区レベル位置参照情報」)。

住所の「◯◯町△丁目□番」(住居表示の街区)や「◯◯町□番地」(地番)ごとに、代表点の
緯度・経度を持つ公開データ。**住所から座標を引く辞典**として持つ —— 店の辞典(地図辞典・
店舗辞典)に載っていない店でも、AI が確かめた住所から機械で座標を引けるようにするため
(`app/resolve.py` の引き当ての最後の段)。1 街区 = 1 文書。

- 取得は都道府県ごとの ZIP(`<県番号>000-<版>.zip`。中は Shift_JIS の CSV 1 本)。
  **版は年に 1 度上がる**(令和 7 年度 = `24.0a`)ので決め打ちにせず、知っている版から
  先を HEAD で確かめていちばん新しいものを使う(`ISJ_VERSION` で固定もできる)
- **代表の行だけを採る**(`代表フラグ` が 1)。同じ街区に地番ごとの行が並ぶことがあり、
  全部採ると 3 割近く増えるうえ、引く側は街区の 1 点しか要らない
- 見出しは人が読む形(`東京都千代田区丸の内一丁目9`)。**引く鍵は別名に置く**
  (`block_keys`)—— 数字を半角にし、丁目・番地・番・号を `-` に、町の名前と数字の
  あいだにも `-` を入れた形(`東京都千代田区丸の内-1-9`)。小字・`大字`・郡を省いた形も
  別名に足す(住所の書き方の揺れ)
- 脇書き(`extra`)は `lat` / `lon` / `feature`(`isj=block`)/ `area`(都道府県)/
  `residential`(住居表示の地区か)/ `version`(版)。タグは `<県><市区町村>` の 1 つ

**出典の書き方が決まっている**(国土交通省「位置参照情報利用約款」第 2 条 3):
加工して使うときは「街区レベル位置参照情報 国土交通省」と整備年(版)を添える。
読む側(座標を地図に出すアプリ)がそう書けるよう、版を脇書きに残す。
**都市計画区域の外は入っていない**(街区レベルの整備範囲がそこまで)。
"""
from __future__ import annotations

import csv
import io
import logging
import os
import re
import unicodedata
import urllib.error
import urllib.request
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

from download import USER_AGENT
from download import fetch as _download

from core import Doc, check_stop

log = logging.getLogger("chiezo.ingest")

BASE_URL = "https://nlftp.mlit.go.jp/isj/dls/data"
# 知っているいちばん新しい版(令和 7 年度)。ここから先を確かめる
KNOWN_VERSION = 24
# 先の版をいくつまで確かめるか(年 1 度しか上がらないので、数年ぶんあれば足りる)
LOOKAHEAD_VERSIONS = 5
PREFECTURE_CODES = [f"{n:02d}" for n in range(1, 48)]

# CSV の列(1 行目の見出しで引く。並びが変わっても読めるように)
COL_PREF = "都道府県名"
COL_CITY = "市区町村名"
COL_TOWN = "大字・丁目名"
COL_KOAZA = "小字・通称名"
COL_BLOCK = "街区符号・地番"
COL_LAT = "緯度"
COL_LON = "経度"
COL_RESIDENTIAL = "住居表示フラグ"
COL_REPRESENTATIVE = "代表フラグ"


def version_name(major: int) -> str:
    return f"{major}.0a"


def zip_url(version: str, pref_code: str) -> str:
    return f"{BASE_URL}/{version}/{pref_code}000-{version}.zip"


class IsjAdapter:
    """街区レベル位置参照情報(全国)。"""

    source = "address_jp"
    source_kind = "isj"
    lang = "ja"
    # 全国で数百万街区。東京都だけで 21 万あるので、大きく欠けた素材はここで止まる
    min_docs = 1_000_000
    # CSV は 1 県ずつ流し読みするので軽い。重いのは構築(FTS)のほう
    min_build_memory_gb = 1.0

    def __init__(self) -> None:
        # 街区は区画整理で変わりうるので、取り込んだ中から代表を選ぶ(`iter_docs`)
        self.sample_titles: list[str] = []

    def _version(self) -> str:
        if pinned := os.environ.get("ISJ_VERSION"):
            return pinned
        found = version_name(KNOWN_VERSION)
        for major in range(KNOWN_VERSION + 1, KNOWN_VERSION + 1 + LOOKAHEAD_VERSIONS):
            if _exists(zip_url(version_name(major), "13")):
                found = version_name(major)
        return found

    def fetch(self, workdir: Path) -> tuple[list[Path], str]:
        workdir.mkdir(parents=True, exist_ok=True)
        version = self._version()
        stamp = os.environ.get("DUMP_DATE") or _last_modified(zip_url(version, "13"))
        paths = []
        for code in PREFECTURE_CODES:
            check_stop()
            paths.append(_download(
                zip_url(version, code), workdir / f"{self.source}-{version}-{code}.zip",
            ))
        return paths, stamp

    def iter_docs(self, path: Path | list[Path]) -> Iterator[Doc]:
        paths = path if isinstance(path, list) else [path]
        doc_id = 0
        for one in paths:
            version = _version_of(one)
            for row in read_rows(one):
                if doc_id % 10_000 == 0:
                    check_stop()
                doc = to_doc(row, doc_id + 1, version)
                if doc is None:
                    continue
                doc_id += 1
                if not self.sample_titles:
                    self.sample_titles = [doc.title]
                yield doc


def read_rows(path: Path) -> Iterator[dict]:
    """ZIP の中の CSV を 1 行ずつ(代表の行だけ。同じ街区は 1 度だけ)。

    **同じかどうかは見出しになる並びで見る**(見出しは UNIQUE。ZIP は 1 県ぶんで、
    見出しは県の名前から始まるので、県をまたいで重なることはない)。
    """
    seen: set[str] = set()
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if not info.filename.lower().endswith(".csv"):
                continue
            with zf.open(info) as raw:
                reader = csv.DictReader(io.TextIOWrapper(raw, encoding="cp932", newline=""))
                for row in reader:
                    if (row.get(COL_REPRESENTATIVE) or "").strip() != "1":
                        continue
                    key = "".join(
                        (row.get(c) or "").strip() for c in (COL_CITY, COL_TOWN, COL_KOAZA, COL_BLOCK)
                    )
                    if key in seen:
                        continue
                    seen.add(key)
                    yield row


def to_doc(row: dict, doc_id: int, version: str) -> Doc | None:
    pref = (row.get(COL_PREF) or "").strip()
    city = (row.get(COL_CITY) or "").strip()
    town = (row.get(COL_TOWN) or "").strip()
    koaza = (row.get(COL_KOAZA) or "").strip()
    block = (row.get(COL_BLOCK) or "").strip()
    try:
        lat, lon = float(row[COL_LAT]), float(row[COL_LON])
    except (KeyError, TypeError, ValueError):
        return None
    if not (pref and city and town and block):
        return None
    title = f"{pref}{city}{town}{koaza}{block}"
    # **1 件は細く持つ**(全国で数百万件あり、1 件の太さがそのまま数 GB の差になる)。
    # 町や番の値は見出しに入っているので脇書きに写さない。冒頭も置かない(見出しの写しになる)。
    # 県は `area`(生成列)で引けるので、タグは市区町村の 1 つだけ
    return Doc(
        doc_id=doc_id,
        title=title,
        tags=[f"{pref}{city}"],
        aliases=sorted(block_keys(pref, city, town, koaza, block) - {title}),
        extra={
            "lat": lat, "lon": lon, "feature": "isj=block", "area": pref,
            "residential": (row.get(COL_RESIDENTIAL) or "").strip() == "1",
            "version": version,
        },
    )


# ---- 引く鍵 -------------------------------------------------------------------------
# **`app/resolve.py` の `address_block_key` と同じ形にすること**(ingest と app は別の
# イメージで、互いを import できない)。食い違うと引けなくなるので、両方を通すテストがある
# (tests/test_ingest_isj.py)

_KANJI_DIGITS = {c: i for i, c in enumerate("〇一二三四五六七八九")}
_DASHES = "‐‑‒–—―−ーｰ"


def _kanji_number(found: re.Match) -> str:
    text = found.group(1)
    if "十" in text:
        head, _, tail = text.partition("十")
        value = (_KANJI_DIGITS.get(head, 1) if head else 1) * 10
        value += _KANJI_DIGITS.get(tail, 0) if tail else 0
        return str(value) + found.group(2)
    return "".join(str(_KANJI_DIGITS[c]) for c in text) + found.group(2)


def block_key(text: str) -> str:
    """住所を引く鍵の形にする(`東京都千代田区丸の内一丁目9番` → `東京都千代田区丸の内-1-9`)。"""
    value = re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))
    value = re.sub(r"^〒?\d{3}-?\d{4}", "", value)
    value = value.replace("ヶ", "ケ").replace("ヵ", "カ")
    value = re.sub(r"([〇一二三四五六七八九十]+)(丁目)", _kanji_number, value)
    value = re.sub(rf"(\d)[{_DASHES}](?=\d)", r"\1-", value)
    value = re.sub(r"(\d)の(?=\d)", r"\1-", value)
    value = re.sub(r"(丁目|番地|番|号)", "-", value)
    value = re.sub(r"([^\d-])(?=\d)", r"\1-", value)
    value = re.sub(r"-+", "-", value)
    return value.strip("-")


def block_keys(pref: str, city: str, town: str, koaza: str, block: str) -> set[str]:
    """1 街区の引く鍵。小字・`大字`・郡を省いた形も足す(住所の書き方の揺れ)。"""
    cities = {city, re.sub(r"^.+?郡", "", city)}
    towns = {town, re.sub(r"^大字", "", town)}
    koazas = {koaza, re.sub(r"^字", "", koaza), ""}
    return {
        block_key(f"{pref}{c}{t}{k}{block}")
        for c in cities for t in towns for k in koazas if c and t
    }


def _version_of(path: Path) -> str:
    found = re.search(r"-(\d+\.\d+[a-z])-\d{2}\.zip$", path.name)
    return found.group(1) if found else ""


def _exists(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError):
        return False


def _last_modified(url: str) -> str:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            if stamp := resp.headers.get("Last-Modified"):
                return parsedate_to_datetime(stamp).strftime("%Y%m%d")
    except (urllib.error.URLError, OSError):
        log.warning("address_jp: Last-Modified を読めなかったので今日の日付にする")
    return datetime.now(UTC).strftime("%Y%m%d")
