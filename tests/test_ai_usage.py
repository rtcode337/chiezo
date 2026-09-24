"""使用量(`/v1/ai/usage`・管理画面の「使用量」節)のテスト。

確かめるのは 2 つの数を混ぜていないこと ——
相手が言う枠(残りが分かるが、聞ける相手が限られる)と、
Chiezo が使ったぶん(全部の相手で測れるが、残りは分からない)。

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
    answer.forget_choices()
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


def breakdown_of(html: str) -> str:
    """内訳の節だけを切り出す。**同じ画面に依頼履歴も並ぶ**ので、
    ページ全体で照合すると、あちらの行を内訳の行として読んでしまう。
    """
    head = html.index('id="ai-breakdown"')
    return html[head:html.index("</table>", head) + len("</table>")]


class TestSpent:
    """Chiezo が使ったぶん —— 全部の相手で同じ物差しで測れる側。"""

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
        """0 と「言われていない」を分ける。 CLI ブリッジはトークン数を返さない ——
        0 と書くと「0 トークンで動く相手」に見える。"""
        with make_client(env, ReplyLLM()) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            body = client.get("/v1/ai/usage").json()

        spent = backend_of(body, "local")["spent"]["5h"]
        assert (spent["requests"], spent["input_tokens"], spent["unknown_tokens"]) == (1, 0, 1)

    def test_it_says_since_when_it_has_been_counting(self, env):
        """「0 回」が「使っていない」と読まれないように、いつからの数かを添える。"""
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
    """相手が言う枠 —— 聞ける相手が限られる側。"""

    def test_backends_without_a_way_to_ask_say_so(self, env):
        """空欄にしない。 「出せない」と「まだ取っていない」は別の状態。"""
        with make_client(env, ReplyLLM()) as client:
            body = client.get("/v1/ai/usage").json()

        assert backend_of(body, "gemini")["quota"]["supported"] is False
        assert backend_of(body, "local")["quota"]["supported"] is False
        # **CLI を包んだ 3 つはどれも出せる側。** claude は長らく出せない側に
        # 置いてあったが、CLI の版が上がって print モードでもパネルが返るように
        # なった(`app/providers.py` に当時の観測と今の文面を並べてある)。
        assert backend_of(body, "claude")["quota"]["supported"] is True
        assert backend_of(body, "codex")["quota"]["supported"] is True

    def test_it_does_not_ask_anyone_unless_told_to(self, env):
        """引かれるたびに外へ出ていかない。 画面もダッシュボードも定期的に引く口。"""
        asked = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            return httpx.Response(200, json={})

        from app import settings_store, usage

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            client.get("/v1/ai/usage")
            assert asked == []
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "openrouter"})

        assert asked == ["https://openrouter.ai/api/v1/key"]

    def test_a_backend_without_a_way_to_ask_is_not_asked(self, env):
        """枠を出さない相手は、取り直しても外へ出ていかない。

        毎回同じ理由が画面に出るだけで打つ手が無いため。**claude はここには
        入らない** —— CLI の版が上がって出せる側になった。
        """
        from app import settings_store, usage

        asked = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            return httpx.Response(200, json={})

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("claude", "sk-ant-oat01-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            # 枠を出せない相手で確かめる(claude は出せるようになったので、
            # あちらを使うと「取り直した」ことになって検査にならない)
            client.get("/v1/ai/usage", params={"backend": "gemini"}).json()

        assert asked == []

    def test_the_value_is_kept_so_the_screen_does_not_have_to_ask(self, env):
        """一度取った枠は控える(次に開いたときは聞きに行かずに出す)。"""
        from app import settings_store, usage

        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(1)
            return httpx.Response(200, json={"data": {"usage": 5.0, "limit": 20}})

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "openrouter"})
            body = client.get("/v1/ai/usage").json()

        quota = backend_of(body, "openrouter")["quota"]
        assert len(calls) == 1
        assert quota["windows"][0]["used"] == 5.0
        assert quota["fetched_at"]

    def test_a_failure_keeps_the_last_value_and_says_why(self, env):
        """取れなかったからといって、直前まで見えていた数字を消さない。"""
        from app import settings_store, usage

        replies = [
            httpx.Response(200, json={"data": {"usage": 30.0, "limit": 100}}),
            httpx.Response(401, text="expired"),
        ]

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda request: replies.pop(0))))
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "openrouter"})
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "openrouter"}).json()

        quota = backend_of(body, "openrouter")["quota"]
        assert quota["windows"][0]["used"] == 30.0
        assert "401" in quota["error"]

    def test_the_screen_refuses_to_refresh_what_cannot_be_asked(self, env):
        """枠を出さない相手の「取り直す」は受け付けない(押す口も画面に出さない)。"""
        with make_client(env, ReplyLLM()) as client:
            res = client.post("/admin/ai/usage", data={"provider": "gemini"},
                              follow_redirects=False)

        assert res.status_code == 400

    def test_a_missing_credential_is_the_reason_not_a_crash(self, env):
        from app import usage

        with make_client(env, ReplyLLM()) as client:
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "openrouter"}).json()

        assert usage  # 相手へは行っていない(鍵が無いので手前で止まる)
        assert backend_of(body, "openrouter")["quota"]["error"] == "認証情報が未登録です"

    def test_elevenlabs_reports_its_credits(self, env):
        """絵と音だけの相手にも枠を聞ける口はある(鍵だけで引ける)。

        声・効果音・曲・絵・動画が同じ 1 つの残量を食うので、ここが見えないと
        「作れなくなった理由」が画面から分からない。
        """
        from app import settings_store, usage

        asked = {}

        def handler(request: httpx.Request) -> httpx.Response:
            asked["url"] = str(request.url)
            asked["key"] = request.headers.get("xi-api-key")
            return httpx.Response(200, json={
                "tier": "creator", "character_count": 41234, "character_limit": 100000,
                "next_character_count_reset_unix": 1788135754,
            })

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("elevenlabs", "xi-test")
            env.setattr(usage, "_client", lambda headers=None, timeout=None: httpx.AsyncClient(
                headers=headers or {}, transport=httpx.MockTransport(handler)))
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "elevenlabs"}).json()

        assert asked["url"] == "https://api.elevenlabs.io/v1/user/subscription"
        # 鍵はヘッダで送る(URL に載せない)
        assert asked["key"] == "xi-test"
        window = backend_of(body, "elevenlabs")["quota"]["windows"][0]
        assert (window["used"], window["limit"]) == (41234.0, 100000.0)
        assert window["used_percent"] == 41.2
        assert window["label"] == "クレジット(creator)"
        assert window["resets_at"].startswith("2026-08-31")

    def test_elevenlabs_without_the_numbers_says_why(self, env):
        """項目が見当たらないときに 0 と書かない —— 相手の返事をそのまま出す。"""
        from app import settings_store, usage

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("elevenlabs", "xi-test")
            env.setattr(usage, "_client", lambda headers=None, timeout=None: httpx.AsyncClient(
                headers=headers or {},
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"tier": "free"}))))
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "elevenlabs"}).json()

        quota = backend_of(body, "elevenlabs")["quota"]
        assert quota["windows"] == []
        assert "見当たりません" in quota["error"]

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
        """ブリッジの失敗は `detail` に包まれて返る。中の文言まで出す ——
        「HTTP 401」だけでは打つ手が分からない。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"detail": {"error": "認証情報が未登録です"}}, status=401)
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        assert backend_of(body, "codex")["quota"]["error"] == "認証情報が未登録です"

    def test_a_backend_that_never_answered_has_no_fetch_time(self, env):
        """一度も取れていない相手に時刻を入れない(何かが取れたように読める)。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"windows": [], "reason": "立っていません"})
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        quota = backend_of(body, "codex")["quota"]
        assert quota["fetched_at"] == "" and quota["error"] == "立っていません"

    def test_what_the_cli_said_is_shown_when_no_number_could_be_read(self, env):
        """数字にできなくても、CLI が何と言ったかは画面に出す。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"windows": [], "reason": "Please sign in to view credits."})
            body = client.get("/v1/ai/usage",
                              params={"refresh": 1, "backend": "antigravity"}).json()

        assert "sign in" in backend_of(body, "antigravity")["quota"]["error"]


class TestAdminSection:
    def test_the_screen_shows_both_numbers_and_a_refresh_button(self, env):
        with make_client(env, ReplyLLM()) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            html = client.get("/admin/ai").text

        assert 'id="ai-usage"' in html
        assert "相手が言う枠" in html and "Chiezo が使ったぶん" in html
        # 枠を出さない相手は「出せない」と書く(空欄にしない)
        assert "この相手は枠を出さない" in html
        assert 'action="/admin/ai/usage"' in html

    def test_the_quota_cell_holds_together_on_a_phone(self):
        """枠の欄は 1 つの塊で返し、スマホで割る場所に印を付けること。

        スマホでは欄が「見出し | 値」の 2 列の格子になり、中の部品が 1 つずつ升に
        入る —— 包まないと「相手が言ったそのまま」が見出しの側の列へ落ちる。
        """
        from app import usage
        from app.views import ai_usage

        quota = usage.Quota(
            supported=True, fetched_at="2026-09-24T13:00:00+00:00", raw="{}",
            windows=[usage.Window(id="5h", label="5 時間", used_percent=41.5,
                                  resets_at="2026-09-24T16:00:00+00:00")],
        )
        html = ai_usage._quota_cell({"quota": quota})

        assert html.startswith("<div>") and html.endswith("</div>")
        assert "相手が言ったそのまま" in html
        assert 'class="quota-name"' in html and "quota-reset" in html
        # 残りの括弧は途中で割らない
        assert '<span class="nowrap">(残り 58.5%)</span>' in html

    def test_the_spent_cell_marks_where_a_phone_breaks(self):
        """使ったぶんは「/」のあとに印を付け、窓ごとに 1 つの塊にすること。

        スマホではそこで行を割って一段下げる。`<br>` でつなぐと、割ったあとに空行ができる。
        """
        from types import SimpleNamespace

        from app.views import ai_usage

        spent = {"5h": SimpleNamespace(requests=3, input_tokens=12, output_tokens=5, unknown=0)}
        html = ai_usage._spent_cell({"spent": spent})

        assert '3 回 /<span class="spent-detail"> 12 in・5 out</span>' in html
        assert "<br>" not in html

    def test_refreshing_a_backend_that_has_no_quota_is_refused(self, env):
        with make_client(env, ReplyLLM()) as client:
            res = client.post("/admin/ai/usage", data={"provider": "gemini"},
                              follow_redirects=False)

        assert res.status_code == 400

    def test_everything_can_be_refreshed_at_once(self, env):
        """行ごとに押すと相手の数だけ往復することになる。まとめて押せる口を出す。"""
        from app import settings_store, usage

        asked = []

        def handler(request: httpx.Request) -> httpx.Response:
            asked.append(str(request.url))
            return httpx.Response(200, json={"data": {"usage": 1.0, "limit": 10}})

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            for pid in ("openrouter", "codex"):
                settings_store.set_enabled(pid, True)
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(handler)))
            res = client.post("/admin/ai/usage/all", follow_redirects=False)

        assert res.status_code == 303
        # 答えない相手は理由つきで失敗する。押した結果は「取れた数」で返す
        assert "usage_refreshed_all=1" in res.headers["location"]
        assert "usage_error" in res.headers["location"]
        # 「使う」にした相手にだけ聞く(並行。1 つ落ちていても残りは取り直す)
        assert "https://openrouter.ai/api/v1/key" in asked
        assert len(asked) == 2

    def test_backends_that_are_off_are_left_alone(self, env):
        """使わない相手の枠は聞きに行かない —— 呼ばない相手の残りを見ても仕方がなく、
        鍵を外したまま残している相手が毎回「取れませんでした」に数えられる。"""
        from app import settings_store, usage

        asked = []

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            settings_store.set_enabled("openrouter", False)
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(
                    lambda r: asked.append(str(r.url)) or httpx.Response(200, json={}))))
            res = client.post("/admin/ai/usage/all", follow_redirects=False)

        assert asked == []
        assert usage.refreshable() == []
        assert "usage_refreshed_all=0" in res.headers["location"]

    def test_the_button_counts_the_same_backends_it_will_ask(self, env):
        """ボタンに出す数と、実際に聞く相手を同じところから数える。"""
        from app import settings_store, usage
        from app.views import ai_usage

        with make_client(env, ReplyLLM()):
            # 絵と音だけの相手も対象に入る(枠を聞ける口があるため)
            settings_store.set_enabled("elevenlabs", True)
            html = ai_usage.section_html()

        assert usage.refreshable() == ["elevenlabs"]
        assert f"全部取り直す({len(usage.refreshable())} 件)" in html

    def test_the_button_is_hidden_when_there_is_nothing_to_ask(self, env):
        """押しても何も起きないボタンは出さない(壊れているのか設定不足か読めない)。"""
        from app.views import ai_usage

        with make_client(env, ReplyLLM()):
            html = ai_usage.section_html()

        assert "/admin/ai/usage/all" not in html
        assert "まとめて取り直せる相手がいません" in html

    def test_the_reason_comes_back_to_the_screen(self, env):
        from app import settings_store, usage

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_credential("openrouter", "sk-or-test")
            env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))))
            res = client.post("/admin/ai/usage", data={"provider": "openrouter"},
                              follow_redirects=False)

        assert res.status_code == 303
        assert "usage_error" in res.headers["location"]
        assert res.headers["location"].endswith("#ai-usage")


class TestWhereTheButtonSendsYou:
    """戻り先は、**このボタンを出している面だけ**を通す。

    「`/admin` で始まるもの」で通していた頃は、外から来た文字列をそのまま行き先へ
    繋いでいた —— 押した先が別のサイトになる余地を残さない。
    """

    def test_it_goes_back_to_the_page_that_showed_the_button(self, env):
        with make_client(env, ReplyLLM()) as client:
            res = client.post("/admin/ai/usage/all", data={"back": "/admin"},
                              follow_redirects=False)

        assert res.headers["location"].startswith("/admin?")

    def test_anywhere_else_falls_back_to_the_ai_page(self, env):
        from app.views import ai_usage

        outside = [
            "https://example.test/admin",   # 別のサイト
            "//example.test/admin",         # プロトコル相対
            "/admin/../../evil",            # 上へ抜ける
            "/adminose",                    # 前方一致だけは通っていた
            "",
        ]
        with make_client(env, ReplyLLM()) as client:
            for back in outside:
                res = client.post("/admin/ai/usage/all", data={"back": back},
                                  follow_redirects=False)
                assert res.headers["location"].startswith(ai_usage.DEFAULT_BACK), back


class TestMediaCounts:
    def test_pictures_and_sound_are_counted_too(self, env, tmp_path):
        """絵と音も同じサブスクの枠を食うので、同じ表に残す。"""
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
        """推測で数字を作らない(読めなければ空で返し、生の返事を渡す)。"""
        import cli_bridge

        assert cli_bridge._windows_in({"plan": "pro", "note": "hello"}) == []

    def test_the_endpoint_returns_what_the_cli_printed(self, monkeypatch, tmp_path):
        """`/usage` の口そのもの。CLI の代わりに python を走らせて、
        起動 → 出力の読み取り → 窓への変換までを通す(本物の CLI は要らない)。"""
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "antigravity")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        # **手元の CLI へ手を伸ばさせない**（立ち上がりで何が選べるかを聞きに行く）
        server._PROBED = True
        server._CATALOG_WARMED = True
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
        """数字にできなくても、CLI が何と言ったかは返す(画面がそれを出す)。"""
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "antigravity")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        # **手元の CLI へ手を伸ばさせない**（立ち上がりで何が選べるかを聞きに行く）
        server._PROBED = True
        server._CATALOG_WARMED = True
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

        # **枠を出せない CLI はもう無い**(claude も print モードで取れる)ので、
        # 「対応していない CLI」を仮に立てて確かめる
        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "claude")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        # **手元の CLI へ手を伸ばさせない**（立ち上がりで何が選べるかを聞きに行く）
        server._PROBED = True
        server._CATALOG_WARMED = True
        monkeypatch.setattr(server, "USAGE_CLIS", frozenset({"codex"}))
        with TestClient(server.app) as client:
            assert client.get("/usage").status_code == 404

    def test_claude_reports_what_it_used_not_what_is_left(self):
        """**claude は「使った割合」を言う**（Antigravity は「残り」）。
        取り違えると、使い切った枠が「まだ全部残っている」ように出る。"""
        import cli_bridge

        raw = (
            '{"result": "You are currently using your subscription\n\n'
            "Current session: 20% used \u00b7 resets Sep 11, 11pm (Asia/Tokyo)\n"
            'Current week (all models): 80% used \u00b7 resets Sep 13, 3pm (Asia/Tokyo)"}'
        )
        windows = cli_bridge._claude_windows(raw)
        assert [w["used_percent"] for w in windows] == [20.0, 80.0]
        assert windows[0]["label"] == "Current session"
        assert "Sep 11" in windows[0]["resets_at"]

    def test_a_warning_in_front_of_the_json_does_not_break_it(self):
        """**stderr を stdout に混ぜて読んでいる**ので、CLI の警告 1 行で
        `json.loads` が落ちる（実測: `Warning: no stdin data received in 3s`）。"""
        import cli_bridge

        raw = 'Warning: no stdin data received in 3s\n{"result": "Current session: 5% used"}'
        assert [w["used_percent"] for w in cli_bridge._claude_windows(raw)] == [5.0]

    def test_a_plain_report_without_json_still_reads(self):
        import cli_bridge

        assert len(cli_bridge._claude_windows("Current week (Fable): 11% used")) == 1

    def test_a_busy_cli_returns_the_last_value_instead_of_failing(self, monkeypatch):
        """**走っている最中こそ枠を見たい。** 枠を聞くにも CLI を 1 本起こすので
        待ち枠が要り、長い会話の後ろでは取れない —— 断るより、古いと分かる形で
        値を返すほうが役に立つ。"""
        import asyncio
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "codex")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        # **手元の CLI へ手を伸ばさせない**（立ち上がりで何が選べるかを聞きに行く）
        server._PROBED = True
        server._CATALOG_WARMED = True
        monkeypatch.setattr(server, "apply_credential", lambda: "")

        calls = []

        async def fake_read():
            calls.append(1)
            return {"cli": "codex", "windows": [{"id": "w", "label": "週", "used_percent": 42.0}],
                    "reason": "", "taken_at": "2026-09-11T10:00:00+00:00", "stale": False}

        monkeypatch.setattr(server, "_read_usage", fake_read)
        with TestClient(server.app) as client:
            first = client.get("/usage").json()
            assert first["stale"] is False
            assert first["windows"][0]["used_percent"] == 42.0

            # 枠を塞いだまま聞く（別の呼び出しが走っている状態）
            monkeypatch.setattr(server, "LOCK_WAIT", 0.01)
            asyncio.new_event_loop().run_until_complete(server._CLI_LOCK.acquire())
            busy = client.get("/usage").json()

        assert busy["stale"] is True
        assert busy["windows"][0]["used_percent"] == 42.0   # 最後の値
        assert "実行中" in busy["reason"]
        assert len(calls) == 1                              # CLI は 1 回しか起こしていない

    def test_a_busy_cli_without_any_record_still_says_why(self, monkeypatch):
        """控えが無いうちは断る（**嘘の 0% を出さない**）。"""
        import asyncio
        import importlib

        from fastapi.testclient import TestClient

        monkeypatch.setenv("CHIEZO_BRIDGE_CLI", "codex")
        monkeypatch.setenv("CHIEZO_BRIDGE_MCP_URL", "")
        import cli_bridge

        server = importlib.reload(cli_bridge)
        # **手元の CLI へ手を伸ばさせない**（立ち上がりで何が選べるかを聞きに行く）
        server._PROBED = True
        server._CATALOG_WARMED = True
        monkeypatch.setattr(server, "apply_credential", lambda: "")
        monkeypatch.setattr(server, "LOCK_WAIT", 0.01)
        with TestClient(server.app) as client:
            loop = asyncio.new_event_loop()
            loop.run_until_complete(server._CLI_LOCK.acquire())
            assert client.get("/usage").status_code == 503

    def test_every_cli_can_be_asked(self):
        """**3 つとも print モードか app-server で取れる**(実測)。
        claude だけ Chiezo が Anthropic に直に聞いていた頃は、
        預かっているトークンのスコープ不足で 403 になっていた。"""
        import cli_bridge

        assert set(cli_bridge.USAGE_CLIS) == {"antigravity", "claude", "codex"}


class TestBreakdown:
    """内訳 —— 詰まった枠の中身を、相手 × モデル × 考える量 × 依頼元で割る側。"""

    def test_the_same_backend_splits_by_model_effort_and_caller(self, env):
        """相手ごとの合計だけでは、次にどれを止めればよいかが決まらない。"""
        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            for _ in range(7):
                usage_store.record("codex", model="gpt-5.5", effort="high",
                                   caller="collect:painters", prompt_bytes=18_000)
            usage_store.record("codex", model="gpt-5.5", effort="low",
                               caller="api:pta", prompt_bytes=288_000)
            table = breakdown_of(client.get("/admin/ai").text)

        # 依頼元は開いて出す(素の印では読めない)
        assert "収集(painters)" in table and "外のアプリ(pta)" in table
        # 多い順。考える量が違えば別の行になる
        assert table.index("7 回") < table.index("1 回")
        assert "gpt-5.5 / high" in table and "gpt-5.5 / low" in table

    def test_weight_is_shown_next_to_the_count(self, env):
        """回数だけでは、小さい依頼と大きい依頼が同じ 1 回に見える。"""
        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.record("codex", caller="api:pta",
                               prompt_bytes=288_000, reply_bytes=1_024)
            table = breakdown_of(client.get("/admin/ai").text)

        assert "依頼 281 KB → 応答 1 KB" in table

    def test_a_backend_that_never_reports_tokens_says_so(self, env):
        """0 と書くと「0 トークンで動く相手」に見える。"""
        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.record("codex", caller="api:pta")
            table = breakdown_of(client.get("/admin/ai").text)

        assert "トークン数なし" in table

    def test_the_window_can_be_switched(self, env):
        """5 時間では無人で回る層の一周が入らず、7 日では今日の跳ね上がりが均される。"""
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.record("codex", caller="collect:painters")
            usage_store.record("antigravity", caller="collect:tech")
            # 3 日前の呼び出しに仕立てる(記録の口は時刻を受け取らない)
            stale = (datetime.now(UTC) - timedelta(days=3)).isoformat(timespec="seconds")
            with sqlite3.connect(usage_store.db_path()) as conn:
                conn.execute("UPDATE calls SET at = ? WHERE provider = 'antigravity'", (stale,))

            day = breakdown_of(client.get("/admin/ai?spent_window=24h").text)
            week = breakdown_of(client.get("/admin/ai?spent_window=7d").text)

        assert "収集(tech)" not in day
        assert "収集(tech)" in week

    def test_an_unknown_window_falls_back_to_the_default(self, env):
        """窓が違うだけで読めないものは無いので、断らずに既定へ倒す。"""
        with make_client(env, ReplyLLM()) as client:
            html = client.get("/admin/ai?spent_window=../secret").text

        assert "../secret" not in html
        assert 'id="ai-breakdown"' in html


class TestQuotaTrail:
    """枠の推移 —— 控えは「いまどうか」で上書きされるので、聞いた値を積む側。"""

    def _window(self, percent: float) -> list[dict]:
        return [{"id": "primary", "label": "直近 5 時間", "used_percent": percent}]

    def test_asking_for_a_quota_leaves_a_point(self, env):
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()):
            usage_store.save_quota("codex", self._window(12.0))
            trail = usage_store.quota_trail(datetime.now(UTC) - timedelta(hours=1))

        assert [t["window_id"] for t in trail] == ["primary"]
        assert trail[0]["points"][-1]["used_percent"] == 12.0
        assert trail[0]["label"] == "直近 5 時間"

    def test_only_the_climb_counts(self, env):
        """窓は転がって明けるので、下がった差は「使わなかった」ではない。"""
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()):
            for percent in (10.0, 50.0, 5.0, 20.0):
                usage_store.save_quota("codex", self._window(percent))
            trail = usage_store.quota_trail(datetime.now(UTC) - timedelta(hours=1))

        # 10 → 50 で +40、50 → 5 は明けたぶんなので数えず、5 → 20 で +15
        assert trail[0]["climbed"] == 55.0

    def test_a_failure_does_not_leave_a_point(self, env):
        """取れなかった回を積むと、動いていない区間として読まれる。"""
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()):
            usage_store.save_quota("codex", [], "繋がりません")
            trail = usage_store.quota_trail(datetime.now(UTC) - timedelta(hours=1))

        assert trail == []

    def test_the_screen_shows_when_it_moved(self, env):
        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.save_quota("codex", self._window(10.0))
            usage_store.save_quota("codex", self._window(51.0))
            html = client.get("/admin/ai").text

        assert 'id="ai-quota-trail"' in html
        assert "+41 ポイント" in html


class TestQuotaSampling:
    """定時に聞きに行くかの判断 —— 呼んでいない相手に聞きに行かないための部品。"""

    def test_the_turn_is_claimed_only_once(self, env):
        """`--workers 2` なので、同じ周期で両方が起きる。"""
        from datetime import timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()):
            first = usage_store.claim_quota_poll("codex", timedelta(minutes=15))
            second = usage_store.claim_quota_poll("codex", timedelta(minutes=15))

        assert first is True
        assert second is False

    def test_the_turn_comes_back_after_the_interval(self, env):
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()):
            usage_store.claim_quota_poll("codex", timedelta(minutes=15))
            # 前回の番を 1 時間前に仕立てる(時刻は秒までなので、待たずに間隔を作る)
            stale = (datetime.now(UTC) - timedelta(hours=1)).isoformat(timespec="seconds")
            with sqlite3.connect(usage_store.db_path()) as conn:
                conn.execute("UPDATE quota_polls SET at = ?", (stale,))
            again = usage_store.claim_quota_poll("codex", timedelta(minutes=15))

        assert again is True

    def test_calls_are_counted_since_the_last_point(self, env):
        """前の点より後に 1 度も呼んでいなければ、聞いても同じ値が返るだけ。"""
        from app import usage_store

        with make_client(env, ReplyLLM()):
            usage_store.save_quota("codex", [{"id": "primary", "used_percent": 1.0}])
            at = usage_store.last_quota_sample_at("codex")
            quiet = usage_store.calls_since("codex", at)
            usage_store.record("codex", caller="collect:painters")
            busy = usage_store.calls_since("codex", at)

        assert quiet == 0
        assert busy == 1


class TestTokensFromTheBridge:
    """CLI ブリッジが言うトークン数 —— 受け側は形を合わせるだけで記録できる。"""

    def test_the_cache_is_kept_apart_from_the_input(self, env):
        """キャッシュから読んだぶんは入力の内訳。同じトークン数でも枠の減り方が違う。"""
        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.record("codex", model="gpt-5.5", caller="api:pta",
                               input_tokens=14610, output_tokens=7, cached_tokens=11520)
            table = breakdown_of(client.get("/admin/ai").text)

        assert "14,610 in・7 out" in table
        assert "うち 11,520 はキャッシュ" in table

    def test_a_reply_with_tokens_is_recorded(self, env):
        """相手が言えば、こちらは形を合わせるだけで数が入る。"""
        fake = ReplyLLM({"prompt_tokens": 14610, "completion_tokens": 7,
                         "prompt_tokens_details": {"cached_tokens": 11520}})
        with make_client(env, fake) as client:
            complete(client, messages=[{"role": "user", "content": "やあ"}])
            spent = backend_of(client.get("/v1/ai/usage").json(), "local")["spent"]["5h"]

        assert spent["input_tokens"] == 14610
        assert spent["output_tokens"] == 7
        assert spent["unknown_tokens"] == 0


class TestWhatMovedTheQuota:
    """枠が動いた区間に何が走っていたか —— 止める相手を決めるのに要る。"""

    def test_the_interval_lists_what_ran(self, env):
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            window = {"id": "primary", "label": "直近 5 時間"}
            usage_store.save_quota("codex", [dict(window, used_percent=10.0)])
            # 1 点目より後、2 点目より前に走ったことにする
            usage_store.record("codex", model="gpt-5.5", caller="collect:painters")
            older = (datetime.now(UTC) - timedelta(minutes=5)).isoformat(timespec="seconds")
            with sqlite3.connect(usage_store.db_path()) as conn:
                conn.execute("UPDATE quota_samples SET at = ?", (older,))
            usage_store.save_quota("codex", [dict(window, used_percent=51.0)])
            html = client.get("/admin/ai").text

        assert "収集(painters) gpt-5.5 ×1" in html

    def test_the_boundary_is_not_counted_twice(self, env):
        """観測のちょうどその秒に入ったぶんが、隣り合う 2 つの区間に出てはいけない。"""
        from app import usage_store

        with make_client(env, ReplyLLM()):
            usage_store.record("codex", caller="api:pta")
            at = usage_store.last_quota_sample_at("codex") or ""
            usage_store.save_quota("codex", [{"id": "primary", "used_percent": 1.0}])
            at = usage_store.last_quota_sample_at("codex")
            # 始まりを含めない(その時刻までのぶんは前の区間に数えた)
            assert usage_store.calls_between("codex", at, at) == []


class TestWindowOrder:
    """窓の並びと名前 —— 相手ごとに返す順が違い、名前もぶつかりうる。"""

    def _window(self, name: str, minutes: float | None) -> "object":
        from app.usage import Window

        return Window(id=name, label=name, used_percent=1.0, window_minutes=minutes)

    def test_the_short_window_comes_first(self, env):
        """実測で codex は 5 時間が先、antigravity は週が先だった。"""
        from app import usage

        weekly = self._window("週", 7 * 24 * 60)
        session = self._window("5 時間", 5 * 60)

        assert [w.label for w in usage.arranged([weekly, session])] == ["5 時間", "週"]

    def test_windows_without_a_length_keep_their_place_at_the_end(self, env):
        """claude は文面から読むので長さを持たない。元の並びのまま末尾へ。"""
        from app import usage

        first = self._window("session", None)
        second = self._window("week", None)
        short = self._window("5 時間", 5 * 60)

        assert [w.label for w in usage.arranged([first, second, short])] == [
            "5 時間", "session", "week",
        ]


class TestClaudeDoesNotNeedARegisteredToken:
    """登録が無くても有効にできる。**required にすると詰む** —— 枠を取るには
    登録を空にする必要があるのに、空にすると二度と有効にできなくなる。
    """

    def test_a_registered_token_is_optional(self, env):
        from app import providers

        spec = providers.get("claude")

        assert spec.credential == providers.CRED_OPTIONAL

    def test_it_can_be_enabled_without_one(self, env):
        """止めているのは「接続を試す」が通っていないことだけであってほしい。"""
        from app import settings_store
        from app.views import ai_settings

        with make_client(env, ReplyLLM()):
            settings_store.set_verified("claude", True)
            row = next(r for r in ai_settings._rows() if r["spec"].id == "claude")

        assert row["has_credential"] is False
        assert row["can_enable"] is True

    def test_windows_with_the_same_name_are_told_apart(self, env):
        """名前は長さから作るので、同じ長さの窓が 2 つあると見分けが付かない
        —— 実測で codex は 7 日の窓を 2 つ返す(別勘定で、値もまったく違う)。
        """
        from app import usage
        from app.usage import Window

        pair = [
            Window(id="secondary", label="直近 7 日", used_percent=33.0,
                   window_minutes=7 * 24 * 60),
            Window(id="primary-10080m", label="直近 7 日", used_percent=0.0,
                   window_minutes=7 * 24 * 60),
        ]

        assert [w.label for w in usage.arranged(pair)] == [
            "直近 7 日(secondary)", "直近 7 日(primary-10080m)",
        ]

    def test_a_name_that_does_not_clash_is_left_alone(self, env):
        from app import usage
        from app.usage import Window

        one = [Window(id="primary", label="直近 5 時間", used_percent=1.0,
                      window_minutes=300)]

        assert [w.label for w in usage.arranged(one)] == ["直近 5 時間"]


class TestAWindowThatStoppedComing:
    """相手が返さなくなった窓は下へ回す。**消さずに、伸びていないことを示す。**"""

    def test_it_sinks_below_the_live_ones(self, env):
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()):
            # 混ざって大きく上がった古い線(もう伸びない)
            usage_store.save_quota("codex", [{"id": "primary", "label": "直近 7 日",
                                              "used_percent": 5.0}])
            usage_store.save_quota("codex", [{"id": "primary", "label": "直近 7 日",
                                              "used_percent": 90.0}])
            old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
            with sqlite3.connect(usage_store.db_path()) as conn:
                conn.execute("UPDATE quota_samples SET at = ?", (old,))
            # いま伸びている線
            usage_store.save_quota("codex", [{"id": "primary-300m", "label": "直近 5 時間",
                                              "used_percent": 3.0}])
            trails = usage_store.quota_trail(datetime.now(UTC) - timedelta(days=1))

        assert [t["window_id"] for t in trails] == ["primary-300m", "primary"]
        assert trails[0]["stale"] is False
        assert trails[1]["stale"] is True

    def test_the_screen_says_it_is_no_longer_reported(self, env):
        import sqlite3
        from datetime import UTC, datetime, timedelta

        from app import usage_store

        with make_client(env, ReplyLLM()) as client:
            usage_store.save_quota("codex", [{"id": "primary", "label": "直近 7 日",
                                              "used_percent": 90.0}])
            old = (datetime.now(UTC) - timedelta(hours=2)).isoformat(timespec="seconds")
            with sqlite3.connect(usage_store.db_path()) as conn:
                conn.execute("UPDATE quota_samples SET at = ?", (old,))
            usage_store.save_quota("codex", [{"id": "primary-300m", "label": "直近 5 時間",
                                              "used_percent": 3.0}])
            html = client.get("/admin/ai").text

        assert "いまは返ってこない窓" in html


class TestWhatTheBackendActuallySaid:
    """相手が言ったそのまま(`Quota.raw`)。

    **正規化した後の画面だけでは答えられないことがある。** 出ているのは Chiezo が
    付けた名前と割合で、窓の名前すら相手のものではない —— 相手が同じ名前の窓を
    2 つ返したときは、長さを添えて呼び分けている。
    """

    def _bridge(self, env, payload: dict, status: int = 200):
        from app import usage

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json=payload)

        env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))

    def test_it_is_kept_even_when_the_windows_were_read_fine(self, env):
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {
                "windows": [{"id": "primary", "used_percent": 12, "window_minutes": 300}],
                "raw": '{"rateLimits":{"primary":{"usedPercent":12}}}',
            })
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        assert backend_of(body, "codex")["quota"]["raw"] == \
            '{"rateLimits":{"primary":{"usedPercent":12}}}'

    def test_the_screen_folds_it_away(self, env):
        """読みに来た人だけが開く —— 常に開いていると、他の相手の行が画面外へ出る。"""
        from app import usage
        from app.views import ai_usage

        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {
                "windows": [{"id": "primary", "used_percent": 12, "window_minutes": 300}],
                "raw": '{"limitId":"weekly-pool"}',
            })
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"})
            html = client.get("/admin/ai").text

        assert "相手が言ったそのまま" in html
        assert "weekly-pool" in html
        assert '<details class="raw-quota">' in html
        assert "<details class=\"raw-quota\" open>" not in html
        assert ai_usage._raw_html(usage.Quota(supported=True)) == "", "無ければ何も出さない"

    def test_it_survives_a_failure(self, env):
        """一時的に繋がらないだけのことがある。直前まで見えていたものを消さない。"""
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {
                "windows": [{"id": "primary", "used_percent": 12, "window_minutes": 300}],
                "raw": "前に取れたもの",
            })
            client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"})

            self._bridge(env, {"detail": {"error": "つながりません"}}, status=502)
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        quota = backend_of(body, "codex")["quota"]
        assert quota["error"] == "つながりません"
        assert quota["raw"] == "前に取れたもの"

    def test_a_backend_that_says_nothing_raw_is_quiet(self, env):
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {
                "windows": [{"id": "primary", "used_percent": 12, "window_minutes": 300}],
            })
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        assert backend_of(body, "codex")["quota"]["raw"] == ""


class TestNamingTheWindowsApart:
    """枠が複数あると、窓の長さもぶつかる。

    実測で、codex は 7 日の窓を 2 つ返し、どちらも `primary` / `secondary` としか
    名乗らなかった。枠のほうには名前が付いている(`limitName`)ので、そこまで
    出せば読み分けられる。
    """

    def _bridge(self, env, payload: dict):
        from app import usage

        def handler(_request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=payload)

        env.setattr(usage, "_client", lambda *a, **k: httpx.AsyncClient(
            transport=httpx.MockTransport(handler)))

    def test_the_group_name_is_added_to_the_label(self, env):
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"windows": [
                {"id": "primary", "used_percent": 0, "window_minutes": 10080,
                 "group": "gpt-reserve"},
            ]})
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        [window] = backend_of(body, "codex")["quota"]["windows"]
        assert window["label"] == "直近 7 日(gpt-reserve)"

    def test_a_window_without_one_is_named_by_its_length(self, env):
        with make_client(env, ReplyLLM()) as client:
            self._bridge(env, {"windows": [
                {"id": "primary", "used_percent": 12, "window_minutes": 300},
            ]})
            body = client.get("/v1/ai/usage", params={"refresh": 1, "backend": "codex"}).json()

        [window] = backend_of(body, "codex")["quota"]["windows"]
        assert window["label"] == "直近 5 時間"


class TestWhileTheCliIsRunning:
    """**CLI が動いている相手の枠は聞けない。**

    枠を聞くのはその相手の CLI を**もう 1 本起こす**動作で、ブリッジは 1 本ずつしか
    動かさない。走っている最中に聞くと空くまで待たされて時間切れになるので、
    押せなくして、まとめて取り直すときも飛ばす。
    """

    def _running(self, backend: str) -> None:
        from app import ai_inflight

        assert ai_inflight.begin(
            backend=backend, model="m", effort="", prompt_bytes=10, timeout=900
        ) is not None

    def test_the_backend_is_named_as_busy(self, env):
        from app import usage

        with make_client(env, ReplyLLM()):
            self._running("codex")

            assert usage.busy_now() == {"codex"}

    def test_a_backend_asked_over_http_is_not_busy(self, env):
        """直に叩く相手は CLI を起こさないので、会話中でも枠は聞ける。"""
        from app import usage

        with make_client(env, ReplyLLM()):
            self._running("openrouter")

            assert usage.busy_now() == set()

    def test_the_row_button_is_disabled(self, env):
        from app import settings_store
        from app.views import ai_usage

        with make_client(env, ReplyLLM()):
            settings_store.set_enabled("codex", True)
            self._running("codex")
            html = ai_usage.section_html()

        assert "CLI 実行中" in html
        assert "disabled" in html

    def test_asking_for_all_skips_it(self, env):
        """飛ばしたことは画面に出す —— 黙って減らすと数が合わない。"""
        from app import settings_store

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_enabled("codex", True)
            self._running("codex")
            res = client.post("/admin/ai/usage/all", follow_redirects=False)

        assert "usage_skipped" in res.headers["location"]

    def test_asking_for_it_alone_is_refused(self, env):
        """描いたあとに走り出すことがあるので、口の側でも断る。"""
        from app import settings_store

        with make_client(env, ReplyLLM()) as client:
            settings_store.set_enabled("codex", True)
            self._running("codex")
            res = client.post(
                "/admin/ai/usage", data={"provider": "codex"}, follow_redirects=False
            )

        assert res.status_code == 303
        assert "usage_skipped" in res.headers["location"]
        assert "usage_refreshed=" not in res.headers["location"]


class TestTheButtonSaysItIsWorking:
    """**押したら「取り直しています…」に変わる。**

    CLI に聞く相手は往復に数十秒かかることがあり、何も変わらないと押せたのか
    壊れているのか分からず、もう一度押される —— 2 本目は枠を食うだけで、
    相手によっては失敗する。
    """

    def test_the_button_carries_the_label_it_will_show(self, env):
        from app import settings_store
        from app.views import ai_usage

        with make_client(env, ReplyLLM()):
            settings_store.set_enabled("codex", True)
            html = ai_usage.section_html()

        assert 'data-busy="取り直しています…"' in html

    def test_the_script_travels_with_the_button(self):
        """印の付いたボタンがある画面にだけ差し込む(セレクトの台本と同じ)。"""
        from app import pages

        with_button = pages.page_shell("t", '<button data-busy="…">押す</button>')
        without = pages.page_shell("t", "<p>なにも無い</p>")

        assert pages.BUSY_FORM_SCRIPT in with_button
        assert pages.BUSY_FORM_SCRIPT not in without

    def test_it_disables_after_the_form_was_sent(self):
        """押した瞬間に無効にすると、そのボタンの name/value が送られない。"""
        from app import pages

        assert "setTimeout" in pages.BUSY_FORM_SCRIPT
        assert "addEventListener('submit'" in pages.BUSY_FORM_SCRIPT
