#!/usr/bin/env python3
"""ingest/sources/wikipedia_editions.py(Wikipedia 言語版カタログ)を生成する。

Wikipedia の言語版は 300 以上あり、`ADAPTERS` に手で 1 行ずつ書き足すのは現実的でない
(しかも wiki_id や URL 言語コードの綴りを間違えるとダウンロード時まで気づけない)。
そこで `scripts/gen_osm_regions.py` と同じ流儀で、公式の一覧から機械的に生成する。
生成物 `ingest/sources/wikipedia_editions.py` はリポジトリにコミットし、実行時に
ネットワークへ出るのはあくまで取り込み本体だけにする。

参照する外部データ:
  - Wikimedia sitematrix API … 言語版の一覧(言語コード / dbname / URL / 英語名 / 自称。
                                closed / private / fishbowl はここで除外する)
  - wikistats(wmcloud)     … 言語版ごとの記事数(検証の最低文書数と表示の目安の素)
  - CLDR ja languages.json   … 言語名の日本語表記(管理画面の言語選択で使う)

注意: 言語コードは 2 系統ある。sitematrix の言語コードは現行の BCP47 寄り(yue, gsw)、
URL・pageview ドメイン・wikistats は歴史的な URL コード(zh-yue, als)を使う。
カタログの `lang` には **URL コード**(サブドメイン)を入れる。ダンプ URL の素になる
`wiki_id`(dbname)はハイフンを含まない(zh_yuewiki)ので、世代ファイル名
`<source>-<date>.db` の区切りとも衝突しない。

使い方:

    python3 scripts/gen_wikipedia_editions.py            # ingest/sources/wikipedia_editions.py を書き換える
    python3 scripts/gen_wikipedia_editions.py --stdout   # 中身を標準出力に出すだけ

記事数は日々増えるため min_docs はあくまで保守的な下限(生成時点の記事数の半分)。
年に一度くらい再生成すれば十分。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "ingest" / "sources" / "wikipedia_editions.py"

SITEMATRIX_URL = "https://meta.wikimedia.org/w/api.php?action=sitematrix&format=json"
WIKISTATS_URL = "https://wikistats.wmcloud.org/api.php?action=dump&table=wikipedias&format=csv"
CLDR_JA_LANGUAGES_URL = (
    "https://raw.githubusercontent.com/unicode-org/cldr-json/main/cldr-json/"
    "cldr-localenames-full/main/ja/languages.json"
)
USER_AGENT = "chiezo-gen-wikipedia-editions/0.1"

# 検証の最低文書数: 生成時点の記事数の何割を下回ったら失敗とみなすか。
# ダンプは記事数の集計より遅れるうえリダイレクト等の勘定も揺れるため、半分で見る。
MIN_DOCS_RATIO = 0.5


def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def fetch_articles() -> dict[str, int]:
    """URL 言語コード → 記事数(wikistats の good 列)。"""
    text = fetch(WIKISTATS_URL).decode("utf-8", "replace")
    counts: dict[str, int] = {}
    for row in csv.DictReader(io.StringIO(text)):
        try:
            counts[row["prefix"]] = int(row["good"])
        except (KeyError, TypeError, ValueError):
            continue
    return counts


def fetch_ja_labels() -> dict[str, str]:
    data = json.loads(fetch(CLDR_JA_LANGUAGES_URL))
    return data["main"]["ja"]["localeDisplayNames"]["languages"]


def build_rows() -> list[dict]:
    matrix = json.loads(fetch(SITEMATRIX_URL))["sitematrix"]
    articles = fetch_articles()
    ja_labels = fetch_ja_labels()

    # 言語版はすべて番号付きエントリに入っている(simple も言語として並ぶ)。
    # "specials" は commons / meta 等で、現在 open な Wikipedia は無い。
    entries = [v for k, v in matrix.items() if k.isdigit()]

    rows: list[dict] = []
    for entry in entries:
        for site in entry.get("site", []):
            if site.get("code") != "wiki":
                continue
            if any(flag in site for flag in ("closed", "private", "fishbowl")):
                continue
            wiki_id = site["dbname"]
            if "-" in wiki_id:
                # 世代ファイル名 <source>-<date>.db の区切りと衝突するため許容しない
                # (dbname はアンダースコア区切りなので実際には来ないはず)
                print(f"warning: hyphen in dbname {wiki_id}; skipped", file=sys.stderr)
                continue
            url_code = site["url"].removeprefix("https://").split(".")[0]
            n_articles = articles.get(url_code, 0)
            rows.append(
                {
                    "lang": url_code,
                    "wiki_id": wiki_id,
                    # 日本語名は現行コード(sitematrix)→ URL コードの順で CLDR を引き、
                    # 無ければ英語名のまま
                    "label": ja_labels.get(entry["code"]) or ja_labels.get(url_code),
                    "label_en": entry.get("localname") or entry.get("name") or url_code,
                    "autonym": entry.get("name") or url_code,
                    "articles": n_articles,
                    "min_docs": max(1, int(n_articles * MIN_DOCS_RATIO)),
                }
            )
    rows.sort(key=lambda r: (-r["articles"], r["wiki_id"]))
    return rows


HEADER = '''"""Wikipedia 言語版カタログ。

**自動生成物。手で編集せず `python3 scripts/gen_wikipedia_editions.py` で作り直すこと。**

`sources/__init__.py` はこの表から `<lang>wiki` のアダプタを一括生成する。言語版は
300 以上あり手書きでは追随できないため、Wikimedia の sitematrix(言語版一覧)と
wikistats(記事数)、CLDR(言語名の日本語表記)から機械的に起こしている。

各項目の意味:
  lang       URL 言語コード(サブドメイン。ハイフン区切り: zh-yue 等)。
             pageview_complete のドメイン `<lang>.wikipedia` の素
  wiki_id    dbname = Chiezo のソース名(zh_yuewiki 等)。ダンプ URL の素
  label      言語名の日本語表記(CLDR。無ければ英名)
  label_en   英語名(sitematrix localname)
  autonym    その言語での自称
  articles   生成時点の記事数(wikistats)。表示の目安
  min_docs   検証で要求する最低文書数(記事数の {ratio:.0%}。保守的な下限)

記事数は日々増えるので articles / min_docs はあくまで目安。
"""
from __future__ import annotations

from typing import NamedTuple


class WikipediaEdition(NamedTuple):
    lang: str          # URL 言語コード(zh-yue 等はハイフン区切り)
    wiki_id: str       # dbname = ソース名(区切りはアンダースコア)
    label: str         # 表示名(日本語。無ければ英名)
    label_en: str
    autonym: str       # その言語での自称
    articles: int
    min_docs: int


WIKIPEDIA_EDITIONS: dict[str, WikipediaEdition] = {{
'''

FOOTER = """}
"""


def render(rows: list[dict]) -> str:
    out = [HEADER.format(ratio=MIN_DOCS_RATIO)]
    for r in rows:
        label = json.dumps(r["label"] or r["label_en"], ensure_ascii=False)
        label_en = json.dumps(r["label_en"], ensure_ascii=False)
        autonym = json.dumps(r["autonym"], ensure_ascii=False)
        out.append(
            f'    "{r["wiki_id"]}": WikipediaEdition(\n'
            f'        lang="{r["lang"]}", wiki_id="{r["wiki_id"]}",\n'
            f"        label={label}, label_en={label_en}, autonym={autonym},\n"
            f'        articles={r["articles"]}, min_docs={r["min_docs"]},\n'
            f"    ),\n"
        )
    out.append(FOOTER)
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
    print(f"wrote {OUTPUT.relative_to(ROOT)}: {len(rows)} editions", file=sys.stderr)


if __name__ == "__main__":
    main()
