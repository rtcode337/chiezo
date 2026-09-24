"""手で回す巡回(`app/handoff.py`)。

**web の画面から使う AI に頼む道。** 鍵も API も持たない代わりに調べものが速い
相手がいるので、依頼文をファイルにして人に渡し、答えのファイルを読み込む。

見ているのは 3 つ —— **両端がいつもと同じ部品であること**(依頼文は
`build_messages`、答えは `parse_response`)、**渡した範囲にだけ印が付くこと**、
**束が溜まらないこと**。
"""
import json

import pytest
from test_agent import make_client


@pytest.fixture()
def state_env(monkeypatch, tmp_path):
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://trigger.invalid/")
    return monkeypatch


def _collection(name: str = "tazuna_meals", **extra):
    from app import collect

    collect.create(
        name=name, description="手で回す", prompt="{partition}\n{current}\nを見てください",
        interval_minutes=60,
    )
    collect.update(name, enabled=True, sweeps=[
        {"name": "手で調べる", "by_hand": True, "prompt": "{partition}\n{current}\n足して"},
    ], **extra)
    return collect.get(name)


def _with_partitions(name: str = "tazuna_meals"):
    from app import collect

    item = _collection(name, partition={"by": "geo", "target": 10})
    collect.update(name, partitions=[
        {"key": "35.0,135.0/35.5,135.5", "count": 2, "visits": {}},
        {"key": "36.0,136.0/36.5,136.5", "count": 1, "visits": {}},
    ])
    return item


class TestTheSweepItself:
    def test_it_does_not_ask_an_ai(self, state_env):
        """控えに既定の相手を書くと、頼んでいない相手の回として履歴に並ぶ。"""
        from app import collect

        item = _collection()
        [sweep] = collect.sweeps_of(item)

        assert sweep.by_hand
        assert not collect.asks_ai(item, sweep)

    def test_it_has_no_clock(self, state_env):
        """**予定で起こすと、答えの無い束が溜まる。** 次を作るのは
        「答えが焼けたとき」か「押したとき」。"""
        from app import collect

        item = _collection()
        [sweep] = collect.sweeps_of(item)

        assert not sweep.is_due()
        assert collect.due_sweeps() == []

    def test_it_still_walks_partitions(self, state_env):
        """区画のぶんを見せて答えをもらう回なので、印は付く。"""
        from app import collect

        item = _with_partitions()
        [sweep] = collect.sweeps_of(item)

        assert sweep.walks_partitions()


class TestItIsNeverAskedToRunByItself:
    """**手で回す回は、自分から走らない。** 走らせる中身(人が持ち帰った答え)が
    揃うのは読み込んだときだけなので、他の道から呼ばれると空振りする。
    """

    def test_a_worker_is_dropped_from_it(self, state_env):
        """**手で回す回にワーカーは持たせない。** 持たせると待ち行列に積まれ、
        答えの無い取り込みが毎周走って 409 で落ちる(本番でそうなった)。"""
        from app import collect

        collect.create(name="tazuna_meals", description="", prompt="集めて",
                       interval_minutes=60)
        collect.update("tazuna_meals", enabled=True, sweeps=[
            {"name": "手で調べる", "by_hand": True, "worker": "整理用ワーカー"},
        ])
        [sweep] = collect.sweeps_of(collect.get("tazuna_meals"))

        assert sweep.by_hand
        assert sweep.worker == ""

    def test_the_backend_select_cannot_give_it_one_either(self, state_env):
        """外のアプリは相手の欄でワーカーを名指しする(`/v1/ai/backends`)。"""
        from app import collect, workers

        collect.create(name="tazuna_meals", description="", prompt="集めて",
                       interval_minutes=60)
        collect.update("tazuna_meals", enabled=True, sweeps=[
            {"name": "手で調べる", "by_hand": True,
             "backend": workers.option_for("w-1a2b3c4d")},
        ])
        [sweep] = collect.sweeps_of(collect.get("tazuna_meals"))

        assert sweep.worker == ""

    def test_it_is_never_queued_for_a_worker(self, state_env, monkeypatch):
        """積む段でも弾く —— 定義を直す前に積まれたぶんが残っていても流さない。"""
        from app import collect, main, workers

        collect.create(name="tazuna_meals", description="", prompt="集めて",
                       interval_minutes=60)
        collect.update("tazuna_meals", enabled=True, sweeps=[
            {"name": "手で調べる", "by_hand": True},
        ])
        # 手で回す回へ変える前に積まれた 1 本(定義からはワーカーが消えている)
        workers.save([workers.Worker("整理用", (workers.Step("codex"),), id="w-1a2b3c4d")])
        workers.enqueue("w-1a2b3c4d", "tazuna_meals", "手で調べる",
                        "2026-09-24T00:00:00+00:00")
        main._fill_worker_queues()
        main._drop_stale_from_queues()

        assert workers.queued("w-1a2b3c4d") == []

    def test_a_focus_does_not_land_on_it(self, state_env):
        """割り込みは人が待っている場面 —— その場で AI に聞ける回へ倒す。"""
        from app import collect

        collect.create(name="tazuna_meals", description="", prompt="直して",
                       interval_minutes=60)
        collect.update("tazuna_meals", enabled=True, sweeps=[
            {"name": "ざっと見る", "prompt": "{current} を直して"},
            {"name": "手で調べる", "by_hand": True},
        ])
        item = collect.get("tazuna_meals")
        asked = collect.Focus(note="ここが違う", sweep="手で調べる")

        assert collect.sweep_for_focus(item, asked).name == "ざっと見る"

    def test_an_unknown_name_does_not_land_on_it_either(self, state_env):
        """巡回を消したあとの取り込みが素材を取りに来ることがある。"""
        from app import collect

        collect.create(name="tazuna_meals", description="", prompt="直して",
                       interval_minutes=60)
        collect.update("tazuna_meals", enabled=True, sweeps=[
            {"name": "ざっと見る", "prompt": "{current} を直して"},
            {"name": "手で調べる", "by_hand": True},
        ])

        assert collect.sweep_named(collect.get("tazuna_meals"), "消えた回").name == "ざっと見る"


class TestMakingTheBundle:
    def test_the_body_is_the_same_request_as_the_ai_gets(self, state_env):
        """**言い換えない。** 手で回した回と AI に頼んだ回で違うことを頼むと、
        結果を比べられなくなる。"""
        from app import collect, handoff

        item = _collection()
        [sweep] = collect.sweeps_of(item)
        messages = collect.build_messages(item, {}, None, {}, sweep, None, None, set())
        body = collect.handoff_body(item, sweep, [messages], [])

        for message in messages:
            assert message["content"] in body
        handoff.put(item.name, sweep=sweep.name, keys=[], shown=[], body=body)
        assert handoff.body_of(item.name) == body

    def test_the_body_says_what_to_do_and_how_to_answer(self, state_env):
        """**材料だけでは動かない。** 何をして、どの形で返すかを材料より先に言う。"""
        from app import collect

        item = _collection()
        [sweep] = collect.sweeps_of(item)
        body = collect.handoff_body(item, sweep, [[{"content": "本文"}]], ["a"])

        assert "answer.json" in body
        assert '"items"' in body
        assert "触ったものと、新しく足すものだけ" in body

    def test_each_partition_gets_its_own_section(self, state_env):
        """**まとめて 1 つの節にしない。** 範囲が「(全体)」になり、差し込みも
        天井で切られる —— 本番の 1 束目は 5 区画 497 件のうち 300 件しか載らず、
        主な仕事(この範囲に足りない店を足す)が 1 件も返ってこなかった。"""
        from app import collect

        item = _with_partitions()
        [sweep] = collect.sweeps_of(item)
        keys = ["35.0,135.0/35.5,135.5", "36.0,136.0/36.5,136.5"]
        sections = [[{"content": f"{key} を見て"}] for key in keys]

        body = collect.handoff_body(item, sweep, sections, keys)

        assert "範囲 1 / 2" in body
        assert "範囲 2 / 2" in body
        for key in keys:
            assert key in body

    def test_a_section_holds_its_own_range_only(self, state_env):
        """節ごとに「その範囲の全部」。よその区画の店が混ざると、
        **その範囲に無いものを「ある」として読ませる**ことになる。"""
        import asyncio

        from app import collect, handoff, main

        item = _with_partitions()
        docs = [
            {"title": "北の店", "extra": {"lat": 36.2, "lon": 136.2}},
            {"title": "南の店", "extra": {"lat": 35.2, "lon": 135.2}},
        ]
        state_env.setattr(collect, "stream_previous", lambda *a, **k: list(docs))
        asyncio.run(main.build_handoff(item.name, {}, "手で調べる"))
        body = handoff.body_of(item.name)
        held = handoff.get(item.name)

        # 1 回に見る区画は巡回の設定で決まる(ここでは 1 つ)
        assert len(held["keys"]) == 1
        assert "35.0000〜35.5000" in body
        assert "南の店" in body
        assert "北の店" not in body

    def test_only_one_bundle_is_held(self, state_env):
        """作る → 渡す → 読み込む が一巡するまで次を作らない
        (溜まると、どの束の答えなのかが分からなくなる)。"""
        from app import handoff

        _collection()
        handoff.put("tazuna_meals", sweep="手で調べる", keys=["a"], shown=[], body="1 つめ")

        assert handoff.waiting("tazuna_meals")

        handoff.put("tazuna_meals", sweep="手で調べる", keys=["b"], shown=[], body="2 つめ")
        assert handoff.body_of("tazuna_meals") == "2 つめ"

    def test_the_rest_endpoint_refuses_a_second_one(self, state_env):
        _with_partitions()
        with make_client(state_env, None) as client:
            assert client.post("/v1/collect/tazuna_meals/handoff").status_code == 200
            assert client.post("/v1/collect/tazuna_meals/handoff").status_code == 409

    def test_the_file_comes_back_as_a_file(self, state_env):
        _with_partitions()
        with make_client(state_env, None) as client:
            client.post("/v1/collect/tazuna_meals/handoff")
            res = client.get("/v1/collect/tazuna_meals/handoff/file")

        assert res.status_code == 200
        assert "attachment" in res.headers["content-disposition"]
        assert "answer.json" in res.text

    def test_a_collection_without_the_sweep_is_refused(self, state_env):
        from app import collect

        collect.create(name="tazuna_tech", description="", prompt="集めて", interval_minutes=60)
        with make_client(state_env, None) as client:
            assert client.post("/v1/collect/tazuna_tech/handoff").status_code == 404


class TestReadingTheAnswer:
    def _answer(self, titles=("新しい店",)) -> str:
        return json.dumps({"items": [{"title": t, "body": "本文"} for t in titles]})

    def test_it_is_read_with_the_same_parser(self, state_env):
        """**前置きや ``` の囲みが混ざっても拾う** —— AI に頼む回と同じ読み方。"""
        from app import collect

        items, _cursor, _note = collect.parse_response(
            "はい、調べました。\n```json\n" + self._answer() + "\n```"
        )

        assert [i["title"] for i in items] == ["新しい店"]

    def test_the_answer_is_held_until_the_bake(self, state_env):
        from app import handoff

        _collection()
        handoff.put("tazuna_meals", sweep="手で調べる", keys=["a"], shown=["店"], body="束")
        handoff.answered("tazuna_meals", [{"title": "新しい店"}])

        assert handoff.ready("tazuna_meals")
        assert not handoff.waiting("tazuna_meals")

    def test_taking_it_clears_the_bundle(self, state_env):
        """残すと、次の取り込みが同じ答えをもう一度焼く(印と履歴が二重になる)。"""
        from app import handoff

        _collection()
        handoff.put("tazuna_meals", sweep="手で調べる", keys=["a"], shown=["店"], body="束")
        handoff.answered("tazuna_meals", [{"title": "新しい店"}])

        items, meta = handoff.take("tazuna_meals")

        assert [i["title"] for i in items] == ["新しい店"]
        assert meta["keys"] == ["a"]
        assert handoff.get("tazuna_meals") is None

    def test_an_answer_with_nothing_in_it_is_refused(self, state_env):
        """形の違う答えを焼くと、その回は「何も返らなかった」として印だけが進む。"""
        _with_partitions()
        with make_client(state_env, None) as client:
            client.post("/v1/collect/tazuna_meals/handoff")
            res = client.post("/v1/collect/tazuna_meals/handoff/answer",
                              content="すみません、分かりませんでした".encode())

        assert res.status_code == 400

    def test_an_answer_without_a_bundle_is_refused(self, state_env):
        _with_partitions()
        with make_client(state_env, None) as client:
            res = client.post("/v1/collect/tazuna_meals/handoff/answer",
                              content=self._answer().encode())

        assert res.status_code == 404


class TestWhatTheBakeSees:
    def test_the_items_come_from_the_bundle(self, state_env):
        """**AI は呼ばない。** 読むのは預かった答えのほう。"""
        import asyncio

        from app import collect, handoff, main

        item = _with_partitions()
        [sweep] = collect.sweeps_of(item)
        handoff.put(item.name, sweep=sweep.name, keys=["35.0,135.0/35.5,135.5"],
                    shown=["前からある店"], body="束")
        handoff.answered(item.name, [{"title": "新しい店", "body": "本文"}])
        done: list[str] = []
        seen: set[str] = set()

        items, _cursor, _note = asyncio.run(main._collect_items(
            item, {}, {}, ["35.0,135.0/35.5,135.5"], sweep, None, None, seen, [], done,
        ))

        assert [i["title"] for i in items] == ["新しい店"]
        # **印を付けるのは、束に入れて渡した区画だけ**
        assert done == ["35.0,135.0/35.5,135.5"]
        # **差し込んだ見出しは「AI が目を通した」に数える**(未精査の印を外す先)
        assert seen == {"前からある店"}

    def test_a_bake_without_an_answer_is_refused(self, state_env):
        """預かりが消えていると、そのまま進むと「何も返らなかった回」として
        印と予定だけが進む。"""
        import asyncio

        import fastapi

        from app import collect, main

        item = _with_partitions()
        [sweep] = collect.sweeps_of(item)

        with pytest.raises(fastapi.HTTPException) as got:
            asyncio.run(main._collect_items(
                item, {}, {}, ["35.0,135.0/35.5,135.5"], sweep, None, None, None, [], [],
            ))

        assert got.value.status_code == 409


class TestTheScreen:
    def test_it_offers_the_bundle_and_the_note(self, state_env):
        """**ファイルだけでは動かない** —— 添えて送る一言も画面に出す。"""
        from app import handoff
        from app.views import admin

        item = _with_partitions()
        handoff.put(item.name, sweep="手で調べる", keys=["a"], shown=[], body="束")
        html = admin._handoff_html(item)

        assert "/v1/collect/tazuna_meals/handoff/file" in html
        assert handoff.PASTE_NOTE[:20] in html
        assert "答えを読み込んで焼く" in html

    def test_without_a_bundle_it_offers_to_make_one(self, state_env):
        from app.views import admin

        html = admin._handoff_html(_with_partitions())

        assert "束を作る" in html

    def test_a_collection_without_the_sweep_shows_nothing(self, state_env):
        from app import collect
        from app.views import admin

        collect.create(name="tazuna_tech", description="", prompt="集めて", interval_minutes=60)

        assert admin._handoff_html(collect.get("tazuna_tech")) == ""
