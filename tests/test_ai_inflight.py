"""いま走っている AI への依頼(`/v1/ai/inflight`)。

控えの表に行が立つのは往復が終わってからなので、走っている最中は何も見えなかった。
CLI ブリッジ越しの相手は数分かかるうえ、無人で回る層が動かしているぶんは、その場に
居合わせる人がいない —— 遅いのか、止まっているのか、呼べてすらいないのかの区別が
付かないままになる。

**依頼文はこの表にだけ残す。** 失敗の控え(`ai_log`)が中身を持たないのは 500 件を
溜め続ける表だからで、こちらは終われば行ごと消える —— そして止めるかどうかを
決めるには「いま何を頼んでいるか」が要る。
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
    def test_it_keeps_the_prompt_so_you_can_tell_two_calls_apart(self, state_env):
        """**相手と大きさだけでは足りない。** 同じ相手へ似た大きさの依頼を 2 本
        投げていると、どちらを止めるべきかが読めない。"""
        from app import ai_inflight

        token = ai_inflight.begin(
            backend="claude", model="fable", effort="high",
            prompt_bytes=307383, timeout=900.0, prompt="第1章の目次を作って",
        )
        rows = ai_inflight.running()
        assert len(rows) == 1
        assert rows[0]["backend"] == "claude"
        assert rows[0]["prompt_bytes"] == 307383
        assert rows[0]["prompt"] == "第1章の目次を作って"
        ai_inflight.end(token)

    def test_a_huge_prompt_is_cut(self, state_env):
        """実測で 300KB を超える依頼がある。**大きさは元のまま**残す ——
        切ったあとの長さで数えると、失敗が大きさに寄っているのか分からなくなる。"""
        from app import ai_inflight

        huge = "あ" * (ai_inflight.PROMPT_MAX + 5_000)
        ai_inflight.begin(
            backend="claude", model="", effort="",
            prompt_bytes=len(huge.encode()), timeout=900.0, prompt=huge,
        )
        row = ai_inflight.running()[0]
        assert len(row["prompt"]) == ai_inflight.PROMPT_MAX
        assert row["prompt_bytes"] == len(huge.encode())

    def test_a_call_made_for_a_job_carries_the_job_id(self, state_env):
        """文章の生成は中で会話の口を呼ぶ。紐が無いと、1 本の依頼が
        ジョブと会話の 2 件に見える。"""
        from app import ai_inflight

        with ai_inflight.on_behalf_of("job-123"):
            ai_inflight.begin(
                backend="codex", model="", effort="", prompt_bytes=1, timeout=900.0
            )
        assert ai_inflight.running()[0]["job_id"] == "job-123"

    def test_a_call_made_on_its_own_carries_no_job_id(self, state_env):
        from app import ai_inflight

        ai_inflight.begin(
            backend="codex", model="", effort="", prompt_bytes=1, timeout=900.0
        )
        assert ai_inflight.running()[0]["job_id"] == ""

    def test_the_job_id_does_not_leak_out_of_the_block(self, state_env):
        """**外まで残ると、無関係な会話がジョブのものとして畳まれる**
        (畳んだ先のジョブはもう終わっているので、行ごと消える)。"""
        from app import ai_inflight

        with ai_inflight.on_behalf_of("job-123"):
            pass
        ai_inflight.begin(
            backend="codex", model="", effort="", prompt_bytes=1, timeout=900.0
        )
        assert ai_inflight.running()[0]["job_id"] == ""

    def test_an_old_record_without_the_new_columns_still_opens(self, state_env):
        """列を足した版を古い `state/` に当てても読めること。
        **動いている最中に入れ替わる**ので、読めなくなると走行中の表が落ちる。"""
        import sqlite3

        from app import ai_inflight

        path = ai_inflight.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
        with conn:
            conn.execute(
                "CREATE TABLE ai_inflight (id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " at TEXT NOT NULL, expires_at TEXT NOT NULL, backend TEXT NOT NULL,"
                " model TEXT NOT NULL, effort TEXT NOT NULL,"
                " kind TEXT NOT NULL DEFAULT 'chat', prompt_bytes INTEGER NOT NULL)"
            )
        conn.close()

        token = ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=3, timeout=900.0,
            prompt="やあ",
        )
        assert token is not None
        assert ai_inflight.running()[0]["prompt"] == "やあ"

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


class TestTheModelThatRan:
    """**送った名前と、走った名前は別**。選ばなかったときに送るのはただの印
    (`answer.PLACEHOLDER_MODEL`)で、実物は応答が名乗る。"""

    @staticmethod
    def _cfg(model: str):
        from app import answer

        return answer.Settings(
            url="http://x/v1", model=model, api_key=None, timeout=1.0, docs=1,
            max_chars=1, agent_max_steps=1, agent_tool_chars=200, agent_timeout=1.0,
            name="codex",
        )

    def test_the_placeholder_is_not_recorded_as_a_model(self, state_env):
        """走る前に実物は分からない。印を残すと「chiezo というモデルで走っている」
        と読める —— 相手の名前だけのほうが、嘘の名前が並ぶよりよい。"""
        from app import ai_inflight, answer

        cfg = self._cfg(answer.PLACEHOLDER_MODEL)
        with answer._inflight(cfg, [{"role": "user", "content": "やあ"}]):
            assert ai_inflight.running()[0]["model"] == ""

    def test_a_chosen_model_is_recorded_as_is(self, state_env):
        from app import ai_inflight, answer

        cfg = self._cfg("gpt-5-codex")
        with answer._inflight(cfg, [{"role": "user", "content": "やあ"}]):
            assert ai_inflight.running()[0]["model"] == "gpt-5-codex"

    def test_the_reply_names_the_model_that_ran(self, state_env):
        """CLI ブリッジは応答の `model` に実物を載せてくる。"""
        from app import answer

        cfg = self._cfg(answer.PLACEHOLDER_MODEL)
        assert answer.model_of({"model": "claude-opus-5"}, cfg) == "claude-opus-5"

    def test_a_reply_that_echoes_the_placeholder_is_ignored(self, state_env):
        """印をそのまま返してくる相手がいても、印は記録しない。"""
        from app import answer

        cfg = self._cfg(answer.PLACEHOLDER_MODEL)
        assert answer.model_of({"model": answer.PLACEHOLDER_MODEL}, cfg) == ""

    def test_a_silent_reply_falls_back_to_what_we_sent(self, state_env):
        from app import answer

        cfg = self._cfg("gpt-5-codex")
        assert answer.model_of({}, cfg) == "gpt-5-codex"

    def test_the_call_fills_in_what_actually_ran(self, state_env):
        """**`model` は書き換えない** —— agent は同じ `cfg` を使い回すので、
        書き換えると次のターンで選んでもいないモデルを名指しで送ることになる。"""
        from app import answer

        state_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(200, json={
                        "model": "claude-opus-5",
                        "choices": [{"message": {"role": "assistant", "content": "はい"}}],
                    })
                )
            ),
        )
        import asyncio

        cfg = self._cfg(answer.PLACEHOLDER_MODEL)
        asyncio.run(answer.complete_message(cfg, [{"role": "user", "content": "やあ"}]))
        assert cfg.ran_model == "claude-opus-5"
        assert cfg.model == answer.PLACEHOLDER_MODEL


    def test_the_screen_says_default_instead_of_leaving_it_blank(self, state_env):
        """**空欄にしない** —— 空だと「取れなかった」とも「モデルを持たない相手」とも
        読めるが、実際に起きたのは「相手の既定に任せた」。"""
        from app import ai_inflight
        from app.views import ai_history

        ai_inflight.begin(
            backend="codex", model="", effort="", prompt_bytes=1, timeout=900.0
        )
        html = ai_history.section_html()
        assert ai_history.DEFAULT_MODEL_LABEL in html

    def test_the_front_page_says_the_same_thing(self, state_env):
        """玄関と表で同じ依頼が違って見えないこと(`elapsed` と同じ理由)。"""
        from app import ai_inflight
        from app.views import ai_history

        ai_inflight.begin(
            backend="codex", model="", effort="", prompt_bytes=1, timeout=900.0
        )
        assert ai_history.who_html("codex", "") == ai_history.who_html("codex", " ")
        assert ai_history.DEFAULT_MODEL_LABEL in ai_history.who_html("codex", "")
        assert "gpt-5-codex" in ai_history.who_html("codex", "gpt-5-codex")


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

    def test_the_prompt_is_shown_folded(self, state_env):
        """止める判断に中身が要る。ただし**広げたまま並べない** ——
        数千字の依頼が表を埋めて、他に何が走っているか見えなくなる。"""
        from app import ai_inflight
        from app.views import ai_history

        ai_inflight.begin(
            backend="claude", model="", effort="", prompt_bytes=9, timeout=900.0,
            prompt="第1章の目次を作ってください",
        )
        html = ai_history.section_html()
        assert "第1章の目次を作ってください" in html
        assert 'class="prompt-open"' in html

    def test_a_text_job_is_one_row_not_two(self, state_env, monkeypatch):
        """文章の生成は中で会話の口を呼ぶ。畳まないと 1 本の依頼が 2 件に見え、
        枠の残りを見るときに使いすぎを誤って判断する。"""
        from app import ai_inflight, media
        from app.views import ai_history

        monkeypatch.setattr(media, "running_jobs", lambda *a, **k: [
            {"id": "job-1", "kind": "text", "backend": "codex", "model": "",
             "state": "running", "created_at": "2026-09-11T03:03:38+00:00",
             "prompt": "第1章の目次を作って"},
        ])
        with ai_inflight.on_behalf_of("job-1"):
            ai_inflight.begin(
                backend="codex", model="", effort="", prompt_bytes=9, timeout=900.0,
                prompt="第1章の目次を作って",
            )
        rows = ai_history.running_rows()
        assert len(rows) == 1
        assert rows[0]["job_id"] == "job-1"

    def test_a_call_whose_job_is_gone_is_still_shown(self, state_env, monkeypatch):
        """紐は持っているのにジョブが見当たらないぶんは残す ——
        取りこぼすより、余分に出るほうがまだ読める。"""
        from app import ai_inflight, media
        from app.views import ai_history

        monkeypatch.setattr(media, "running_jobs", lambda *a, **k: [])
        with ai_inflight.on_behalf_of("job-消えた"):
            ai_inflight.begin(
                backend="codex", model="", effort="", prompt_bytes=1, timeout=900.0
            )
        assert len(ai_history.running_rows()) == 1
