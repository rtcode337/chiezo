"""巨大な「ID → 値」対応表をメモリではなくディスク(一時 SQLite)に持つための小道具。

取り込み中に参照するだけの対応表(ページビュー、wikidata の Q 番号など)を素朴に
`dict` へ貯めると、ja Wikipedia 規模では次の実測どおり数百 MB 単位を常駐で食う:

  - page_props(wikidata)  186 万件 → 約 270MiB
  - pageview_complete      数百万件 → 同等以上

これらは XML 本体のストリーミング解析(mwparserfromhell の一時オブジェクトを含む)と
同時に生きているため、メモリ 8GB 級のホストでは他のコンテナごと OOM killer に巻き込まれる
(実際に host 全体が落ちた)。一方でアクセスパターンは「取り込みループから ID 1 件ずつの
点引き」しかなく、範囲検索も反復も要らない。ディスクに置いて索引で引けば十分で、
常駐メモリは SQLite のページキャッシュ上限(既定 32MiB)だけに固定できる。

ディスクは潤沢(数百 GB)でメモリが希少、という Chiezo の運用環境に合わせた交換である。
"""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger(__name__)

# SQLite のページキャッシュ上限(KiB 単位の負値 = バイト指定)。ここが常駐メモリの上限になる。
CACHE_SIZE_KIB = 32_000

# INSERT のまとめ書き単位(この件数ぶんだけ一時的にメモリへ載る)
BATCH_SIZE = 50_000


class DiskLookup:
    """ID → 値の対応表。値の型は問わない(SQLite の動的型付けに委ねる)。

    `accumulate=True` にすると同じ ID への追加を合算する(ページビューのように
    1 ページが access_method ごとに複数行へ分かれているダンプ用)。
    """

    def __init__(self, path: Path, *, accumulate: bool = False):
        self.path = path
        self.accumulate = accumulate
        self.path.unlink(missing_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(
            "PRAGMA journal_mode=OFF;"
            "PRAGMA synchronous=OFF;"
            f"PRAGMA cache_size=-{CACHE_SIZE_KIB};"
            "CREATE TABLE kv (k INTEGER PRIMARY KEY, v) WITHOUT ROWID;"
        )
        self._pending: list[tuple[int, Any]] = []
        self._count = 0

    # ---- 書き込み ---------------------------------------------------------

    def add(self, key: int, value: Any) -> None:
        self._pending.append((key, value))
        if len(self._pending) >= BATCH_SIZE:
            self._flush()

    def extend(self, pairs: Iterable[tuple[int, Any]]) -> None:
        for key, value in pairs:
            self.add(key, value)

    def _flush(self) -> None:
        if not self._pending:
            return
        conflict = (
            "ON CONFLICT(k) DO UPDATE SET v = v + excluded.v"
            if self.accumulate
            else "ON CONFLICT(k) DO UPDATE SET v = excluded.v"
        )
        self._conn.executemany(f"INSERT INTO kv (k, v) VALUES (?, ?) {conflict}", self._pending)
        self._count += len(self._pending)
        self._pending.clear()

    def finish(self) -> "DiskLookup":
        """書き込みを確定する。以降は get() のみ。"""
        self._flush()
        self._conn.commit()
        return self

    # ---- 参照 -------------------------------------------------------------

    def get(self, key: int) -> Any:
        row = self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,)).fetchone()
        return row[0] if row else None

    def __len__(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(*) FROM kv").fetchone()
        return n

    # ---- 後始末 -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "DiskLookup":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class DiskMultiMap:
    """1 つのキーに複数の値がぶら下がる対応表(リダイレクト元タイトルの一覧など)。

    こちらはキーが文字列で値も文字列のため、`dict[str, list[str]]` で持つと
    ja Wikipedia のリダイレクト 160 万件で GB 級になる(文字列オブジェクトと
    リストのオーバーヘッドが件数ぶん乗る)。DiskLookup と同じくディスクへ逃がす。

    索引は一括投入が終わってから張る(投入しながら維持するより大幅に速い)。
    """

    def __init__(self, path: Path):
        self.path = path
        self.path.unlink(missing_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(
            "PRAGMA journal_mode=OFF;"
            "PRAGMA synchronous=OFF;"
            f"PRAGMA cache_size=-{CACHE_SIZE_KIB};"
            "CREATE TABLE kv (k TEXT, v TEXT);"
        )
        self._pending: list[tuple[str, str]] = []

    def add(self, key: str, value: str) -> None:
        self._pending.append((key, value))
        if len(self._pending) >= BATCH_SIZE:
            self._flush()

    def _flush(self) -> None:
        if self._pending:
            self._conn.executemany("INSERT INTO kv (k, v) VALUES (?, ?)", self._pending)
            self._pending.clear()

    def finish(self) -> "DiskMultiMap":
        self._flush()
        self._conn.execute("CREATE INDEX idx_kv_k ON kv(k)")
        self._conn.commit()
        return self

    def get(self, key: str) -> list[str]:
        return [r[0] for r in self._conn.execute("SELECT v FROM kv WHERE k = ?", (key,))]

    def __len__(self) -> int:
        (n,) = self._conn.execute("SELECT COUNT(DISTINCT k) FROM kv").fetchone()
        return n

    def close(self) -> None:
        self._conn.close()
        self.path.unlink(missing_ok=True)

    def __enter__(self) -> "DiskMultiMap":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class EmptyLookup:
    """対応表が無い場合(ダンプ未取得・未対応 wiki)のヌルオブジェクト。"""

    def get(self, key: int) -> Any:
        return None

    def __len__(self) -> int:
        return 0

    def close(self) -> None:
        pass


EMPTY = EmptyLookup()
