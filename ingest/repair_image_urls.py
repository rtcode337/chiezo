#!/usr/bin/env python3
"""代表画像の URL に紛れ込んだ、ダンプの逃がし文字を取り除く。

`page_props` ダンプの中では、ファイル名の `'` は `\\'`、`"` は `\\"` と書かれている。
取り込みがそれを外さずに使っていたため、名前に `\\` が 1 文字混ざったままになり、
**置き場の枝もファイル名も両方ずれていた**(枝は名前の MD5 で決まるので、1 文字違えば
別の枝になる)。その URL は 404 を返す。

取り込み側は直したが、**焼いてしまった DB はそのまま**なので、ここで直す。
影響するのは名前に `'` か `"` を含む画像だけで、本文もタグも索引も無傷 ——
焼き直しは数時間かかるのに対し、これは数分で終わる。

直し方は取り込みと同じ規則を使う(`sources.wikipedia.commons_url`)。URL に
入っているのは逃がしたままの名前なので、URL からファイル名を取り出して
組み直せば、取り込みをやり直したのと同じ URL になる。

**取り込みのイメージに同梱する**(`ingest/Dockerfile`)。配信機に要るのは docker
だけで、リポジトリも compose も置かない運用なので、リポジトリの中に置くと
配信機からは走らせられない。組み立ての規則を取り込みと共有している都合もあり、
どのみちこのイメージの依存が要る。

使い方(配信機で):

    docker ps                               # 配信しているコンテナの名前を確かめる
    docker stop <配信のコンテナ>            # 読み取り中の DB を書き換えないため
    docker run --rm -v /path/to/chiezo-data:/data -u 10001:10001 \
      ghcr.io/rtcode337/chiezo-ingest:latest \
      python repair_image_urls.py /data/jawiki.db
    docker start <配信のコンテナ>

`/data` に当てるのは `jawiki.db` のあるディレクトリ。シンボリックリンクを渡して
よい(実体の世代ファイルを書き換える)。何度実行してもよい(直すものが無ければ
何もしない)。

**配信機は corpus を immutable で開く**(`app/db.py`)。開いたまま書き換えると、
読み手は「変わらない」前提のまま古いページを読み続ける —— 止めてから当てること。
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import urllib.parse
from pathlib import Path

# URL の組み立ては取り込み側と同じものを使う(コピーを持つと必ず食い違うため)。
from sources.wikipedia import commons_url

# 逃がしのバックスラッシュが残った URL だけが対象。エンコードされて `%5C` になっている
MARKER = "%5C"


def repaired(url: str) -> str | None:
    """その URL を組み直す。変わらなければ None。

    URL の末尾がファイル名(逃がしたまま)なので、戻してから組み直す。
    `commons_url` が逃がしを外して MD5 を取り直すので、枝も一緒に直る。
    """
    name = urllib.parse.unquote(url.rsplit("/", 1)[-1])
    fixed = commons_url(name)
    return fixed if fixed and fixed != url else None


def repair(db_path: Path) -> tuple[int, int]:
    """直した件数と、見に行った件数を返す。"""
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT doc_id, extra FROM docs WHERE instr(extra, ?) > 0", (MARKER,)
        ).fetchall()
        changes: list[tuple[str, int]] = []
        for doc_id, raw in rows:
            try:
                extra = json.loads(raw)
            except (TypeError, ValueError):
                continue
            url = extra.get("image")
            if not isinstance(url, str):
                continue
            if fixed := repaired(url):
                extra["image"] = fixed
                changes.append((json.dumps(extra, ensure_ascii=False), doc_id))
        conn.executemany("UPDATE docs SET extra = ? WHERE doc_id = ?", changes)
        conn.commit()
        return len(changes), len(rows)
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db", nargs="+", type=Path, help="対象の DB(リンクでもよい)")
    args = parser.parse_args()
    for db_path in args.db:
        if not db_path.exists():
            raise SystemExit(f"{db_path}: no such file")
        fixed, looked = repair(db_path)
        print(f"{db_path}: {looked} 件を見て {fixed} 件を直した")


if __name__ == "__main__":
    main()
