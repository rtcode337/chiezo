"""脇書きの値の書式 —— 決まった形のものだけ通す(`app/formats.py`)。

AI は書式を守ったつもりで崩す(「11月〜3月」「冬季」「11時〜14時」)。崩れた値が
読む側まで届くと、読めなかった 1 件は黙って「通年」や「営業時間なし」に化ける。
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app import formats


class TestMonths:
    @pytest.mark.parametrize("raw, want", [
        ("11-3", "11-3"),
        ("6,7,8", "6,7,8"),
        ("６〜８月、12月", "6-8,12"),
        ("11月～3月", "11-3"),
        (" 7 ", "7"),
        ("5-5", "5"),
    ])
    def test_readable_values_are_normalized(self, raw, want):
        assert formats.months(raw) == (want, "")

    @pytest.mark.parametrize("raw", ["", "通年", None])
    def test_all_year_means_no_value(self, raw):
        value, why = formats.check("months", raw)
        assert value is None and why == ""

    @pytest.mark.parametrize("raw", ["冬季", "13", "0-3", "11-", "春から夏", True])
    def test_broken_values_are_dropped_with_a_reason(self, raw):
        value, why = formats.months(raw)
        assert value is None and why

    def test_ranges_wrap_around_the_year(self):
        assert formats.month_set("11-3") == {11, 12, 1, 2, 3}
        assert formats.month_set("6-8,12") == {6, 7, 8, 12}


class TestOpeningHours:
    @pytest.mark.parametrize("raw", [
        "Mo-Fr 11:00-14:00,17:00-22:00; Sa,Su 11:00-21:00",
        "24/7",
        "Mo,Tu,Th-Su,PH 10:00-16:00; We off",
        "Tu-Su 18:00-26:00",
        'Mo-Sa 11:00-15:00 "売り切れ次第終了"',
        "Dec 31 off; Jan 01-03 off",
        "sunrise-sunset",
    ])
    def test_osm_values_pass(self, raw):
        assert formats.opening_hours(raw) == (raw, "")

    @pytest.mark.parametrize("raw", [
        "11時〜14時", "月〜金 11:00-14:00", "Mo-Fr 25:61-26:00", "open daily from noon",
        "--", 11,
    ])
    def test_other_values_are_dropped(self, raw):
        value, why = formats.opening_hours(raw)
        assert value is None and why


class TestSpec:
    def test_normalize(self):
        assert formats.normalize({"available_months": "months"}) == {"available_months": "months"}
        assert formats.normalize(None) == {}

    @pytest.mark.parametrize("raw", [["months"], {"a": "dates"}, {"": "months"}])
    def test_unreadable_specs_are_refused(self, raw):
        with pytest.raises(HTTPException):
            formats.normalize(raw)


SPEC = {"available_months": "months", "opening_hours": "opening_hours"}


class TestApply:
    def test_broken_values_are_dropped_and_reported(self):
        items, dropped = formats.apply(SPEC, [{
            "title": "料理",
            "extra": {"available_months": "冬", "opening_hours": "Mo 10:00-12:00"},
            "_facts": {"opening_hours": "10時から"},
        }], "_facts")
        assert items[0]["extra"] == {"opening_hours": "Mo 10:00-12:00"}
        assert items[0]["_facts"] == {}
        assert [(d["key"], d["from"]) for d in dropped] == [
            ("available_months", "AI"), ("opening_hours", "辞典"),
        ]

    def test_all_year_removes_the_key(self):
        """「通年」は値を消す印(null)になる(前に入っていた時期を外せるように)。"""
        items, dropped = formats.apply(SPEC, [{
            "title": "料理", "extra": {"available_months": "通年"},
        }], "_facts")
        assert items[0]["extra"] == {"available_months": None}
        assert dropped == []
