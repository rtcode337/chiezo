"""素の問い合わせ(/v1/ai/backends・/v1/ai/complete)のテスト。

`/v1/chat` が知識ベースを引いて答えるのに対し、こちらは**渡したプロンプトをそのまま**
相手へ投げるだけの口。呼ぶ側(tech-antenna のサマリー生成など)は自分の材料と
プロンプトを持っていて、Chiezo に借りたいのは「話せる相手と鍵」だけ。
"""
import pytest
from fastapi.testclient import TestClient
from test_agent import ToolLLM, make_client


@pytest.fixture()
def monkeypatch_env(monkeypatch, built_data_dir, tmp_path):
    from app import answer

    monkeypatch.setenv("CHIEZO_DATA_DIR", str(built_data_dir))
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    # モデル一覧はプロセス内にキャッシュされる(TTL つき)。他のテストが入れた値を
    # 引き継ぐと、この偽の相手が名乗った一覧と食い違う
    answer._MODELS_CACHE.clear()
    return monkeypatch


def complete(client: TestClient, **body):
    return client.post("/v1/ai/complete", json=body)


class TestBackends:
    def test_lists_enabled_backends_with_models(self, monkeypatch_env):
        """呼ぶ側が画面を作れるだけの材料(相手・モデル・エフォート)を返す。"""
        fake = ToolLLM(models=["qwen3-8b"])
        with make_client(monkeypatch_env, fake) as client:
            body = client.get("/v1/ai/backends").json()

        local = next(b for b in body["backends"] if b["id"] == "local")
        assert local["label"] == "推論サーバ"
        assert local["models"] == ["qwen3-8b"]
        # 推論サーバは 1 プロセス 1 モデルなので、モデル指定は必須ではない
        assert local["model_required"] is False

    def test_disabled_backends_are_not_listed(self, monkeypatch_env):
        """管理画面で on にしていない相手は出さない(鍵が無く、選ばせても失敗する)。"""
        from app import settings_store

        with make_client(monkeypatch_env, ToolLLM()) as client:
            settings_store.set_enabled("local", False)
            body = client.get("/v1/ai/backends").json()

        assert [b["id"] for b in body["backends"]] == []


class TestComplete:
    def test_sends_the_prompt_as_given(self, monkeypatch_env):
        """**抽出を混ぜない。** 渡したメッセージがそのまま相手へ届く。"""
        fake = ToolLLM("まとめました。")
        messages = [
            {"role": "system", "content": "あなたは編集者。"},
            {"role": "user", "content": "材料A / 材料B"},
        ]
        with make_client(monkeypatch_env, fake) as client:
            res = complete(client, messages=messages)

        assert res.status_code == 200
        assert res.json()["content"] == "まとめました。"
        assert res.json()["backend"] == "local"
        # 知識ベースを引かないので、送られるのは渡した2件だけ
        assert fake.requests[0]["messages"] == messages

    def test_model_and_effort_are_passed_through(self, monkeypatch_env):
        fake = ToolLLM("はい")
        with make_client(monkeypatch_env, fake) as client:
            res = complete(
                client,
                messages=[{"role": "user", "content": "やあ"}],
                backend="local",
                model="qwen3-8b",
            )

        assert res.json()["model"] == "qwen3-8b"
        assert fake.requests[0]["model"] == "qwen3-8b"

    def test_unknown_backend_is_404_with_the_choices(self, monkeypatch_env):
        """選べる相手を返す —— 名前を間違えたときに次にすることが分かるように。"""
        with make_client(monkeypatch_env, ToolLLM()) as client:
            res = complete(
                client, messages=[{"role": "user", "content": "やあ"}], backend="gpt5"
            )

        assert res.status_code == 404
        # エラーは detail で包まずそのまま返す(このアプリの例外ハンドラの約束)
        assert "local" in res.json()["backends"]

    def test_empty_messages_is_400(self, monkeypatch_env):
        with make_client(monkeypatch_env, ToolLLM()) as client:
            res = complete(client, messages=[{"role": "user", "content": "   "}])

        assert res.status_code == 400


class TestCompleteWeb:
    """相手自身の web 検索を開ける口(`web=true`)。

    ニュースの収集のように「いまの外の情報」が要る仕事を、`/v1/chat` の抽出を混ぜずに
    頼めるようにするためのもの。**持っているのは CLI ブリッジで包んだ相手だけ。**
    """

    def test_web_is_off_by_default(self, monkeypatch_env):
        """頼まなければ道具は渡さない(既定の振る舞いを変えない)。"""
        fake = ToolLLM("はい")
        with make_client(monkeypatch_env, fake) as client:
            res = complete(client, messages=[{"role": "user", "content": "やあ"}])

        assert res.json()["web"] is False
        assert "chiezo_web" not in fake.requests[0]

    def test_web_is_rejected_when_no_route_exists(self, monkeypatch_env):
        """**黙って道具無しで答えさせない。**

        呼ぶ側はそれを「調べた結果」として受け取り、学習データから作った話が
        最新の材料として保存されてしまう。開けないなら断る。
        推論サーバは CLI ブリッジでなく、SearXNG も設定されていない状態。
        """
        fake = ToolLLM("はい")
        with make_client(monkeypatch_env, fake) as client:
            res = complete(
                client, messages=[{"role": "user", "content": "やあ"}], web=True
            )

        assert res.status_code == 400
        assert "web" in res.json()["error"]
        # 断ったのだから相手には投げていない
        assert fake.requests == []

    def test_web_uses_searxng_for_api_backends(self, monkeypatch_env):
        """**API で直に叩く相手には Chiezo の SearXNG を道具として貸す。**

        OpenAI 互換の口に検索の項目が無くても、道具として渡せば外を見られる。
        """
        from app import websearch

        monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searxng:8080/search")
        # 検索そのものは外へ出さない（実行されたことと、その結果が積まれることを見る）
        asked: list[str] = []

        async def fake_search(q, limit=None):
            asked.append(q)
            return {"results": [{"title": "終値", "url": "https://example.com", "snippet": "…"}]}

        monkeypatch_env.setattr(websearch, "search", fake_search)
        # 1ターン目で検索を呼び、2ターン目で答える
        fake = ToolLLM([("web_search", {"q": "日経平均 終値"})], "終値は…でした。")
        with make_client(monkeypatch_env, fake) as client:
            res = complete(
                client, messages=[{"role": "user", "content": "今日の終値は?"}], web=True
            )

        assert res.status_code == 200
        assert res.json()["content"] == "終値は…でした。"
        assert res.json()["web"] is True
        # 道具として渡したのは web 検索だけ（知識ベースの道具は混ぜない）
        names = [t["function"]["name"] for t in fake.requests[0]["tools"]]
        assert names == ["web_search"]
        assert asked == ["日経平均 終値"]

    def test_backends_tell_whether_web_is_available(self, monkeypatch_env):
        """選べてしまってから 400 で断られないよう、一覧に能力を出す。"""
        with make_client(monkeypatch_env, ToolLLM(models=["qwen3-8b"])) as client:
            body = client.get("/v1/ai/backends").json()

        local = next(b for b in body["backends"] if b["id"] == "local")
        # SearXNG 未設定・CLIブリッジでもないので開けられない
        assert local["web"] is False

    def test_backends_report_web_once_searxng_is_set(self, monkeypatch_env):
        monkeypatch_env.setenv("CHIEZO_WEB_SEARCH_URL", "http://searxng:8080/search")
        with make_client(monkeypatch_env, ToolLLM(models=["qwen3-8b"])) as client:
            body = client.get("/v1/ai/backends").json()

        local = next(b for b in body["backends"] if b["id"] == "local")
        assert local["web"] is True
