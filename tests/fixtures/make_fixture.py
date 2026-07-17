"""テスト用の小型 CirrusSearch ダンプ fixtures/mini_jawiki.json.gz を生成する。

再生成: python tests/fixtures/make_fixture.py
"""
from __future__ import annotations

import gzip
import json
from pathlib import Path

DOCS = [
    {
        "_id": 1,
        "title": "東京都",
        "opening_text": "東京都は、日本の首都機能を有する都である。",
        "text": "東京都は、日本の首都機能を有する都である。関東地方に位置し、日本の政治・経済の中心地である。人口は日本最大であり、多くの企業や大学が集中している。",
        "category": ["日本の都道府県", "関東地方"],
        "outgoing_link": ["日本", "関東地方", "新宿区"],
        "redirect": [{"namespace": 0, "title": "東京"}],
        "timestamp": "2026-06-01T00:00:00Z",
        "popularity_score": 0.9,
    },
    {
        "_id": 2,
        "title": "浅草寺",
        "opening_text": "浅草寺は、雷門で知られる東京都台東区の寺院である。",
        "text": "浅草寺は、雷門で知られる東京都台東区の寺院である。その歴史は飛鳥時代にまで遡り、都内最古の寺院とされる。仲見世通りには多くの観光客が訪れる。",
        "category": ["東京都の寺", "台東区"],
        "outgoing_link": ["東京都", "雷門", "台東区"],
        "redirect": [
            {"namespace": 0, "title": "金龍山浅草寺"},
            {"namespace": 4, "title": "Wikipedia:浅草寺関連"},
        ],
        "timestamp": "2026-06-02T00:00:00Z",
        "popularity_score": 0.7,
    },
    {
        "_id": 3,
        "title": "関ヶ原の戦い",
        "opening_text": "関ヶ原の戦いは、1600年に美濃国関ヶ原で行われた合戦である。",
        "text": "関ヶ原の戦いは、1600年に美濃国関ヶ原で行われた合戦である。徳川家康率いる東軍と石田三成らの西軍が激突し、天下分け目の戦いと呼ばれる。",
        "category": ["日本の合戦", "安土桃山時代"],
        "outgoing_link": ["徳川家康", "石田三成"],
        "redirect": [{"namespace": 0, "title": "関ケ原の戦い"}],
        "timestamp": "2026-06-03T00:00:00Z",
        "popularity_score": 0.6,
    },
    {
        "_id": 4,
        "title": "富士山",
        "opening_text": "富士山は、静岡県と山梨県にまたがる日本最高峰の山である。",
        "text": "富士山は、静岡県と山梨県にまたがる日本最高峰の山である。標高は3776メートルで、世界文化遺産に登録されている。",
        "category": ["日本の山", "世界遺産"],
        "outgoing_link": ["静岡県", "山梨県"],
        "timestamp": "2026-06-04T00:00:00Z",
        "popularity_score": 0.8,
    },
    {
        "_id": 5,
        "title": "夏目漱石",
        "opening_text": "夏目漱石は、日本の小説家・英文学者である。",
        "text": "夏目漱石は、日本の小説家・英文学者である。代表作に『吾輩は猫である』『坊っちゃん』『こころ』などがある。",
        "category": ["日本の小説家"],
        "outgoing_link": ["吾輩は猫である", "坊っちゃん"],
        "redirect": [{"namespace": 0, "title": "夏目金之助"}],
        "timestamp": "2026-06-05T00:00:00Z",
        "popularity_score": 0.5,
    },
    {
        "_id": 6,
        "title": "日本",
        "opening_text": "日本は、東アジアに位置する島国である。",
        "text": "日本は、東アジアに位置する島国である。首都は東京。四季があり、独自の文化を持つ。",
        "category": ["アジアの国"],
        "outgoing_link": ["東京都", "アジア"],
        "timestamp": "2026-06-06T00:00:00Z",
        "popularity_score": 1.0,
    },
    {
        "_id": 7,
        "title": "京都市",
        "opening_text": "京都市は、京都府の府庁所在地である。",
        "text": "京都市は、京都府の府庁所在地である。平安京として長く都が置かれ、多くの寺院や神社が残る古都である。",
        "category": ["京都府の市町村"],
        "outgoing_link": ["京都府", "平安京"],
        "timestamp": "2026-06-07T00:00:00Z",
        "popularity_score": 0.65,
    },
    {
        "_id": 8,
        "title": "新幹線",
        "opening_text": "新幹線は、日本の高速鉄道システムである。",
        "text": "新幹線は、日本の高速鉄道システムである。1964年に東海道新幹線が開業し、現在は全国に路線網が広がっている。",
        "category": ["日本の鉄道"],
        "outgoing_link": ["東海道新幹線"],
        "timestamp": "2026-06-08T00:00:00Z",
        "popularity_score": 0.55,
    },
    {
        "_id": 9,
        "title": "源氏物語",
        "opening_text": "源氏物語は、紫式部による平安時代の長編物語である。",
        "text": "源氏物語は、紫式部による平安時代の長編物語である。光源氏の生涯を描き、世界最古の長編小説のひとつとされる。",
        "category": ["平安時代の文学"],
        "outgoing_link": ["紫式部", "光源氏"],
        "timestamp": "2026-06-09T00:00:00Z",
        "popularity_score": 0.45,
    },
    {
        "_id": 10,
        "title": "大阪府",
        "opening_text": "大阪府は、近畿地方に位置する府である。",
        "text": "大阪府は、近畿地方に位置する府である。西日本の経済の中心地であり、商人の街として発展してきた。",
        "category": ["日本の都道府県", "近畿地方"],
        "outgoing_link": ["日本", "近畿地方"],
        "timestamp": "2026-06-10T00:00:00Z",
        "popularity_score": 0.75,
    },
    {
        "_id": 11,
        "title": "犬",
        "opening_text": "犬は、古くから人間に飼われてきた動物である。",
        "text": "犬は、古くから人間に飼われてきた動物である。忠実な性格で、ペットや使役動物として世界中で親しまれている。",
        "category": ["哺乳類"],
        "outgoing_link": ["哺乳類"],
        "timestamp": "2026-06-11T00:00:00Z",
        "popularity_score": 0.3,
    },
    {
        # namespace != 0 は ingest でスキップされること
        "_id": 999,
        "namespace": 1,
        "title": "ノート:東京都",
        "text": "これはノートページであり取り込まれない。",
        "timestamp": "2026-06-01T00:00:00Z",
    },
]


def main() -> None:
    out = Path(__file__).parent / "mini_jawiki.json.gz"
    with gzip.open(out, "wt", encoding="utf-8") as f:
        for doc in DOCS:
            doc = dict(doc)
            doc_id = doc.pop("_id")
            doc.setdefault("namespace", 0)
            f.write(json.dumps({"index": {"_type": "page", "_id": str(doc_id)}}, ensure_ascii=False) + "\n")
            f.write(json.dumps(doc, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({len(DOCS)} docs)")


if __name__ == "__main__":
    main()
