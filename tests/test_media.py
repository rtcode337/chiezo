"""画像生成(/v1/media/*・MCP の image_*)のテスト。

**知識を引くのとは別の仕事**だが、口は Chiezo にまとめてある(MCP の登録先を増やさない
ため)。ここで確かめるのは、相手ごとの呼び方・出来た画像の受け渡し・置き場の守り。

外部にも GPU にも出ないよう、`media_backends._client` を差し替えて偽のサーバーを演じさせる。
"""
import asyncio
import base64
import json

import httpx
import pytest
from fastapi import HTTPException

from app import media, media_backends, media_providers, settings_store

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32


@pytest.fixture()
def state(monkeypatch, tmp_path):
    """置き場と設定 DB を一時ディレクトリに逃がす。

    **自前の GPU は既定で無効**(話す相手と同じで、明示的に on にする)。
    ほとんどのテストは「使える状態」を前提にするので、ここで on にしておく。
    """
    monkeypatch.setenv("CHIEZO_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("CHIEZO_MEDIA_DIR", raising=False)
    monkeypatch.delenv("CHIEZO_IMAGE_URL", raising=False)
    settings_store.set_enabled("comfyui", True)
    return monkeypatch


def fake_comfy(handler_images=1):
    """ComfyUI を演じる。/object_info → /prompt → /history → /view の順に応じる。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/object_info/CheckpointLoaderSimple"):
            return httpx.Response(200, json={
                "CheckpointLoaderSimple": {
                    "input": {"required": {"ckpt_name": [["sdxl.safetensors", "sd15.ckpt"]]}}
                }
            })
        if path.endswith("/prompt"):
            fake_comfy.sent = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": "p1"})
        if "/history/" in path:
            return httpx.Response(200, json={
                "p1": {"outputs": {"7": {"images": [{"filename": "a.png", "type": "output"}]}}}
            })
        if path.endswith("/view"):
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)

    return handler


def fake_gemini(status=200, body=None):
    def handler(request: httpx.Request) -> httpx.Response:
        fake_gemini.sent = json.loads(request.content)
        fake_gemini.key = request.headers.get("x-goog-api-key")
        if status >= 400:
            return httpx.Response(status, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=body or {
            "steps": [{"type": "model_output", "content": [
                {"type": "image", "data": base64.b64encode(PNG).decode()}
            ]}]
        })

    return handler


def use(monkeypatch, handler):
    monkeypatch.setattr(
        media_backends, "_client",
        lambda timeout: httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


class TestComfyUI:
    def test_generates_with_the_first_checkpoint_when_no_model_is_given(self, state):
        """置いてあるモデルは環境ごとに違うので、**相手に聞く**。"""
        use(state, fake_comfy())

        image = asyncio.run(media_backends.generate(
            "comfyui", media_backends.ImageRequest(prompt="猫", size="512x512", seed=7)))

        assert image.data == PNG
        assert image.model == "sdxl.safetensors"
        assert image.seed == 7
        graph = fake_comfy.sent["prompt"]
        assert graph["2"]["inputs"]["text"] == "猫"
        assert graph["4"]["inputs"] == {"width": 512, "height": 512, "batch_size": 1}
        assert graph["5"]["inputs"]["seed"] == 7

    def test_seed_is_decided_here_when_not_given(self, state):
        """0 のまま相手任せにすると、あとで同じ絵を作り直せない。"""
        use(state, fake_comfy())

        image = asyncio.run(media_backends.generate(
            "comfyui", media_backends.ImageRequest(prompt="猫")))

        assert image.seed > 0
        assert fake_comfy.sent["prompt"]["5"]["inputs"]["seed"] == image.seed

    def test_url_can_be_overridden_for_another_machine(self, state):
        """GPU は別マシンに置くことが多い(コンテナ名では辿り着けない)。"""
        state.setenv("CHIEZO_IMAGE_URL", "http://192.0.2.5:7014/")
        spec = media_providers.get("comfyui")

        assert media_providers.url_of(spec) == "http://192.0.2.5:7014"


class TestGemini:
    def test_uses_the_key_registered_for_chat(self, state):
        """**鍵を 2 か所に持たない** —— 「話す相手」に登録済みのものを流用する。"""
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", True)
        use(state, fake_gemini())

        image = asyncio.run(media_backends.generate(
            "gemini", media_backends.ImageRequest(prompt="城", size="1536x1024")))

        assert image.data == PNG
        assert fake_gemini.key == "AIza-test"
        # 画素ではなく比率で頼む(相手の語彙に合わせる)。1536x1024 は 3:2
        assert fake_gemini.sent["response_format"]["aspect_ratio"] == "3:2"
        assert fake_gemini.sent["model"] == "gemini-3.1-flash-image"

    def test_missing_key_says_where_to_put_it(self, state):
        settings_store.set_enabled("gemini", True)
        use(state, fake_gemini())

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "gemini", media_backends.ImageRequest(prompt="城")))

        # 鍵が無いのは 401(入れれば直る)、無効にしてあるのは 403(画面で有効にする)
        assert e.value.status_code == 401
        assert "話す相手" in e.value.detail["hint"]


class TestOpenAI:
    def test_asks_for_the_nearest_allowed_size(self, state):
        """**相手は決まった組み合わせしか取らない**ので、近いものへ寄せる。"""
        settings_store.set_credential("openai", "sk-test")
        settings_store.set_enabled("openai", True)

        def handler(request: httpx.Request) -> httpx.Response:
            handler.sent = json.loads(request.content)
            handler.auth = request.headers.get("authorization")
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        use(state, handler)

        image = asyncio.run(media_backends.generate(
            "openai", media_backends.ImageRequest(prompt="剣", size="1280x720")))

        assert image.data == PNG
        assert image.model == "gpt-image-2"
        assert handler.auth == "Bearer sk-test"
        # 16:9 に近いのは 3840x2160(1024x1024 でも 1536x1024 でもない)
        assert handler.sent["size"] == "3840x2160"

    def test_missing_key_says_where_to_put_it(self, state):
        settings_store.set_enabled("openai", True)
        use(state, lambda request: httpx.Response(200, json={"data": []}))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "openai", media_backends.ImageRequest(prompt="剣")))

        assert e.value.status_code == 401
        assert "話す相手" in e.value.detail["hint"]

    def test_upstream_error_does_not_leak_the_key(self, state):
        """理由の頭だけ返す(鍵は載せない)。403 は組織の本人確認で返ることがある。"""
        settings_store.set_credential("openai", "sk-test")
        settings_store.set_enabled("openai", True)
        use(state, lambda request: httpx.Response(403, json={"error": {"message": "must verify"}}))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "openai", media_backends.ImageRequest(prompt="剣")))

        assert e.value.status_code == 502
        assert "403" in e.value.detail["error"]
        assert "sk-test" not in json.dumps(e.value.detail)


class TestCodex:
    """**ChatGPT のサブスク枠**で gpt-image-2 を使う経路(API の従量課金とは別勘定)。"""

    def test_asks_the_bridge_and_takes_the_image(self, state):
        settings_store.set_enabled("codex", True)
        settings_store.set_credential("codex", '{"tokens": "…"}')

        def handler(request: httpx.Request) -> httpx.Response:
            handler.url = str(request.url)
            handler.sent = json.loads(request.content)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        use(state, handler)

        image = asyncio.run(media_backends.generate(
            "codex", media_backends.ImageRequest(prompt="盾", size="1536x1024")))

        assert image.data == PNG
        # 鍵はこちらに無い(ブリッジが持つ)。投げ先はブリッジの画像の口
        assert handler.url.endswith("/v1/images/generations")
        assert handler.sent == {"prompt": "盾", "size": "1536x1024", "n": 1}
        # 実際に描くのは gpt-image-2(モデルは Codex の内蔵ツールが決める)
        assert image.model == "gpt-image-2"

    def test_follows_the_codex_switch_in_the_chat_providers(self, state):
        """鍵も on/off も「話す相手」の Codex と共通。"""
        settings_store.set_credential("codex", '{"tokens": "…"}')
        settings_store.set_enabled("codex", False)
        use(state, lambda request: httpx.Response(200, json={"data": []}))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "codex", media_backends.ImageRequest(prompt="盾")))

        assert e.value.status_code == 403


class TestJobs:
    def test_saves_the_image_and_returns_a_path_and_url(self, state):
        """**画像そのものは返さない**(1 枚 1〜2MB でコンテキストが飛ぶ)。"""
        use(state, fake_comfy())
        job = media.create_job("猫", backend="comfyui", size="512x512")
        asyncio.run(media._run(
            job["id"], "comfyui", media_backends.ImageRequest(prompt="猫", size="512x512"), 1))

        done = media.get_job(job["id"])
        assert done["state"] == "done"
        file = done["files"][0]
        assert file["url"].startswith("/media/")
        assert media.resolve(file["url"].removeprefix("/media/")).read_bytes() == PNG

    def test_keeps_what_was_drawn_when_one_fails(self, state):
        """3 枚頼んで 2 枚描けたなら、その 2 枚は使える(GPU の時間を捨てない)。"""
        calls = {"n": 0}

        async def flaky(backend, req):
            calls["n"] += 1
            if calls["n"] == 2:
                raise HTTPException(502, {"error": "落ちた"})
            return media_backends.GeneratedImage(PNG, "image/png", 1, "m")

        state.setattr(media_backends, "generate", flaky)
        job = media.create_job("猫", backend="comfyui", count=3)
        asyncio.run(media._run(job["id"], "comfyui", media_backends.ImageRequest(prompt="猫"), 3))

        done = media.get_job(job["id"])
        assert done["state"] == "partial"
        assert len(done["files"]) == 1
        assert "落ちた" in done["error"]

    def test_unknown_backend_is_404_with_the_choices(self, state):
        with pytest.raises(HTTPException) as e:
            media.create_job("猫", backend="dalle")

        assert e.value.status_code == 404
        assert "comfyui" in e.value.detail["backends"]

    def test_broken_size_is_rejected_before_starting(self, state):
        """走らせてから落ちると、待たされ損になる。"""
        with pytest.raises(HTTPException) as e:
            media.create_job("猫", size="おおきめ")

        assert e.value.status_code == 400


class TestSwitchedOff:
    def test_comfyui_has_its_own_toggle(self, state):
        """自前の GPU は「話す相手」に出てこないので、自分の on/off を持つ。"""
        settings_store.set_enabled("comfyui", False)
        use(state, fake_comfy())

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "comfyui", media_backends.ImageRequest(prompt="猫")))

        assert e.value.status_code == 403
        assert "無効" in e.value.detail["error"]

    def test_connection_check_reports_the_checkpoints(self, state):
        """「接続を試す」は繋がるかとモデルの有無まで見る(立っているだけでは描けない)。"""
        use(state, fake_comfy())
        ok, why = asyncio.run(media.check("comfyui"))
        assert ok is True
        assert "sdxl.safetensors" in why

        use(state, lambda request: httpx.Response(500))
        ok, why = asyncio.run(media.check("comfyui"))
        assert ok is False
        assert "繋がりません" in why

    def test_connection_check_is_only_for_our_own_gpu(self, state):
        """外部サービスは「話す相手」側で試す(同じ確認を 2 か所に持たない)。"""
        with pytest.raises(HTTPException) as e:
            asyncio.run(media.check("gemini"))

        assert e.value.status_code == 404


    """**「話す相手」で無効にしたら絵も描けない。** 鍵を持っている相手を止めたのに
    片方だけ動き続けるのは、止めたつもりの人にとって事故になる。"""

    def test_disabled_provider_cannot_draw(self, state):
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", False)
        use(state, fake_gemini())

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "gemini", media_backends.ImageRequest(prompt="城")))

        assert e.value.status_code == 403
        assert "無効" in e.value.detail["error"]

    def test_stopping_the_answer_layer_stops_everything(self, state):
        """元栓を止めたら、自前の GPU も含めて全部止まる。"""
        settings_store.set_answer_enabled(False)
        use(state, fake_comfy())

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "comfyui", media_backends.ImageRequest(prompt="猫")))

        assert e.value.status_code == 403
        assert "答える" in e.value.detail["error"]

    def test_mcp_tools_are_not_offered_when_stopped(self, state):
        """使えない道具をコンテナに並べない(notes と同じ扱い)。"""
        assert media.tools_enabled() is True

        settings_store.set_answer_enabled(False)

        assert media.tools_enabled() is False


class TestServing:
    def test_paths_outside_the_media_dir_are_not_served(self, state):
        """`../` を踏ませない。"""
        media.require_dir()

        with pytest.raises(HTTPException) as e:
            media.resolve("../state/settings.db")

        assert e.value.status_code == 404

    def test_old_days_are_cleaned_up(self, state):
        """1 枚 1〜2MB あり、放っておくと際限なく溜まる。"""
        root = media.require_dir()
        old = root / "20200101"
        old.mkdir(parents=True)
        (old / "a.png").write_bytes(PNG)
        new = root / "29991231"
        new.mkdir(parents=True)
        (new / "b.png").write_bytes(PNG)

        removed = media.cleanup(keep_days=14)

        assert removed == 1
        assert not old.exists()
        assert new.exists()


class TestBackendList:
    def test_reports_why_a_backend_is_not_usable(self, state):
        """使えない相手も理由つきで出す(出さないと選べない理由が分からない)。"""
        use(state, lambda request: httpx.Response(500))

        backends = {b["id"]: b for b in asyncio.run(media.backends())}

        assert backends["comfyui"]["usable"] is False
        assert "繋がらない" in backends["comfyui"]["reason"]
        # 自前の GPU は自分の on/off を持つ(画面がボタンを出す)
        assert backends["comfyui"]["owns_toggle"] is True
        assert backends["gemini"]["usable"] is False
        # 「話す相手」で有効にしていないので、鍵より先にそちらが理由になる
        assert "無効" in backends["gemini"]["reason"]

    def test_lists_the_checkpoints_comfyui_has(self, state):
        use(state, fake_comfy())

        backends = {b["id"]: b for b in asyncio.run(media.backends())}

        assert backends["comfyui"]["usable"] is True
        assert backends["comfyui"]["models"] == ["sdxl.safetensors", "sd15.ckpt"]
