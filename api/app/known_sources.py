"""既知ソースの静的な一覧(chiezo-trigger に繋がらないときの代替)。

chiezo-api は ingest 側のアダプタ実装を import しない(コンテナが別・依存関係も別のため)。
管理画面の「初期化」に出すソース名は、通常は chiezo-trigger の `GET /sources`
(ingest/server.py)から取る。OSM の国別ソースは 195 件あり、こちらへ手で複製するのは
現実的でないため、カタログの正は ingest 側の 1 か所だけにしてある。

ここに残すのは、trigger が未設定・到達不能なときに管理画面を空にしないための最小限の
控えだけ。新ソースを追加しても、ここへの追記は必須ではない
(`ingest/sources/__init__.py` の `ADAPTERS` に足せば trigger 経由で自動的に出る)。
"""
from __future__ import annotations

KNOWN_SOURCES: dict[str, dict] = {
    # group="wikipedia" のソースは 1 行にまとめ、言語の選択は /admin/wikipedia で行う
    "jawiki": {
        "kind": "wikipedia", "lang": "ja", "group": "wikipedia",
        "label": "日本語", "label_en": "Japanese", "autonym": "日本語",
    },
    "enwiki": {
        "kind": "wikipedia", "lang": "en", "group": "wikipedia",
        "label": "英語", "label_en": "English", "autonym": "English",
    },
    "geonames": {"kind": "geonames", "lang": ""},
    # group="osm" のソースは 1 行にまとめ、国の選択は /admin/osm で行う
    "osm_japan": {
        "kind": "osm", "lang": "ja", "group": "osm", "slug": "japan",
        "label": "日本", "label_en": "Japan", "continent": "asia", "region": "asia/japan",
    },
}

# 言語選択画面(/admin/wikipedia)での記事数の階層。(下限, 表示名) を大きい順に並べる。
WIKIPEDIA_TIERS: tuple[tuple[int, str], ...] = (
    (1_000_000, "100 万記事以上"),
    (100_000, "10 万〜100 万記事"),
    (10_000, "1 万〜10 万記事"),
    (0, "1 万記事未満"),
)

# 国選択画面での大陸の表示名。russia / antarctica は大陸に属さないため standalone。
CONTINENT_LABELS: dict[str, str] = {
    "africa": "アフリカ",
    "asia": "アジア",
    "australia-oceania": "オセアニア",
    "central-america": "中央アメリカ・カリブ",
    "europe": "ヨーロッパ",
    "north-america": "北アメリカ",
    "south-america": "南アメリカ",
    "standalone": "その他(ロシア・南極)",
}
