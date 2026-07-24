"""テスト用の小型 OSM PBF フィクスチャ fixtures/mini_osm.osm.pbf を生成する。

再生成: python tests/fixtures/make_osm_fixture.py

OSM PBF の規約どおり node → way → relation の順で書き出す(アダプタの
2 パス走査はこの順序を前提とする)。収録内容:
  - place / natural ノード(重複名「中央」で title 弁別も検証)
  - POI ノード(amenity=restaurant。旧版では対象外だったが現在は取り込み対象)
  - 交通インフラノード(railway=station / highway=motorway_junction / aeroway=aerodrome)
  - 対象外ノード(タグ無し、name の無い amenity、値が対象外の railway=rail / highway=residential)
  - way 地物(河口湖: 構成ノードの平均座標)
  - admin_centre ノード付き行政境界 relation(京都市)
  - ラベルノード無しの multipolygon relation(琵琶湖: メンバー way 座標の平均)
  - admin_level=4 の行政境界 relation(京都府: 矩形ポリゴン。extra.area の点内包判定用)
"""
from __future__ import annotations

from pathlib import Path

import osmium
from osmium.osm.mutable import Node, Relation, Way

NODES = [
    # (id, lon, lat, tags, timestamp)
    (1, 139.6917, 35.6895, {
        "place": "city", "name": "東京", "name:en": "Tokyo", "name:ja": "東京",
        "population": "13960000", "wikipedia": "ja:東京都区部", "wikidata": "Q7473516",
    }, "2026-06-01T00:00:00Z"),
    (2, 138.7274, 35.3606, {
        "natural": "peak", "name": "富士山", "name:en": "Mount Fuji",
        "ele": "3776", "alt_name": "富士の山;不二山",
    }, "2026-06-02T00:00:00Z"),
    (3, 139.7700, 35.6700, {"place": "neighbourhood", "name": "中央"}, None),
    (4, 135.5000, 34.6900, {"place": "suburb", "name": "中央"}, None),
    (5, 135.0000, 35.0000, {
        "amenity": "restaurant", "name": "ラーメン一番",
        "addr:city": "京都市", "addr:street": "河原町通", "phone": "075-000-0000",
    }, None),
    (12, 135.0100, 35.0100, {"amenity": "restaurant"}, None),  # name 無し: 対象外のまま
    # 交通インフラ(VALUE_LIMITED_KEYS)。駅は京都府の矩形内、空港は矩形外に置く。
    (13, 135.7581, 34.9858, {
        "railway": "station", "name": "京都駅", "name:en": "Kyoto Station",
    }, "2026-06-07T00:00:00Z"),
    (14, 135.7200, 34.9300, {
        "highway": "motorway_junction", "name": "京都南インターチェンジ",
    }, None),
    (15, 135.4400, 34.7850, {
        "aeroway": "aerodrome", "name": "大阪国際空港", "name:en": "Osaka International Airport",
    }, None),
    # 値が対象外の名前付き線形地物: 取り込まれないことの確認用
    (16, 135.7600, 34.9900, {"railway": "rail", "name": "東海道本線"}, None),
    (17, 135.7590, 34.9950, {"highway": "residential", "name": "五条通"}, None),
    # 以下はタグ無しの座標解決用ノード
    (6, 135.7681, 35.0116, {}, None),   # 京都市 relation の admin_centre
    (7, 138.7500, 35.5000, {}, None),   # way 100 (河口湖)
    (8, 138.7600, 35.5200, {}, None),
    (9, 138.7700, 35.5100, {}, None),
    (10, 135.9000, 35.0000, {}, None),  # way 101 (relation メンバー)
    (11, 136.1000, 35.2000, {}, None),
    # admin_level=4 境界(京都府)の矩形ポリゴン。lon 134.9〜135.9 / lat 34.9〜35.9。
    # ラーメン一番(135.00, 35.00)と京都市の admin_centre(135.7681, 35.0116)を含み、
    # 東京・富士山・河口湖・琵琶湖・中央(node:4) は含まない。
    (20, 134.9000, 34.9000, {}, None),
    (21, 135.9000, 34.9000, {}, None),
    (22, 135.9000, 35.9000, {}, None),
    (23, 134.9000, 35.9000, {}, None),
]

WAYS = [
    # (id, tags, refs, timestamp)
    (100, {"natural": "water", "name": "河口湖", "name:en": "Lake Kawaguchi"},
     [7, 8, 9], "2026-06-03T00:00:00Z"),
    (101, {}, [10, 11], None),  # タグ無し: relation 経由でのみ使われる
    (102, {}, [20, 21, 22, 23, 20], None),  # 京都府の境界(閉じた環)
]

RELATIONS = [
    # (id, tags, members=[(type, ref, role)], timestamp)
    (200, {
        "type": "boundary", "boundary": "administrative", "admin_level": "7",
        "name": "京都市", "name:en": "Kyoto", "population": "1463723",
        "wikipedia": "ja:京都市",
    }, [("n", 6, "admin_centre"), ("w", 101, "outer")], "2026-06-04T00:00:00Z"),
    (201, {"type": "multipolygon", "natural": "water", "name": "琵琶湖"},
     [("w", 101, "outer")], "2026-06-05T00:00:00Z"),
    (202, {
        "type": "boundary", "boundary": "administrative", "admin_level": "4",
        "name": "京都府", "name:en": "Kyoto Prefecture",
    }, [("w", 102, "outer")], "2026-06-06T00:00:00Z"),
]


def _ts(value: str | None) -> str:
    return value or "1970-01-01T00:00:00Z"


def main() -> None:
    out = Path(__file__).parent / "mini_osm.osm.pbf"
    if out.exists():
        out.unlink()
    writer = osmium.SimpleWriter(str(out))
    try:
        for node_id, lon, lat, tags, ts in NODES:
            writer.add_node(
                Node(id=node_id, location=(lon, lat), tags=tags, version=1, timestamp=_ts(ts))
            )
        for way_id, tags, refs, ts in WAYS:
            writer.add_way(
                Way(id=way_id, nodes=refs, tags=tags, version=1, timestamp=_ts(ts))
            )
        for rel_id, tags, members, ts in RELATIONS:
            writer.add_relation(
                Relation(id=rel_id, members=members, tags=tags, version=1, timestamp=_ts(ts))
            )
    finally:
        writer.close()
    print(f"wrote {out} ({len(NODES)} nodes, {len(WAYS)} ways, {len(RELATIONS)} relations)")


if __name__ == "__main__":
    main()
