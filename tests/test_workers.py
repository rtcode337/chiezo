"""ワーカー(app/workers.py)のテスト。

**押さえているのは 3 つ**: ①優先度順に見て枠に余裕のある相手を選ぶ、
②どれも詰まっていたら「待て」を返す(相手がいない、ではない)、
③枠を出さない相手を締め出さない。

③が要るのは、振り替えの仕組みが「枠を出せる相手しか使えない」ものになると、
いちばん頼りたい相手を外す羽目になるため。
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime

import pytest

from app import machine_store, usage_store, workers
from app import partition as partitioning


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """機械の置き場(`state/chiezo_settings.db`)と使用量の控えを同じところに用意する。"""
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _quota(provider: str, percent: float) -> None:
    usage_store.save_quota(provider, [{"id": "primary", "label": "直近 5 時間",
                                       "used_percent": percent}])


def _worker(*steps: workers.Step) -> workers.Worker:
    return workers.Worker("精査", tuple(steps))


def _define(monkeypatch, tmp_path, name: str, *sweeps: str) -> None:
    """ワーカーに任せた巡回を持つ収集を 1 つ置く(行列に積むものの実体)。"""
    from app import collect

    monkeypatch.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    monkeypatch.setenv("CHIEZO_TRIGGER_URL", "http://trigger.invalid/")
    collect.create(name=name, description=name, prompt="{partition}", interval_minutes=60)
    collect.update(name, enabled=True, sweeps=[
        {"name": s, "worker": "精査", "interval_minutes": 60, "prompt": "{partition}"}
        for s in sweeps
    ])


class TestChoosing:
    """優先度順に見て、枠に余裕のある最初の相手に頼む。"""

    def test_the_first_one_with_room_wins(self, enabled):
        _quota("codex", 95.0)
        _quota("claude", 10.0)
        picked = workers.pick(_worker(workers.Step("codex"), workers.Step("claude")))

        assert picked == workers.Step("claude")

    def test_the_head_is_used_while_it_has_room(self, enabled):
        _quota("codex", 10.0)
        _quota("claude", 10.0)
        picked = workers.pick(_worker(workers.Step("codex"), workers.Step("claude")))

        assert picked == workers.Step("codex")

    def test_everyone_crowded_means_wait(self, enabled):
        """**「相手がいない」ではなく「待て」。** 呼ぶ側はその回を見送る。"""
        _quota("codex", 95.0)
        _quota("claude", 99.0)

        assert workers.pick(_worker(workers.Step("codex"), workers.Step("claude"))) is None

    def test_a_backend_without_a_quota_is_not_shut_out(self, enabled):
        """枠を出さない相手も、まだ一度も取れていない相手もここに落ちる ——
        取れないことを理由に頼まないのでは、仕組みの意味が無い。
        """
        assert workers.pick(_worker(workers.Step("gemini"))) == workers.Step("gemini")

    def test_zero_is_not_the_same_as_unknown(self, enabled):
        """0 は「まだ使っていない」で、頼んでよい相手。"""
        _quota("codex", 0.0)

        assert workers.pick(_worker(workers.Step("codex"))) == workers.Step("codex")

    def test_the_busiest_window_decides(self, enabled):
        """**窓は何本もある。** 1 本でも詰まっていれば、その相手は避ける。"""
        usage_store.save_quota("codex", [
            {"id": "primary", "label": "直近 5 時間", "used_percent": 95.0},
            {"id": "secondary", "label": "直近 7 日", "used_percent": 3.0},
        ])

        assert workers.pick(_worker(workers.Step("codex"))) is None

    def test_the_limit_can_be_moved(self, enabled):
        _quota("codex", 85.0)

        assert workers.pick(_worker(workers.Step("codex")), limit=90.0) is not None
        assert workers.pick(_worker(workers.Step("codex")), limit=80.0) is None


class TestABackendWithMoreThanOneQuota:
    """**1 人の相手が、独立した枠を何本も持つことがある。**

    Antigravity は Gemini と Claude/GPT で週も 5 時間も別勘定で、どちらを食うかは
    選んだモデルで決まる —— まとめて「いちばん詰まっている窓」で見ると、片方が
    詰まっただけで相手ごと避けることになり、**逃げ先として置いた段まで巻き添えで
    飛ばされる**(実測: Claude 枠が 100% の回に、5 時間 78% 空いていた Gemini の
    段が使われず次の相手へ落ちた)。
    """

    @staticmethod
    def _split_quota():
        usage_store.save_quota("antigravity", [
            {"id": "gemini-5h", "label": "Gemini Models(直近 5 時間)",
             "group": "Gemini Models", "used_percent": 22.0},
            {"id": "3p-5h", "label": "Claude and GPT models(直近 5 時間)",
             "group": "Claude and GPT models", "used_percent": 100.0},
            {"id": "gemini-weekly", "label": "Gemini Models(直近 7 日)",
             "group": "Gemini Models", "used_percent": 54.0},
            {"id": "3p-weekly", "label": "Claude and GPT models(直近 7 日)",
             "group": "Claude and GPT models", "used_percent": 69.0},
        ])

    def test_the_crowded_group_is_avoided(self, enabled):
        self._split_quota()

        assert not workers.room_left(workers.Step("antigravity", "claude-opus-4-6-thinking"))

    def test_the_other_group_is_still_open(self, enabled):
        """ここが眼目。**同じ相手でも、空いている枠の段は使える。**"""
        self._split_quota()

        assert workers.room_left(workers.Step("antigravity", "gemini-3.8-flash-medium"))

    def test_the_fallback_step_on_the_same_backend_is_reached(self, enabled):
        """本番の並びそのまま —— Claude 枠が詰まったら、次の段(同じ相手の
        Gemini)へ落ちる。Codex まで飛ばさない。"""
        self._split_quota()
        picked = workers.pick(_worker(
            workers.Step("antigravity", "claude-opus-4-6-thinking"),
            workers.Step("antigravity", "gemini-3.8-flash-medium"),
            workers.Step("codex", "gpt-5.6-sol-medium"),
        ))

        assert picked == workers.Step("antigravity", "gemini-3.8-flash-medium")

    def test_a_step_without_a_model_still_sees_every_window(self, enabled):
        """どの枠を食うか決まらないので、**慎重な側に倒す**。"""
        self._split_quota()

        assert not workers.room_left(workers.Step("antigravity"))

    def test_an_unknown_group_name_falls_back_to_every_window(self, enabled):
        """相手が呼び名を変えた日に、避けるのをやめてしまわない。"""
        usage_store.save_quota("antigravity", [
            {"id": "a", "label": "何か", "group": "Renamed Models", "used_percent": 100.0},
        ])

        assert not workers.room_left(workers.Step("antigravity", "gemini-3.8-flash-medium"))

    def test_the_screen_shows_the_group_the_step_actually_uses(self, enabled):
        """**画面の数字も枠ごとに出す。** 渡さないと全窓の最大が出るので、
        Gemini の段に Claude 枠の数字が並んでいた —— 判断する側
        (`room_left`)は枠ごとに見ているのに、画面だけが混ぜたままだった。
        数字と振る舞いが食い違うと、避けられていない段が「詰まっている」に見える。
        """
        from app.views import ai_workers

        self._split_quota()

        assert "100% 使用" in ai_workers._percent("antigravity", "claude-opus-4-6-thinking")
        assert "54% 使用" in ai_workers._percent("antigravity", "gemini-3.8-flash-medium")

    def test_a_fraction_is_not_rounded_away(self, enabled):
        """**79.7% が「80% 使用」と出ていた。**

        ワーカーは 80 未満ならその相手を使うので(`QUOTA_LIMIT`)、丸めると
        「上限を超えているのに使われた」に見える —— 判定は生の値で見ている。
        """
        from app.views import ai_workers

        usage_store.save_quota("antigravity", [
            {"id": "gemini-weekly", "label": "Gemini Models(直近 7 日)",
             "group": "Gemini Models", "used_percent": 79.7},
        ])

        shown = ai_workers._percent("antigravity", "gemini-3.8-flash-medium")

        assert "79.7% 使用" in shown
        # 上限を超えていないので、避ける印も付かない
        assert workers.room_left(workers.Step("antigravity", "gemini-3.8-flash-medium"))

    def test_the_screen_and_the_decision_agree(self, enabled):
        """**⚠️ が付く段と、実際に避ける段が一致すること。**"""
        from app.views import ai_workers

        self._split_quota()
        for model in ("claude-opus-4-6-thinking", "gemini-3.8-flash-medium"):
            step = workers.Step("antigravity", model)
            marked = "⚠️" in ai_workers._percent(step.backend, step.model)
            assert marked is not workers.room_left(step), model

    def test_a_stored_row_from_before_the_split_still_works(self, enabled):
        """**入れ替えた日の控えには枠の呼び名が入っていない。** 突き合わないので
        全部の窓で見る = 入れ替え前とまったく同じ挙動。次の採取で呼び名が入る。
        """
        usage_store.save_quota("antigravity", [
            {"id": "gemini-5h", "label": "Gemini Models(直近 5 時間)", "used_percent": 22.0},
            {"id": "3p-5h", "label": "Claude and GPT models(直近 5 時間)",
             "used_percent": 100.0},
        ])

        assert not workers.room_left(workers.Step("antigravity", "gemini-3.8-flash-medium"))

    def test_a_backend_with_one_quota_is_unchanged(self, enabled):
        usage_store.save_quota("codex", [
            {"id": "primary", "label": "直近 5 時間", "used_percent": 95.0},
        ])

        assert not workers.room_left(workers.Step("codex", "gpt-5.6-sol-medium"))

    def test_being_refused_only_shuts_out_that_group(self, enabled):
        """枠切れの言い分も枠ごとに控える —— 相手ごと避けると、別の枠に置いた
        段まで巻き添えになる(使用率で見るときと同じ話)。"""
        workers.avoid_for_now(
            "antigravity", "quota exceeded. Resets in 1h0m0s", "claude-opus-4-6-thinking",
        )

        assert not workers.room_left(workers.Step("antigravity", "claude-opus-4-6-thinking"))
        assert workers.room_left(workers.Step("antigravity", "gemini-3.8-flash-medium"))

    def test_a_mark_without_a_group_shuts_the_whole_backend_out(self, enabled):
        """相手の名前だけの印は「どの枠か分からないまま断られた」ぶん ——
        **その相手ぜんぶに効く**(枠を分ける前に置かれた古い印もここに入る)。"""
        workers.mark_full("antigravity", "2099-01-01T00:00:00+00:00")

        assert not workers.room_left(workers.Step("antigravity", "gemini-3.8-flash-medium"))
        assert not workers.room_left(workers.Step("antigravity", "claude-opus-4-6-thinking"))


class TestDefinitions:
    """定義の読み書き。**壊れていたら黙って作り直さない。**"""

    def test_it_comes_back_as_it_went_in(self, enabled):
        workers.save([workers.Worker("精査", (
            workers.Step("codex", "gpt-5.5"), workers.Step("claude"),
        ))])
        [found] = workers.load()

        assert found.name == "精査"
        assert found.steps[0] == workers.Step("codex", "gpt-5.5")
        assert found.steps[1] == workers.Step("claude")

    def test_nothing_stored_is_an_empty_list(self, enabled):
        assert workers.load() == []

    def test_a_broken_body_is_shown_not_swallowed(self, enabled):
        machine_store.put(workers.DEFS_KIND, workers.DEFS_KEY, "{壊れている")

        with pytest.raises(ValueError):
            workers.load()

    def test_a_step_without_a_backend_is_dropped(self, enabled):
        """相手の名前が無い段は頼みようがない。"""
        machine_store.put(workers.DEFS_KIND, workers.DEFS_KEY, json.dumps(
            {"workers": [{"name": "精査", "steps": [{"model": "gpt-5.5"},
                                                    {"backend": "codex"}]}]}))
        [found] = workers.load()

        assert found.steps == (workers.Step("codex"),)

    def test_one_can_be_found_by_name(self, enabled):
        workers.save([workers.Worker("ざっと", (workers.Step("antigravity"),)),
                      workers.Worker("精査", (workers.Step("codex"),))])

        assert workers.get("精査").steps == (workers.Step("codex"),)
        assert workers.get("いない") is None


def _collection():
    from app import collect

    return collect.Collection(
        name="tazuna_painters", description="", prompt="育てて", interval_minutes=60,
        enabled=True, backend=None, model=None, effort=None, web=True, cursor="",
        created_at="", updated_at="",
    )


def _sweep(worker: str = "", name: str = "精査"):
    from app import collect

    return collect.Sweep(name=name, prompt="育てて", interval_minutes=60, enabled=True,
                         backend=None, model=None, effort=None, worker=worker)


class TestTheQueue:
    """ワーカーは**自分の待ち行列**を持ち、自分の間隔で起きて、1 度に拾った塊を
    1 本ずつ流す。

    巡回の側は「自分がまだ行列に居ない」かつ「前回の完了から間隔が空いた」ときに
    自分を積む —— **予定では見ない**。あれは起こした時点で進むので、行列で待って
    いるあいだに何度も予定が来る。
    """

    def _entry(self, collection="tazuna_painters", sweep="精査"):
        return {"collection": collection, "sweep": sweep}

    def test_nothing_queued_is_nothing_queued(self, enabled):
        assert workers.queued("精査") == []

    def test_it_goes_in_once(self, enabled):
        """**二重に積まない** —— 同じ回が 2 本走ることになる。"""
        assert workers.enqueue("精査", "tazuna_painters", "精査", "2026-01-01T00:00:00+00:00")
        assert not workers.enqueue("精査", "tazuna_painters", "精査", "2026-01-01T01:00:00+00:00")

        assert [
            (e["collection"], e["sweep"]) for e in workers.queued("精査")
        ] == [("tazuna_painters", "精査")]

    def test_a_claim_takes_at_most_its_share(self, enabled):
        for i in range(5):
            workers.enqueue("精査", f"c{i}", "精査", "2026-01-01T00:00:00+00:00")

        batch = workers.claim("精査", 2, "2026-01-01T00:00:00+00:00")

        assert [e["collection"] for e in batch] == ["c0", "c1"]
        # 拾ったぶんは行列から外れるが、**塊として残る**(流し切るまで優先権を持つ)
        assert [e["collection"] for e in workers.queued("精査")] == ["c0", "c1", "c2", "c3", "c4"]

    def test_it_does_not_claim_again_while_one_is_running(self, enabled):
        """**流し切るまで拾い直さない** —— 途中で拾うと「1 度に N 本」が意味を失う。"""
        for i in range(4):
            workers.enqueue("精査", f"c{i}", "精査", "2026-01-01T00:00:00+00:00")
        workers.claim("精査", 2, "2026-01-01T00:00:00+00:00")

        again = workers.claim("精査", 2, "2026-01-01T01:00:00+00:00")

        assert [e["collection"] for e in again] == ["c0", "c1"]
        assert workers.claim_ready("精査")

    def test_finishing_the_batch_frees_the_next_claim(self, enabled):
        for i in range(3):
            workers.enqueue("精査", f"c{i}", "精査", "2026-01-01T00:00:00+00:00")
        workers.claim("精査", 2, "2026-01-01T00:00:00+00:00")
        workers.done("精査", "c0", "精査")
        workers.done("精査", "c1", "精査")

        assert not workers.claim_ready("精査")
        assert [e["collection"] for e in workers.claim("精査", 2, "x")] == ["c2"]

    def test_a_collection_that_went_away_is_dropped(self, enabled):
        workers.enqueue("精査", "きえた", "精査", "2026-01-01T00:00:00+00:00")
        workers.enqueue("精査", "のこる", "精査", "2026-01-01T00:00:00+00:00")

        workers.forget("きえた")

        assert [e["collection"] for e in workers.queued("精査")] == ["のこる"]

    def test_the_settings_form_does_not_wipe_the_queue(self, enabled):
        """**行列は定義と別の置き場**。同じ控えに入れると、編集のたびに消える。"""
        workers.enqueue("精査", "tazuna_painters", "精査", "2026-01-01T00:00:00+00:00")

        workers.save([workers.Worker("精査", (workers.Step("codex"),))])

        assert len(workers.queued("精査")) == 1


class TestWhenASweepGoesIntoTheQueue:
    """積む条件は「まだ居ない」と「前回の完了から間隔が空いた」の 2 つ。"""

    def test_one_that_never_ran_goes_in(self, enabled):
        from app import main

        assert main._due_for_queue(_sweep(), datetime(2026, 1, 1, tzinfo=UTC))

    def test_it_waits_for_the_interval_after_the_last_finish(self, enabled):
        from app import main

        sweep = _sweep()
        sweep = replace(sweep, last_run_at="2026-01-01T00:00:00+00:00")

        assert not main._due_for_queue(sweep, datetime(2026, 1, 1, 0, 30, tzinfo=UTC))
        assert main._due_for_queue(sweep, datetime(2026, 1, 1, 1, 0, tzinfo=UTC))

    def test_a_failed_run_is_spaced_out_too(self, enabled):
        """**落ち続ける回がすぐ積み直されると、その 1 本が行列を占める。**

        失敗しても完了の時刻は入るので、同じ規則で間隔が空く。
        """
        from app import main

        sweep = replace(_sweep(), last_run_at="2026-01-01T00:00:00+00:00",
                        last_status="error")

        assert not main._due_for_queue(sweep, datetime(2026, 1, 1, 0, 30, tzinfo=UTC))


class TestWhenAWorkerWakesUp:
    def test_the_first_time_is_now(self, enabled):
        from app import main

        assert main._worker_due(workers.Worker("精査"), datetime(2026, 1, 1, tzinfo=UTC))

    def test_then_it_waits_for_its_own_interval(self, enabled):
        from app import main

        workers.enqueue("精査", "c", "精査", "2026-01-01T00:00:00+00:00")
        workers.claim("精査", 1, "2026-01-01T00:00:00+00:00")
        worker = workers.Worker("精査", (), interval_minutes=30)

        assert not main._worker_due(worker, datetime(2026, 1, 1, 0, 10, tzinfo=UTC))
        assert main._worker_due(worker, datetime(2026, 1, 1, 0, 30, tzinfo=UTC))


class TestBelievingTheProviderOverTheSample:
    """相手が「枠を使い切った」と言ったら、明けるまで避ける(`workers.avoid_for_now`)。

    **控えてある使用率は定時にしか採らない。** 1 回が長い回の途中で窓が閉まっても、
    次の採取まで気づけない —— 本番で、41% と控えたまま同じ相手へ 4 回続けて投げ、
    最後に断られて 29 分ぶんの収穫が消えた。断られた事実のほうが新しい。
    """

    def test_a_refused_backend_is_skipped(self, enabled):
        _quota("antigravity", 41.0)
        _quota("codex", 10.0)
        step = workers.Step("antigravity")

        assert workers.room_left(step)
        workers.avoid_for_now("antigravity", "Individual quota reached. Resets in 1h15m20s.")

        assert not workers.room_left(step)
        assert workers.pick(_worker(step, workers.Step("codex"))) == workers.Step("codex")

    def test_it_comes_back_once_the_window_opens(self, enabled):
        _quota("antigravity", 41.0)
        workers.avoid_for_now("antigravity", "quota reached. Resets in 1h0m0s.")

        # 明けたあとの時刻で見れば、また頼める
        assert workers.room_left(workers.Step("antigravity"), now="2099-01-01T00:00:00+00:00")

    def test_it_reads_when_the_window_opens(self):
        assert workers.cooldown_minutes("Resets in 1h15m20s.") == 76
        assert workers.cooldown_minutes("resets in 45m") == 45
        # **読めなければ既定**（短くしすぎると、明ける前に頼み直して 1 回ぶん損をする）
        assert workers.cooldown_minutes("quota reached") == workers.DEFAULT_COOLDOWN_MINUTES

    def test_it_knows_a_quota_message_from_any_other_failure(self):
        assert workers.looks_full("Individual quota reached. Please upgrade")
        assert workers.looks_full("RESOURCE_EXHAUSTED")
        # **枠と関係ない失敗で相手を締め出さない**（つながらないだけの回もある）
        assert not workers.looks_full("llm unreachable / ConnectError")
        assert not workers.looks_full("")


class TestDecidingWhoToAsk:
    """相手が None なのは 2 通りあり、次にすることが逆になる ——
    ワーカーを使わない回(巡回の指定で走る)と、どれも枠が詰まっている回
    (走らせずに見送る)。**前者はワーカーが None、後者は相手が None**。
    """

    def test_no_worker_means_the_sweep_decides(self, enabled):
        from app import main

        assert main._worker_of(_sweep()) is None

    def test_the_worker_names_the_backend(self, enabled):
        from app import main

        workers.save([workers.Worker("精査", (workers.Step("codex", "gpt-5.5"),))])
        _quota("codex", 10.0)

        found = main._worker_of(_sweep(worker="精査"))
        assert workers.pick(found) == workers.Step("codex", "gpt-5.5")

    def test_all_crowded_is_wait_not_fall_back(self, enabled):
        """**巡回の指定へ落ちない。** 落ちると、避けたかった相手に頼むことがある。"""
        from app import main

        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 95.0)

        found = main._worker_of(_sweep(worker="精査"))
        assert found is not None
        assert workers.pick(found) is None

    def test_a_name_that_is_not_there_falls_back(self, enabled):
        """綴りを間違えただけで無人の層が止まるより、走って控えに相手が残るほうがよい。"""
        from app import main

        assert main._worker_of(_sweep(worker="いない")) is None


class TestTheChosenBackendIsUsed:
    """選んだ相手が、実際に投げる定義に載る。"""

    def test_the_step_wins_over_the_sweep(self, enabled):
        from app import collect

        item = _collection()
        sweep = collect.Sweep(name="精査", prompt="育てて", interval_minutes=60, enabled=True,
                              backend="antigravity", model="gemini", effort="low",
                              worker="精査")
        asked = sweep.applied_to(item, workers.Step("codex", "gpt-5.5"))

        # **考える量はモデルの名前に畳んである**ので、段は持たない。巡回側に
        # 書いてあった考える量も引き継がない —— 相手が変わっているので意味を持たない
        assert (asked.backend, asked.model, asked.effort) == ("codex", "gpt-5.5", None)

    def test_without_a_step_the_sweep_decides(self, enabled):
        from app import collect

        item = _collection()
        sweep = collect.Sweep(name="ざっと", prompt="育てて", interval_minutes=60, enabled=True,
                              backend="antigravity", model="gemini", effort="low")
        asked = sweep.applied_to(item)

        assert (asked.backend, asked.model, asked.effort) == ("antigravity", "gemini", "low")


class TestRelayingPartWayThrough:
    """**相手を決めるのは区画ごと。** 1 回で何区画も回るので、決めるのが回の頭
    1 度きりだと、途中で窓が閉まっても同じ相手に投げ続ける ——
    本番で、41% で通した相手が 2 区画目で 89% に跳ね、残り 4 区画ぶんを投げ切って
    から断られ、29 分ぶんの収穫がまるごと消えた。
    """

    @pytest.fixture
    def asking(self, enabled, monkeypatch):
        """AI を呼ぶところを差し替えて、**何回目にどの相手へ行ったか**を控える。"""
        from app import main

        seen: list[str] = []

        async def fake(asked, _messages):
            seen.append(asked.backend)
            # 返すのは (本文, 実際に走った相手, モデル)
            return ('{"items": [{"title": "1 件", "body": "本文"}]}',
                    asked.backend, asked.model or "")

        monkeypatch.setattr(main, "_ask_for_collection", fake)
        return main, seen

    def _run(self, main, keys):
        return asyncio.run(main._collect_items(
            _collection(), {}, {}, keys, _sweep(worker="精査"), None, None, None, [],
        ))

    def _closes_after(self, monkeypatch, seen, calls, percent):
        """**呼んだ回数がある数に達したら窓が閉まる**、を仕込む。"""
        real = workers.pick

        def closing(worker, limit=None, now=""):
            if len(seen) >= calls:
                _quota("antigravity", percent)
            return real(worker, limit, now)

        monkeypatch.setattr(workers, "pick", closing)

    def test_it_moves_on_when_the_window_closes_mid_run(self, asking, monkeypatch):
        main, seen = asking
        workers.save([workers.Worker(
            "精査", (workers.Step("antigravity"), workers.Step("codex")),
        )])
        _quota("antigravity", 41.0)
        _quota("codex", 10.0)

        self._closes_after(monkeypatch, seen, 1, 89.0)

        collected, _cursor, _note = self._run(main, ["a", "b", "c"])

        assert seen == ["antigravity", "codex", "codex"]
        assert len(collected) == 3

    def test_what_was_collected_is_not_thrown_away(self, asking, monkeypatch):
        """**残りの区画は見送るが、集めたぶんは焼く。** 捨てると、窓が閉まった
        時点までの仕事がまるごと消える(実際にそうなった)。
        """
        main, seen = asking
        workers.save([workers.Worker("精査", (workers.Step("antigravity"),))])
        _quota("antigravity", 41.0)

        self._closes_after(monkeypatch, seen, 2, 95.0)

        collected, _cursor, note = self._run(main, ["a", "b", "c"])

        assert len(collected) == 2
        assert "見送りました" in note

    def test_nothing_collected_at_all_is_refused(self, asking):
        """1 件も集まっていないなら、控えに理由を残して断る(予定は進めない)。"""
        import fastapi

        main, _seen = asking
        workers.save([workers.Worker("精査", (workers.Step("antigravity"),))])
        _quota("antigravity", 95.0)

        with pytest.raises(fastapi.HTTPException) as got:
            self._run(main, ["a", "b"])

        assert got.value.status_code == 429

    def test_a_quota_refusal_moves_to_the_next_step(self, asking, monkeypatch):
        """相手自身が枠切れを返したら、その場で次の段へ回す。"""
        import fastapi

        main, seen = asking
        workers.save([workers.Worker(
            "精査", (workers.Step("antigravity"), workers.Step("codex")),
        )])
        _quota("antigravity", 41.0)
        _quota("codex", 10.0)

        async def refusing(asked, _messages):
            seen.append(asked.backend)
            if asked.backend == "antigravity":
                raise fastapi.HTTPException(502, {
                    "error": "llm error 502",
                    "reason": "antigravity failed / error: Individual quota reached."
                              " Resets in 1h15m20s.",
                })
            return ('{"items": [{"title": "1 件", "body": "本文"}]}',
                    asked.backend, asked.model or "")

        monkeypatch.setattr(main, "_ask_for_collection", refusing)

        collected, _cursor, _note = self._run(main, ["a"])

        assert seen == ["antigravity", "codex"]
        assert len(collected) == 1
        # 断られた事実は控える（次の回は待たずに次の段から始まる）
        assert workers.full_until("antigravity")

    def test_a_plain_failure_is_not_swallowed(self, asking, monkeypatch):
        """**枠と関係ない失敗で相手を締め出さない。** つながらないだけの回もある。"""
        import fastapi

        main, _seen = asking
        workers.save([workers.Worker("精査", (workers.Step("antigravity"),))])
        _quota("antigravity", 10.0)

        async def broken(_asked, _messages):
            raise fastapi.HTTPException(502, {"error": "llm unreachable"})

        monkeypatch.setattr(main, "_ask_for_collection", broken)

        with pytest.raises(fastapi.HTTPException):
            self._run(main, ["a"])

        assert not workers.full_until("antigravity")


class TestRunningItByHandInstead:
    """画面の「今すぐ実行」は**行列を通さず、その場で走らせる**。

    行列に居たぶんは、その場で外す —— 外さないと、あとでワーカーが起きたときに
    もう一度同じ回が流れて、枠を 1 回ぶん余計に食う。
    """

    def test_a_hand_run_takes_it_out_of_the_queue(self, enabled, monkeypatch):
        from app import collect, main

        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(enabled / "corpus"))
        monkeypatch.setattr("app.views.admin.TRIGGER_URL", "http://trigger")
        monkeypatch.setattr("app.views.admin.trigger_run", lambda _name: None)
        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        collect.create("news", prompt="p", interval_minutes=60,
                       sweeps=[{"name": "ざっと", "worker": "精査"}])
        workers.enqueue("精査", "news", "ざっと", "2026-01-01T00:00:00+00:00")
        assert workers.queued("精査")

        main.start_collection_bake("news", "ざっと")

        assert workers.queued("精査") == []

    def test_the_queue_is_cleared_from_either_list(self, enabled):
        """流している最中の塊に居ても外す(押されたのはその回そのもの)。"""
        workers.enqueue("精査", "news", "ざっと", "2026-01-01T00:00:00+00:00")
        workers.claim("精査", 1, "2026-01-01T00:00:00+00:00")

        workers.done("精査", "news", "ざっと")

        assert workers.queued("精査") == []


class TestPuttingBackARoundThatFailedToBake:
    """**焼くところで落ちた回を、時計が拾って戻す**(`main._rewind_failed_bakes`)。

    取り込みは収集の名前しか運べず、終わったことを教えに来る道も無い ——
    落ちたことが分かるのは取り込みの状態だけなので、こちらから拾いに行く。
    """

    @pytest.fixture
    def ran(self, enabled, monkeypatch):
        from app import collect

        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(enabled / "corpus"))
        monkeypatch.setattr("app.views.admin.TRIGGER_URL", "http://trigger")
        collect.create("news", prompt="p", interval_minutes=60,
                       sweeps=[{"name": "ざっと"}])
        collect.update("news", cursor="a", partitions=[{"key": "あ", "count": 1}])
        collect.record_result(
            "news", status="ok", sweep="ざっと", visited=["あ"], next_cursor="b",
        )
        return collect

    def _status(self, monkeypatch, job):
        monkeypatch.setattr("app.views.admin._fetch_trigger_status", lambda: job)

    def _window(self, collection):
        at = collection.get("news").last_undo["at"]
        return {"started_at": at, "finished_at": at}

    def test_the_failure_showing_now_is_put_back(self, ran, monkeypatch):
        from app import main

        self._status(monkeypatch, {
            "state": "error", "source": "news",
            "error": "validation failed: only 5 docs (< 9)", **self._window(ran),
        })

        main._rewind_failed_bakes()

        item = ran.get("news")
        assert item.cursor == "a"
        assert item.last_status == "error"
        assert partitioning.progress(item.partitions, "ざっと") == (0, 1)

    def test_a_failure_that_has_already_been_pushed_aside_is_put_back(self, ran, monkeypatch):
        """**次の取り込みが始まっていれば `last_failure` に退く。** 1 分の周期の
        合間に次が始まった回を取りこぼさない。
        """
        from app import main

        self._status(monkeypatch, {
            "state": "running", "source": "painters",
            "last_failure": {"source": "news", "error": "boom", **self._window(ran)},
        })

        main._rewind_failed_bakes()

        assert ran.get("news").cursor == "a"

    def test_a_source_that_is_not_a_collection_is_passed_over(self, ran, monkeypatch):
        """地図辞典などの取り込みが落ちても、ここは何もしない。"""
        from app import main

        self._status(monkeypatch, {
            "state": "error", "source": "jawiki", "error": "boom", **self._window(ran),
        })

        main._rewind_failed_bakes()

        assert ran.get("news").cursor == "b"

    def test_a_round_that_baked_is_left_alone(self, ran, monkeypatch):
        """**戻すのは、その取り込みが運んだ回だけ。**"""
        from app import main

        self._status(monkeypatch, {
            "state": "error", "source": "news", "error": "boom",
            "started_at": "2020-01-01T00:00:00+00:00",
            "finished_at": "2020-01-02T00:00:00+00:00",
        })

        main._rewind_failed_bakes()

        assert ran.get("news").cursor == "b"
        assert partitioning.progress(ran.get("news").partitions, "ざっと") == (1, 1)

    def test_an_unreachable_trigger_does_not_stop_the_clock(self, ran, monkeypatch):
        from app import main

        def boom():
            raise RuntimeError("no route to host")

        monkeypatch.setattr("app.views.admin._fetch_trigger_status", boom)

        main._rewind_failed_bakes()  # 落ちない

        assert ran.get("news").cursor == "b"


class TestNotStartingOnTopOfARunningOne:
    """取り込みは同時に 1 本。**走っている最中は起こさない**。

    どのみち trigger に断られるが、**断られる前に控え(`pending_sweep`)を
    書いてしまう** —— 残ると、いま走っている取り込みがその巡回のつもりで
    素材を取りに来る(押した覚えのない回が、押した覚えのない設定で走る)。
    """

    @pytest.fixture
    def collection(self, enabled, monkeypatch):
        from app import collect

        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(enabled / "corpus"))
        monkeypatch.setattr("app.views.admin.TRIGGER_URL", "http://trigger")
        collect.create("news", prompt="p", interval_minutes=60,
                       sweeps=[{"name": "ざっと"}, {"name": "整理"}])
        return collect

    def _busy(self, monkeypatch, source):
        monkeypatch.setattr(
            "app.views.admin._fetch_trigger_status",
            lambda: {"state": "running", "source": source},
        )

    def test_it_refuses_while_one_is_running(self, collection, monkeypatch):
        import fastapi

        from app import main

        self._busy(monkeypatch, "tazuna_meals")

        with pytest.raises(fastapi.HTTPException) as got:
            main.start_collection_bake("news", "整理")

        assert got.value.status_code == 409
        assert "tazuna_meals" in got.value.detail["error"]

    def test_the_pending_mark_is_left_alone(self, collection, monkeypatch):
        """**断られた回の控えを残さない。** 残すと、走っている取り込みが
        その巡回のつもりで素材を取りに来る。
        """
        import fastapi

        from app import main

        collection.mark_pending("news", "ざっと")
        self._busy(monkeypatch, "tazuna_meals")

        with pytest.raises(fastapi.HTTPException):
            main.start_collection_bake("news", "整理")

        assert collection.get("news").pending_sweep == "ざっと"

    def test_it_is_put_back_when_the_trigger_refuses(self, collection, monkeypatch):
        """**擦れ違ったときも戻す。** 空いて見えた直後に他が入ることはある。"""
        import fastapi

        from app import main

        collection.mark_pending("news", "ざっと")
        monkeypatch.setattr("app.views.admin._fetch_trigger_status", lambda: {"state": "idle"})

        def refused(_name):
            raise fastapi.HTTPException(409, {"error": "a job is already running"})

        monkeypatch.setattr("app.views.admin.trigger_run", refused)

        with pytest.raises(fastapi.HTTPException):
            main.start_collection_bake("news", "整理")

        assert collection.get("news").pending_sweep == "ざっと"

    def test_it_is_kept_when_the_running_job_is_this_collection(
        self, collection, monkeypatch,
    ):
        """**負けたほうは控えを消さない。** 時計はプロセスごとに立っているので
        (`--workers 2`)、同じ組を同時に起こしにいく —— 戻すと、勝ったほうが
        起こした取り込みが読む控えが消え、別の巡回として素材が組まれる。
        """
        import fastapi

        from app import main

        status = {"state": "idle"}
        monkeypatch.setattr("app.views.admin._fetch_trigger_status", lambda: status)

        def refused(_name):
            # もう 1 本の時計が先に起こした後(この収集が走っている)
            status.update({"state": "running", "source": "news"})
            raise fastapi.HTTPException(409, {"error": "a job is already running: news"})

        monkeypatch.setattr("app.views.admin.trigger_run", refused)

        with pytest.raises(fastapi.HTTPException):
            main.start_collection_bake("news", "整理")

        assert collection.get("news").pending_sweep == "整理"

    def test_waking_a_worker_says_why_it_cannot(self, enabled, monkeypatch):
        import fastapi

        from app import main

        monkeypatch.setattr("app.views.admin.TRIGGER_URL", "http://trigger")
        self._busy(monkeypatch, "tazuna_meals")
        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 10.0)
        workers.enqueue("精査", "news", "ざっと", "2026-01-01T00:00:00+00:00")

        with pytest.raises(fastapi.HTTPException) as got:
            main.wake_worker("精査")

        assert got.value.status_code == 409
        # **行列は残す**（順番が飛ばないことを、断り文でも言う）
        assert "順番は飛びません" in got.value.detail["hint"]
        assert workers.queued("精査")


class TestWakingItByHand:
    """時計を待たずに 1 本流す(`main.wake_worker` / 画面の「今すぐ起こす」)。

    **枠が明いているうちに回しておきたい、が普通に起きる** —— 次の起動まで待つと、
    待っているあいだに他の依頼が枠を食う(実測で、外からの 1 回が 5 時間枠を
    48 ポイント持っていった)。
    """

    @pytest.fixture
    def baking(self, enabled, monkeypatch, tmp_path):
        from app import main

        started: list[tuple] = []
        monkeypatch.setattr(
            main, "start_collection_bake",
            lambda name, sweep=None: started.append((name, sweep)),
        )
        # **行列の中身は実在する収集にする。** 流す段は流す前に「まだ走らせて
        # よいか」を確かめる(`_no_longer_due`)ので、定義の無い名前は外される
        _define(monkeypatch, tmp_path, "news", "ざっと", "整理")
        return main, started

    def test_it_runs_without_waiting_for_the_clock(self, baking):
        """**間隔が来ていなくても流す。** それが押した意味。"""
        main, started = baking
        workers.save([workers.Worker("精査", (workers.Step("codex"),), interval_minutes=600)])
        _quota("codex", 10.0)
        workers.enqueue("精査", "news", "ざっと", "2026-01-01T00:00:00+00:00")
        workers.claim("精査", 1, "2099-01-01T00:00:00+00:00")
        workers.done("精査", "news", "ざっと")
        workers.enqueue("精査", "news", "整理", "2026-01-01T00:00:00+00:00")

        # 時計では起きない（前回起きたのが未来の時刻になっている）
        assert not main._run_one_from_a_worker()

        main.wake_worker("精査")

        assert started == [("news", "整理")]

    def test_an_empty_queue_says_so(self, baking):
        """**理由を書き分ける。** 行列が空なのか枠が詰まっているのかで、
        次にすることが逆になる(積むのを待つ / 窓が明くのを待つ)。
        """
        import fastapi

        main, _started = baking
        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 10.0)

        with pytest.raises(fastapi.HTTPException) as got:
            main.wake_worker("精査")

        assert got.value.status_code == 409
        assert "待ち行列は空" in got.value.detail["error"]

    def test_all_crowded_says_so(self, baking):
        import fastapi

        main, _started = baking
        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 95.0)
        workers.enqueue("精査", "news", "ざっと", "2026-01-01T00:00:00+00:00")

        with pytest.raises(fastapi.HTTPException) as got:
            main.wake_worker("精査")

        assert got.value.status_code == 429

    def test_an_unknown_worker_is_404(self, baking):
        import fastapi

        main, _started = baking
        with pytest.raises(fastapi.HTTPException) as got:
            main.wake_worker("いない")
        assert got.value.status_code == 404


class TestWhoActuallyRan:
    """控えに残すのは、決めた相手ではなく**頼んだ相手**。

    ワーカーを使う巡回は自分の欄に相手を書いていないので、書き換えないと
    履歴の相手の欄が既定の名前で埋まる(実際にそうなっていた)。
    """

    def test_it_names_the_backend_the_worker_chose(self):
        from app import main

        out = main._who_ran([workers.Step("codex", "gpt-5.5")] * 3)

        assert out == {"backend": "codex", "model": "gpt-5.5", "effort": ""}

    def test_a_relayed_run_names_both(self):
        """**1 つに丸めない** —— どちらの相手もその回を走らせている。"""
        from app import main

        out = main._who_ran([
            workers.Step("antigravity", "gemini"), workers.Step("codex", "gpt-5.5"),
        ])

        assert out["backend"] == "antigravity → codex"
        assert out["model"] == "gemini → gpt-5.5"

    def test_a_failed_run_still_names_who_answered(self, enabled, monkeypatch):
        """**落ちた回にも頼んだ相手を残す。** 既定の名前のままだと、控えを見た人は
        「claude が壊れた答えを返した」と読む —— 実際に返したのは別の相手だった。
        """
        import fastapi

        from app import collect, main

        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(enabled / "corpus"))
        workers.save([workers.Worker("精査", (workers.Step("antigravity", "gemini"),))])
        _quota("antigravity", 10.0)

        async def garbage(asked, _messages):
            return "背景の仕事を待っています", asked.backend, asked.model or ""

        monkeypatch.setattr(main, "_ask_for_collection", garbage)
        collect.create("news", prompt="p", interval_minutes=60,
                       sweeps=[{"name": "ざっと", "worker": "精査"}])

        used: list = []
        with pytest.raises((ValueError, fastapi.HTTPException)):
            asyncio.run(main._collect_items(
                collect.get("news"), {}, {}, [], collect.sweeps_of(collect.get("news"))[0],
                None, None, None, used,
            ))

        assert main._who_ran(used)["backend"] == "antigravity"

    def test_nothing_ran_leaves_the_record_alone(self):
        """AI を呼ばない回では書き換えない(既定の相手が並ぶのを防ぐ)。"""
        from app import main

        assert main._who_ran([]) == {}


class TestSavingOne:
    """保存は **その 1 つだけ**を書き換える(足す口も消す口も名前 1 つ)。"""

    def _one(self, name: str) -> workers.Worker:
        return workers.Worker(name, (workers.Step("codex"),))

    def test_a_new_name_is_added(self):
        out = workers.merged([self._one("ざっと")], "", "精査",
                             (workers.Step("claude"),))

        assert [w.name for w in out] == ["ざっと", "精査"]

    def test_an_existing_one_is_replaced_in_place(self):
        """**並びを崩さない** —— 順番は人が決めたもの。"""
        out = workers.merged([self._one("ざっと"), self._one("精査")], "ざっと", "ざっと",
                             (workers.Step("antigravity"),))

        assert [w.name for w in out] == ["ざっと", "精査"]
        assert out[0].steps == (workers.Step("antigravity"),)

    def test_clearing_the_name_removes_it(self):
        out = workers.merged([self._one("ざっと"), self._one("精査")], "精査", "", ())

        assert [w.name for w in out] == ["ざっと"]

    def test_renaming_keeps_the_place(self):
        """巡回側の名指しは追いかけない —— 同じ名前の別物を作ったときに取り違える。"""
        out = workers.merged([self._one("ざっと"), self._one("精査")], "精査", "じっくり",
                             (workers.Step("codex"),))

        assert [w.name for w in out] == ["ざっと", "じっくり"]


class TestTheEditorOnTheScreen:
    """段ごとに相手・モデル・考える量を選ぶ欄(`app/views/ai_workers.py`)。"""

    def _selects(self):
        from app.views import admin

        return (admin._backend_select, admin._model_select)

    def test_each_step_is_its_own_row(self, enabled):
        """**段は何本でも並ぶ。** 行で囲わないと、どの段を選び直しても
        先頭の段のモデルが入れ替わる。
        """
        from app.views import ai_workers

        html = ai_workers._worker_form(_worker(workers.Step("codex")), self._selects())

        assert html.count('<div class="sweep-row">') >= 2
        assert '<select name="step_model">' in html
        # **考える量の欄は持たない** —— モデルの名前に畳んであるので、
        # 残すと選べるのに効かない欄になる
        assert "step_effort" not in html

    def test_the_picker_reaches_this_form_too(self, enabled):
        """欄の名前が `step_backend` なので、前置きを場合分けで書くと
        ここだけ黙って何もしない側へ落ちる。
        """
        from app import pages
        from app.views import ai_workers

        page = pages.page_shell(
            "AI と鍵", ai_workers._worker_form(None, self._selects())
        )

        assert pages.BACKEND_PICKER_SCRIPT in page
        assert "'backend'.length" in pages.BACKEND_PICKER_SCRIPT


class TestPickingAWorkerAsTheBackend:
    """ワーカーは**相手と同じ欄**で選ぶ。

    欄を分けていた頃は相手とワーカーの両方を選べて、**どちらが効くのかが画面から
    読めなかった**(効くのはワーカー)。1 つの欄にすれば、選べるのは片方だけになる。
    """

    def test_the_value_round_trips(self):
        assert workers.named_in(workers.option_for("精査")) == "精査"
        assert workers.named_in("codex") == ""
        assert workers.named_in("") == ""

    def test_the_select_lists_them_apart_from_the_backends(self, enabled):
        from app.views import admin

        workers.save([_worker(workers.Step("codex"))])
        html = admin._backend_select(None, "sweep_backend", with_workers=True)

        assert "<optgroup" in html
        assert f'value="{workers.option_for("精査")}"' in html

    def test_a_worker_step_cannot_point_at_a_worker(self, enabled):
        """並べると、ワーカーがワーカーを指せてしまう。"""
        from app.views import admin

        workers.save([_worker(workers.Step("codex"))])

        assert "<optgroup" not in admin._backend_select(None, "step_backend")

    def test_no_workers_means_no_group(self, enabled):
        """空の見出しだけが並ぶと、設定し忘れているように見える。"""
        from app.views import admin

        assert "<optgroup" not in admin._backend_select(None, "sweep_backend", True)

    def test_saving_splits_the_worker_out_of_the_backend_field(self, enabled):
        from starlette.datastructures import FormData

        from app.views import admin

        form = FormData([
            ("sweep_name", "精査の回"),
            ("sweep_backend", workers.option_for("精査")),
            ("sweep_model", "sonnet"),
            ("sweep_effort", "high"),
        ])
        [sweep] = admin._parse_sweeps_form(form)

        assert sweep["worker"] == "精査"
        # **どの相手に渡るかはそのときの枠で決まる。** ここに 1 つ書いても、
        # どの相手に対する指定なのかが決まらない(モデルの名前は相手ごとに違う)
        assert "backend" not in sweep
        assert "model" not in sweep and "effort" not in sweep

    def test_a_plain_backend_keeps_its_model_and_effort(self, enabled):
        from starlette.datastructures import FormData

        from app.views import admin

        form = FormData([
            ("sweep_name", "整理"),
            ("sweep_backend", "codex"),
            ("sweep_model", "gpt-5.6-terra"),
            ("sweep_effort", "medium"),
        ])
        [sweep] = admin._parse_sweeps_form(form)

        assert sweep["worker"] == ""
        assert (sweep["backend"], sweep["model"], sweep["effort"]) == \
            ("codex", "gpt-5.6-terra", "medium")

    def test_the_form_hides_the_fields_that_cannot_apply(self, enabled):
        """**選べるのに効かない欄は「設定したつもり」を作る。**"""
        from app import collect
        from app.views import admin

        picked = collect.Sweep("精査の回", "", 60, True, None, None, None, worker="精査")
        html = admin._sweep_backend_fields(picked, None, mechanical=False)

        assert 'name="sweep_model"' not in html
        assert 'name="sweep_effort"' not in html
        assert "ワーカーの段が持ちます" in html

    def test_the_row_says_it_is_a_worker_not_one_backend(self, enabled):
        """相手の名前を出すと、その 1 つに固定で頼んでいるように読める。"""
        from app import collect
        from app.views import admin

        picked = collect.Sweep("精査の回", "", 60, True, "codex", "x", "high", worker="精査")

        assert "精査" in admin._backend_label(picked)
        assert "枠を見て振り替える" in admin._backend_label(picked)


class TestAskingForOneFromOutside:
    """外のアプリ(tazuna など)が、収集を頼むときにワーカーを名指しできること。

    **一覧に無いものは選ばせようがない。** 相手の一覧に混ぜないと、ワーカーに頼む
    巡回は Chiezo の画面からしか作れない機能になる。
    """

    @pytest.fixture()
    def client(self, enabled, monkeypatch):
        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_NOTES_DIR", str(enabled / "notes"))
        from app.main import app

        with TestClient(app) as c:
            yield c

    def test_the_backends_api_lists_them(self, client, enabled):
        workers.save([_worker(workers.Step("codex"))])

        found = client.get("/v1/ai/backends").json()["backends"]
        mine = [b for b in found if b["kind"] == "worker"]

        assert [b["id"] for b in mine] == [workers.option_for("精査")]
        assert mine[0]["label"] == "精査"

    def test_a_worker_offers_no_model_or_effort(self, client, enabled):
        """渡す先はそのときの枠で決まるので、ここで 1 つ選んでも
        どの相手に対する指定なのかが決まらない。
        """
        workers.save([_worker(workers.Step("codex"))])

        [mine] = [b for b in client.get("/v1/ai/backends").json()["backends"]
                  if b["kind"] == "worker"]

        assert mine["models"] == [] and mine["efforts"] == []
        assert mine["model_required"] is False

    def test_the_backends_keep_their_kind(self, client, enabled):
        """足したのは後からなので、書いていない読み手が今までどおり動くこと。"""
        from app import settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        workers.save([_worker(workers.Step("codex"))])

        found = client.get("/v1/ai/backends").json()["backends"]

        assert [b["kind"] for b in found if b["id"] == "gemini"] == ["backend"]
        assert all(b["kind"] in ("backend", "worker") for b in found)

    def test_no_workers_means_only_backends(self, client, enabled):
        found = client.get("/v1/ai/backends").json()["backends"]

        assert [b for b in found if b["kind"] == "worker"] == []

    def test_the_value_goes_straight_into_a_sweep(self, enabled):
        """**呼ぶ側に「ワーカーは別の欄へ」を覚えさせない。**

        一覧から選んだ値をそのまま `backend` に入れて送れば、Chiezo が振り分ける。
        """
        from app import collect

        [sweep] = collect.normalize_sweeps([
            {"name": "精査の回", "backend": workers.option_for("精査"),
             "model": "sonnet", "effort": "high"},
        ])

        assert sweep["worker"] == "精査"
        assert sweep["backend"] is None
        assert sweep["model"] is None and sweep["effort"] is None

    def test_writing_the_worker_field_still_works(self, enabled):
        """前からの書き方を壊さない。"""
        from app import collect

        [sweep] = collect.normalize_sweeps([{"name": "精査の回", "worker": "精査"}])

        assert sweep["worker"] == "精査"

    def test_a_plain_backend_is_left_alone(self, enabled):
        from app import collect

        [sweep] = collect.normalize_sweeps([
            {"name": "整理", "backend": "codex", "model": "gpt-5.6-terra"},
        ])

        assert sweep.get("worker") is None
        assert sweep["backend"] == "codex" and sweep["model"] == "gpt-5.6-terra"


class TestReadingWhyItIsNotMoving:
    """ワーカーの節に、回り方と待ち行列を出す。

    **積まれているのに動かないなら**枠が詰まっているか起動待ち、**積まれていない
    なら**巡回の側がまだ積んでいない —— どちらなのかは、時刻と行列を並べないと
    外から判らない。
    """

    def _form(self, worker):
        from app.views import admin, ai_workers

        return ai_workers._worker_form(
            worker, (admin._backend_select, admin._model_select)
        )

    def test_it_says_when_it_last_woke_and_when_it_will(self, enabled):
        workers.enqueue("精査", "c", "整理", "2026-09-19T00:00:00+00:00")
        workers.claim("精査", 1, "2026-09-19T08:00:00+00:00")
        worker = workers.Worker("精査", (workers.Step("codex"),), interval_minutes=30)

        html = self._form(worker)

        assert "2026-09-19 17:00 JST" in html, "前回起きた"
        assert "2026-09-19 17:30 JST" in html, "次に起きる(前回 + 間隔)"

    def test_one_that_never_woke_says_so(self, enabled):
        html = self._form(workers.Worker("精査", (workers.Step("codex"),)))

        assert "まだ" in html
        assert "いますぐ" in html

    def test_the_queue_is_listed_in_order(self, enabled):
        for name in ("aa", "bb", "cc"):
            workers.enqueue("精査", name, "整理", "2026-09-19T00:00:00+00:00")

        html = self._form(workers.Worker("精査", (workers.Step("codex"),)))

        assert html.index("aa") < html.index("bb") < html.index("cc")

    def test_the_one_being_run_is_marked(self, enabled):
        for name in ("aa", "bb"):
            workers.enqueue("精査", name, "整理", "2026-09-19T00:00:00+00:00")
        workers.claim("精査", 1, "2026-09-19T08:00:00+00:00")

        html = self._form(workers.Worker("精査", (workers.Step("codex"),)))

        assert "いま流している" in html

    def test_an_empty_queue_says_so(self, enabled):
        """**空の表を出さない** —— 「まだ動いていない」と読めてしまう。"""
        html = self._form(workers.Worker("精査", (workers.Step("codex"),)))

        assert "待っているものはありません" in html

    def test_it_is_not_folded_away(self, enabled):
        """動いているかを確かめに来る場所なので、開かないと読めないのでは値打ちが消える。"""
        workers.enqueue("精査", "aa", "整理", "2026-09-19T00:00:00+00:00")

        html = self._form(workers.Worker("精査", (workers.Step("codex"),)))

        assert "<details><summary>待ち行列" not in html


class TestReorderingTheSteps:
    """段の並びは「詰まったら次へ」の順そのもの。**入れ替えるのに打ち直させない。**

    並びを変えるには相手を選び直すしかなく、3 段あれば 3 つとも選び直すことに
    なっていた —— そのあいだに 1 つ間違えると、無人で回る層が別の相手に回り続ける。
    """

    def _post(self, **extra):
        from starlette.datastructures import FormData

        rows = extra.pop("rows", [("codex", ""), ("antigravity", ""), ("claude", "")])
        items = [("worker_key", "精査"), ("worker_name", "精査")]
        for backend, model in rows:
            items += [("step_backend", backend), ("step_model", model)]
        items += list(extra.items())
        return FormData(items)

    async def _save(self, form):
        from app.views import ai_workers

        class _Request:
            async def form(self):
                return form

        await ai_workers.save_worker(_Request())
        return [s.backend for s in workers.load()[0].steps]

    def test_up_swaps_with_the_one_before(self, enabled):
        got = asyncio.run(self._save(self._post(step_move="up:1")))
        assert got == ["antigravity", "codex", "claude"]

    def test_down_swaps_with_the_one_after(self, enabled):
        got = asyncio.run(self._save(self._post(step_move="down:0")))
        assert got == ["antigravity", "codex", "claude"]

    def test_the_typed_rows_are_kept(self, enabled):
        """**書きかけの欄も一緒に保存してから動く**(別のフォームにはできない)。"""
        rows = [("codex", "gpt-6"), ("antigravity", "gemini-3.8-flash")]
        got = asyncio.run(self._save(self._post(rows=rows, step_move="up:1")))
        assert got == ["antigravity", "codex"]
        assert [s.model for s in workers.load()[0].steps] == ["gemini-3.8-flash", "gpt-6"]

    def test_the_ends_do_not_move(self, enabled):
        """画面は端にボタンを出さないが、押された形は入ってくる。"""
        assert asyncio.run(self._save(self._post(step_move="up:0"))) == [
            "codex", "antigravity", "claude"
        ]
        assert asyncio.run(self._save(self._post(step_move="down:2"))) == [
            "codex", "antigravity", "claude"
        ]

    def test_a_move_we_cannot_read_changes_nothing(self, enabled):
        for raw in ("", "sideways:1", "up:", "up:x", "up:99"):
            assert asyncio.run(self._save(self._post(step_move=raw))) == [
                "codex", "antigravity", "claude"
            ], raw

    def test_the_screen_shows_the_arrows_only_where_they_work(self, enabled):
        from app.views import admin, ai_workers

        workers.save([_worker(workers.Step("codex"), workers.Step("antigravity"))])
        html = ai_workers.section_html(
            (admin._backend_select, admin._model_select)
        )

        # 2 段あるので ↑ は 2 番目だけ、↓ は 1 番目だけ。空の 3 行目には出ない
        assert 'value="up:1"' in html and 'value="up:0"' not in html
        assert 'value="down:0"' in html and 'value="down:1"' not in html
        assert 'value="down:2"' not in html and 'value="up:2"' not in html


class TestAStoppedCollectionDoesNotRun:
    """**止めたことは、行列にも効かないといけない。**

    積む段は止まっている収集を飛ばすが、**積んだあとに止めたぶんは行列に残って
    いて、そのまま流れていた** —— 押した「止める」が効かず、1 回ぶんの取り込みと
    AI の枠を食う。
    """

    @pytest.fixture()
    def ready(self, enabled, monkeypatch, tmp_path):
        import app.main as m
        import app.views.admin as admin
        from app import collect

        started: list[str] = []
        # `TRIGGER_URL` は import のときに読む定数なので、環境変数では動かない
        monkeypatch.setattr(admin, "TRIGGER_URL", "http://trigger.invalid/")
        monkeypatch.setattr(admin, "trigger_run", lambda name: started.append(name))
        monkeypatch.setattr(m, "ingest_busy", lambda: None)

        def make(name: str) -> None:
            _define(monkeypatch, tmp_path, name, "ざっと見る")

        workers.save([_worker(workers.Step("codex"))])
        return m, collect, make, started

    def _flush(self, m):
        from datetime import UTC, datetime

        return m._flush_one(workers.get("精査"), datetime.now(UTC), wake=True)

    def test_it_runs_while_the_collection_is_on(self, ready):
        m, _collect, make, started = ready
        make("meals")
        m._fill_worker_queues()

        assert self._flush(m) is True
        assert started == ["meals"]

    def test_stopping_it_takes_it_off_the_queue(self, ready):
        m, collect, make, started = ready
        make("meals")
        m._fill_worker_queues()
        assert workers.queued("精査")

        collect.update("meals", enabled=False)
        m._fill_worker_queues()

        assert workers.queued("精査") == []
        assert self._flush(m) is False
        assert started == []

    def test_stopping_the_sweep_alone_also_counts(self, ready):
        m, collect, make, started = ready
        make("meals")
        m._fill_worker_queues()

        collect.update("meals", sweeps=[
            {"name": "ざっと見る", "worker": "精査", "interval_minutes": 60,
             "prompt": "{partition}", "enabled": False},
        ])
        m._fill_worker_queues()

        assert workers.queued("精査") == []
        assert started == []

    def test_a_stopped_one_in_the_batch_is_skipped_not_waited_on(self, ready):
        """**外さずに見送ると、先頭に居座ってそのワーカーが 1 本も進まない。**

        積んでから流すまでのあいだに止められた回は、行列を片付ける段を通らずに
        塊の中に残る。
        """
        m, collect, make, started = ready
        make("meals")
        make("painters")
        workers.save([workers.Worker("精査", (workers.Step("codex"),), per_run=2)])
        m._fill_worker_queues()
        assert len(workers.queued("精査")) == 2

        collect.update("meals", enabled=False)

        assert self._flush(m) is True
        assert started == ["painters"], "止まっている先頭は飛ばして次を流す"

    def test_a_stopped_head_does_not_block_the_next_round(self, ready):
        """1 度に 1 本しか拾わないワーカーでも、次の周で先へ進むこと。"""
        m, collect, make, started = ready
        make("meals")
        make("painters")
        m._fill_worker_queues()
        collect.update("meals", enabled=False)

        assert self._flush(m) is False, "止まっているぶんしか拾っていない周"
        m._fill_worker_queues()

        assert self._flush(m) is True
        assert started == ["painters"]

    def test_a_collection_that_is_gone_is_dropped_too(self, ready):
        m, collect, make, started = ready
        make("meals")
        m._fill_worker_queues()

        collect.remove("meals")
        m._fill_worker_queues()

        assert workers.queued("精査") == []
        assert started == []

    def test_the_by_hand_button_can_still_run_a_stopped_one(self, ready):
        """画面の「今すぐ実行」は試し撃ちなので、止めてあっても走らせる ——
        有効にする前に試せる道を塞がない。"""
        m, collect, make, started = ready
        make("meals")
        collect.update("meals", enabled=False)

        m.start_collection_bake("meals", "ざっと見る")

        assert started == ["meals"]


class TestRunningItByHandClearsTheQueue:
    """**走らせたのだから、もうどこにも積まれていてはいけない。**

    画面の「今すぐ実行」も、外のアプリからの依頼(`POST /v1/collect/{name}/run`)も
    行列を通さずその場で走らせる —— 残すと、そのワーカーが起きたときに同じ回が
    もう一度流れて、枠を 1 回ぶん余計に食う。
    """

    def test_it_leaves_the_queue_of_the_worker_that_holds_it(self, enabled):
        workers.enqueue("精査", "news", "ざっと見る", "2026-09-23T00:00:00+00:00")

        workers.drop("news", "ざっと見る")

        assert workers.queued("精査") == []

    def test_it_looks_in_every_worker(self, enabled):
        """**積んだ後に巡回のワーカーを付け替えれば、積まれているのは前のほう**
        —— いま書いてあるワーカーだけ見ても居ない(試し撃ちで相手を上書きした
        回も同じ)。誰の行列に居るかを当てに行かない。
        """
        workers.enqueue("前のワーカー", "news", "ざっと見る", "2026-09-23T00:00:00+00:00")

        workers.drop("news", "ざっと見る")

        assert workers.queued("前のワーカー") == []

    def test_other_sweeps_are_left_alone(self, enabled):
        workers.enqueue("精査", "news", "ざっと見る", "2026-09-23T00:00:00+00:00")
        workers.enqueue("精査", "news", "整理", "2026-09-23T00:00:00+00:00")
        workers.enqueue("精査", "ほか", "ざっと見る", "2026-09-23T00:00:00+00:00")

        workers.drop("news", "ざっと見る")

        left = {(e["collection"], e["sweep"]) for e in workers.queued("精査")}
        assert left == {("news", "整理"), ("ほか", "ざっと見る")}

    def test_it_takes_it_out_of_the_claimed_batch_too(self, enabled):
        """**拾われた後でも外す** —— 塊に移っているだけで、流れるのはこれから。"""
        workers.enqueue("精査", "news", "ざっと見る", "2026-09-23T00:00:00+00:00")
        workers.claim("精査", 5, "2026-09-23T00:01:00+00:00")

        workers.drop("news", "ざっと見る")

        assert workers.queued("精査") == []
        assert not workers.claim_ready("精査")

    def test_nothing_named_does_nothing(self, enabled):
        workers.enqueue("精査", "news", "ざっと見る", "2026-09-23T00:00:00+00:00")

        workers.drop("", "ざっと見る")
        workers.drop("news", "")

        assert len(workers.queued("精査")) == 1
