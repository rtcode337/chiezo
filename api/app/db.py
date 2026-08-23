"""SQLite 接続管理とクエリ実行(設計書 §5.2)。

- 接続はソースごと・スレッドごとに読み取り専用 (immutable=1) で開く。
- ただし追記されうる DB(notes)だけは `mode=ro` で開く(下の `set_mutable_paths`)。
- 全クエリに 5 秒のタイムアウト(progress handler で打ち切り)。超過は QueryTimeout。
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
from pathlib import Path

QUERY_TIMEOUT_SECONDS = 5.0
_PROGRESS_STEP = 50_000  # この命令数ごとにタイムアウト判定

_local = threading.local()


class QueryTimeout(Exception):
    """クエリがタイムアウトした(HTTP 504 に対応)。"""


# 追記されうる DB のパス。ソース走査のたびに main.refresh_sources が入れ替える。
_mutable_paths: set[str] = set()


def set_mutable_paths(paths) -> None:
    """`immutable=1` で開いてはいけない DB を登録する(実体は notes だけ)。

    `immutable=1` は「このファイルは開いている間 1 バイトも変わらない」という宣言で、
    SQLite はそれを信じてロックも WAL の確認も一切しない。書き込みが起きる DB をこれで
    開くと、読み手が中途半端なページを掴んで壊れた結果や例外を返す。notes は追記される
    ので、そこだけ通常の読み取り専用(`mode=ro`)に落とす。
    巨大な `/data` 側は今までどおり immutable のまま(42GB を毎回ロックさせない)。
    """
    global _mutable_paths
    changed = {str(p) for p in paths}
    _mutable_paths = changed


def is_mutable(db_path: Path) -> bool:
    return str(db_path) in _mutable_paths


def get_connection(db_path: Path) -> sqlite3.Connection:
    """スレッドローカルに読み取り専用接続をキャッシュして返す。

    キャッシュにはリンク先の実体 (st_dev, st_ino) と開き方(immutable かどうか)を添えて
    持ち、呼び出しごとに現在の状態と突き合わせる。ブルーグリーン切り替えでシンボリック
    リンクが別の世代ファイルへ差し替わったら、古い実体への接続を閉じて開き直す
    (immutable 接続は開いた時点のファイルを掴み続けるため、これをしないと再起動まで
    旧世代を読み続ける)。開き方が変わったときも同様に開き直す(notes の有効化)。
    stat 1 回の上乗せはクエリ本体に比べて無視できる。
    """
    conns: dict[str, tuple[sqlite3.Connection, tuple[int, int] | None, bool]] = (
        getattr(_local, "conns", None) or {}
    )
    if not hasattr(_local, "conns"):
        _local.conns = conns
    key = str(db_path)
    try:
        st = os.stat(db_path)  # シンボリックリンクは辿って実体を見る
        ident = (st.st_dev, st.st_ino)
    except OSError:
        ident = None
    mutable = is_mutable(db_path)
    cached = conns.get(key)
    if cached is not None and (cached[1] != ident or cached[2] != mutable):
        cached[0].close()
        del conns[key]
        cached = None
    if cached is None:
        # 追記される DB は immutable にできない(上の set_mutable_paths 参照)
        uri = f"file:{db_path}?mode=ro" if mutable else f"file:{db_path}?immutable=1"
        conn = sqlite3.connect(uri, uri=True)
        conn.row_factory = sqlite3.Row
        # title の前方一致 (LIKE 'prefix%') を idx_docs_title の範囲検索へ最適化するため。
        # SQLite は case_sensitive_like=OFF(既定)+ BINARY インデックスだと LIKE 前方一致を
        # 範囲検索に落とせず全走査になる(百万件規模でタイムアウト)。ON にすると BINARY
        # インデックスで範囲検索が効く。副作用として LIKE の ASCII 大小同一視は無効になるが、
        # 用途は titles / search フォールバック等の前方一致のみで実害はない。
        conn.execute("PRAGMA case_sensitive_like=ON")
        conns[key] = (conn, ident, mutable)
    return conns[key][0]


def close_thread_connections() -> None:
    conns = getattr(_local, "conns", None)
    if conns:
        for entry in conns.values():
            entry[0].close()
        conns.clear()


def query(
    db_path: Path,
    sql: str,
    params: tuple | dict = (),
    timeout: float = QUERY_TIMEOUT_SECONDS,
) -> list[sqlite3.Row]:
    conn = get_connection(db_path)
    deadline = time.monotonic() + timeout

    def _check() -> int:
        return 1 if time.monotonic() > deadline else 0

    conn.set_progress_handler(_check, _PROGRESS_STEP)
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as e:
        if "interrupted" in str(e):
            raise QueryTimeout() from e
        raise
    finally:
        conn.set_progress_handler(None, 0)
