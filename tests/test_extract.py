"""抽出の指定 —— AI ではなく、手元の長期記憶から機械的に 1 回ぶんを作る。

見ているのは 2 つ。**指定どおりに引けること**と、**壊れた指定を作る時点で断ること**
(実行時に落ちると、無人で回っている最中に「集められなかった」だけが残る)。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from fastapi import HTTPException

from app import extract


def run(spec, sources):
    """`extract.run` を読み切って返す。

    本番は 1 行ずつ流す(名簿が数十万件になるので丸ごとは持てない)が、テストは
    丸ごと見たい —— 置き場は読み切ってから片づける。
    """
    roster, cursor = extract.run(spec, sources)
    try:
        return list(roster), cursor
    finally:
        if hasattr(roster, "close"):
            roster.close()

@pytest.fixture
def source(tmp_path):
    """タグで引けるソース(焼き上がりと同じコアスキーマ)を作る。

    並び順は `rank_score` の降順。「有名なほうから N 件」はソース側が既に
    持っている順で、抽出の指定には並べ替えを書かせない。
    """
    from app import notes
    from app.registry import Source

    def make(docs, name="jawiki"):
        path = tmp_path / f"{name}.db"
        conn = sqlite3.connect(path)
        conn.executescript(notes.SCHEMA_DDL)
        for doc_id, doc in enumerate(docs, start=1):
            conn.execute(
                "INSERT INTO docs (doc_id, title, opening, body, tags, extra, links,"
                " updated_at, rank_score)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, '2026-01-01T00:00:00+00:00', ?)",
                (
                    doc_id,
                    doc["title"],
                    doc.get("opening", doc["title"] + "の要約。"),
                    doc.get("body", doc["title"] + "の本文。"),
                    json.dumps(doc.get("tags", []), ensure_ascii=False),
                    json.dumps(doc["extra"], ensure_ascii=False) if doc.get("extra") else None,
                    json.dumps(doc.get("links", []), ensure_ascii=False),
                    doc.get("rank", 0.0),
                ),
            )
            for tag in doc.get("tags", []):
                conn.execute(
                    "INSERT INTO doc_tags (tag, doc_id) VALUES (?, ?)", (tag, doc_id)
                )
                # 焼き上がりには集計表も入る。**末尾一致はここを引く**
                # （転置表は jawiki で 764 万行あり、舐めさせるわけにいかない）
                conn.execute(
                    "INSERT INTO tag_counts (tag, docs) VALUES (?, 1)"
                    " ON CONFLICT(tag) DO UPDATE SET docs = docs + 1",
                    (tag,),
                )
        conn.commit()
        conn.close()
        return {
            name: Source(
                name=name,
                kind="wikipedia",
                lang="ja",
                dump_date="20260101",
                schema_version=4,
                built_at="2026-01-01T00:00:00+00:00",
                doc_count=len(docs),
                path=Path(path),
            )
        }

    return make


def painters():
    return [
        {
            "title": "クロード・モネ",
            "tags": ["印象派の画家", "19世紀フランスの画家", "1840年生", "1926年没"],
            "extra": {"wikidata": "Q296"},
            "rank": 0.9,
        },
        {
            "title": "エドゥアール・マネ",
            "tags": ["印象派の画家", "1832年生", "1883年没"],
            "rank": 0.7,
        },
        {
            "title": "葛飾北斎",
            "tags": ["江戸時代の画家", "1760年生", "1849年没"],
            "rank": 0.8,
        },
    ]


def spec(**overrides):
    base = {
        "source": "jawiki",
        "tag": "印象派の画家",
        "limit": 30,
        "tags": [
            {"const": "画家"},
            {"patterns": [r"^(\d{3,4})年生$", r"^(\d{3,4})年没$"], "format": "年代:{1}-{2}"},
            {"pattern": "^(.+)派の画家$", "format": "様式:{1}派"},
        ],
    }
    return extract.normalize({**base, **overrides})


class TestPullingFromWhatIsAlreadyThere:
    def test_it_takes_only_the_tag_that_was_asked_for(self, source):
        items, _cursor = run(spec(), source(painters()))

        # 江戸時代の画家は入らない（同じ「画家」でも頼まれていない）
        assert [i["title"] for i in items] == ["クロード・モネ", "エドゥアール・マネ"]

    def test_it_keeps_the_order_the_source_already_has(self, source):
        """有名な順はソースが持っている（ページビュー由来の rank_score）。"""
        items, _cursor = run(spec(tag="1840年生,1832年生,1760年生"), source(painters()))

        assert [i["title"] for i in items] == ["クロード・モネ", "葛飾北斎", "エドゥアール・マネ"]

    def test_it_takes_everything_when_no_number_was_given(self, source):
        items, _cursor = run(spec(limit=0, tag="1840年生,1832年生,1760年生"), source(painters()))

        assert len(items) == 3

    def test_it_stops_at_the_limit(self, source):
        items, _cursor = run(spec(limit=1), source(painters()))

        assert [i["title"] for i in items] == ["クロード・モネ"]

    def test_two_tags_become_one_range(self, source):
        """生年と没年は別々のタグ。読む側が要るのは年代なので、ここで 1 つにする。"""
        items, _cursor = run(spec(), source(painters()))

        assert "年代:1840-1926" in items[0]["tags"]

    def test_a_half_match_makes_nothing(self, source):
        """没年が無ければ年代を作らない（「1840-」のような半端を残さない）。"""
        docs = [{"title": "存命の画家", "tags": ["印象派の画家", "1940年生"], "rank": 0.5}]

        items, _cursor = run(spec(), source(docs))

        assert not [t for t in items[0]["tags"] if t.startswith("年代:")]

    def test_a_rule_can_hit_more_than_once(self, source):
        docs = [
            {
                "title": "両方の画家",
                "tags": ["印象派の画家", "象徴派の画家", "1840年生", "1900年没"],
                "rank": 0.5,
            }
        ]

        items, _cursor = run(spec(), source(docs))

        assert "様式:印象派" in items[0]["tags"]
        assert "様式:象徴派" in items[0]["tags"]

    def test_it_can_leave_out_what_it_does_not_want(self, source):
        """カテゴリは持ち主の職業を選ばない。

        「画家」のカテゴリには絵も描く俳優や作家が入っていて、人気の順に取ると
        そちらが先に並ぶ（実際に俳優が 2 人、上位 30 人に入った）。
        """
        docs = [
            {"title": "絵も描く俳優", "tags": ["印象派の画家", "アメリカ合衆国の男優"], "rank": 0.95},
            {"title": "本職の画家", "tags": ["印象派の画家", "1840年生", "1926年没"], "rank": 0.5},
        ]

        items, _cursor = run(
            spec(not_tag="アメリカ合衆国の男優"), source(docs)
        )

        assert [i["title"] for i in items] == ["本職の画家"]

    def test_it_can_pull_a_work_from_another_article(self, source):
        """「クロード・モネの作品」のようなカテゴリから、人気の順に代表作を取る。"""
        docs = [
            {"title": "クロード・モネ", "tags": ["印象派の画家"], "rank": 0.9},
            {"title": "睡蓮", "tags": ["クロード・モネの作品"], "rank": 0.3},
            {"title": "印象・日の出", "tags": ["クロード・モネの作品"], "rank": 0.8},
        ]

        items, _cursor = run(
            spec(tags=[{"from_tag": "{title}の作品", "format": "代表作:{1}", "take": 1}]),
            source(docs),
        )

        assert items[0]["tags"] == ["代表作:印象・日の出"]

    def test_a_painter_without_a_works_category_gets_nothing(self, source):
        items, _cursor = run(
            spec(tags=[{"from_tag": "{title}の作品", "format": "代表作:{1}"}]),
            source([{"title": "作品の無い画家", "tags": ["印象派の画家"], "rank": 0.5}]),
        )

        assert items[0]["tags"] == []

    def test_it_can_read_the_links_between_what_it_pulled(self, source):
        """**相互リンクだけ**を見る。片側だと有名どうしが軒並み繋がって毛玉になる。"""
        docs = [
            {"title": "マネ", "tags": ["印象派の画家"], "links": ["モネ", "ドガ"], "rank": 0.9},
            {"title": "モネ", "tags": ["印象派の画家"], "links": ["マネ"], "rank": 0.8},
            {"title": "ドガ", "tags": ["印象派の画家"], "links": [], "rank": 0.7},
        ]

        items, _cursor = run(
            spec(tags=[{"linked": "mutual", "format": "関連:{1}"}]), source(docs)
        )

        by_title = {i["title"]: i["tags"] for i in items}
        assert by_title["マネ"] == ["関連:モネ"]
        assert by_title["モネ"] == ["関連:マネ"]
        # 片側しか張っていない相手は出ない
        assert by_title["ドガ"] == []

    def test_links_outside_the_extraction_are_not_edges(self, source):
        """図に出ない相手への線は引かない（Chiezo は図を知らないが、集合は知っている）。"""
        docs = [
            {"title": "画家", "tags": ["印象派の画家"], "links": ["外の人"], "rank": 0.5},
        ]

        items, _cursor = run(
            spec(tags=[{"linked": "mutual", "format": "関連:{1}"}]), source(docs)
        )

        assert items[0]["tags"] == []

    def test_the_original_tags_do_not_come_along(self, source):
        """カテゴリはソースの都合で付いている。読む側に選ばせない。"""
        items, _cursor = run(spec(), source(painters()))

        assert "19世紀フランスの画家" not in items[0]["tags"]
        assert items[0]["tags"][0] == "画家"

    def test_the_source_of_each_item_can_be_built_from_the_title(self, source):
        items, _cursor = run(
            spec(url="https://ja.wikipedia.org/wiki/{title}"), source(painters())
        )

        assert items[0]["url"] == "https://ja.wikipedia.org/wiki/クロード・モネ"

    def test_extra_can_fill_the_source_too(self, source):
        items, _cursor = run(
            spec(url="https://www.wikidata.org/wiki/{wikidata}"), source(painters())
        )

        assert items[0]["url"] == "https://www.wikidata.org/wiki/Q296"

    def test_the_body_is_the_opening_by_default(self, source):
        """焼くのは要点。全文は元のソースにある。"""
        sources = source(painters())

        assert run(spec(), sources)[0][0]["body"] == "クロード・モネの要約。"
        assert run(spec(body="body"), sources)[0][0]["body"] == "クロード・モネの本文。"

    def test_it_marks_that_the_bulk_pass_is_done(self, source):
        """次の実行は「進み具合が入っている」ほうへ進む（＝ AI が肉付けする）。"""
        sources = source(painters())

        assert run(spec(), sources)[1] == extract.DEFAULT_CURSOR
        # 印は指定側で決められる（依頼した側の言葉で書ける）
        assert run(spec(cursor="粗く作成済み"), sources)[1] == "粗く作成済み"

    def test_an_unknown_source_says_so(self, source):
        with pytest.raises(HTTPException) as got:
            run(spec(source="nosuch"), source(painters()))

        assert got.value.status_code == 404


class TestRefusingABrokenSpec:
    def test_no_spec_means_the_ai_collects_as_before(self):
        assert extract.normalize(None) is None
        assert extract.normalize({}) is None

    @pytest.mark.parametrize(
        "broken",
        [
            {"tag": "印象派の画家"},  # source が無い
            {"source": "jawiki"},  # tag が無い
            {"source": "jawiki", "tag": "x", "body": "title"},  # 取れない欄
            {"source": "jawiki", "tag": "x", "tags": "画家"},  # 配列ではない
            {"source": "jawiki", "tag": "x", "tags": [{"format": "年代:{1}"}]},  # 型が無い
            {"source": "jawiki", "tag": "x", "tags": [{"pattern": "^(.+$"}]},  # 読めない
        ],
    )
    def test_it_is_refused_when_it_is_written(self, broken):
        with pytest.raises(HTTPException) as got:
            extract.normalize(broken)

        assert got.value.status_code == 400

    def test_no_limit_means_everything(self):
        """引くのは索引を 1 本引くだけ。取る側に件数を絞る理由は無い。"""
        assert extract.normalize({"source": "j", "tag": "t"})["limit"] is None
        assert extract.normalize({"source": "j", "tag": "t", "limit": 0})["limit"] is None

    def test_a_huge_limit_is_clamped_instead_of_refused(self):
        """件数は書き間違えても意味が通る。断るより上限で止めるほうが親切。"""
        assert extract.normalize({"source": "j", "tag": "t", "limit": 10_000_000})["limit"] \
            == extract.MAX_ROWS

    def test_it_can_be_written_back_as_it_was_given(self):
        """定義に残すので、書いた正規表現がそのまま読み返せる必要がある。"""
        given = {
            "source": "jawiki",
            "tag": "印象派の画家",
            "not_tag": "アメリカ合衆国の男優",
            "limit": 30,
            "body": "opening",
            "url": "https://ja.wikipedia.org/wiki/{title}",
            "tags": [
                {"const": "画家"},
                {"patterns": [r"^(\d{3,4})年生$", r"^(\d{3,4})年没$"], "format": "年代:{1}-{2}"},
                {"pattern": "^(.+)派の画家$", "format": "様式:{1}派"},
            ],
            "cursor": "抽出済み",
        }

        assert extract.to_json(extract.normalize(given)) == {
            **given, "tag_suffix": "", "extra": [],
        }


class TestPickingAWholeFamilyOfTags:
    """`tag_suffix` —— 「〜の画家」のような**族をなすカテゴリ**をまとめて指す。

    書き並べる形だと、書く側が名前を思い出しで補うことになり、抜けても気づけない。
    実際に、地域を書き並べた画家の指定で「アメリカ合衆国」が丸ごと落ち、
    「イギリス」と書いたせいで中身の大半がある「イングランド」が取れていなかった。
    """

    def test_it_takes_every_tag_that_ends_with_it(self, source):
        docs = [
            {"title": "モネ", "tags": ["19世紀フランスの画家"]},
            {"title": "ホッパー", "tags": ["20世紀アメリカ合衆国の画家"]},
            {"title": "ターナー", "tags": ["19世紀イングランドの画家"]},
            {"title": "ある俳優", "tags": ["20世紀アメリカ合衆国の男優"]},
        ]
        spec = extract.normalize({"source": "jawiki", "tag_suffix": "の画家"})
        items, _cursor = run(spec, source(docs))
        assert sorted(i["title"] for i in items) == ["ターナー", "ホッパー", "モネ"]

    def test_several_suffixes_can_be_written(self, source):
        """**同じものの呼び方が 1 つとは限らない。**

        画家の名簿で「の画家」だけを書いていたせいで、「〜の女性画家」が丸ごと
        落ちていた(本番の実測で 44 カテゴリ・1,877 記事)。
        """
        docs = [
            {"title": "モネ", "tags": ["19世紀フランスの画家"]},
            {"title": "草間彌生", "tags": ["20世紀日本の女性画家"]},
            {"title": "ある俳優", "tags": ["20世紀日本の男優"]},
        ]
        spec = extract.normalize(
            {"source": "jawiki", "tag_suffix": "の画家,の女性画家"}
        )
        items, _cursor = run(spec, source(docs))
        assert sorted(i["title"] for i in items) == ["モネ", "草間彌生"]

    def test_a_short_one_among_them_is_still_refused(self, source):
        """短い末尾は何にでも当たる。1 つでも混ざっていれば断る。"""
        with pytest.raises(HTTPException):
            extract.normalize({"source": "jawiki", "tag_suffix": "の画家,家"})

    def test_it_can_be_combined_with_exact_tags(self, source):
        """族に入らない 1 つを足したいことがある(様式のカテゴリなど)。"""
        docs = [
            {"title": "モネ", "tags": ["19世紀フランスの画家"]},
            {"title": "北斎", "tags": ["浮世絵師"]},
        ]
        spec = extract.normalize(
            {"source": "jawiki", "tag": "浮世絵師", "tag_suffix": "の画家"}
        )
        items, _cursor = run(spec, source(docs))
        assert sorted(i["title"] for i in items) == ["モネ", "北斎"]

    def test_what_is_not_wanted_still_comes_off_with_not_tag(self, source):
        docs = [
            {"title": "モネ", "tags": ["19世紀フランスの画家"]},
            {"title": "絵も描く俳優", "tags": ["20世紀フランスの画家", "俳優"]},
        ]
        spec = extract.normalize(
            {"source": "jawiki", "tag_suffix": "の画家", "not_tag": "俳優"}
        )
        items, _cursor = run(spec, source(docs))
        assert [i["title"] for i in items] == ["モネ"]

    def test_a_short_suffix_is_refused(self):
        """短い語は何にでも当たる(「家」だけで数万のカテゴリが並ぶ)。"""
        with pytest.raises(HTTPException):
            extract.normalize({"source": "jawiki", "tag_suffix": "家"})

    def test_neither_a_tag_nor_a_suffix_is_refused(self):
        with pytest.raises(HTTPException):
            extract.normalize({"source": "jawiki"})

    def test_a_suffix_that_matches_nothing_says_so(self, source):
        """静かな 0 件にしない(それらしい末尾を書いた側には確かめようがない)。"""
        spec = extract.normalize({"source": "jawiki", "tag_suffix": "の陶芸家"})
        with pytest.raises(HTTPException):
            run(spec, source([{"title": "モネ", "tags": ["19世紀フランスの画家"]}]))

    def test_too_many_tags_are_refused_not_truncated(self, source, monkeypatch):
        """切ると、広すぎる指定が「そこまでしか無い」ように見える。"""
        monkeypatch.setattr(extract, "MAX_SUFFIX_TAGS", 2)
        docs = [{"title": f"画家{i}", "tags": [f"{i}世紀某国の画家"]} for i in range(5)]
        spec = extract.normalize({"source": "jawiki", "tag_suffix": "の画家"})
        with pytest.raises(HTTPException):
            run(spec, source(docs))

    def test_the_wildcards_in_a_suffix_are_not_special(self, source):
        """`_` は LIKE では 1 文字に当たる。素通しにすると当たりすぎる。"""
        docs = [{"title": "モネ", "tags": ["19世紀フランスの画家"]}]
        spec = extract.normalize({"source": "jawiki", "tag_suffix": "_の画家"})
        with pytest.raises(HTTPException):
            run(spec, source(docs))

    def test_the_suffix_survives_a_round_trip(self):
        given = {
            "source": "jawiki",
            "tag": "",
            "tag_suffix": "の画家",
            "not_tag": "俳優",
            "limit": None,
            "body": "opening",
            "url": "",
            "tags": [],
            "extra": [],
            "cursor": "抽出済み",
        }
        assert extract.to_json(extract.normalize(given)) == given


class TestWhenItIsUsedInsteadOfTheAI:
    """機械で埋めるのは**最初の 1 回だけ**。以降は AI が肉付けする。"""

    async def _items(self, item, sources):
        from app.main import _collect_items

        return await _collect_items(item, {}, sources, [])

    def test_the_first_run_does_not_ask_the_ai(self, source, monkeypatch):
        import asyncio

        from app import collect, main

        async def refuse(*_args, **_kwargs):
            raise AssertionError("最初の 1 回で AI を呼んでいる")

        monkeypatch.setattr(main, "_ask_for_collection", refuse)
        item = collect.Collection(
            name="painters", description="", prompt="{cursor}", interval_minutes=60,
            enabled=False, backend=None, model=None, effort=None, web=True, cursor="",
            created_at="", updated_at="", extract=extract.to_json(spec()),
        )

        items, cursor, _note = asyncio.run(self._items(item, source(painters())))

        assert [i["title"] for i in items] == ["クロード・モネ", "エドゥアール・マネ"]
        assert cursor == extract.DEFAULT_CURSOR

    def test_once_it_has_run_the_ai_takes_over(self, source, monkeypatch):
        """進み具合が入っていれば、いつもどおり AI に頼む（肉付けの番）。"""
        import asyncio

        from app import collect, main

        async def answer(*_args, **_kwargs):
            return '{"items": [{"title": "肉付け", "body": "AI が書いた"}], "next_cursor": "次"}'

        monkeypatch.setattr(main, "_ask_for_collection", answer)
        item = collect.Collection(
            name="painters", description="", prompt="{cursor}", interval_minutes=60,
            enabled=False, backend=None, model=None, effort=None, web=True,
            cursor=extract.DEFAULT_CURSOR, created_at="", updated_at="",
            extract=extract.to_json(spec()),
        )

        items, cursor, _note = asyncio.run(self._items(item, source(painters())))

        assert [i["title"] for i in items] == ["肉付け"]
        assert cursor == "次"


class TestWritingTheSpecFromARequest:
    """依頼文を指定にするのは AI。**そこだけが AI の仕事**で、以降は指定を回す。"""

    @pytest.fixture()
    def client(self, tmp_path, built_data_dir, monkeypatch):
        from fastapi.testclient import TestClient

        from app import db

        notes_dir = tmp_path / "notes"
        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
        # 定義の置き場（`state/chiezo_settings.db`）。人が読む短期記憶とは別のファイル
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        db.set_mutable_paths([notes_dir / "notes.db"])
        from app.main import app

        with TestClient(app) as c:
            yield c

    def _answer(self, monkeypatch, content: str):
        from app import main

        async def reply(*_args, **_kwargs):
            return content

        monkeypatch.setattr(main, "_ask_for_collection", reply)

    def test_it_says_how_many_the_tag_matches_not_just_what_it_took(self, client, monkeypatch):
        """取った数だけ見せると、絞られていることに気づけない。"""
        self._answer(monkeypatch, json.dumps({
            "source": "jawiki", "tag": "日本の都道府県", "limit": 1,
        }))

        body = client.post("/v1/collect/draft-extract", json={"want": "都道府県を1件"}).json()

        assert body["total"] == 1
        assert body["matched"] == 2

    def test_too_many_is_refused_instead_of_cut(self, client, monkeypatch):
        """黙って切ると、絞ったつもりのない指定が「そこまでしか無い」ように見える。"""
        monkeypatch.setattr(extract, "MAX_ROWS", 1)
        self._answer(monkeypatch, json.dumps({"source": "jawiki", "tag": "日本の都道府県"}))

        res = client.post("/v1/collect/draft-extract", json={"want": "都道府県を全部"})

        assert res.status_code == 409
        # 何件に当たっているかを言う（言わないと、どこまで絞ればよいか分からない）
        assert "2" in json.dumps(res.json(), ensure_ascii=False)

    def test_it_comes_back_with_what_the_spec_actually_pulls(self, client, monkeypatch):
        """保存する前に空振りが分かるように、その場で引いた結果を添える。"""
        self._answer(monkeypatch, json.dumps({
            "source": "jawiki",
            "tag": "日本の都道府県",
            "limit": 10,
            "tags": [{"const": "都道府県"}],
        }))

        body = client.post(
            "/v1/collect/draft-extract", json={"want": "日本の都道府県を全部"}
        ).json()

        assert body["extract"]["tag"] == "日本の都道府県"
        assert body["total"] == 2
        assert body["sample"][0]["tags"] == ["都道府県"]

    def test_an_invented_tag_comes_back_with_real_ones(self, client, monkeypatch):
        """タグは完全一致でしか引けない。それらしい名前は静かな 0 件になる。"""
        self._answer(monkeypatch, json.dumps({"source": "jawiki", "tag": "都道府県"}))

        body = client.post("/v1/collect/draft-extract", json={"want": "都道府県"}).json()

        assert body["total"] == 0
        assert {"tag": "日本の都道府県", "docs": 2} in body["candidates"]

    def test_a_thin_pick_gets_one_more_go_with_the_real_tags(self, client, monkeypatch):
        """「画家」のような一般名は実在するが数件しか付いていない。

        それらしい名前を当てるしかない側に、実在するタグを見せて選び直させる。
        """
        from app import main

        asked = []

        async def reply(_settings, messages):
            asked.append(messages)
            # 1 回目は当てずっぽう、2 回目は見せられた中から選ぶ
            return json.dumps(
                {"source": "jawiki", "tag": "都道府県" if len(asked) == 1 else "日本の都道府県"}
            )

        monkeypatch.setattr(main, "_ask_for_collection", reply)

        body = client.post("/v1/collect/draft-extract", json={"want": "都道府県を30件"}).json()

        assert body["extract"]["tag"] == "日本の都道府県"
        assert body["total"] == 2
        # 選び直しには、実在するタグを文書数つきで見せている
        assert "日本の都道府県(2 件)" in asked[1][1]["content"]

    def test_a_pick_that_is_only_a_little_short_is_left_alone(self, client, monkeypatch):
        """少し足りないだけで投げ直さない。

        投げ直すと、件数を満たそうとして条件のほうが広がる（頼んだ範囲の外まで
        タグを足しにいく）。足りないままのほうが、頼んだものだけが入っている。
        """
        from app import main

        asked = []

        async def reply(_settings, messages):
            asked.append(messages)
            return json.dumps({"source": "jawiki", "tag": "日本の都道府県", "limit": 3})

        monkeypatch.setattr(main, "_ask_for_collection", reply)

        body = client.post("/v1/collect/draft-extract", json={"want": "都道府県を3件"}).json()

        # 3 件頼んで 2 件。半端だが、広げさせるほどではない
        assert body["total"] == 2
        assert len(asked) == 1

    def test_it_keeps_the_first_pick_when_the_second_is_no_better(self, client, monkeypatch):
        """投げ直して悪くなるくらいなら、最初のものを返す。"""
        from app import main

        asked = []

        async def reply(_settings, messages):
            asked.append(messages)
            return json.dumps(
                {"source": "jawiki", "tag": "日本の都道府県" if len(asked) == 1 else "存在しない"}
            )

        monkeypatch.setattr(main, "_ask_for_collection", reply)

        body = client.post("/v1/collect/draft-extract", json={"want": "都道府県を30件"}).json()

        assert body["extract"]["tag"] == "日本の都道府県"
        assert body["total"] == 2

    def test_the_caller_can_name_who_writes_it(self, client, monkeypatch):
        """収集を作る前にも書かせるので、名前で相手を引けない。

        指定を書くのは道具を何度も引く仕事なので、遅い相手だと十数分待つことになる。
        """
        from app import main

        seen = {}

        async def reply(settings, _messages):
            seen["backend"] = settings.backend
            seen["model"] = settings.model
            return json.dumps({"source": "jawiki", "tag": "日本の都道府県"})

        monkeypatch.setattr(main, "_ask_for_collection", reply)

        client.post(
            "/v1/collect/draft-extract",
            json={"want": "都道府県", "backend": "claude", "model": "haiku"},
        )

        assert seen == {"backend": "claude", "model": "haiku"}

    def test_the_ai_can_say_it_cannot_be_pulled(self, client, monkeypatch):
        """引けないものを無理に指定へ落とすと、当たらないタグで静かな 0 件になる。

        頼んだ側は今までどおり AI に集めさせればよい。
        """
        self._answer(monkeypatch, json.dumps({
            "extract": None,
            "reason": "その日のニュースは長期記憶に入っていないので、外から集めるしかありません",
        }))

        body = client.post(
            "/v1/collect/draft-extract", json={"want": "今日のニュースを10件"}
        ).json()

        assert body["extract"] is None
        assert "外から集める" in body["reason"]

    def test_a_spec_that_cannot_be_run_is_refused(self, client, monkeypatch):
        self._answer(monkeypatch, json.dumps({"source": "jawiki"}))

        assert client.post("/v1/collect/draft-extract", json={"want": "何か"}).status_code == 400

    def test_an_answer_that_is_not_json_is_refused(self, client, monkeypatch):
        self._answer(monkeypatch, "指定は書けませんでした")

        assert client.post("/v1/collect/draft-extract", json={"want": "何か"}).status_code == 502

    def test_it_says_which_sources_can_be_pulled_from(self, source):
        """名前を知らなければ、実在しないソースを書く。"""
        messages = extract.build_draft_messages("画家", source(painters()))

        assert "jawiki(wikipedia)" in messages[1]["content"]
        assert "抽出の指定は次の形" in messages[0]["content"]

    def test_the_current_spec_comes_along_when_there_is_one(self, source):
        messages = extract.build_draft_messages("直して", source(painters()), {"tag": "印象派の画家"})

        assert "印象派の画家" in messages[1]["content"]

    def test_a_fenced_answer_is_still_read(self):
        assert extract.parse_draft('```json\n{"source": "jawiki"}\n```') == {"source": "jawiki"}


class TestTheAdminScreen:
    @pytest.fixture()
    def stored(self, tmp_path, monkeypatch):
        """指定を持った収集を 1 つ置く。"""
        from app import collect, db

        notes_dir = tmp_path / "notes"
        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(notes_dir))
        # 定義の置き場（`state/chiezo_settings.db`）。人が読む短期記憶とは別のファイル
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
        db.set_mutable_paths([notes_dir / "notes.db"])
        collect.create("painters", prompt="{cursor} と {current}", interval_minutes=60)
        return collect.update("painters", extract=extract.to_json(spec()))

    def test_consulting_about_the_prompt_keeps_the_rest(self, stored):
        """相談から保存したときに、フォームに載っていない項目が既定へ戻らないこと。

        載せていないと、抽出の指定が消える。
        """
        from app.views import admin

        html = admin._consult_page_html("painters", "画家", "新しい指示文", "")

        assert "印象派の画家" in html

    def test_the_drafted_spec_comes_with_what_it_pulls(self, stored):
        from app.views import admin

        drafted = {
            "extract": extract.to_json(spec()),
            "total": 2,
            "sample": [{"title": "クロード・モネ", "tags": ["画家"], "url": "https://example.com/1"}],
        }

        html = admin._draft_extract_page_html("painters", "画家を30人", drafted, "")

        assert "2 件" in html
        assert "クロード・モネ" in html
        assert html.count("<form") == html.count("</form>")

    def test_a_thin_result_says_which_tags_are_real(self, stored):
        from app.views import admin

        drafted = {"extract": extract.to_json(spec()), "total": 0, "sample": [],
                   "candidates": [{"tag": "印象派の画家", "docs": 39}]}

        html = admin._draft_extract_page_html("painters", "画家", drafted, "")

        assert "頼んだ件数に届きませんでした" in html
        # 数まで出す。「実在はするが数件しか付いていない」タグを選ばないため
        assert "印象派の画家(39 件)" in html


class TestCarryingFactsFromTheArticle:
    """元の記事に載っている事実を、そのまま運ぶ(`extra`)。

    知名度(月次ページビュー)のような値は既に長期記憶にあるので、読む側が 1 件ずつ
    引き直す理由が無い —— 引き直していた頃は、図を開くたびに画家の数だけ往復していた。
    """

    def spec(self, **over):
        return extract.normalize({
            "source": "jawiki", "tag": "画家", "extra": ["pageviews_month"], **over,
        })

    def test_it_copies_the_named_keys(self, source):
        docs = [{"title": "モネ", "tags": ["画家"],
                 "extra": {"pageviews_month": 8869, "wikidata": "Q296"}}]

        items, _cursor = run(self.spec(), source(docs))

        # 名指ししたものだけ。**丸写しにはしない**
        assert items[0]["extra"] == {"pageviews_month": 8869}

    def test_a_missing_key_is_skipped(self, source):
        """記事によって持っている値が違う(ページビューを持たない記事もある)。"""
        docs = [{"title": "無名さん", "tags": ["画家"], "extra": {"wikidata": "Q1"}}]

        items, _cursor = run(self.spec(), source(docs))

        assert "extra" not in items[0]

    def test_nothing_is_carried_without_the_spec(self, source):
        docs = [{"title": "モネ", "tags": ["画家"], "extra": {"pageviews_month": 8869}}]

        items, _cursor = run(self.spec(extra=[]), source(docs))

        assert "extra" not in items[0]

    def test_too_many_keys_are_refused(self):
        """1 件の脇に添える札であって、記事を丸ごと写す場所ではない。"""
        with pytest.raises(HTTPException):
            extract.normalize({
                "source": "jawiki", "tag": "画家",
                "extra": [f"k{i}" for i in range(extract.MAX_CARRIED_KEYS + 1)],
            })

    def test_it_must_be_a_list(self):
        with pytest.raises(HTTPException):
            extract.normalize({"source": "jawiki", "tag": "画家", "extra": "pageviews_month"})


class TestPullingFromSeveralSources:
    """ソースをまたいで 1 つの名簿にする。

    見ているのは**先に書いたほうが勝つ**こと。どの項目をどのソースから採るかは、
    本の順番と、その本が運ぶ `extra` の鍵で表す —— 項目ごとの順位を別に書ける
    ようにはしていない(同じことを 2 通りで書けるだけになる)。
    """

    @staticmethod
    def famous():
        """百科事典の側。**説明を持っているが座標は持ち込まない**。"""
        return [
            {
                "title": "すきやばし次郎",
                "opening": "東京都中央区銀座にある寿司店。",
                "tags": ["東京都の飲食店"],
                "extra": {"pageviews_month": 12_000},
                "rank": 0.9,
            }
        ]

    @staticmethod
    def mapped():
        """地図の側。**座標を持っているが説明は持っていない**。"""
        return [
            {
                "title": "すきやばし次郎",
                "opening": "すきやばし次郎\n種別: 寿司店",
                "tags": ["amenity=restaurant"],
                "extra": {"lat": 35.67, "lon": 139.76},
                "rank": 0.8,
            },
            {
                "title": "近所の定食屋",
                "opening": "近所の定食屋\n種別: 食堂",
                "tags": ["amenity=restaurant"],
                "extra": {"lat": 35.70, "lon": 139.70},
                "rank": 0.5,
            },
        ]

    def both(self, source):
        return {
            **source(self.famous(), name="jawiki"),
            **source(self.mapped(), name="osm_japan"),
        }

    @staticmethod
    def two_specs(**overrides):
        """百科事典が説明で勝ち、地図が座標で勝つ形。

        どちらも相手の項目を持っているので、順番だけでは表せない ——
        譲る項目を `provides` から外して書く。
        """
        wiki = {
            "source": "jawiki",
            "tag": "東京都の飲食店",
            "extra": ["lat", "lon", "pageviews_month"],
            "provides": ["body", "url"],
            "tags": [{"const": "出典:Wikipedia"}],
        }
        osm = {
            "source": "osm_japan",
            "tag": "amenity=restaurant",
            "extra": ["lat", "lon"],
            "provides": ["extra"],
            "tags": [{"const": "出典:OSM"}],
        }
        return extract.normalize([{**wiki, **overrides.get("wiki", {})},
                                  {**osm, **overrides.get("osm", {})}])

    def test_it_makes_one_roster_out_of_every_source(self, source):
        items, _cursor = run(self.two_specs(), self.both(source))

        assert sorted(i["title"] for i in items) == ["すきやばし次郎", "近所の定食屋"]

    def test_each_field_goes_to_the_source_that_claims_it(self, source):
        # 説明は百科事典、座標は地図。**順番だけでは表せない** ——
        # どちらも相手の項目を持っているので、譲る側を書いて分ける
        items, _cursor = run(self.two_specs(), self.both(source))
        famous = next(i for i in items if i["title"] == "すきやばし次郎")

        assert famous["body"] == "東京都中央区銀座にある寿司店。"
        assert (famous["extra"]["lat"], famous["extra"]["lon"]) == (35.67, 139.76)

    def test_a_source_that_gave_up_a_field_still_fills_the_hole(self, source):
        """**順位を譲ることと、穴を空けたままにすることは別**。

        地図に載っていない 1 軒の座標は百科事典にしか無い。譲った本の値を捨てると、
        その 1 軒はどの区画にも入らず、AI から永遠に見えなくなる。
        """
        only_in_wikipedia = [{
            "title": "名店",
            "opening": "由緒ある店。",
            "tags": ["東京都の飲食店"],
            "extra": {"lat": 34.7, "lon": 135.5},
            "rank": 0.9,
        }]
        sources = {
            **source(only_in_wikipedia, name="jawiki"),
            **source(self.mapped(), name="osm_japan"),
        }
        items, _cursor = run(self.two_specs(), sources)
        alone = next(i for i in items if i["title"] == "名店")

        assert (alone["extra"]["lat"], alone["extra"]["lon"]) == (34.7, 135.5)

    def test_the_boilerplate_body_is_used_when_nothing_better_exists(self, source):
        # 地図しか持っていない店は、地図の本文で埋まる（譲っただけで、捨てていない）
        items, _cursor = run(self.two_specs(), self.both(source))
        ordinary = next(i for i in items if i["title"] == "近所の定食屋")

        assert ordinary["body"].startswith("近所の定食屋")

    def test_it_fills_key_by_key(self, source):
        # **まるごと見ない。** 勝った本が 1 つでも鍵を持っていた時点で止めると、
        # 譲った本が持つ別の鍵（ページビューや電話）が永遠に入らない
        items, _cursor = run(self.two_specs(), self.both(source))
        famous = next(i for i in items if i["title"] == "すきやばし次郎")

        assert famous["extra"]["pageviews_month"] == 12_000

    def test_a_field_name_it_does_not_know_is_refused(self):
        with pytest.raises(HTTPException, match="provides"):
            extract.normalize({"source": "jawiki", "tag": "画家", "provides": ["tags"]})

    def test_the_default_is_to_go_for_everything(self):
        # 1 本しか書いていない指定に順位の話は無い。控えにも書かない
        written = extract.to_json(extract.normalize({"source": "jawiki", "tag": "画家"}))

        assert "provides" not in written

    def test_tags_are_added_without_moving_the_ones_already_there(self, source):
        # 読む側は先頭のタグを代表として使うので、後ろの本が並びを変えると意味が変わる
        items, _cursor = run(self.two_specs(), self.both(source))
        famous = next(i for i in items if i["title"] == "すきやばし次郎")

        assert famous["tags"] == ["出典:Wikipedia", "出典:OSM"]

    def test_the_cursor_comes_from_the_first_source(self, source):
        # 収集が持てる進み具合は 1 つしかない。選ぶなら優先の先頭がいちばん読める
        specs = self.two_specs(wiki={"cursor": "名簿を作った"}, osm={"cursor": "地図から引いた"})
        _items, cursor = run(specs, self.both(source))

        assert cursor == "名簿を作った"

    def test_it_counts_every_source(self, source):
        # どれか 1 本の数を見せると、他の本が当たっていないように見える
        assert extract.count(self.two_specs(), self.both(source)) == 3

    def test_the_same_source_twice_is_refused(self):
        # 2 本目は「1 本目が埋めなかったところ」しか埋められない。
        # **書けるが効かない指定**を残すと、効いていないことに気づけない
        with pytest.raises(HTTPException, match="2 度"):
            extract.normalize([
                {"source": "jawiki", "tag": "画家"},
                {"source": "jawiki", "tag": "彫刻家"},
            ])

    def test_too_many_specs_are_refused(self):
        with pytest.raises(HTTPException, match=str(extract.MAX_SPECS)):
            extract.normalize([
                {"source": f"src{n}", "tag": "画家"} for n in range(extract.MAX_SPECS + 1)
            ])

    def test_an_empty_spec_in_the_list_is_refused(self):
        with pytest.raises(HTTPException):
            extract.normalize([{"source": "jawiki", "tag": "画家"}, {}])

    def test_an_empty_list_means_no_extract_at_all(self):
        assert extract.normalize([]) is None

    def test_it_keeps_the_shape_it_was_written_in(self):
        # 畳んで返すと、定義に控えたものが書いた人の書いたものと違う形になる
        written = extract.to_json(extract.normalize([
            {"source": "jawiki", "tag": "画家"},
            {"source": "osm_japan", "tag": "amenity=restaurant"},
        ]))

        assert isinstance(written, list)
        assert [one["source"] for one in written] == ["jawiki", "osm_japan"]
        assert isinstance(extract.to_json(extract.normalize({"source": "jawiki", "tag": "画家"})), dict)


class TestWhenTheSourceIsTooSlow:
    """名簿を引くのは取り込みの中で動く背景の仕事で、誰も応答を待っていない。

    読み口の 5 秒はそこに当てない —— あれは人が待っている問い合わせを守る数で、
    地図の名簿のように数十万件に当たる指定は並べ替えだけで数秒かかる。
    """

    def test_it_gets_more_time_than_a_reader_does(self):
        from app import db

        assert extract.EXTRACT_TIMEOUT_SECONDS > db.QUERY_TIMEOUT_SECONDS

    def test_it_says_which_source_ran_out(self, source, monkeypatch):
        """**打ち切りをそのまま上げない。** 控えに「QueryTimeout」の一語だけが
        残ると、何本も書いてある指定のどれが重かったのかを後から辿れない
        (無人で回る層なので、そのとき見ている人はいない)。
        """
        from app import db

        def too_slow(*_args, **_kwargs):
            raise db.QueryTimeout()

        monkeypatch.setattr(db, "query", too_slow)
        monkeypatch.setattr(db, "stream", too_slow)
        spec = extract.normalize({"source": "jawiki", "tag": "印象派の画家", "limit": 30})

        with pytest.raises(HTTPException) as caught:
            run(spec, source(painters()))

        assert caught.value.status_code == 504
        assert "jawiki" in caught.value.detail["error"]
        assert "limit" in caught.value.detail["hint"]


class TestASlowReaderIsNotTheQuerysFault:
    """**流し読みの締め切りは「次の 1 行が出てくるまで」**(`db.stream`)。

    1 行ずつ返すあいだ、時間を使っているのは読み手のほうで SQLite は止まっている。
    流し終えるまでに締め切りを掛けると、**行数が多いほど読み手のせいで切れる** ——
    本番で 60 万件を焼く回が 10 分 47 秒で `query timeout` になった(読み手は
    素材を組んで HTTP で流していただけで、どの問い合わせも詰まっていない)。
    """

    ROWS = 20_000

    def _db(self, tmp_path):
        """**打ち切りの判定が回る量**を入れる(`_PROGRESS_STEP` ごとに見るので、
        数行では一度も判定されず、何を確かめても通ってしまう)。"""
        import sqlite3

        path = tmp_path / "rows.db"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE t (n INTEGER)")
        conn.executemany("INSERT INTO t VALUES (?)", [(n,) for n in range(self.ROWS)])
        conn.commit()
        conn.close()
        return path

    def test_a_slow_reader_does_not_run_out_the_clock(self, tmp_path):
        """読み手が 1 行ずつ手間をかけても、合計で締め切りを超えたことにしない。"""
        import time

        from app import db

        path = self._db(tmp_path)

        seen = 0
        for _row in db.stream(path, "SELECT n FROM t ORDER BY n", timeout=0.05):
            # 1 行の手間は小さいが、行数を掛けると締め切りをはるかに超える
            time.sleep(0.0001)
            seen += 1

        assert seen == self.ROWS

    def test_a_query_that_never_returns_a_row_is_still_cut(self, tmp_path):
        """**止めたいのはこちら。** 行が出てこない問い合わせは今までどおり切る。"""
        import pytest as _pytest

        from app import db

        path = self._db(tmp_path)

        with _pytest.raises(db.QueryTimeout):
            # 自分自身との総当たり(4 億通り)を 1 行も返さない条件で回す
            list(db.stream(
                path,
                "SELECT a.n FROM t a, t b WHERE a.n + b.n < 0",
                timeout=0.05,
            ))


class TestTheRosterKeepsTitlesBakeable:
    """名簿の鍵は**切り詰めた見出し**(`notes.TITLE_MAX_CHARS`)。

    焼く側は見出しを切ってから書き、長期記憶は見出しに一意の索引を張る ——
    生のまま鍵にすると、**先頭 60 字が同じ 2 件が別物として通り、焼く段で索引が
    張れずに取り込みがまるごと落ちる**(世代は切り替わらないので、集めたぶんが
    静かに消えたように見える。本番で 68 万件がこれで焼けなかった)。
    AI が返したぶんを持つ側(`collect.Edits`)は初めから切ってある。
    """

    def test_two_long_titles_that_share_the_head_become_one(self):
        from app import notes
        from app.extract import Roster

        head = "あ" * notes.TITLE_MAX_CHARS
        roster = Roster()
        try:
            roster.merge({"title": head + "その1", "body": "先に入ったほう"}, claims={"body"})
            roster.merge({"title": head + "その2", "body": "あとから来たほう"}, claims={"body"})

            assert roster.count == 1
            [only] = list(roster)
            assert only["title"] == head
            # **既に入っている値は上書きしない**（畳む向きは今までどおり）
            assert only["body"] == "先に入ったほう"
        finally:
            roster.close()

    def test_a_truncated_title_still_matches_what_is_already_baked(self):
        """前世代の見出しは既に切り詰まっている。**鍵が揃っていないと引き当てられず**、
        同じ 1 件が「足すもの」として通って見出しがぶつかる。
        """
        from app import notes
        from app.extract import Roster

        head = "い" * notes.TITLE_MAX_CHARS
        roster = Roster()
        try:
            roster.merge({"title": head + "ながい続き", "body": "本文"}, claims={"body"})

            assert roster.take(head) is not None
            assert list(roster.rest()) == []
        finally:
            roster.close()


class TestKeepingTheRosterOffMemory:
    """引いた名簿は一時の SQLite に置く。**丸ごとは持たない**。

    dict で持っていた頃は引いた件数ぶんのメモリが要り、日本の飲食店を 3 つの辞典から
    引くと 68 万件で 2 GB を超えた。焼く側も 1 行ずつ受け取るようになったので、
    ここも持たずに渡せる形にする。
    """

    def test_it_is_not_a_list(self, source):
        roster, _cursor = extract.run(spec(), source(painters()))
        try:
            assert not isinstance(roster, list)
            assert roster.count == 2
            assert [i["title"] for i in roster] == ["クロード・モネ", "エドゥアール・マネ"]
        finally:
            roster.close()

    def test_the_baking_side_can_pull_by_title(self, source):
        """焼く側は「前世代を流しながら、その見出しに来ている直しを引く」形で回る。"""
        roster, _cursor = extract.run(spec(), source(painters()))
        try:
            assert roster.take("クロード・モネ")["title"] == "クロード・モネ"
            assert roster.take("居ない人") is None
            # 取ったものは「残り」から外れる
            assert [i["title"] for i in roster.rest()] == ["エドゥアール・マネ"]
            roster.reset()
            assert len(list(roster.rest())) == 2
        finally:
            roster.close()

    def test_the_place_is_cleaned_up(self, source):
        # 残すと一時ファイルが溜まる
        roster, _cursor = extract.run(spec(), source(painters()))
        path = Path(roster.path)
        assert path.exists()

        roster.close()

        assert not path.exists()
