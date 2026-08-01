"""「使う」層(/v1/ask・/localllm/chat)のテスト。

推論サーバは立てず、`answer._llm_client` を `httpx.MockTransport` 入りのクライアントに
差し替えて偽の OpenAI 互換サーバを演じさせる。こうするとクエリ生成 → 検索 → 回答の
全経路を、実データもネットワークも無しで通せる。
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import CHAT_PATH


@pytest.fixture()
def monkeypatch_env(monkeypatch, built_data_dir):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    return monkeypatch


@pytest.fixture()
def disabled_client(monkeypatch_env):
    """CHIEZO_LLM_URL 未設定 = 使う層が無効な状態。"""
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
    from app import answer
    from app.main import app

    monkeypatch.setenv("CHIEZO_LLM_URL", env.pop("url", "http://llm.test:8080/v1"))
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
    def test_ask_returns_503_when_llm_url_is_unset(self, disabled_client):
        res = disabled_client.get("/v1/ask", params={"q": "浅草寺はどこ?"})
        assert res.status_code == 503
        assert res.json()["error"] == "answering is disabled"
        assert "CHIEZO_LLM_URL" in res.json()["hint"]

    def test_ask_page_explains_how_to_enable(self, disabled_client):
        res = disabled_client.get(CHAT_PATH)
        assert res.status_code == 200
        assert "CHIEZO_LLM_URL" in res.text
        # 無効でもフォーム自体は出す(何のページか分かるように)
        assert 'name="q"' in res.text

    def test_admin_shows_the_feature_as_disabled(self, disabled_client):
        res = disabled_client.get("/admin")
        assert res.status_code == 200
        assert "「使う」層は無効です" in res.text


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
        assert "ConnectError" == res.json()["reason"]  # 種別だけ返す(文言は返さない)

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
        ],
    )
    def test_base_url_is_normalized(self, monkeypatch, raw, expected):
        from app import answer

        monkeypatch.setenv("CHIEZO_LLM_URL", raw)
        cfg = answer.load_settings()
        assert cfg.url == expected
        assert cfg.endpoint == f"{expected}/chat/completions"

    def test_disabled_when_url_is_blank(self, monkeypatch):
        from app import answer

        monkeypatch.setenv("CHIEZO_LLM_URL", "   ")
        assert answer.load_settings() is None
        assert answer.is_enabled() is False
