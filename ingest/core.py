"""コアスキーマ定義と Doc 型(全ソース共通)。

設計書 §3 に対応。ソースアダプタはここで定義する Doc を yield するだけでよく、
DB 構築・FTS・検証・切り替えは共通フレーム(main.py)が担う。
"""
from __future__ import annotations

import math
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SCHEMA_VERSION = 4

CORE_SCHEMA_DDL = """
-- ソース自身のメタ情報(1行)
CREATE TABLE meta (
    source        TEXT NOT NULL,
    source_kind   TEXT NOT NULL,
    lang          TEXT,
    dump_date     TEXT,
    schema_version INTEGER NOT NULL DEFAULT 1,
    built_at      TEXT NOT NULL
);

-- 文書(Wikipediaなら記事、青空文庫なら作品、に相当する共通単位)
CREATE TABLE docs (
    doc_id      INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    opening     TEXT,
    body        TEXT,
    tags        TEXT,
    links       TEXT,
    updated_at  TEXT,
    rank_score  REAL DEFAULT 0,
    extra       TEXT,
    -- 絞り込み用の生成列(schema_version 2 で追加)。
    -- 実体は extra(JSON)のままで、ここでは索引を張るための射影だけを定義する
    -- (VIRTUAL = 値を保存せず参照時に json_extract する)。アダプタ側は今までどおり
    -- extra に詰めるだけでよく、Doc の形も変わらない。
    feature     TEXT GENERATED ALWAYS AS (json_extract(extra, '$.feature')) VIRTUAL,
    area        TEXT GENERATED ALWAYS AS (json_extract(extra, '$.area')) VIRTUAL,
    lat         REAL GENERATED ALWAYS AS (json_extract(extra, '$.lat')) VIRTUAL,
    lon         REAL GENERATED ALWAYS AS (json_extract(extra, '$.lon')) VIRTUAL,
    wikidata    TEXT GENERATED ALWAYS AS (json_extract(extra, '$.wikidata')) VIRTUAL
);

-- 別名 → 正規文書(Wikipediaのリダイレクト等)
CREATE TABLE aliases (
    alias     TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);

-- タグ(Wikipediaのカテゴリ等)→ 文書の転置表(schema_version 3 で追加)。
-- docs.tags(JSON 配列)を展開しただけの射影で、実体は今までどおり docs.tags のまま。
-- 生成列にできないのは 1 文書が複数のタグを持つ(1 行に畳めない)ため。
-- 全文検索でカテゴリを探す代用(本文の "Category:" 行を引く)は、ソートキー付き
-- (`[[Category:ラーメン店|らあめんしろう]]`)の記事で本文側からカテゴリ名が落ちるため
-- 取りこぼす。tags は wikitext のリンク先から取っており落ちないので、こちらを索引する。
CREATE TABLE doc_tags (
    tag       TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);

-- タグ名 → 文書数の集計表(schema_version 4 で追加)。doc_tags を GROUP BY した
-- だけの要約で、新しい情報は持たない。分けてあるのは配信側の読み取り量のため:
-- 「どんなタグがあるか」を探す /v1/<source>/tags は doc_tags 側だと転置表全体
-- (jawiki で 764 万行・索引 300MB)を読む必要があるのに対し、ここは重複を畳んだ
-- 29 万行・12MB で済む。配信機は数百 MB メモリの小型機で毎回ディスクから読むため、
-- この差がそのまま応答時間になる(部分一致 tags?contains= が 5 秒のクエリ
-- タイムアウトを超えて 504 になっていた)。
CREATE TABLE tag_counts (
    tag       TEXT PRIMARY KEY,
    docs      INTEGER NOT NULL
) WITHOUT ROWID;

-- 座標 → 文書の索引表(schema_version 4 で追加)。docs の生成列 lat/lon と同じ値の
-- 射影で、新しい情報は持たない。生成列の索引では bbox が引けないため分けてある:
-- lat/lon は VIRTUAL(値を保存せず参照時に json_extract する)なので、SQLite は
-- idx_docs_lat_lon で緯度の範囲までは絞れても、経度の判定には行本体を読み直す
-- (被覆索引にならない)。結果として費用が「該当件数」ではなく「その緯度帯にある
-- 全文書数」に比例し、0.05 度四方の bbox でも 3.5 万行を読んでいた(配信機で 13 秒 = 504)。
-- ここは実体の値を持つので、緯度帯の走査も経度の判定も索引の中だけで完結する。
CREATE TABLE doc_coords (
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    doc_id    INTEGER NOT NULL,
    PRIMARY KEY (lat, lon, doc_id)
) WITHOUT ROWID;

-- 全文検索(external content 方式で本文の二重保存を回避)
CREATE VIRTUAL TABLE docs_fts USING fts5(
    title, body,
    content='docs',
    content_rowid='doc_id',
    tokenize='trigram'
);
"""

# インデックスは全行 INSERT 後に張る方が速い
CORE_INDEX_DDL = """
CREATE UNIQUE INDEX idx_docs_title ON docs(title);
CREATE INDEX idx_aliases_alias ON aliases(alias);
-- 生成列の索引。/v1/<source>/filter の「地物種別 × 地域」「bbox」「wikidata 逆引き」用。
-- 該当値を持たないソース(jawiki の area/lat/lon 等)では NULL が並ぶだけで害はない。
CREATE INDEX idx_docs_feature_area ON docs(feature, area);
CREATE INDEX idx_docs_area_feature ON docs(area, feature);
CREATE INDEX idx_docs_lat_lon ON docs(lat, lon);
CREATE INDEX idx_docs_wikidata ON docs(wikidata);
-- /v1/<source>/filter?tag= と /v1/<source>/tags?prefix= 用。doc_id まで含めた複合に
-- するのは、タグ → doc_id の引き当てとタグ名の集計をこの索引だけで済ませるため
-- (covering index。docs 本体を触らない)。
CREATE INDEX idx_doc_tags_tag ON doc_tags(tag, doc_id);
-- /v1/<source>/tags の「文書数の多い順に上位 N 件」用。この順で並んだ索引があると
-- 部分一致(contains)でも先頭から数えて N 件見つかった時点で走査を打ち切れる。
CREATE INDEX idx_tag_counts_docs ON tag_counts(docs DESC, tag);
-- /v1/<source>/filter の ORDER BY rank_score DESC, title 用(schema_version 4 で追加)。
-- 並び順そのものを索引に持たせて、上位 N 件で走査を打ち切れるようにするためのもの。
-- これが無いと、該当文書を全部 docs から読んでから並べ替えることになり、jawiki の
-- 「存命人物」(25 万件)は limit=1 でも 33 秒かかった(実測。この索引を使わせると 0.05 秒)。
-- title まで入れているのは第 2 キーまで索引で満たすため(rank_score は同点が多い)。
CREATE INDEX idx_docs_rank ON docs(rank_score DESC, title);
"""

# docs 投入後に doc_tags を組み立てる SQL(索引を張る前に流す)。
# docs から作り直す方式にしているのは、docs 側が INSERT OR REPLACE を使う(同じ doc_id が
# 二度来ても最後の 1 件が残る)ため。行ごとに append すると置き換えられた古いタグが残る。
# scripts/add_tag_index.py(既存 DB の schema 2 → 3 移行)も同じ SQL を使う。
DOC_TAGS_POPULATE_SQL = """
INSERT INTO doc_tags (tag, doc_id)
SELECT DISTINCT json_each.value, docs.doc_id
  FROM docs, json_each(docs.tags)
 WHERE docs.tags IS NOT NULL AND json_each.value <> ''
"""

# 上の分割版(scripts/add_tag_index.py が既存 DB を少しずつ埋めるのに使う)。
# doc_id の値で等分割できないので(osm の doc_id は osm_id*4 で 10 桁を超える)、
# 「前回の続きから N 件」という件数での刻みにしている。パラメータは (前回の最大 doc_id, 件数)。
DOC_TAGS_POPULATE_BATCH_SQL = """
INSERT INTO doc_tags (tag, doc_id)
SELECT DISTINCT json_each.value, d.doc_id
  FROM (SELECT doc_id, tags FROM docs WHERE doc_id > ? ORDER BY doc_id LIMIT ?) AS d,
       json_each(d.tags)
 WHERE d.tags IS NOT NULL AND json_each.value <> ''
"""

# doc_tags を畳んで tag_counts を作る SQL(doc_tags の索引を張った後に流す。
# idx_doc_tags_tag が tag 順なので GROUP BY が並べ替えなしのストリーム集計になる)。
# scripts/add_tag_index.py(既存 DB の移行)も同じ SQL を使う。
TAG_COUNTS_POPULATE_SQL = """
INSERT INTO tag_counts (tag, docs)
SELECT tag, COUNT(*) FROM doc_tags GROUP BY tag
"""

# docs の生成列から doc_coords を作る SQL(idx_docs_lat_lon を張った後に流す)。
# INDEXED BY は速度のため。放っておくと SQLite は docs の全走査を選び、座標を持つ
# 文書が一部でも全行の json_extract を回す(手元の jawiki 41GB で 7.4 秒 → 0.04 秒)。
# scripts/add_tag_index.py(既存 DB の移行)も同じ SQL を使う。
DOC_COORDS_POPULATE_SQL = """
INSERT INTO doc_coords (lat, lon, doc_id)
SELECT lat, lon, doc_id FROM docs INDEXED BY idx_docs_lat_lon
 WHERE lat IS NOT NULL AND lon IS NOT NULL
"""


# ---- 構築プロファイル ---------------------------------------------------------

# 構築の速度とメモリのどちらを優先するかの切り替え(環境変数 BUILD_PROFILE)。
#   low_memory(既定): メモリ優先。どのソースも 2GiB で構築できる。構築用 SQLite
#                      キャッシュを 64MiB に絞り、osm のノード座標索引をディスクに置く
#                      (osm は数倍〜10 倍遅い。wikipedia / geonames はほぼ変わらない)。
#   fast             : 速度優先。SQLite キャッシュ 512MiB、osm はソースごとの既定索引
#                      (小さい国は RAM 索引で 5〜12GiB)。
# 既定を low_memory にしてあるのは、本番(配信)サーバも開発機もメモリ 2GiB 級という
# 運用のため — 何も指定せずに走らせても安全に完走するほうを既定にする。fast は
# メモリの潤沢なビルド機で ingest を走らせるときに `-e BUILD_PROFILE=fast` と
# 実行時の引数として明示したときだけ使う(compose 等に常設しない)。
# main(PRAGMA)と各アダプタ(必要メモリ宣言・osm の索引方式)の両方が参照するため、
# ここ core に置く。
BUILD_PROFILE_FAST = "fast"
BUILD_PROFILE_LOW_MEMORY = "low_memory"
BUILD_PROFILES = (BUILD_PROFILE_LOW_MEMORY, BUILD_PROFILE_FAST)

# low_memory プロファイルでの必要メモリ宣言(GiB)。全ソース共通でこの値に収まることを
# テスト(test_low_memory_profile_fits_every_source_in_2gb)で担保している。
LOW_MEMORY_BUILD_GB = 2.0


def build_profile() -> str:
    """現在の構築プロファイル(環境変数 BUILD_PROFILE、既定 low_memory)。

    未知の値は既定に黙って倒さず中止する。綴り間違い(low-mem 等)を見逃すと、
    「fast を指定したのに遅い」「絞ったつもりで 12GiB 要求される」だけの状態になるため。
    """
    value = os.environ.get("BUILD_PROFILE") or BUILD_PROFILE_LOW_MEMORY
    if value not in BUILD_PROFILES:
        raise SystemExit(
            f"unknown BUILD_PROFILE: {value!r} (expected one of: {', '.join(BUILD_PROFILES)})"
        )
    return value


def is_low_memory_build() -> bool:
    return build_profile() == BUILD_PROFILE_LOW_MEMORY


# rank_score の正規化に使う対数の上限。ソースごとに桁が違う量(ページビュー・人口)を
# 同じ 0.0〜1.0 に写すための係数で、この値で 1.0 に張り付く。
POPULARITY_LOG_MAX_PAGEVIEWS = 7.0        # 月間 1000 万 PV
POPULARITY_LOG_MAX_CITY_POPULATION = 8.0  # 人口 1 億(osm が元から使っていた係数)
# geonames は国そのものも 1 文書として持ち、人口が 14 億まで行く。都市規模と同じ
# 係数だと 1 億超の国が全部 1.0 に張り付いて、上位の区別が消えるため分けてある。
POPULARITY_LOG_MAX_COUNTRY_POPULATION = 10.0


def normalized_popularity(value: float | int | None, log_max: float) -> float:
    """人気度の生値(ページビュー・人口)を 0.0〜1.0 の rank_score に正規化する。

    rank_score は全ソース共通で 0.0〜1.0 という約束にしてある。API が関連度
    (bm25)に人気度を掛け合わせて並べるため、ソースごとに桁が違うと混ぜられないから
    (人口 3000 万とページビュー 100 万を同じ式には入れられない)。
    対数を使うのは、人気度が桁で効く量だから(10 万 PV と 20 万 PV の差より、
    1000 PV と 10 万 PV の差の方が意味がある)。
    """
    if not value or value <= 0:
        return 0.0
    return round(min(1.0, math.log10(1 + value) / log_max), 4)


@dataclass
class Doc:
    """コアスキーマ 1 文書。アダプタが iter_docs() で yield する単位。"""

    doc_id: int
    title: str
    opening: str | None = None
    body: str | None = None
    tags: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)
    aliases: list[str] = field(default_factory=list)
    updated_at: str | None = None
    # 0.0〜1.0 に正規化した知名度・重要度(normalized_popularity() 参照)。
    # 検索の並びで bm25 に掛け合わせるので、この範囲を外れると並びが壊れる。
    rank_score: float = 0.0
    extra: dict[str, Any] | None = None


class SourceAdapter(Protocol):
    """ソースアダプタが満たすべきインターフェース(設計書 §3.3)。"""

    source: str          # 'jawiki'
    source_kind: str     # 'wikipedia'
    lang: str | None     # 'ja'
    min_docs: int        # 検証: 最低文書数
    sample_titles: list[str]  # 検証: 検索が通るべきタイトル
    # 構築に必要なメモリの目安(GiB)。取り込み開始前に main.require_build_memory() が
    # 実際に使えるメモリと突き合わせ、足りなければ構築せず中止する。取り込みは潤沢メモリの
    # マシンで回す前提で、上限で締めて OOM に殺されるより先に落とすほうが安全なため。
    min_build_memory_gb: float

    def fetch(self, workdir: Path) -> tuple[Path, str]:
        """元データを取得し (ローカルパス, ダンプ日付YYYYMMDD) を返す(再開可能に)。"""
        ...

    def iter_docs(self, path: Path) -> Iterator[Doc]:
        """元データをストリーミングで読み、コアスキーマの Doc を順に返す。"""
        ...
