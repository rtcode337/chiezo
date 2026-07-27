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

非力なマシンでの実行について:

- **メモリは要らない**。SQLite が数 MB のページキャッシュで流し込むだけで、実測でも
  100 万文書(300 万タグ行)の展開でピーク RSS 24MiB だった。文書数によらずほぼ一定。
- 効くのは**ディスクと時間**。タグ 1 行あたり約 50 バイト増える(jawiki 15〜25M 行で
  1GB 前後、geonames 50M 行で 2〜3GB)。時間は docs の全走査が支配的で、遅いディスクだと
  jawiki(42GB)で 10 分以上かかりうる。
- `DISTINCT` の並べ替えは一時ファイルに落ちる。**一度に全件やらず `--batch` 件ずつ**
  処理して一時領域を小さく保つ(既定 20 万文書)。それでも足りない環境では
  `SQLITE_TMPDIR=/空きのある場所` を指定する。
- ロールバックジャーナルは**無効化しない**。取り込み時の一時 DB と違ってこれは運用 DB
  そのものなので、途中で kill されても壊れないことを速度より優先する。
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
    from core import DOC_TAGS_POPULATE_BATCH_SQL, SCHEMA_VERSION
except ImportError as e:  # pragma: no cover - 配置ミスの案内
    raise SystemExit(
        f"ingest/core.py を読めない({e})。chiezo のチェックアウト内から"
        " scripts/add_tag_index.py を実行してください"
    ) from e

TARGET_VERSION = 3

# 1 バッチで処理する文書数。小さいほど一時領域が小さく進捗も細かいが、
# バッチごとのコミット回数が増える。
DEFAULT_BATCH = 200_000

DDL = """
CREATE TABLE IF NOT EXISTS doc_tags (
    tag       TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);
"""


def upgrade(db_path: Path, vacuum: bool = False, batch: int = DEFAULT_BATCH) -> int:
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
        # ジャーナルは切らない。ここは取り込み時の使い捨て .building と違って運用 DB 本体で、
        # 途中で kill されたときに壊れないことを速度より優先する。
        conn.executescript(DDL)
        # 途中で落ちた前回の残骸があっても二重に積まないよう、毎回作り直す
        conn.execute("DROP INDEX IF EXISTS idx_doc_tags_tag")
        conn.execute("DELETE FROM doc_tags")
        conn.commit()

        started = time.monotonic()
        (total_docs,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        print(f"expanding docs.tags for {total_docs:,} docs (batch={batch:,}) ...")
        # 一度に全件やると DISTINCT の並べ替えが巨大な一時ファイルになるので、
        # 前回の続きから batch 件ずつ処理する(doc_id は等間隔でないので件数で刻む)。
        last_id, done = -(1 << 62), 0
        while True:
            (next_last,) = conn.execute(
                "SELECT MAX(doc_id) FROM (SELECT doc_id FROM docs WHERE doc_id > ?"
                " ORDER BY doc_id LIMIT ?)",
                (last_id, batch),
            ).fetchone()
            if next_last is None:
                break
            conn.execute(DOC_TAGS_POPULATE_BATCH_SQL, (last_id, batch))
            conn.commit()
            last_id = next_last
            done = min(done + batch, total_docs)
            (tag_rows,) = conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()
            print(
                f"  {done:,}/{total_docs:,} docs -> {tag_rows:,} tag rows"
                f" ({time.monotonic() - started:.0f}s)"
            )
        (tag_rows,) = conn.execute("SELECT COUNT(*) FROM doc_tags").fetchone()

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
    parser.add_argument(
        "--batch",
        type=int,
        default=DEFAULT_BATCH,
        help=f"1 バッチで処理する文書数(既定 {DEFAULT_BATCH:,})。一時領域が厳しい環境では小さくする",
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
        upgrade(db_path, vacuum=args.vacuum, batch=args.batch)


if __name__ == "__main__":
    main()
