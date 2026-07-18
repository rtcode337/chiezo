"""テスト用の小型 OSM XML ダンプ fixtures/mini_osm.osm.bz2 を生成する。

再生成: python tests/fixtures/make_osm_fixture.py

OSM XML の規約どおり node → way → relation の順で並べる(アダプタの
3 パス走査はこの順序を前提とする)。収録内容:
  - place / natural ノード(重複名「中央」で title 弁別も検証)
  - 対象外ノード(amenity=restaurant)
  - way 地物(河口湖: 構成ノードの平均座標)
  - admin_centre ノード付き行政境界 relation(京都市)
  - ラベルノード無しの multipolygon relation(琵琶湖: メンバー way 座標の平均)
"""
from __future__ import annotations

import bz2
import xml.etree.ElementTree as ET
from pathlib import Path

NODES = [
    # (id, lat, lon, tags, timestamp)
    (1, 35.6895, 139.6917, {
        "place": "city", "name": "東京", "name:en": "Tokyo", "name:ja": "東京",
        "population": "13960000", "wikipedia": "ja:東京都区部", "wikidata": "Q7473516",
    }, "2026-06-01T00:00:00Z"),
    (2, 35.3606, 138.7274, {
        "natural": "peak", "name": "富士山", "name:en": "Mount Fuji",
        "ele": "3776", "alt_name": "富士の山;不二山",
    }, "2026-06-02T00:00:00Z"),
    (3, 35.6700, 139.7700, {"place": "neighbourhood", "name": "中央"}, None),
    (4, 34.6900, 135.5000, {"place": "suburb", "name": "中央"}, None),
    (5, 35.0000, 135.0000, {"amenity": "restaurant", "name": "ラーメン一番"}, None),
    # 以下はタグ無しの座標解決用ノード
    (6, 35.0116, 135.7681, {}, None),   # 京都市 relation の admin_centre
    (7, 35.5000, 138.7500, {}, None),   # way 100 (河口湖)
    (8, 35.5200, 138.7600, {}, None),
    (9, 35.5100, 138.7700, {}, None),
    (10, 35.0000, 135.9000, {}, None),  # way 101 (relation メンバー)
    (11, 35.2000, 136.1000, {}, None),
]

WAYS = [
    # (id, tags, nd_refs, timestamp)
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
    }, [("node", 6, "admin_centre"), ("way", 101, "outer")], "2026-06-04T00:00:00Z"),
    (201, {"type": "multipolygon", "natural": "water", "name": "琵琶湖"},
     [("way", 101, "outer")], "2026-06-05T00:00:00Z"),
]


def main() -> None:
    root = ET.Element("osm", version="0.6", generator="chiezo-test-fixture")
    for node_id, lat, lon, tags, ts in NODES:
        attrs = {"id": str(node_id), "lat": str(lat), "lon": str(lon)}
        if ts:
            attrs["timestamp"] = ts
        elem = ET.SubElement(root, "node", attrs)
        for k, v in tags.items():
            ET.SubElement(elem, "tag", k=k, v=v)
    for way_id, tags, refs, ts in WAYS:
        attrs = {"id": str(way_id)}
        if ts:
            attrs["timestamp"] = ts
        elem = ET.SubElement(root, "way", attrs)
        for ref in refs:
            ET.SubElement(elem, "nd", ref=str(ref))
        for k, v in tags.items():
            ET.SubElement(elem, "tag", k=k, v=v)
    for rel_id, tags, members, ts in RELATIONS:
        attrs = {"id": str(rel_id)}
        if ts:
            attrs["timestamp"] = ts
        elem = ET.SubElement(root, "relation", attrs)
        for mtype, ref, role in members:
            ET.SubElement(elem, "member", type=mtype, ref=str(ref), role=role)
        for k, v in tags.items():
            ET.SubElement(elem, "tag", k=k, v=v)

    out = Path(__file__).parent / "mini_osm.osm.bz2"
    xml_bytes = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    with bz2.open(out, "wb") as f:
        f.write(xml_bytes)
    print(f"wrote {out} ({len(NODES)} nodes, {len(WAYS)} ways, {len(RELATIONS)} relations)")


if __name__ == "__main__":
    main()
