"""chiezo-ingest 共通フレーム(設計書 §6)。

処理フロー:
  1. 取得    adapter.fetch()(再開可能ダウンロード。DUMP_FILE 指定でスキップ可)
  2. 構築    /data/<source>-<date>.db.building へストリーミング + バルク INSERT
  3. FTS     docs_fts 構築 → optimize → VACUUM
  4. 検証    最低件数 + サンプルタイトルの検索が通ること
  5. 切り替え <source>-<date>.db にリネームし、シンボリックリンク <source>.db を差し替え

中断時は最初からやり直しで良い(.building の一時ファイル方式のため運用 DB は壊れない)。
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import CORE_INDEX_DDL, CORE_SCHEMA_DDL, SCHEMA_VERSION, SourceAdapter

log = logging.getLogger("chiezo.ingest")

BATCH_SIZE = 2000
PROGRESS_EVERY = 100_000

BUILD_PRAGMAS = [
    "PRAGMA journal_mode=OFF",
    "PRAGMA synchronous=OFF",
    "PRAGMA cache_size=-524288",
]


def build_db(adapter: SourceAdapter, dump_path: Path, dump_date: str, building_path: Path) -> None:
    """一時ファイル building_path にコアスキーマ DB を構築する。"""
    if building_path.exists():
        building_path.unlink()
    conn = sqlite3.connect(building_path)
    try:
        for pragma in BUILD_PRAGMAS:
            conn.execute(pragma)
        conn.executescript(CORE_SCHEMA_DDL)
        conn.execute(
            "INSERT INTO meta (source, source_kind, lang, dump_date, schema_version, built_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                adapter.source,
                adapter.source_kind,
                adapter.lang,
                dump_date,
                SCHEMA_VERSION,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        conn.commit()

        doc_rows: list[tuple] = []
        alias_rows: list[tuple] = []
        total = 0

        def flush() -> None:
            if doc_rows:
                conn.executemany(
                    "INSERT OR REPLACE INTO docs"
                    " (doc_id, title, opening, body, tags, links, updated_at, rank_score, extra)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    doc_rows,
                )
                doc_rows.clear()
            if alias_rows:
                conn.executemany("INSERT INTO aliases (alias, doc_id) VALUES (?, ?)", alias_rows)
                alias_rows.clear()
            conn.commit()

        for doc in adapter.iter_docs(dump_path):
            doc_rows.append(
                (
                    doc.doc_id,
                    doc.title,
                    doc.opening,
                    doc.body,
                    json.dumps(doc.tags, ensure_ascii=False) if doc.tags else None,
                    json.dumps(doc.links, ensure_ascii=False) if doc.links else None,
                    doc.updated_at,
                    doc.rank_score,
                    json.dumps(doc.extra, ensure_ascii=False) if doc.extra else None,
                )
            )
            alias_rows.extend((alias, doc.doc_id) for alias in doc.aliases)
            total += 1
            if len(doc_rows) >= BATCH_SIZE:
                flush()
            if total % PROGRESS_EVERY == 0:
                log.info("inserted %d docs...", total)
        flush()
        log.info("inserted %d docs total", total)

        log.info("creating indexes...")
        conn.executescript(CORE_INDEX_DDL)
        conn.commit()

        log.info("building FTS index...")
        conn.execute("INSERT INTO docs_fts(rowid, title, body) SELECT doc_id, title, body FROM docs")
        conn.commit()
        conn.execute("INSERT INTO docs_fts(docs_fts) VALUES('optimize')")
        conn.commit()

        log.info("VACUUM...")
        conn.execute("VACUUM")
        conn.commit()
    finally:
        conn.close()


def validate_db(adapter: SourceAdapter, db_path: Path) -> None:
    """最低件数とサンプルタイトルの検索が通ることを確認する(設計書 §6.1-4)。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        (count,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        if count < adapter.min_docs:
            raise RuntimeError(f"validation failed: only {count} docs (< {adapter.min_docs})")
        for title in adapter.sample_titles:
            row = conn.execute(
                "SELECT doc_id FROM docs WHERE title = ?"
                " UNION SELECT doc_id FROM aliases WHERE alias = ? LIMIT 1",
                (title, title),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"validation failed: sample title not found: {title!r}")
            # trigram トークナイザは 3 文字未満を扱えない(api/app/fts.py の MIN_TRIGRAM_LEN と同じ閾値)。
            # API 自体もその場合はタイトル前方一致にフォールバックするため、FTS 検証は対象外。
            if len(title) < 3:
                continue
            fts = conn.execute(
                "SELECT rowid FROM docs_fts WHERE docs_fts MATCH ? LIMIT 1",
                ('"' + title.replace('"', "") + '"',),
            ).fetchone()
            if fts is None:
                raise RuntimeError(f"validation failed: FTS search returned nothing for {title!r}")
        log.info("validation OK: %d docs, %d sample titles", count, len(adapter.sample_titles))
    finally:
        conn.close()


def switch_db(data_dir: Path, source: str, dump_date: str, building_path: Path) -> Path:
    """ブルーグリーン切り替え: リネーム → シンボリックリンク差し替え → 旧世代を 1 つだけ残して削除。"""
    final_path = data_dir / f"{source}-{dump_date}.db"
    building_path.replace(final_path)

    link = data_dir / f"{source}.db"
    previous_target = link.resolve() if link.is_symlink() else None
    tmp_link = data_dir / f"{source}.db.tmp"
    if tmp_link.exists() or tmp_link.is_symlink():
        tmp_link.unlink()
    tmp_link.symlink_to(final_path.name)
    tmp_link.replace(link)  # アトミックに差し替え
    log.info("switched %s -> %s", link.name, final_path.name)

    # 旧世代は 1 つだけ保持
    generations = sorted(
        p for p in data_dir.glob(f"{source}-*.db") if p != final_path and not p.is_symlink()
    )
    keep = {final_path}
    if previous_target and previous_target.exists():
        keep.add(previous_target)
    elif generations:
        keep.add(generations[-1])
    for p in generations:
        if p not in keep:
            log.info("removing old generation: %s", p.name)
            p.unlink()
    return final_path


def run(source: str, data_dir: Path) -> Path:
    from sources import get_adapter

    adapter = get_adapter(source)
    # 検証パラメータの上書き(小規模データでの動作確認用)
    if min_docs := os.environ.get("MIN_DOCS"):
        adapter.min_docs = int(min_docs)
    if sample_titles := os.environ.get("SAMPLE_TITLES"):
        adapter.sample_titles = [t for t in sample_titles.split(",") if t]
    dumps_dir = data_dir / "dumps"
    dumps_dir.mkdir(parents=True, exist_ok=True)

    # DUMP_FILE 指定時はダウンロードをスキップ(テスト・手動投入用。カンマ区切りで複数シャード可)
    if dump_file := os.environ.get("DUMP_FILE"):
        files = [Path(f) for f in dump_file.split(",") if f]
        dump_path = files[0] if len(files) == 1 else files
        dump_date = os.environ.get("DUMP_DATE") or datetime.now(timezone.utc).strftime("%Y%m%d")
    else:
        dump_path, dump_date = adapter.fetch(dumps_dir)
        # アダプタが対応していれば、docs.extra 補強用の追加データ(ページビュー等)も取得する
        if fetch_extra := getattr(adapter, "fetch_pageviews", None):
            fetch_extra(dumps_dir)

    building_path = data_dir / f"{source}-{dump_date}.db.building"
    n_files = len(dump_path) if isinstance(dump_path, list) else 1
    log.info("building %s from %d dump file(s)", building_path.name, n_files)
    build_db(adapter, dump_path, dump_date, building_path)
    validate_db(adapter, building_path)
    final_path = switch_db(data_dir, source, dump_date, building_path)
    log.info("done: %s", final_path)
    log.info("restart the API to pick up the new DB: docker compose restart chiezo-api")
    return final_path


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    source = os.environ.get("SOURCE")
    if not source:
        log.error("SOURCE environment variable is required (e.g. SOURCE=jawiki)")
        sys.exit(2)
    data_dir = Path(os.environ.get("CHIEZO_DATA_DIR", "/data"))
    run(source, data_dir)


if __name__ == "__main__":
    main()
