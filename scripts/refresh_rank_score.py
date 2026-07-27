#!/usr/bin/env python3
"""既存 DB の `rank_score` を、取り込み済みの `extra` から計算し直す。

`rank_score` は「0.0〜1.0 に正規化した知名度・重要度」という約束で、検索の並びで
bm25 に掛け合わせて使う。ところが古い DB は:

- `<lang>wiki` … `rank_score` が 0.0 固定(XML ダンプに人気度が無いため入れていなかった)。
  月間ページビューは `extra.pageviews_month` に入っているので、そこから計算できる
- `geonames` … `rank_score` に人口の生値(最大 3000 万)が入っている。`extra.population`
  から入れ直す。対数変換なので人口による大小関係は変わらない
- `osm_<国>` … 元から 0〜1 なので対象外(何もしない)

いずれも `extra` を読み直すだけで、ダンプの取り直しは要らない。スキーマは変わらない
ので `schema_version` も上げない(古い DB でも API は動く。API 側が rank_score を
0〜1 に丸めてから使うので、入れ直していない DB では実質 bm25 のみの並びに戻るだけ)。

使い方:

    docker compose stop chiezo-api          # 読み取り中の DB を書き換えないため
    python3 scripts/refresh_rank_score.py data/jawiki.db data/geonames.db
    docker compose start chiezo-api

何度実行しても結果は同じ(`extra` から毎回計算し直す)。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ingest"))
try:
    from core import (
        POPULARITY_LOG_MAX_PAGEVIEWS,
        POPULARITY_LOG_MAX_COUNTRY_POPULATION,
        normalized_popularity,
    )
except ImportError as e:  # pragma: no cover - 配置ミスの案内
    raise SystemExit(
        f"ingest/core.py を読めない({e})。chiezo のチェックアウト内から実行してください"
    ) from e

# source_kind → (extra のどのキーを人気度として使うか, 対数の上限)
POPULARITY_SOURCE = {
    "wikipedia": ("pageviews_month", POPULARITY_LOG_MAX_PAGEVIEWS),
    "geonames": ("population", POPULARITY_LOG_MAX_COUNTRY_POPULATION),
}

BATCH = 200_000


def refresh(db_path: Path) -> int:
    """rank_score を計算し直す。更新した行数を返す。"""
    target = db_path.resolve()  # シンボリックリンクなら実体の世代ファイルを触る
    conn = sqlite3.connect(target)
    try:
        row = conn.execute("SELECT source, source_kind FROM meta").fetchone()
        if row is None:
            raise SystemExit(f"{db_path}: meta table is empty (not a chiezo DB?)")
        source, kind = row
        if kind not in POPULARITY_SOURCE:
            print(f"{source}: source_kind={kind} は対象外(元から 0〜1)。何もしない")
            return 0
        key, log_max = POPULARITY_SOURCE[kind]

        print(f"{source}: extra.{key} から rank_score を計算し直す ({target})")
        started = time.monotonic()
        (total,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
        # 一度に全件読むとメモリに載らないので、doc_id で刻んで進める
        # (doc_id は等間隔でないため値ではなく件数で刻む。add_tag_index.py と同じ)
        last_id, done, changed = -(1 << 62), 0, 0
        while True:
            rows = conn.execute(
                "SELECT doc_id, extra, rank_score FROM docs WHERE doc_id > ?"
                " ORDER BY doc_id LIMIT ?",
                (last_id, BATCH),
            ).fetchall()
            if not rows:
                break
            updates = []
            for doc_id, extra, current in rows:
                value = None
                if extra:
                    try:
                        value = (json.loads(extra) or {}).get(key)
                    except ValueError:
                        value = None
                score = normalized_popularity(value, log_max)
                if score != current:
                    updates.append((score, doc_id))
            if updates:
                conn.executemany("UPDATE docs SET rank_score = ? WHERE doc_id = ?", updates)
                conn.commit()
            changed += len(updates)
            last_id = rows[-1][0]
            done += len(rows)
            print(f"  {done:,}/{total:,} docs ({changed:,} 更新, {time.monotonic()-started:.0f}s)")

        top = conn.execute(
            "SELECT title, rank_score FROM docs ORDER BY rank_score DESC LIMIT 5"
        ).fetchall()
        print("  上位:", ", ".join(f"{t}({s:.3f})" for t, s in top))
        print(f"done in {time.monotonic() - started:.0f}s. restart chiezo-api to pick it up.")
        return changed
    finally:
        conn.close()


def main() -> None:
    # 進捗は 1 行ずつすぐ出す。既定だと標準出力が端末以外(nohup のリダイレクト等)では
    # ブロックバッファになり、数十分かかる処理の途中経過が溜まったまま出てこない。
    # このスクリプトは「動いているのか固まったのか」を見せるのが目的なので必ず流す。
    sys.stdout.reconfigure(line_buffering=True)
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("db", type=Path, nargs="+", help="対象の <source>.db(複数可)")
    args = parser.parse_args()
    for db_path in args.db:
        if not db_path.exists():
            raise SystemExit(f"{db_path}: no such file")
        refresh(db_path)


if __name__ == "__main__":
    main()
