"""OpenStreetMap(Geofabrik)の国別抽出カタログ。

**自動生成物。手で編集せず `python3 scripts/gen_osm_regions.py` で作り直すこと。**

`sources/__init__.py` はこの表から `osm_<国>` のアダプタを一括生成する。国別抽出は
200 以上あり手書きでは追随できないうえ、region パスを 1 文字間違えるとダウンロード時
まで気づけないため、Geofabrik の公式索引(index-v1.json)から機械的に起こしている。

各項目の意味:
  region      Geofabrik のパス。`<region>-latest.osm.pbf` を落とす
  label       管理画面に出す表示名(日本語。CLDR 由来。取れない地域は英名のまま)
  lang        その国の主要言語(CLDR territoryInfo)。`wikipedia:<lang>` タグの解決に使う
  pbf_bytes   生成時点の pbf サイズ。必要メモリ・構築時間・ディスクの目安の素
  memory_gb   RAM 索引で構築する場合に要るメモリの目安(pbf 1GB あたり 5GiB)
  node_index  既定のノード座標索引。12GiB を超える国はディスク索引を既定にする
              (RAM に載らないため。遅くなる代わりに 2GiB で焼ける)
  min_docs    検証で要求する最低文書数(pbf サイズから起こした保守的な下限)

サイズは日々増えるので memory_gb / min_docs はあくまで目安。実際に足りるかは取り込み
開始前のメモリ検査(ingest/main.py の require_build_memory)が実測で判定する。
"""
from __future__ import annotations

from typing import NamedTuple


class OsmRegion(NamedTuple):
    slug: str          # Geofabrik 側の識別子(ハイフン区切り)
    source: str        # Chiezo のソース名(osm_<国>。区切りはアンダースコア)
    region: str        # Geofabrik のパス(例: asia/japan)
    continent: str     # 大陸(表示のグルーピング用。russia / antarctica は standalone)
    label: str         # 表示名(日本語。無ければ英名)
    label_en: str
    lang: str | None   # 主要言語コード(取れなければ None)
    pbf_bytes: int
    memory_gb: float
    node_index: str
    min_docs: int


# 大陸の表示順(管理画面の国選択で使う)
CONTINENTS: tuple[str, ...] = (
    "africa",
    "asia",
    "australia-oceania",
    "central-america",
    "europe",
    "north-america",
    "south-america",
    "standalone",
)

OSM_REGIONS: dict[str, OsmRegion] = {
    "algeria": OsmRegion(
        slug="algeria", source="osm_algeria", region="africa/algeria",
        continent="africa", label="アルジェリア", label_en="Algeria",
        lang="ar", pbf_bytes=285000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=5700,
    ),
    "angola": OsmRegion(
        slug="angola", source="osm_angola", region="africa/angola",
        continent="africa", label="アンゴラ", label_en="Angola",
        lang="pt", pbf_bytes=80000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1600,
    ),
    "benin": OsmRegion(
        slug="benin", source="osm_benin", region="africa/benin",
        continent="africa", label="ベナン", label_en="Benin",
        lang="fr", pbf_bytes=45600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=912,
    ),
    "botswana": OsmRegion(
        slug="botswana", source="osm_botswana", region="africa/botswana",
        continent="africa", label="ボツワナ", label_en="Botswana",
        lang="en", pbf_bytes=83000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1660,
    ),
    "burkina-faso": OsmRegion(
        slug="burkina-faso", source="osm_burkina_faso", region="africa/burkina-faso",
        continent="africa", label="ブルキナファソ", label_en="Burkina Faso",
        lang="fr", pbf_bytes=80000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1600,
    ),
    "burundi": OsmRegion(
        slug="burundi", source="osm_burundi", region="africa/burundi",
        continent="africa", label="ブルンジ", label_en="Burundi",
        lang="rn", pbf_bytes=44100000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=882,
    ),
    "cameroon": OsmRegion(
        slug="cameroon", source="osm_cameroon", region="africa/cameroon",
        continent="africa", label="カメルーン", label_en="Cameroon",
        lang="fr", pbf_bytes=212000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4240,
    ),
    "canary-islands": OsmRegion(
        slug="canary-islands", source="osm_canary_islands", region="africa/canary-islands",
        continent="africa", label="カナリア諸島", label_en="Canary Islands",
        lang="es", pbf_bytes=56000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1120,
    ),
    "cape-verde": OsmRegion(
        slug="cape-verde", source="osm_cape_verde", region="africa/cape-verde",
        continent="africa", label="カーボベルデ", label_en="Cape Verde",
        lang="pt", pbf_bytes=11100000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=222,
    ),
    "central-african-republic": OsmRegion(
        slug="central-african-republic", source="osm_central_african_republic", region="africa/central-african-republic",
        continent="africa", label="中央アフリカ共和国", label_en="Central African Republic",
        lang="sg", pbf_bytes=94000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1880,
    ),
    "chad": OsmRegion(
        slug="chad", source="osm_chad", region="africa/chad",
        continent="africa", label="チャド", label_en="Chad",
        lang="ar", pbf_bytes=128000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2560,
    ),
    "comores": OsmRegion(
        slug="comores", source="osm_comores", region="africa/comores",
        continent="africa", label="コモロ", label_en="Comores",
        lang="ar", pbf_bytes=3800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=76,
    ),
    "congo-brazzaville": OsmRegion(
        slug="congo-brazzaville", source="osm_congo_brazzaville", region="africa/congo-brazzaville",
        continent="africa", label="コンゴ共和国", label_en="Congo (Republic/Brazzaville)",
        lang="fr", pbf_bytes=30800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=616,
    ),
    "congo-democratic-republic": OsmRegion(
        slug="congo-democratic-republic", source="osm_congo_democratic_republic", region="africa/congo-democratic-republic",
        continent="africa", label="コンゴ民主共和国", label_en="Congo (Democratic Republic/Kinshasa)",
        lang="fr", pbf_bytes=394000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7880,
    ),
    "djibouti": OsmRegion(
        slug="djibouti", source="osm_djibouti", region="africa/djibouti",
        continent="africa", label="ジブチ", label_en="Djibouti",
        lang="fr", pbf_bytes=6700000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=134,
    ),
    "egypt": OsmRegion(
        slug="egypt", source="osm_egypt", region="africa/egypt",
        continent="africa", label="エジプト", label_en="Egypt",
        lang="ar", pbf_bytes=169000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3380,
    ),
    "equatorial-guinea": OsmRegion(
        slug="equatorial-guinea", source="osm_equatorial_guinea", region="africa/equatorial-guinea",
        continent="africa", label="赤道ギニア", label_en="Equatorial Guinea",
        lang="es", pbf_bytes=6200000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=124,
    ),
    "eritrea": OsmRegion(
        slug="eritrea", source="osm_eritrea", region="africa/eritrea",
        continent="africa", label="エリトリア", label_en="Eritrea",
        lang="ti", pbf_bytes=29600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=592,
    ),
    "ethiopia": OsmRegion(
        slug="ethiopia", source="osm_ethiopia", region="africa/ethiopia",
        continent="africa", label="エチオピア", label_en="Ethiopia",
        lang="am", pbf_bytes=132000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2640,
    ),
    "gabon": OsmRegion(
        slug="gabon", source="osm_gabon", region="africa/gabon",
        continent="africa", label="ガボン", label_en="Gabon",
        lang="fr", pbf_bytes=24200000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=484,
    ),
    "ghana": OsmRegion(
        slug="ghana", source="osm_ghana", region="africa/ghana",
        continent="africa", label="ガーナ", label_en="Ghana",
        lang="en", pbf_bytes=107000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2140,
    ),
    "guinea": OsmRegion(
        slug="guinea", source="osm_guinea", region="africa/guinea",
        continent="africa", label="ギニア", label_en="Guinea",
        lang="fr", pbf_bytes=111000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2220,
    ),
    "guinea-bissau": OsmRegion(
        slug="guinea-bissau", source="osm_guinea_bissau", region="africa/guinea-bissau",
        continent="africa", label="ギニアビサウ", label_en="Guinea-Bissau",
        lang="pt", pbf_bytes=10600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=212,
    ),
    "ivory-coast": OsmRegion(
        slug="ivory-coast", source="osm_ivory_coast", region="africa/ivory-coast",
        continent="africa", label="コートジボワール", label_en="Ivory Coast",
        lang="fr", pbf_bytes=85000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1700,
    ),
    "kenya": OsmRegion(
        slug="kenya", source="osm_kenya", region="africa/kenya",
        continent="africa", label="ケニア", label_en="Kenya",
        lang="sw", pbf_bytes=332000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6640,
    ),
    "lesotho": OsmRegion(
        slug="lesotho", source="osm_lesotho", region="africa/lesotho",
        continent="africa", label="レソト", label_en="Lesotho",
        lang="st", pbf_bytes=120000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2400,
    ),
    "liberia": OsmRegion(
        slug="liberia", source="osm_liberia", region="africa/liberia",
        continent="africa", label="リベリア", label_en="Liberia",
        lang="en", pbf_bytes=35600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=712,
    ),
    "libya": OsmRegion(
        slug="libya", source="osm_libya", region="africa/libya",
        continent="africa", label="リビア", label_en="Libya",
        lang="ar", pbf_bytes=72000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1440,
    ),
    "madagascar": OsmRegion(
        slug="madagascar", source="osm_madagascar", region="africa/madagascar",
        continent="africa", label="マダガスカル", label_en="Madagascar",
        lang="mg", pbf_bytes=368000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7360,
    ),
    "malawi": OsmRegion(
        slug="malawi", source="osm_malawi", region="africa/malawi",
        continent="africa", label="マラウイ", label_en="Malawi",
        lang="en", pbf_bytes=147000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2940,
    ),
    "mali": OsmRegion(
        slug="mali", source="osm_mali", region="africa/mali",
        continent="africa", label="マリ", label_en="Mali",
        lang="fr", pbf_bytes=164000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3280,
    ),
    "mauritania": OsmRegion(
        slug="mauritania", source="osm_mauritania", region="africa/mauritania",
        continent="africa", label="モーリタニア", label_en="Mauritania",
        lang="ar", pbf_bytes=29000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=580,
    ),
    "mauritius": OsmRegion(
        slug="mauritius", source="osm_mauritius", region="africa/mauritius",
        continent="africa", label="モーリシャス", label_en="Mauritius",
        lang="fr", pbf_bytes=9100000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=182,
    ),
    "morocco": OsmRegion(
        slug="morocco", source="osm_morocco", region="africa/morocco",
        continent="africa", label="モロッコ", label_en="Morocco",
        lang="ar", pbf_bytes=231000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4620,
    ),
    "mozambique": OsmRegion(
        slug="mozambique", source="osm_mozambique", region="africa/mozambique",
        continent="africa", label="モザンビーク", label_en="Mozambique",
        lang="pt", pbf_bytes=242000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4840,
    ),
    "namibia": OsmRegion(
        slug="namibia", source="osm_namibia", region="africa/namibia",
        continent="africa", label="ナミビア", label_en="Namibia",
        lang="en", pbf_bytes=51000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1020,
    ),
    "niger": OsmRegion(
        slug="niger", source="osm_niger", region="africa/niger",
        continent="africa", label="ニジェール", label_en="Niger",
        lang="fr", pbf_bytes=72000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1440,
    ),
    "nigeria": OsmRegion(
        slug="nigeria", source="osm_nigeria", region="africa/nigeria",
        continent="africa", label="ナイジェリア", label_en="Nigeria",
        lang="en", pbf_bytes=678000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=13560,
    ),
    "rwanda": OsmRegion(
        slug="rwanda", source="osm_rwanda", region="africa/rwanda",
        continent="africa", label="ルワンダ", label_en="Rwanda",
        lang="rw", pbf_bytes=62000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1240,
    ),
    "saint-helena-ascension-and-tristan-da-cunha": OsmRegion(
        slug="saint-helena-ascension-and-tristan-da-cunha", source="osm_saint_helena_ascension_and_tristan_da_cunha", region="africa/saint-helena-ascension-and-tristan-da-cunha",
        continent="africa", label="セントヘレナ・アセンション・トリスタンダクーニャ", label_en="Saint Helena, Ascension, and Tristan da Cunha",
        lang="en", pbf_bytes=873000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "sao-tome-and-principe": OsmRegion(
        slug="sao-tome-and-principe", source="osm_sao_tome_and_principe", region="africa/sao-tome-and-principe",
        continent="africa", label="サントメ・プリンシペ", label_en="Sao Tome and Principe",
        lang="pt", pbf_bytes=1200000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "senegal-and-gambia": OsmRegion(
        slug="senegal-and-gambia", source="osm_senegal_and_gambia", region="africa/senegal-and-gambia",
        continent="africa", label="セネガル・ガンビア", label_en="Senegal and Gambia",
        lang="fr", pbf_bytes=100000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2000,
    ),
    "seychelles": OsmRegion(
        slug="seychelles", source="osm_seychelles", region="africa/seychelles",
        continent="africa", label="セーシェル", label_en="Seychelles",
        lang="fr", pbf_bytes=2600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=52,
    ),
    "sierra-leone": OsmRegion(
        slug="sierra-leone", source="osm_sierra_leone", region="africa/sierra-leone",
        continent="africa", label="シエラレオネ", label_en="Sierra Leone",
        lang="en", pbf_bytes=41000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=820,
    ),
    "somalia": OsmRegion(
        slug="somalia", source="osm_somalia", region="africa/somalia",
        continent="africa", label="ソマリア", label_en="Somalia",
        lang="so", pbf_bytes=156000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3120,
    ),
    "south-africa": OsmRegion(
        slug="south-africa", source="osm_south_africa", region="africa/south-africa",
        continent="africa", label="南アフリカ", label_en="South Africa",
        lang="en", pbf_bytes=397000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7940,
    ),
    "south-sudan": OsmRegion(
        slug="south-sudan", source="osm_south_sudan", region="africa/south-sudan",
        continent="africa", label="南スーダン", label_en="South Sudan",
        lang="en", pbf_bytes=131000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2620,
    ),
    "sudan": OsmRegion(
        slug="sudan", source="osm_sudan", region="africa/sudan",
        continent="africa", label="スーダン", label_en="Sudan",
        lang="ar", pbf_bytes=193000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3860,
    ),
    "swaziland": OsmRegion(
        slug="swaziland", source="osm_swaziland", region="africa/swaziland",
        continent="africa", label="エスワティニ", label_en="Swaziland",
        lang="en", pbf_bytes=29200000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=584,
    ),
    "tanzania": OsmRegion(
        slug="tanzania", source="osm_tanzania", region="africa/tanzania",
        continent="africa", label="タンザニア", label_en="Tanzania",
        lang="sw", pbf_bytes=672000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=13440,
    ),
    "togo": OsmRegion(
        slug="togo", source="osm_togo", region="africa/togo",
        continent="africa", label="トーゴ", label_en="Togo",
        lang="fr", pbf_bytes=59000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1180,
    ),
    "tunisia": OsmRegion(
        slug="tunisia", source="osm_tunisia", region="africa/tunisia",
        continent="africa", label="チュニジア", label_en="Tunisia",
        lang="ar", pbf_bytes=79000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1580,
    ),
    "uganda": OsmRegion(
        slug="uganda", source="osm_uganda", region="africa/uganda",
        continent="africa", label="ウガンダ", label_en="Uganda",
        lang="sw", pbf_bytes=353000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7060,
    ),
    "zambia": OsmRegion(
        slug="zambia", source="osm_zambia", region="africa/zambia",
        continent="africa", label="ザンビア", label_en="Zambia",
        lang="en", pbf_bytes=239000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4780,
    ),
    "zimbabwe": OsmRegion(
        slug="zimbabwe", source="osm_zimbabwe", region="africa/zimbabwe",
        continent="africa", label="ジンバブエ", label_en="Zimbabwe",
        lang="sn", pbf_bytes=170000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3400,
    ),
    "afghanistan": OsmRegion(
        slug="afghanistan", source="osm_afghanistan", region="asia/afghanistan",
        continent="asia", label="アフガニスタン", label_en="Afghanistan",
        lang="fa", pbf_bytes=107000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2140,
    ),
    "armenia": OsmRegion(
        slug="armenia", source="osm_armenia", region="asia/armenia",
        continent="asia", label="アルメニア", label_en="Armenia",
        lang="hy", pbf_bytes=50000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1000,
    ),
    "azerbaijan": OsmRegion(
        slug="azerbaijan", source="osm_azerbaijan", region="asia/azerbaijan",
        continent="asia", label="アゼルバイジャン", label_en="Azerbaijan",
        lang="az", pbf_bytes=43400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=868,
    ),
    "bangladesh": OsmRegion(
        slug="bangladesh", source="osm_bangladesh", region="asia/bangladesh",
        continent="asia", label="バングラデシュ", label_en="Bangladesh",
        lang="bn", pbf_bytes=333000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6660,
    ),
    "bhutan": OsmRegion(
        slug="bhutan", source="osm_bhutan", region="asia/bhutan",
        continent="asia", label="ブータン", label_en="Bhutan",
        lang="dz", pbf_bytes=22500000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=450,
    ),
    "cambodia": OsmRegion(
        slug="cambodia", source="osm_cambodia", region="asia/cambodia",
        continent="asia", label="カンボジア", label_en="Cambodia",
        lang="km", pbf_bytes=38400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=768,
    ),
    "china": OsmRegion(
        slug="china", source="osm_china", region="asia/china",
        continent="asia", label="中国", label_en="China",
        lang="zh", pbf_bytes=1500000000, memory_gb=8.0,
        node_index="sparse_mmap_array", min_docs=30000,
    ),
    "east-timor": OsmRegion(
        slug="east-timor", source="osm_east_timor", region="asia/east-timor",
        continent="asia", label="東ティモール", label_en="East Timor",
        lang="pt", pbf_bytes=16900000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=338,
    ),
    "gcc-states": OsmRegion(
        slug="gcc-states", source="osm_gcc_states", region="asia/gcc-states",
        continent="asia", label="湾岸協力会議諸国(サウジ・UAE・カタール等)", label_en="GCC States",
        lang="ar", pbf_bytes=240000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4800,
    ),
    "india": OsmRegion(
        slug="india", source="osm_india", region="asia/india",
        continent="asia", label="インド", label_en="India",
        lang="hi", pbf_bytes=1600000000, memory_gb=8.0,
        node_index="sparse_mmap_array", min_docs=32000,
    ),
    "indonesia": OsmRegion(
        slug="indonesia", source="osm_indonesia", region="asia/indonesia",
        continent="asia", label="インドネシア(東ティモールを含む)", label_en="Indonesia (with East Timor)",
        lang="id", pbf_bytes=1600000000, memory_gb=8.0,
        node_index="sparse_mmap_array", min_docs=32000,
    ),
    "iran": OsmRegion(
        slug="iran", source="osm_iran", region="asia/iran",
        continent="asia", label="イラン", label_en="Iran",
        lang="fa", pbf_bytes=217000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4340,
    ),
    "iraq": OsmRegion(
        slug="iraq", source="osm_iraq", region="asia/iraq",
        continent="asia", label="イラク", label_en="Iraq",
        lang="ar", pbf_bytes=85000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1700,
    ),
    "israel-and-palestine": OsmRegion(
        slug="israel-and-palestine", source="osm_israel_and_palestine", region="asia/israel-and-palestine",
        continent="asia", label="イスラエル・パレスチナ", label_en="Israel and Palestine",
        lang="he", pbf_bytes=114000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2280,
    ),
    "japan": OsmRegion(
        slug="japan", source="osm_japan", region="asia/japan",
        continent="asia", label="日本", label_en="Japan",
        lang="ja", pbf_bytes=2300000000, memory_gb=12.0,
        node_index="sparse_mmap_array", min_docs=46000,
    ),
    "jordan": OsmRegion(
        slug="jordan", source="osm_jordan", region="asia/jordan",
        continent="asia", label="ヨルダン", label_en="Jordan",
        lang="ar", pbf_bytes=29600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=592,
    ),
    "kazakhstan": OsmRegion(
        slug="kazakhstan", source="osm_kazakhstan", region="asia/kazakhstan",
        continent="asia", label="カザフスタン", label_en="Kazakhstan",
        lang="ru", pbf_bytes=209000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4180,
    ),
    "kyrgyzstan": OsmRegion(
        slug="kyrgyzstan", source="osm_kyrgyzstan", region="asia/kyrgyzstan",
        continent="asia", label="キルギス", label_en="Kyrgyzstan",
        lang="ky", pbf_bytes=50000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1000,
    ),
    "laos": OsmRegion(
        slug="laos", source="osm_laos", region="asia/laos",
        continent="asia", label="ラオス", label_en="Laos",
        lang="lo", pbf_bytes=50000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1000,
    ),
    "lebanon": OsmRegion(
        slug="lebanon", source="osm_lebanon", region="asia/lebanon",
        continent="asia", label="レバノン", label_en="Lebanon",
        lang="ar", pbf_bytes=49800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=996,
    ),
    "malaysia-singapore-brunei": OsmRegion(
        slug="malaysia-singapore-brunei", source="osm_malaysia_singapore_brunei", region="asia/malaysia-singapore-brunei",
        continent="asia", label="マレーシア・シンガポール・ブルネイ", label_en="Malaysia, Singapore, and Brunei",
        lang="ms", pbf_bytes=237000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4740,
    ),
    "maldives": OsmRegion(
        slug="maldives", source="osm_maldives", region="asia/maldives",
        continent="asia", label="モルディブ", label_en="Maldives",
        lang="dv", pbf_bytes=3400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=68,
    ),
    "mongolia": OsmRegion(
        slug="mongolia", source="osm_mongolia", region="asia/mongolia",
        continent="asia", label="モンゴル", label_en="Mongolia",
        lang="mn", pbf_bytes=58000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1160,
    ),
    "myanmar": OsmRegion(
        slug="myanmar", source="osm_myanmar", region="asia/myanmar",
        continent="asia", label="ミャンマー", label_en="Myanmar (a.k.a. Burma)",
        lang="my", pbf_bytes=262000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=5240,
    ),
    "nepal": OsmRegion(
        slug="nepal", source="osm_nepal", region="asia/nepal",
        continent="asia", label="ネパール", label_en="Nepal",
        lang="ne", pbf_bytes=392000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7840,
    ),
    "north-korea": OsmRegion(
        slug="north-korea", source="osm_north_korea", region="asia/north-korea",
        continent="asia", label="北朝鮮", label_en="North Korea",
        lang="ko", pbf_bytes=86000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1720,
    ),
    "pakistan": OsmRegion(
        slug="pakistan", source="osm_pakistan", region="asia/pakistan",
        continent="asia", label="パキスタン", label_en="Pakistan",
        lang="ur", pbf_bytes=147000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2940,
    ),
    "philippines": OsmRegion(
        slug="philippines", source="osm_philippines", region="asia/philippines",
        continent="asia", label="フィリピン", label_en="Philippines",
        lang="en", pbf_bytes=573000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=11460,
    ),
    "south-korea": OsmRegion(
        slug="south-korea", source="osm_south_korea", region="asia/south-korea",
        continent="asia", label="韓国", label_en="South Korea",
        lang="ko", pbf_bytes=270000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=5400,
    ),
    "sri-lanka": OsmRegion(
        slug="sri-lanka", source="osm_sri_lanka", region="asia/sri-lanka",
        continent="asia", label="スリランカ", label_en="Sri Lanka",
        lang="si", pbf_bytes=136000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2720,
    ),
    "syria": OsmRegion(
        slug="syria", source="osm_syria", region="asia/syria",
        continent="asia", label="シリア", label_en="Syria",
        lang="ar", pbf_bytes=77000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1540,
    ),
    "taiwan": OsmRegion(
        slug="taiwan", source="osm_taiwan", region="asia/taiwan",
        continent="asia", label="台湾", label_en="Taiwan",
        lang="zh", pbf_bytes=310000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6200,
    ),
    "tajikistan": OsmRegion(
        slug="tajikistan", source="osm_tajikistan", region="asia/tajikistan",
        continent="asia", label="タジキスタン", label_en="Tajikistan",
        lang="tg", pbf_bytes=45700000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=914,
    ),
    "thailand": OsmRegion(
        slug="thailand", source="osm_thailand", region="asia/thailand",
        continent="asia", label="タイ", label_en="Thailand",
        lang="th", pbf_bytes=310000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6200,
    ),
    "turkmenistan": OsmRegion(
        slug="turkmenistan", source="osm_turkmenistan", region="asia/turkmenistan",
        continent="asia", label="トルクメニスタン", label_en="Turkmenistan",
        lang="tk", pbf_bytes=23500000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=470,
    ),
    "uzbekistan": OsmRegion(
        slug="uzbekistan", source="osm_uzbekistan", region="asia/uzbekistan",
        continent="asia", label="ウズベキスタン", label_en="Uzbekistan",
        lang="uz", pbf_bytes=115000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2300,
    ),
    "vietnam": OsmRegion(
        slug="vietnam", source="osm_vietnam", region="asia/vietnam",
        continent="asia", label="ベトナム", label_en="Vietnam",
        lang="vi", pbf_bytes=310000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6200,
    ),
    "yemen": OsmRegion(
        slug="yemen", source="osm_yemen", region="asia/yemen",
        continent="asia", label="イエメン", label_en="Yemen",
        lang="ar", pbf_bytes=41000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=820,
    ),
    "american-oceania": OsmRegion(
        slug="american-oceania", source="osm_american_oceania", region="australia-oceania/american-oceania",
        continent="australia-oceania", label="アメリカ領オセアニア", label_en="American Oceania",
        lang="en", pbf_bytes=5100000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=102,
    ),
    "australia": OsmRegion(
        slug="australia", source="osm_australia", region="australia-oceania/australia",
        continent="australia-oceania", label="オーストラリア", label_en="Australia",
        lang="en", pbf_bytes=909000000, memory_gb=5.0,
        node_index="sparse_mmap_array", min_docs=18180,
    ),
    "cook-islands": OsmRegion(
        slug="cook-islands", source="osm_cook_islands", region="australia-oceania/cook-islands",
        continent="australia-oceania", label="クック諸島", label_en="Cook Islands",
        lang="en", pbf_bytes=937000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "fiji": OsmRegion(
        slug="fiji", source="osm_fiji", region="australia-oceania/fiji",
        continent="australia-oceania", label="フィジー", label_en="Fiji",
        lang="en", pbf_bytes=16399999, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=328,
    ),
    "ile-de-clipperton": OsmRegion(
        slug="ile-de-clipperton", source="osm_ile_de_clipperton", region="australia-oceania/ile-de-clipperton",
        continent="australia-oceania", label="クリッパートン島", label_en="Île de Clipperton",
        lang="fr", pbf_bytes=41500, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "kiribati": OsmRegion(
        slug="kiribati", source="osm_kiribati", region="australia-oceania/kiribati",
        continent="australia-oceania", label="キリバス", label_en="Kiribati",
        lang="en", pbf_bytes=2300000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "marshall-islands": OsmRegion(
        slug="marshall-islands", source="osm_marshall_islands", region="australia-oceania/marshall-islands",
        continent="australia-oceania", label="マーシャル諸島", label_en="Marshall Islands",
        lang="en", pbf_bytes=1900000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "micronesia": OsmRegion(
        slug="micronesia", source="osm_micronesia", region="australia-oceania/micronesia",
        continent="australia-oceania", label="ミクロネシア連邦", label_en="Micronesia",
        lang="en", pbf_bytes=1900000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "nauru": OsmRegion(
        slug="nauru", source="osm_nauru", region="australia-oceania/nauru",
        continent="australia-oceania", label="ナウル", label_en="Nauru",
        lang="en", pbf_bytes=258000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "new-caledonia": OsmRegion(
        slug="new-caledonia", source="osm_new_caledonia", region="australia-oceania/new-caledonia",
        continent="australia-oceania", label="ニューカレドニア", label_en="New Caledonia",
        lang="fr", pbf_bytes=13500000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=270,
    ),
    "new-zealand": OsmRegion(
        slug="new-zealand", source="osm_new_zealand", region="australia-oceania/new-zealand",
        continent="australia-oceania", label="ニュージーランド", label_en="New Zealand",
        lang="en", pbf_bytes=381000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7620,
    ),
    "niue": OsmRegion(
        slug="niue", source="osm_niue", region="australia-oceania/niue",
        continent="australia-oceania", label="ニウエ", label_en="Niue",
        lang="en", pbf_bytes=414000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "palau": OsmRegion(
        slug="palau", source="osm_palau", region="australia-oceania/palau",
        continent="australia-oceania", label="パラオ", label_en="Palau",
        lang="pau", pbf_bytes=800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "papua-new-guinea": OsmRegion(
        slug="papua-new-guinea", source="osm_papua_new_guinea", region="australia-oceania/papua-new-guinea",
        continent="australia-oceania", label="パプアニューギニア", label_en="Papua New Guinea",
        lang="tpi", pbf_bytes=51000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1020,
    ),
    "pitcairn-islands": OsmRegion(
        slug="pitcairn-islands", source="osm_pitcairn_islands", region="australia-oceania/pitcairn-islands",
        continent="australia-oceania", label="ピトケアン諸島", label_en="Pitcairn Islands",
        lang="en", pbf_bytes=111000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "polynesie-francaise": OsmRegion(
        slug="polynesie-francaise", source="osm_polynesie_francaise", region="australia-oceania/polynesie-francaise",
        continent="australia-oceania", label="フランス領ポリネシア", label_en="Polynésie française (French Polynesia)",
        lang="fr", pbf_bytes=14900000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=298,
    ),
    "samoa": OsmRegion(
        slug="samoa", source="osm_samoa", region="australia-oceania/samoa",
        continent="australia-oceania", label="サモア", label_en="Samoa",
        lang="sm", pbf_bytes=3300000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=66,
    ),
    "solomon-islands": OsmRegion(
        slug="solomon-islands", source="osm_solomon_islands", region="australia-oceania/solomon-islands",
        continent="australia-oceania", label="ソロモン諸島", label_en="Solomon Islands",
        lang="en", pbf_bytes=11400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=228,
    ),
    "tokelau": OsmRegion(
        slug="tokelau", source="osm_tokelau", region="australia-oceania/tokelau",
        continent="australia-oceania", label="トケラウ", label_en="Tokelau",
        lang="en", pbf_bytes=141000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "tonga": OsmRegion(
        slug="tonga", source="osm_tonga", region="australia-oceania/tonga",
        continent="australia-oceania", label="トンガ", label_en="Tonga",
        lang="to", pbf_bytes=3500000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=70,
    ),
    "tuvalu": OsmRegion(
        slug="tuvalu", source="osm_tuvalu", region="australia-oceania/tuvalu",
        continent="australia-oceania", label="ツバル", label_en="Tuvalu",
        lang="tvl", pbf_bytes=348000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "vanuatu": OsmRegion(
        slug="vanuatu", source="osm_vanuatu", region="australia-oceania/vanuatu",
        continent="australia-oceania", label="バヌアツ", label_en="Vanuatu",
        lang="bi", pbf_bytes=7500000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=150,
    ),
    "wallis-et-futuna": OsmRegion(
        slug="wallis-et-futuna", source="osm_wallis_et_futuna", region="australia-oceania/wallis-et-futuna",
        continent="australia-oceania", label="ウォリス・フツナ", label_en="Wallis et Futuna",
        lang="fr", pbf_bytes=602000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "bahamas": OsmRegion(
        slug="bahamas", source="osm_bahamas", region="central-america/bahamas",
        continent="central-america", label="バハマ", label_en="Bahamas",
        lang="en", pbf_bytes=13500000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=270,
    ),
    "belize": OsmRegion(
        slug="belize", source="osm_belize", region="central-america/belize",
        continent="central-america", label="ベリーズ", label_en="Belize",
        lang="en", pbf_bytes=17400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=348,
    ),
    "costa-rica": OsmRegion(
        slug="costa-rica", source="osm_costa_rica", region="central-america/costa-rica",
        continent="central-america", label="コスタリカ", label_en="Costa Rica",
        lang="es", pbf_bytes=37000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=740,
    ),
    "cuba": OsmRegion(
        slug="cuba", source="osm_cuba", region="central-america/cuba",
        continent="central-america", label="キューバ", label_en="Cuba",
        lang="es", pbf_bytes=58000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1160,
    ),
    "el-salvador": OsmRegion(
        slug="el-salvador", source="osm_el_salvador", region="central-america/el-salvador",
        continent="central-america", label="エルサルバドル", label_en="El Salvador",
        lang="es", pbf_bytes=33200000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=664,
    ),
    "guatemala": OsmRegion(
        slug="guatemala", source="osm_guatemala", region="central-america/guatemala",
        continent="central-america", label="グアテマラ", label_en="Guatemala",
        lang="es", pbf_bytes=124000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2480,
    ),
    "haiti-and-domrep": OsmRegion(
        slug="haiti-and-domrep", source="osm_haiti_and_domrep", region="central-america/haiti-and-domrep",
        continent="central-america", label="ハイチ・ドミニカ共和国", label_en="Haiti and Dominican Republic",
        lang="es", pbf_bytes=84000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1680,
    ),
    "honduras": OsmRegion(
        slug="honduras", source="osm_honduras", region="central-america/honduras",
        continent="central-america", label="ホンジュラス", label_en="Honduras",
        lang="es", pbf_bytes=69000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1380,
    ),
    "jamaica": OsmRegion(
        slug="jamaica", source="osm_jamaica", region="central-america/jamaica",
        continent="central-america", label="ジャマイカ", label_en="Jamaica",
        lang="en", pbf_bytes=36800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=736,
    ),
    "nicaragua": OsmRegion(
        slug="nicaragua", source="osm_nicaragua", region="central-america/nicaragua",
        continent="central-america", label="ニカラグア", label_en="Nicaragua",
        lang="es", pbf_bytes=57000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1140,
    ),
    "panama": OsmRegion(
        slug="panama", source="osm_panama", region="central-america/panama",
        continent="central-america", label="パナマ", label_en="Panama",
        lang="es", pbf_bytes=32000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=640,
    ),
    "albania": OsmRegion(
        slug="albania", source="osm_albania", region="europe/albania",
        continent="europe", label="アルバニア", label_en="Albania",
        lang="sq", pbf_bytes=51000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1020,
    ),
    "andorra": OsmRegion(
        slug="andorra", source="osm_andorra", region="europe/andorra",
        continent="europe", label="アンドラ", label_en="Andorra",
        lang="ca", pbf_bytes=3300000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=66,
    ),
    "austria": OsmRegion(
        slug="austria", source="osm_austria", region="europe/austria",
        continent="europe", label="オーストリア", label_en="Austria",
        lang="de", pbf_bytes=767000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=15340,
    ),
    "azores": OsmRegion(
        slug="azores", source="osm_azores", region="europe/azores",
        continent="europe", label="アゾレス諸島", label_en="Azores",
        lang="pt", pbf_bytes=16800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=336,
    ),
    "belarus": OsmRegion(
        slug="belarus", source="osm_belarus", region="europe/belarus",
        continent="europe", label="ベラルーシ", label_en="Belarus",
        lang="ru", pbf_bytes=330000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6600,
    ),
    "belgium": OsmRegion(
        slug="belgium", source="osm_belgium", region="europe/belgium",
        continent="europe", label="ベルギー", label_en="Belgium",
        lang="nl", pbf_bytes=658000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=13160,
    ),
    "bosnia-herzegovina": OsmRegion(
        slug="bosnia-herzegovina", source="osm_bosnia_herzegovina", region="europe/bosnia-herzegovina",
        continent="europe", label="ボスニア・ヘルツェゴビナ", label_en="Bosnia-Herzegovina",
        lang="bs", pbf_bytes=151000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3020,
    ),
    "bulgaria": OsmRegion(
        slug="bulgaria", source="osm_bulgaria", region="europe/bulgaria",
        continent="europe", label="ブルガリア", label_en="Bulgaria",
        lang="bg", pbf_bytes=162000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3240,
    ),
    "croatia": OsmRegion(
        slug="croatia", source="osm_croatia", region="europe/croatia",
        continent="europe", label="クロアチア", label_en="Croatia",
        lang="hr", pbf_bytes=188000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3760,
    ),
    "cyprus": OsmRegion(
        slug="cyprus", source="osm_cyprus", region="europe/cyprus",
        continent="europe", label="キプロス", label_en="Cyprus",
        lang="el", pbf_bytes=35100000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=702,
    ),
    "czech-republic": OsmRegion(
        slug="czech-republic", source="osm_czech_republic", region="europe/czech-republic",
        continent="europe", label="チェコ", label_en="Czech Republic",
        lang="cs", pbf_bytes=897000000, memory_gb=5.0,
        node_index="sparse_mmap_array", min_docs=17940,
    ),
    "denmark": OsmRegion(
        slug="denmark", source="osm_denmark", region="europe/denmark",
        continent="europe", label="デンマーク", label_en="Denmark",
        lang="da", pbf_bytes=468000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=9360,
    ),
    "estonia": OsmRegion(
        slug="estonia", source="osm_estonia", region="europe/estonia",
        continent="europe", label="エストニア", label_en="Estonia",
        lang="et", pbf_bytes=116000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2320,
    ),
    "faroe-islands": OsmRegion(
        slug="faroe-islands", source="osm_faroe_islands", region="europe/faroe-islands",
        continent="europe", label="フェロー諸島", label_en="Faroe Islands",
        lang="fo", pbf_bytes=7400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=148,
    ),
    "finland": OsmRegion(
        slug="finland", source="osm_finland", region="europe/finland",
        continent="europe", label="フィンランド", label_en="Finland",
        lang="fi", pbf_bytes=696000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=13920,
    ),
    "france": OsmRegion(
        slug="france", source="osm_france", region="europe/france",
        continent="europe", label="フランス", label_en="France",
        lang="fr", pbf_bytes=4700000000, memory_gb=24.0,
        node_index="sparse_file_array", min_docs=94000,
    ),
    "georgia": OsmRegion(
        slug="georgia", source="osm_georgia", region="europe/georgia",
        continent="europe", label="ジョージア", label_en="Georgia",
        lang="ka", pbf_bytes=95000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1900,
    ),
    "germany": OsmRegion(
        slug="germany", source="osm_germany", region="europe/germany",
        continent="europe", label="ドイツ", label_en="Germany",
        lang="de", pbf_bytes=4500000000, memory_gb=23.0,
        node_index="sparse_file_array", min_docs=90000,
    ),
    "great-britain": OsmRegion(
        slug="great-britain", source="osm_great_britain", region="europe/great-britain",
        continent="europe", label="イギリス(グレートブリテン島)", label_en="Great Britain",
        lang="en", pbf_bytes=2000000000, memory_gb=10.0,
        node_index="sparse_mmap_array", min_docs=40000,
    ),
    "greece": OsmRegion(
        slug="greece", source="osm_greece", region="europe/greece",
        continent="europe", label="ギリシャ", label_en="Greece",
        lang="el", pbf_bytes=322000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6440,
    ),
    "guernsey-jersey": OsmRegion(
        slug="guernsey-jersey", source="osm_guernsey_jersey", region="europe/guernsey-jersey",
        continent="europe", label="ガーンジー・ジャージー", label_en="Guernsey and Jersey",
        lang="en", pbf_bytes=3700000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=74,
    ),
    "hungary": OsmRegion(
        slug="hungary", source="osm_hungary", region="europe/hungary",
        continent="europe", label="ハンガリー", label_en="Hungary",
        lang="hu", pbf_bytes=305000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6100,
    ),
    "iceland": OsmRegion(
        slug="iceland", source="osm_iceland", region="europe/iceland",
        continent="europe", label="アイスランド", label_en="Iceland",
        lang="is", pbf_bytes=61000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1220,
    ),
    "ireland-and-northern-ireland": OsmRegion(
        slug="ireland-and-northern-ireland", source="osm_ireland_and_northern_ireland", region="europe/ireland-and-northern-ireland",
        continent="europe", label="アイルランド・北アイルランド", label_en="Ireland and Northern Ireland",
        lang="en", pbf_bytes=388000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7760,
    ),
    "isle-of-man": OsmRegion(
        slug="isle-of-man", source="osm_isle_of_man", region="europe/isle-of-man",
        continent="europe", label="マン島", label_en="Isle of Man",
        lang="en", pbf_bytes=5700000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=114,
    ),
    "italy": OsmRegion(
        slug="italy", source="osm_italy", region="europe/italy",
        continent="europe", label="イタリア", label_en="Italy",
        lang="it", pbf_bytes=2100000000, memory_gb=11.0,
        node_index="sparse_mmap_array", min_docs=42000,
    ),
    "kosovo": OsmRegion(
        slug="kosovo", source="osm_kosovo", region="europe/kosovo",
        continent="europe", label="コソボ", label_en="Kosovo",
        lang="sq", pbf_bytes=29200000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=584,
    ),
    "latvia": OsmRegion(
        slug="latvia", source="osm_latvia", region="europe/latvia",
        continent="europe", label="ラトビア", label_en="Latvia",
        lang="lv", pbf_bytes=132000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2640,
    ),
    "liechtenstein": OsmRegion(
        slug="liechtenstein", source="osm_liechtenstein", region="europe/liechtenstein",
        continent="europe", label="リヒテンシュタイン", label_en="Liechtenstein",
        lang="de", pbf_bytes=3300000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=66,
    ),
    "lithuania": OsmRegion(
        slug="lithuania", source="osm_lithuania", region="europe/lithuania",
        continent="europe", label="リトアニア", label_en="Lithuania",
        lang="lt", pbf_bytes=211000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4220,
    ),
    "luxembourg": OsmRegion(
        slug="luxembourg", source="osm_luxembourg", region="europe/luxembourg",
        continent="europe", label="ルクセンブルク", label_en="Luxembourg",
        lang="fr", pbf_bytes=45000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=900,
    ),
    "macedonia": OsmRegion(
        slug="macedonia", source="osm_macedonia", region="europe/macedonia",
        continent="europe", label="北マケドニア", label_en="Macedonia",
        lang="mk", pbf_bytes=28100000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=562,
    ),
    "malta": OsmRegion(
        slug="malta", source="osm_malta", region="europe/malta",
        continent="europe", label="マルタ", label_en="Malta",
        lang="mt", pbf_bytes=8400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=168,
    ),
    "moldova": OsmRegion(
        slug="moldova", source="osm_moldova", region="europe/moldova",
        continent="europe", label="モルドバ", label_en="Moldova",
        lang="ro", pbf_bytes=95000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1900,
    ),
    "monaco": OsmRegion(
        slug="monaco", source="osm_monaco", region="europe/monaco",
        continent="europe", label="モナコ", label_en="Monaco",
        lang="fr", pbf_bytes=670000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=50,
    ),
    "montenegro": OsmRegion(
        slug="montenegro", source="osm_montenegro", region="europe/montenegro",
        continent="europe", label="モンテネグロ", label_en="Montenegro",
        lang="sr", pbf_bytes=32400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=648,
    ),
    "netherlands": OsmRegion(
        slug="netherlands", source="osm_netherlands", region="europe/netherlands",
        continent="europe", label="オランダ", label_en="Netherlands",
        lang="nl", pbf_bytes=1300000000, memory_gb=7.0,
        node_index="sparse_mmap_array", min_docs=26000,
    ),
    "norway": OsmRegion(
        slug="norway", source="osm_norway", region="europe/norway",
        continent="europe", label="ノルウェー", label_en="Norway",
        lang="nb", pbf_bytes=1300000000, memory_gb=7.0,
        node_index="sparse_mmap_array", min_docs=26000,
    ),
    "poland": OsmRegion(
        slug="poland", source="osm_poland", region="europe/poland",
        continent="europe", label="ポーランド", label_en="Poland",
        lang="pl", pbf_bytes=1900000000, memory_gb=10.0,
        node_index="sparse_mmap_array", min_docs=38000,
    ),
    "portugal": OsmRegion(
        slug="portugal", source="osm_portugal", region="europe/portugal",
        continent="europe", label="ポルトガル", label_en="Portugal",
        lang="pt", pbf_bytes=399000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=7980,
    ),
    "romania": OsmRegion(
        slug="romania", source="osm_romania", region="europe/romania",
        continent="europe", label="ルーマニア", label_en="Romania",
        lang="ro", pbf_bytes=309000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6180,
    ),
    "serbia": OsmRegion(
        slug="serbia", source="osm_serbia", region="europe/serbia",
        continent="europe", label="セルビア", label_en="Serbia",
        lang="sr", pbf_bytes=226000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4520,
    ),
    "slovakia": OsmRegion(
        slug="slovakia", source="osm_slovakia", region="europe/slovakia",
        continent="europe", label="スロバキア", label_en="Slovakia",
        lang="sk", pbf_bytes=325000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6500,
    ),
    "slovenia": OsmRegion(
        slug="slovenia", source="osm_slovenia", region="europe/slovenia",
        continent="europe", label="スロベニア", label_en="Slovenia",
        lang="sl", pbf_bytes=295000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=5900,
    ),
    "spain": OsmRegion(
        slug="spain", source="osm_spain", region="europe/spain",
        continent="europe", label="スペイン", label_en="Spain",
        lang="es", pbf_bytes=1400000000, memory_gb=7.0,
        node_index="sparse_mmap_array", min_docs=28000,
    ),
    "sweden": OsmRegion(
        slug="sweden", source="osm_sweden", region="europe/sweden",
        continent="europe", label="スウェーデン", label_en="Sweden",
        lang="sv", pbf_bytes=773000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=15460,
    ),
    "switzerland": OsmRegion(
        slug="switzerland", source="osm_switzerland", region="europe/switzerland",
        continent="europe", label="スイス", label_en="Switzerland",
        lang="de", pbf_bytes=515000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=10300,
    ),
    "turkey": OsmRegion(
        slug="turkey", source="osm_turkey", region="europe/turkey",
        continent="europe", label="トルコ", label_en="Turkey",
        lang="tr", pbf_bytes=609000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=12180,
    ),
    "ukraine": OsmRegion(
        slug="ukraine", source="osm_ukraine", region="europe/ukraine",
        continent="europe", label="ウクライナ(クリミアを含む)", label_en="Ukraine (with Crimea)",
        lang="uk", pbf_bytes=829000000, memory_gb=5.0,
        node_index="sparse_mmap_array", min_docs=16580,
    ),
    "united-kingdom": OsmRegion(
        slug="united-kingdom", source="osm_united_kingdom", region="europe/united-kingdom",
        continent="europe", label="イギリス", label_en="United Kingdom",
        lang="en", pbf_bytes=2100000000, memory_gb=11.0,
        node_index="sparse_mmap_array", min_docs=42000,
    ),
    "canada": OsmRegion(
        slug="canada", source="osm_canada", region="north-america/canada",
        continent="north-america", label="カナダ", label_en="Canada",
        lang="en", pbf_bytes=6000000000, memory_gb=30.0,
        node_index="sparse_file_array", min_docs=120000,
    ),
    "greenland": OsmRegion(
        slug="greenland", source="osm_greenland", region="north-america/greenland",
        continent="north-america", label="グリーンランド", label_en="Greenland",
        lang="kl", pbf_bytes=24400000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=488,
    ),
    "mexico": OsmRegion(
        slug="mexico", source="osm_mexico", region="north-america/mexico",
        continent="north-america", label="メキシコ", label_en="Mexico",
        lang="es", pbf_bytes=607000000, memory_gb=4.0,
        node_index="sparse_mmap_array", min_docs=12140,
    ),
    "us": OsmRegion(
        slug="us", source="osm_us", region="north-america/us",
        continent="north-america", label="アメリカ合衆国", label_en="United States of America",
        lang="en", pbf_bytes=11200000000, memory_gb=56.0,
        node_index="sparse_file_array", min_docs=224000,
    ),
    "argentina": OsmRegion(
        slug="argentina", source="osm_argentina", region="south-america/argentina",
        continent="south-america", label="アルゼンチン", label_en="Argentina",
        lang="es", pbf_bytes=405000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=8100,
    ),
    "bolivia": OsmRegion(
        slug="bolivia", source="osm_bolivia", region="south-america/bolivia",
        continent="south-america", label="ボリビア", label_en="Bolivia",
        lang="es", pbf_bytes=164000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=3280,
    ),
    "brazil": OsmRegion(
        slug="brazil", source="osm_brazil", region="south-america/brazil",
        continent="south-america", label="ブラジル", label_en="Brazil",
        lang="pt", pbf_bytes=1900000000, memory_gb=10.0,
        node_index="sparse_mmap_array", min_docs=38000,
    ),
    "chile": OsmRegion(
        slug="chile", source="osm_chile", region="south-america/chile",
        continent="south-america", label="チリ", label_en="Chile",
        lang="es", pbf_bytes=329000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6580,
    ),
    "colombia": OsmRegion(
        slug="colombia", source="osm_colombia", region="south-america/colombia",
        continent="south-america", label="コロンビア", label_en="Colombia",
        lang="es", pbf_bytes=307000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=6140,
    ),
    "ecuador": OsmRegion(
        slug="ecuador", source="osm_ecuador", region="south-america/ecuador",
        continent="south-america", label="エクアドル", label_en="Ecuador",
        lang="es", pbf_bytes=118000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2360,
    ),
    "guyana": OsmRegion(
        slug="guyana", source="osm_guyana", region="south-america/guyana",
        continent="south-america", label="ガイアナ", label_en="Guyana",
        lang="en", pbf_bytes=14800000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=296,
    ),
    "paraguay": OsmRegion(
        slug="paraguay", source="osm_paraguay", region="south-america/paraguay",
        continent="south-america", label="パラグアイ", label_en="Paraguay",
        lang="gn", pbf_bytes=146000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2920,
    ),
    "peru": OsmRegion(
        slug="peru", source="osm_peru", region="south-america/peru",
        continent="south-america", label="ペルー", label_en="Peru",
        lang="es", pbf_bytes=242000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=4840,
    ),
    "suriname": OsmRegion(
        slug="suriname", source="osm_suriname", region="south-america/suriname",
        continent="south-america", label="スリナム", label_en="Suriname",
        lang="nl", pbf_bytes=20300000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=406,
    ),
    "uruguay": OsmRegion(
        slug="uruguay", source="osm_uruguay", region="south-america/uruguay",
        continent="south-america", label="ウルグアイ", label_en="Uruguay",
        lang="es", pbf_bytes=53000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=1060,
    ),
    "venezuela": OsmRegion(
        slug="venezuela", source="osm_venezuela", region="south-america/venezuela",
        continent="south-america", label="ベネズエラ", label_en="Venezuela",
        lang="es", pbf_bytes=118000000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=2360,
    ),
    "antarctica": OsmRegion(
        slug="antarctica", source="osm_antarctica", region="antarctica",
        continent="standalone", label="南極", label_en="Antarctica",
        lang=None, pbf_bytes=31600000, memory_gb=3.0,
        node_index="sparse_mmap_array", min_docs=632,
    ),
    "russia": OsmRegion(
        slug="russia", source="osm_russia", region="russia",
        continent="standalone", label="ロシア", label_en="Russian Federation",
        lang="ru", pbf_bytes=3800000000, memory_gb=19.0,
        node_index="sparse_file_array", min_docs=76000,
    ),
}


def by_continent() -> dict[str, list[OsmRegion]]:
    """大陸 → その大陸の抽出一覧(CONTINENTS の順、国は表示名順)。"""
    grouped: dict[str, list[OsmRegion]] = {c: [] for c in CONTINENTS}
    for region in OSM_REGIONS.values():
        grouped.setdefault(region.continent, []).append(region)
    return {c: rs for c, rs in grouped.items() if rs}
