"""コアスキーマ定義と Doc 型(全ソース共通)。

設計書 §3 に対応。ソースアダプタはここで定義する Doc を yield するだけでよく、
DB 構築・FTS・検証・切り替えは共通フレーム(main.py)が担う。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

SCHEMA_VERSION = 3

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


# rank_score の正規化に使う対数の上限。ソースごとに桁が違う量(ページビュー・人口)を
# 同じ 0.0〜1.0 に写すための係数で、この値で 1.0 に張り付く。
POPULARITY_LOG_MAX_PAGEVIEWS = 7.0        # 月間 1000 万 PV
POPULARITY_LOG_MAX_CITY_POPULATION = 8.0  # 人口 1 億(osm が元から使っていた係数)
# geonames は国そのものも 1 文書として持ち、人口が 14 億まで行く。都市規模と同じ
# 係数だと 1 億超の国が全部 1.0 に張り付いて、上位の区別が消えるため分けてある。
POPULARITY_LOG_MAX_COUNTRY_POPULATION = 10.0


def normalized_popularity(value: float | int | None, log_max: float) -> float:
    """人気度の生値(ページビュー・人口)を 0.0〜1.0 の rank_score に正規化する。

    **rank_score は全ソース共通で 0.0〜1.0 という約束**にしてある。API が関連度
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
