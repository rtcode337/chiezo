"""Overture Places の取り込み(`ingest/sources/overture.py`)のうち、S3 を叩かない部分。

見るのは 2 つ。

**リリース探し**。ここが 0 件を返すと `SystemExit` になり、取り込みは 1 行も
ログを残さずに終わる —— しかも本文の抽出まで進まないので、外からは
「preflight で止まっている」ようにしか見えない。実際にそうなった。

**国の絞り込み**。矩形は国境に沿わないので、日本の枠には韓国が丸ごと入る。
条件が落ちたことは取り込みの成否からは分からない(件数が増えるだけ)ので、
S3 へ投げる文そのものを見る。
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

    def close(self) -> None:
        pass


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


class TestCountry:
    """**入れる範囲は国で決める。** 矩形は S3 から読む量を抑えるための枠でしかない。"""

    def test_the_sql_asks_for_one_country(self, adapter, tmp_path):
        conn = _FakeConn(_files(RELEASES))
        adapter._connect = lambda: conn

        adapter.fetch(tmp_path)

        assert "addresses[1].country = 'JP'" in conn.sql[-1]

    def test_the_country_goes_to_s3_with_the_box(self, adapter, tmp_path):
        # **落としてから捨てない。** 要らない国のぶんを転送すると、
        # 日本の枠では 1 割を超える量が無駄になる
        conn = _FakeConn(_files(RELEASES))
        adapter._connect = lambda: conn
        adapter.fetch(tmp_path)
        sql = conn.sql[-1]

        assert sql.index("bbox.xmin") < sql.index("addresses[1].country")
        assert "COPY" in sql

    def test_without_a_country_nothing_is_filtered(self, tmp_path):
        # 国を書かないアダプタ(将来の別の国や、国の付かない母集団)では条件を足さない
        plain = overture.OvertureAdapter(
            "overture_test", lang=None, bbox=(0.0, 0.0, 1.0, 1.0), min_docs=1
        )
        conn = _FakeConn(_files(RELEASES))
        plain._connect = lambda: conn

        plain.fetch(tmp_path)

        assert "addresses[1].country" not in conn.sql[-1]

    @pytest.mark.parametrize("bad", ["Japan", "J", "", "12"])
    def test_a_code_that_is_not_two_letters_is_refused(self, bad):
        # SQL に直に埋めるので、書き間違いを黙って通すと条件が効かないまま全部入る
        with pytest.raises(SystemExit):
            overture.OvertureAdapter(
                "overture_test", lang=None, bbox=(0.0, 0.0, 1.0, 1.0), country=bad, min_docs=1
            )


class TestReuse:
    """**抜き方が変わったら、前の回のファイルは拾わない。**"""

    def test_the_name_carries_the_terms(self, adapter, tmp_path):
        conn = _FakeConn(_files(RELEASES))
        adapter._connect = lambda: conn

        path, date = adapter.fetch(tmp_path)

        assert date == "20260819"
        assert path.name.startswith("overture_japan-20260819-")

    def test_a_file_from_other_terms_is_not_reused(self, adapter, tmp_path):
        # 取り込みは何事もなく終わるのに中身が変わらない、がいちばん気づきにくい
        # (件数が 1 件も減らず、効いていないのか書き間違えたのかが外から分からない)
        stale = tmp_path / "overture_japan-20260819.parquet"
        stale.write_bytes(b"old")
        conn = _FakeConn(_files(RELEASES))
        adapter._connect = lambda: conn

        path, _date = adapter.fetch(tmp_path)

        assert path != stale
        assert "COPY" in conn.sql[-1]

    def test_the_same_terms_are_reused(self, adapter, tmp_path):
        conn = _FakeConn(_files(RELEASES))
        adapter._connect = lambda: conn
        first, _ = adapter.fetch(tmp_path)
        first.write_bytes(b"done")

        again, _ = adapter.fetch(tmp_path)

        assert again == first
        # 2 度目は S3 へ抜きに行かない(最後の文はリリース探しのまま)
        assert "COPY" not in conn.sql[-1]
