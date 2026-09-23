"""区画 —— 定期の巡回が「どこを見るか」を割り出す層(`app/partition.py`)。

押さえているのは 2 つ。**割るのは対象としている空間であって、集まったものでは
ない**こと(まだ 1 件も集めていない範囲にも区画ができる)と、**密度で割れて
いる**こと(都市部は細かく・地方は粗く、面積では等分しない)。
"""
from __future__ import annotations

import itertools
import json
import sqlite3
from pathlib import Path
from typing import ClassVar

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
    def resplit(self, spec, ledger, docs):
        """数えてから判ずる(本番も同じ順で、数えた結果は台帳へも書き戻される)。"""
        return partition.outgrown(spec, partition.counts_of(spec, ledger, docs))

    def test_an_outside_population_does_not_move(self, source):
        """こちらが何件集めようと点の数は変わらない。動かすと記録が毎回消える。"""
        spec = partition.normalize({"by": "title", "target": 10, "source": "osm_japan"})
        ledger = [{"key": "あ〜ん", "count": 10, "visited_at": None}]
        many = {f"あ{i}": {"title": f"あ{i}"} for i in range(100)}
        assert self.resplit(spec, ledger, many) is False

    def test_splitting_itself_follows_the_growth(self):
        """自分自身を割っているときは、育って target を超えたら割り直す。"""
        spec = partition.normalize({"by": "title", "target": 10})
        ledger = [{"key": "あ〜ん", "count": 10, "visited_at": None}]
        assert self.resplit(spec, ledger, {"あ": {"title": "あ"}}) is False
        many = {f"あ{i}": {"title": f"あ{i}"} for i in range(30)}
        assert self.resplit(spec, ledger, many) is True

    def test_an_emptied_partition_triggers_a_resplit(self):
        """中身は別の区画へ移ることがある(年代が入ると本来の帯へ移る)。

        空の区画は回ってきても渡すものが無く、AI に空の範囲を見せて 1 回ぶんの枠を
        捨てることになる。割り直せば組み立てられないので消える。
        """
        spec = partition.normalize({"by": "title", "target": 10})
        ledger = [{"key": "あ〜い", "count": 2}, {"key": "う〜え", "count": 2}]

        both = {"あ": {"title": "あ"}, "う": {"title": "う"}}
        assert self.resplit(spec, ledger, both) is False

        # 「う〜え」の中身が無くなった
        assert self.resplit(spec, ledger, {"あ": {"title": "あ"}}) is True

    def test_nothing_moves_while_the_population_is_unreadable(self):
        """まだ 1 度も焼けていない回と、焼いたものが読めなかった回は区別が付かない。

        0 を答えにすると、読めなかっただけの回に台帳ごと捨てることになる。
        """
        spec = partition.normalize({"by": "title", "target": 10})
        ledger = [{"key": "あ〜い", "count": 2}]

        assert self.resplit(spec, ledger, {}) is False


class TestMergingSmallPartitions:
    """小さすぎる区画は、隣と 1 つにする。

    割り直しの引き金は「育った」と「空になった」しかないので、中身が別の区画へ
    移って痩せた帯も、母集団がそもそも小さい分類も、小さいまま回り続ける ——
    1 人のために 1 回ぶんの枠を使うことになる(本番の台帳では 407 区画のうち
    18 が 1 人だった)。
    """

    def spec(self, target: int = 10):
        return partition.normalize({"by": "title", "target": target})

    def test_it_joins_the_neighbour_when_both_are_small(self):
        """合わせても `target` の 8 割に満たないなら 1 つでよい。

        **まとめた先がまた 1 つの範囲になる** —— 鍵はただの広い範囲なので、
        どの文書がどこへ入るかの規則はそのまま効く。
        """
        ledger = [
            {"key": "あ〜い", "count": 3, "visits": {}},
            {"key": "う〜え", "count": 4, "visits": {}},
        ]

        [one] = partition.merged(self.spec(), ledger)

        assert one["key"] == "あ〜え"
        assert one["count"] == 7

    def test_it_leaves_them_alone_when_the_pair_is_big_enough(self):
        """**`target` 丸ごとを閾値にしない。** ぎりぎり届かないものまでまとめると、
        まとめた先が `target` を超えて、割り直しと押し合いになる。
        """
        ledger = [
            {"key": "あ〜い", "count": 4, "visits": {}},
            {"key": "う〜え", "count": 4, "visits": {}},
        ]

        assert [p["key"] for p in partition.merged(self.spec(), ledger)] == ["あ〜い", "う〜え"]

    def test_it_does_not_join_across_a_different_lap(self):
        """**片方だけ見終えている区画をくっつけない。**

        くっつけると、見ていないぶんが「見終えた」に混ざる(逆に、見終えたぶんを
        もう一度回すことにもなる)。
        """
        ledger = [
            {"key": "あ〜い", "count": 1, "visits": {"ざっと": "2026-09-01T00:00:00+00:00"}},
            {"key": "う〜え", "count": 1, "visits": {}},
        ]

        assert len(partition.merged(self.spec(), ledger)) == 2

    def test_it_keeps_the_shared_lap(self):
        """記録が同じ隣どうしなので、まとめても進み具合は動かない。"""
        when = "2026-09-01T00:00:00+00:00"
        ledger = [
            {"key": "あ〜い", "count": 1, "visits": {"ざっと": when}},
            {"key": "う〜え", "count": 1, "visits": {"ざっと": when}},
        ]

        [one] = partition.merged(self.spec(), ledger)

        assert one["visits"] == {"ざっと": when}

    def test_it_can_chain_until_it_is_big_enough(self):
        """1 人の区画が並ぶところでは、届くまで続けて 1 つにする。"""
        ledger = [{"key": f"{c}〜{c}", "count": 1, "visits": {}} for c in "あいうえおかきくけこ"]

        out = partition.merged(self.spec(), ledger)

        # 8 人でちょうど 8 割なので、そこは「満たない」に入らない
        assert [p["count"] for p in out] == [7, 3]

    def test_a_doc_lands_in_the_merged_partition(self):
        """**まとめは「1 回に見せる単位」をくっつけただけ。**

        どの文書がどこへ入るかの規則は変えないので、開いて判じてからまとめ先を返す。
        """
        spec = self.spec()
        ledger = partition.merged(spec, [
            {"key": partition.title_key("あ", "い"), "count": 1, "visits": {}},
            {"key": partition.title_key("う", "え"), "count": 1, "visits": {}},
        ])
        key = ledger[0]["key"]

        for title in ("あ", "う", "え"):
            assert partition.partition_of(spec, ledger, {"title": title}) == key
        assert partition.belongs(spec, key, {"title": "う"})
        assert not partition.belongs(spec, key, {"title": "お"})

    def test_it_does_not_cross_a_group(self):
        """**隣とつなぐ道は分類をまたがない。** またぐと、出来た区画が 1 つの範囲で
        表せなくなる —— 範囲が言えなければ、漏れを問うこと自体が成り立たない。

        小さすぎるものを分類をまたいで寄せ集める道は別にある(`_pooled`)。
        あちらは 1 つの範囲にせず、値を並べた鍵を作る。
        """
        spec = partition.normalize({"by": "band", "prefix": "地域", "value": "年代", "target": 10})
        ledger = [
            {"key": partition.band_key("日本", 1800, 1850), "count": 6, "visits": {}},
            {"key": partition.band_key("朝鮮", 1288, 1900), "count": 6, "visits": {}},
        ]

        assert len(partition.merged(spec, ledger)) == 2

    def test_it_does_not_join_a_band_to_the_unknown_pile(self):
        """年代の帯と「値が分からないものの置き場」は、1 つの範囲にならない。"""
        spec = partition.normalize({"by": "band", "prefix": "地域", "value": "年代", "target": 10})
        ledger = [
            {"key": partition.band_key("日本", 1800, 1850), "count": 1, "visits": {}},
            {"key": partition.band_key("日本", partition.BAND_UNKNOWN, "あ〜い"),
             "count": 1, "visits": {}},
        ]

        assert len(partition.merged(spec, ledger)) == 2

    def test_the_unknown_piles_join_by_title(self):
        spec = partition.normalize({"by": "band", "prefix": "地域", "value": "年代", "target": 10})
        ledger = [
            {"key": partition.band_key("日本", partition.BAND_UNKNOWN, "あ〜い"),
             "count": 1, "visits": {}},
            {"key": partition.band_key("日本", partition.BAND_UNKNOWN, "う〜え"),
             "count": 1, "visits": {}},
        ]

        [one] = partition.merged(spec, ledger)

        assert one["key"] == partition.band_key("日本", partition.BAND_UNKNOWN, "あ〜え")

    def test_joining_bands_closes_the_gap_between_them(self):
        """帯は「値が詰まっているところ」で切るので、あいだが 1 年空くことがある。

        空いた年に入る文書はどの区画にも入らない —— まとめると、そこも埋まる。
        """
        spec = partition.normalize({"by": "band", "prefix": "地域", "value": "年代", "target": 10})
        ledger = [
            {"key": partition.band_key("日本", 1689, 1816), "count": 3, "visits": {}},
            {"key": partition.band_key("日本", 1818, 1864), "count": 3, "visits": {}},
        ]

        [one] = partition.merged(spec, ledger)

        assert one["key"] == partition.band_key("日本", 1689, 1864)
        doc = {"title": "誰か", "tags": ["地域:日本", "年代:1817"]}
        assert partition.partition_of(spec, [one], doc) == one["key"]

    def test_rectangles_join_when_they_share_an_edge(self):
        """矩形も「幅」なので、**割った親へ戻る組**は 1 つにできる。"""
        spec = partition.normalize({"by": "geo", "target": 10})
        ledger = [
            {"key": partition.geo_key([0.0, 0.0, 1.0, 1.0]), "count": 1, "visits": {}},
            {"key": partition.geo_key([1.0, 0.0, 2.0, 1.0]), "count": 1, "visits": {}},
        ]

        [one] = partition.merged(spec, ledger)

        assert one["key"] == partition.geo_key([0.0, 0.0, 2.0, 1.0])

    def test_rectangles_that_do_not_line_up_are_left_alone(self):
        """**ずれたまま囲む矩形を作ると、隣の区画へ食い込む** ——
        そこの文書が 2 つの区画に入り、どちらで見せるかが並び順で決まってしまう。
        """
        spec = partition.normalize({"by": "geo", "target": 10})
        ledger = [
            {"key": partition.geo_key([0.0, 0.0, 1.0, 1.0]), "count": 1, "visits": {}},
            {"key": partition.geo_key([1.0, 0.5, 2.0, 2.0]), "count": 1, "visits": {}},
        ]

        assert len(partition.merged(spec, ledger)) == 2

    def test_a_tag_is_left_alone(self):
        """2 つのタグは 1 つのタグで表せない(名前は幅ではない)。"""
        spec = partition.normalize({"by": "tag", "target": 10, "prefix": "地域:"})
        ledger = [
            {"key": "あ", "count": 1, "visits": {}},
            {"key": "い", "count": 1, "visits": {}},
        ]

        assert len(partition.merged(spec, ledger)) == 2

class TestTheCountInTheLedger:
    """台帳の数は**割ったときの写し**で、以後は更新されない。

    中身は別の区画へ移る(年代が入ると本来の帯へ移る)。一周目に配った 6 区画は
    全員が移って空になったのに、画面には割ったときの 25〜27 が出たままだった。
    """

    def spec(self):
        return partition.normalize({"by": "title", "target": 10})

    def test_it_is_taken_again_without_resplitting(self):
        spec = self.spec()
        ledger = [{"key": "あ〜い", "count": 9}, {"key": "う〜え", "count": 9}]
        docs = {"あ": {"title": "あ"}, "う": {"title": "う"}, "え": {"title": "え"}}

        counts = partition.counts_of(spec, ledger, docs)
        fresh = partition.counted(ledger, counts)

        assert [(p["key"], p["count"]) for p in fresh] == [("あ〜い", 1), ("う〜え", 2)]
        # 鍵は動かない(割り直してはいない)
        assert [p["key"] for p in fresh] == [p["key"] for p in ledger]

    def test_it_keeps_the_old_number_when_nothing_can_be_counted(self):
        """読めなかっただけの回に 0 を書き込まない。"""
        ledger = [{"key": "あ〜い", "count": 9}]

        assert partition.counted(ledger, {}) == ledger


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


class TestTheSecondLap:
    """一周したあとの進み具合。

    **「見終えた / 全区画」は一周すると総数に張り付いて動かなくなる**。区画は消えないので、
    2 周目からは「どこまで来たか」ではなく「いちばん古いところがいつのものか」が
    読みたい値になる —— 次に見るのは必ずそこ。
    """

    def test_the_oldest_comes_first_on_the_second_lap(self):
        partitions = [
            {"key": "あ", "count": 1, "visits": {"ざっと": "2026-09-10T00:00:00+00:00"}},
            {"key": "い", "count": 1, "visits": {"ざっと": "2026-09-01T00:00:00+00:00"}},
            {"key": "う", "count": 1, "visits": {"ざっと": "2026-09-05T00:00:00+00:00"}},
        ]

        assert partition.pick(partitions, "ざっと", 2) == ["い", "う"]

    def test_the_oldest_visit_is_only_known_after_a_full_lap(self):
        partitions = [
            {"key": "あ", "count": 1, "visits": {"ざっと": "2026-09-10T00:00:00+00:00"}},
            {"key": "い", "count": 1},
        ]
        # まだ見ていない区画があるうちは、そちらが先(「いちばん古い」は意味を持たない)
        assert partition.oldest_visit(partitions, "ざっと") is None
        assert partition.pick(partitions, "ざっと", 1) == ["い"]

        walked = partition.mark_visited(partitions, ["い"], "ざっと", "2026-09-12T00:00:00+00:00")
        assert partition.oldest_visit(walked, "ざっと") == "2026-09-10T00:00:00+00:00"

    def test_another_sweep_walks_on_its_own(self):
        """ざっとが一周した区画を、じっくりはまだ見ていない、が普通に起きる。"""
        partitions = [{"key": "あ", "count": 1, "visits": {"ざっと": "2026-09-10T00:00:00+00:00"}}]

        assert partition.oldest_visit(partitions, "ざっと") == "2026-09-10T00:00:00+00:00"
        assert partition.oldest_visit(partitions, "じっくり") is None


class TestNotLosingGroundToTheCeiling:
    """区画の数が天井に当たっても、**範囲を欠かさない**。

    打ち切ると、打ち切った先の母集団がどの区画にも入らない —— そこはどの巡回にも
    回ってこないので、入っているものは誰にも見られないまま溜まり続ける。
    本番でこれが起きた: 68 万件を目安 150 で割ろうとして 2,000 区画で打ち切られ、
    1 区画 200 未満 × 2,000 しか台帳に残らなかった(40 万件ぶんの範囲が消えた)。
    """

    def test_it_refuses_instead_of_quietly_going_coarse(self):
        """**黙って減らさない。** 数で打ち切ると打ち切った先が台帳から消え、
        目安を勝手に上げると頼んだ細かさと違うもので回り続ける ——
        どちらを選ぶかは人が決めること。
        """
        import fastapi

        # **二分は割り切れない**ので、余裕を見て天井の半分で数える
        room = partition.MAX_PARTITIONS // 2
        partition._must_fit(10, room * 10)  # ちょうど収まる
        with pytest.raises(fastapi.HTTPException) as got:
            partition._must_fit(10, room * 10 + 1)

        assert got.value.status_code == 409
        assert "target" in got.value.detail["hint"]

    def test_every_point_lands_in_some_partition(self):
        import random

        random.seed(7)
        points = [
            (35.0 + random.random(), 139.0 + random.random())
            for _ in range(20_000)
        ]
        spec = partition.normalize({"by": "geo", "target": 10})

        out = partition._geo(spec, points)

        # **数えた合計が母集団と一致する**（どこにも入らない点がない）
        assert sum(p["count"] for p in out) == len(points)
        assert len(out) <= partition.MAX_PARTITIONS

    def test_the_titles_are_all_covered_too(self):
        spec = partition.normalize({"by": "title", "target": 10})
        own = {f"見出し{i:05d}": {"doc_id": i} for i in range(20_000)}

        out = partition._titles(spec, {}, own)

        assert len(out) <= partition.MAX_PARTITIONS
        assert sum(p["count"] for p in out) == 20_000


class TestStartingTheLapOver:
    """母集団が入れ替わったら、「見た」は当てにならない。

    **割られた区画の子は親の記録を写す**(`_inherited`)。区画が少し育つたびに
    一周が巻き戻るのを防ぐための作りだが、名簿を作り直して母集団が何十倍にも
    なると逆に効く —— 中身が 40 倍になった区画が「見終えたまま」になる。
    """

    def test_a_changed_partition_loses_every_mark(self):
        before = [{"key": "あ", "count": 130, "visits": {"ざっと見る": "2026-09-19T00:00:00+00:00"}}]
        after = [{"key": "あ", "count": 5200, "visits": {"ざっと見る": "2026-09-19T00:00:00+00:00"}}]

        out = partition.cleared_where_changed(before, after)

        assert out[0]["visits"] == {}

    def test_an_untouched_partition_keeps_its_marks(self):
        """**動いていない区画まで戻さない** —— 戻すと一周が永遠に終わらない。"""
        marks = {"ざっと見る": "2026-09-19T00:00:00+00:00"}
        before = [{"key": "あ", "count": 130, "visits": marks}]
        after = [{"key": "あ", "count": 130, "visits": dict(marks)}]

        assert partition.cleared_where_changed(before, after)[0]["visits"] == marks

    def test_a_new_key_starts_clean(self):
        """割り直しで生まれた区画は、親から写した記録を持ったまま来る。"""
        before = [{"key": "あ", "count": 130, "visits": {"ざっと見る": "2026-09-19T00:00:00+00:00"}}]
        after = [{"key": "あ-1", "count": 60, "visits": {"ざっと見る": "2026-09-19T00:00:00+00:00"}}]

        assert partition.cleared_where_changed(before, after)[0]["visits"] == {}

    def test_every_sweep_loses_its_mark_not_just_one(self):
        """**どの巡回のぶんも外す** —— 中身が入れ替わったのは 1 つの巡回の都合ではない。"""
        before = [{"key": "あ", "count": 1, "visits": {}}]
        after = [{"key": "あ", "count": 9, "visits": {"ざっと見る": "x", "整理": "y"}}]

        assert partition.cleared_where_changed(before, after)[0]["visits"] == {}

    def test_the_whole_lap_can_be_forgotten_for_one_sweep(self):
        """一周をやり直す口。**その巡回のぶんだけ**外す。"""
        partitions = [
            {"key": "あ", "count": 1, "visits": {"ざっと見る": "x", "整理": "y"}},
            {"key": "い", "count": 1, "visits": {"ざっと見る": "z"}},
        ]

        out = partition.forget_all_visits(partitions, "ざっと見る")

        assert out[0]["visits"] == {"整理": "y"}
        assert out[1]["visits"] == {}


class TestBands:
    """分類 × 数で割る(`by=band`)。

    **辞書順はやめた。** 「Kage〜おおやちき」のような塊には何の意味も無く、
    同じ区画に入った 2 つに関係が無い —— 漏れも影響関係も見つけようがない。
    分類(地域)と数(生年)で割れば、流派と世代の塊になる。
    """

    def spec(self, target=10):
        return partition.normalize(
            {"by": "band", "prefix": "地域:", "value": "年代:", "target": target}
        )

    def docs(self, *rows):
        return {
            title: {"title": title, "tags": tags}
            for title, tags in rows
        }

    def test_it_groups_by_the_category_then_the_number(self):
        own = self.docs(
            *[(f"ふ{i}", ["地域:フランス", f"年代:{1840 + i}-1926"]) for i in range(20)],
            ("に", ["地域:日本", "年代:1840-1900"]),
        )
        built = partition.build(self.spec(), {}, own)
        keys = [p["key"] for p in built]

        # **端は開いて書く**(下は `-1849`、上は `1850-`)—— 端の帯は割ったときの
        # 値より外も引き受けるので、閉じて書くと鍵と受け持ちがずれる
        assert "フランス|-1849" in keys
        assert "フランス|1850-" in keys
        # 1 人しかいない分類は 1 帯。上も下も端なので `-` だけ
        assert "日本|-" in keys

    def test_a_sparse_category_gets_a_wide_band(self):
        """人の少ないところは、年の幅が自然に広がる。

        1 帯しか無ければ**値では絞らない** —— その分類の全員がここなので、
        年を書くと書いた範囲の外を誰も探しに行かなくなる。
        """
        own = self.docs(
            ("あ", ["地域:カナダ", "年代:1810-1880"]),
            ("い", ["地域:カナダ", "年代:1866-1950"]),
        )
        built = partition.build(self.spec(), {}, own)

        assert [p["key"] for p in built] == ["カナダ|-"]

    def test_one_number_is_never_split(self):
        """**「フランスの 1660 年生まれ」を 2 つに分けない。** 分けると
        「この範囲の全員」が並ばなくなり、漏れを探す問いが成り立たない。"""
        own = self.docs(*[(f"ひと{i}", ["地域:日本", "年代:1907-1990"]) for i in range(25)])
        built = partition.build(self.spec(), {}, own)

        assert [p["key"] for p in built] == ["日本|-"]
        assert built[0]["count"] == 25

    def test_the_leftover_joins_the_band_before_it(self):
        """**ちょうどで切らない。** 切りのいいところで閉じると、その次の 1 人が
        1 人だけの帯になる(「アイルランド 1928-1928 に 1 人」)。区画は
        「この範囲の全員」を並べて漏れを問う単位なので、1 人に問う意味はほとんど無く、
        区画の数と 1 回ぶんの依頼だけが増える。
        """
        own = self.docs(
            *[(f"あ{i}", ["地域:アイルランド", f"年代:{1700 + i}-1800"]) for i in range(10)],
            ("はぐれ", ["地域:アイルランド", "年代:1928-1990"]),
        )
        built = partition.build(self.spec(), {}, own)

        assert [p["key"] for p in built] == ["アイルランド|-"]
        assert built[0]["count"] == 11

    def test_the_leftover_stays_apart_when_it_does_not_fit(self):
        """遊びを超えるなら寄せない(寄せると 1 回に渡す量が膨らむ)。"""
        own = self.docs(
            *[(f"あ{i}", ["地域:アイルランド", f"年代:{1700 + i}-1800"]) for i in range(10)],
            *[(f"い{i}", ["地域:アイルランド", f"年代:{1920 + i}-1990"]) for i in range(5)],
        )
        built = partition.build(self.spec(), {}, own)

        # target 10 の 2 割増しは 12。10 + 5 は入らないので別のまま
        assert len(built) == 2
        assert [p["count"] for p in built] == [10, 5]

    def test_the_leftover_of_the_unknown_place_joins_too(self):
        """値の分からない置き場も同じ(1 件だけの置き場を作らない)。"""
        own = self.docs(*[(f"ひと{i:02d}", ["地域:日本"]) for i in range(11)])
        built = partition.build(self.spec(), {}, own)

        assert len(built) == 1
        assert built[0]["count"] == 11

    def test_things_without_a_number_get_their_own_place(self):
        """値の分からないものの集合に漏れという概念は無いので、見出しで割ってよい。"""
        own = self.docs(*[(f"ひと{i:02d}", ["地域:日本"]) for i in range(4)])
        built = partition.build(self.spec(), {}, own)

        assert all("|不明|" in p["key"] for p in built)
        assert sum(p["count"] for p in built) == 4

    def test_everything_lands_somewhere(self):
        """入るところの無い文書は、以後どの回にも出てこない。"""
        own = self.docs(
            ("あ", ["地域:フランス", "年代:1840-1926"]),
            ("い", ["地域:日本"]),
            ("う", []),
        )
        spec = self.spec()
        built = partition.build(spec, {}, own)
        keys = {p["key"] for p in built}

        for title, doc in own.items():
            assert partition.partition_of(spec, built, doc) in keys, title

    def test_an_unvisited_one_does_not_borrow_a_parent_mark(self):
        """**「鍵が同じ」と「記録が空」を取り違えない。**

        前は「記録が空なら親を探す」と書いていたので、**まだ 1 度も見ていない
        区画が、それを覆う区画の記録をもらっていた** —— 見ていない区画に印が付き、
        一周が嘘になる。ついでに、親探しは台帳を端から舐めるので割り直すたびに
        区画の数の 2 乗だけ鍵を数へ直していた(8,192 区画で 33.8 秒)。
        """
        spec = self.spec()
        current = [
            {"key": "フランス|-", "count": 9, "visits": {"ざっと": "2026-09-01"}},
            {"key": "フランス|1850-1899", "count": 3, "visits": {}},
        ]
        built = [{"key": "フランス|1850-1899", "count": 3}]

        [got] = partition.refresh(built, current, spec)

        assert got["visits"] == {}, "見ていない区画に印が付いている"

    def test_the_split_keeps_the_visits(self):
        """**割られた区画の子は、親の記録を写す** —— 写さないと、区画が育つたびに
        そこだけ一周が巻き戻る。"""
        spec = self.spec()
        current = [{"key": "フランス|1840-1900", "count": 6, "visits": {"ざっと": "2026-09-01"}}]
        built = [{"key": "フランス|1840-1860", "count": 3}, {"key": "フランス|1861-1900", "count": 3}]

        got = partition.refresh(built, current, spec)

        assert [p["visits"] for p in got] == [{"ざっと": "2026-09-01"}, {"ざっと": "2026-09-01"}]

    def test_the_description_says_the_category_and_the_span(self):
        spec = self.spec()
        assert "フランス" in partition.describe(spec, "フランス|1840-1869", {})
        assert "1840〜1869" in partition.describe(spec, "フランス|1840-1869", {})


class TestTheOpenEndsOfABand:
    """端の帯は、割ったときの値より外も引き受ける —— **鍵もそう書く**。

    値の軸には上限も下限も無いので、いちばん古い帯はそれより古い年を、いちばん
    新しい帯はその先を取る(`_band_of`)。そこを `1600-1641` と閉じて書くと、
    **引き受けているのに誰も探しに行かない範囲**ができる —— 鍵を読んだ人にも、
    `{partition}` を渡された AI にも、その外は別の区画のものだと映る。
    """

    def spec(self, target=10):
        return partition.normalize(
            {"by": "band", "prefix": "地域:", "value": "年代:", "target": target}
        )

    def built(self, years, target=10):
        own = {
            f"ひと{i}": {"title": f"ひと{i}", "tags": ["地域:日本", f"年代:{y}"]}
            for i, y in enumerate(years)
        }
        return partition.build(self.spec(target), {}, own)

    def test_the_bottom_one_is_written_open(self):
        keys = [p["key"] for p in self.built([1840 + i for i in range(30)])]

        assert keys[0].startswith("日本|-"), keys
        assert keys[0] != "日本|-", "真ん中があるので、上まで開いてはいない"

    def test_the_top_one_is_written_open(self):
        keys = [p["key"] for p in self.built([1840 + i for i in range(30)])]

        assert keys[-1].endswith("-"), keys
        assert keys[-1] != "日本|-"

    def test_the_middle_ones_stay_closed(self):
        keys = [p["key"] for p in self.built([1840 + i for i in range(30)])]

        assert len(keys) == 3, keys
        assert partition.parse_band_key(keys[1])[1:3] == (1850, 1859)

    def test_one_band_is_open_at_both_ends(self):
        """上も下も端なら `-` だけ(その分類ぜんぶがここなので、値では絞らない)。"""
        assert [p["key"] for p in self.built([1840, 1841])] == ["日本|-"]

    def test_the_bands_are_still_gapless(self):
        built = self.built([1840 + i for i in range(30)])
        spans = [partition.parse_band_key(p["key"]) for p in built]

        for left, right in itertools.pairwise(spans):
            assert left[2] + 1 == right[1]

    def test_a_year_outside_the_split_lands_on_the_end_band(self):
        spec = self.spec()
        built = self.built([1840 + i for i in range(30)])

        older = {"title": "むかし", "tags": ["地域:日本", "年代:1500"]}
        newer = {"title": "これから", "tags": ["地域:日本", "年代:2200"]}
        assert partition.partition_of(spec, built, older) == built[0]["key"]
        assert partition.partition_of(spec, built, newer) == built[-1]["key"]

    def test_the_key_can_be_read_back(self):
        assert partition.parse_band_key("日本|-1849") == (["日本"], None, 1849, None)
        assert partition.parse_band_key("日本|1850-") == (["日本"], 1850, None, None)
        assert partition.parse_band_key("日本|-") == (["日本"], None, None, None)
        assert partition.parse_band_key("日本|1850-1859") == (["日本"], 1850, 1859, None)

    def test_the_unknown_pile_is_still_read_as_a_title_range(self):
        """分類が「不明」でも取り違えない(範囲は必ず `数字-数字` の形)。"""
        assert partition.parse_band_key("不明|-") == (["不明"], None, None, None)
        assert partition.parse_band_key("日本|不明|あ〜い") == (["日本"], None, None, "あ〜い")

    def test_the_description_says_where_it_stops(self):
        spec = self.spec()
        built = self.built([1840 + i for i in range(30)])

        assert "以下" in partition.describe(spec, built[0]["key"], {}, built)
        assert "以上" in partition.describe(spec, built[-1]["key"], {}, built)
        assert "問いません" in partition.describe(spec, "日本|-", {}, [{"key": "日本|-"}])

    def test_an_old_closed_ledger_still_says_where_it_stops(self):
        """**割り直すまで、台帳は端も閉じた鍵のまま。** 鍵だけを見て
        「1901〜1910 のもの」と伝えると、そこより古い年を誰も探しに行かない。
        """
        spec = self.spec()
        old = [{"key": "日本|1901-1910"}, {"key": "日本|1911-1920"}]

        assert "以下" in partition.describe(spec, "日本|1901-1910", {}, old)


class TestLookingUpWithoutWalkingTheLedger:
    """**区画が増えるほど 1 件が重くなる**、をやめる(`locator`)。

    1 件ごとに台帳を頭から舐め、そのたびに鍵の文字列を数へ直していた ——
    本番の食事処は文書 686,602 件・区画 10,457 個で、1 回の巡回が 8.7 時間
    (うち AI は十数分)。索引を 1 回組めば、1 件の費用が区画の数に依らなくなる。
    """

    def grid(self, side=40):
        """格子に切った矩形の台帳(実物と同じで、隙間なく敷き詰まっている)。"""
        out = []
        for i in range(side):
            for j in range(side):
                s = 20.0 + 20.0 * i / side
                w = 120.0 + 30.0 * j / side
                out.append({"key": partition.geo_key(
                    (s, w, s + 20.0 / side, w + 30.0 / side)), "count": 1})
        return out

    def test_it_finds_the_same_one_as_walking_the_ledger(self):
        import random

        spec = partition.normalize({"by": "geo", "target": 150})
        ledger = self.grid()
        find = partition.locator(spec, ledger)
        random.seed(3)
        for _ in range(200):
            doc = {"title": "x", "extra": {"lat": random.uniform(19, 41),
                                           "lon": random.uniform(119, 151)}}
            walked = next(
                (p["key"] for p in ledger if partition.belongs(spec, p["key"], doc)), None
            )
            assert find(doc) == walked

    def test_it_does_not_get_slower_as_the_ledger_grows(self):
        """区画を 16 倍にしても、1 件の費用は変わらないこと。"""
        import time

        spec = partition.normalize({"by": "geo", "target": 150})
        doc = {"title": "x", "extra": {"lat": 35.0, "lon": 135.0}}

        def per_doc(side):
            find = partition.locator(spec, self.grid(side))
            start = time.perf_counter()
            for _ in range(2000):
                find(doc)
            return time.perf_counter() - start

        small, big = per_doc(10), per_doc(40)
        assert big < small * 4, f"小さい台帳 {small:.4f}s / 大きい台帳 {big:.4f}s"

    def test_a_doc_outside_every_rectangle_is_in_none(self):
        spec = partition.normalize({"by": "geo", "target": 150})
        find = partition.locator(spec, self.grid())

        assert find({"title": "x", "extra": {"lat": 60.0, "lon": 135.0}}) is None
        assert find({"title": "座標なし"}) is None

    def test_a_wide_rectangle_is_still_found(self):
        """割り直しで残った広い範囲は格子に撒かない(索引が太る)ので、別に見る。"""
        spec = partition.normalize({"by": "geo", "target": 150})
        wide = {"key": partition.geo_key((0.0, 0.0, 80.0, 179.0)), "count": 1}
        find = partition.locator(spec, [*self.grid(), wide])

        # 格子の外だが、広い範囲が覆っている
        assert find({"title": "x", "extra": {"lat": 5.0, "lon": 100.0}}) == wide["key"]
        # 格子の中は、台帳で先に並んでいる細かいほうが勝つ
        assert find({"title": "y", "extra": {"lat": 25.0, "lon": 125.0}}) != wide["key"]

    def test_the_other_ways_of_splitting_agree_too(self):
        """見出し・タグ・帯も、索引を通しても同じところへ落ちること。"""
        cases = [
            (
                {"by": "title", "target": 10},
                [{"key": partition.title_key(a, b)} for a, b in
                 (("あ", "お"), ("か", "こ"), ("さ", "そ"))],
                [{"title": t} for t in ("あい", "きく", "そう", "ん", "*")],
            ),
            (
                {"by": "tag", "prefix": "地域:", "target": 10},
                [{"key": "地域:日本"}, {"key": "地域:韓国"}],
                [{"title": "x", "tags": ["地域:日本"]}, {"title": "y", "tags": ["地域:タイ"]}],
            ),
            (
                {"by": "band", "prefix": "地域:", "value": "年代:", "target": 10},
                [{"key": "日本|-1849"}, {"key": "日本|1850-"},
                 {"key": "日本|不明|あ〜い"}, {"key": "韓国|-"}],
                [{"title": "あ", "tags": ["地域:日本", "年代:1700"]},
                 {"title": "い", "tags": ["地域:日本", "年代:1900"]},
                 {"title": "う", "tags": ["地域:日本"]},
                 {"title": "え", "tags": ["地域:韓国", "年代:1900"]},
                 {"title": "お", "tags": ["地域:タイ"]}],
            ),
        ]
        for raw, ledger, docs in cases:
            spec = partition.normalize(raw)
            find = partition.locator(spec, ledger)
            for doc in docs:
                assert find(doc) == partition.partition_of(spec, ledger, doc), (raw, doc)


class TestWhereTheLapStarts:
    """**端から順ではなく、決めた 1 点から広げる**(`origin`)。

    区画は鍵の文字列順に配られるので、日本の地図なら南西の隅(八重山)から北上する
    —— 一周に何十日もかかる台帳では、主要なところに着くのが何か月も先になる
    (本番の食事処は 10,457 区画・1 回 5 区画・1 時間おきで、一周に 87 日)。
    """

    def ledger(self):
        # 那覇・大阪・東京・札幌のあたりを 1 区画ずつ
        return [
            {"key": partition.geo_key((26.0, 127.0, 26.5, 127.5)), "visits": {}},  # 那覇
            {"key": partition.geo_key((34.5, 135.3, 35.0, 135.8)), "visits": {}},  # 大阪
            {"key": partition.geo_key((35.5, 139.5, 36.0, 140.0)), "visits": {}},  # 東京
            {"key": partition.geo_key((43.0, 141.2, 43.5, 141.7)), "visits": {}},  # 札幌
        ]

    def spec(self, origin=None):
        raw = {"by": "geo", "target": 150}
        if origin:
            raw["origin"] = origin
        return partition.normalize(raw)

    def test_without_an_origin_it_starts_at_the_south_west_corner(self):
        got = partition.pick(self.ledger(), "ざっと", 4, self.spec())
        assert got[0] == self.ledger()[0]["key"], "鍵の文字列順なので那覇から"

    def test_it_spreads_out_from_the_point(self):
        got = partition.pick(self.ledger(), "ざっと", 4, self.spec([35.681, 139.767]))
        keys = [p["key"] for p in self.ledger()]
        # 東京 → 大阪 → 札幌 → 那覇
        assert got == [keys[2], keys[1], keys[3], keys[0]]

    def test_the_unseen_ones_still_come_first(self):
        """近さは**同じ周回の中の**並べ替え。見ていないものより先には出ない。"""
        ledger = self.ledger()
        ledger[2]["visits"] = {"ざっと": "2026-09-01"}  # 東京はもう見た
        got = partition.pick(ledger, "ざっと", 1, self.spec([35.681, 139.767]))
        assert got == [ledger[1]["key"]], "次に近い大阪"

    def test_the_second_lap_follows_the_same_spread(self):
        """1 周目を近い順に回れば印も近い順に付くので、「古い順」がそれをなぞる。"""
        ledger = self.ledger()
        spec = self.spec([35.681, 139.767])
        for n, key in enumerate(partition.pick(ledger, "ざっと", 4, spec)):
            for p in ledger:
                if p["key"] == key:
                    p["visits"] = {"ざっと": f"2026-09-0{n + 1}"}
        again = partition.pick(ledger, "ざっと", 4, spec)
        assert again == [p["key"] for p in sorted(
            ledger, key=lambda p: p["visits"]["ざっと"]
        )]

    def test_a_key_we_cannot_read_goes_last(self):
        ledger = [{"key": "こわれた", "visits": {}}, *self.ledger()]
        got = partition.pick(ledger, "ざっと", 5, self.spec([35.681, 139.767]))
        assert got[-1] == "こわれた"

    def test_it_is_refused_outside_the_map(self):
        with pytest.raises(HTTPException):
            partition.normalize({"by": "geo", "origin": [95.0, 139.0]})
        with pytest.raises(HTTPException):
            partition.normalize({"by": "geo", "origin": [35.6]})
        with pytest.raises(HTTPException):
            partition.normalize({"by": "geo", "origin": ["東京", "駅"]})

    def test_it_only_makes_sense_for_rectangles(self):
        """距離で並べるので座標が要る。他の割り方では**黙って効かない**ほうが困る。"""
        with pytest.raises(HTTPException):
            partition.normalize({"by": "title", "origin": [35.681, 139.767]})

    def test_it_survives_the_round_trip(self):
        spec = partition.normalize({"by": "geo", "origin": [35.681, 139.767]})
        assert partition.to_json(spec)["origin"] == [35.681, 139.767]
        assert "origin" not in partition.to_json(partition.normalize({"by": "geo"}))


class TestNobodyIsLeftWithoutAPartition:
    """**どの区画にも入らない文書は、どの回にも出てこない。**

    例外にはならないので気づけない —— `{current}` にも件数にも現れないまま、
    巡回を何周しても AI の目に触れずに残る。
    """

    def spec(self, by="band", target=10):
        return partition.normalize(
            {"by": by, "prefix": "地域:", "value": "生年:", "target": target}
        )

    def test_a_value_that_shows_up_later_has_no_home_at_first(self):
        """割った時点で全員が値を持っていると、「不明」の置き場は作られない。"""
        spec = self.spec()
        own = {f"ふ{i}": {"title": f"ふ{i}", "tags": ["地域:フランス", f"生年:{1840 + i}"]}
               for i in range(25)}
        built = partition.build(spec, {}, own)

        newcomer = {"title": "生年不明のふ", "tags": ["地域:フランス"]}
        assert partition.partition_of(spec, built, newcomer) is None

    def test_it_is_counted_so_the_resplit_can_notice(self):
        spec = self.spec()
        own = {f"ふ{i}": {"title": f"ふ{i}", "tags": ["地域:フランス", f"生年:{1840 + i}"]}
               for i in range(25)}
        built = partition.build(spec, {}, own)
        own["生年不明のふ"] = {"title": "生年不明のふ", "tags": ["地域:フランス"]}

        counts = partition.counts_of(spec, built, own)

        assert counts[partition.HOMELESS] == 1
        assert partition.outgrown(spec, counts), "行き場が無いなら割り直す"

    def test_the_resplit_gives_it_a_home(self):
        """割り直せばその分類に置き場ができて収まる(空回りしない)。"""
        spec = self.spec()
        own = {f"ふ{i}": {"title": f"ふ{i}", "tags": ["地域:フランス", f"生年:{1840 + i}"]}
               for i in range(25)}
        own["生年不明のふ"] = {"title": "生年不明のふ", "tags": ["地域:フランス"]}

        again = partition.build(spec, {}, own)

        assert partition.partition_of(spec, again, own["生年不明のふ"]) is not None
        assert partition.counts_of(spec, again, own)[partition.HOMELESS] == 0
        assert not partition.outgrown(spec, partition.counts_of(spec, again, own))

    def test_a_rectangle_without_coordinates_does_not_trigger_a_resplit(self):
        """**矩形は割り直しても行き場ができない**(座標が無いのは仕様)——
        真にすると、直りようのない 1 件のために毎回全件を割り直すことになる。
        """
        spec = self.spec(by="geo")
        ledger = [{"key": partition.geo_key((30.0, 130.0, 40.0, 140.0)), "count": 1}]
        docs = {
            "ある": {"title": "ある", "extra": {"lat": 35.0, "lon": 135.0}},
            "ない": {"title": "ない"},
        }

        counts = partition.counts_of(spec, ledger, docs)

        assert counts[partition.HOMELESS] == 1
        assert not partition.outgrown(spec, counts)


class TestPoolingTheSmallCategories:
    """**範囲で区切っていない小さい分類**を、値を並べた 1 区画に寄せ集める。

    隣とつなぐ道(`_joined`)は分類をまたげないので、1 人しかいない国はその 1 人で
    1 区画のまま残っていた —— その 1 人のために巡回の 1 回ぶんの枠を使うことになる。
    """

    def spec(self, target=10):
        return partition.normalize(
            {"by": "band", "prefix": "地域:", "value": "年代:", "target": target}
        )

    def row(self, key, count, visits=None):
        return {"key": key, "count": count, "visits": dict(visits or {})}

    def test_they_become_one_partition_that_lists_the_values(self):
        ledger = [
            self.row("アゼルバイジャン|-", 1),
            self.row("アルメニア|-", 2),
            self.row("エストニア|-", 1),
        ]

        got = partition.merged(self.spec(), ledger)

        assert [p["key"] for p in got] == ["アゼルバイジャン|アルメニア|エストニア|-"]
        assert got[0]["count"] == 4

    def test_an_old_closed_key_is_pooled_too(self):
        """割り直す前の台帳(端も閉じた鍵)にも効く —— そこが 1 帯なら受け持ちは同じ。"""
        ledger = [
            self.row("アゼルバイジャン|1958-1958", 1),
            self.row("アルメニア|1880-1902", 2),
        ]

        got = partition.merged(self.spec(), ledger)

        assert [p["key"] for p in got] == ["アゼルバイジャン|アルメニア|-"]

    def test_a_category_split_by_range_is_never_pooled(self):
        """**範囲で区切っている分類は混ぜない。** 混ぜると「この範囲の全員」が
        並ばなくなり、別の帯にいる人を漏れとして挙げることになる。

        帯 1 本ずつは小さくても、2 本あるかぎり触らない(隣とつないで 1 本に
        なったなら、そのときは「区切っていない分類」として寄せてよい)。
        """
        ledger = [
            self.row("フランス|-1849", 5),
            self.row("フランス|1850-", 5),
            self.row("アルメニア|-", 1),
        ]

        got = partition.merged(self.spec(), ledger)

        assert "アルメニア|-" in [p["key"] for p in got]
        assert not any("フランス" in p["key"] and "アルメニア" in p["key"] for p in got)

    def test_a_big_category_is_left_alone(self):
        """1 つで 1 区画ぶんの仕事があるものは、そのまま。"""
        ledger = [self.row("日本|-", 40), self.row("アルメニア|-", 1)]

        got = partition.merged(self.spec(), ledger)

        assert sorted(p["key"] for p in got) == ["アルメニア|-", "日本|-"]

    def test_it_does_not_mix_laps(self):
        """見ていないぶんが「見終えた」に混ざらないこと(隣とつなぐのと同じ理由)。"""
        ledger = [
            self.row("アゼルバイジャン|-", 1, {"ざっと": "2026-09-01"}),
            self.row("アルメニア|-", 1),
        ]

        got = partition.merged(self.spec(), ledger)

        assert len(got) == 2
        assert [p["visits"] for p in got] == [{"ざっと": "2026-09-01"}, {}]

    def test_it_pools_within_each_lap_even_when_they_alternate(self):
        """**記録ごとに分けてから寄せる。** 並び順のまま見ると、見た分類と
        見ていない分類が交互に並んだ一周の途中では 1 つも寄せられない。"""
        seen = {"ざっと": "2026-09-01"}
        ledger = [
            self.row("あ|-", 1, seen),
            self.row("い|-", 1),
            self.row("う|-", 1, seen),
            self.row("え|-", 1),
        ]

        got = partition.merged(self.spec(), ledger)

        assert sorted(p["key"] for p in got) == ["あ|う|-", "い|え|-"]
        assert {tuple(sorted(p["visits"].items())) for p in got} == {
            tuple(sorted(seen.items())), (),
        }

    def test_it_stops_before_the_partition_gets_big(self):
        ledger = [self.row(f"くに{i:02d}|-", 5) for i in range(10)]

        got = partition.merged(self.spec(target=10), ledger)

        # target 10 の 8 割に満たないあいだだけ足すので、1 区画は 5 人まで
        assert all(p["count"] < 10 * partition.MERGE_RATIO for p in got)
        assert sum(p["count"] for p in got) == 50

    def test_it_stops_before_the_key_gets_unreadable(self):
        """数では届いていなくても、**並べてよい値の数**で頭打ちにする。"""
        ledger = [self.row(f"くに{i:02d}|-", 1) for i in range(partition.MAX_BAND_VALUES + 5)]

        got = partition.merged(self.spec(target=1000), ledger)

        assert len(got) == 2
        assert len(partition.parse_band_key(got[0]["key"])[0]) == partition.MAX_BAND_VALUES

    def test_a_lonely_one_keeps_its_key(self):
        """寄せる相手がいなければ書き換えない(次の割り直しでどのみち揃う)。"""
        ledger = [self.row("アルメニア|1880-1902", 1)]

        assert [p["key"] for p in partition.merged(self.spec(), ledger)] == ["アルメニア|1880-1902"]

    def test_a_doc_lands_in_the_pooled_partition(self):
        spec = self.spec()
        got = partition.merged(spec, [self.row("アルメニア|-", 1), self.row("エストニア|-", 1)])

        for name in ("アルメニア", "エストニア"):
            doc = {"title": "ひと", "tags": [f"地域:{name}", "年代:1900"]}
            assert partition.partition_of(spec, got, doc) == got[0]["key"]

    def test_a_doc_from_outside_the_pool_stays_outside(self):
        spec = self.spec()
        got = partition.merged(spec, [self.row("アルメニア|-", 1), self.row("エストニア|-", 1)])
        doc = {"title": "ひと", "tags": ["地域:ラトビア", "年代:1900"]}

        assert partition.partition_of(spec, got, doc) is None

    def test_the_description_names_every_value_it_holds(self):
        """**どこまでがこの区画かが分からないと、漏れを問えない。**"""
        spec = self.spec()
        where = partition.describe(spec, "アルメニア|エストニア|-", {})

        assert "アルメニア・エストニア" in where
        assert "問いません" in where

    def test_growing_out_of_the_pool_keeps_the_lap(self):
        """寄せ集めから独り立ちしたら、寄せ集めの記録を引き継ぐ ——
        引き継がないと、育つたびにそこだけ一周が巻き戻る。"""
        spec = self.spec()
        current = [{"key": "アルメニア|エストニア|-", "count": 4,
                    "visits": {"ざっと": "2026-09-01"}}]
        built = [{"key": "アルメニア|-", "count": 30}, {"key": "エストニア|-", "count": 1}]

        got = partition.refresh(built, current, spec)

        assert [p["visits"] for p in got] == [
            {"ざっと": "2026-09-01"}, {"ざっと": "2026-09-01"}
        ]
        assert "アルメニア|エストニア|-" not in [p["key"] for p in got], "吸収先があるので残さない"

    def test_it_settles_instead_of_churning(self):
        """同じ台帳を何度通しても同じ形に落ち着くこと(鍵が動くたび記録が消える)。"""
        spec = self.spec()
        ledger = [self.row("アルメニア|-", 1), self.row("エストニア|-", 1),
                  self.row("日本|-", 40)]

        once = partition.merged(spec, ledger)
        twice = partition.merged(spec, once)

        assert [p["key"] for p in once] == [p["key"] for p in twice]

    def test_a_separator_inside_a_value_cannot_split_a_category_in_two(self):
        """**鍵は「分類を並べて、最後に範囲」**なので、値に区切りが混ざると
        分類が 2 つに割れて読まれる —— `地域:A|B` の人が入るはずの区画へ
        `地域:A` の人が落ちる。値のほうを潰しておく。
        """
        spec = self.spec()
        own = {
            "むこう": {"title": "むこう", "tags": ["地域:A|B", "年代:1900"]},
            "こっち": {"title": "こっち", "tags": ["地域:A", "年代:1950"]},
        }
        built = partition.build(spec, {}, own)

        assert sorted(p["key"] for p in built) == ["A/B|-", "A|-"]
        assert partition.partition_of(spec, built, own["むこう"]) == "A/B|-"
        assert partition.partition_of(spec, built, own["こっち"]) == "A|-"

    def test_a_tag_split_is_left_alone(self):
        spec = partition.normalize({"by": "tag", "prefix": "地域:", "target": 10})
        ledger = [self.row("地域:アルメニア", 1), self.row("地域:エストニア", 1)]

        assert len(partition.merged(spec, ledger)) == 2


class TestNarrowingTheRowsToRead:
    """**区画を 1 つ開くのに全文書を読んでいた**(`partition.narrowing`)。

    本番の食事処(686,602 件)で、いちばん小さい区画を開くのに 48.9 秒 ——
    中身の件数は関係なく、全部が「全表を本文ごと dict に読む」ぶんだった。

    **狭すぎるのだけが危ない。** 最後の判定は `locator` が行うので、広すぎる
    ぶんには結果が変わらない —— 迷うところは絞らない側へ倒す。
    """

    def test_a_box_is_exact(self):
        """矩形は判定が「鍵の矩形の中にあるか」そのものなので厳密に絞れる。"""
        key = partition.geo_key((35.0, 139.0, 36.0, 140.0))
        where, args = partition.narrowing(
            {"by": "geo"}, [{"key": key}], key, schema_version=4,
        )

        assert "doc_coords" in where
        assert args == (35.0, 36.0, 139.0, 140.0)

    def test_an_old_database_is_not_narrowed(self):
        """`doc_coords` は schema_version 4 から。無い DB に書くと落ちる。"""
        key = partition.geo_key((35.0, 139.0, 36.0, 140.0))

        assert partition.narrowing(
            {"by": "geo"}, [{"key": key}], key, schema_version=3,
        ) == ("", ())

    def test_a_tag_is_exact(self):
        where, args = partition.narrowing(
            {"by": "tag", "tag": "ラーメン"}, [{"key": "ラーメン"}], "ラーメン",
            schema_version=3,
        )

        assert "doc_tags" in where and args == ("ラーメン",)

    def test_titles_are_cut_at_the_next_start_not_at_their_own_end(self):
        """**鍵の終わりで切ってはいけない。** 判定は「始まりが自分以下のうち
        いちばん後ろ」なので、区画は次の始まりの手前までを引き受ける ——
        鍵の終わりで切ると、隙間に落ちた見出しを取りこぼす。
        """
        led = [{"key": partition.title_key("あ", "か")},
               {"key": partition.title_key("さ", "な")}]

        where, args = partition.narrowing({"by": "title"}, led, led[0]["key"])

        # 先頭なので下限なし。上限は「か」ではなく次の始まりの「さ」
        assert where == " AND title < ?"
        assert args == ("さ",)

    def test_the_last_title_partition_has_no_ceiling(self):
        led = [{"key": partition.title_key("あ", "か")},
               {"key": partition.title_key("さ", "な")}]

        where, args = partition.narrowing({"by": "title"}, led, led[1]["key"])

        assert where == " AND title >= ?"
        assert args == ("さ",)

    def test_a_band_is_left_alone(self):
        """分類の値は鍵に入るときに区切りの字を落としてある(`tag_value` が
        `|` を `/` にする)ので、**鍵からもとのタグを組み直せない** ——
        組み直したつもりで絞ると、その分類の一部が黙って消える。
        """
        key = partition.band_key("日本", 1800, 1900)

        assert partition.narrowing(
            {"by": "band", "prefix": "地域", "value": "年代"}, [{"key": key}], key,
            schema_version=4,
        ) == ("", ())

    def test_a_key_the_ledger_does_not_have_is_left_alone(self):
        """範囲を決める手がかりが無い。絞らないほうへ倒す。"""
        led = [{"key": partition.title_key("あ", "か")}]

        assert partition.narrowing({"by": "title"}, led, "知らない〜鍵") == ("", ())

    def test_nothing_is_narrowed_without_a_spec(self):
        assert partition.narrowing(None, [], "鍵") == ("", ())

    def test_it_never_drops_a_member(self):
        """**これが要。** 絞ったあとに `locator` を通した結果が、絞らずに通した
        結果と一致すること —— 狭すぎる絞りは、その区画の一部を黙って消す。
        """
        led = [{"key": partition.title_key("あ", "か")},
               {"key": partition.title_key("さ", "な")},
               {"key": partition.title_key("ま", "ん")}]
        find = partition.locator({"by": "title"}, led)
        docs = [{"title": t} for t in
                ["A", "あ", "え", "き", "さ", "た", "ぬ", "ま", "も", "ん", "ゑ"]]

        for p in led:
            here = {d["title"] for d in docs if find(d) == p["key"]}
            where, args = partition.narrowing({"by": "title"}, led, p["key"])
            lo = args[0] if " AND title >= ?" in where else None
            hi = args[-1] if " AND title < ?" in where else None
            kept = {
                d["title"] for d in docs
                if (lo is None or d["title"] >= lo) and (hi is None or d["title"] < hi)
            }
            assert here <= kept, f"{p['key']} で {here - kept} を取りこぼす"


class TestAnOldPartitionThatStraddlesTheNewOnes:
    """**割り直しで境目が動くと、古い帯が新しい帯 2 本にまたがる。**

    どちらの向きにも含まれないので「行き場が無い」と判ぜられ、台帳に残っていた。
    **残ると重なる**: 所属は「始まりが自分以下のうちいちばん後ろ」で決まるので、
    1922 生まれは新しい `1914-1927` ではなく古い `1922-1932` に落ちる。件数は
    新しい側で数えるから、**古い側は「母集団 0」と出るのに中身がある**
    (本番で、0 と表示された帯に 16 人いた)。しかも空の区画は割り直しの引き金
    なので、押すたびに重なりが増えていた。
    """

    @staticmethod
    def _spec():
        return partition.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 25}
        )

    def test_a_straddling_band_is_dropped(self):
        built = [{"key": partition.band_key("アメリカ合衆国", 1895, 1913)},
                 {"key": partition.band_key("アメリカ合衆国", 1914, 1927)}]
        current = [{"key": partition.band_key("アメリカ合衆国", 1903, 1916)}]

        kept = partition._uncovered(self._spec(), built, current)

        assert kept == []

    def test_a_category_the_new_ledger_lost_is_kept(self):
        """**引き受け先が本当に無いぶんは残す** —— 消すと、そこへ「足すべきものが
        無いか」を問う回ごと無くなる(漏れを探す仕事はそこにしか無い)。"""
        built = [{"key": partition.band_key("アメリカ合衆国", 1895, 1913)}]
        current = [{"key": partition.band_key("フランドル", 1556, 1615)}]

        kept = partition._uncovered(self._spec(), built, current)

        assert [p["key"] for p in kept] == [partition.band_key("フランドル", 1556, 1615)]

    def test_a_pooled_key_needs_every_category_served(self):
        """寄せ集めの区画が持つ分類のうち 1 つでも新しい台帳に無ければ、
        その分類の文書は行き場を失う。"""
        built = [{"key": partition.band_key("アルメニア", 1900, 1950)}]
        current = [{"key": partition.band_key(["アルメニア", "エストニア"], None, None)}]

        kept = partition._uncovered(self._spec(), built, current)

        assert len(kept) == 1

    def test_the_unknown_pile_is_counted_apart_from_the_bands(self):
        """値の分かる帯と「不明」の置き場は落ちる先が別なので、別に数える
        (`_served` の判定。残すかどうかは `_only_a_landing_place` が別に決める)。"""
        built = [{"key": partition.band_key("アメリカ合衆国", 1900, 1950)}]

        assert not partition._served(self._spec(), built, "アメリカ合衆国|不明|あ〜ん")

    def test_a_straddling_box_is_still_kept(self):
        """**矩形はいままでどおり。** 鍵の矩形の中にある点だけを引き受けるので、
        はみ出したぶんは本当にどこにも入らない。"""
        spec = partition.normalize({"by": "geo", "target": 150,
                                    "bbox": [20.0, 120.0, 46.0, 154.0]})
        built = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0))}]
        current = [{"key": partition.geo_key((35.5, 139.5, 36.5, 140.5))}]

        assert len(partition._uncovered(spec, built, current)) == 1


class TestAnEmptyLandingPlaceIsNotKept:
    """**値を持たないものの置き場は、空なら残す意味が無い。**

    残すのは「この範囲に足すべきものが無いか」を問う回を失わないためだが、
    置き場にはその問いが立たない —— 「生年の分からない画家で、漏れている人は
    いますか」も「地域の分からない画家で〜」も問いとして成立しない
    (漏れている画家は生年か地域のどちらかで探すもので、分からない側からは
    探しに行けない)。本番の名簿で、中身が全部墓標の置き場が 4 つ残っていた。
    """

    @staticmethod
    def _spec():
        return partition.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 25}
        )

    def test_an_empty_unknown_pile_is_dropped(self):
        built = [{"key": partition.band_key("イギリス", 1900, 1950)}]
        current = [{"key": "イギリス|不明|あ〜ん"}]

        assert partition._uncovered(self._spec(), built, current) == []

    def test_an_empty_catch_all_category_is_dropped(self):
        """`other` は分類のタグを持たない文書が落ちる先。そこも探す場所ではない。"""
        spec = self._spec()
        built = [{"key": partition.band_key("イギリス", 1900, 1950)}]
        current = [{"key": partition.band_key(spec["other"], 326, 1963)}]

        assert partition._uncovered(spec, built, current) == []

    def test_a_real_category_is_still_kept(self):
        """**本物の分類には問いが立つ** —— 「フランドルの 1556-1615 生まれで
        漏れている画家はいますか」は答えのある問い。"""
        built = [{"key": partition.band_key("イギリス", 1900, 1950)}]
        current = [{"key": partition.band_key("フランドル", 1556, 1615)}]

        assert len(partition._uncovered(self._spec(), built, current)) == 1

    def test_a_pool_with_a_real_category_is_kept(self):
        """寄せ集めに本物の分類が 1 つでも混じっていれば、そちらには問いが立つ。"""
        spec = self._spec()
        built = [{"key": partition.band_key("イギリス", 1900, 1950)}]
        current = [{"key": partition.band_key([spec["other"], "マルタ"], None, None)}]

        assert len(partition._uncovered(spec, built, current)) == 1

    def test_a_pile_with_living_docs_is_built_by_the_ledger_anyway(self):
        """**中身のある置き場は消えない** —— ここが効くのは新しい台帳が作らなかった
        区画だけで、生きている文書がいれば台帳の側が作る(回る値打ちもある ——
        そこにいる人の生年を埋めてもらう回になる)。"""
        built = [{"key": "イギリス|不明|あ〜ん"}]
        current = [{"key": "イギリス|不明|あ〜ん"}]

        assert partition._uncovered(self._spec(), built, current) == []


class TestWhereABoxRoughlyIs:
    """**矩形の鍵は数字が並ぶだけで、人には場所が読めない。**

    一覧で「これはどこか」を確かめるのに、いちいち開くことになっていた。
    **割るときに所属行政区(`extra.area`)を拾っておく** —— 区画は数千あるので、
    描くたびに 1 行ずつ問い合わせると、それだけで面が重くなる。
    """

    SPEC: ClassVar[dict] = {"by": "geo", "target": 40, "bbox": [20.2, 122.5, 45.8, 154.0]}

    @staticmethod
    def _docs(area: str, lat: float, lon: float, n: int) -> dict:
        return {
            f"{area}{i}": {"extra": {"lat": lat + i * 0.001, "lon": lon, "area": area}}
            for i in range(n)
        }

    def test_the_box_says_roughly_where_it_is(self):
        own = {**self._docs("沖縄県", 26.2, 127.68, 60),
               **self._docs("東京都", 35.68, 139.76, 60)}

        built = partition.build(partition.normalize(self.SPEC), {}, own)

        assert {p.get("area") for p in built} == {"沖縄県", "東京都"}

    def test_a_box_across_a_border_names_both(self):
        """**1 つだと県境をまたぐ矩形で嘘になる**ので、多いほうから 2 つまで。"""
        own = {**self._docs("東京都", 35.68, 139.76, 30),
               **self._docs("神奈川県", 35.44, 139.63, 20)}

        # 割れずに 1 区画で収まる目安にして、1 つの矩形が両県を抱える形を作る
        spec = partition.normalize({**self.SPEC, "target": 100})
        [box] = partition.build(spec, {}, own)

        assert box["area"] == "東京都・神奈川県"

    def test_a_population_without_areas_is_fine(self):
        """**添え物なので、無くても区画は成り立つ**(座標と違う)。"""
        own = {f"x{i}": {"extra": {"lat": 35.0 + i * 0.001, "lon": 139.0}} for i in range(10)}

        [box] = partition.build(partition.normalize({**self.SPEC, "target": 100}), {}, own)

        assert "area" not in box

    def test_it_is_filled_in_without_a_re_cut(self):
        """**割り直さない回にも要る。** 台帳を組み直すのは「育った」「空になった」が
        起きたときだけなので、**健全な台帳では `build` が走らない** —— そちらでしか
        札を付けていなかった頃は、「区画を割り直す」を押しても県名が出なかった
        (本番で 8,185 区画すべて空欄のまま)。
        """
        spec = partition.normalize(self.SPEC)
        led = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0)), "count": 0}]
        own = {**self._docs("東京都", 35.5, 139.5, 5),
               **self._docs("神奈川県", 35.6, 139.6, 3)}

        counts, areas = partition.tally(spec, led, own)
        [kept] = partition.counted(led, counts, areas)

        assert kept["area"] == "東京都・神奈川県"

    def test_a_third_area_is_not_hidden(self):
        """**黙って 2 つに丸めると「この区画は 2 県ぶん」と読まれる。**"""
        spec = partition.normalize(self.SPEC)
        led = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0)), "count": 0}]
        own = {**self._docs("東京都", 35.5, 139.5, 5),
               **self._docs("神奈川県", 35.6, 139.6, 3),
               **self._docs("埼玉県", 35.7, 139.7, 2)}

        _counts, areas = partition.tally(spec, led, own)

        assert areas[led[0]["key"]] == "東京都・神奈川県 ほか1"

    def test_a_band_ledger_is_not_labelled(self):
        """帯や見出しの区画に県名は要らない(場所で割っていない)。"""
        spec = partition.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 25}
        )
        led = [{"key": partition.band_key("日本", 1900, 1950), "count": 0}]
        own = {"x": {"title": "x", "tags": ["地域:日本", "年代:1920"],
                     "extra": {"area": "東京都"}}}

        _counts, areas = partition.tally(spec, led, own)

        assert areas == {}

    def test_a_partition_nobody_lives_in_keeps_its_old_label(self):
        """**数えられなかったぶんは前の札を残す** —— 消すと、母集団が一時的に
        読めなかっただけで札が飛ぶ。"""
        led = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0)),
                "count": 3, "area": "東京都"}]

        [kept] = partition.counted(led, {led[0]["key"]: 0}, {})

        assert kept["area"] == "東京都"

    def test_it_survives_a_re_cut(self):
        """割り直しても運ぶ(`refresh`)—— 入れ直すために引き直さない。"""
        built = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0)),
                  "count": 3, "area": "東京都"}]

        [kept] = partition.refresh(built, [], partition.normalize(self.SPEC))

        assert kept["area"] == "東京都"

    def test_joining_two_boxes_keeps_both_names(self):
        """つないだ先は両方を引き受けるので、両方の名前を残す。"""
        spec = partition.normalize({**self.SPEC, "target": 100})
        pair = [
            {"key": partition.geo_key((35.0, 139.0, 35.5, 140.0)), "count": 1,
             "area": "東京都", "visits": {}},
            {"key": partition.geo_key((35.5, 139.0, 36.0, 140.0)), "count": 1,
             "area": "埼玉県", "visits": {}},
        ]

        [one] = partition.merged(spec, pair)

        assert one["area"] == "東京都・埼玉県"

    def test_the_ledger_shows_it(self):
        from app.views import admin

        assert "東京都" in admin._where_html({"key": "x", "area": "東京都"})
        assert admin._where_html({"key": "x"}) == ""


def _only(ledger, key):
    """組み直した台帳から、その鍵の行だけ取る。

    `refresh` は**覆われていない古い区画も後ろに足す**(`_uncovered`)ので、
    返る行は 1 つとは限らない —— 見たいのは新しく組んだほう。
    """
    return next(p for p in ledger if p["key"] == key)


class TestTheMarksSurviveARecut:
    """**割り直すと境目が動くので、新しい区画が古い区画 2 本にまたがる。**

    どちらにも含まれないので親が見つからず、**両方とも見終えていたのに印が
    落ちていた** —— 落ちた区画は「まだ」に戻るので、一周がそのぶん巻き戻り、
    AI をもう一度呼ぶ。`_uncovered` で直したのと同じ取り違えが、印を継ぐ側に
    残っていた。
    """

    OLD = "2026-09-18T04:00:00+00:00"
    NEW = "2026-09-20T04:00:00+00:00"

    @staticmethod
    def _spec():
        return partition.normalize(
            {"by": "band", "prefix": "地域", "value": "年代", "target": 25}
        )

    def _seen(self, when: str) -> dict:
        return {"ざっと見る": when}

    def test_a_key_that_did_not_change_keeps_its_mark(self):
        key = partition.band_key("日本", 1900, 1950)
        kept = partition.refresh(
            [{"key": key}], [{"key": key, "visits": self._seen(self.NEW)}], self._spec(),
        )

        assert kept[0]["visits"] == self._seen(self.NEW)

    def test_a_child_still_inherits_from_its_parent(self):
        """**含む区画は重なる区画の一種**なので、今までの形はそのまま残る。"""
        parent = [{"key": partition.band_key("日本", 1900, 1950),
                   "visits": self._seen(self.NEW)}]
        built = [{"key": partition.band_key("日本", 1900, 1924)},
                 {"key": partition.band_key("日本", 1925, 1950)}]

        kept = partition.refresh(built, parent, self._spec())

        assert [p["visits"] for p in kept] == [self._seen(self.NEW)] * 2

    def test_straddling_two_seen_bands_keeps_the_mark(self):
        """**これが落ちていた。** 元は 2 本とも見終えているのに空になっていた。"""
        current = [{"key": partition.band_key("日本", 1895, 1913),
                    "visits": self._seen(self.OLD)},
                   {"key": partition.band_key("日本", 1914, 1927),
                    "visits": self._seen(self.NEW)}]
        built = [{"key": partition.band_key("日本", 1903, 1916)}]

        [kept] = partition.refresh(built, current, self._spec())

        # **いちばん古い時刻を採る** —— 次に見るのは古い順なので、新しいほうを
        # 採ると「まだ見ていないに等しい範囲」が後回しになる
        assert kept["visits"] == self._seen(self.OLD)

    def test_one_unseen_band_in_the_way_stops_it(self):
        """**見ていないぶんが「見終えた」に混ざる**のがいちばん困る(一周が嘘になる)。"""
        current = [{"key": partition.band_key("日本", 1895, 1913),
                    "visits": self._seen(self.NEW)},
                   {"key": partition.band_key("日本", 1914, 1927), "visits": {}}]
        built = [{"key": partition.band_key("日本", 1903, 1916)}]

        kept = _only(partition.refresh(built, current, self._spec()), built[0]["key"])

        assert kept["visits"] == {}

    def test_another_category_does_not_lend_its_mark(self):
        current = [{"key": partition.band_key("フランス", 1900, 1950),
                    "visits": self._seen(self.NEW)}]
        built = [{"key": partition.band_key("日本", 1900, 1950)}]

        kept = _only(partition.refresh(built, current, self._spec()), built[0]["key"])

        assert kept["visits"] == {}

    def test_the_unknown_pile_does_not_lend_to_a_band(self):
        """値の分かる帯と「不明」の置き場は落ちる先が別。"""
        current = [{"key": "日本|不明|あ〜ん", "visits": self._seen(self.NEW)}]
        built = [{"key": partition.band_key("日本", 1900, 1950)}]

        kept = _only(partition.refresh(built, current, self._spec()), built[0]["key"])

        assert kept["visits"] == {}

    def test_boxes_that_overlap_lend_their_mark(self):
        spec = partition.normalize({"by": "geo", "target": 150,
                                    "bbox": [20.0, 120.0, 46.0, 154.0]})
        current = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0)),
                    "visits": self._seen(self.NEW)}]
        built = [{"key": partition.geo_key((35.5, 139.5, 36.5, 140.5))}]

        kept = _only(partition.refresh(built, current, spec), built[0]["key"])

        assert kept["visits"] == self._seen(self.NEW)

    def test_a_box_that_does_not_touch_lends_nothing(self):
        spec = partition.normalize({"by": "geo", "target": 150,
                                    "bbox": [20.0, 120.0, 46.0, 154.0]})
        current = [{"key": partition.geo_key((35.0, 139.0, 36.0, 140.0)),
                    "visits": self._seen(self.NEW)}]
        built = [{"key": partition.geo_key((40.0, 141.0, 41.0, 142.0))}]

        kept = _only(partition.refresh(built, current, spec), built[0]["key"])

        assert kept["visits"] == {}
