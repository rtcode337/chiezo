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
        """**分類はまたがない。** またぐと、その区画がどの範囲なのかを言えなくなる ——
        範囲が言えなければ、漏れを問うこと自体が成り立たない。
        """
        spec = partition.normalize({"by": "band", "prefix": "地域", "value": "年代", "target": 10})
        ledger = [
            {"key": partition.band_key("日本", 1800, 1850), "count": 1, "visits": {}},
            {"key": partition.band_key("朝鮮", 1288, 1900), "count": 1, "visits": {}},
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
            *[(f"ふ{i}", ["地域:フランス", f"年代:{1840 + i}-1926"]) for i in range(10)],
            ("に", ["地域:日本", "年代:1840-1900"]),
        )
        built = partition.build(self.spec(), {}, own)
        keys = [p["key"] for p in built]

        assert "フランス|1840-1849" in keys
        assert "日本|1840-1840" in keys

    def test_a_sparse_category_gets_a_wide_band(self):
        """人の少ないところは、年の幅が自然に広がる。"""
        own = self.docs(
            ("あ", ["地域:カナダ", "年代:1810-1880"]),
            ("い", ["地域:カナダ", "年代:1866-1950"]),
        )
        built = partition.build(self.spec(), {}, own)

        # 2 人しかいないので、1 区画が 56 年ぶんを覆う
        assert [p["key"] for p in built] == ["カナダ|1810-1866"]

    def test_one_number_is_never_split(self):
        """**「フランスの 1660 年生まれ」を 2 つに分けない。** 分けると
        「この範囲の全員」が並ばなくなり、漏れを探す問いが成り立たない。"""
        own = self.docs(*[(f"ひと{i}", ["地域:日本", "年代:1907-1990"]) for i in range(25)])
        built = partition.build(self.spec(), {}, own)

        assert [p["key"] for p in built] == ["日本|1907-1907"]
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

        assert [p["key"] for p in built] == ["アイルランド|1700-1928"]
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
