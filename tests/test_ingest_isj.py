"""街区レベル位置参照情報(`address_jp`)。

本物の ZIP は取りに行かない —— 同じ形(Shift_JIS の CSV が 1 本入った ZIP)を手元で作る。
押さえているのは、**引き当て側(`app/resolve.py`)と同じ鍵を作ること**(別のイメージなので
互いを import できず、形が食い違うと住所から引けなくなる)。
"""
from __future__ import annotations

import zipfile

import pytest

from app import resolve
from sources.isj import IsjAdapter, block_key, block_keys

HEADER = (
    '"都道府県名","市区町村名","大字・丁目名","小字・通称名","街区符号・地番","座標系番号",'
    '"Ｘ座標","Ｙ座標","緯度","経度","住居表示フラグ","代表フラグ","更新前履歴フラグ","更新後履歴フラグ"'
)


def _row(city, town, koaza, block, lat, lon, residential="1", representative="1"):
    cells = ["東京都", city, town, koaza, block, "9", "0", "0", lat, lon, residential, representative, "0", "0"]
    return ",".join(f'"{c}"' for c in cells)


def _zip(tmp_path, rows: list[str]):
    path = tmp_path / "address_jp-24.0a-13.zip"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("13_2025.csv", ("\r\n".join([HEADER, *rows]) + "\r\n").encode("cp932"))
    return path


@pytest.fixture
def adapter():
    return IsjAdapter()


def test_one_block_is_one_doc_and_only_representatives(adapter, tmp_path):
    path = _zip(tmp_path, [
        _row("千代田区", "丸の内一丁目", "", "9", "35.681252", "139.767235"),
        # 同じ街区の代表でない行は採らない
        _row("千代田区", "丸の内一丁目", "", "9", "35.681300", "139.767300", representative="0"),
        _row("新宿区", "早稲田鶴巻町", "", "533", "35.707876", "139.722314", residential="0"),
    ])

    docs = list(adapter.iter_docs(path))

    assert [d.title for d in docs] == ["東京都千代田区丸の内一丁目9", "東京都新宿区早稲田鶴巻町533"]
    first = docs[0]
    assert (first.extra["lat"], first.extra["lon"]) == (35.681252, 139.767235)
    assert first.extra["version"] == "24.0a"
    assert first.extra["residential"] is True
    assert "東京都千代田区丸の内-1-9" in first.aliases
    assert docs[1].extra["residential"] is False
    assert adapter.sample_titles == ["東京都千代田区丸の内一丁目9"]


def test_keys_drop_koaza_oaza_and_gun(adapter):
    keys = block_keys("東京都", "西多摩郡瑞穂町", "大字箱根ケ崎", "字見本", "12")
    assert "東京都西多摩郡瑞穂町大字箱根ケ崎字見本-12" in keys
    assert "東京都瑞穂町箱根ケ崎-12" in keys
    assert "東京都瑞穂町箱根ケ崎見本-12" in keys


@pytest.mark.parametrize("address", [
    "東京都千代田区丸の内一丁目9番1号",
    "〒100-0005 東京都千代田区丸の内1-9-1",
    "東京都千代田区丸の内１－９－１ 見本ビル2F",
    "東京都新宿区早稲田鶴巻町533",
    "神奈川県横浜市中区野毛町２―８１",
    "東京都台東区浅草1丁目31-12",
    "東京都千代田区神田神保町1の39の8",
    "東京都西多摩郡瑞穂町大字箱根ヶ崎12番地",
])
def test_ingest_and_resolve_make_the_same_key(address):
    """取り込みと引き当てで、同じ住所から同じ鍵ができる(片方だけ直すと引けなくなる)。"""
    assert block_key(address) == resolve.address_block_key(address)


def test_keys_read_common_address_forms():
    assert block_key("東京都千代田区丸の内一丁目9番1号") == "東京都千代田区丸の内-1-9-1"
    assert block_key("神奈川県横浜市中区野毛町２―８１") == "神奈川県横浜市中区野毛町-2-81"
    assert block_key("東京都西多摩郡瑞穂町大字箱根ヶ崎12番地") == "東京都西多摩郡瑞穂町大字箱根ケ崎-12"
