#!/usr/bin/env python3
"""trigram と形態素トークナイザの FTS を、同じ文書集合の上で作って比べる実験台。

chiezo は FTS5 の trigram を使っている。辞書が要らず何語でも引ける代わりに、
**3 文字未満のクエリが原理的に不可能**(タイトル前方一致に落ちる)で、索引も大きい。
形態素トークナイザ(SQLite の loadable extension)に替えると何がどう変わるかを、
本番 DB を触らずに測るためのスクリプト。

やること:
  1. 既存の <source>.db から部分コピー(狙いの記事 + 先頭からの干し草)を作る
  2. 同じ docs に docs_fts_trigram と docs_fts_morph を張る
  3. 同じ問い合わせを両方に投げ、ヒット数・目当ての記事の順位・所要時間・索引サイズを出す

拡張は同梱していないので、使う前に取ってくること(既定は sqlite-vaporetto の
配布バイナリ。ビルド不要で、-with-model ならモデルも同梱されている):

    curl -sL -O https://github.com/hotchpotch/sqlite-vaporetto/releases/download/\
v0.4.0/sqlite-vaporetto-v0.4.0-linux-x86_64-with-model.tar.gz
    tar xzf sqlite-vaporetto-*.tar.gz

使い方:
  FTS_EXT=<.so のパス> python scripts/fts_lab.py build <src.db> <lab.db> [件数]
  FTS_EXT=<.so のパス> python scripts/fts_lab.py compare <lab.db>

別のトークナイザを試すときは FTS_EXT / FTS_EXT_ENTRY / FTS_TOKENIZER を差し替える。
なお lindera-sqlite 2.0.0 は現行の SQLite では初期化に失敗する
(fts5_api の iVersion を 2 と決め打ちしており、SQLite 3.43 以降は 3 を返す)。
"""
from __future__ import annotations

import os
import sqlite3
import sys
import time

EXT = os.environ.get("FTS_EXT", "./vap/sqlite-vaporetto-v0.4.0-linux-x86_64-with-model/libsqlite_vaporetto.so")
ENTRY = os.environ.get("FTS_EXT_ENTRY", "sqlite3_vaporetto_init")
TOKENIZER = os.environ.get("FTS_TOKENIZER", "vaporetto")

# 実験で狙い撃ちする記事(2 文字クエリ・誤ヒットで実害が出たもの)
NEEDLES = [
    "一蘭", "味仙", "天下一品", "ラーメン二郎", "すみれ", "山頭火",
    "京都", "東京都", "京都市", "浅草寺", "犬", "柴犬", "夏目漱石", "富士山",
]

# (問い合わせ, 期待する記事) — 期待が None なら「誤ヒットしないこと」を見る
QUERIES = [
    ("一蘭", "一蘭"),
    ("味仙", "味仙"),
    ("京都", "京都"),
    ("犬", "犬"),
    ("浅草寺", "浅草寺"),
    ("ラーメン", None),
    ("夏目漱石", "夏目漱石"),
    ("天下一品", "天下一品"),
]


def connect(path: str, write: bool = False) -> sqlite3.Connection:
    conn = sqlite3.connect(path if write else f"file:{path}?mode=ro", uri=not write)
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    conn.load_extension(EXT, entrypoint=ENTRY)
    conn.enable_load_extension(False)
    return conn


def build(src_path: str, lab_path: str, n: int) -> None:
    if os.path.exists(lab_path):
        os.remove(lab_path)
    lab = connect(lab_path, write=True)
    lab.executescript(
        """
        PRAGMA journal_mode=OFF;
        PRAGMA synchronous=OFF;
        CREATE TABLE docs (doc_id INTEGER PRIMARY KEY, title TEXT, body TEXT);
        """
    )
    src = sqlite3.connect(f"file:{src_path}?immutable=1", uri=True)
    src.row_factory = sqlite3.Row

    rows = []
    for title in NEEDLES:
        r = src.execute("SELECT doc_id, title, body FROM docs WHERE title = ?", (title,)).fetchone()
        if r:
            rows.append((r["doc_id"], r["title"], r["body"]))
        else:
            print(f"  needle miss: {title}", file=sys.stderr)
    print(f"needles: {len(rows)}")

    seen = {r[0] for r in rows}
    # rowid 順の先頭から詰める(RANDOM() の全表走査は 150 万件では重すぎる)
    for r in src.execute("SELECT doc_id, title, body FROM docs LIMIT ?", (n,)):
        if r["doc_id"] not in seen:
            rows.append((r["doc_id"], r["title"], r["body"]))
    lab.executemany("INSERT INTO docs VALUES (?,?,?)", rows)
    lab.commit()
    src.close()

    chars = lab.execute("SELECT SUM(LENGTH(body)) FROM docs").fetchone()[0] or 0
    print(f"docs: {len(rows):,} / {chars:,} chars")

    for name, tokenizer in (("trigram", "trigram"), ("morph", TOKENIZER)):
        t = time.monotonic()
        lab.execute(
            f"CREATE VIRTUAL TABLE docs_fts_{name} USING fts5("
            f"title, body, content='docs', content_rowid='doc_id', tokenize='{tokenizer}')"
        )
        lab.execute(
            f"INSERT INTO docs_fts_{name}(rowid, title, body)"
            " SELECT doc_id, title, body FROM docs"
        )
        lab.execute(f"INSERT INTO docs_fts_{name}(docs_fts_{name}) VALUES('optimize')")
        lab.commit()
        size = lab.execute(
            f"SELECT SUM(pgsize) FROM dbstat WHERE name LIKE 'docs_fts_{name}%'"
        ).fetchone()[0] or 0
        print(f"  {name:8s}: 構築 {time.monotonic()-t:6.1f}s  索引 {size/1e6:8.1f} MB")
    lab.close()


def run_query(conn: sqlite3.Connection, table: str, q: str) -> tuple[int, list[str], float, str]:
    """(ヒット数, 上位 5 件のタイトル, 秒, エラー) を返す。"""
    phrase = '"' + q.replace('"', "") + '"'
    t = time.monotonic()
    try:
        rows = conn.execute(
            f"SELECT d.title FROM {table} JOIN docs d ON d.doc_id = {table}.rowid"
            f" WHERE {table} MATCH ? ORDER BY bm25({table}, 5.0, 1.0) LIMIT 5",
            (phrase,),
        ).fetchall()
        (total,) = conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {table} MATCH ?", (phrase,)
        ).fetchone()
    except sqlite3.OperationalError as e:
        return 0, [], time.monotonic() - t, str(e)
    return total, [r["title"] for r in rows], time.monotonic() - t, ""


def compare(lab_path: str) -> None:
    conn = connect(lab_path)
    (docs,) = conn.execute("SELECT COUNT(*) FROM docs").fetchone()
    print(f"corpus: {docs:,} docs\n")
    header = f"{'query':12s} {'':4s} {'hits':>8s} {'ms':>7s}  rank  top-5"
    for q, expect in QUERIES:
        print(f"--- {q}" + (f"  (期待: {expect})" if expect else "  (誤ヒットを見る)"))
        print(header)
        for name in ("trigram", "morph"):
            total, top, secs, err = run_query(conn, f"docs_fts_{name}", q)
            if err:
                print(f"{'':12s} {name[:4]:4s} {'-':>8s} {'-':>7s}  -     ERROR: {err[:60]}")
                continue
            rank = "-"
            if expect:
                rank = str(top.index(expect) + 1) if expect in top else "圏外"
            print(
                f"{'':12s} {name[:4]:4s} {total:>8,} {secs*1000:>7.1f}  {rank:5s} "
                + " / ".join(top[:5])
            )
        print()
    conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1]
    if cmd == "build":
        build(sys.argv[2], sys.argv[3], int(sys.argv[4]) if len(sys.argv) > 4 else 50_000)
    elif cmd == "compare":
        compare(sys.argv[2])
    else:
        raise SystemExit(__doc__)
