"""コアスキーマ定義と Doc 型(全ソース共通)。

設計書 §3 に対応。ソースアダプタはここで定義する Doc を yield するだけでよく、
DB 構築・FTS・検証・切り替えは共通フレーム(main.py)が担う。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Protocol

SCHEMA_VERSION = 2

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
"""


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
