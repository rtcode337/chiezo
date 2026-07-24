#!/usr/bin/env python3
"""ingest/sources/osm_regions.py(OSM 国別抽出カタログ)を生成する。

Geofabrik の国別抽出は 200 以上あり、`ADAPTERS` に手で 1 行ずつ書き足すのは現実的でない
(しかも region パスの綴りを間違えるとダウンロード時まで気づけない)。そこで公式の索引
から機械的に生成する。生成物 `ingest/sources/osm_regions.py` はリポジトリにコミットし、
実行時にネットワークへ出るのはあくまで取り込み本体だけにする。

参照する外部データ:
  - Geofabrik index-v1.json  … 抽出の一覧(id / parent / 表示名 / pbf の URL)
  - Geofabrik の大陸別 HTML  … pbf のファイルサイズ(必要メモリと構築時間の目安の素)
  - CLDR territories.json    … 国名の日本語表記(管理画面の国選択で使う)
  - CLDR territoryInfo.json  … 国ごとの主要言語(OsmAdapter の lang)

使い方:

    python3 scripts/gen_osm_regions.py            # ingest/sources/osm_regions.py を書き換える
    python3 scripts/gen_osm_regions.py --stdout   # 中身を標準出力に出すだけ

pbf のサイズは時間とともに増えるため、生成物の memory_gb / min_docs はあくまで目安。
年に一度くらい再生成すれば十分で、ずれても取り込み前のメモリ検査
(ingest/main.py の require_build_memory)が実測値で止めるので致命傷にはならない。
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "ingest" / "sources" / "osm_regions.py"

GEOFABRIK = "https://download.geofabrik.de/"
CLDR = "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/"
USER_AGENT = "chiezo-gen-osm-regions/0.1"

# 大陸(Geofabrik のトップレベル)。russia / antarctica は大陸配下ではなく単独で並ぶ。
CONTINENTS = (
    "africa", "asia", "australia-oceania", "central-america",
    "europe", "north-america", "south-america",
)
STANDALONE = ("russia", "antarctica")

# 国ではなく「複数国をまとめた抽出」。個別の国が別に存在するので出さない
# (出すと同じ地域を二重に焼くことになり、しかも巨大)。
EXCLUDED = {
    "alps",                    # 独仏伊墺瑞のアルプス周辺
    "dach",                    # ドイツ + オーストリア + スイス
    "britain-and-ireland",     # 英 + 愛
    "sea",                     # 東南アジア一括
    "south-africa-and-lesotho",  # south-africa と lesotho が個別にある
    "us-midwest", "us-northeast", "us-pacific", "us-south", "us-west",  # 米国の地方単位
}

# RAM 上のノード座標索引に要るメモリの見積もり: pbf 1GB あたり何 GiB か。
# osm_japan(pbf 2.3GB)で実測 5〜10GB + 諸々の常駐を見て 12GiB を要件としていた実績に
# 合わせてある(2.3 * 5 = 11.5 → 切り上げ 12)。
MEMORY_GB_PER_PBF_GB = 5.0
MIN_MEMORY_GB = 3.0

# これを超えるならディスク索引(sparse_file_array)を既定にする。
# 「既定設定ではどのソースも 12GiB のマシンで構築できる」という方針の閾値
# (README / CLAUDE.md「メモリ方針」)。大きい国は遅くなる代わりに 2GiB で焼ける。
RAM_INDEX_BUDGET_GB = 12.0

# 検証の最低文書数: pbf 1GB あたり何件を下回ったら失敗とみなすか。
# osm_japan の 50,000 件(実際にはその何十倍も入る)と同じ水準の安全余裕。
MIN_DOCS_PER_PBF_GB = 20_000
MIN_DOCS_FLOOR = 50


# CLDR から日本語名・主要言語を引けない抽出の手当て。
# Geofabrik の名前が CLDR の英語名と一致しない(「Ukraine (with Crimea)」「Ivory Coast」)、
# 複数国をまとめた抽出で ISO が 1 つに定まらない、といった理由で自動では埋まらない。
# ここだけは人手で書く(自動生成に混ぜると Geofabrik 側の ISO 誤りをそのまま引き写して
# 「トケラウ = バヌアツ」のような取り違えを生むため、名前の一致を確認できた分だけ自動、
# 残りは明示、という切り分けにしている)。
OVERRIDES: dict[str, tuple[str, str | None]] = {
    # slug: (日本語表示名, 主要言語コード)
    "american-oceania": ("アメリカ領オセアニア", "en"),
    "azores": ("アゾレス諸島", "pt"),
    "canary-islands": ("カナリア諸島", "es"),
    "comores": ("コモロ", "ar"),
    "congo-brazzaville": ("コンゴ共和国", "fr"),
    "congo-democratic-republic": ("コンゴ民主共和国", "fr"),
    "czech-republic": ("チェコ", "cs"),
    "east-timor": ("東ティモール", "pt"),
    "gcc-states": ("湾岸協力会議諸国(サウジ・UAE・カタール等)", "ar"),
    "great-britain": ("イギリス(グレートブリテン島)", "en"),
    "guernsey-jersey": ("ガーンジー・ジャージー", "en"),
    "haiti-and-domrep": ("ハイチ・ドミニカ共和国", "es"),
    "ile-de-clipperton": ("クリッパートン島", "fr"),
    "indonesia": ("インドネシア(東ティモールを含む)", "id"),
    "ireland-and-northern-ireland": ("アイルランド・北アイルランド", "en"),
    "isle-of-man": ("マン島", "en"),
    "israel-and-palestine": ("イスラエル・パレスチナ", "he"),
    "ivory-coast": ("コートジボワール", "fr"),
    "kosovo": ("コソボ", "sq"),
    "macedonia": ("北マケドニア", "mk"),
    "malaysia-singapore-brunei": ("マレーシア・シンガポール・ブルネイ", "ms"),
    "myanmar": ("ミャンマー", "my"),
    "pitcairn-islands": ("ピトケアン諸島", "en"),
    "polynesie-francaise": ("フランス領ポリネシア", "fr"),
    "russia": ("ロシア", "ru"),
    "saint-helena-ascension-and-tristan-da-cunha": ("セントヘレナ・アセンション・トリスタンダクーニャ", "en"),
    "sao-tome-and-principe": ("サントメ・プリンシペ", "pt"),
    "senegal-and-gambia": ("セネガル・ガンビア", "fr"),
    "swaziland": ("エスワティニ", "en"),
    "tokelau": ("トケラウ", "en"),
    "turkey": ("トルコ", "tr"),
    "ukraine": ("ウクライナ(クリミアを含む)", "uk"),
    "us": ("アメリカ合衆国", "en"),
    "wallis-et-futuna": ("ウォリス・フツナ", "fr"),
}


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def fetch_json(url: str) -> dict:
    return json.loads(fetch(url))


# HTML 側の pbf 行: <a href="asia/japan-latest.osm.pbf">[.osm.pbf]</a></td><td ...>(2.3&nbsp;GB)</td>
SIZE_RE = re.compile(
    r'href="([^"]+?)-latest\.osm\.pbf"[^>]*>\[\.osm\.pbf\]</a></td>'
    r'<td[^>]*>\((\d+(?:\.\d+)?)&nbsp;([KMG]B)\)'
)
SIZE_UNIT = {"KB": 10 ** 3, "MB": 10 ** 6, "GB": 10 ** 9}


def fetch_pbf_sizes() -> dict[str, int]:
    """region パス → pbf のバイト数。大陸別ページとトップページから集める。"""
    sizes: dict[str, int] = {}
    for page in (*[f"{c}.html" for c in CONTINENTS], ""):
        html = fetch(GEOFABRIK + page).decode("utf-8", "replace")
        for path, number, unit in SIZE_RE.findall(html):
            # トップページは "./africa"、大陸ページから見た russia は "/russia" のように出る
            sizes[path.removeprefix("./").removeprefix("/")] = int(
                float(number) * SIZE_UNIT[unit]
            )
    return sizes


def fetch_cldr() -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
    """(ISO alpha2 → 日本語名, ISO alpha2 → 英語名, ISO alpha2 → 主要言語コード)。"""
    ja = fetch_json(CLDR + "cldr-localenames-full/main/ja/territories.json")
    en = fetch_json(CLDR + "cldr-localenames-full/main/en/territories.json")
    info = fetch_json(CLDR + "cldr-core/supplemental/territoryInfo.json")

    ja_names = ja["main"]["ja"]["localeDisplayNames"]["territories"]
    en_names = en["main"]["en"]["localeDisplayNames"]["territories"]

    langs: dict[str, str] = {}
    for code, entry in info["supplemental"]["territoryInfo"].items():
        best: tuple[float, str] | None = None
        for lang, attrs in (entry.get("languagePopulation") or {}).items():
            if attrs.get("_officialStatus") not in ("official", "de_facto_official"):
                continue
            share = float(attrs.get("_populationPercent", 0))
            if best is None or share > best[0]:
                best = (share, lang)
        if best:
            # "zh_Hant" のような表記付きコードは Wikipedia の言語コードに合わせて素の部分だけ使う
            langs[code] = best[1].split("_")[0]
    return ja_names, en_names, langs


def normalize(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def build_rows() -> list[dict]:
    index = fetch_json(GEOFABRIK + "index-v1.json")
    sizes = fetch_pbf_sizes()
    ja_names, en_names, langs = fetch_cldr()

    rows: list[dict] = []
    for feature in index["features"]:
        props = feature["properties"]
        slug = props["id"]
        parent = props.get("parent")
        if slug in EXCLUDED or "/" in slug:
            continue
        if parent in CONTINENTS:
            continent = parent
        elif slug in STANDALONE:
            continent = "standalone"
        else:
            continue

        region = f"{parent}/{slug}" if parent else slug
        pbf_bytes = sizes.get(region)
        if pbf_bytes is None:
            print(f"warning: no pbf size for {region}; skipped", file=sys.stderr)
            continue

        name_en = props["name"]
        iso_codes = props.get("iso3166-1:alpha2") or []
        # Geofabrik の ISO には取り違え(トケラウに VU など)が混ざっているため、
        # CLDR の英語名と一致したときだけ ISO 由来の日本語名・言語を採用する。
        iso = iso_codes[0] if len(iso_codes) == 1 else None
        if iso and normalize(en_names.get(iso, "")) != normalize(name_en):
            iso = None

        label, lang = OVERRIDES.get(
            slug, (ja_names.get(iso) if iso else None, langs.get(iso) if iso else None)
        )

        pbf_gb = pbf_bytes / 10 ** 9
        memory_gb = max(MIN_MEMORY_GB, float(math.ceil(pbf_gb * MEMORY_GB_PER_PBF_GB)))
        rows.append(
            {
                "slug": slug,
                "source": "osm_" + slug.replace("-", "_"),
                "region": region,
                "continent": continent,
                "label": label,
                "label_en": name_en,
                "lang": lang,
                "pbf_bytes": pbf_bytes,
                "memory_gb": memory_gb,
                "node_index": (
                    "sparse_mmap_array" if memory_gb <= RAM_INDEX_BUDGET_GB else "sparse_file_array"
                ),
                "min_docs": max(MIN_DOCS_FLOOR, round(pbf_gb * MIN_DOCS_PER_PBF_GB)),
            }
        )
    rows.sort(key=lambda r: (CONTINENT_ORDER.get(r["continent"], 99), r["slug"]))
    return rows


CONTINENT_ORDER = {c: i for i, c in enumerate((*CONTINENTS, "standalone"))}

HEADER = '''"""OpenStreetMap(Geofabrik)の国別抽出カタログ。

**自動生成物。手で編集せず `python3 scripts/gen_osm_regions.py` で作り直すこと。**

`sources/__init__.py` はこの表から `osm_<国>` のアダプタを一括生成する。国別抽出は
200 以上あり手書きでは追随できないうえ、region パスを 1 文字間違えるとダウンロード時
まで気づけないため、Geofabrik の公式索引(index-v1.json)から機械的に起こしている。

各項目の意味:
  region      Geofabrik のパス。`<region>-latest.osm.pbf` を落とす
  label       管理画面に出す表示名(日本語。CLDR 由来。取れない地域は英名のまま)
  lang        その国の主要言語(CLDR territoryInfo)。`wikipedia:<lang>` タグの解決に使う
  pbf_bytes   生成時点の pbf サイズ。必要メモリ・構築時間・ディスクの目安の素
  memory_gb   RAM 索引で構築する場合に要るメモリの目安(pbf 1GB あたり {mem_per_gb:.0f}GiB)
  node_index  既定のノード座標索引。{budget:.0f}GiB を超える国はディスク索引を既定にする
              (RAM に載らないため。遅くなる代わりに 2GiB で焼ける)
  min_docs    検証で要求する最低文書数(pbf サイズから起こした保守的な下限)

サイズは日々増えるので memory_gb / min_docs はあくまで目安。実際に足りるかは取り込み
開始前のメモリ検査(ingest/main.py の require_build_memory)が実測で判定する。
"""
from __future__ import annotations

from typing import NamedTuple


class OsmRegion(NamedTuple):
    slug: str          # Geofabrik 側の識別子(ハイフン区切り)
    source: str        # chiezo のソース名(osm_<国>。区切りはアンダースコア)
    region: str        # Geofabrik のパス(例: asia/japan)
    continent: str     # 大陸(表示のグルーピング用。russia / antarctica は standalone)
    label: str         # 表示名(日本語。無ければ英名)
    label_en: str
    lang: str | None   # 主要言語コード(取れなければ None)
    pbf_bytes: int
    memory_gb: float
    node_index: str
    min_docs: int


# 大陸の表示順(管理画面の国選択で使う)
CONTINENTS: tuple[str, ...] = (
{continents}
)

OSM_REGIONS: dict[str, OsmRegion] = {{
'''

FOOTER = '''}}


def by_continent() -> dict[str, list[OsmRegion]]:
    """大陸 → その大陸の抽出一覧(CONTINENTS の順、国は表示名順)。"""
    grouped: dict[str, list[OsmRegion]] = {{c: [] for c in CONTINENTS}}
    for region in OSM_REGIONS.values():
        grouped.setdefault(region.continent, []).append(region)
    return {{c: rs for c, rs in grouped.items() if rs}}
'''


def render(rows: list[dict]) -> str:
    continents = "\n".join(
        f'    "{c}",' for c in (*CONTINENTS, "standalone")
    )
    out = [
        HEADER.format(
            mem_per_gb=MEMORY_GB_PER_PBF_GB,
            budget=RAM_INDEX_BUDGET_GB,
            continents=continents,
        )
    ]
    for r in rows:
        label = r["label"] or r["label_en"]
        lang = f'"{r["lang"]}"' if r["lang"] else "None"
        out.append(
            f'    "{r["slug"]}": OsmRegion(\n'
            f'        slug="{r["slug"]}", source="{r["source"]}", region="{r["region"]}",\n'
            f'        continent="{r["continent"]}", label="{label}", label_en="{r["label_en"]}",\n'
            f'        lang={lang}, pbf_bytes={r["pbf_bytes"]}, memory_gb={r["memory_gb"]},\n'
            f'        node_index="{r["node_index"]}", min_docs={r["min_docs"]},\n'
            f"    ),\n"
        )
    out.append(FOOTER.format())
    return "".join(out)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stdout", action="store_true", help="ファイルを書かず標準出力に出す")
    args = parser.parse_args()

    rows = build_rows()
    text = render(rows)
    if args.stdout:
        sys.stdout.write(text)
        return
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(rows)} regions", file=sys.stderr)


if __name__ == "__main__":
    main()
