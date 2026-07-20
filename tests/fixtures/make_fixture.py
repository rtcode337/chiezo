"""テスト用の小型 MediaWiki エクスポート XML fixtures/mini_jawiki.xml.gz を生成する。

再生成: python tests/fixtures/make_fixture.py
"""
from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from pathlib import Path

# 通常記事(ns=0、リダイレクトなし)。doc_id は <id> にそのまま入る。
ARTICLES = [
    {
        "id": 1,
        "title": "東京都",
        "timestamp": "2026-06-01T00:00:00Z",
        "text": (
            "'''東京都'''は、[[日本]]の首都機能を有する[[都道府県|都]]である。"
            "[[関東地方]]に位置し、日本の政治・経済の中心地である。"
            "人口は日本最大であり、多くの企業や大学が集中している。\n\n"
            "== 地理 ==\n"
            "[[新宿区]]など多くの区で構成される。\n\n"
            "[[Category:日本の都道府県]]\n"
            "[[Category:関東地方]]\n"
        ),
    },
    {
        "id": 2,
        "title": "浅草寺",
        "timestamp": "2026-06-02T00:00:00Z",
        "text": (
            "'''浅草寺'''は、[[雷門]]で知られる[[東京都]][[台東区]]の寺院である。"
            "その歴史は飛鳥時代にまで遡り、都内最古の寺院とされる。"
            "仲見世通りには多くの観光客が訪れる。\n\n"
            "[[Category:東京都の寺]]\n"
            "[[Category:台東区]]\n"
        ),
    },
    {
        "id": 3,
        "title": "関ヶ原の戦い",
        "timestamp": "2026-06-03T00:00:00Z",
        "text": (
            "'''関ヶ原の戦い'''は、1600年に美濃国関ヶ原で行われた合戦である。"
            "[[徳川家康]]率いる東軍と[[石田三成]]らの西軍が激突し、"
            "天下分け目の戦いと呼ばれる。\n\n"
            "[[Category:日本の合戦]]\n"
            "[[Category:安土桃山時代]]\n"
        ),
    },
    {
        "id": 4,
        "title": "富士山",
        "timestamp": "2026-06-04T00:00:00Z",
        "text": (
            "'''富士山'''は、[[静岡県]]と[[山梨県]]にまたがる日本最高峰の山である。"
            "標高は3776メートルで、世界文化遺産に登録されている。\n\n"
            "[[Category:日本の山]]\n"
            "[[Category:世界遺産]]\n"
        ),
    },
    {
        "id": 5,
        "title": "夏目漱石",
        "timestamp": "2026-06-05T00:00:00Z",
        "text": (
            "'''夏目漱石'''は、日本の小説家・英文学者である。"
            "代表作に『[[吾輩は猫である]]』『[[坊っちゃん]]』『こころ』などがある。\n\n"
            "[[Category:日本の小説家]]\n"
        ),
    },
    {
        "id": 6,
        "title": "日本",
        "timestamp": "2026-06-06T00:00:00Z",
        "text": (
            "'''日本'''は、東アジアに位置する島国である。"
            "首都は[[東京都]]。四季があり、独自の文化を持つ。\n\n"
            "[[Category:アジアの国]]\n"
        ),
    },
    {
        "id": 7,
        "title": "京都市",
        "timestamp": "2026-06-07T00:00:00Z",
        "text": (
            "'''京都市'''は、[[京都府]]の府庁所在地である。"
            "[[平安京]]として長く都が置かれ、多くの寺院や神社が残る古都である。\n\n"
            "[[Category:京都府の市町村]]\n"
        ),
    },
    {
        "id": 8,
        "title": "新幹線",
        "timestamp": "2026-06-08T00:00:00Z",
        "text": (
            "'''新幹線'''は、日本の高速鉄道システムである。"
            "1964年に[[東海道新幹線]]が開業し、現在は全国に路線網が広がっている。\n\n"
            "[[Category:日本の鉄道]]\n"
        ),
    },
    {
        "id": 9,
        "title": "源氏物語",
        "timestamp": "2026-06-09T00:00:00Z",
        "text": (
            "'''源氏物語'''は、[[紫式部]]による平安時代の長編物語である。"
            "[[光源氏]]の生涯を描き、世界最古の長編小説のひとつとされる。\n\n"
            "[[Category:平安時代の文学]]\n"
        ),
    },
    {
        "id": 10,
        "title": "大阪府",
        "timestamp": "2026-06-10T00:00:00Z",
        "text": (
            "'''大阪府'''は、近畿地方に位置する府である。"
            "西日本の経済の中心地であり、商人の街として発展してきた。\n\n"
            "[[Category:日本の都道府県]]\n"
            "[[Category:近畿地方]]\n"
        ),
    },
    {
        # 折りたたみ(collapsible)テンプレート内の表が body に含まれることの回帰テスト用。
        # CirrusSearch ダンプの text フィールドはこの種のテンプレート内容を検索インデックス
        # から除外していたため(ブラタモリの放送回一覧が欠落していた実例と同型の構造)、
        # XML ダンプ + wikitext 解析への切り替えでここが正しく本文に入ることを確認する。
        "id": 11,
        "title": "犬",
        "timestamp": "2026-06-11T00:00:00Z",
        "text": (
            "'''犬'''は、古くから人間に飼われてきた動物である。"
            "忠実な性格で、ペットや使役動物として世界中で親しまれている。\n\n"
            "== 品種 ==\n"
            "{{hidden begin|title=代表的な品種}}\n"
            "{| class=\"wikitable\"\n"
            "!品種\n"
            "!原産国\n"
            "|-\n"
            "|柴犬\n"
            "|日本\n"
            "|-\n"
            "|プードル\n"
            "|フランス\n"
            "|}\n"
            "{{hidden end}}\n"
        ),
    },
]

# リダイレクトページ(ns=0、<redirect> あり)。docs には含まれず aliases になる。
REDIRECTS = [
    {"id": 101, "title": "東京", "target": "東京都", "timestamp": "2026-06-01T00:00:00Z"},
    {"id": 102, "title": "金龍山浅草寺", "target": "浅草寺", "timestamp": "2026-06-02T00:00:00Z"},
    {"id": 103, "title": "関ケ原の戦い", "target": "関ヶ原の戦い", "timestamp": "2026-06-03T00:00:00Z"},
    {"id": 104, "title": "夏目金之助", "target": "夏目漱石", "timestamp": "2026-06-05T00:00:00Z"},
]

# 非 ns=0 ページ(namespace フィルタで除外されることのテスト用)。
NON_ARTICLE_PAGES = [
    {
        "id": 999,
        "title": "ノート:東京都",
        "ns": 1,
        "timestamp": "2026-06-01T00:00:00Z",
        "text": "これはノートページであり取り込まれない。",
        "redirect": None,
    },
    {
        # ns!=0 の redirect は aliases に含まれないことのテスト用
        # (浅草寺への「リダイレクト」だが Wikipedia 名前空間なので除外される)。
        "id": 105,
        "title": "Wikipedia:浅草寺関連",
        "ns": 4,
        "timestamp": "2026-06-02T00:00:00Z",
        "text": "#REDIRECT [[浅草寺]]",
        "redirect": "浅草寺",
    },
]


def _add_page(root: ET.Element, *, page_id: int, title: str, ns: int, timestamp: str,
              text: str, redirect_title: str | None = None) -> None:
    page = ET.SubElement(root, "page")
    ET.SubElement(page, "title").text = title
    ET.SubElement(page, "ns").text = str(ns)
    ET.SubElement(page, "id").text = str(page_id)
    if redirect_title:
        redirect = ET.SubElement(page, "redirect")
        redirect.set("title", redirect_title)
    revision = ET.SubElement(page, "revision")
    ET.SubElement(revision, "timestamp").text = timestamp
    text_elem = ET.SubElement(revision, "text")
    text_elem.set("xml:space", "preserve")
    text_elem.text = text


def build_tree() -> ET.Element:
    root = ET.Element("mediawiki")
    for a in ARTICLES:
        _add_page(root, page_id=a["id"], title=a["title"], ns=0, timestamp=a["timestamp"], text=a["text"])
    for r in REDIRECTS:
        _add_page(
            root, page_id=r["id"], title=r["title"], ns=0, timestamp=r["timestamp"],
            text=f"#REDIRECT [[{r['target']}]]", redirect_title=r["target"],
        )
    for p in NON_ARTICLE_PAGES:
        _add_page(
            root, page_id=p["id"], title=p["title"], ns=p["ns"], timestamp=p["timestamp"],
            text=p["text"], redirect_title=p["redirect"],
        )
    return root


def main() -> None:
    out = Path(__file__).parent / "mini_jawiki.xml.gz"
    root = build_tree()
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with gzip.open(out, "wb") as f:
        f.write(xml_bytes)
    n = len(ARTICLES)
    print(f"wrote {out} ({n} articles + {len(REDIRECTS)} redirects + {len(NON_ARTICLE_PAGES)} non-article pages)")


if __name__ == "__main__":
    main()
