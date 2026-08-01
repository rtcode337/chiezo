"""OpenStreetMap (Geofabrik 地域抽出) アダプタ。

Nominatim のようなフルジオコーダ(住所補間・逆ジオコーディング)ではなく、
OSM の「名前付き地物」= 地名・行政区・自然地物・主要 POI をコアスキーマに落とし込んだ
ローカル地名辞典 + POI 辞典を構築する。region をパラメータ化しており、
Geofabrik にある任意の地域(asia/japan, europe/france, ...)で再利用できる。

データ形式: Geofabrik の <region>-latest.osm.pbf を pyosmium (libosmium バインディング) で
ストリーミング解析する(旧 .osm.bz2 [OSM XML] は 2026 年に Geofabrik 配布が終了したため、
標準ライブラリのみでの手書き XML パーサから pyosmium 依存へ切り替えた)。

OSM の PBF は node → way → relation の順に並ぶ規約のため、3 パスで読む:

  パス1(_RelationScanHandler): relation を走査し、対象地物の relation が参照する
        way ID(センロイド計算に必要な分のみ、"outer" ロールをサンプリング)を集める。
        あわせて admin_level=4(既定。都道府県相当)の行政境界 relation について、
        メンバー way ID を「間引かずに」集める(パス2 の境界ポリゴン構築用)。
  パス2(_AreaWayHandler): パス1 で集めた境界メンバー way の全ノード座標を読み、
        _AreaIndex(緯度バンド索引付きのレイキャスティング判定器)を組み立てる。
        relation は way より後ろに並ぶため、境界ポリゴンはノード/way の Doc 生成より
        前に用意しておく必要があり、この専用パスを挟んでいる。副作用として
        NodeLocationsForWays の位置インデックスもここで構築済みになる。
  パス3(_MainHandler): pyosmium の NodeLocationsForWays でノード座標を自動解決しながら
        node → way → relation の順で Doc を生成する。node/own-way/relation は該当すれば
        即座に Doc を作って yield し(スレッド + Queue で pyosmium のコールバック駆動を
        ジェネレータへ橋渡し)、relation のセントロイド計算に必要な way だけ座標を
        way_centroids に保持する。label / admin_centre ロールの node 座標は
        NodeLocationsForWays が構築する位置インデックス (idx) に直接問い合わせる
        (way に使われるノードに限らず全ノードの座標を保持しているため引ける)。

取り込み対象:
  - place=*, boundary=administrative, 主要 natural=*(地名辞典。既存)
  - amenity=*, shop=*, tourism=*, leisure=*, historic=*, craft=*, office=*,
    healthcare=*(POI辞典。name タグ必須。旧版では対象外だった)
  - railway / aeroway / aerialway / public_transport / highway / man_made の
    「地点を指す値」だけ(VALUE_LIMITED_KEYS。駅・空港・IC/SA・橋・灯台など)。
    値を限定するのは railway=rail や highway=residential のような線形地物を
    除くため。

座標を持つ Doc には extra.area(所属する都道府県相当の行政区名)を付ける。これにより
API 側で「京都府の amenity=place_of_worship を全件」といった Overpass 相当の絞り込みが
できる。判定は境界ポリゴンへの点内包判定で、bbox 近似のような県境のはみ出しは無い。
"""
from __future__ import annotations

import hashlib
import itertools
import logging
import math
import os
import queue
import re
import subprocess
import threading
import urllib.request
from array import array
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterator

import osmium

from core import (
    LOW_MEMORY_BUILD_GB,
    POPULARITY_LOG_MAX_CITY_POPULATION,
    Doc,
    is_low_memory_build,
    normalized_popularity,
)

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

# 取り込み対象の POI タグキー(name タグ必須。値は問わない)
POI_KEYS = ("amenity", "shop", "tourism", "leisure", "historic", "craft", "office", "healthcare")

# 値を限定して取り込むタグキー(name タグ必須)。
#
# 交通インフラ(駅・空港・港・IC/SA)は place/POI のどのキーにも属さず、旧版では
# 丸ごと取りこぼしていた。訪問地や経路を扱う用途では「倉敷駅」「青森空港」
# 「北熊本サービスエリア」のような地点が引けないのが最も痛いため、専用に対象へ加える。
# ただし値を問わずに取り込むと railway=rail(線路そのもの)や highway=residential(道路)のような、
# 名前付きではあっても地点辞典として意味を成さない線形地物が大量に混ざるため、
# 「その場所を指す」値だけを列挙する方式にしている。
# (港湾は amenity=ferry_terminal / harbour として既に POI_KEYS 側で拾える)
VALUE_LIMITED_KEYS: dict[str, set[str]] = {
    "natural": NATURAL_VALUES,
    "railway": {"station", "halt", "tram_stop"},
    "aeroway": {"aerodrome", "terminal", "helipad", "heliport"},
    "aerialway": {"station"},
    # 鉄道タグを持たない地下鉄・バスの結節点や、駅を表す stop_area relation を拾う。
    # railway=* より後ろに置き、駅ノードとの重複は title の弁別に任せる。
    "public_transport": {"station"},
    "highway": {"services", "rest_area", "motorway_junction", "toll_gate"},
    "man_made": {"bridge", "lighthouse", "pier", "tower"},
}

# 別名として aliases へ展開するタグ(";" 区切りの複数値に対応)
NAME_KEYS = (
    "name:ja", "name:en", "name:ja-Hira", "name:ja_kana", "name:ja_rm",
    "alt_name", "official_name", "short_name", "old_name", "int_name",
    "loc_name", "nat_name",
)

# body / extra へ拾い上げる住所・連絡先タグ
ADDR_KEYS = ("addr:postcode", "addr:city", "addr:street", "addr:housenumber")
CONTACT_KEYS = {
    "phone": ("phone", "contact:phone"),
    "website": ("website", "contact:website", "url"),
    "opening_hours": ("opening_hours",),
}

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
    # 主要 POI(未列挙の値は "key=value" にフォールバックする)
    ("amenity", "hospital"): "病院",
    ("amenity", "clinic"): "診療所",
    ("amenity", "school"): "学校",
    ("amenity", "university"): "大学",
    ("amenity", "kindergarten"): "幼稚園・保育園",
    ("amenity", "library"): "図書館",
    ("amenity", "restaurant"): "レストラン",
    ("amenity", "cafe"): "カフェ",
    ("amenity", "bank"): "銀行",
    ("amenity", "post_office"): "郵便局",
    ("amenity", "police"): "警察署",
    ("amenity", "fire_station"): "消防署",
    ("amenity", "place_of_worship"): "宗教施設",
    ("amenity", "townhall"): "役場",
    ("amenity", "parking"): "駐車場",
    ("amenity", "fuel"): "ガソリンスタンド",
    ("shop", "supermarket"): "スーパーマーケット",
    ("shop", "convenience"): "コンビニ",
    ("shop", "department_store"): "百貨店",
    ("shop", "mall"): "ショッピングモール",
    ("shop", "bakery"): "パン屋",
    ("tourism", "hotel"): "ホテル",
    ("tourism", "museum"): "博物館・美術館",
    ("tourism", "attraction"): "観光名所",
    ("tourism", "viewpoint"): "展望地点",
    ("tourism", "information"): "観光案内所",
    ("leisure", "park"): "公園",
    ("leisure", "stadium"): "スタジアム",
    ("leisure", "sports_centre"): "スポーツセンター",
    ("historic", "castle"): "城",
    ("historic", "monument"): "記念碑",
    ("historic", "memorial"): "慰霊碑",
    ("historic", "ruins"): "史跡・遺跡",
    ("healthcare", "hospital"): "病院",
    ("healthcare", "pharmacy"): "薬局",
    ("amenity", "ferry_terminal"): "フェリーターミナル・港",
    ("amenity", "bus_station"): "バスターミナル",
    # 交通インフラ
    ("railway", "station"): "駅",
    ("railway", "halt"): "停留所",
    ("railway", "tram_stop"): "停留場",
    ("aeroway", "aerodrome"): "空港",
    ("aeroway", "terminal"): "空港ターミナル",
    ("aeroway", "helipad"): "ヘリポート",
    ("aeroway", "heliport"): "ヘリポート",
    ("aerialway", "station"): "索道駅",
    ("public_transport", "station"): "駅・ターミナル",
    ("highway", "services"): "サービスエリア",
    ("highway", "rest_area"): "パーキングエリア",
    ("highway", "motorway_junction"): "インターチェンジ",
    ("highway", "toll_gate"): "料金所",
    ("man_made", "bridge"): "橋",
    ("man_made", "lighthouse"): "灯台",
    ("man_made", "pier"): "桟橋",
    ("man_made", "tower"): "塔",
}

# rank_score: place 種別ごとの基礎スコア
PLACE_SCORE = {
    "country": 1.0, "state": 0.9, "province": 0.9, "region": 0.85,
    "city": 0.85, "county": 0.8, "town": 0.7, "island": 0.6,
    "village": 0.55, "suburb": 0.5, "quarter": 0.45, "neighbourhood": 0.4,
    "hamlet": 0.35, "islet": 0.3, "locality": 0.25, "square": 0.25,
}

# rank_score: POI 種別キーごとの基礎スコア(タグキー単位。値までは分けない)
# 交通インフラを POI より高く置くのは、「博多駅」のような名前で駅より先に同名の
# 飲食店が返る取り違えを rank_score の段階で防ぐため。
POI_KEY_SCORE = {
    "aeroway": 0.65, "railway": 0.6, "public_transport": 0.55, "aerialway": 0.5,
    "highway": 0.5, "tourism": 0.45, "historic": 0.45, "healthcare": 0.4,
    "amenity": 0.4, "man_made": 0.35, "leisure": 0.35, "shop": 0.3,
    "office": 0.25, "craft": 0.25,
}

# 座標解決用のノード参照サンプリング上限(メモリ抑制。重心は近似で十分)
MAX_WAY_NODE_SAMPLES = 50        # 対象地物 way 自身
MAX_MEMBER_WAYS = 30             # relation 1 件あたりのメンバー way
MAX_MEMBER_WAY_NODE_SAMPLES = 10  # メンバー way 1 本あたりのノード参照

# extra.area に入れる行政区の admin_level。日本では 4 = 都道府県。
# 0 を指定すると境界ポリゴンのパスごとスキップする(旧来の 2 パス動作に戻る)。
AREA_ADMIN_LEVEL = int(os.environ.get("OSM_AREA_ADMIN_LEVEL", "4"))

# 点内包判定用の緯度バンド幅(度)。1 バンド ≒ 110m。細かくすると 1 点あたりの
# 辺の判定数が減る代わりに索引が太る。0.001 で 1 県 2000 バンド前後になる。
AREA_BAND_DEGREES = 0.001

# パス3 のコールバック駆動をジェネレータへ橋渡しする Queue の上限(メモリ抑制)
QUEUE_MAXSIZE = 1000
_SENTINEL = object()

# 重複タイトル判定用ビット配列のサイズ(2^31 bit = 256MiB 固定)とハッシュ数。
# osm_japan 規模では set[str] でも問題なかったが、osm_europe(大陸単位、
# 数千万〜億件規模の名前付き地物)では全タイトル文字列を set[str] に貯めると
# メモリを食い尽くして OS ごとスワップで暴走したため、コーパス規模によらず
# 固定サイズで済むビットフィルタに置き換えた。
_TITLE_BLOOM_BITS = 1 << 31
_TITLE_BLOOM_HASHES = 7


class _TitleBloomFilter:
    """「このタイトルは前に見たか」を固定メモリで概算判定する(誤検出のみ許容)。

    docs.title の UNIQUE 制約回避のための弁別要否判定にしか使わないため、
    誤検出(未出現なのに「出現済み」と判定)は不要な "(type:id)" 弁別を
    余分に招くだけで実害はない(alias に元名が残るため引ける)。逆に
    見逃し(出現済みなのに「未出現」と判定)は起きない設計
    (ビットフィルタは false positive のみで false negative は原理上出ない)。
    """

    def __init__(self, size_bits: int = _TITLE_BLOOM_BITS, num_hashes: int = _TITLE_BLOOM_HASHES):
        self._size = size_bits
        self._k = num_hashes
        self._bits = bytearray(size_bits // 8)

    def _indexes(self, item: str) -> Iterator[int]:
        digest = hashlib.blake2b(item.encode("utf-8"), digest_size=16).digest()
        h1 = int.from_bytes(digest[:8], "little")
        h2 = int.from_bytes(digest[8:], "little")
        for i in range(self._k):
            yield (h1 + i * h2) % self._size

    def add_and_test(self, item: str) -> bool:
        """ビットを立てつつ、追加前から既に立っていたか(≒出現済みの可能性)を返す。"""
        seen = True
        for idx in self._indexes(item):
            byte_idx, bit_idx = divmod(idx, 8)
            mask = 1 << bit_idx
            if not (self._bits[byte_idx] & mask):
                seen = False
                self._bits[byte_idx] |= mask
        return seen


DEFAULT_VALIDATION = {
    "osm_japan": {
        "min_docs": 50_000,
        "sample_titles": [
            "東京都", "京都市", "大阪市", "北海道", "富士山",
            "琵琶湖", "渋谷区", "那覇市",
            # 交通インフラ(VALUE_LIMITED_KEYS)が取り込まれていることの検証も兼ねる
            "東京駅", "東京国際空港",
        ],
    },
}


def _feature(tags: dict[str, str]) -> tuple[str, str] | None:
    """取り込み対象なら地物種別 (key, value) を返す。対象外は None。"""
    if not tags.get("name"):
        return None
    if tags.get("place"):
        return ("place", tags["place"])
    if tags.get("boundary") == "administrative":
        return ("boundary", "administrative")
    for key, values in VALUE_LIMITED_KEYS.items():
        if tags.get(key) in values:
            return (key, tags[key])
    for key in POI_KEYS:
        if tags.get(key):
            return (key, tags[key])
    return None


def _centroid(pts: list[tuple[float, float]]) -> tuple[float | None, float | None]:
    if not pts:
        return None, None
    return sum(p[0] for p in pts) / len(pts), sum(p[1] for p in pts) / len(pts)


def _way_centroid(way, limit: int) -> tuple[float | None, float | None]:
    pts = [
        (nd.lat, nd.lon)
        for nd in itertools.islice(way.nodes, limit)
        if nd.location.valid()
    ]
    return _centroid(pts)


class _AreaIndex:
    """行政境界ポリゴンへの点内包判定器(緯度バンド索引付きレイキャスティング)。

    境界 relation のメンバー way は「閉じた環をなす折れ線の集合」なので、環を組み立て
    直さなくても、全メンバー way の辺をそのまま集めてレイキャスティングの交差数を
    数えれば内外を判定できる(奇数なら内側)。inner ロール(飛び地の穴)の辺も同じ
    集合に混ぜてよく、交差数の偶奇が自動的に穴を打ち消す。

    素朴に全辺を走査すると 1 県あたり数十万辺 × 全 POI で到底終わらないため、
    辺を緯度バンド(AREA_BAND_DEGREES 幅)ごとにバケットへ振り分け、判定時は点の緯度が
    属するバンドの辺だけを見る。さらに整数緯度で候補地域を絞ってから bbox で弾く。
    """

    def __init__(self, band: float = AREA_BAND_DEGREES):
        self._band = band
        # 地域名 → 辺の平坦配列 [lat1, lon1, lat2, lon2, ...]
        self._edges: dict[str, array] = {}
        self._bbox: dict[str, list[float]] = {}  # 地域名 → [min_lat, min_lon, max_lat, max_lon]
        self._bands: dict[str, dict[int, array]] = {}
        self._by_degree: dict[int, list[str]] = {}

    def add_polyline(self, area: str, points: list[tuple[float, float]]) -> None:
        """メンバー way 1 本分の折れ線を、その地域の辺集合へ追加する。"""
        if len(points) < 2:
            return
        edges = self._edges.get(area)
        if edges is None:
            edges = self._edges[area] = array("d")
            self._bbox[area] = [90.0, 180.0, -90.0, -180.0]
        box = self._bbox[area]
        prev_lat, prev_lon = points[0]
        for lat, lon in points[1:]:
            edges.extend((prev_lat, prev_lon, lat, lon))
            prev_lat, prev_lon = lat, lon
        for lat, lon in points:
            box[0] = min(box[0], lat)
            box[1] = min(box[1], lon)
            box[2] = max(box[2], lat)
            box[3] = max(box[3], lon)

    def finalize(self) -> None:
        band = self._band
        for area, edges in self._edges.items():
            buckets: dict[int, array] = {}
            for i in range(0, len(edges), 4):
                lat1, lat2 = edges[i], edges[i + 2]
                lo = int(math.floor(min(lat1, lat2) / band))
                hi = int(math.floor(max(lat1, lat2) / band))
                for b in range(lo, hi + 1):
                    bucket = buckets.get(b)
                    if bucket is None:
                        bucket = buckets[b] = array("i")
                    bucket.append(i)
            self._bands[area] = buckets
            box = self._bbox[area]
            for deg in range(int(math.floor(box[0])), int(math.floor(box[2])) + 1):
                self._by_degree.setdefault(deg, []).append(area)
        log.info(
            "area index: %d areas, %d edges total",
            len(self._edges), sum(len(e) // 4 for e in self._edges.values()),
        )

    def __len__(self) -> int:
        return len(self._edges)

    def find(self, lat: float, lon: float) -> str | None:
        """点を含む地域名を返す(県境上など複数該当時は最初の 1 件)。"""
        candidates = self._by_degree.get(int(math.floor(lat)))
        if not candidates:
            return None
        band_key = int(math.floor(lat / self._band))
        for area in candidates:
            box = self._bbox[area]
            if not (box[0] <= lat <= box[2] and box[1] <= lon <= box[3]):
                continue
            bucket = self._bands[area].get(band_key)
            if not bucket:
                continue
            edges = self._edges[area]
            crossings = 0
            for i in bucket:
                lat1, lon1, lat2, lon2 = edges[i], edges[i + 1], edges[i + 2], edges[i + 3]
                if (lat1 > lat) != (lat2 > lat):
                    # 辺が点の緯度を跨ぐ → 交点の経度が点より東側なら 1 交差
                    if lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1) > lon:
                        crossings += 1
            if crossings % 2:
                return area
        return None


class _RelationScanHandler(osmium.SimpleHandler):
    """パス1: 対象 relation が参照する way ID を集める(センロイド計算に必要な分だけ)。

    あわせて、extra.area 用の行政境界 relation(admin_level == area_admin_level)の
    メンバー way を間引かずに集める。こちらは重心ではなくポリゴンそのものが要るため、
    MAX_MEMBER_WAYS のサンプリングを適用してはいけない。
    """

    def __init__(self, area_admin_level: int = AREA_ADMIN_LEVEL):
        super().__init__()
        self.area_admin_level = area_admin_level
        self.member_way_ids: set[int] = set()
        self.relation_count = 0
        # 境界メンバー way id → その way を共有する地域名(県境の way は複数県に属する)
        self.area_way_owners: dict[int, list[str]] = {}
        self.area_names: set[str] = set()

    def relation(self, r) -> None:
        tags = {t.k: t.v for t in r.tags}
        if _feature(tags) is None:
            return
        self.relation_count += 1
        way_ids = (m.ref for m in r.members if m.type == "w" and m.role in ("outer", ""))
        for ref in itertools.islice(way_ids, MAX_MEMBER_WAYS):
            self.member_way_ids.add(ref)
        self._collect_area(r, tags)

    def _collect_area(self, r, tags: dict[str, str]) -> None:
        if not self.area_admin_level:
            return
        if tags.get("boundary") != "administrative":
            return
        if tags.get("admin_level") != str(self.area_admin_level):
            return
        name = tags.get("name")
        if not name:
            return
        self.area_names.add(name)
        for m in r.members:
            if m.type == "w" and m.role in ("outer", "inner", ""):
                self.area_way_owners.setdefault(m.ref, []).append(name)


class _AreaWayHandler(osmium.SimpleHandler):
    """パス2: 境界メンバー way の全ノード座標を読み、_AreaIndex を組み立てる。"""

    def __init__(self, owners: dict[int, list[str]], index: _AreaIndex):
        super().__init__()
        self.owners = owners
        self.index = index
        self.way_count = 0

    def way(self, w) -> None:
        areas = self.owners.get(w.id)
        if not areas:
            return
        points = [(nd.lat, nd.lon) for nd in w.nodes if nd.location.valid()]
        if len(points) < 2:
            return
        for area in areas:
            self.index.add_polyline(area, points)
        self.way_count += 1


class _MainHandler(osmium.SimpleHandler):
    """パス2: node/way/relation を順に処理し、Doc を q へ積む。"""

    def __init__(self, adapter: "OsmAdapter", member_way_ids: set[int], idx, q: "queue.Queue"):
        super().__init__()
        self.adapter = adapter
        self.member_way_ids = member_way_ids
        self.idx = idx
        self.q = q
        self.way_centroids: dict[int, tuple[float | None, float | None]] = {}
        self.node_count = 0
        self.way_count = 0
        self.relation_count = 0

    def node(self, n) -> None:
        if not n.location.valid():
            return
        tags = {t.k: t.v for t in n.tags}
        feature = _feature(tags)
        if feature is None:
            return
        doc = self.adapter._make_doc(
            "node", n.id, tags, feature, n.location.lat, n.location.lon,
            n.timestamp.isoformat() if n.timestamp else None,
        )
        self.q.put(doc)
        self.node_count += 1

    def way(self, w) -> None:
        if w.id in self.member_way_ids:
            self.way_centroids[w.id] = _way_centroid(w, MAX_MEMBER_WAY_NODE_SAMPLES)
        tags = {t.k: t.v for t in w.tags}
        feature = _feature(tags)
        if feature is None:
            return
        lat, lon = _way_centroid(w, MAX_WAY_NODE_SAMPLES)
        doc = self.adapter._make_doc(
            "way", w.id, tags, feature, lat, lon,
            w.timestamp.isoformat() if w.timestamp else None,
        )
        self.q.put(doc)
        self.way_count += 1

    def relation(self, r) -> None:
        tags = {t.k: t.v for t in r.tags}
        feature = _feature(tags)
        if feature is None:
            return
        lat, lon = self._relation_coords(r)
        doc = self.adapter._make_doc(
            "relation", r.id, tags, feature, lat, lon,
            r.timestamp.isoformat() if r.timestamp else None,
        )
        self.q.put(doc)
        self.relation_count += 1

    def _node_location(self, node_id: int) -> tuple[float, float] | None:
        try:
            loc = self.idx.get(node_id)
        except KeyError:
            return None
        return (loc.lat, loc.lon) if loc.valid() else None

    def _relation_coords(self, r) -> tuple[float | None, float | None]:
        """label / admin_centre ノードを優先し、無ければメンバー座標の平均。"""
        for wanted_role in ("label", "admin_centre"):
            for m in r.members:
                if m.type == "n" and m.role == wanted_role:
                    if loc := self._node_location(m.ref):
                        return loc
        pts: list[tuple[float, float]] = []
        for m in r.members:
            if m.type == "n":
                if loc := self._node_location(m.ref):
                    pts.append(loc)
            elif m.type == "w" and m.role in ("outer", ""):
                lat, lon = self.way_centroids.get(m.ref, (None, None))
                if lat is not None:
                    pts.append((lat, lon))
        return _centroid(pts)


class OsmAdapter:
    source_kind = "osm"

    def __init__(
        self,
        source: str,
        region: str,
        lang: str | None = None,
        min_docs: int | None = None,
        sample_titles: list[str] | None = None,
        area_admin_level: int = AREA_ADMIN_LEVEL,
        ram_index_memory_gb: float = 12.0,
        default_node_index: str = "sparse_mmap_array",
    ):
        self.source = source
        self.region = region  # Geofabrik のパス(例: "asia/japan")
        self.lang = lang
        self.area_admin_level = area_admin_level
        # RAM 上のノード座標インデックスを使う場合に必要なメモリの目安(抽出範囲で変わる)
        self.ram_index_memory_gb = ram_index_memory_gb
        # このソースの既定のノード座標インデックス方式。国スケールまでは RAM 索引が速くて
        # 現実的だが、大陸スケールでは RAM に載る量ではないためディスク索引を既定にする。
        # どちらも環境変数 OSM_NODE_INDEX で上書きできる。
        self.default_node_index = default_node_index
        self._area_index: _AreaIndex | None = None
        defaults = DEFAULT_VALIDATION.get(source, {})
        self.min_docs = min_docs if min_docs is not None else defaults.get("min_docs", 1)
        self.sample_titles = (
            sample_titles if sample_titles is not None else defaults.get("sample_titles", [])
        )
        # ビットフィルタは 256MiB 固定。国別ソースが 195 件あり、レジストリの走査などで
        # アダプタだけ作られる場面も多いため、iter_docs で実際に使うまで確保しない。
        self._seen_titles: _TitleBloomFilter | None = None

    @property
    def node_index_kind(self) -> str:
        """実際に使うノード座標インデックス方式。

        優先順は 環境変数 OSM_NODE_INDEX > BUILD_PROFILE=low_memory > ソースごとの既定。
        明示指定を最優先にするのは、low_memory のまま「この 1 ソースだけは RAM 索引で
        速く焼く」という使い方を残すため。
        """
        if explicit := os.environ.get("OSM_NODE_INDEX"):
            return explicit
        if is_low_memory_build():
            return "sparse_file_array"
        return self.default_node_index

    @property
    def min_build_memory_gb(self) -> float:
        """必要メモリの目安。ノード座標インデックスをどこに置くかで桁が変わる。

        RAM 索引は参照ノードぶんの座標を抱えるため抽出範囲に比例して大きい(日本抽出で
        5〜10GB、欧州大陸なら 70GB 超と実機では成立しない)。ディスク索引なら常駐は
        ビットフィルタ(256MiB)+ SQLite キャッシュ + 境界ポリゴン程度で済む。
        """
        if self.node_index_kind.endswith("file_array"):
            return LOW_MEMORY_BUILD_GB
        return self.ram_index_memory_gb

    # ---- 取得 -------------------------------------------------------------

    def _latest_url(self) -> str:
        return f"{GEOFABRIK_URL}{self.region}-latest.osm.pbf"

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
        dest = workdir / f"{self.source}-{date}.osm.pbf"
        part = dest.with_suffix(".pbf.part")
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

    @staticmethod
    def _feature(tags: dict[str, str]) -> tuple[str, str] | None:
        return _feature(tags)

    def _build_area_index(self, path: Path, scan: _RelationScanHandler, lh) -> _AreaIndex | None:
        """パス2: 境界メンバー way を読んで点内包判定器を作る(位置インデックスも埋まる)。"""
        if not scan.area_way_owners:
            if self.area_admin_level:
                log.warning(
                    "no admin_level=%d boundaries found; extra.area will be omitted",
                    self.area_admin_level,
                )
            return None
        index = _AreaIndex()
        handler = _AreaWayHandler(scan.area_way_owners, index)
        osmium.apply(str(path), lh, handler)
        index.finalize()
        log.info(
            "pass 2: %d boundary ways for %d areas (admin_level=%d)",
            handler.way_count, len(index), self.area_admin_level,
        )
        return index if len(index) else None

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        scan = _RelationScanHandler(self.area_admin_level)
        scan.apply_file(str(path))
        log.info(
            "pass 1: %d relations of interest, %d member ways, %d areas",
            scan.relation_count, len(scan.member_way_ids), len(scan.area_names),
        )

        # ノード座標インデックス。方式は node_index_kind(環境変数 > ソースごとの既定)で決まる。
        # 国スケール(osm_japan)は RAM 索引が既定で、これが一番速い(参照ノードぶんの座標を
        # 抱えるため 5〜10GB 使う)。大陸スケール(osm_europe)は RAM に載る量ではないので
        # ディスク索引が既定。ディスク索引は RSS が 2GB 程度まで下がる代わりに、ノード解決が
        # ランダム読みになって数倍〜10 倍遅い。足りるかどうかは実行前に
        # require_build_memory() が min_build_memory_gb と突き合わせて検査する。
        # 一時ファイルは下の finally で必ず消す(中断時も残さない。lookup.py と同じ精神)。
        index_kind = self.node_index_kind
        if index_kind.endswith("file_array"):
            idx_path = path.parent / f"{self.source}.nodeloc.idx"
            idx_path.unlink(missing_ok=True)
            idx = osmium.index.create_map(f"{index_kind},{idx_path}")
        else:
            idx_path = None
            idx = osmium.index.create_map(index_kind)
        log.info("node location index: %s", index_kind)
        lh = osmium.NodeLocationsForWays(idx)
        lh.ignore_errors()

        self._area_index = self._build_area_index(path, scan, lh)

        self._seen_titles = _TitleBloomFilter()
        q: queue.Queue = queue.Queue(maxsize=QUEUE_MAXSIZE)

        def run() -> None:
            handler = _MainHandler(self, scan.member_way_ids, idx, q)
            try:
                osmium.apply(str(path), lh, handler)
                log.info(
                    "pass 2: %d node docs, %d way docs, %d relation docs",
                    handler.node_count, handler.way_count, handler.relation_count,
                )
            except Exception as exc:  # スレッド内例外をメインスレッドへ伝播する
                q.put(exc)
            finally:
                q.put(_SENTINEL)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            while True:
                item = q.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, Exception):
                    raise item
                yield item
        finally:
            thread.join()
            if idx_path is not None:
                idx_path.unlink(missing_ok=True)

    # ---- Doc 生成 ---------------------------------------------------------

    def _unique_title(self, name: str, osm_type: str, osm_id: int) -> tuple[str, list[str]]:
        """title の UNIQUE 制約に合わせて重複名を弁別する(先勝ち)。

        2 件目以降は "名前 (node:123)" 形式にし、元の名前を alias に残す
        (doc?title= は alias 解決されるため元の名前でも引ける)。
        """
        if self._seen_titles is None:
            self._seen_titles = _TitleBloomFilter()
        if not self._seen_titles.add_and_test(name):
            return name, []
        title = f"{name} ({osm_type}:{osm_id})"
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
            score = POI_KEY_SCORE.get(key, 0.4)
        population = self._int_tag(tags, "population")
        if population:
            score = max(score, normalized_popularity(population, POPULARITY_LOG_MAX_CITY_POPULATION))
        return round(score, 4)

    @staticmethod
    def _address(tags: dict[str, str]) -> str | None:
        if full := tags.get("addr:full"):
            return full
        parts = [tags.get(k) for k in ("addr:postcode", "addr:city", "addr:street", "addr:housenumber")]
        parts = [p for p in parts if p]
        return " ".join(parts) if parts else None

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
        address = self._address(tags)
        area = (
            self._area_index.find(lat, lon)
            if self._area_index is not None and lat is not None and lon is not None
            else None
        )
        details = []
        if area:
            details.append(f"所在 {area}")
        if level := tags.get("admin_level"):
            details.append(f"admin_level={level}")
        if ele := tags.get("ele"):
            details.append(f"標高 {ele}m")
        if population := tags.get("population"):
            details.append(f"人口 {population}")
        if address:
            details.append(f"住所 {address}")
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
        for extra_key, source_keys in CONTACT_KEYS.items():
            for source_key in source_keys:
                if contact_value := tags.get(source_key):
                    body_lines.append(f"{extra_key}: {contact_value}")
                    break
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
        if area:
            extra["area"] = area
        if (level_int := self._int_tag(tags, "admin_level")) is not None:
            extra["admin_level"] = level_int
        if (population_int := self._int_tag(tags, "population")) is not None:
            extra["population"] = population_int
        if wikidata := tags.get("wikidata"):
            extra["wikidata"] = wikidata
        if wikipedia:
            extra["wikipedia"] = wikipedia
        if address:
            extra["address"] = address
        for extra_key, source_keys in CONTACT_KEYS.items():
            for source_key in source_keys:
                if contact_value := tags.get(source_key):
                    extra[extra_key] = contact_value
                    break
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
