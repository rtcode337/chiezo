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
        items, _cursor = extract.run(spec(), source(painters()))

        # 江戸時代の画家は入らない（同じ「画家」でも頼まれていない）
        assert [i["title"] for i in items] == ["クロード・モネ", "エドゥアール・マネ"]

    def test_it_keeps_the_order_the_source_already_has(self, source):
        """有名な順はソースが持っている（ページビュー由来の rank_score）。"""
        items, _cursor = extract.run(spec(tag="1840年生,1832年生,1760年生"), source(painters()))

        assert [i["title"] for i in items] == ["クロード・モネ", "葛飾北斎", "エドゥアール・マネ"]

    def test_it_takes_everything_when_no_number_was_given(self, source):
        items, _cursor = extract.run(spec(limit=0, tag="1840年生,1832年生,1760年生"), source(painters()))

        assert len(items) == 3

    def test_it_stops_at_the_limit(self, source):
        items, _cursor = extract.run(spec(limit=1), source(painters()))

        assert [i["title"] for i in items] == ["クロード・モネ"]

    def test_two_tags_become_one_range(self, source):
        """生年と没年は別々のタグ。読む側が要るのは年代なので、ここで 1 つにする。"""
        items, _cursor = extract.run(spec(), source(painters()))

        assert "年代:1840-1926" in items[0]["tags"]

    def test_a_half_match_makes_nothing(self, source):
        """没年が無ければ年代を作らない（「1840-」のような半端を残さない）。"""
        docs = [{"title": "存命の画家", "tags": ["印象派の画家", "1940年生"], "rank": 0.5}]

        items, _cursor = extract.run(spec(), source(docs))

        assert not [t for t in items[0]["tags"] if t.startswith("年代:")]

    def test_a_rule_can_hit_more_than_once(self, source):
        docs = [
            {
                "title": "両方の画家",
                "tags": ["印象派の画家", "象徴派の画家", "1840年生", "1900年没"],
                "rank": 0.5,
            }
        ]

        items, _cursor = extract.run(spec(), source(docs))

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

        items, _cursor = extract.run(
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

        items, _cursor = extract.run(
            spec(tags=[{"from_tag": "{title}の作品", "format": "代表作:{1}", "take": 1}]),
            source(docs),
        )

        assert items[0]["tags"] == ["代表作:印象・日の出"]

    def test_a_painter_without_a_works_category_gets_nothing(self, source):
        items, _cursor = extract.run(
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

        items, _cursor = extract.run(
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

        items, _cursor = extract.run(
            spec(tags=[{"linked": "mutual", "format": "関連:{1}"}]), source(docs)
        )

        assert items[0]["tags"] == []

    def test_the_original_tags_do_not_come_along(self, source):
        """カテゴリはソースの都合で付いている。読む側に選ばせない。"""
        items, _cursor = extract.run(spec(), source(painters()))

        assert "19世紀フランスの画家" not in items[0]["tags"]
        assert items[0]["tags"][0] == "画家"

    def test_the_source_of_each_item_can_be_built_from_the_title(self, source):
        items, _cursor = extract.run(
            spec(url="https://ja.wikipedia.org/wiki/{title}"), source(painters())
        )

        assert items[0]["url"] == "https://ja.wikipedia.org/wiki/クロード・モネ"

    def test_extra_can_fill_the_source_too(self, source):
        items, _cursor = extract.run(
            spec(url="https://www.wikidata.org/wiki/{wikidata}"), source(painters())
        )

        assert items[0]["url"] == "https://www.wikidata.org/wiki/Q296"

    def test_the_body_is_the_opening_by_default(self, source):
        """焼くのは要点。全文は元のソースにある。"""
        sources = source(painters())

        assert extract.run(spec(), sources)[0][0]["body"] == "クロード・モネの要約。"
        assert extract.run(spec(body="body"), sources)[0][0]["body"] == "クロード・モネの本文。"

    def test_it_marks_that_the_bulk_pass_is_done(self, source):
        """次の実行は「進み具合が入っている」ほうへ進む（＝ AI が肉付けする）。"""
        sources = source(painters())

        assert extract.run(spec(), sources)[1] == extract.DEFAULT_CURSOR
        # 印は指定側で決められる（依頼した側の言葉で書ける）
        assert extract.run(spec(cursor="粗く作成済み"), sources)[1] == "粗く作成済み"

    def test_an_unknown_source_says_so(self, source):
        with pytest.raises(HTTPException) as got:
            extract.run(spec(source="nosuch"), source(painters()))

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

        assert extract.to_json(extract.normalize(given)) == given


class TestWhenItIsUsedInsteadOfTheAI:
    """機械で埋めるのは**最初の 1 回だけ**。以降は AI が肉付けする。"""

    async def _items(self, item, sources):
        from app.main import _collect_items

        return await _collect_items(item, {}, sources)

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

        items, cursor = asyncio.run(self._items(item, source(painters())))

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

        items, cursor = asyncio.run(self._items(item, source(painters())))

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
        monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://chiezo-trigger:7011")
        db.set_mutable_paths([notes_dir / "notes.db"])
        collect.create("painters", prompt="{cursor} と {current}", interval_minutes=60,
                       mode=collect.MODE_REFINE)
        return collect.update("painters", extract=extract.to_json(spec()))

    def test_consulting_about_the_prompt_keeps_the_rest(self, stored):
        """相談から保存したときに、フォームに載っていない項目が既定へ戻らないこと。

        載せていないと、集め方が「足す」に戻り、抽出の指定が消える。
        """
        from app.views import admin

        html = admin._consult_page_html("painters", "画家", "新しい指示文", "")

        assert 'name="mode" value="refine"' in html
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
