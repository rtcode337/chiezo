#!/usr/bin/env python3
"""既存 DB を schema_version 2 → 3 に移行する(タグ転置表 doc_tags を足す)。

タグ(Wikipedia のカテゴリ等)は 2 以前の DB にも `docs.tags`(JSON 配列)として
入っている。3 で足したのは、それを引くための転置表と索引だけなので、ダンプを
取り直さなくても既存 DB をその場で移行できる(jawiki の再取り込みは 2〜6 時間、
この移行は数分〜十数分)。

使い方:

    docker compose stop chiezo-api          # 読み取り中の DB を書き換えないため
    python3 scripts/add_tag_index.py data/jawiki.db
    docker compose start chiezo-api

シンボリックリンク(`jawiki.db`)を渡してよい(実体の世代ファイルを書き換える)。
すでに 3 の DB に対しては何もしない。中断した場合はもう一度実行すればよい
(doc_tags を作り直すところからやり直す。meta の更新は最後の 1 ステップなので、
中途半端に 3 を名乗る DB は残らない)。
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

# DDL と展開 SQL は取り込み側と同じものを使う(コピーを持つと必ず食い違うため)。
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))
try:
    from core import DOC_TAGS_POPULATE_SQL, SCHEMA_VERSION
except ImportError as e:  # pragma: no cover - 配置ミスの案内
    raise SystemExit(
        f"ingest/core.py を読めない({e})。chiezo のチェックアウト内から"
        " scripts/add_tag_index.py を実行してください"
    ) from e

TARGET_VERSION = 3

DDL = """
CREATE TABLE IF NOT EXISTS doc_tags (
    tag       TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);
"""


def upgrade(db_path: Path, vacuum: bool = False) -> int:
    """doc_tags を作って索引を張り、meta.schema_version を上げる。追加した行数を返す。"""
    target = db_path.resolve()  # シンボリックリンクなら実体の世代ファイルを触る
    conn = sqlite3.connect(target)
    try:
        row = conn.execute("SELECT source, schema_version FROM meta").fetchone()
        if row is None:
            raise SystemExit(f"{db_path}: meta table is empty (not a chiezo DB?)")
        source, version = row
        if version >= TARGET_VERSION:
            print(f"{source}: already schema_version={version}; nothing to do")
            return 0
        if version < 2:
            raise SystemExit(
                f"{source}: schema_version={version} lacks the generated columns added in 2;"
                " re-run ingest instead of migrating"
            )

        print(f"{source}: schema_version {version} -> {TARGET_VERSION} ({target})")
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.executescript(DDL)
        # 途中で落ちた前回の残骸があっても二重に積まないよう、毎回作り直す
        conn.execute("DROP INDEX IF EXISTS idx_doc_tags_tag")
        conn.execute("DELETE FROM doc_tags")
        conn.commit()

        started = time.monotonic()
        print("expanding docs.tags ...")
        conn.execute(DOC_TAGS_POPULATE_SQL)
        conn.commit()
        (tag_rows,) = conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()
        print(f"  {tag_rows:,} tag rows in {time.monotonic() - started:.0f}s")

        print("creating index ...")
        conn.execute("CREATE INDEX idx_doc_tags_tag ON doc_tags(tag, doc_id)")
        conn.commit()

        if vacuum:
            print("VACUUM ...")
            conn.execute("VACUUM")

        conn.execute("UPDATE meta SET schema_version = ?", (TARGET_VERSION,))
        conn.commit()
        print(f"done in {time.monotonic() - started:.0f}s. restart chiezo-api to pick it up.")
        return tag_rows
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path, nargs="+", help="移行する <source>.db(複数可)")
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="移行後に VACUUM する(ファイルを詰めるが、DB と同じだけの空きディスクが要る)",
    )
    args = parser.parse_args()
    if SCHEMA_VERSION < TARGET_VERSION:  # 取り込み側と食い違ったまま配らないための保険
        raise SystemExit(
            f"ingest/core.py の SCHEMA_VERSION={SCHEMA_VERSION} がこのスクリプトの想定"
            f"({TARGET_VERSION})より古い"
        )
    for db_path in args.db:
        if not db_path.exists():
            raise SystemExit(f"{db_path}: no such file")
        upgrade(db_path, vacuum=args.vacuum)


if __name__ == "__main__":
    main()
