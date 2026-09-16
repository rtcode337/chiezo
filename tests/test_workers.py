"""ワーカー(app/workers.py)のテスト。

**押さえているのは 3 つ**: ①優先度順に見て枠に余裕のある相手を選ぶ、
②どれも詰まっていたら「待て」を返す(相手がいない、ではない)、
③枠を出さない相手を締め出さない。

③が要るのは、振り替えの仕組みが「枠を出せる相手しか使えない」ものになると、
いちばん頼りたい相手を外す羽目になるため。
"""
from __future__ import annotations

import json

import pytest

from app import machine_store, usage_store, workers


@pytest.fixture
def enabled(tmp_path, monkeypatch):
    """機械の置き場(`state/machine.db`)と使用量の控えを同じところに用意する。"""
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return tmp_path


def _quota(provider: str, percent: float) -> None:
    usage_store.save_quota(provider, [{"id": "primary", "label": "直近 5 時間",
                                       "used_percent": percent}])


def _worker(*steps: workers.Step) -> workers.Worker:
    return workers.Worker("精査", tuple(steps))


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


class TestDefinitions:
    """定義の読み書き。**壊れていたら黙って作り直さない。**"""

    def test_it_comes_back_as_it_went_in(self, enabled):
        workers.save([workers.Worker("精査", (
            workers.Step("codex", "gpt-5.5", "high"), workers.Step("claude"),
        ))])
        [found] = workers.load()

        assert found.name == "精査"
        assert found.steps[0] == workers.Step("codex", "gpt-5.5", "high")
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


class TestWhatTheClockDoesWithThem:
    """時計は、**枠の詰まっている回を飛ばして後ろの回を先に走らせる**。

    予定は進めないので、窓が明ければその回も次の周で走る(混んでいる trigger に
    断られたときと同じ扱い)。
    """

    def test_a_crowded_sweep_is_passed_over(self, enabled):
        from app import main

        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 95.0)
        crowded, plain = _sweep(worker="精査"), _sweep(name="ざっと")

        assert main._first_with_room([(None, crowded), (None, plain)]) == (None, plain)

    def test_nothing_runnable_means_nothing_runs(self, enabled):
        from app import main

        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 95.0)

        assert main._first_with_room([(None, _sweep(worker="精査"))]) is None

    def test_a_sweep_without_a_worker_always_runs(self, enabled):
        """枠を見て振り替えるのは、名指しした回だけの話。"""
        from app import main

        assert main._first_with_room([(None, _sweep())]) is not None


class TestDecidingWhoToAsk:
    """相手が None なのは 2 通りあり、次にすることが逆になる。"""

    def test_no_worker_means_the_sweep_decides(self, enabled):
        from app import main

        assert main._worker_step(_sweep()) == (None, False)

    def test_the_worker_names_the_backend(self, enabled):
        from app import main

        workers.save([workers.Worker("精査", (workers.Step("codex", "gpt-5.5"),))])
        _quota("codex", 10.0)

        assert main._worker_step(_sweep(worker="精査")) == (workers.Step("codex", "gpt-5.5"), True)

    def test_all_crowded_is_wait_not_fall_back(self, enabled):
        """**巡回の指定へ落ちない。** 落ちると、避けたかった相手に頼むことがある。"""
        from app import main

        workers.save([workers.Worker("精査", (workers.Step("codex"),))])
        _quota("codex", 95.0)

        assert main._worker_step(_sweep(worker="精査")) == (None, True)

    def test_a_name_that_is_not_there_falls_back(self, enabled):
        """綴りを間違えただけで無人の層が止まるより、走って控えに相手が残るほうがよい。"""
        from app import main

        assert main._worker_step(_sweep(worker="いない")) == (None, False)


class TestTheChosenBackendIsUsed:
    """選んだ相手が、実際に投げる定義に載る。"""

    def test_the_step_wins_over_the_sweep(self, enabled):
        from app import collect

        item = _collection()
        sweep = collect.Sweep(name="精査", prompt="育てて", interval_minutes=60, enabled=True,
                              backend="antigravity", model="gemini", effort="low",
                              worker="精査")
        asked = sweep.applied_to(item, workers.Step("codex", "gpt-5.5", "high"))

        assert (asked.backend, asked.model, asked.effort) == ("codex", "gpt-5.5", "high")

    def test_without_a_step_the_sweep_decides(self, enabled):
        from app import collect

        item = _collection()
        sweep = collect.Sweep(name="ざっと", prompt="育てて", interval_minutes=60, enabled=True,
                              backend="antigravity", model="gemini", effort="low")
        asked = sweep.applied_to(item)

        assert (asked.backend, asked.model, asked.effort) == ("antigravity", "gemini", "low")


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
