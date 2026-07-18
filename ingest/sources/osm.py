"""OpenStreetMap (Geofabrik 地域抽出) アダプタ。

Nominatim のようなフルジオコーダ(住所補間・逆ジオコーディング)ではなく、
OSM の「名前付き地物」= 地名・行政区・自然地物をコアスキーマに落とし込んだ
ローカル地名辞典(gazetteer)を構築する。region をパラメータ化しており、
Geofabrik にある任意の地域(asia/japan, europe/france, ...)で再利用できる。

データ形式: Geofabrik の <region>-latest.osm.bz2 (OSM XML) を
標準ライブラリ (bz2 + xml.etree.iterparse) のみでストリーミング解析する。
OSM XML は node → way → relation の順に並ぶ規約のため、3 パスで読む:

  パス1: relation を走査し、対象地物の relation とそのメンバー(node/way)を記録
  パス2: way を走査し、対象地物の way と relation メンバー way のノード参照を記録
         (relation セクションに入ったら打ち切り)
  パス3: node を走査し、node の Doc を yield しつつ必要ノードの座標を解決
         (way セクションに入ったら打ち切り)。その後 way / relation の Doc を yield

way / relation の座標は構成ノードの平均(近似重心)。行政境界 relation は
admin_centre / label メンバーノードの座標を優先する。メモリに載せるのは
「対象地物のメタ情報 + 必要ノードの座標」のみで、全ノードは保持しない。
"""
from __future__ import annotations

import bz2
import logging
import math
import os
import re
import subprocess
import urllib.request
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import IO, Iterator

from core import Doc

log = logging.getLogger(__name__)

GEOFABRIK_URL = "https://download.geofabrik.de/"
USER_AGENT = "chiezo-ingest/0.1 (https://github.com/; contact via repo issues)"

# doc_id = osm_id * 4 + タイプコード(node/way/relation の ID 空間は独立のため)
TYPE_CODE = {"node": 0, "way": 1, "relation": 2}

# 取り込み対象の natural=* 値(名前付きのもののみ)
NATURAL_VALUES = {
    "peak", "volcano", "hot_spring", "spring", "bay", "cape", "beach",
    "island", "islet", "peninsula", "isthmus", "strait", "water", "wetland",
    "glacier", "dune", "cliff",
}

# 別名として aliases へ展開するタグ(";" 区切りの複数値に対応)
NAME_KEYS = (
    "name:ja", "name:en", "name:ja-Hira", "name:ja_kana", "name:ja_rm",
    "alt_name", "official_name", "short_name", "old_name", "int_name",
    "loc_name", "nat_name",
)

# opening / body 用の地物種別ラベル
FEATURE_LABEL = {
    ("boundary", "administrative"): "行政境界",
    ("place", "country"): "国",
    ("place", "state"): "州",
    ("place", "province"): "県・州",
    ("place", "region"): "地方",
    ("place", "city"): "市",
    ("place", "county"): "郡",
    ("place", "town"): "町",
    ("place", "village"): "村",
    ("place", "hamlet"): "小集落",
    ("place", "suburb"): "地区",
    ("place", "quarter"): "街区",
    ("place", "neighbourhood"): "近隣地区",
    ("place", "island"): "島",
    ("place", "islet"): "小島",
    ("place", "locality"): "地名",
    ("place", "square"): "広場",
    ("natural", "peak"): "山頂",
    ("natural", "volcano"): "火山",
    ("natural", "hot_spring"): "温泉",
    ("natural", "spring"): "泉",
    ("natural", "bay"): "湾",
    ("natural", "cape"): "岬",
    ("natural", "beach"): "浜",
    ("natural", "island"): "島",
    ("natural", "islet"): "小島",
    ("natural", "peninsula"): "半島",
    ("natural", "isthmus"): "地峡",
    ("natural", "strait"): "海峡",
    ("natural", "water"): "水域",
    ("natural", "wetland"): "湿地",
    ("natural", "glacier"): "氷河",
    ("natural", "dune"): "砂丘",
    ("natural", "cliff"): "崖",
}

# rank_score: place 種別ごとの基礎スコア
PLACE_SCORE = {
    "country": 1.0, "state": 0.9, "province": 0.9, "region": 0.85,
    "city": 0.85, "county": 0.8, "town": 0.7, "island": 0.6,
    "village": 0.55, "suburb": 0.5, "quarter": 0.45, "neighbourhood": 0.4,
    "hamlet": 0.35, "islet": 0.3, "locality": 0.25, "square": 0.25,
}

# 座標解決用のノード参照サンプリング上限(メモリ抑制。重心は近似で十分)
MAX_WAY_NODE_SAMPLES = 50        # 対象地物 way 自身
MAX_MEMBER_WAYS = 30             # relation 1 件あたりのメンバー way
MAX_MEMBER_WAY_NODE_SAMPLES = 10  # メンバー way 1 本あたりのノード参照

DEFAULT_VALIDATION = {
    "osm_japan": {
        "min_docs": 50_000,
        "sample_titles": [
            "東京都", "京都市", "大阪市", "北海道", "富士山",
            "琵琶湖", "渋谷区", "那覇市",
        ],
    },
}


def _sample(values: list, limit: int) -> list:
    """先頭 limit 件のサンプル(重心近似用。全体を保持しない)。"""
    return values[:limit]


class OsmAdapter:
    source_kind = "osm"

    def __init__(
        self,
        source: str,
        region: str,
        lang: str | None = None,
        min_docs: int | None = None,
        sample_titles: list[str] | None = None,
    ):
        self.source = source
        self.region = region  # Geofabrik のパス(例: "asia/japan")
        self.lang = lang
        defaults = DEFAULT_VALIDATION.get(source, {})
        self.min_docs = min_docs if min_docs is not None else defaults.get("min_docs", 1)
        self.sample_titles = (
            sample_titles if sample_titles is not None else defaults.get("sample_titles", [])
        )
        self._seen_titles: set[str] = set()

    # ---- 取得 -------------------------------------------------------------

    def _latest_url(self) -> str:
        return f"{GEOFABRIK_URL}{self.region}-latest.osm.bz2"

    def _remote_date(self, url: str) -> str:
        """Last-Modified ヘッダからダンプ日付 YYYYMMDD を得る(無ければ今日)。"""
        req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=60) as resp:
            last_modified = resp.headers.get("Last-Modified")
        if last_modified:
            return parsedate_to_datetime(last_modified).strftime("%Y%m%d")
        from datetime import datetime, timezone

        return datetime.now(timezone.utc).strftime("%Y%m%d")

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """Geofabrik の最新抽出を取得する。curl -C - で再開可能。

        Geofabrik は「最新」1 世代のみ配布のため、DUMP_DATE は取得対象の固定ではなく
        世代ファイル名のラベル上書きとしてのみ機能する(常に latest を取得)。
        """
        url = self._latest_url()
        date = os.environ.get("DUMP_DATE") or self._remote_date(url)
        dest = workdir / f"{self.source}-{date}.osm.bz2"
        part = dest.with_suffix(".bz2.part")
        if dest.exists() and not part.exists():
            log.info("dump already downloaded: %s", dest)
            return dest, date
        log.info("downloading %s", url)
        subprocess.run(
            ["curl", "-fSL", "-A", USER_AGENT, "--retry", "5", "-C", "-", "-o", str(part), url],
            check=True,
        )
        part.rename(dest)
        return dest, date

    # ---- 変換 -------------------------------------------------------------

    def _open(self, path: Path) -> IO[bytes]:
        if path.suffix == ".bz2":
            return bz2.open(path, "rb")
        return open(path, "rb")

    def _iter_elements(self, path: Path) -> Iterator[ET.Element]:
        """トップレベル要素 (node/way/relation) を end イベントで順に返す。

        処理済み要素をルート(<osm>)から都度切り離さないとツリーが伸び続けるため、
        1 要素 yield するごとに root.clear() でメモリを解放する。
        """
        with self._open(path) as f:
            context = ET.iterparse(f, events=("start", "end"))
            _, root = next(context)  # <osm> ルートの start イベント
            for event, elem in context:
                if event == "end" and elem.tag in ("node", "way", "relation"):
                    yield elem
                    root.clear()

    @staticmethod
    def _tags(elem: ET.Element) -> dict[str, str]:
        return {
            t.get("k", ""): t.get("v", "")
            for t in elem.iter("tag")
            if t.get("k")
        }

    @staticmethod
    def _feature(tags: dict[str, str]) -> tuple[str, str] | None:
        """取り込み対象なら地物種別 (key, value) を返す。対象外は None。"""
        if not tags.get("name"):
            return None
        if tags.get("place"):
            return ("place", tags["place"])
        if tags.get("boundary") == "administrative":
            return ("boundary", "administrative")
        if tags.get("natural") in NATURAL_VALUES:
            return ("natural", tags["natural"])
        return None

    def _scan_relations(self, path: Path) -> tuple[list[dict], set[int]]:
        """パス1: 対象 relation のタグとメンバー参照を収集する。"""
        relations: list[dict] = []
        member_way_ids: set[int] = set()
        for elem in self._iter_elements(path):
            if elem.tag != "relation":
                continue
            tags = self._tags(elem)
            feature = self._feature(tags)
            if feature is None:
                continue
            node_members: list[tuple[int, str]] = []
            way_ids: list[int] = []
            for m in elem.iter("member"):
                ref = m.get("ref")
                if not ref:
                    continue
                if m.get("type") == "node":
                    node_members.append((int(ref), m.get("role", "")))
                elif m.get("type") == "way" and m.get("role", "") in ("outer", ""):
                    way_ids.append(int(ref))
            way_ids = _sample(way_ids, MAX_MEMBER_WAYS)
            member_way_ids.update(way_ids)
            relations.append(
                {
                    "id": int(elem.get("id", 0)),
                    "tags": tags,
                    "node_members": node_members,
                    "way_ids": way_ids,
                    "timestamp": elem.get("timestamp"),
                }
            )
        log.info("pass 1: %d relations of interest", len(relations))
        return relations, member_way_ids

    def _scan_ways(
        self, path: Path, member_way_ids: set[int]
    ) -> tuple[list[dict], dict[int, list[int]]]:
        """パス2: 対象 way と relation メンバー way のノード参照を収集する。"""
        own_ways: list[dict] = []
        member_way_refs: dict[int, list[int]] = {}
        for elem in self._iter_elements(path):
            if elem.tag == "relation":
                break  # way セクション終了
            if elem.tag != "way":
                continue
            way_id = int(elem.get("id", 0))
            is_member = way_id in member_way_ids
            tags = self._tags(elem)
            feature = self._feature(tags)
            if not is_member and feature is None:
                continue
            refs = [int(nd.get("ref", 0)) for nd in elem.iter("nd") if nd.get("ref")]
            if is_member:
                member_way_refs[way_id] = _sample(refs, MAX_MEMBER_WAY_NODE_SAMPLES)
            if feature is not None:
                own_ways.append(
                    {
                        "id": way_id,
                        "tags": tags,
                        "refs": _sample(refs, MAX_WAY_NODE_SAMPLES),
                        "timestamp": elem.get("timestamp"),
                    }
                )
        log.info(
            "pass 2: %d ways of interest, %d member ways", len(own_ways), len(member_way_refs)
        )
        return own_ways, member_way_refs

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        relations, member_way_ids = self._scan_relations(path)
        own_ways, member_way_refs = self._scan_ways(path, member_way_ids)

        needed: set[int] = set()
        for r in relations:
            needed.update(ref for ref, _role in r["node_members"])
        for w in own_ways:
            needed.update(w["refs"])
        for refs in member_way_refs.values():
            needed.update(refs)

        coords: dict[int, tuple[float, float]] = {}
        self._seen_titles = set()
        emitted = 0

        # パス3: node を yield しつつ必要ノードの座標を解決する
        for elem in self._iter_elements(path):
            if elem.tag != "node":
                break  # node セクション終了
            node_id = int(elem.get("id", 0))
            lat_s, lon_s = elem.get("lat"), elem.get("lon")
            if lat_s is None or lon_s is None:
                continue
            lat, lon = float(lat_s), float(lon_s)
            if node_id in needed:
                coords[node_id] = (lat, lon)
            if len(elem) == 0:  # タグ無しノードは大半なので早期スキップ
                continue
            tags = self._tags(elem)
            feature = self._feature(tags)
            if feature is None:
                continue
            yield self._make_doc("node", node_id, tags, feature, lat, lon, elem.get("timestamp"))
            emitted += 1
        log.info("pass 3: %d node docs, resolved %d/%d coords", emitted, len(coords), len(needed))

        for w in own_ways:
            lat, lon = self._centroid(w["refs"], coords)
            feature = self._feature(w["tags"])
            yield self._make_doc("way", w["id"], w["tags"], feature, lat, lon, w["timestamp"])

        for r in relations:
            lat, lon = self._relation_coords(r, coords, member_way_refs)
            feature = self._feature(r["tags"])
            yield self._make_doc(
                "relation", r["id"], r["tags"], feature, lat, lon, r["timestamp"]
            )

    @staticmethod
    def _centroid(
        refs: list[int], coords: dict[int, tuple[float, float]]
    ) -> tuple[float | None, float | None]:
        pts = [coords[ref] for ref in refs if ref in coords]
        if not pts:
            return None, None
        return (
            sum(p[0] for p in pts) / len(pts),
            sum(p[1] for p in pts) / len(pts),
        )

    def _relation_coords(
        self,
        rel: dict,
        coords: dict[int, tuple[float, float]],
        member_way_refs: dict[int, list[int]],
    ) -> tuple[float | None, float | None]:
        """label / admin_centre ノードを優先し、無ければメンバー座標の平均。"""
        for wanted_role in ("label", "admin_centre"):
            for ref, role in rel["node_members"]:
                if role == wanted_role and ref in coords:
                    return coords[ref]
        refs = [ref for ref, _role in rel["node_members"]]
        for way_id in rel["way_ids"]:
            refs.extend(member_way_refs.get(way_id, []))
        return self._centroid(refs, coords)

    # ---- Doc 生成 ---------------------------------------------------------

    def _unique_title(self, name: str, osm_type: str, osm_id: int) -> tuple[str, list[str]]:
        """title の UNIQUE 制約に合わせて重複名を弁別する(先勝ち)。

        2 件目以降は "名前 (node:123)" 形式にし、元の名前を alias に残す
        (doc?title= は alias 解決されるため元の名前でも引ける)。
        """
        if name not in self._seen_titles:
            self._seen_titles.add(name)
            return name, []
        title = f"{name} ({osm_type}:{osm_id})"
        self._seen_titles.add(title)
        return title, [name]

    @staticmethod
    def _int_tag(tags: dict[str, str], key: str) -> int | None:
        raw = tags.get(key)
        if raw is None:
            return None
        digits = re.sub(r"[^\d]", "", raw)
        return int(digits) if digits else None

    def _rank_score(self, feature: tuple[str, str], tags: dict[str, str]) -> float:
        key, value = feature
        if key == "place":
            score = PLACE_SCORE.get(value, 0.3)
        elif key == "boundary":
            level = self._int_tag(tags, "admin_level") or 11
            score = max(0.2, 1.0 - 0.06 * level)
        else:
            score = 0.4
        population = self._int_tag(tags, "population")
        if population:
            score = max(score, min(1.0, math.log10(population) / 8))
        return round(score, 4)

    def _make_doc(
        self,
        osm_type: str,
        osm_id: int,
        tags: dict[str, str],
        feature: tuple[str, str],
        lat: float | None,
        lon: float | None,
        timestamp: str | None,
    ) -> Doc:
        key, value = feature
        name = tags["name"]
        title, aliases = self._unique_title(name, osm_type, osm_id)

        for name_key in NAME_KEYS:
            raw = tags.get(name_key)
            if not raw:
                continue
            for candidate in raw.split(";"):
                candidate = candidate.strip()
                if candidate and candidate != title and candidate not in aliases:
                    aliases.append(candidate)

        label = FEATURE_LABEL.get(feature, f"{key}={value}")
        details = []
        if level := tags.get("admin_level"):
            details.append(f"admin_level={level}")
        if ele := tags.get("ele"):
            details.append(f"標高 {ele}m")
        if population := tags.get("population"):
            details.append(f"人口 {population}")
        if lat is not None and lon is not None:
            details.append(f"座標 {lat:.4f}, {lon:.4f}")
        opening = f"OpenStreetMap の地物: {label}"
        if details:
            opening += "(" + "、".join(details) + ")"

        body_lines = [name]
        body_lines.extend(a for a in aliases if a != name)
        body_lines.append(f"種別: {label} ({key}={value})")
        body_lines.extend(details)
        if is_in := tags.get("is_in"):
            body_lines.append(f"is_in: {is_in}")
        body = "\n".join(body_lines)

        links = []
        wikipedia = tags.get("wikipedia") or tags.get(f"wikipedia:{self.lang}" if self.lang else "")
        if wikipedia:
            # "ja:東京都" 形式の言語プレフィックスを外してタイトルだけ残す
            links.append(wikipedia.split(":", 1)[-1])

        doc_tags = [f"{key}={value}"]
        if level := tags.get("admin_level"):
            doc_tags.append(f"admin_level={level}")

        extra: dict = {
            "osm_type": osm_type,
            "osm_id": osm_id,
            "feature": f"{key}={value}",
        }
        if lat is not None and lon is not None:
            extra["lat"] = round(lat, 7)
            extra["lon"] = round(lon, 7)
        if (level_int := self._int_tag(tags, "admin_level")) is not None:
            extra["admin_level"] = level_int
        if (population_int := self._int_tag(tags, "population")) is not None:
            extra["population"] = population_int
        if wikidata := tags.get("wikidata"):
            extra["wikidata"] = wikidata
        if wikipedia:
            extra["wikipedia"] = wikipedia
        extra["tags"] = tags

        return Doc(
            doc_id=osm_id * 4 + TYPE_CODE[osm_type],
            title=title,
            opening=opening,
            body=body,
            tags=doc_tags,
            links=links,
            aliases=aliases,
            updated_at=timestamp,
            rank_score=self._rank_score(feature, tags),
            extra=extra,
        )
