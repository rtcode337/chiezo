"""集める層の中の引き当て・書式・県の区画(`resolve` / `formats` / 区画の `values`)。

辞典の引き当てそのものは `tests/test_resolve.py`、書式は `tests/test_formats.py`。
ここで押さえるのは、**焼く流れに乗ったときに約束が守られるか** —— 運んだ座標が
AI の回で動かないこと、引き当てられなかった 1 件が理由ごと読めること、
まだ 1 件も無い県にも区画があること。
"""
from __future__ import annotations

import json

import pytest
from fastapi import HTTPException

from app import collect, notes, resolve
from app import partition as partitioning
from tests.test_resolve import HONTEN, NOW, make_source, osm_docs, overture_docs

RESOLVE = {
    "clue": "shop",
    "sources": [
        {"source": "osm_japan", "identity": ["osm_type", "osm_id"], "tag": "出典:OSM"},
        {"source": "overture_japan", "identity": ["overture_id"], "tag": "出典:Overture"},
    ],
    "area_tag": "地域:",
    "to_extra": {"opening_hours": "opening_hours"},
    "failed_tag": "状態:代表店未定",
}
FORMATS = {"available_months": "months", "opening_hours": "opening_hours"}


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    from app import db

    notes_dir = tmp_path / "notes"
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
    db.set_mutable_paths([notes_dir / "notes.db"])
    return tmp_path


@pytest.fixture
def gourmet(enabled):
    return collect.create(
        "gourmet", prompt="{partition}\n{current}", interval_minutes=60,
        resolve_spec=RESOLVE, formats_spec=FORMATS,
    )


@pytest.fixture
def dictionaries(tmp_path):
    return {
        "osm_japan": make_source(tmp_path, "osm_japan", osm_docs()),
        "overture_japan": make_source(tmp_path, "overture_japan", overture_docs()),
    }


def _baked(body: str) -> dict[str, dict]:
    return {d["title"]: d for d in map(json.loads, body.splitlines()[1:])}


def _dish(**extra):
    return {
        "title": "名物丼", "body": "その土地で通っている丼。",
        "tags": ["地域:東京都", "種類:丼"], "lat": 35.0, "lon": 135.0,
        "extra": {"shop": {"name": "サンプル軒総本店", "address": "東京都千代田区丸の内1-9-1"},
                  "available_months": "通年じゃない", **extra},
    }


class TestBaking:
    def test_the_shop_is_resolved_and_ai_coordinates_dropped(self, gourmet, dictionaries):
        items, checks = collect.check_collected(gourmet, [_dish()], dictionaries)
        body, _diff = collect.ndjson(gourmet, dictionaries, {}, items)
        doc = _baked(body)["名物丼"]

        assert (doc["extra"]["lat"], doc["extra"]["lon"]) == HONTEN
        assert doc["extra"]["shop"]["ref"] == "osm_japan:サンプル軒 総本店"
        # 辞典の営業時間が 1 件の脇書きにも写る(書式を通ったもの)
        assert doc["extra"]["opening_hours"] == "Mo,We-Su 11:00-20:00; Tu off"
        # 崩れた月は落ち、理由が控えに残る
        assert "available_months" not in doc["extra"]
        assert "出典:OSM" in doc["tags"] and "出典:Overture" in doc["tags"]
        assert checks["resolved"] == 1
        assert checks["dropped"] == 1
        assert checks["dropped_values"][0]["key"] == "available_months"

    def test_a_later_ai_round_does_not_move_the_coordinates(self, gourmet, dictionaries):
        """手がかりが同じなら引き直さず、AI の座標も捨てる。"""
        items, _ = collect.check_collected(gourmet, [_dish()], dictionaries)
        first = _baked(collect.ndjson(gourmet, dictionaries, {}, items)[0])["名物丼"]
        previous = {"名物丼": {**first, "doc_id": 1}}
        baked = make_source_for(gourmet, previous)
        again = {**_dish(), "lat": 1.0, "lon": 1.0, "tags": ["地域:東京都", "種類:丼"],
                 "extra": {"lat": 1.0, "lon": 1.0, "shop": {"name": "サンプル軒 総本店"}}}
        items, checks = collect.check_collected(gourmet, [again], {**dictionaries, **baked})
        body, _diff = collect.ndjson(gourmet, {**dictionaries, **baked}, previous, items, edits=True)
        doc = _baked(body)["名物丼"]

        assert checks["kept"] == 1
        assert (doc["extra"]["lat"], doc["extra"]["lon"]) == HONTEN
        assert doc["extra"]["shop"] == first["extra"]["shop"]
        # AI が書き直したタグから出どころが落ちても戻る
        assert "出典:OSM" in doc["tags"]

    def test_an_unresolved_item_has_no_coordinates_and_a_reason(self, gourmet, dictionaries):
        dish = {**_dish(), "extra": {"shop": {"name": "どこにも無い店"}}}
        items, checks = collect.check_collected(gourmet, [dish], dictionaries)
        doc = _baked(collect.ndjson(gourmet, dictionaries, {}, items)[0])["名物丼"]

        assert "lat" not in doc["extra"]
        assert doc["extra"]["shop"]["unresolved"] == "辞典に同じ名前がありません"
        assert "状態:代表店未定" in doc["tags"]
        assert checks["failures"] == [{"title": "名物丼", "reason": "辞典に同じ名前がありません"}]
        # 次の回の AI には理由が見える
        text, _ = collect.render_material({"名物丼": doc}, clue="shop")
        assert "引き当てられなかった —— 辞典に同じ名前がありません" in text

    def test_a_previously_failed_item_is_retried_when_a_dictionary_can_place_it(
        self, gourmet, dictionaries, tmp_path,
    ):
        """前に落ちた 1 件は、AI が返さない回でも引き直す(辞典を足したあとに置けるように)。
        本文と他のタグは動かさず、店・座標・出どころのタグだけを重ねる。"""
        from tests.test_resolve import address_docs

        dish = {**_dish(), "extra": {"shop": {"name": "どこにも無い店", "address": "東京都千代田区丸の内2-3-4"}}}
        items, _ = collect.check_collected(gourmet, [dish], dictionaries)
        first = _baked(collect.ndjson(gourmet, dictionaries, {}, items)[0])["名物丼"]
        assert first["extra"]["shop"]["unresolved"]
        previous = {"名物丼": {**first, "doc_id": 1}}
        baked = make_source_for(gourmet, previous)

        # 住所の辞典を足した。AI は何も返さない回
        collect.update("gourmet", resolve={**RESOLVE, "address": {"source": "address_jp", "tag": "出典:位置参照情報"}})
        gourmet = collect.get("gourmet")
        srcs = {**dictionaries, **baked, "address_jp": make_source(tmp_path, "address_jp", address_docs())}
        items, checks = collect.check_collected(gourmet, [], srcs)
        body, diff = collect.ndjson(gourmet, srcs, previous, items, edits=True)
        doc = _baked(body)["名物丼"]

        assert checks["retried"] == 1
        assert (doc["extra"]["lat"], doc["extra"]["lon"]) == (35.68, 139.764)
        assert doc["extra"]["shop"]["basis"] == [resolve.BASIS_ADDRESS]
        assert "出典:位置参照情報" in doc["tags"]
        assert "状態:代表店未定" not in doc["tags"]
        assert "種類:丼" in doc["tags"]
        assert doc["body"] == first["body"]
        assert diff["updated"] == 1 and diff["added"] == 0
        assert collect.checks_note(checks).endswith("前に落ちたものを引き直して 1 件")

    def test_a_retry_that_still_fails_changes_nothing(self, gourmet, dictionaries):
        dish = {**_dish(), "extra": {"shop": {"name": "どこにも無い店"}}}
        items, _ = collect.check_collected(gourmet, [dish], dictionaries)
        first = _baked(collect.ndjson(gourmet, dictionaries, {}, items)[0])["名物丼"]
        previous = {"名物丼": {**first, "doc_id": 1}}
        srcs = {**dictionaries, **make_source_for(gourmet, previous)}

        items, checks = collect.check_collected(gourmet, [], srcs)

        assert items == []
        assert checks["retried"] == 0

    def test_collections_without_the_specs_are_untouched(self, enabled, dictionaries):
        plain = collect.create("plain", prompt="x", interval_minutes=60)
        items = [_dish()]
        assert collect.check_collected(plain, items, dictionaries) == (items, None)

    def test_the_checks_are_kept_on_the_definition(self, gourmet):
        collect.record_result("gourmet", status="ok", checks={"resolved": 2})
        assert collect.get("gourmet").last_checks == {"resolved": 2}
        # 渡さなかった回は前のまま
        collect.record_result("gourmet", status="ok")
        assert collect.get("gourmet").last_checks == {"resolved": 2}
        assert collect.checks_note({"resolved": 2, "unresolved": 1, "dropped": 3}) == (
            "引き当て 2 件・できず 1 件 / 書式に合わず落とした値 3 件"
        )


def make_source_for(item, docs: dict[str, dict]):
    """焼いた収集の代わり(脇書きとタグを持つ)。"""
    import sqlite3
    import tempfile
    from pathlib import Path

    from tests.test_resolve import Src

    path = Path(tempfile.mkdtemp()) / f"{item.name}.db"
    conn = sqlite3.connect(path)
    conn.executescript(notes.SCHEMA_DDL)
    for doc in docs.values():
        conn.execute(
            "INSERT INTO docs (doc_id, title, opening, body, tags, extra, links, updated_at,"
            " rank_score) VALUES (?, ?, '', ?, ?, ?, '[]', ?, 0.0)",
            (doc["doc_id"], doc["title"], doc["body"], json.dumps(doc["tags"], ensure_ascii=False),
             json.dumps(doc["extra"], ensure_ascii=False), NOW),
        )
        for tag in doc["tags"]:
            conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc["doc_id"]))
    conn.commit()
    conn.close()
    src = Src(path)
    src.doc_count = len(docs)
    src.dump_date = "20261017000000"
    return {item.name: src}


class TestReadingWhatFailed:
    def test_unresolved_items_are_listed_with_reasons(self, gourmet):
        docs = {
            "A": {"doc_id": 1, "title": "A", "body": "本文", "tags": [], "extra": {
                "shop": {"clue": {"name": "店A"}, "unresolved": "辞典に同じ名前がありません",
                         "checked_at": NOW}}},
            "B": {"doc_id": 2, "title": "B", "body": "本文", "tags": [], "extra": {
                "shop": {"name": "店B", "resolved_at": NOW}}},
            "C": {"doc_id": 3, "title": "C", "body": "本文", "tags": [notes.REMOVED_TAG], "extra": {
                "shop": {"clue": {"name": "店C"}, "unresolved": "消えたもの"}}},
        }
        rows = collect.unresolved_docs(gourmet, make_source_for(gourmet, docs))
        assert [(r["title"], r["reason"]) for r in rows] == [("A", "辞典に同じ名前がありません")]
        assert rows[0]["clue"] == {"name": "店A"}


class TestTheDefinition:
    def test_specs_round_trip_and_can_be_cleared(self, gourmet):
        assert collect.get("gourmet").resolve == resolve.to_json(resolve.normalize(RESOLVE))
        assert collect.get("gourmet").formats == FORMATS
        collect.update("gourmet", resolve={}, formats={})
        assert collect.get("gourmet").resolve is None
        assert collect.get("gourmet").formats == {}

    def test_broken_specs_are_refused(self, gourmet):
        with pytest.raises(HTTPException):
            collect.update("gourmet", resolve={"clue": "shop"})
        with pytest.raises(HTTPException):
            collect.update("gourmet", formats={"available_months": "dates"})

    def test_the_ai_is_told_the_shape_and_not_asked_for_coordinates(self, enabled):
        item = collect.create(
            "shops", prompt="{partition}\n{current}", interval_minutes=60,
            partition_spec={"by": "geo", "target": 10}, resolve_spec=RESOLVE,
        )
        system = collect.build_messages(item, {})[0]["content"]
        assert "extra.shop" in system
        assert "座標(lat / lon)は書かない" in system
        assert collect.GEO_SYSTEM_NOTE not in system

    def test_a_store_collection_cannot_resolve(self, enabled):
        notes.create_store("hina", set())
        with pytest.raises(HTTPException):
            collect.create(
                "hina_learning", prompt="x", interval_minutes=60,
                store="hina", store_tags=["学習"], resolve_spec=RESOLVE,
                sweeps=[{"name": "天文", "by_caller": True}],
            )

    def test_blank_keys_are_shown_on_each_line(self, gourmet):
        """空欄を埋める仕事のために、空いている脇書きの鍵を 1 行に添える(値は載せない)。"""
        collect.update("gourmet", show_blanks=["shop_reason", "opening_hours"])
        item = collect.get("gourmet")
        assert item.show_blanks == ["shop_reason", "opening_hours"]
        docs = {
            "名物丼": {"title": "名物丼", "body": "説明", "tags": [],
                       "extra": {"shop_reason": "本店", "opening_hours": ""}},
            "名物餅": {"title": "名物餅", "body": "説明", "tags": [], "extra": {}},
            "名物汁": {"title": "名物汁", "body": "説明", "tags": [],
                       "extra": {"shop_reason": "本店", "opening_hours": "Mo-Su 10:00-18:00"}},
        }
        text, _ = collect.render_material(docs, blanks=item.show_blanks)
        lines = {line.split(" ")[1]: line for line in text.splitlines() if line.startswith("- ")}
        assert lines["名物丼"].endswith("〔空: opening_hours〕")
        assert lines["名物餅"].endswith("〔空: shop_reason, opening_hours〕")
        assert "〔空" not in lines["名物汁"]
        assert "本店" not in text
        # 空の並びで外れる
        collect.update("gourmet", show_blanks=[])
        assert collect.get("gourmet").show_blanks == []

    def test_unreadable_blank_keys_are_refused(self, gourmet):
        with pytest.raises(HTTPException):
            collect.update("gourmet", show_blanks=["shop reason"])
        with pytest.raises(HTTPException):
            collect.update("gourmet", show_blanks="shop_reason")

    def test_renaming_a_dictionary_follows(self, gourmet):
        moved = collect._with_source_renamed(collect.get("gourmet"), "osm_japan", "osm_jp")
        assert [one["source"] for one in moved.resolve["sources"]] == ["osm_jp", "overture_japan"]


class TestAreaPartitions:
    """**県を並べて、まだ 1 件も無い県にも区画を作る**(区画の `values`)。"""

    def test_every_area_has_a_partition_in_the_written_order(self, gourmet):
        collect.update("gourmet", partition={
            "by": "tag", "prefix": "地域:", "target": 5000,
            "values": ["東京都", "神奈川県", "大阪府"],
        })
        previous = {"名物丼": {"doc_id": 1, "title": "名物丼", "tags": ["地域:大阪府"]}}
        ledger = collect.plan_partitions(collect.get("gourmet"), {}, previous)
        assert [(p["key"], p["count"]) for p in ledger] == [
            ("地域:東京都", 0), ("地域:神奈川県", 0), ("地域:大阪府", 1),
        ]
        spec = partitioning.normalize(collect.get("gourmet").partition)
        assert partitioning.pick(ledger, "既定", 1, spec) == ["地域:東京都"]

    def test_a_changed_list_regroups_and_keeps_visits(self, gourmet):
        collect.update(
            "gourmet",
            partition={"by": "tag", "prefix": "地域:", "values": ["東京都", "大阪府"]},
            partitions=[
                {"key": "地域:大阪府", "count": 1, "visits": {"既定": NOW}},
                {"key": "地域:東京都", "count": 0},
            ],
        )
        # 並びを変えただけでは台帳を捨てない(割り方は同じ)
        item = collect.update("gourmet", partition={
            "by": "tag", "prefix": "地域:", "values": ["大阪府", "東京都", "神奈川県"],
        })
        assert len(item.partitions) == 2
        ledger = collect.plan_partitions(item, {}, {})
        assert [p["key"] for p in ledger] == ["地域:大阪府", "地域:東京都", "地域:神奈川県"]
        assert ledger[0]["visits"] == {"既定": NOW}


def _form(values: dict):
    from starlette.datastructures import FormData

    pairs = [
        (key, one) for key, value in values.items()
        for one in (value if isinstance(value, list) else [value])
    ]
    data = FormData(pairs)

    async def form():
        return data
    return form


class TestTheAdminPage:
    def test_the_specs_are_edited_and_the_checks_shown(self, gourmet):
        import asyncio
        from types import SimpleNamespace

        from app.views import admin

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(sources={})),
            form=_form({
                "prompt": "{current}", "resolve": json.dumps(RESOLVE, ensure_ascii=False),
                "formats": "",
            }),
        )
        asyncio.run(admin.admin_collect_edit("gourmet", request))
        item = collect.get("gourmet")
        assert item.resolve["clue"] == "shop"
        # 空の欄は「使わない」
        assert item.formats == {}

        collect.record_result("gourmet", status="ok", checks={
            "at": NOW, "resolved": 1, "unresolved": 1,
            "dropped": 1, "dropped_values": [
                {"title": "料理", "key": "available_months", "value": "冬", "reason": "読めない",
                 "from": "AI"},
            ],
        })
        html = admin._collect_detail_html(collect.get("gourmet"), "")
        assert "引き当てと書式" in html
        assert "引き当て 1 件" in html and "読めない" in html
        assert 'name="resolve"' in html and 'name="formats"' in html

    def test_an_empty_area_partition_can_be_named_for_one_run(self, gourmet, monkeypatch):
        """まだ 1 件も無い県でも、画面からその県だけを 1 回走らせられる。"""
        import asyncio
        from types import SimpleNamespace

        from app import main
        from app.views import admin

        collect.update(
            "gourmet",
            partition={"by": "tag", "prefix": "地域:", "values": ["東京都", "大阪府"]},
            partitions=[{"key": "地域:東京都", "count": 0}, {"key": "地域:大阪府", "count": 0}],
        )
        asked = {}
        monkeypatch.setattr(main, "run_or_queue", lambda name, sweep, **kw: asked.update(kw))
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(sources={})),
            form=_form({"partition": "地域:大阪府"}),
        )
        asyncio.run(admin.admin_collect_partition_run("gourmet", request))
        assert asked["run_once"]["partitions"] == ["地域:大阪府"]
