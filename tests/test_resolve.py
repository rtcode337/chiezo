"""引き当て —— AI が返した店の手がかりから、辞典の 1 件を機械で引き当てる(`app/resolve.py`)。

押さえているのは、**同じ名前の別の店舗を掴まない**こと。名前だけで辞典を引くと、
素の見出しが別の町の店だったり、住所が同じなのに座標が隣の県だったりする
(実データで起きた形を、架空の店で小さな辞典にして再現している)。
"""
from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi import HTTPException

from app import notes, resolve

NOW = "2026-10-17T00:00:00+00:00"


class Src:
    def __init__(self, path):
        self.path = path
        self.schema_version = notes.SCHEMA_VERSION


def make_source(tmp_path, name: str, docs: list[dict]) -> Src:
    """辞典の代わり(コアスキーマ)。1 件は `{"title", "extra", "aliases"}`。"""
    path = tmp_path / f"{name}.db"
    conn = sqlite3.connect(path)
    conn.executescript(notes.SCHEMA_DDL)
    for doc_id, doc in enumerate(docs, start=1):
        extra = doc.get("extra") or {}
        conn.execute(
            "INSERT INTO docs (doc_id, title, opening, body, tags, extra, links, updated_at,"
            " rank_score) VALUES (?, ?, '', '本文', '[]', ?, '[]', ?, 0.0)",
            (doc_id, doc["title"], json.dumps(extra, ensure_ascii=False), NOW),
        )
        for alias in doc.get("aliases") or []:
            conn.execute("INSERT INTO aliases (alias, doc_id) VALUES (?, ?)", (alias, doc_id))
        if "lat" in extra:
            conn.execute(
                "INSERT INTO doc_coords (lat, lon, doc_id) VALUES (?, ?, ?)",
                (extra["lat"], extra["lon"], doc_id),
            )
    conn.commit()
    conn.close()
    resolve._AREAS.clear()
    return Src(path)


# 実データ(2026-10 の地図辞典と店舗辞典)で起きた形を、架空の店と東京駅のあたりに
# 置き直したもの。本店は 1 か所で、店舗辞典には同じ名前が 3 件ある —— 素の見出しは
# 別の区の店、1 件は住所が本店と同じなのに座標が隣の県
HONTEN = (35.6810, 139.7660)


def osm_docs(extra_docs=()):
    return [
        {"title": "サンプル軒 総本店", "aliases": [], "extra": {
            "osm_type": "node", "osm_id": 1001, "feature": "amenity=restaurant",
            "lat": HONTEN[0], "lon": HONTEN[1], "area": "東京都",
            "opening_hours": "Mo,We-Su 11:00-20:00; Tu off",
        }},
        # 周りの地物(点の県を読むため)
        {"title": "東京駅", "extra": {"lat": 35.6812, "lon": 139.7671, "area": "東京都"}},
        {"title": "上野駅", "extra": {"lat": 35.7138, "lon": 139.7773, "area": "東京都"}},
        {"title": "横浜駅", "extra": {"lat": 35.4660, "lon": 139.6223, "area": "神奈川県"}},
        {"title": "大宮駅", "extra": {"lat": 35.9064, "lon": 139.6239, "area": "埼玉県"}},
        *extra_docs,
    ]


def overture_docs():
    return [
        {"title": "サンプル軒総本店 (2001)", "aliases": ["サンプル軒総本店"], "extra": {
            "overture_id": "ov-2001", "locality": "千代田区",
            "lat": 35.68101, "lon": 139.76598, "address": "丸の内1丁目9-1",
            "website": "http://sample.example.com/", "phone": "+81300000000",
        }},
        {"title": "サンプル軒総本店 (2002)", "aliases": ["サンプル軒総本店"], "extra": {
            "overture_id": "ov-2002", "locality": "横浜市西区",
            "lat": 35.46610, "lon": 139.62240, "address": "丸の内1-9-1",
        }},
        {"title": "サンプル軒総本店", "extra": {
            "overture_id": "ov-2003", "locality": "台東区",
            "lat": 35.71380, "lon": 139.77750,
        }},
    ]


@pytest.fixture
def sources(tmp_path):
    return {
        "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()),
        "overture_japan": make_source(tmp_path, "overture_japan", overture_docs()),
    }


SPEC = resolve.normalize({
    "clue": "shop",
    "sources": [
        {"source": "osm_japan", "identity": ["osm_type", "osm_id"], "tag": "出典:OSM"},
        {"source": "overture_japan", "identity": ["overture_id"], "tag": "出典:Overture"},
    ],
    "area_tag": "地域:",
    "to_extra": {"opening_hours": "opening_hours"},
    "failed_tag": "状態:代表店未定",
})


class TestPickingTheRightShop:
    def test_the_main_shop_is_found_and_not_its_namesakes(self, sources):
        """住所の市区町村で別の町の 2 件を落とし、2 つの辞典の点が重なる 1 か所を採る。"""
        result = resolve.resolve(
            SPEC, {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"},
            "東京都", sources, NOW,
        )
        assert result.ok
        # 座標は先に書いた辞典(地図辞典)のもの
        assert (result.lat, result.lon) == HONTEN
        shop = result.shop
        assert shop["name"] == "サンプル軒 総本店"
        assert shop["ref"] == "osm_japan:サンプル軒 総本店"
        assert shop["identity"] == [
            "osm_japan:osm_type=node;osm_id=1001",
            "overture_japan:overture_id=ov-2001",
        ]
        assert {"県", "市区町村", "番地", "辞典の一致"} <= set(shop["basis"])
        # 地図辞典に無い電話は店舗辞典から写す(出どころは両方)
        assert shop["phone"] == "+81300000000"
        assert shop["sources"] == ["osm_japan", "overture_japan"]

    def test_without_an_address_the_two_dictionaries_decide(self, sources):
        """住所が無くても、2 つの辞典が同じ所で重なる 1 か所は確か。素の見出し(別の町)は
        県が同じでも、それだけでは採らない。"""
        result = resolve.resolve(SPEC, {"name": "サンプル軒総本店"}, "東京都", sources, NOW)
        assert result.ok
        assert result.shop["ref"] == "osm_japan:サンプル軒 総本店"

    def test_namesakes_alone_are_not_taken(self, tmp_path):
        """本店が辞典に無ければ、同じ名前の別の店舗を掴まずに理由を返す。"""
        only_wrong = {
            "overture_japan": make_source(tmp_path, "overture_japan", overture_docs()[1:]),
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()[1:]),
        }
        result = resolve.resolve(
            SPEC, {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"},
            "東京都", only_wrong, NOW,
        )
        assert not result.ok
        assert "住所と合いません" in result.shop["unresolved"]
        assert any("横浜市西区" in c for c in result.shop["candidates"])

    def test_a_shop_outside_the_dish_area_is_read_from_its_address(self, sources):
        """代表の店は料理の県の外にあってよい。住所が書いてあれば、県はそちらで確かめる。"""
        result = resolve.resolve(
            SPEC, {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"},
            "大阪府", sources, NOW,
        )
        assert result.ok

    def test_wrong_area_without_address_is_refused(self, sources):
        result = resolve.resolve(SPEC, {"name": "サンプル軒総本店"}, "大阪府", sources, NOW)
        assert not result.ok

    def test_two_sure_places_are_not_decided(self, tmp_path):
        """同じ名前で、どちらも 2 つの辞典が重なる場所が 2 つあれば決めない。"""
        twin = {"title": "サンプル軒 総本店 (node:2)", "aliases": ["サンプル軒 総本店"], "extra": {
            "osm_type": "node", "osm_id": 2, "lat": 35.7138, "lon": 139.7775, "area": "東京都",
        }}
        srcs = {
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs([twin])),
            "overture_japan": make_source(tmp_path, "overture_japan", overture_docs()),
        }
        result = resolve.resolve(SPEC, {"name": "サンプル軒総本店"}, "東京都", srcs, NOW)
        assert not result.ok
        assert "2 か所" in result.shop["unresolved"]
        # 辞典の見出しを添えれば、そちらに決まる
        pinned = resolve.resolve(
            SPEC, {"name": "サンプル軒総本店", "ref": "osm_japan:サンプル軒 総本店 (node:2)"},
            "東京都", srcs, NOW,
        )
        assert pinned.ok and pinned.shop["ref"] == "osm_japan:サンプル軒 総本店 (node:2)"

    def test_a_single_namesake_in_the_area_is_taken_on_area_alone(self, tmp_path):
        """確かな束が無くても、名前の合う場所がその県にただ 1 つなら採る(根拠は県と名前)。"""
        srcs = {"osm_japan": make_source(tmp_path, "osm_japan", osm_docs())}
        result = resolve.resolve(SPEC, {"name": "サンプル軒総本店"}, "東京都", srcs, NOW)
        assert result.ok
        assert result.shop["basis"] == ["名前", "県"]

    def test_free_text_areas_do_not_become_the_prefecture(self, tmp_path):
        """店舗辞典の `area` の自由記述(「〒…」「東京都/日本」)を県として読まない。

        本番で、比べる形で空になる「〒343-0828」が長い順でいちばん先に選ばれ、
        どの住所にも県として当たったので、東京都の候補が全部「県が違う」で落ちた。
        候補自身の `area` が崩れていれば、周りの地物の県で読み直す。
        """
        noisy = overture_docs()
        noisy[0] = {**noisy[0], "extra": {**noisy[0]["extra"], "area": "東京都/日本"}}
        noisy += [
            {"title": "郵便番号だけの店", "extra": {"area": "〒343-0828"}},
            {"title": "崩れた県の店", "extra": {"area": "東京都/日本"}},
            # 県らしい値は件数が桁違いに多い(崩れた値は 1 桁)
            *({"title": f"都内の店 {i}", "extra": {"area": "東京都"}} for i in range(1200)),
        ]
        srcs = {
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()),
            "overture_japan": make_source(tmp_path, "overture_japan", noisy),
        }
        assert "〒343-0828" not in resolve.known_areas(srcs["overture_japan"])
        assert "東京都/日本" not in resolve.known_areas(srcs["overture_japan"])
        result = resolve.resolve(
            SPEC, {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"},
            "東京都", srcs, NOW,
        )
        assert result.ok, result.shop

    def test_decorations_and_branch_names_are_matched_with_the_address(self, tmp_path):
        """飾りの記号・伸ばす音・末尾の支店名が辞典と違っても、住所が合えば採る。"""
        overture = [{"title": "サンプルファーム", "extra": {
            "overture_id": "ov-3001", "locality": "千代田区",
            "lat": 35.6850, "lon": 139.7600, "address": "丸の内2-3-4",
        }}]
        srcs = {
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()),
            "overture_japan": make_source(tmp_path, "overture_japan", overture),
        }
        result = resolve.resolve(
            SPEC, {"name": "サンプルファ〜ム♪ 丸の内店", "address": "東京都千代田区丸の内2-3-4"},
            "東京都", srcs, NOW,
        )
        assert result.ok, result.shop
        # 住所が無ければ、寄せた名前だけでは採らない
        result = resolve.resolve(SPEC, {"name": "サンプルファ〜ム♪ 丸の内店"}, "東京都", srcs, NOW)
        assert not result.ok

    def test_a_romanized_locality_is_not_compared(self, tmp_path):
        """町の名前がローマ字の店は、市区町村を比べずに番地で確かめる。"""
        overture = [{"title": "サンプル漬本舗", "extra": {
            "overture_id": "ov-3003", "locality": "Chiyoda-ku",
            "lat": 35.6850, "lon": 139.7600, "address": "丸の内2-3-4",
        }}]
        srcs = {
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()),
            "overture_japan": make_source(tmp_path, "overture_japan", overture),
        }
        result = resolve.resolve(
            SPEC, {"name": "サンプル漬本舗", "address": "東京都千代田区丸の内2-3-4"},
            "東京都", srcs, NOW,
        )
        assert result.ok, result.shop

    def _pointed(self, tmp_path, osm_title: str):
        """住所を持たない地図辞典の 1 点だけがある辞典(直売所や小さな店に多い形)。"""
        extra_osm = [{"title": osm_title, "extra": {
            "osm_type": "node", "osm_id": 3001, "feature": "shop=farm",
            "lat": 35.6850, "lon": 139.7600, "area": "東京都",
        }}]
        return {
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs(extra_osm)),
            "overture_japan": make_source(tmp_path, "overture_japan", []),
        }

    def test_a_pointed_entry_with_a_longer_formal_name_is_taken(self, tmp_path):
        """AI が辞典を見て指した見出しは、運営の名前が頭に付いた正式名でも採る(根拠に残す)。"""
        srcs = self._pointed(tmp_path, "サンプルの郷")
        result = resolve.resolve(SPEC, {
            "name": "JAサンプル サンプルの郷", "address": "東京都千代田区丸の内2-3-4",
            "ref": "osm_japan:サンプルの郷",
        }, "東京都", srcs, NOW)
        assert result.ok, result.shop
        assert resolve.BASIS_REF in result.shop["basis"]

    def test_names_alike(self):
        assert resolve._named_alike("見本", "見本製菓")
        assert resolve._named_alike("見本農協ファーマーズマーケット見本柑", "JA見本 ファーマーズマーケット 見本柑")
        assert not resolve._named_alike("見本菓舗", "見本 本店")

    def test_a_pointed_entry_with_little_in_common_is_not_taken(self, tmp_path):
        """重なりが 2 字だけの名前は、指していても採らない(別の店を掴みうる)。"""
        srcs = self._pointed(tmp_path, "見本菓舗")
        result = resolve.resolve(SPEC, {
            "name": "見本 本店", "address": "東京都千代田区丸の内2-3-4", "ref": "osm_japan:見本菓舗",
        }, "東京都", srcs, NOW)
        assert not result.ok

    def test_a_similar_name_without_pointing_is_not_taken(self, tmp_path):
        """指していなければ今までどおり(名前が似ているだけの点は採らない)。"""
        srcs = self._pointed(tmp_path, "サンプルの郷")
        result = resolve.resolve(SPEC, {
            "name": "JAサンプル サンプルの郷", "address": "東京都千代田区丸の内2-3-4",
        }, "東京都", srcs, NOW)
        assert not result.ok

    def test_a_main_shop_name_finds_the_branch_spelling(self, tmp_path):
        """「◯◯本店」と書いた店が、辞典では「◯◯店」の見出しのとき。"""
        overture = [{"title": "サンプル麺 丸の内店", "extra": {
            "overture_id": "ov-3002", "locality": "千代田区",
            "lat": 35.6850, "lon": 139.7600, "address": "丸の内2-3-4",
        }}]
        srcs = {
            "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()),
            "overture_japan": make_source(tmp_path, "overture_japan", overture),
        }
        result = resolve.resolve(
            SPEC, {"name": "サンプル麺 丸の内本店", "address": "東京都千代田区丸の内2-3-4"},
            "東京都", srcs, NOW,
        )
        assert result.ok, result.shop

    def test_an_unknown_name_says_so(self, sources):
        result = resolve.resolve(SPEC, {"name": "どこにも無い店"}, "東京都", sources, NOW)
        assert not result.ok
        assert result.shop["unresolved"] == "辞典に同じ名前がありません"

    def test_no_dictionary_yet(self):
        result = resolve.resolve(SPEC, {"name": "サンプル軒総本店"}, "東京都", {}, NOW)
        assert not result.ok


class TestAddresses:
    def test_blocks_are_compared_after_the_town(self):
        ours = resolve.address_key("東京都千代田区丸の内1丁目9番1号")
        assert ours == "東京都千代田区丸の内1-9-1"
        assert resolve._same_block(resolve.address_key("丸の内1-9-1"), ours, "") is True
        assert resolve._same_block(resolve.address_key("丸の内2-1-1"), ours, "") is False
        # 町の名前で位置を合わせられなければ、違うとは言わない
        assert resolve._same_block(resolve.address_key("大手町3-1-1"), ours, "") is None

    def test_a_different_house_number_in_the_same_block_is_allowed(self):
        """丁目と番が合えば、号の違いは同じ店として扱う。"""
        ours = resolve.address_key("東京都千代田区丸の内1-9-2")
        assert resolve._same_block(resolve.address_key("丸の内1丁目9-1"), ours, "") is True
        assert resolve._same_block(resolve.address_key("丸の内1-8-2"), ours, "") is False
        # 2 つしか無い番地(番地-号)は両方とも比べる
        ours = resolve.address_key("東京都千代田区丸の内650-1")
        assert resolve._same_block(resolve.address_key("丸の内650-2"), ours, "") is False

    def test_kanji_chome_and_postcode(self):
        assert resolve.address_key("〒100-0005 千代田区丸の内一丁目9-1") == "千代田区丸の内1-9-1"


def _items(*items):
    return [dict(i) for i in items]


class TestApplyingToCollectedItems:
    def test_ai_coordinates_are_dropped_and_facts_carried(self, sources):
        items, report = resolve.apply(SPEC, _items({
            "title": "名物丼", "body": "説明", "tags": ["地域:東京都", "出典:OSM"],
            "lat": 1.0, "lon": 2.0,
            "extra": {"lat": 1.0, "lon": 2.0, "opening_hours": "Mo-Su 10:00-22:00",
                      "shop": {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"}},
        }), {}, sources, NOW)
        raw = items[0]
        assert "lat" not in raw and "lat" not in raw["extra"]
        assert "shop" not in raw["extra"]
        facts = raw[resolve.FACTS_KEY]
        assert (facts["lat"], facts["lon"]) == HONTEN
        # 営業時間は辞典の値が先
        assert facts["opening_hours"] == "Mo,We-Su 11:00-20:00; Tu off"
        assert raw["tags"] == ["地域:東京都", "出典:OSM", "出典:Overture"]
        assert report["resolved"] == 1

    def test_a_failure_marks_the_item_and_clears_coordinates(self, sources):
        items, report = resolve.apply(SPEC, _items({
            "title": "名の無い料理", "body": "説明", "tags": ["地域:東京都", "出典:OSM"],
            "extra": {"shop": {"name": "どこにも無い店"}},
        }), {}, sources, NOW)
        raw = items[0]
        assert raw[resolve.FACTS_KEY]["lat"] is None
        assert raw[resolve.FACTS_KEY]["shop"]["unresolved"]
        assert raw["tags"] == ["地域:東京都", "状態:代表店未定"]
        assert report["failures"] == [{"title": "名の無い料理", "reason": "辞典に同じ名前がありません"}]

    def test_the_same_shop_is_not_resolved_again(self):
        """AI が前に引き当てた店をそのまま返しても、引き直さない(辞典が無くても動かない)。"""
        before = {"shop": {
            "name": "サンプル軒 総本店", "ref": "osm_japan:サンプル軒 総本店",
            "source": "osm_japan", "sources": ["osm_japan"], "resolved_at": NOW,
            "clue": {"name": "サンプル軒総本店"}, "opening_hours": "Mo-Su 11:00-20:00",
        }, "lat": HONTEN[0], "lon": HONTEN[1]}
        items, report = resolve.apply(SPEC, _items({
            "title": "名物丼", "body": "説明", "tags": ["地域:東京都"],
            "lat": 1.0, "lon": 2.0,
            "extra": {"shop": {"name": "サンプル軒 総本店", "ref": "osm_japan:サンプル軒 総本店"}},
        }), {"名物丼": before}, {}, NOW)
        raw = items[0]
        assert report["kept"] == 1
        # 店にも座標にも触らない(営業時間は辞典のものを写し直す)
        assert raw[resolve.FACTS_KEY] == {"opening_hours": "Mo-Su 11:00-20:00"}
        assert "lat" not in raw
        assert raw["tags"] == ["地域:東京都", "出典:OSM"]

    def test_omitted_tags_are_put_back_from_the_shop(self):
        before = {"shop": {"name": "店", "source": "osm_japan", "resolved_at": NOW}}
        items, _ = resolve.apply(SPEC, _items({
            "title": "料理", "body": "説明", "tags": ["地域:東京都"], "extra": {},
        }), {"料理": before}, {}, NOW)
        assert items[0]["tags"] == ["地域:東京都", "出典:OSM"]

    def test_null_clears_the_shop(self):
        before = {"shop": {"name": "店", "source": "osm_japan", "resolved_at": NOW,
                           "opening_hours": "Mo 10:00-12:00"},
                  "opening_hours": "Mo 10:00-12:00", "lat": 1.0, "lon": 2.0}
        items, report = resolve.apply(SPEC, _items({
            "title": "料理", "body": "説明", "tags": ["地域:東京都", "出典:OSM"],
            "extra": {"shop": None},
        }), {"料理": before}, {}, NOW)
        facts = items[0][resolve.FACTS_KEY]
        assert facts == {"shop": None, "lat": None, "lon": None, "opening_hours": None}
        assert items[0]["tags"] == ["地域:東京都"]
        assert report["cleared"] == 1

    def test_a_bad_dictionary_value_is_not_lifted(self, sources):
        items, _ = resolve.apply(SPEC, _items({
            "title": "名物丼", "body": "説明", "tags": ["地域:東京都"],
            "extra": {"shop": {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"}},
        }), {}, sources, NOW, valid=lambda key, value: False)
        assert "opening_hours" not in items[0][resolve.FACTS_KEY]

    def test_tombstones_are_left_alone(self, sources):
        raw = {"title": "料理", "body": "閉店", "tags": [notes.TOMBSTONE_TAG], "extra": {"shop": None}}
        items, _ = resolve.apply(SPEC, [raw], {}, sources, NOW)
        assert items[0] is raw


class TestTheSpec:
    def test_round_trip(self):
        assert resolve.normalize(resolve.to_json(SPEC)) == SPEC

    @pytest.mark.parametrize("raw", [
        {"sources": ["osm_japan"]},
        {"clue": "shop"},
        {"clue": "lat", "sources": ["osm_japan"]},
        {"clue": "shop", "sources": ["osm_japan", "osm_japan"]},
        {"clue": "shop", "sources": ["osm_japan"], "failed_tag": "_chiezo_x"},
        {"clue": "shop", "sources": ["osm_japan"], "to_extra": {"opening_hours": "lat"}},
    ])
    def test_unreadable_specs_are_refused(self, raw):
        with pytest.raises(HTTPException):
            resolve.normalize(raw)
