"""区画 —— 定期の巡回が「どこを見るか」を割り出す層(`app/partition.py`)。

押さえているのは 2 つ。**割るのは対象としている空間であって、集まったものでは
ない**こと(まだ 1 件も集めていない範囲にも区画ができる)と、**密度で割れて
いる**こと(都市部は細かく・地方は粗く、面積では等分しない)。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import partition


@pytest.fixture
def source(tmp_path):
    """座標とタグを持つソース(焼き上がりと同じコアスキーマ)を作る。"""
    from app import notes
    from app.registry import Source

    def make(docs, name="osm_japan"):
        path = tmp_path / f"{name}.db"
        conn = sqlite3.connect(path)
        conn.executescript(notes.SCHEMA_DDL)
        for doc_id, doc in enumerate(docs, start=1):
            extra = {k: doc[k] for k in ("lat", "lon", "feature") if k in doc}
            conn.execute(
                "INSERT INTO docs (doc_id, title, opening, body, tags, extra, links,"
                " updated_at, rank_score)"
                " VALUES (?, ?, '', '', ?, ?, '[]', '2026-01-01T00:00:00+00:00', ?)",
                (
                    doc_id,
                    doc["title"],
                    json.dumps(doc.get("tags", []), ensure_ascii=False),
                    json.dumps(extra, ensure_ascii=False),
                    doc.get("rank", 0.0),
                ),
            )
            if "lat" in doc:
                conn.execute(
                    "INSERT INTO doc_coords (lat, lon, doc_id) VALUES (?, ?, ?)",
                    (doc["lat"], doc["lon"], doc_id),
                )
            for tag in doc.get("tags", []):
                conn.execute("INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id))
                conn.execute(
                    "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
                    " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
                    (tag,),
                )
        conn.commit()
        conn.close()
        return {
            name: Source(
                name=name, kind="osm", lang="ja", dump_date="20260101", schema_version=4,
                built_at="2026-01-01T00:00:00+00:00", doc_count=len(docs), path=Path(path),
            )
        }

    return make


def spots():
    """都市部に密集した 300 件と、遠く離れたところの 3 件。

    面積で等分すると使いものにならない並び方 —— 現実の飲食店がこうなっている。
    """
    docs = []
    for i in range(300):
        docs.append({
            "title": f"都市の店{i}",
            "lat": 35.65 + (i % 20) * 0.002,
            "lon": 139.70 + (i // 20) * 0.002,
            "feature": "amenity=restaurant",
            "rank": float(i),
        })
    for i, (lat, lon) in enumerate([(43.05, 141.35), (43.20, 142.80), (44.35, 142.45)]):
        docs.append({
            "title": f"北の店{i}", "lat": lat, "lon": lon,
            "feature": "amenity=restaurant", "rank": 1000.0 - i,
        })
    return docs


class TestTheSpec:
    def test_an_unknown_way_of_splitting_is_refused(self):
        """読めない指定を黙って無視すると、区画を作ったつもりの収集が区画なしで回る。"""
        with pytest.raises(HTTPException):
            partition.normalize({"by": "いいかげんな値"})

    def test_splitting_by_tag_needs_a_prefix(self):
        with pytest.raises(HTTPException):
            partition.normalize({"by": "tag"})
        assert partition.normalize({"by": "tag", "prefix": "地域:"})["prefix"] == "地域:"

    def test_the_bbox_is_south_west_north_east(self):
        with pytest.raises(HTTPException):
            partition.normalize({"by": "geo", "bbox": [46.0, 154.0, 20.0, 122.0]})
        spec = partition.normalize({"by": "geo", "bbox": [20, 122, 46, 154]})
        assert spec["bbox"] == [20.0, 122.0, 46.0, 154.0]

    def test_nothing_means_no_partitions(self):
        assert partition.normalize(None) is None
        assert partition.normalize({}) is None


class TestSplittingByDensity:
    def test_the_crowded_place_is_cut_finer_than_the_empty_one(self, source):
        """面積で等分しない。都市部は 1 回で見切れず、地方は空振りになるため。"""
        spec = partition.normalize(
            {"by": "geo", "target": 50, "source": "osm_japan", "feature": "amenity=restaurant"}
        )
        built = partition.build(spec, source(spots()))
        assert len(built) > 1
        boxes = [partition.parse_geo_key(p["key"]) for p in built]
        areas = [(b[2] - b[0]) * (b[3] - b[1]) for b in boxes]
        crowded = min(areas)
        empty = max(areas)
        # 密なところの区画は、疎なところの区画よりずっと小さい
        assert empty > crowded * 100

    def test_every_partition_stays_under_the_target(self, source):
        spec = partition.normalize(
            {"by": "geo", "target": 50, "source": "osm_japan", "feature": "amenity=restaurant"}
        )
        built = partition.build(spec, source(spots()))
        assert all(p["count"] <= 50 for p in built)
        assert sum(p["count"] for p in built) == 303

    def test_a_range_with_nothing_collected_still_gets_a_partition(self, source):
        """**この層の眼目。** 集まった点だけから作ると、まだ何も無い範囲には
        区画が生まれず、永遠に空のままになる。
        """
        spec = partition.normalize({
            "by": "geo", "target": 50, "source": "osm_japan",
            "feature": "amenity=restaurant", "bbox": [20.0, 122.0, 46.0, 154.0],
        })
        built = partition.build(spec, source(spots()), own={})
        # 収集は 1 件も持っていないのに、区画は割れている
        assert len(built) > 1
        # 指定した矩形の端(沖縄のあたり)も、どれかの区画に入っている
        assert any(
            partition.belongs(spec, p["key"], {"extra": {"lat": 26.2, "lon": 127.7}})
            for p in built
        )

    def test_the_whole_bbox_is_covered(self, source):
        """親の矩形をきっかり 2 つに割るので、隙間ができない。"""
        spec = partition.normalize({
            "by": "geo", "target": 50, "source": "osm_japan",
            "feature": "amenity=restaurant", "bbox": [20.0, 122.0, 46.0, 154.0],
        })
        built = partition.build(spec, source(spots()))
        total = sum(
            (b[2] - b[0]) * (b[3] - b[1])
            for b in (partition.parse_geo_key(p["key"]) for p in built)
        )
        assert total == pytest.approx((46.0 - 20.0) * (154.0 - 122.0))

    def test_points_piled_on_one_spot_do_not_loop_forever(self, source):
        """同じ座標に固まった点は割り切れない。止まらなくならないための保険。"""
        same = [
            {"title": f"同じ場所{i}", "lat": 35.0, "lon": 135.0, "feature": "amenity=restaurant"}
            for i in range(50)
        ]
        spec = partition.normalize(
            {"by": "geo", "target": 10, "source": "osm_japan", "feature": "amenity=restaurant"}
        )
        built = partition.build(spec, source(same))
        assert len(built) >= 1
        assert sum(p["count"] for p in built) == 50

    def test_the_population_can_be_the_collection_itself(self, source):
        """母集団を書かなければ自分自身を割る(まだ無い範囲には区画ができない)。"""
        spec = partition.normalize({"by": "geo", "target": 10})
        own = {
            f"店{i}": {"title": f"店{i}", "extra": {"lat": 35.0 + i * 0.01, "lon": 135.0}}
            for i in range(40)
        }
        built = partition.build(spec, {}, own)
        assert len(built) > 1
        assert sum(p["count"] for p in built) == 40


class TestSplittingByTag:
    def test_each_tag_is_one_partition(self, source):
        """タグはもともと対象が付けた区切りなので、こちらで混ぜない。"""
        docs = [
            {"title": "A", "tags": ["地域:東京都", "飲食店"]},
            {"title": "B", "tags": ["地域:東京都"]},
            {"title": "C", "tags": ["地域:北海道"]},
        ]
        spec = partition.normalize({"by": "tag", "prefix": "地域:", "source": "osm_japan"})
        built = partition.build(spec, source(docs))
        assert [p["key"] for p in built] == ["地域:東京都", "地域:北海道"]
        assert [p["count"] for p in built] == [2, 1]

    def test_a_doc_belongs_to_its_tag(self):
        spec = partition.normalize({"by": "tag", "prefix": "地域:"})
        assert partition.belongs(spec, "地域:東京都", {"tags": ["地域:東京都"]})
        assert not partition.belongs(spec, "地域:東京都", {"tags": ["地域:北海道"]})


class TestSplittingByTitle:
    def test_the_key_is_where_it_starts_and_ends(self):
        """番号で区切ると、1 件増えるたびに全部の区画が 1 つずつずれる。"""
        spec = partition.normalize({"by": "title", "target": 10})
        own = {f"{i:02d}": {"title": f"{i:02d}"} for i in range(25)}
        built = partition.build(spec, {}, own)
        assert [p["key"] for p in built] == ["00〜09", "10〜19", "20〜24"]
        assert [p["count"] for p in built] == [10, 10, 5]
        assert partition.belongs(spec, "00〜09", {"title": "09"})
        assert not partition.belongs(spec, "00〜09", {"title": "10"})


class TestTheLedger:
    def test_resplitting_keeps_what_was_already_seen(self):
        """引き継がないと、割り直すたびに一周が最初に戻り、永遠に終わらない。"""
        current = [
            {"key": "A", "count": 1, "visits": {"ざっと": "2026-09-01T00:00:00+00:00"}},
            {"key": "B", "count": 1, "visits": {}},
        ]
        built = [{"key": "A", "count": 2}, {"key": "C", "count": 3}]
        merged = partition.refresh(built, current)
        assert merged[0]["visits"] == {"ざっと": "2026-09-01T00:00:00+00:00"}
        assert merged[0]["count"] == 2
        # 鍵が変わった区画は新しく始まる
        assert merged[1] == {"key": "C", "count": 3, "visits": {}}

    def test_the_unseen_ones_come_first(self):
        ledger = [
            {"key": "A", "count": 1, "visits": {"ざっと": "2026-09-01T00:00:00+00:00"}},
            {"key": "B", "count": 1, "visits": {}},
        ]
        assert partition.due(ledger, "ざっと") == "B"
        marked = partition.mark_visited(ledger, ["B"], "ざっと", "2026-09-02T00:00:00+00:00")
        assert partition.due(marked, "ざっと") == "A"
        assert partition.progress(marked, "ざっと") == (2, 2)

    def test_each_sweep_keeps_its_own_progress(self):
        """ざっとが一周した区画を、じっくりはまだ見ていない、が普通に起きる。"""
        ledger = [{"key": "A", "count": 1, "visits": {}}, {"key": "B", "count": 1, "visits": {}}]
        ledger = partition.mark_visited(ledger, ["A", "B"], "ざっと", "2026-09-01T00:00:00+00:00")
        assert partition.progress(ledger, "ざっと") == (2, 2)
        assert partition.progress(ledger, "じっくり") == (0, 2)
        assert partition.due(ledger, "じっくり") == "A"

    def test_it_can_hand_out_several_at_once(self):
        """1 回に何区画見るかは巡回が決める(ここは順番だけを持つ)。"""
        ledger = [{"key": k, "count": 1, "visits": {}} for k in ["A", "B", "C"]]
        assert partition.pick(ledger, "ざっと", 2) == ["A", "B"]
        ledger = partition.mark_visited(ledger, ["A", "B"], "ざっと", "2026-09-01T00:00:00+00:00")
        assert partition.pick(ledger, "ざっと", 2) == ["C", "A"]

    def test_an_unknown_key_is_ignored(self):
        """割り直しと行き違ったときに、知らない鍵で落ちない。"""
        ledger = [{"key": "A", "count": 1, "visits": {}}]
        assert partition.mark_visited(
            ledger, ["消えた区画"], "ざっと", "2026-09-02T00:00:00+00:00"
        ) == ledger

    def test_an_empty_ledger_has_nothing_due(self):
        assert partition.due([], "ざっと") is None

    def test_a_broken_row_is_dropped(self):
        assert partition.normalize_ledger([{"key": ""}, "文字列", {"key": "A"}]) == [
            {"key": "A", "count": 0, "visits": {}}
        ]


class TestWhenToResplit:
    def test_an_outside_population_does_not_move(self, source):
        """こちらが何件集めようと点の数は変わらない。動かすと記録が毎回消える。"""
        spec = partition.normalize({"by": "title", "target": 10, "source": "osm_japan"})
        ledger = [{"key": "あ〜ん", "count": 10, "visited_at": None}]
        many = {f"あ{i}": {"title": f"あ{i}"} for i in range(100)}
        assert partition.outgrown(spec, ledger, many) is False

    def test_splitting_itself_follows_the_growth(self):
        """自分自身を割っているときは、育って target を超えたら割り直す。"""
        spec = partition.normalize({"by": "title", "target": 10})
        ledger = [{"key": "あ〜ん", "count": 10, "visited_at": None}]
        assert partition.outgrown(spec, ledger, {"あ": {"title": "あ"}}) is False
        many = {f"あ{i}": {"title": f"あ{i}"} for i in range(30)}
        assert partition.outgrown(spec, ledger, many) is True


class TestWhatIsHandedToTheAI:
    def test_a_rectangle_comes_with_a_hint_of_where_it_is(self, source):
        """矩形だけ渡しても、AI にはそこがどこか分からない。"""
        spec = partition.normalize({
            "by": "geo", "target": 50, "source": "osm_japan", "feature": "amenity=restaurant",
        })
        text = partition.describe(spec, "43.0000,141.0000/44.5000,143.0000", source(spots()))
        assert "緯度" in text and "経度" in text
        assert "北の店" in text

    def test_it_still_works_without_a_population_source(self):
        spec = partition.normalize({"by": "geo", "target": 50})
        text = partition.describe(spec, "43.0000,141.0000/44.5000,143.0000", {})
        assert "43.0000" in text

    def test_a_doc_without_coordinates_is_in_no_rectangle(self):
        """座標が無いものは、次にその区画を見るとき「まだ無い」と見える。"""
        spec = partition.normalize({"by": "geo", "target": 50})
        assert not partition.belongs(spec, "43.0000,141.0000/44.5000,143.0000", {"title": "X"})
