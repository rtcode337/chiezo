"""Overture Places の取り込み(`ingest/sources/overture.py`)のうち、S3 を叩かない部分。

**リリース探しだけを見る**。ここが 0 件を返すと `SystemExit` になり、取り込みは
1 行もログを残さずに終わる —— しかも本文の抽出まで進まないので、外からは
「preflight で止まっている」ようにしか見えない。実際にそうなった。
"""
from __future__ import annotations

import pytest

from sources import overture

RELEASES = ["2026-07-22.0", "2026-08-19.0"]


class _FakeConn:
    """`glob(...)` の応答だけを差し替える DuckDB の代わり。"""

    def __init__(self, files: list[str]):
        self.files = files
        self.sql: list[str] = []

    def execute(self, sql: str):
        self.sql.append(sql)
        self.rows = self._rows(sql)
        return self

    def _rows(self, sql: str) -> list[tuple]:
        import re

        # 本物と同じ形で答える(DISTINCT + 降順のリリース名)
        if "regexp_extract" in sql:
            found = {m for f in self.files
                     if (m := (re.search(r"release/([^/]+)/", f) or [None, ""])[1])}
            return [(r,) for r in sorted(found, reverse=True)]
        return [(f,) for f in self.files]

    def fetchall(self) -> list[tuple]:
        return self.rows


def _files(releases: list[str]) -> list[str]:
    return [f"{overture.S3_BASE}/{r}/theme=places/type=place/part-0000{i}.parquet"
            for i, r in enumerate(releases)]


@pytest.fixture
def adapter():
    """本番と同じ組み立て(`ADAPTERS["overture_japan"]` が作るもの)。"""
    return overture.overture_japan()


class TestLatestRelease:
    def test_it_picks_the_newest(self, adapter):
        conn = _FakeConn(_files(RELEASES))
        assert adapter._latest_release(conn) == "2026-08-19.0"

    def test_it_walks_down_to_the_files(self, adapter):
        """**`release/*` では引けない**(エラーにならず 0 件が返る)。

        S3 にディレクトリという実体は無いので、glob の `*` は「その下にある
        オブジェクト」しか返さない —— リリース名はプレフィックスの一部でしかない。
        `**` で葉まで辿ってパスから切り出す必要がある。
        """
        conn = _FakeConn(_files(RELEASES))
        adapter._latest_release(conn)
        (sql,) = conn.sql
        assert "/**" in sql
        assert "regexp_extract" in sql

    def test_nothing_that_looks_like_a_release_is_a_dead_end(self, adapter):
        """**黙って進ませない** —— 空のまま抽出へ行くと理由の分からない失敗になる。"""
        with pytest.raises(SystemExit) as got:
            adapter._latest_release(_FakeConn([]))
        assert "リリースが見つかりません" in str(got.value)

    def test_other_entries_are_ignored(self, adapter):
        """将来 release/ の下に別のものが並んでも拾わない。"""
        conn = _FakeConn([*_files(RELEASES), f"{overture.S3_BASE}/index/README.md"])
        assert adapter._latest_release(conn) == "2026-08-19.0"
