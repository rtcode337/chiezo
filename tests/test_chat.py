"""会話(/v1/chat)と web 検索の道具のテスト。

`/v1/ask` が 1 問 1 答なのに対し、`/v1/chat` は **messages をまるごと受け取る**
(サーバーは会話の状態を持たず、履歴はクライアントが毎回送る)。ここで確かめるのは
その約束と、履歴がちゃんとモデルまで届いていること。

web 検索は `websearch._client` を差し替えて偽の検索サーバを演じさせる
(外に出ずに、道具の出方・出典の付き方・無効時の振る舞いを通せる)。
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import CHAT_PATH
from test_agent import ANSWER, SEARCH_ASAKUSA, ToolLLM, make_client  # noqa: F401


@pytest.fixture()
def monkeypatch_env(monkeypatch, built_data_dir):
    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.delenv("CHIEZO_WEB_SEARCH_URL", raising=False)
    monkeypatch.delenv("CHIEZO_ASK_DEFAULT_MODE", raising=False)
    monkeypatch.delenv("CHIEZO_ASK_DEFAULT_GROUNDED", raising=False)
    return monkeypatch


PLAN_OK = json.dumps({"queries": [{"source": "jawiki", "q": "浅草寺"}]}, ensure_ascii=False)


def chat(client: TestClient, messages, **body):
    return client.post("/v1/chat", json={"messages": messages, **body})


class TestChatContract:
    def test_last_user_message_is_the_question(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = chat(client, [{"role": "user", "content": "浅草寺はどこ?"}], mode="agent")
        assert res.status_code == 200
        assert res.json()["answer"] == ANSWER
        assert res.json()["question"] == "浅草寺はどこ?"

    def test_history_reaches_the_model(self, monkeypatch_env):
        """「じゃあ京都のほうは?」が通じるのは、直前のやり取りを毎回送るから。"""
        fake = ToolLLM(ANSWER)
        history = [
            {"role": "user", "content": "浅草寺について教えて"},
            {"role": "assistant", "content": "東京都台東区の寺院です。"},
            {"role": "user", "content": "じゃあ京都のほうは?"},
        ]
        with make_client(monkeypatch_env, fake) as client:
            chat(client, history, mode="agent")
        sent = fake.requests[0]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
        assert sent[-1]["content"] == "じゃあ京都のほうは?"
        assert sent[2]["content"] == "東京都台東区の寺院です。"

    def test_rag_mode_sees_the_history_when_building_queries(self, monkeypatch_env):
        """rag はクエリ生成の段でも履歴が要る(指示語を検索語に直せない)。"""
        fake = ToolLLM(PLAN_OK, ANSWER)
        history = [
            {"role": "user", "content": "浅草寺について教えて"},
            {"role": "assistant", "content": "東京都台東区の寺院です。"},
            {"role": "user", "content": "その最寄り駅は?"},
        ]
        with make_client(monkeypatch_env, fake) as client:
            res = chat(client, history, mode="rag")
        assert res.status_code == 200
        plan_prompt = fake.requests[0]["messages"][-1]["content"]
        assert "これまでのやり取り:" in plan_prompt
        assert "東京都台東区の寺院です。" in plan_prompt

    def test_empty_or_assistant_tail_is_400(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM(ANSWER)) as client:
            assert chat(client, []).status_code == 400
            assert chat(client, [{"role": "assistant", "content": "はい"}]).status_code == 400

    def test_streaming_uses_the_same_events(self, monkeypatch_env):
        fake = ToolLLM(SEARCH_ASAKUSA, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            res = client.post(
                "/v1/chat?stream=1",
                json={"messages": [{"role": "user", "content": "浅草寺は?"}], "mode": "agent"},
            )
        events = [
            line[len("event: "):]
            for line in res.text.splitlines() if line.startswith("event: ")
        ]
        assert events == ["meta", "step", "references", "delta", "done"]

    def test_disabled_layer_is_503(self, monkeypatch_env):
        monkeypatch_env.delenv("CHIEZO_LLM_URL", raising=False)
        from app.main import app

        with TestClient(app) as client:
            res = chat(client, [{"role": "user", "content": "こんにちは"}])
        assert res.status_code == 503


class TestDefaults:
    """既定は環境変数で決める(GPU の機械と CPU だけの機械で妥当な既定が違うため)。"""

    def test_rag_and_grounded_are_the_plain_defaults(self, monkeypatch_env):
        fake = ToolLLM(PLAN_OK, ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = chat(client, [{"role": "user", "content": "浅草寺は?"}]).json()
        assert "queries" in body and body["grounded"] is True

    def test_env_can_flip_them(self, monkeypatch_env):
        fake = ToolLLM(ANSWER)
        with make_client(
            monkeypatch_env, fake,
            CHIEZO_ASK_DEFAULT_MODE="agent", CHIEZO_ASK_DEFAULT_GROUNDED="0",
        ) as client:
            body = chat(client, [{"role": "user", "content": "こんにちは"}]).json()
        assert body["mode"] == "agent" and body["grounded"] is False
        # 道具を呼ばずに答えてよい(= 雑談がそのまま通る)
        assert body["steps"] == []

    def test_page_follows_the_env_default(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM(), CHIEZO_ASK_DEFAULT_MODE="agent") as client:
            res = client.get(CHAT_PATH)
        assert '<option value="agent" selected>' in res.text


class FakeSearch:
    """偽の検索サーバ(SearXNG の JSON 形式)。"""

    def __init__(self, *titles):
        self.titles = titles
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200, json={
            "results": [
                {"title": t, "content": f"{t} の要約", "url": f"https://example.com/{i}"}
                for i, t in enumerate(self.titles)
            ]
        })


@pytest.fixture()
def web(monkeypatch_env):
    from app import websearch

    fake = FakeSearch("最新のニュース", "続報")
    monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searx.test/search")
    monkeypatch_env.setattr(websearch, "MIN_INTERVAL", 0.0)
    monkeypatch_env.setattr(
        websearch, "_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    return fake


class TestWebSearch:
    def test_tool_is_hidden_while_disabled(self, monkeypatch_env):
        """無効なら道具ごと出さない(使えないものを文脈に並べない)。"""
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            chat(client, [{"role": "user", "content": "こんにちは"}], mode="agent")
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "web_search" not in names

    def test_tool_appears_when_enabled(self, monkeypatch_env, web):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            chat(client, [{"role": "user", "content": "こんにちは"}], mode="agent")
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "web_search" in names
        # Chiezo が先、という順番はプロンプト側で固定する
        assert "まず Chiezo を引く" in fake.requests[0]["messages"][0]["content"]

    def test_results_become_web_references(self, monkeypatch_env, web):
        fake = ToolLLM([("web_search", {"q": "最新のニュース"})], "web で調べた限り…")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "最近のニュースは?"}], mode="agent"
            ).json()
        assert body["steps"][0]["ok"] is True
        # 出典は web と分かる形で並ぶ(Chiezo の文書と混ざっても区別できる)
        assert [r["source"] for r in body["references"]] == ["web", "web"]
        assert body["references"][0]["url"].startswith("https://example.com/")
        assert len(web.requests) == 1

    def test_user_agent_carries_no_personal_information(self, monkeypatch_env):
        """名乗るのはプロジェクト名だけ。連絡先や個人名を相手のログに残さない。"""
        from app import websearch

        monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searx.test/search")
        ua = websearch._client().headers["user-agent"]
        assert ua == "chiezo (local knowledge server)"
        assert "@" not in ua

    def test_web_results_count_as_evidence_when_grounded(self, monkeypatch_env, web):
        """grounded は「取ってきた根拠に限る」で、その根拠源に web も含む。"""
        fake = ToolLLM([("web_search", {"q": "最新のニュース"})], "web によれば…")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "最近のニュースは?"}],
                mode="agent", grounded=True,
            ).json()
        assert body["answer"] == "web によれば…"

    def test_calling_it_while_disabled_is_refused(self, monkeypatch_env):
        fake = ToolLLM([("web_search", {"q": "何か"})], "はい")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "何か"}], mode="agent", grounded=False
            ).json()
        assert body["steps"][0]["ok"] is False
        assert "disabled" in body["steps"][0]["summary"]

    def test_failures_are_handed_back_to_the_model(self, monkeypatch_env, monkeypatch):
        from app import websearch

        def refuse(request):
            raise httpx.ConnectError("connection refused", request=request)

        monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searx.test/search")
        monkeypatch_env.setattr(websearch, "MIN_INTERVAL", 0.0)
        monkeypatch_env.setattr(
            websearch, "_client",
            lambda: httpx.AsyncClient(transport=httpx.MockTransport(refuse)),
        )
        fake = ToolLLM([("web_search", {"q": "何か"})], "web は使えませんでした")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "何か"}], mode="agent", grounded=False
            ).json()
        assert body["steps"][0]["ok"] is False
        assert "unreachable" in body["steps"][0]["summary"]
        assert body["answer"] == "web は使えませんでした"


class TestWhoYouAreTalkingTo:
    """話す相手は AI(モデル)で、Chiezo はその AI が引く知識、という関係を画面に出す。"""

    def test_heading_names_the_model(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM(), CHIEZO_LLM_MODEL="Qwen/Qwen3-8B-GGUF:Q4_K_M") as c:
            res = c.get(CHAT_PATH)
        # 配布元・GGUF・量子化は落として名乗る
        assert "<h1>AI(Qwen3-8B)と話す</h1>" in res.text
        assert "Chiezo と話す" not in res.text

    def test_heading_falls_back_when_the_model_is_unknown(self, monkeypatch_env):
        """モデル名が取れない(推論サーバに繋がらない)ときも画面は出す。"""
        from app import answer

        answer._MODEL_LABEL_CACHE.clear()
        with make_client(monkeypatch_env, None) as c:   # 推論サーバは居ない
            res = c.get(CHAT_PATH)
        assert "<h1>AI と話す</h1>" in res.text

    def test_prompt_tells_the_model_it_is_not_chiezo(self, monkeypatch_env):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            chat(client, [{"role": "user", "content": "こんにちは"}], mode="agent")
        system = fake.requests[0]["messages"][0]["content"]
        assert "Chiezo はあなたが引く知識であって、あなた自身ではありません" in system


class TestWebToggle:
    """web 検索はやり取りごとに切れる(画面のトグルがこれを毎回送る)。"""

    def test_request_can_turn_it_off(self, monkeypatch_env, web):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "こんにちは"}], mode="agent", web=False
            ).json()
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "web_search" not in names
        # 使わせないときは、使い分けの指示もプロンプトに載せない
        assert "まず Chiezo を引く" not in fake.requests[0]["messages"][0]["content"]
        assert body["web"] is False

    def test_request_cannot_conjure_it_when_unconfigured(self, monkeypatch_env):
        """サーバー側で設定していなければ、頼まれても使えない。"""
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "こんにちは"}], mode="agent", web=True
            ).json()
        from app import agent

        assert {t["function"]["name"] for t in fake.requests[0]["tools"]} == set(agent.AGENT_TOOLS)
        assert body["web"] is False

    def test_default_is_the_server_setting(self, monkeypatch_env, web):
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = chat(client, [{"role": "user", "content": "こんにちは"}], mode="agent").json()
        assert body["web"] is True

    def test_turning_it_off_also_refuses_the_call(self, monkeypatch_env, web):
        """道具を出していないのにモデルが呼んできたら、実行せず突き返す。"""
        fake = ToolLLM([("web_search", {"q": "何か"})], "Chiezo だけで答えます")
        with make_client(monkeypatch_env, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "何か"}],
                mode="agent", grounded=False, web=False,
            ).json()
        assert body["steps"][0]["ok"] is False
        assert len(web.requests) == 0  # 外へは出ていない


class TestChatPageLayout:
    """入力欄は高さを持たせ、設定はその下に並べる。"""

    def test_composer_has_a_textarea_with_room(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            html = client.get(CHAT_PATH).text
        assert '<textarea id="q"' in html and 'rows="3"' in html
        # 設定は入力欄の「下」
        assert html.index("composer-settings") > html.index("composer-box")

    def test_web_toggle_shows_up_only_when_configured(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            assert 'id="web"' not in client.get(CHAT_PATH).text

    def test_web_toggle_is_there_when_configured(self, monkeypatch_env, web):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            html = client.get(CHAT_PATH).text
        assert 'id="web"' in html and "web 検索" in html

    def test_admin_pages_keep_their_own_plain_look(self, monkeypatch_env):
        """会話画面のスタイルは管理画面に漏らさない(あちらは素っ気ないままでよい)。"""
        with make_client(monkeypatch_env, ToolLLM()) as client:
            assert "composer-box" not in client.get("/admin").text
            assert "composer-box" in client.get(CHAT_PATH).text


@pytest.fixture()
def notes_on(monkeypatch_env, tmp_path):
    """notes(唯一書き込めるソース)を有効にした状態。"""
    monkeypatch_env.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
    return monkeypatch_env


class TestNotesTools:
    """「覚えておいて」に応えられること。

    書き込みを伴うので、当初は agent に渡していなかった。会話で明示的に頼まれるなら
    副作用ではないので渡すが、**やり取りごとに切れる**ことと、**何を書いたかが手順に
    出る**ことをここで固定する。
    """

    def test_tools_are_hidden_while_notes_are_disabled(self, monkeypatch_env):
        monkeypatch_env.delenv("CHIEZO_NOTES_DIR", raising=False)
        fake = ToolLLM(ANSWER)
        with make_client(monkeypatch_env, fake) as client:
            body = chat(client, [{"role": "user", "content": "覚えておいて"}], mode="agent").json()
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert "remember" not in names and "recall" not in names
        assert body["notes"] is False

    def test_tools_appear_when_notes_are_enabled(self, notes_on):
        fake = ToolLLM(ANSWER)
        with make_client(notes_on, fake) as client:
            body = chat(client, [{"role": "user", "content": "覚えておいて"}], mode="agent").json()
        names = {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert {"remember", "recall"} <= names
        assert body["notes"] is True

    def test_request_can_turn_them_off(self, notes_on):
        fake = ToolLLM(ANSWER)
        with make_client(notes_on, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "覚えておいて"}], mode="agent", notes=False
            ).json()
        assert "remember" not in {t["function"]["name"] for t in fake.requests[0]["tools"]}
        assert body["notes"] is False

    def test_writing_is_refused_when_turned_off(self, notes_on):
        """道具を出していないのにモデルが呼んできたら、実行せず突き返す。"""
        fake = ToolLLM([("remember", {"text": "勝手に書く"})], "書けませんでした")
        with make_client(notes_on, fake) as client:
            body = chat(
                client, [{"role": "user", "content": "何か"}],
                mode="agent", grounded=False, notes=False,
            ).json()
        assert body["steps"][0]["ok"] is False
        assert "disabled" in body["steps"][0]["summary"]

    def test_remember_then_recall_round_trip(self, notes_on):
        """覚えたものが、次の会話で思い出せる(Chiezo に実際に書かれている)。"""
        write = ToolLLM([("remember", {"text": "agent モードは 8B 級が前提"})], "覚えました")
        with make_client(notes_on, write) as client:
            body = chat(
                client, [{"role": "user", "content": "…と覚えておいて"}], mode="agent"
            ).json()
            assert body["steps"][0]["ok"] is True
            # REST から見ても入っている
            stored = client.get("/v1/notes/recall").json()["notes"]
            assert any("8B 級" in n["text"] for n in stored)

        read = ToolLLM([("recall", {"q": "agent"})], "8B 級が前提、でした")
        with make_client(notes_on, read) as client:
            body = chat(
                client, [{"role": "user", "content": "さっきの話を思い出して"}], mode="agent"
            ).json()
        assert body["steps"][0]["ok"] is True
        # 思い出したメモは出典としても並ぶ(リンク先は notes のブラウズ画面)
        assert body["references"], "recall の結果が出典に出ていない"
        assert body["references"][0]["url"].startswith("/search/notes/doc/")

    def test_page_shows_the_toggle_only_when_enabled(self, monkeypatch_env, tmp_path):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            monkeypatch_env.delenv("CHIEZO_NOTES_DIR", raising=False)
            assert 'id="notes"' not in client.get(CHAT_PATH).text
        monkeypatch_env.setenv("CHIEZO_NOTES_DIR", str(tmp_path / "notes"))
        with make_client(monkeypatch_env, ToolLLM()) as client:
            assert 'id="notes"' in client.get(CHAT_PATH).text
