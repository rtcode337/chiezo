"""/data/*.db の走査とソース登録(設計書 §5)。

起動時に CHIEZO_DATA_DIR を走査し、各 DB の meta を読んでソースとして登録する。
`<source>.db` というファイル名(通常は世代 DB へのシンボリックリンク)のみを
登録対象とし、`jawiki-20260701.db` のような世代ファイル自体は無視する
(ファイル名の拡張子を除いた部分と meta.source が一致するものだけを登録)。
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("chiezo.app")


@dataclass
class Source:
    name: str
    kind: str
    lang: str | None
    dump_date: str | None
    schema_version: int
    built_at: str
    doc_count: int
    path: Path
    # 追記されうるソース(notes)。`immutable=1` で開けないので db 側に伝える必要がある。
    # 通常のソースは ingest のブルーグリーンでしか変わらないので False。
    mutable: bool = False


SUPPORTED_SCHEMA_VERSIONS = {1, 2, 3, 4}

# 生成列 (feature/area/lat/lon/wikidata) と索引が入ったのは schema_version 2 から。
# 1 のまま残っている DB に対しては /v1/<source>/filter を 409 で断る。
FILTER_MIN_SCHEMA_VERSION = 2

# タグ転置表 (doc_tags) が入ったのは schema_version 3 から。
# 2 以下の DB に対しては tag での絞り込みと /v1/<source>/tags を 409 で断る
# (取り込み直すか scripts/add_tag_index.py で移行する)。
TAG_MIN_SCHEMA_VERSION = 3

# タグ名の集計表 (tag_counts) が入ったのは schema_version 4 から。
# 3 の DB でも /v1/<source>/tags は動くが、doc_tags 全体を読む遅い経路になる
# (jawiki・geonames の部分一致は配信機だと 5 秒のクエリタイムアウトを超える)。
# 断らずに落とすだけなのは、移行前でも今までどおりには使えるようにするため。
TAG_COUNTS_MIN_SCHEMA_VERSION = 4

# 並び順(rank_score DESC, title)そのものを持つ索引 idx_docs_rank が入ったのも 4 から。
# /v1/<source>/filter がタグだけで絞るときに INDEXED BY で名指しするので、
# 3 以下の DB では名指ししない(索引が無い DB に INDEXED BY を書くとエラーになる)。
RANK_INDEX_MIN_SCHEMA_VERSION = 4

# 座標表 (doc_coords) が入ったのも 4 から。3 以下では docs の生成列 lat/lon を直接
# 引く旧経路になる(引けるが、緯度帯にある全文書の行を読むので bbox が遅い)。
COORDS_MIN_SCHEMA_VERSION = 4


def _load_source(db_path: Path, mutable: bool = False) -> Source | None:
    uri = f"file:{db_path}?mode=ro" if mutable else f"file:{db_path}?immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as e:
        log.warning("skipping %s: cannot open (%s)", db_path.name, e)
        return None
    try:
        row = conn.execute(
            "SELECT source, source_kind, lang, dump_date, schema_version, built_at FROM meta"
        ).fetchone()
        if row is None:
            log.warning("skipping %s: empty meta table", db_path.name)
            return None
        source, kind, lang, dump_date, schema_version, built_at = row
        if db_path.stem != source:
            # 世代ファイル(jawiki-20260701.db 等)は登録しない
            return None
        if schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            log.warning(
                "skipping %s: unsupported schema_version=%s", db_path.name, schema_version
            )
            return None
        (doc_count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        return Source(
            name=source,
            kind=kind,
            lang=lang,
            dump_date=dump_date,
            schema_version=schema_version,
            built_at=built_at,
            doc_count=doc_count,
            path=db_path,
            mutable=mutable,
        )
    except sqlite3.Error as e:
        log.warning("skipping %s: %s", db_path.name, e)
        return None
    finally:
        conn.close()


def data_dir_fingerprint(data_dir: Path) -> dict[str, tuple[int, int, int, int]]:
    """世代切り替え検知用の指紋: `*.db` それぞれのリンク先実体の (dev, ino, size, mtime)。

    シンボリックリンクの差し替え(ino が変わる)だけでなく、コピー途中のファイル
    (size / mtime が動き続ける)も指紋の変化として現れるので、書き込みが落ち着くまで
    呼び出し側が再走査を繰り返す形で安定を待てる。stat のみでクエリは発行しない。
    """
    fp: dict[str, tuple[int, int, int, int]] = {}
    if not data_dir.is_dir():
        return fp
    for p in sorted(data_dir.glob("*.db")):
        try:
            st = p.stat()
        except OSError:
            continue  # 走査中に消えた(旧世代の削除など)
        fp[p.name] = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
    return fp


def scan_sources(data_dir: Path, mutable: bool = False) -> dict[str, Source]:
    sources: dict[str, Source] = {}
    if not data_dir.is_dir():
        log.warning("data dir does not exist: %s", data_dir)
        return sources
    for db_path in sorted(data_dir.glob("*.db")):
        src = _load_source(db_path, mutable)
        if src is not None:
            sources[src.name] = src
            log.info(
                "registered source %s (kind=%s docs=%d dump_date=%s)",
                src.name, src.kind, src.doc_count, src.dump_date,
            )
    return sources
