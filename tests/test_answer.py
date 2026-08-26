"""「使う」層(/v1/ask・/ai/chat)のテスト。

推論サーバは立てず、`answer._llm_client` を `httpx.MockTransport` 入りのクライアントに
差し替えて偽の OpenAI 互換サーバを演じさせる。こうするとクエリ生成 → 検索 → 回答の
全経路を、実データもネットワークも無しで通せる。
"""
import asyncio
import json

import httpx
import pytest
from conftest import CHAT_PATH
from fastapi.testclient import TestClient


@pytest.fixture()
def monkeypatch_env(monkeypatch, built_data_dir, tmp_path):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    return monkeypatch


@pytest.fixture()
def disabled_client(monkeypatch_env):
    """相手を 1 つも有効にしていない = 使う層が無効な状態。"""
    monkeypatch_env.delenv("CHIEZO_LLM_URL", raising=False)
    from app.main import app

    with TestClient(app) as c:
        yield c


class FakeLLM:
    """偽の OpenAI 互換サーバ。呼ばれた順に応答を返し、リクエストを記録する。"""

    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.requests: list[dict] = []

    def _next(self) -> str:
        return self.replies.pop(0) if self.replies else ""

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        self.requests.append(payload)
        content = self._next()
        if not payload.get("stream"):
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": content}}]},
            )
        # ストリーミングは 1 文字ずつの SSE フレームにして返す
        frames = "".join(
            "data: " + json.dumps({"choices": [{"delta": {"content": ch}}]}) + "\n\n"
            for ch in content
        )
        return httpx.Response(
            200, text=frames + "data: [DONE]\n\n",
            headers={"Content-Type": "text/event-stream"},
        )

    @property
    def calls(self) -> int:
        return len(self.requests)


def make_client(monkeypatch, fake: FakeLLM | None, **env) -> TestClient:
    from app import answer, settings_store
    from app.main import app

    # 相手の on/off は設定ストアに入る。URL は `local` の逃げ道で偽の相手へ向ける。
    monkeypatch.setenv("CHIEZO_LLM_URL", env.pop("url", "http://llm.test:8080/v1"))
    settings_store.set_verified("local", True)
    settings_store.set_enabled("local", True)
    for key, value in env.items():
        monkeypatch.setenv(key, str(value))
    if fake is not None:
        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
        )
    return TestClient(app)


PLAN_OK = json.dumps({"queries": [{"source": "jawiki", "q": "浅草寺"}]}, ensure_ascii=False)
ANSWER_OK = "浅草寺は東京都台東区にある寺院です [1]。"


class TestDisabled:
    def test_ask_returns_503_when_nothing_is_enabled(self, disabled_client):
        res = disabled_client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 503
        assert res.json()["error"] == "answering is disabled"
        # 次にすることが分かるよう、有効にする場所を案内する
        assert "/admin" in res.json()["hint"]

    def test_ask_page_explains_how_to_enable(self, disabled_client):
        res = disabled_client.get(CHAT_PATH)
        assert res.status_code == 200
        assert "CHIEZO_LLM_URL" in res.text
        # 無効でもフォーム自体は出す(何のページか分かるように)
        assert 'name="q"' in res.text

    def test_admin_shows_the_feature_as_disabled(self, disabled_client):
        res = disabled_client.get("/admin")
        assert res.status_code == 200
        assert "まだ話せる相手がいません" in res.text
        # 相手を増やす入口(「話す相手」節)も同じページに出ていること
        assert "話す相手" in res.text


class TestAskJson:
    def test_plans_searches_then_answers(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこにある?"})
        assert res.status_code == 200
        body = res.json()
        assert body["answer"] == ANSWER_OK
        assert body["queries"] == [{"source": "jawiki", "q": "浅草寺"}]
        assert fake.calls == 2  # クエリ生成 + 回答
        # 出典はフィクスチャの実在文書を指す
        (ref,) = [r for r in body["references"] if r["title"] == "浅草寺"]
        assert ref["source"] == "jawiki"
        assert ref["url"] == f"/search/jawiki/doc/{ref['doc_id']}"
        assert ref["n"] == 1

    def test_snippets_are_handed_to_the_model(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            client.get("/v1/ask", params={"q": "浅草寺はどこにある?"})
        prompt = fake.requests[1]["messages"][-1]["content"]
        assert "[1] jawiki / 浅草寺" in prompt
        assert "浅草寺はどこにある?" in prompt

    def test_explicit_source_narrows_the_catalog_but_still_plans(self, monkeypatch_env):
        """source 指定は選べるソースを絞るだけ。質問文 → 検索語の変換は依然として要る。"""
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get(
                "/v1/ask", params={"q": "浅草寺はどこにある?", "source": "jawiki"}
            )
        assert res.status_code == 200
        assert fake.calls == 2
        assert res.json()["queries"] == [{"source": "jawiki", "q": "浅草寺"}]
        # クエリ生成のプロンプトには絞ったソースだけが載る
        assert "利用できるソース:\n- jawiki" in fake.requests[0]["messages"][-1]["content"]

    def test_unknown_source_is_404(self, monkeypatch_env):
        fake = FakeLLM(ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "x", "source": "nosuch"})
        assert res.status_code == 404
        assert res.json()["sources"] == ["jawiki"]

    def test_broken_planning_json_still_reaches_an_answer(self, monkeypatch_env):
        """小型モデルが JSON を返せなくても、劣化した検索で回答まで到達する。"""
        fake = FakeLLM("すみません、よく分かりません", ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺 の 歴史"})
        assert res.status_code == 200
        assert res.json()["answer"] == ANSWER_OK
        # 質問文からいちばん長い断片を拾って jawiki へ投げている
        assert res.json()["queries"] == [{"source": "jawiki", "q": "浅草寺"}]

    def test_planning_json_wrapped_in_a_code_fence_is_still_parsed(self, monkeypatch_env):
        fake = FakeLLM(f"```json\n{PLAN_OK}\n```", ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.json()["queries"] == [{"source": "jawiki", "q": "浅草寺"}]

    def test_context_stays_within_the_budget(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake, CHIEZO_ANSWER_MAX_CHARS=200) as client:
            client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        prompt = fake.requests[1]["messages"][-1]["content"]
        # 抜粋本文の合計。見出し行・質問文のぶんは上限の外なので余裕を見て比べる
        assert len(prompt) < 200 + 400

    def test_empty_numeric_env_falls_back_to_defaults(self, monkeypatch_env):
        """compose は未設定の変数を `VAR=` で渡すので、空文字で 500 にならないこと。"""
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(
            monkeypatch_env, fake,
            CHIEZO_ANSWER_DOCS="", CHIEZO_ANSWER_MAX_CHARS="", CHIEZO_ANSWER_TIMEOUT="",
        ) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 200


class TestGrounding:
    """回答方針の切り替え。「抜粋だけ」は Chiezo の思想ではなくモデルの幻覚対策。"""

    def test_grounded_is_the_default(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.json()["grounded"] is True
        assert "抜粋に書かれていないことは答えない" in fake.requests[1]["messages"][0]["content"]

    def test_open_mode_lets_the_model_fill_gaps(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?", "grounded": 0})
        assert res.json()["grounded"] is False
        system = fake.requests[1]["messages"][0]["content"]
        assert "自分の知識で補ってよい" in system
        assert "抜粋に書かれていないことは答えない" not in system

    def test_grounded_without_any_snippet_never_calls_the_model(self, monkeypatch_env):
        """実測で 1B は「抜粋が空でも自分の知識で答える」。経路として断つ。"""
        no_hit = json.dumps(
            {"queries": [{"source": "jawiki", "q": "存在しない語句ですよこれは"}]},
            ensure_ascii=False,
        )
        fake = FakeLLM(no_hit, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "存在しない語句ですよこれは"})
        body = res.json()
        assert body["references"] == []
        assert "抜粋からは分かりません" in body["answer"]
        assert fake.calls == 1  # クエリ生成のみ。回答の推論は走らせない

    def test_open_mode_still_answers_without_snippets(self, monkeypatch_env):
        no_hit = json.dumps(
            {"queries": [{"source": "jawiki", "q": "存在しない語句ですよこれは"}]},
            ensure_ascii=False,
        )
        fake = FakeLLM(no_hit, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get(
                "/v1/ask", params={"q": "存在しない語句ですよこれは", "grounded": 0}
            )
        assert res.json()["answer"] == ANSWER_OK
        assert fake.calls == 2
        # 根拠 0 件なので、番号を付けるなと明示して渡す(1B は放っておくと [1] を捏造する)
        assert "出典番号は絶対に付けないこと" in fake.requests[1]["messages"][-1]["content"]

    def test_streaming_reports_the_no_basis_answer_too(self, monkeypatch_env):
        no_hit = json.dumps(
            {"queries": [{"source": "jawiki", "q": "存在しない語句ですよこれは"}]},
            ensure_ascii=False,
        )
        fake = FakeLLM(no_hit, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get(
                "/v1/ask", params={"q": "存在しない語句ですよこれは", "stream": 1}
            )
        assert "抜粋からは分かりません" in res.text
        assert fake.calls == 1

    def test_page_offers_both_policies(self, monkeypatch_env):
        with make_client(monkeypatch_env, FakeLLM()) as client:
            res = client.get(CHAT_PATH)
        assert 'name="grounded"' in res.text
        assert "モデルの知識で補ってよい" in res.text


class TestAskStreaming:
    def test_sse_emits_references_then_deltas_then_done(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, "東京都です")
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?", "stream": 1})
        assert res.status_code == 200
        assert res.headers["content-type"].startswith("text/event-stream")
        events = [
            line[len("event: "):]
            for line in res.text.splitlines() if line.startswith("event: ")
        ]
        assert events[0] == "references"
        assert events[-1] == "done"
        assert events.count("delta") == len("東京都です")
        # 差分をつなぐと本文になる
        deltas = [
            json.loads(line[len("data: "):])["text"]
            for line in res.text.splitlines()
            if line.startswith("data: ") and "text" in line
        ]
        assert "".join(deltas) == "東京都です"

    def test_failures_before_streaming_are_real_http_errors(self, monkeypatch_env):
        """流し始めた後はステータスを変えられないので、失敗しうる段は先に済ませる。"""
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get("/v1/ask", params={"q": "x", "source": "nosuch", "stream": 1})
        assert res.status_code == 404


class TestUpstreamFailures:
    def test_unreachable_llm_is_502(self, monkeypatch_env):
        from app import answer

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        )
        with make_client(monkeypatch_env, None) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 502
        assert res.json()["error"] == "llm unreachable"
        assert res.json()["reason"] == "ConnectError"  # 種別だけ返す(文言は返さない)

    def test_timeout_is_504(self, monkeypatch_env):
        from app import answer

        def stall(request):
            raise httpx.ReadTimeout("too slow", request=request)

        monkeypatch_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(stall)),
        )
        with make_client(monkeypatch_env, None) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 504
        assert "llm timeout" in res.json()["error"]

    def test_upstream_error_status_is_502(self, monkeypatch_env):
        from app import answer

        monkeypatch_env.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(500, text="boom"))
            ),
        )
        with make_client(monkeypatch_env, None) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 502
        assert res.json()["error"] == "llm error 500"
        # 相手の応答本文は返さない(ログにだけ残す)。認証の無い画面から
        # 内部の構成が読めてしまうため
        assert "boom" not in res.text


class TestAskPage:
    def test_form_only_without_a_question(self, monkeypatch_env):
        with make_client(monkeypatch_env, FakeLLM()) as client:
            res = client.get(CHAT_PATH)
        assert res.status_code == 200
        assert 'name="q"' in res.text
        assert '<option value="jawiki"' in res.text
        assert "data-first" not in res.text  # 質問が無ければ何も送らない

    def test_question_is_handed_to_the_chat_js(self, monkeypatch_env):
        """既定はサーバ側で推論を回さない(JS が /v1/chat から埋める)。二重に走らせないため。"""
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get(CHAT_PATH, params={"q": "浅草寺はどこ?"})
        assert res.status_code == 200
        assert 'data-first="浅草寺はどこ?"' in res.text  # JS が最初の発言として送る
        assert "fetch('/v1/chat?stream=1'" in res.text
        assert "nojs=1" in res.text  # JS が無い環境向けの導線
        assert fake.calls == 0

    def test_nojs_renders_the_answer_server_side(self, monkeypatch_env):
        fake = FakeLLM(PLAN_OK, ANSWER_OK)
        with make_client(monkeypatch_env, fake) as client:
            res = client.get(CHAT_PATH, params={"q": "浅草寺はどこ?", "nojs": 1})
        assert res.status_code == 200
        assert ANSWER_OK in res.text
        assert "/search/jawiki/doc/" in res.text
        assert fake.calls == 2

    def test_markdown_renderer_is_served_with_the_page(self, monkeypatch_env):
        """返事の Markdown は画面で組む。外部のライブラリは読まない(LAN 内・オフライン前提)。"""
        with make_client(monkeypatch_env, FakeLLM()) as client:
            res = client.get(CHAT_PATH)
        assert "window.chiezoMarkdown" in res.text   # 描画器そのもの
        assert "show(t.text, answer)" in res.text    # 本文をそれで描いている
        assert "cdn." not in res.text                # 外から読み込まない

    def test_admin_lists_every_provider_in_one_table(self, monkeypatch_env):
        """相手ごとに 1 行。 話す相手と絵・音の相手を分けていた頃は、同じ相手が
        2 か所に出ていて、どちらの on/off が効くのか画面から読めなかった。"""
        with make_client(monkeypatch_env, FakeLLM()) as client:
            res = client.get("/admin")

        assert "AI の相手" in res.text
        assert "できること" in res.text
        # 話せない相手（自前の GPU・ElevenLabs）も同じ表に並ぶ
        assert "ComfyUI" in res.text
        assert "ElevenLabs" in res.text
        # 節は 1 つだけ（古い見出しが残っていない）
        assert "絵と音を作る相手" not in res.text

    def test_admin_greys_out_the_providers_that_are_off(self, monkeypatch_env):
        """いまどちらの状態かを、ボタンの文字だけに頼らせない。"""
        with make_client(monkeypatch_env, FakeLLM()) as client:
            res = client.get("/admin")

        assert 'class="off"' in res.text
        # 有効にするボタンの文言は「無効にする」と同じ長さに揃える（並ぶと目立つため）
        assert "有効にする" in res.text
        assert "話せるようにする" not in res.text

    def test_admin_links_to_the_chat_page_when_enabled(self, monkeypatch_env):
        with make_client(monkeypatch_env, FakeLLM()) as client:
            res = client.get("/admin")
        assert f'href="{CHAT_PATH}"' in res.text


class TestSettings:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("http://llm:8080", "http://llm:8080/v1"),
            ("http://llm:8080/", "http://llm:8080/v1"),
            ("http://llm:8080/v1", "http://llm:8080/v1"),
            ("http://llm:8080/v1/", "http://llm:8080/v1"),
            # パスを持つ相手には足さない。Gemini の OpenAI 互換の口はこの形で、
            # 直下が chat/completions なので `/v1` を挟むと 404 になる。
            (
                "https://generativelanguage.googleapis.com/v1beta/openai",
                "https://generativelanguage.googleapis.com/v1beta/openai",
            ),
            (
                "https://generativelanguage.googleapis.com/v1beta/openai/",
                "https://generativelanguage.googleapis.com/v1beta/openai",
            ),
            ("https://openrouter.ai/api/v1", "https://openrouter.ai/api/v1"),
        ],
    )
    def test_base_url_is_normalized(self, raw, expected):
        from app import answer

        assert answer._normalize_base_url(raw) == expected

    def test_disabled_when_nothing_is_enabled(self, monkeypatch, tmp_path):
        from app import answer

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        assert answer.load_settings("local") is None
        assert answer.is_enabled() is False


class TestBackends:
    """話せる相手（`app/providers.py` の決め打ち + 管理画面の設定）。

    URL と表示名はコードに固定してあり、ユーザーが決めるのは on/off・API キー・モデルだけ。
    `CHIEZO_LLM_URL` で指す相手だけは別扱いで、環境変数から URL を取る
    （LAN の別マシンの推論サーバを指す用途があり、IP は環境ごとに違うため）。
    """

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, tmp_path):
        import os

        from app import answer

        for key in list(os.environ):
            if key.startswith("CHIEZO_LLM_"):
                monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        # 相手に聞いたモデル一覧の控えは、テスト間で持ち越さない
        answer._MODELS_CACHE.clear()
        answer._MODEL_LABEL_CACHE.clear()

    def test_local_is_just_another_provider(self):
        """同居の推論サーバも他と同じ扱い（管理画面で on にして初めて使える）。"""
        from app import answer, settings_store

        assert answer.backend_names() == []
        settings_store.set_verified("local", True)
        settings_store.set_enabled("local", True)
        assert answer.backend_names() == ["local"]
        # URL はコードの決め打ち（compose 同梱の chiezo-llm）
        assert answer.load_settings("local").url == "http://chiezo-llm:7011/v1"

    def test_local_url_can_be_pointed_elsewhere(self, monkeypatch):
        """LAN の別マシンで動かしている場合の逃げ道（IP は環境ごとに違い決め打ちできない）。"""
        from app import answer, settings_store

        settings_store.set_verified("local", True)
        settings_store.set_enabled("local", True)
        monkeypatch.setenv("CHIEZO_LLM_URL", "http://192.0.2.10:11434")
        assert answer.load_settings("local").url == "http://192.0.2.10:11434/v1"

    def test_only_local_takes_a_url_override(self, monkeypatch):
        """逃げ道を持つのは local だけ（他は URL が 1 つに決まる）。"""
        from app import providers

        monkeypatch.setenv("CHIEZO_LLM_URL", "http://192.0.2.10:11434")
        assert providers.url_of(providers.get("gemini")) == providers.get("gemini").url

    def test_provider_appears_only_after_it_is_enabled(self):
        from app import answer, settings_store

        assert answer.backend_names() == []
        settings_store.set_credential("gemini", "k")
        # 認証情報を入れただけでは出てこない（有効化は別操作）
        assert answer.backend_names() == []
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        assert answer.backend_names() == ["gemini"]

    def test_url_and_label_come_from_code(self):
        from app import answer, settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        cfg = answer.load_settings("gemini")
        # パスを持つ相手なので /v1 は足さない（足すと Gemini は 404 になる）
        assert cfg.url == "https://generativelanguage.googleapis.com/v1beta/openai"
        assert cfg.api_key == "k"
        assert answer.backend_label("gemini") == "Gemini"

    def test_enabled_without_a_credential_is_not_usable(self):
        """認証情報の要る相手を未登録のまま有効にしても使えない（設定を直に書き換えられた場合の保険）。"""
        from app import answer, settings_store

        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        assert answer.load_settings("gemini") is None

    def test_clearing_the_credential_also_disables(self):
        from app import answer, settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        settings_store.clear_credential("gemini")
        assert answer.backend_names() == []
        assert answer.load_settings("gemini") is None

    def test_model_can_be_chosen_per_request(self):
        from app import answer, settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        settings_store.set_model("gemini", "gemini-2.5-pro")
        assert answer.load_settings("gemini").model == "gemini-2.5-pro"
        # 都度の指定が保存してある既定より優先する
        assert answer.load_settings("gemini", model="gemini-2.5-flash").model == "gemini-2.5-flash"

    def test_model_falls_back_to_the_first_candidate(self):
        from app import answer, settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        # 控えの先頭(`app/providers.py`)。名前を直書きしない —— 相手のモデルは
        # 入れ替わるので、控えを更新するたびにテストが落ちるのは見張りたいものと関係がない
        from app import providers

        assert answer.load_settings("gemini").model == providers.get("gemini").models[0]

    def test_works_without_a_state_dir(self, monkeypatch):
        """保存先が無い環境でも落ちない（どの相手も無効なだけ）。"""
        from app import answer

        monkeypatch.delenv("CHIEZO_STATE_DIR", raising=False)
        assert answer.backend_names() == []
        assert answer.load_settings("gemini") is None
        assert answer.is_enabled() is False

    def test_unknown_backend_is_404_with_the_choices(self, monkeypatch, built_data_dir):
        from app import settings_store
        from app.main import app

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        settings_store.set_verified("local", True)
        settings_store.set_enabled("local", True)
        with TestClient(app) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺", "backend": "nope"})
        assert res.status_code == 404
        # このAPIのエラーは detail で包まず本文をそのまま返す（app/main.py の例外ハンドラ）
        body = res.json()
        assert body["backends"] == ["local"]

    def test_disabled_layer_is_still_503(self, monkeypatch, built_data_dir):
        """相手が 1 つも無いときは「無効」であって「未知の相手」ではない。"""
        from app.main import app

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        with TestClient(app) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺", "backend": "gemini"})
        assert res.status_code == 503


class TestStaleModelName:
    """控えのモデル名は古くなる。 実測で、選んでも保存してもいない Gemini が
    「llm error 404」になった —— 控えの先頭(gemini-2.5-flash)が相手から消えていた。"""

    @staticmethod
    def _offering(monkeypatch, *ids):
        """`/models` でその一覧を名乗る相手を立てる。"""
        from app import answer, settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)
        answer._MODELS_CACHE.clear()
        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"data": [{"id": i} for i in ids]})
            )),
        )
        return answer

    def test_a_fallback_model_is_replaced_by_what_the_provider_offers(
        self, monkeypatch_env, tmp_path
    ):
        monkeypatch_env.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        answer = self._offering(monkeypatch_env, "gemini-9.9-flash", "gemini-9.9-pro")

        cfg = answer.require_settings("gemini")
        assert cfg.model_is_fallback is True

        assert asyncio.run(answer.ensure_model(cfg)).model == "gemini-9.9-flash"

    def test_a_chosen_model_is_left_alone(self, monkeypatch_env, tmp_path):
        """選んだ・保存したモデルには触らない(選び直した意味が無くなる)。"""
        monkeypatch_env.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        answer = self._offering(monkeypatch_env, "gemini-9.9-flash")

        cfg = answer.require_settings("gemini", model="gemini-2.5-pro")
        assert cfg.model_is_fallback is False

        assert asyncio.run(answer.ensure_model(cfg)).model == "gemini-2.5-pro"

    def test_the_models_prefix_is_stripped(self, monkeypatch_env, tmp_path):
        """Gemini の一覧だけ `models/` が付くが、会話の口はその形を受け付けない
        (実測で 404)。画面の選択肢は一覧から作るので、剥がさないと選んだ瞬間に失敗する。"""
        monkeypatch_env.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        answer = self._offering(monkeypatch_env, "models/gemini-9.9-flash")

        assert asyncio.run(answer.available_models("gemini")) == ["gemini-9.9-flash"]
        # 保存済み・選択された値に付いていても剥がす(古い値のまま直らないのを避ける)
        assert answer.require_settings("gemini", model="models/gemini-9.9-pro").model == (
            "gemini-9.9-pro"
        )

    def test_a_fallback_that_is_gone_falls_to_the_next_candidate(
        self, monkeypatch_env, tmp_path
    ):
        """相手の一覧は「新しい順」でも「会話用だけ」でもない(引退したモデルも並ぶ)。
        控えの並びはこちらが選んだ順なので、そこから生き残りを拾う。"""
        from app import providers

        monkeypatch_env.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        spec = providers.get("gemini")
        # 控えの 2 番目だけが残っている相手を演じる(先頭は消えた)
        answer = self._offering(monkeypatch_env, "models/gemini-2.5-flash", spec.models[1])

        cfg = asyncio.run(answer.ensure_model(answer.require_settings("gemini")))

        assert cfg.model == spec.models[1]

    def test_404_says_the_model_may_be_gone(self, monkeypatch_env):
        """画面に「llm error 404」しか出ないと、何を直せばよいか分からない。"""
        from app import answer

        detail = answer._llm_error(404, '{"error": {"message": "not found"}}', "gemini-2.5-flash")

        assert "gemini-2.5-flash" in detail["hint"]
        assert "選び直す" in detail["hint"]


class TestBusyUpstream:
    """混んでいるだけの失敗は引き直す。 Gemini は「いま混んでいる」を 503 で返し
    (`The model is overloaded`)、数秒後には通ることが多い。agent モードでは道具を
    何度も引いた後に落ちるので、1 回の 503 でその手間ごと捨てるのは惜しい。"""

    @staticmethod
    def _client(monkeypatch, statuses):
        from app import answer

        seen = []

        def handler(request: httpx.Request) -> httpx.Response:
            status = statuses[min(len(seen), len(statuses) - 1)]
            seen.append(status)
            if status == 200:
                return httpx.Response(
                    200, json={"choices": [{"message": {"role": "assistant", "content": "はい"}}]}
                )
            return httpx.Response(status, json=[{"error": {"message": "The model is overloaded."}}])

        # 待ち時間は飛ばす(元の sleep を捕まえてから差し替える。差し替え後の
        # asyncio.sleep を呼ぶと自分を呼び続ける)
        real_sleep = asyncio.sleep
        monkeypatch.setattr(answer.asyncio, "sleep", lambda _s: real_sleep(0))
        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        return answer, seen

    def test_a_busy_answer_is_retried(self, monkeypatch_env, tmp_path):
        monkeypatch_env.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        answer, seen = self._client(monkeypatch_env, [503, 200])
        from app import settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)
        cfg = answer.require_settings("gemini")

        got = asyncio.run(answer.complete_message(cfg, [{"role": "user", "content": "やあ"}]))

        assert answer.content_of(got) == "はい"
        assert seen == [503, 200]   # 1 回目で諦めていない

    def test_it_gives_up_and_says_why(self, monkeypatch_env, tmp_path):
        """粘り続けない(相手が本当に落ちているとき、待たされるだけになる)。"""
        monkeypatch_env.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        answer, seen = self._client(monkeypatch_env, [503])
        from app import settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)
        cfg = answer.require_settings("gemini")

        with pytest.raises(Exception) as e:
            asyncio.run(answer.complete_message(cfg, [{"role": "user", "content": "やあ"}]))

        assert len(seen) == 3   # 初回 + 2 回の引き直し
        # 理由を出す。 Gemini はエラーを配列で返すので、dict しか見ていないと
        # 画面に「llm error 503」しか出ない
        assert "overloaded" in e.value.detail["reason"]


class TestModeForBackendsWithoutTools:
    """道具を引けない相手では agent を選ばせない。 Codex は codex exec で MCP の
    呼び出しが必ずキャンセルされる(上流の不具合)ので、agent だと道具の無いまま
    1 往復し、Chiezo の知識がまったく効かない答えが返る。"""

    def test_agent_falls_back_to_rag(self):
        from app import answer

        assert answer.resolve_mode("codex", "agent") == "rag"

    def test_other_backends_keep_agent(self):
        from app import answer

        assert answer.resolve_mode("claude", "agent") == "agent"
        assert answer.resolve_mode("local", "agent") == "agent"

    def test_the_chat_page_does_not_offer_agent_for_codex(self, monkeypatch_env):
        with make_client(monkeypatch_env, FakeLLM()) as client:
            page = client.get(CHAT_PATH, params={"backend": "codex"}).text
            other = client.get(CHAT_PATH, params={"backend": "local"}).text

        assert 'value="agent"' not in page
        assert "この相手は道具を引けません" in page
        # 他の相手では今までどおり選べる
        assert 'value="agent"' in other


class TestModelCandidates:
    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, tmp_path):
        from app import answer

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
        answer._MODELS_CACHE.clear()

    def test_falls_back_to_the_code_candidates(self):
        """相手に聞けないときはコードの控えを使う（無効な相手は聞きにいけない）。"""
        import asyncio

        from app import answer, providers

        got = asyncio.run(answer.available_models("gemini"))
        assert got == list(providers.get("gemini").models)

    def test_the_upstream_list_wins(self, monkeypatch):
        import asyncio

        from app import answer, settings_store

        settings_store.set_credential("openrouter", "k")
        settings_store.set_verified("openrouter", True)
        settings_store.set_enabled("openrouter", True)

        def handler(request):
            return httpx.Response(200, json={"data": [{"id": "x/y:free"}, {"id": "a/b:free"}]})

        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        assert asyncio.run(answer.available_models("openrouter")) == ["x/y:free", "a/b:free"]


class TestAnswerLayerSwitch:
    """「答える」層そのものの on/off（元栓）。

    相手を 1 つずつ切って回らずに機能ごと止められること、止めたら相手が有効でも
    話せなくなることを見る。
    """

    @pytest.fixture(autouse=True)
    def _state(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))

    def test_enabled_by_default(self):
        from app import settings_store

        assert settings_store.answer_enabled() is True

    def test_closing_the_master_switch_hides_every_backend(self):
        from app import answer, settings_store

        settings_store.set_verified("local", True)
        settings_store.set_enabled("local", True)
        assert answer.backend_names() == ["local"]

        settings_store.set_answer_enabled(False)
        # 相手の設定はそのまま残るが、話せる相手は 0 になる
        assert answer.backend_names() == []
        assert answer.is_enabled() is False
        # 相手を名指ししても素通りしない
        assert answer.load_settings("local") is None
        assert settings_store.load("local").enabled is True

        settings_store.set_answer_enabled(True)
        assert answer.backend_names() == ["local"]

    def test_ask_is_503_while_the_layer_is_off(self, monkeypatch, built_data_dir):
        from app import settings_store
        from app.main import app

        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        settings_store.set_verified("local", True)
        settings_store.set_enabled("local", True)
        settings_store.set_answer_enabled(False)
        with TestClient(app) as client:
            res = client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 503
        assert res.json()["error"] == "answering is disabled"


class TestSettingsMigration:
    def test_the_old_api_key_column_is_renamed(self, monkeypatch, tmp_path):
        """列名を変えたときの移行。置かないと既存 DB を読んだ瞬間に落ちる。"""
        import sqlite3

        from app import settings_store

        state = tmp_path / "state"
        state.mkdir()
        db = state / "settings.db"
        # この機能を入れた当初のスキーマ（api_key のまま）で作る
        conn = sqlite3.connect(db)
        conn.executescript(
            "CREATE TABLE provider_settings (provider TEXT PRIMARY KEY, enabled INTEGER NOT NULL"
            " DEFAULT 0, api_key TEXT, model TEXT, updated_at TEXT NOT NULL);"
            "INSERT INTO provider_settings VALUES ('claude', 1, 'old-token', '', '2026-08-13');"
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(state))
        got = settings_store.load("claude")
        assert got.credential == "old-token"  # 中身は失われない
        assert got.enabled is True
        # 二度目も落ちない（init は何度でも走る）
        assert settings_store.load("claude").credential == "old-token"


class TestJournalMode:
    def test_an_existing_wal_database_is_converted(self, monkeypatch, tmp_path):
        """WAL のまま残っていたファイルも戻す。

        journal_mode はファイルに焼き付く属性なので、コードから PRAGMA を消しただけでは
        既存の DB は WAL のまま。CLI ブリッジは /state を読み取り専用でマウントして読むので、
        WAL のままだと `unable to open database file` になり、認証情報が空に見える
        （本番で会話が 502 になった原因）。
        """
        import sqlite3

        from app import settings_store

        state = tmp_path / "state"
        state.mkdir()
        db = state / "settings.db"
        conn = sqlite3.connect(db, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE provider_settings (provider TEXT PRIMARY KEY, enabled INTEGER NOT NULL"
            " DEFAULT 0, credential TEXT, model TEXT, updated_at TEXT NOT NULL)"
        )
        conn.close()

        probe = sqlite3.connect(db, isolation_level=None)
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        probe.close()

        monkeypatch.setenv("CHIEZO_STATE_DIR", str(state))
        settings_store.set_credential("claude", "token")

        probe = sqlite3.connect(db, isolation_level=None)
        assert probe.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        probe.close()
        # -wal / -shm が残っていないこと（読み取り専用の読み手が要求してしまうため）
        assert not (state / "settings.db-wal").exists()
        assert not (state / "settings.db-shm").exists()


class TestConnectionTest:
    """「接続を試す」。登録の有無ではなく「いま使えるか」を見る。

    打ち間違えた認証情報や期限切れは登録の有無では分からず、会話して初めて失敗する
    （本番でそれが 502 として出た）。会話は 1 往復もせず `/models` を引くだけ。
    """

    @pytest.fixture(autouse=True)
    def _state(self, monkeypatch, tmp_path, built_data_dir):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))

    def _test_button(self, client, provider):
        res = client.post("/admin/ai/test", data={"provider": provider}, follow_redirects=False)
        assert res.status_code == 303
        from urllib.parse import parse_qs, urlparse

        return parse_qs(urlparse(res.headers["location"]).query)

    def test_a_rejected_credential_is_reported(self, monkeypatch):
        from app import answer, settings_store
        from app.main import app

        settings_store.set_credential("gemini", "wrong")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)

        def handler(request):
            return httpx.Response(401, json={"error": "invalid key"})

        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with TestClient(app) as client:
            q = self._test_button(client, "gemini")
        assert q["ok"] == ["0"]
        assert "401" in q["why"][0]

    def test_a_working_credential_is_reported(self, monkeypatch):
        from app import answer, settings_store
        from app.main import app

        settings_store.set_credential("gemini", "good")
        settings_store.set_verified("gemini", True)
        settings_store.set_enabled("gemini", True)
        monkeypatch.setattr(
            answer, "_llm_client",
            lambda cfg: httpx.AsyncClient(
                transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"data": []}))
            ),
        )
        with TestClient(app) as client:
            q = self._test_button(client, "gemini")
        assert q["ok"] == ["1"]

    def test_an_unregistered_provider_says_so(self):
        from app.main import app

        with TestClient(app) as client:
            q = self._test_button(client, "openrouter")
        assert q["ok"] == ["0"]
        assert "未登録" in q["why"][0]

    def test_the_result_shows_up_on_the_admin_page(self, built_data_dir):
        from app.main import app

        with TestClient(app) as client:
            ok = client.get("/admin", params={"tested": "gemini", "ok": "1"}).text
            ng = client.get("/admin", params={"tested": "gemini", "ok": "0", "why": "だめ"}).text
        assert "✅ Gemini と話せます。" in ok
        assert "⚠️ Gemini と話せません: だめ" in ng


class TestVerificationGate:
    """「接続を試す」が一度でも通るまで on にできない。

    到達できることと話せることは別で（認証情報が間違っていても到達はする）、
    登録の有無だけでは会話して初めて失敗する。本番でそれが 502 として出た。
    """

    @pytest.fixture(autouse=True)
    def _state(self, monkeypatch, tmp_path, built_data_dir):
        monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))

    def test_enabling_is_refused_until_a_test_passes(self):
        from app import settings_store
        from app.main import app

        settings_store.set_credential("gemini", "k")
        with TestClient(app) as client:
            res = client.post("/admin/ai/enabled", data={"provider": "gemini", "enabled": "1"})
            assert res.status_code == 400
            assert "接続を試す" in res.json()["error"]

            settings_store.set_verified("gemini", True)
            res = client.post(
                "/admin/ai/enabled", data={"provider": "gemini", "enabled": "1"},
                follow_redirects=False,
            )
        assert res.status_code == 303
        assert settings_store.load("gemini").enabled is True

    def test_changing_the_credential_clears_the_mark(self):
        """入れ替えた認証情報はまだ確かめていないので、試し直させる。"""
        from app import settings_store

        settings_store.set_credential("gemini", "k")
        settings_store.set_verified("gemini", True)
        assert settings_store.load("gemini").verified is True

        settings_store.set_credential("gemini", "another")
        assert settings_store.load("gemini").verified is False

    def test_a_failing_test_clears_the_mark(self):
        """一度通ったあとに壊れた相手を、通ったままにしておかない。"""
        from app import settings_store

        settings_store.set_verified("gemini", True)
        settings_store.set_verified("gemini", False)
        assert settings_store.load("gemini").verified is False

    def test_a_disabled_provider_can_still_be_tested(self, monkeypatch):
        """試さないと on にできない仕様なので、無効のままでも試せること。

        ここを弾くと「試せないから on にできない」の堂々巡りになる（実際に踏んだ）。
        """
        from app import answer, settings_store

        settings_store.set_credential("gemini", "k")
        assert settings_store.load("gemini").enabled is False
        assert answer.load_settings("gemini") is None
        assert answer.load_settings("gemini", require_enabled=False) is not None


class TestUpstreamReason:
    """相手のエラーから画面に出す理由を取り出す。

    握り潰していたころは「llm error 502」しか出ず、CLI ブリッジが理由を
    返していても画面から追えなかった。
    """

    def test_it_reads_the_cli_bridge_shape(self):
        from app import answer

        body = (
            '{"detail": {"error": "claude failed", "exit_code": 1,'
            ' "stderr": "--dangerously-skip-permissions cannot be used with root"}}'
        )
        reason = answer._upstream_reason(body)
        assert "claude failed" in reason
        assert "root" in reason

    def test_it_keeps_the_exit_code_when_the_cli_says_nothing(self):
        """理由を書かずに落ちることがある(実測: 307KB の生成が 6 秒で exit 1、stderr も空)。

        そのとき「claude failed」だけだと、断られたのか落ちたのかも分からない。
        終了コードだけは必ず残す。
        """
        from app import answer

        body = '{"detail": {"error": "claude failed", "exit_code": 1, "stderr": ""}}'
        reason = answer._upstream_reason(body)
        assert reason == "claude failed / exit 1"

    def test_it_reads_the_openai_shape(self):
        from app import answer

        body = '{"error": {"message": "invalid app key", "type": "invalid_request_error"}}'
        assert answer._upstream_reason(body) == "invalid app key"

    def test_it_reads_a_plain_detail_string(self):
        from app import answer

        assert answer._upstream_reason('{"detail": "Not Found"}') == "Not Found"

    def test_it_stays_quiet_on_shapes_it_cannot_read(self):
        from app import answer

        """**読めない本文はそのまま返さない。** 内部構成が漏れるため。"""
        assert answer._upstream_reason("<html>gateway error</html>") == ""
        assert answer._upstream_reason('["unexpected"]') == ""
        assert answer._upstream_reason('{"unrelated": "http://chiezo-llm:7011"}') == ""

    def test_it_truncates(self):
        from app import answer

        body = '{"detail": {"error": "%s"}}' % ("x" * 1000)
        assert len(answer._upstream_reason(body)) == answer.REASON_MAX

    def test_the_error_carries_the_reason(self):
        from app import answer

        detail = answer._llm_error(502, '{"detail": {"error": "claude failed"}}')
        assert detail == {"error": "llm error 502", "reason": "claude failed"}
        assert answer._llm_error(500, "boom") == {"error": "llm error 500"}


class TestEffort:
    """エフォート（考える量）は、持っている相手にだけ送る。"""

    def test_it_is_sent_only_when_chosen(self):
        from app import answer

        cfg = answer.Settings(
            url="http://x/v1", model="sonnet", api_key=None, timeout=1.0, docs=1,
            max_chars=1, agent_max_steps=1, agent_tool_chars=200, agent_timeout=1.0,
            name="claude", effort="xhigh",
        )
        assert answer._payload(cfg, [], stream=False)["reasoning_effort"] == "xhigh"
        bare = answer.Settings(
            url="http://x/v1", model="sonnet", api_key=None, timeout=1.0, docs=1,
            max_chars=1, agent_max_steps=1, agent_tool_chars=200, agent_timeout=1.0,
            name="claude",
        )
        assert "reasoning_effort" not in answer._payload(bare, [], stream=False)

    def test_unknown_values_fall_back_to_the_default(self):
        """相手が検証してくれないので、知らない値はここで落とす。"""
        from app import answer

        assert answer.normalize_effort("claude", "xhigh") == "xhigh"
        assert answer.normalize_effort("claude", "XHIGH ") == "xhigh"
        assert answer.normalize_effort("claude", "bogus") == ""
        # 相手ごとに段階が違う（agy に xhigh は無い）
        assert answer.normalize_effort("antigravity", "xhigh") == ""
        # 持たない相手には何も送らない
        assert answer.normalize_effort("codex", "high") == ""
        assert answer.normalize_effort("gemini", "high") == ""


class TestModelDefault:
    """モデルも「既定」を選べる（エフォートと同じ扱い）。"""

    @pytest.fixture(autouse=True)
    def _clean(self, monkeypatch, tmp_path):
        monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))

    def _settings(self, backend, model=None):
        from app import answer, settings_store

        settings_store.set_verified(backend, True)
        settings_store.set_enabled(backend, True)
        return answer.load_settings(backend, model)

    def test_a_relay_that_decides_for_itself_gets_nothing(self):
        """CLI ブリッジは自分で決められるので、選ばなければ渡さない。"""
        from app import settings_store

        settings_store.set_credential("claude", "token")
        cfg = self._settings("claude")
        assert cfg.model == "chiezo"  # 送っても無視される置き字（相手の既定が使われる）

    def test_a_relay_that_needs_one_still_gets_a_model(self):
        """Gemini はモデル無しでは通らないので、控えの先頭を当てる。"""
        from app import providers, settings_store

        settings_store.set_credential("gemini", "key")
        cfg = self._settings("gemini")
        assert cfg.model == providers.get("gemini").models[0]

    def test_choosing_one_still_wins(self):
        from app import settings_store

        settings_store.set_credential("claude", "token")
        assert self._settings("claude", "opus").model == "opus"



class TestBackendTimeout:
    """相手を待つ秒数はその相手が何をするかで変える。

    CLI ブリッジは道具を何度も引くので分単位になりうるうえ、ブリッジ自身が上限を持つ。
    待つ側が先に切れると、向こうの判断が一切見えなくなる（実測: claude を
    effort=high で呼んだら 120 秒で切れ、504 llm timeout しか残らなかった）。
    """

    def test_bridge_backends_wait_longer_than_the_bridge_itself(self):
        from app import answer, providers

        claude = providers.get("claude")
        assert claude.bridge, "前提: claude は CLI ブリッジ経由"
        # ブリッジ自身の既定は 300 秒、compose は 600 秒。待つ側はそれより長い
        assert answer._default_timeout(claude) > 600

    def test_direct_backends_keep_the_short_default(self):
        from app import answer, providers

        gemini = providers.get("gemini")
        assert not gemini.bridge, "前提: gemini は API で直に叩く"
        assert answer._default_timeout(gemini) == answer.DIRECT_TIMEOUT_SECONDS

    def test_env_still_wins(self, monkeypatch):
        """明示した値が常に優先する（相手で決めるのは既定だけ）。"""
        from app import answer

        monkeypatch.setenv("CHIEZO_ANSWER_TIMEOUT", "42")
        assert answer._env_num("CHIEZO_ANSWER_TIMEOUT", 900.0, float) == 42.0
        monkeypatch.delenv("CHIEZO_ANSWER_TIMEOUT")
        assert answer._env_num("CHIEZO_ANSWER_TIMEOUT", 900.0, float) == 900.0
