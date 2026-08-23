"""AI への問い合わせが失敗したときの控え(`/v1/ai/failures`)。

無人の呼び出し(朝の定期実行など)が落ちたとき、呼んだ側のログには
「llm error 502」しか残らない。その場に居合わせないと理由が分からないので、
Chiezo 側に相手・状態・理由を残す。中身は残さない(大きさだけ)。
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


class TestRecord:
    def test_it_keeps_the_reason_and_the_size_but_not_the_prompt(self, state_env):
        """残すのは相手・状態・理由と大きさだけ。プロンプトは残さない。"""
        from app import ai_log

        ai_log.record(
            backend="claude",
            model="fable",
            effort="high",
            status=502,
            reason="claude failed / exit 1",
            prompt_bytes=307383,
        )
        rows = ai_log.recent()
        assert len(rows) == 1
        assert rows[0]["backend"] == "claude"
        assert rows[0]["model"] == "fable"
        assert rows[0]["status"] == 502
        assert rows[0]["reason"] == "claude failed / exit 1"
        assert rows[0]["prompt_bytes"] == 307383
        assert "prompt" not in rows[0]

    def test_it_returns_the_newest_first(self, state_env):
        from app import ai_log

        for i in range(3):
            ai_log.record(backend="claude", model="", effort="", status=502,
                          reason=f"failure {i}", prompt_bytes=i)
        assert [r["reason"] for r in ai_log.recent()] == ["failure 2", "failure 1", "failure 0"]

    def test_it_does_not_grow_without_bound(self, state_env, monkeypatch):
        """読むのは直近だけ。溜め続けると、消すためだけの運用が増える。"""
        from app import ai_log

        monkeypatch.setattr(ai_log, "MAX_ROWS", 5)
        for i in range(12):
            ai_log.record(backend="claude", model="", effort="", status=502,
                          reason=f"failure {i}", prompt_bytes=0)
        rows = ai_log.recent(limit=100)
        assert len(rows) <= 5
        assert rows[0]["reason"] == "failure 11"

    def test_it_is_off_without_a_state_dir(self, monkeypatch, built_data_dir):
        """`CHIEZO_STATE_DIR` が機能フラグを兼ねる(設定・notes と同じ流儀)。"""
        from app import ai_log

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        ai_log.record(backend="claude", model="", effort="", status=502,
                      reason="どこにも残らない", prompt_bytes=0)
        assert ai_log.recent() == []

    def test_a_broken_log_does_not_break_the_call(self, state_env, monkeypatch):
        """控えが取れないことと、AI が答えられないことは別の話。"""
        import sqlite3

        from app import ai_log

        def boom(_path):
            raise sqlite3.OperationalError("disk is full")

        monkeypatch.setattr(ai_log, "_connect", boom)
        ai_log.record(backend="claude", model="", effort="", status=502,
                      reason="書けない", prompt_bytes=0)  # 例外を投げない


class TestEndpoint:
    def test_a_failed_call_shows_up(self, state_env):
        """相手が落ちたら、その理由が後から読める。"""
        from app import answer

        state_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(500, text='{"error": {"message": "overloaded"}}')
                )
            ),
        )
        with make_client(state_env, None) as client:
            res = client.post("/v1/ai/complete", json={
                "backend": "local", "messages": [{"role": "user", "content": "こんにちは"}],
            })
            assert res.status_code == 502
            failures = client.get("/v1/ai/failures").json()["failures"]

        assert len(failures) == 1
        assert failures[0]["status"] == 500
        assert "overloaded" in failures[0]["reason"]
        # 送った文の大きさは残るが、文そのものは残らない
        assert failures[0]["prompt_bytes"] == len("こんにちは".encode())

    def test_it_is_empty_before_anything_fails(self, state_env):
        with make_client(state_env, None) as client:
            assert client.get("/v1/ai/failures").json() == {"failures": []}
