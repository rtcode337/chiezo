"""いま走っている AI への依頼(`/v1/ai/inflight`)。

控えの表に行が立つのは往復が終わってからなので、走っている最中は何も見えなかった。
CLI ブリッジ越しの相手は数分かかるうえ、無人で回る層が動かしているぶんは、その場に
居合わせる人がいない —— 遅いのか、止まっているのか、呼べてすらいないのかの区別が
付かないままになる。中身は残さない(`ai_log` と同じ理由)。
"""
import httpx
import pytest
from test_agent import make_client


@pytest.fixture()
def state_env(monkeypatch, built_data_dir, tmp_path):
    from app import answer

    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    answer._MODELS_CACHE.clear()
    return monkeypatch


class TestTheRecord:
    def test_it_keeps_the_size_but_not_the_prompt(self, state_env):
        from app import ai_inflight

        token = ai_inflight.begin(
            backend="claude", model="fable", effort="high",
            prompt_bytes=307383, timeout=900.0,
        )
        rows = ai_inflight.running()
        assert len(rows) == 1
        assert rows[0]["backend"] == "claude"
        assert rows[0]["prompt_bytes"] == 307383
        assert "prompt" not in rows[0]
        ai_inflight.end(token)

    def test_finishing_removes_the_row(self, state_env):
        """済んだ依頼は控えの表(成功・失敗)が引き受ける。ここに残すと二重になる。"""
        from app import ai_inflight

        token = ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=10, timeout=120.0
        )
        assert len(ai_inflight.running()) == 1
        ai_inflight.end(token)
        assert ai_inflight.running() == []

    def test_a_row_left_behind_by_a_dead_worker_expires(self, state_env):
        """**ワーカーごと落ちると `end()` を通らない。** 期限で消えないと、
        画面に「ずっと走っている依頼」が並んで本物が埋もれる。"""
        from app import ai_inflight

        # 期限を過ぎた行(待つ秒数を負にすると、入れた時点で既に期限切れ)
        ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=1,
            timeout=-ai_inflight.STALE_GRACE_SECONDS - 60,
        )
        assert ai_inflight.running() == []

    def test_the_deadline_follows_the_backend_not_one_fixed_number(self, state_env):
        """待つ秒数は相手で桁が違う(ブリッジは 900 秒、直叩きは 120 秒)。
        掃除する側が 1 つの数字で切ると、粘っている相手を消すことになる。"""
        from app import ai_inflight

        ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=1, timeout=900.0
        )
        rows = ai_inflight.running()
        assert len(rows) == 1
        assert rows[0]["expires_at"] > rows[0]["at"]

    def test_it_is_off_without_a_state_dir(self, monkeypatch, built_data_dir):
        """`CHIEZO_STATE_DIR` が機能フラグを兼ねる(`ai_log` と同じ流儀)。"""
        from app import ai_inflight

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        assert ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=1, timeout=120.0
        ) is None
        assert ai_inflight.running() == []

    def test_a_broken_record_does_not_break_the_call(self, state_env, monkeypatch):
        """走っているものが見えないことと、AI が答えられないことは別の話。"""
        import sqlite3

        from app import ai_inflight

        def boom(_path):
            raise sqlite3.OperationalError("disk is full")

        monkeypatch.setattr(ai_inflight, "_connect", boom)
        assert ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=1, timeout=120.0
        ) is None
        ai_inflight.end(1)  # 例外を投げない


class TestAroundTheCall:
    def test_the_row_is_gone_once_the_answer_comes_back(self, state_env):
        """成功しても消える。消し忘れると、終わった依頼が走り続けているように見える。"""
        from app import ai_inflight, answer

        state_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={
                        "choices": [{"message": {"role": "assistant", "content": "はい"}}]
                    })
                )
            ),
        )
        with make_client(state_env, None) as client:
            res = client.post("/v1/ai/complete", json={
                "backend": "local", "messages": [{"role": "user", "content": "こんにちは"}],
            })
            assert res.status_code == 200
            assert client.get("/v1/ai/inflight").json()["calls"] == []
        assert ai_inflight.running() == []

    def test_the_row_is_gone_even_when_the_call_fails(self, state_env):
        """**消すのは finally**。失敗のときに残ると、落ちた依頼が走り続けて見える。"""
        from app import ai_inflight, answer

        state_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(500, text='{"error": {"message": "overloaded"}}')
                )
            ),
        )
        with make_client(state_env, None) as client:
            assert client.post("/v1/ai/complete", json={
                "backend": "local", "messages": [{"role": "user", "content": "こんにちは"}],
            }).status_code == 502
        assert ai_inflight.running() == []


class TestEndpoint:
    def test_it_is_empty_before_anything_runs(self, state_env):
        with make_client(state_env, None) as client:
            assert client.get("/v1/ai/inflight").json() == {"calls": [], "jobs": []}

    def test_it_shows_what_is_running(self, state_env):
        from app import ai_inflight

        ai_inflight.begin(
            backend="claude", model="fable", effort="", prompt_bytes=42, timeout=900.0
        )
        with make_client(state_env, None) as client:
            got = client.get("/v1/ai/inflight").json()
        assert [c["backend"] for c in got["calls"]] == ["claude"]
        assert got["calls"][0]["prompt_bytes"] == 42


class TestTheScreen:
    def test_a_running_call_shows_up_at_the_top(self, state_env):
        """**ページから外して常に先頭に出す** —— 2 ページ目を見ているあいだに
        見えなくなっては、いまの状態を見る用をなさない。"""
        from app import ai_inflight
        from app.views import ai_history

        ai_inflight.begin(
            backend="claude", model="fable", effort="", prompt_bytes=42, timeout=900.0
        )
        html = ai_history.section_html()
        assert "走っている" in html
        assert "いま 1 件走っている" in html

    def test_nothing_running_does_not_offer_a_refresh(self, state_env):
        """何も走っていないのに読み直す導線を出すと、「更新すれば何か出る」と読める。"""
        from app.views import ai_history

        assert "件走っている" not in ai_history.section_html()
