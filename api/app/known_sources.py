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
    "jawiki": {"kind": "wikipedia", "lang": "ja"},
    "geonames": {"kind": "geonames", "lang": ""},
    # group="osm" のソースは 1 行にまとめ、国の選択は /admin/osm で行う
    "osm_japan": {
        "kind": "osm", "lang": "ja", "group": "osm", "slug": "japan",
        "label": "日本", "label_en": "Japan", "continent": "asia", "region": "asia/japan",
    },
}

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
