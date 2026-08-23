"""使用量(`/v1/ai/usage`・管理画面の「使用量」節)のテスト。

確かめるのは 2 つの数を混ぜていないこと ——
**相手が言う枠**(残りが分かるが、聞ける相手が限られる)と、
**Chiezo が使ったぶん**(全部の相手で測れるが、残りは分からない)。

相手は立てない。枠を聞きに行く口(`app/usage.py` の `_client`)を差し替えて、
応答の読み方まで通しで見る(`app/answer.py` の `_llm_client` と同じ流儀)。
"""
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from test_agent import make_client


@pytest.fixture()
def env(monkeypatch, built_data_dir, tmp_path):
    from app import answer

    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    answer._MODELS_CACHE.clear()
    return monkeypatch


class ReplyLLM:
    """偽の OpenAI 互換サーバ。`usage` を返すかどうかを切り替えられる。"""

    def __init__(self, usage: dict | None = None):
        self.usage = usage

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "qwen3-8b"}]})
        body = {"choices": [{"message": {"role": "assistant", "content": "はい"}}]}
        if self.usage is not None:
            body["usage"] = self.usage
        return httpx.Response(200, json=body)


def complete(client: TestClient, **body):
    return client.post("/v1/ai/complete", json={"backend": "local", **body})


def backend_of(body: dict, name: str) -> dict:
    return next(b for b in body["backends"] if b["id"] == name)


class TestSpent:
    """Chiezo が使ったぶん —— **全部の相手で同じ物差し**で測れる側。"""

    def test_a_call_is_counted_with_its_tokens(self, env):
        fake = ReplyLLM({"prompt_tokens": 120, "completion_tokens": 30})
        with make_client(env, fake) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            body = client.get("/v1/ai/usage").json()

        spent = backend_of(body, "local")["spent"]["5h"]
        assert spent["requests"] == 1
        assert spent["input_tokens"] == 120
        assert spent["output_tokens"] == 30
        assert spent["unknown_tokens"] == 0

    def test_a_reply_without_usage_counts_the_call_but_not_tokens(self, env):
        """**0 と「言われていない」を分ける。** CLI ブリッジはトークン数を返さない ——
        0 と書くと「0 トークンで動く相手」に見える。"""
        with make_client(env, ReplyLLM()) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            body = client.get("/v1/ai/usage").json()

        spent = backend_of(body, "local")["spent"]["5h"]
        assert (spent["requests"], spent["input_tokens"], spent["unknown_tokens"]) == (1, 0, 1)

    def test_it_says_since_when_it_has_been_counting(self, env):
        """**「0 回」が「使っていない」と読まれないように**、いつからの数かを添える。"""
        with make_client(env, ReplyLLM()) as client:
            assert client.get("/v1/ai/usage").json()["recorded_since"] is None
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            assert client.get("/v1/ai/usage").json()["recorded_since"]

    def test_openai_style_token_names_are_read_too(self, env):
        """相手によっては input/output で名乗る(prompt/completion ではなく)。"""
        fake = ReplyLLM({"input_tokens": 7, "output_tokens": 3})
        with make_client(env, fake) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            body = client.get("/v1/ai/usage").json()

        spent = backend_of(body, "local")["spent"]["5h"]
        assert (spent["input_tokens"], spent["output_tokens"]) == (7, 3)


class TestQuota:
    """相手が言う枠 —— **聞ける相手が限られる**側。"""

    def test_backends_without_a_way_to_ask_say_so(self, env):
        """**空欄にしない。** 「出せない」と「まだ取っていない」は別の状態。"""
        with make_client(env, ReplyLLM()) as client:
            body = client.get("/v1/ai/usage").json()

        assert backend_of(body, "gemini")["quota"]["supported"] is False
        assert backend_of(body, "local")["quota"]["supported"] is False
        assert backend_of(body, "claude")["quota"]["supported"] is True

    def test_it_does_not_ask_anyone_unless_told_to(self, env):
        """**引かれるたびに外へ出ていかない。** 画面もダッシュボードも定期的に引く口。"""
        asked = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            return httpx.Response(200, json={})

        from app import settings_store, usage

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "sk-ant-oat01-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            client.get("/v1/ai/usage")
            assert asked == []
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"})

        assert asked == [usage.ANTHROPIC_USAGE_URL]

    def test_claude_windows_are_read_with_their_names(self, env):
        """`five_hour` は「セッション」、`seven_day` は「今週」。**残りも出す。**"""
        from app import settings_store, usage

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "five_hour": {"utilization": 42.5, "resets_at": "2026-08-23T15:00:00Z"},
                "seven_day": {"utilization": 10, "resets_at": "2026-08-28T00:00:00Z"},
                # 知らない窓は出さない(意味の違う数字に名前を付けないため)
                "some_new_window": {"utilization": 99},
            })

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "sk-ant-oat01-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"}).json()

        windows = backend_of(body, "claude")["quota"]["windows"]
        assert [w["id"] for w in windows] == ["five_hour", "seven_day"]
        assert windows[0]["label"] == "セッション(5 時間)"
        assert (windows[0]["used_percent"], windows[0]["remaining_percent"]) == (42.5, 57.5)

    def test_the_value_is_kept_so_the_screen_does_not_have_to_ask(self, env):
        """一度取った枠は控える(次に開いたときは聞きに行かずに出す)。"""
        from app import settings_store, usage

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"five_hour": {"utilization": 5}})

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "sk-ant-oat01-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"})
            body = client.get("/v1/ai/usage").json()

        quota = backend_of(body, "claude")["quota"]
        assert len(calls) == 1
        assert quota["windows"][0]["used_percent"] == 5.0
        assert quota["fetched_at"]

    def test_a_failure_keeps_the_last_value_and_says_why(self, env):
        """**取れなかったからといって、直前まで見えていた数字を消さない。**"""
        from app import settings_store, usage

        replies = [
            httpx.Response(200, json={"five_hour": {"utilization": 30}}),
            httpx.Response(401, text="expired"),
        ]

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "sk-ant-oat01-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: replies.pop(0))))
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"})
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"}).json()

        quota = backend_of(body, "claude")["quota"]
        assert quota["windows"][0]["used_percent"] == 30.0
        assert "401" in quota["error"]

    def test_a_scope_error_says_what_to_do(self, env):
        """**実測**: `claude setup-token` の長期トークンは推論だけに絞られていて、
        この口(user:profile が要る)では 403 になる。生の英文だけだと「鍵が違う」と
        読んで入れ直すことになるので、そうではないと書く。"""
        from app import settings_store, usage

        body = {"type": "error", "error": {
            "type": "permission_error",
            "message": "OAuth token does not meet scope requirement user:profile"}}

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "sk-ant-oat01-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(403, json=body))))
            out = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"}).json()

        error = backend_of(out, "claude")["quota"]["error"]
        assert "setup-token" in error and "user:profile" in error
        # 相手の言い分も残す(こちらの言い換えだけにしない)
        assert "403" in error

    def test_a_missing_credential_is_the_reason_not_a_crash(self, env):
        from app import usage

        with make_client(env, ReplyLLM()) as client:
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "claude"}).json()

        assert usage  # 相手へは行っていない(鍵が無いので手前で止まる)
        assert backend_of(body, "claude")["quota"]["error"] == "認証情報が未登録です"

    def test_openrouter_reports_credits_without_a_limit_as_such(self, env):
        """上限の無い鍵で「残り 0」と書かない(使い切ったように読める)。"""
        from app import settings_store, usage

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"data": {"usage": 1.5, "limit": None}})

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "openrouter"}).json()

        window = backend_of(body, "openrouter")["quota"]["windows"][0]
        assert window["used"] == 1.5
        assert window["used_percent"] is None
        assert window["remaining_percent"] is None

    def test_an_unknown_backend_is_rejected_with_the_list(self, env):
        with make_client(env, ReplyLLM()) as client:
            res = client.get("/v1/ai/usage", params={"backend": "nope"})

        assert res.status_code == 404
        assert "claude" in res.json()["backends"]


class TestBridgeQuota:
    """CLI ブリッジ越しの枠(Codex / Antigravity)。"""

    def _bridge(self, env, payload: dict, status: int = 200):
        from app import usage

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(str(request.url))
            return httpx.Response(status, json=payload)

        env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))
        return seen

    def test_it_asks_the_bridge_not_the_chat_endpoint(self, env):
        with make_client(env, ReplyLLM()) as client:
            seen = self._bridge(env, {"windows": [
                {"id": "primary", "used_percent": 12, "window_minutes": 300},
            ]})
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        assert seen == ["http://chiezo-bridge-codex:7013/usage"]
        window = backend_of(body, "codex")["quota"]["windows"][0]
        # 相手は名前を持たない(primary としか言わない)ので、窓の長さで呼ぶ
        assert window["label"] == "直近 5 時間"
        assert window["used_percent"] == 12.0

    def test_the_bridge_reason_survives_fastapis_detail_wrapper(self, env):
        """ブリッジの失敗は `detail` に包まれて返る。**中の文言まで出す** ——
        「HTTP 401」だけでは打つ手が分からない。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"detail": {"error": "認証情報が未登録です"}}, status=401)
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        assert backend_of(body, "codex")["quota"]["error"] == "認証情報が未登録です"

    def test_a_backend_that_never_answered_has_no_fetch_time(self, env):
        """**一度も取れていない相手に時刻を入れない**(何かが取れたように読める)。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"windows": [], "reason": "立っていません"})
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        quota = backend_of(body, "codex")["quota"]
        assert quota["fetched_at"] == "" and quota["error"] == "立っていません"

    def test_what_the_cli_said_is_shown_when_no_number_could_be_read(self, env):
        """数字にできなくても、**CLI が何と言ったかは画面に出す**。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"windows": [], "reason": "Please sign in to view credits."})
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "antigravity"}).json()

        assert "sign in" in backend_of(body, "antigravity")["quota"]["error"]


class TestAdminSection:
    def test_the_screen_shows_both_numbers_and_a_refresh_button(self, env):
        with make_client(env, ReplyLLM()) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            html = client.get("/admin").text

        assert 'id="ai-usage"' in html
        assert "相手が言う枠" in html and "Chiezo が使ったぶん" in html
        # 枠を出さない相手は「出せない」と書く(空欄にしない)
        assert "この相手は枠を出さない" in html
        assert 'action="/admin/ai/usage"' in html

    def test_refreshing_a_backend_that_has_no_quota_is_refused(self, env):
        with make_client(env, ReplyLLM()) as client:
            res = client.post("/admin/ai/usage", data={"provider": "gemini"},
                              follow_redirects=False)

        assert res.status_code == 400

    def test_the_reason_comes_back_to_the_screen(self, env):
        from app import settings_store, usage

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "x")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
            res = client.post("/admin/ai/usage", data={"provider": "claude"},
                              follow_redirects=False)

        assert res.status_code == 303
        assert "usage_error" in res.headers["location"]
        assert res.headers["location"].endswith("#ai-usage")


class TestMediaCounts:
    def test_pictures_and_sound_are_counted_too(self, env, tmp_path):
        """**絵と音も同じサブスクの枠を食う**ので、同じ表に残す。"""
        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.record("codex", model="gpt-image-2", kind="image")
            body = client.get("/v1/ai/usage").json()

        assert backend_of(body, "codex")["spent"]["24h"]["requests"] == 1


class TestJst:
    def test_times_are_shown_in_japan_time(self):
        """人が読む行は JST(実行環境の TZ に依らせない)。"""
        from datetime import UTC, datetime

        from app import jst

        assert jst.format(datetime(2026, 8, 23, 15, 12, tzinfo=UTC)) == "2026-08-24 00:12 JST"
        # 時差の無い値は UTC とみなす(ローカル時刻を当てない)
        assert jst.format(datetime(2026, 8, 23, 15, 12)) == "2026-08-24 00:12 JST"
        assert jst.parse("") is None and jst.parse("なんだこれ") is None


class TestBridgeSide:
    """ブリッジ側(CLI の返事の読み方)。CLI は起動しない。"""

    def test_it_reads_percentage_windows(self):
        import cli_bridge

        windows = cli_bridge._windows_in(
            {"rate_limits": {
                "primary": {"used_percent": 23.0, "window_minutes": 300, "resets_at": 1800000000},
                "secondary": {"used_percent": 4.0, "window_minutes": 10080},
            }}
        )

        assert {w["id"] for w in windows} == {"primary", "secondary"}
        assert next(w for w in windows if w["id"] == "primary")["used_percent"] == 23.0

    def test_it_reads_windows_that_only_say_what_is_left(self):
        """残量しか言わない相手のために、使用率はこちらで出す。"""
        import cli_bridge

        windows = cli_bridge._windows_in({"buckets": [{"name": "prompt", "used": 25, "remaining": 75}]})

        assert windows[0]["used_percent"] == 25.0
        assert windows[0]["limit"] == 100.0
        assert windows[0]["label"] == "prompt"

    def test_it_invents_nothing_when_the_shape_is_unknown(self):
        """**推測で数字を作らない**(読めなければ空で返し、生の返事を渡す)。"""
        import cli_bridge

        assert cli_bridge._windows_in({"plan": "pro", "note": "hello"}) == []

    def test_the_endpoint_returns_what_the_cli_printed(self, monkeypatch, tmp_path):
        """`/usage` の口そのもの。**CLI の代わりに python を走らせて**、
        起動 → 出力の読み取り → 窓への変換までを通す(本物の CLI は要らない)。"""
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "antigravity")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        monkeypatch.setattr(server, "ANTIGRAVITY_USAGE_CMD", [
            sys.executable, "-c",
            'print(\'{"buckets": [{"name": "prompt", "used": 10, "remaining": 90}]}\')',
        ])
        with TestClient(server.app) as client:
            body = client.get("/usage").json()

        assert body["cli"] == "antigravity"
        assert body["windows"][0]["used_percent"] == 10.0
        assert body["reason"] == ""

    def test_the_endpoint_hands_back_the_raw_reply_when_it_reads_nothing(
        self, monkeypatch, tmp_path
    ):
        """**数字にできなくても、CLI が何と言ったかは返す**(画面がそれを出す)。"""
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "antigravity")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        monkeypatch.setattr(server, "ANTIGRAVITY_USAGE_CMD", [
            sys.executable, "-c", "print('Please sign in first.')",
        ])
        with TestClient(server.app) as client:
            body = client.get("/usage").json()

        assert body["windows"] == [] and body["reason"] == "Please sign in first."

    def test_a_cli_without_a_way_to_ask_says_404(self, monkeypatch):
        """claude はここに来ない(Chiezo が Anthropic に直に聞く)。"""
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "claude")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        with TestClient(server.app) as client:
            assert client.get("/usage").status_code == 404

    def test_only_the_clis_that_can_answer_have_the_endpoint(self):
        """claude はここに来ない(Chiezo が Anthropic に直に聞く)。"""
        import cli_bridge

        assert set(cli_bridge.USAGE_CLIS) == {"antigravity", "codex"}
