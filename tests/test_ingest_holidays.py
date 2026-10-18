"""内閣府の祝日一覧(`holidays_jp`)。

本物の CSV は取りに行かない —— 同じ形(Shift_JIS・1 行目が見出し)の小さな CSV を手元で作る。
"""
from __future__ import annotations

import sqlite3

import main as ingest_main
import pytest

from sources.holidays import HolidaysAdapter, parse_line

HEADER = "国民の祝日・休日月日,国民の祝日・休日名称"


def _csv(tmp_path, lines: list[str]):
    path = tmp_path / "holidays_jp-20260202.csv"
    path.write_bytes(("\r\n".join([HEADER, *lines]) + "\r\n").encode("cp932"))
    return path


@pytest.fixture
def adapter():
    return HolidaysAdapter()


def test_one_day_is_one_doc_with_date_tags_and_extra(adapter, tmp_path):
    path = _csv(tmp_path, ["2026/9/21,敬老の日", "2026/9/22,休日", "2027/1/1,元日"])

    docs = list(adapter.iter_docs(path))

    assert [d.title for d in docs] == ["2026-09-21 敬老の日", "2026-09-22 休日", "2027-01-01 元日"]
    first = docs[0]
    assert first.extra == {"date": "2026-09-21", "name": "敬老の日"}
    assert first.tags == ["祝日", "2026", "2026-09"]
    assert "2026-09-21" in first.aliases
    assert first.doc_id == 20260921


def test_unreadable_lines_and_duplicates_are_skipped(adapter, tmp_path):
    path = _csv(tmp_path, ["2026/9/21,敬老の日", "2026/9/21,敬老の日", "", "壊れた行", "2026/13/1,ない日"])

    assert [d.title for d in adapter.iter_docs(path)] == ["2026-09-21 敬老の日"]


def test_parse_line():
    assert parse_line("2026/5/6,休日") is not None
    assert parse_line("2026/5/6,") is None
    assert parse_line("nope") is None


def test_it_builds_and_validates(adapter, tmp_path):
    # 検証は 500 件以上と、1955 年の元日が引けることを求める
    lines = [f"{y}/1/1,元日" for y in range(1955, 2028)]
    lines += [f"{y}/{m}/15,テストの日" for y in range(1955, 2028) for m in range(2, 12)]
    path = _csv(tmp_path, lines)
    building = tmp_path / "holidays_jp-20260202.db.building"

    ingest_main.build_db(adapter, path, "20260202", building)
    ingest_main.validate_db(adapter, building)

    conn = sqlite3.connect(building)
    try:
        assert conn.execute("SELECT COUNT(*) FROM docs").fetchone()[0] == len(lines)
        tagged = conn.execute(
            "SELECT COUNT(*) FROM doc_tags WHERE tag = '2026-01'"
        ).fetchone()[0]
        assert tagged == 1
    finally:
        conn.close()
