"""SQLite 接続管理とクエリ実行(設計書 §5.2)。

- 接続はソースごと・スレッドごとに読み取り専用 (immutable=1) で開く。
- 全クエリに 5 秒のタイムアウト(progress handler で打ち切り)。超過は QueryTimeout。
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

QUERY_TIMEOUT_SECONDS = 5.0
_PROGRESS_STEP = 50_000  # この命令数ごとにタイムアウト判定

_local = threading.local()


class QueryTimeout(Exception):
    """クエリがタイムアウトした(HTTP 504 に対応)。"""


def get_connection(db_path: Path) -> sqlite3.Connection:
    """スレッドローカルに immutable 接続をキャッシュして返す。"""
    conns: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    if not hasattr(_local, "conns"):
        _local.conns = conns
    key = str(db_path)
    conn = conns.get(key)
    if conn is None:
        conn = sqlite3.connect(f"file:{db_path}?immutable=1", uri=True)
        conn.row_factory = sqlite3.Row
        # title の前方一致 (LIKE 'prefix%') を idx_docs_title の範囲検索へ最適化するため。
        # SQLite は case_sensitive_like=OFF(既定)+ BINARY インデックスだと LIKE 前方一致を
        # 範囲検索に落とせず全走査になる(百万件規模でタイムアウト)。ON にすると BINARY
        # インデックスで範囲検索が効く。副作用として LIKE の ASCII 大小同一視は無効になるが、
        # 用途は titles / search フォールバック等の前方一致のみで実害はない。
        conn.execute("PRAGMA case_sensitive_like=ON")
        conns[key] = conn
    return conn


def close_thread_connections() -> None:
    conns = getattr(_local, "conns", None)
    if conns:
        for conn in conns.values():
            conn.close()
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
