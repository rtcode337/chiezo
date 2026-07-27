#!/usr/bin/env python3
"""既存 DB をタグまわりの最新スキーマへ移行する(2 → 3 → 4)。

- 2 → 3: タグ転置表 `doc_tags`(タグ → 文書)と索引を足す
- 3 → 4: タグ名の集計表 `tag_counts`(タグ → 文書数)、並び順を持つ索引
  `idx_docs_rank`(rank_score DESC, title)、座標表 `doc_coords` を足す

どちらも元の情報は `docs.tags`(JSON 配列)と `doc_tags` にすでにあり、足すのは
引くための形だけなので、ダンプを取り直さなくても既存 DB をその場で移行できる
(jawiki の再取り込みは 2〜6 時間、この移行は数分〜十数分)。足りないステップだけ
流すので、3 の DB に対しては tag_counts を作るところだけが走る(jawiki で 1 分弱)。

4 が要るのは、どちらも「配信機で 5 秒のクエリタイムアウトを超えて 504 になる」経路を
畳むため(3 の DB でも動きはするが遅い):

- `tags?contains=`: 転置表(jawiki で 764 万行・索引 300MB)を毎回丸ごと読んでいた。
  tag_counts は同じ内容を 29 万行・12MB に畳んだものなので 1 秒以内に収まる。
- `filter?tag=`: 該当文書を全部 docs から読んでから並べ替えていた(「存命人物」25 万件で
  33 秒)。idx_docs_rank があると上位 N 件で走査を打ち切れて 0.05 秒。
- `filter?bbox=`: lat/lon は VIRTUAL な生成列で、索引では緯度の範囲までしか絞れず、
  経度の判定に行本体を読み直していた(0.05 度四方でも 3.5 万行。配信機で 13 秒)。
  doc_coords は実体の値を持つので索引の中だけで完結する。

使い方:

    docker compose stop chiezo-api          # 読み取り中の DB を書き換えないため
    python3 scripts/add_tag_index.py data/jawiki.db
    docker compose start chiezo-api

シンボリックリンク(`jawiki.db`)を渡してよい(実体の世代ファイルを書き換える)。
すでに 4 の DB に対しては何もしない。中断した場合はもう一度実行すればよい
(その版で作る表を作り直すところからやり直す。meta の更新は最後の 1 ステップなので、
中途半端に新しい版を名乗る DB は残らない)。

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
    from core import (
        DOC_COORDS_POPULATE_SQL,
        DOC_TAGS_POPULATE_BATCH_SQL,
        SCHEMA_VERSION,
        TAG_COUNTS_POPULATE_SQL,
    )
except ImportError as e:  # pragma: no cover - 配置ミスの案内
    raise SystemExit(
        f"ingest/core.py を読めない({e})。chiezo のチェックアウト内から"
        " scripts/add_tag_index.py を実行してください"
    ) from e

TARGET_VERSION = 4

# 1 バッチで処理する文書数。小さいほど一時領域が小さく進捗も細かいが、
# バッチごとのコミット回数が増える。
DEFAULT_BATCH = 200_000

DDL = """
CREATE TABLE IF NOT EXISTS doc_tags (
    tag       TEXT NOT NULL,
    doc_id    INTEGER NOT NULL REFERENCES docs(doc_id)
);
"""

TAG_COUNTS_DDL = """
CREATE TABLE IF NOT EXISTS tag_counts (
    tag       TEXT PRIMARY KEY,
    docs      INTEGER NOT NULL
) WITHOUT ROWID;
"""

DOC_COORDS_DDL = """
CREATE TABLE IF NOT EXISTS doc_coords (
    lat       REAL NOT NULL,
    lon       REAL NOT NULL,
    doc_id    INTEGER NOT NULL,
    PRIMARY KEY (lat, lon, doc_id)
) WITHOUT ROWID;
"""


def upgrade(db_path: Path, vacuum: bool = False, batch: int = DEFAULT_BATCH) -> int:
    """足りない移行ステップだけを流し、最後に meta.schema_version を上げる。

    戻り値は doc_tags の行数(3 へ上げたときだけ数える。既に 3 なら 0)。
    """
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
        started = time.monotonic()
        tag_rows = _build_doc_tags(conn, batch) if version < 3 else 0
        _build_tag_counts(conn)
        _build_rank_index(conn)
        _build_doc_coords(conn)

        if vacuum:
            print("VACUUM ...")
            conn.execute("VACUUM")

        conn.execute("UPDATE meta SET schema_version = ?", (TARGET_VERSION,))
        conn.commit()
        print(f"done in {time.monotonic() - started:.0f}s. restart chiezo-api to pick it up.")
        return tag_rows
    finally:
        conn.close()


def _build_doc_tags(conn: sqlite3.Connection, batch: int) -> int:
    """docs.tags を doc_tags へ展開して索引を張る(schema_version 2 → 3 の分)。"""
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
    return tag_rows


def _build_tag_counts(conn: sqlite3.Connection) -> None:
    """doc_tags を畳んで tag_counts を作る(schema_version 3 → 4 の分)。

    doc_tags 全体を 1 回読むだけで、一時領域も要らない(idx_doc_tags_tag が
    tag 順なので GROUP BY が並べ替えなしのストリーム集計になる)。
    """
    print("summarizing tags ...")
    conn.executescript(TAG_COUNTS_DDL)
    # 前回の中断や、古い版で作った内容が残っていても作り直す
    conn.execute("DROP INDEX IF EXISTS idx_tag_counts_docs")
    conn.execute("DELETE FROM tag_counts")
    conn.execute(TAG_COUNTS_POPULATE_SQL)
    conn.execute("CREATE INDEX idx_tag_counts_docs ON tag_counts(docs DESC, tag)")
    conn.commit()
    (tag_names,) = conn.execute("SELECT COUNT(*) FROM tag_counts").fetchone()
    print(f"  {tag_names:,} distinct tags")


def _build_rank_index(conn: sqlite3.Connection) -> None:
    """並び順(rank_score DESC, title)を持つ索引を張る(schema_version 3 → 4 の分)。

    `/v1/<source>/filter` がタグで絞るときの ORDER BY をこれで満たす。docs の全走査に
    なるので、tag_counts より時間がかかる(手元の jawiki 41GB で 18 秒、+50MB)。
    """
    print("creating rank index ...")
    conn.execute("DROP INDEX IF EXISTS idx_docs_rank")
    conn.execute("CREATE INDEX idx_docs_rank ON docs(rank_score DESC, title)")
    conn.commit()


def _build_doc_coords(conn: sqlite3.Connection) -> None:
    """docs の生成列 lat/lon を実体の値として doc_coords に写す(schema_version 3 → 4 の分)。

    座標を持つ文書の数だけ行を読む(全走査ではない。手元の jawiki 41GB で 0.04 秒、
    osm_japan 155 万件で 0.4 秒)。
    """
    print("extracting coordinates ...")
    conn.executescript(DOC_COORDS_DDL)
    conn.execute("DELETE FROM doc_coords")
    conn.execute(DOC_COORDS_POPULATE_SQL)
    conn.commit()
    (coords,) = conn.execute("SELECT COUNT(*) FROM doc_coords").fetchone()
    print(f"  {coords:,} coordinates")


def main() -> None:
    # 進捗は 1 行ずつすぐ出す。既定だと標準出力が端末以外(nohup のリダイレクト等)では
    # ブロックバッファになり、数十分かかる処理の途中経過が溜まったまま出てこない。
    # このスクリプトは「動いているのか固まったのか」を見せるのが目的なので必ず流す。
    sys.stdout.reconfigure(line_buffering=True)
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
