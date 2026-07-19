"""テスト用の小型 OSM PBF フィクスチャ fixtures/mini_osm.osm.pbf を生成する。

再生成: python tests/fixtures/make_osm_fixture.py

OSM PBF の規約どおり node → way → relation の順で書き出す(アダプタの
2 パス走査はこの順序を前提とする)。収録内容:
  - place / natural ノード(重複名「中央」で title 弁別も検証)
  - POI ノード(amenity=restaurant。旧版では対象外だったが現在は取り込み対象)
  - 対象外ノード(タグ無し、および name の無い amenity)
  - way 地物(河口湖: 構成ノードの平均座標)
  - admin_centre ノード付き行政境界 relation(京都市)
  - ラベルノード無しの multipolygon relation(琵琶湖: メンバー way 座標の平均)
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
    # 以下はタグ無しの座標解決用ノード
    (6, 135.7681, 35.0116, {}, None),   # 京都市 relation の admin_centre
    (7, 138.7500, 35.5000, {}, None),   # way 100 (河口湖)
    (8, 138.7600, 35.5200, {}, None),
    (9, 138.7700, 35.5100, {}, None),
    (10, 135.9000, 35.0000, {}, None),  # way 101 (relation メンバー)
    (11, 136.1000, 35.2000, {}, None),
]

WAYS = [
    # (id, tags, refs, timestamp)
    (100, {"natural": "water", "name": "河口湖", "name:en": "Lake Kawaguchi"},
     [7, 8, 9], "2026-06-03T00:00:00Z"),
    (101, {}, [10, 11], None),  # タグ無し: relation 経由でのみ使われる
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
