"""絵と音の生成(/v1/media/*・MCP の image_* / audio_*)のテスト。

**知識を引くのとは別の仕事**だが、口は Chiezo にまとめてある(MCP の登録先を増やさない
ため)。ここで確かめるのは、相手ごとの呼び方・出来たものの受け渡し・置き場の守り。

外部にも GPU にも出ないよう、`media_backends._client` を差し替えて偽のサーバーを演じさせる。
"""
import asyncio
import base64
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from app import media, media_backends, media_providers, settings_store

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 32
MP3 = b"ID3" + b"0" * 64


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


def fake_comfy(checkpoints=("sdxl.safetensors", "sd15.ckpt")):
    """ComfyUI を演じる。/object_info → /prompt → /history → /view の順に応じる。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/object_info/CheckpointLoaderSimple"):
            return httpx.Response(200, json={
                "CheckpointLoaderSimple": {
                    "input": {"required": {"ckpt_name": [list(checkpoints)]}}
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
        assert "AI の相手" in e.value.detail["hint"]


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
        assert "AI の相手" in e.value.detail["hint"]

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
        job = media.create_job("猫", backend="comfyui", size="1024x1024")
        asyncio.run(media._run(
            job["id"], "comfyui", media_backends.ImageRequest(prompt="猫", size="1024x1024"), 1))

        done = media.get_job(job["id"])
        assert done["state"] == "done"
        file = done["files"][0]
        assert file["url"].startswith("/media/")
        assert media.resolve(file["url"].removeprefix("/media/")).read_bytes() == PNG

    def test_jpeg_is_saved_as_jpg(self, state):
        """**Gemini は JPEG しか返さない。** png 決め打ちで書くと、名前と中身が食い違う。"""
        async def one(*a, **k):
            return media_backends.GeneratedImage(PNG, "image/jpeg", 1, "m")

        with patch.object(media_backends, "generate", one):
            job = media.create_job("猫", backend="gemini")
            asyncio.run(media._run(
                job["id"], "gemini", media_backends.ImageRequest(prompt="猫"), 1))

        assert media.get_job(job["id"])["files"][0]["url"].endswith(".jpg")

    def test_cancelled_job_does_not_stay_running(self, state):
        """**中断は Exception ではない。** ここを書き残さないと、絵は描き上がっているのに
        image_status が running を返し続ける(実際に MCP の接続が切れて起きた)。"""
        async def scenario():
            async def forever(*a, **k):
                await asyncio.sleep(3600)

            with patch.object(media_backends, "generate", forever):
                job = media.create_job("猫", backend="comfyui")
                task = asyncio.create_task(
                    media._run(job["id"], "comfyui", media_backends.ImageRequest(prompt="猫"), 1))
                await asyncio.sleep(0)          # running まで進める
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                return job["id"]

        job_id = asyncio.run(scenario())
        done = media.get_job(job_id)
        assert done["state"] == "failed"
        assert "中断" in done["error"]

    def test_stale_running_job_is_reaped(self, state):
        """ワーカーごと落ちると `_run` の後始末すら通らない。
        **running のまま残った job は、読み出すときに畳む。**"""
        job = media.create_job("猫", backend="comfyui")
        media._update(job["id"], state="running")
        old = (datetime.now(UTC) - timedelta(seconds=media.STALE_AFTER + 10)).isoformat()
        with media._connect() as conn:
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old, job["id"]))

        assert media.get_job(job["id"])["state"] == "failed"

    def test_running_job_is_left_alone_until_it_goes_quiet(self, state):
        """**動いている job を畳まない。** 生成は数分かかることがある。"""
        job = media.create_job("猫", backend="comfyui")
        media._update(job["id"], state="running")

        assert media.get_job(job["id"])["state"] == "running"

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

    def test_size_the_model_cannot_draw_is_rejected(self, state):
        """**崩れた絵は「成功」として返ってくる。** ComfyUI は頼まれた画素で潜在空間を
        作るので、SDXL に 512 を頼むと意味を成さない絵が done で返る —— 見るまで
        気づけないぶん、書き方の間違いより性質が悪い。"""
        with pytest.raises(HTTPException) as e:
            media.create_job("猫", backend="comfyui", size="512x512")

        assert e.value.status_code == 400
        assert "1024x1024" in e.value.detail["sizes"]

    def test_other_backends_still_take_any_size(self, state):
        """外部サービスは自分の語彙へ丸めてくれる(openai は近いものを選ぶ)ので、
        こちら側で狭めない。"""
        job = media.create_job("猫", backend="openai", size="1280x720")

        assert job["size"] == "1280x720"


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

    def test_both_the_image_and_the_audio_tools_are_registered(self, state):
        """**道具の定義は常時コンテキストに載る**ので、出す・出さないは 1 か所で決める。
        絵だけ出て音が出ない、という取りこぼしをここで止める。"""
        from fastapi import FastAPI

        from app import mcp_server

        app = FastAPI()
        app.state.sources = {}
        names = {t.name for t in asyncio.run(mcp_server.build_mcp(app).list_tools())}

        assert {"image_generate", "image_status", "image_backends"} <= names
        assert {"audio_generate", "audio_status", "audio_backends"} <= names


class TestServing:
    @pytest.mark.parametrize("path", [
        "../state/settings.db",          # 上へ抜ける
        "20260814/../../state/x.db",     # 途中で抜ける
        "/etc/passwd",                   # 絶対パス(連結すると置き場を無視する)
        "20260814/.hidden.png",          # 隠しファイル
        "20260814",                      # 日付だけ(ディレクトリ)
        "20260814/a/b.png",              # 深すぎる
        "2026081/a.png",                 # 日付の桁が足りない
    ])
    def test_paths_outside_the_media_dir_are_not_served(self, state, path):
        """**形で弾く。** 置き場は `<日付 8 桁>/<ファイル名>` の 2 段しかないので、
        そこから外れたものはパスを組み立てる前に断る。"""
        media.require_dir()

        with pytest.raises(HTTPException) as e:
            media.resolve(path)

        assert e.value.status_code == 404

    def test_a_symlink_pointing_outside_is_not_followed(self, state):
        """置き場の中に外を指すリンクが混ざっても外へ出さない
        (書くのは chiezo だけだが、形の検査だけでは防げない)。"""
        root = media.require_dir()
        secret = root.parent / "settings.db"
        secret.write_bytes(b"secret")
        day = root / "20260814"
        day.mkdir(parents=True)
        (day / "leak.png").symlink_to(secret)

        with pytest.raises(HTTPException) as e:
            media.resolve("20260814/leak.png")

        assert e.value.status_code == 404

    def test_a_normal_file_is_still_served(self, state):
        """弾きすぎない(実際に `_save` が書く形は通る)。"""
        day = media.require_dir() / "20260814"
        day.mkdir(parents=True)
        (day / "abc123-0.png").write_bytes(PNG)

        assert media.resolve("20260814/abc123-0.png").read_bytes() == PNG

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

    def test_audio_checkpoints_are_kept_out_of_the_image_list(self, state):
        """**置き場が同じなので混ざる。** 混ざったまま先頭を既定にすると、モデルを
        指定しなかった絵の生成が音のモデルを掴む(`ace_step_…` は `sd_xl_…` より前)。"""
        use(state, fake_comfy_audio(
            checkpoints=("ace_step_v1_3.5b.safetensors", "sd_xl_base_1.0.safetensors")))

        image = {b["id"]: b for b in asyncio.run(media.backends("image"))}

        assert image["comfyui"]["models"] == ["sd_xl_base_1.0.safetensors"]

    def test_the_default_image_model_is_never_an_audio_one(self, state):
        use(state, fake_comfy(
            checkpoints=("ace_step_v1_3.5b.safetensors", "sd_xl_base_1.0.safetensors")))

        asyncio.run(media_backends.generate("comfyui", media_backends.ImageRequest(prompt="猫")))

        graph = fake_comfy.sent["prompt"]
        assert graph["1"]["inputs"]["ckpt_name"] == "sd_xl_base_1.0.safetensors"


# ---- 音 ---------------------------------------------------------------------
#
# 絵と同じ層をそのまま使うので、ここで確かめるのは**音でだけ違うところ** ——
# チェックポイントの選び分け・グラフの形・相手ごとの口・長さの扱い。


def fake_comfy_audio(checkpoints=("stable-audio-open-1.0.safetensors", "ace_step_v1_3.5b.safetensors"),
                     encoders=("t5-base.safetensors",)):
    """音を作る ComfyUI を演じる。絵と違うのは CLIPLoader と出力の入れ物の名前。"""
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/object_info/CheckpointLoaderSimple"):
            return httpx.Response(200, json={
                "CheckpointLoaderSimple": {
                    "input": {"required": {"ckpt_name": [list(checkpoints)]}}
                }
            })
        if path.endswith("/object_info/CLIPLoader"):
            return httpx.Response(200, json={
                "CLIPLoader": {"input": {"required": {"clip_name": [list(encoders)]}}}
            })
        if path.endswith("/prompt"):
            fake_comfy_audio.sent = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": "p1"})
        if "/history/" in path:
            return httpx.Response(200, json={
                "p1": {"outputs": {"8": {"audio": [{"filename": "a.mp3", "type": "output"}]}}}
            })
        if path.endswith("/view"):
            return httpx.Response(200, content=MP3)
        return httpx.Response(404)

    return handler


def fake_elevenlabs(status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        fake_elevenlabs.path = request.url.path
        fake_elevenlabs.sent = json.loads(request.content)
        fake_elevenlabs.key = request.headers.get("xi-api-key")
        if status >= 400:
            return httpx.Response(status, json={"detail": "boom"})
        return httpx.Response(200, content=MP3)

    return handler


class TestComfyUIAudio:
    def test_music_uses_the_ace_step_checkpoint(self, state):
        """**曲は ACE-Step 優先。** 系統でグラフそのものが変わる(歌詞を渡す口がある)。"""
        use(state, fake_comfy_audio())

        audio = asyncio.run(media_backends.generate_audio(
            "comfyui", media_backends.AudioRequest(prompt="祭囃子", sound="music", seconds=20, seed=3)))

        assert audio.data == MP3
        assert audio.model == "ace_step_v1_3.5b.safetensors"
        graph = fake_comfy_audio.sent["prompt"]
        assert graph["2"]["class_type"] == "TextEncodeAceStepAudio"
        assert graph["2"]["inputs"]["tags"] == "祭囃子"
        # 歌詞を渡さなければ器楽として頼む(BGM に歌が乗ると台詞と喧嘩する)
        assert graph["2"]["inputs"]["lyrics"] == "[instrumental]"
        assert graph["4"]["inputs"]["seconds"] == 20

    def test_sfx_uses_stable_audio_and_loads_the_text_encoder(self, state):
        """**効果音は Stable Audio Open 側。** text encoder を別に読む必要がある。"""
        use(state, fake_comfy_audio())

        audio = asyncio.run(media_backends.generate_audio(
            "comfyui", media_backends.AudioRequest(prompt="剣がぶつかる音", seed=5)))

        assert audio.model == "stable-audio-open-1.0.safetensors"
        graph = fake_comfy_audio.sent["prompt"]
        assert graph["2"]["inputs"] == {"clip_name": "t5-base.safetensors", "type": "stable_audio"}
        # 頼まなければ既定の長さ(効果音は短く)
        assert graph["5"]["inputs"]["seconds"] == media_backends.DEFAULT_SECONDS["sfx"]

    def test_missing_text_encoder_is_refused_before_the_gpu_spins(self, state):
        """ComfyUI 側のエラーは「clip_name が不正」としか出ないので、手前で断る。"""
        use(state, fake_comfy_audio(encoders=()))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_audio(
                "comfyui", media_backends.AudioRequest(prompt="剣")))

        assert "text encoder" in e.value.detail["error"]

    def test_says_what_to_place_when_no_audio_checkpoint_exists(self, state):
        """絵のチェックポイントしか無いのはよくある(置き場が同じ)。"""
        use(state, fake_comfy_audio(checkpoints=("sd_xl_base_1.0.safetensors",)))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_audio(
                "comfyui", media_backends.AudioRequest(prompt="剣")))

        assert "音のチェックポイント" in e.value.detail["error"]
        assert "stable-audio" in e.value.detail["hint"]


class TestGeminiAudio:
    def test_music_goes_through_the_same_interactions_endpoint(self, state):
        """**絵と同じ口。** 違うのは response_format だけ(鍵も「話す相手」と共通)。"""
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", True)
        use(state, fake_gemini(body={"steps": [{"content": [
            {"type": "audio", "data": base64.b64encode(MP3).decode(), "mime_type": "audio/mpeg"}
        ]}]}))

        audio = asyncio.run(media_backends.generate_audio(
            "gemini", media_backends.AudioRequest(prompt="森のBGM", sound="music")))

        assert audio.data == MP3
        assert fake_gemini.key == "AIza-test"
        assert fake_gemini.sent["response_format"] == {"type": "audio"}
        assert fake_gemini.sent["model"] == "lyria-3-clip-preview"

    def test_sfx_is_refused_because_lyria_only_makes_music(self, state):
        """短い衝突音を頼んで 30 秒の曲が返るほうが、呼んだ側には分かりにくい。"""
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", True)
        use(state, fake_gemini())

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_audio(
                "gemini", media_backends.AudioRequest(prompt="剣", sound="sfx")))

        assert e.value.status_code == 400
        assert e.value.detail["sounds"] == ["music"]


class TestElevenLabs:
    def test_sfx_and_music_go_to_different_endpoints(self, state):
        """**口が別。** 効果音は /sound-generation、曲は /music。"""
        settings_store.set_credential("elevenlabs", "xi-test")
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs())

        asyncio.run(media_backends.generate_audio(
            "elevenlabs", media_backends.AudioRequest(prompt="爆発", seconds=3, loop=True)))
        assert fake_elevenlabs.path.endswith("/sound-generation")
        assert fake_elevenlabs.sent["duration_seconds"] == 3
        assert fake_elevenlabs.sent["loop"] is True
        assert fake_elevenlabs.key == "xi-test"

        asyncio.run(media_backends.generate_audio(
            "elevenlabs", media_backends.AudioRequest(prompt="行進曲", sound="music", seconds=45)))
        assert fake_elevenlabs.path.endswith("/music")
        # 相手の語彙はミリ秒。歌詞を渡していないので器楽で頼む
        assert fake_elevenlabs.sent["music_length_ms"] == 45000
        assert fake_elevenlabs.sent["force_instrumental"] is True

    def test_missing_key_is_reported_before_anything_is_sent(self, state):
        """**この相手だけ鍵を自分で持つ**(会話ができないので借り先が無い)。"""
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs())

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_audio(
                "elevenlabs", media_backends.AudioRequest(prompt="爆発")))

        assert e.value.status_code == 401
        assert "AI の相手" in e.value.detail["hint"]


class TestAudioJobs:
    def test_saves_mp3_and_records_how_long_it_is(self, state):
        use(state, fake_comfy_audio())
        job = media.create_job("剣", backend="comfyui", kind="audio", sound="sfx", seconds=4)
        asyncio.run(media._run(
            job["id"], "comfyui",
            media_backends.AudioRequest(prompt="剣", sound="sfx", seconds=4), 1))

        done = media.get_job(job["id"])
        assert done["kind"] == "audio"
        assert done["files"][0]["url"].endswith(".mp3")
        assert done["files"][0]["seconds"] == 4
        assert media.resolve(done["files"][0]["url"].removeprefix("/media/")).read_bytes() == MP3

    def test_too_long_is_refused_instead_of_silently_shortened(self, state):
        """**黙って丸めない。** 短くして返すと、頼んだ尺で出来たと思われる。"""
        with pytest.raises(HTTPException) as e:
            media.create_job("行進曲", backend="comfyui", kind="audio", sound="music", seconds=600)

        assert e.value.status_code == 400
        assert "240 秒" in e.value.detail["error"]

    def test_length_cannot_be_asked_of_a_backend_that_has_no_such_knob(self, state):
        """Lyria は尺がモデルで決まる。黙って無視すると、頼んだ長さで出来たと思われる。"""
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", True)

        with pytest.raises(HTTPException) as e:
            media.create_job("BGM", backend="gemini", kind="audio", sound="music", seconds=30)

        assert "長さを指定できません" in e.value.detail["error"]

    def test_the_default_backend_for_audio_is_the_local_gpu(self, state):
        """外へ出さず、枠も食わないほうを既定にする(絵と同じ考え方)。"""
        job = media.create_job("剣", kind="audio", sound="sfx")

        assert job["backend"] == "comfyui"
        assert job["sound"] == "sfx"


class TestAudioBackendList:
    def test_only_lists_backends_that_can_make_sound(self, state):
        """混ぜると、頼めない相手が並んで見える(ElevenLabs に絵は描けない)。"""
        use(state, fake_comfy_audio())

        audio = {b["id"]: b for b in asyncio.run(media.backends("audio"))}
        image = {b["id"]: b for b in asyncio.run(media.backends("image"))}

        assert set(audio) == {"comfyui", "gemini", "elevenlabs"}
        assert "elevenlabs" not in image
        assert "openai" in image

    def test_says_what_each_backend_accepts(self, state):
        """**頼める種類と長さの上限は相手ごとに違う。** 0 は「指定できない」。"""
        use(state, fake_comfy_audio())

        audio = {b["id"]: b for b in asyncio.run(media.backends("audio"))}

        assert audio["comfyui"]["sounds"] == {"sfx": 47.0, "music": 240.0}
        assert audio["gemini"]["sounds"] == {"music": 0.0}
        # 音のチェックポイントだけが並ぶ(絵のものは混ざらない)
        assert audio["comfyui"]["models"] == [
            "ace_step_v1_3.5b.safetensors", "stable-audio-open-1.0.safetensors"
        ]

    def test_reports_a_missing_audio_checkpoint_separately(self, state):
        """絵は描けるのに音は作れない、はよくある(置き場が同じで別のファイルが要る)。"""
        use(state, fake_comfy_audio(checkpoints=("sd_xl_base_1.0.safetensors",)))

        audio = {b["id"]: b for b in asyncio.run(media.backends("audio"))}
        image = {b["id"]: b for b in asyncio.run(media.backends("image"))}

        assert image["comfyui"]["usable"] is True
        assert audio["comfyui"]["usable"] is False
        assert "音のチェックポイント" in audio["comfyui"]["reason"]


class TestOwnCredential:
    """**ElevenLabs だけ鍵を自分で持つ**(「話す相手」に対応が無く、借り先がない)。"""

    def test_a_key_can_be_registered_and_removed_from_the_media_section(self, state):
        """登録しかできないと、間違えて入れた鍵を画面から外せなくなる。"""
        settings_store.set_credential("elevenlabs", "xi-test")
        assert media_backends.credential_of(media_providers.get("elevenlabs")) == "xi-test"

        settings_store.clear_credential("elevenlabs")

        spec = media_providers.get("elevenlabs")
        assert media_backends.credential_of(spec) == ""
        # 鍵を消したら同時に無効になる(鍵の無い相手を有効のまま残さない)
        assert settings_store.load("elevenlabs").enabled is False
        assert media_backends.unusable_reason(spec) != ""

    def test_the_key_is_not_borrowed_from_a_chat_provider(self, state):
        """借り先を持たない相手なので、他の鍵に引きずられない。"""
        settings_store.set_credential("gemini", "AIza-test")

        assert media_backends.credential_of(media_providers.get("elevenlabs")) == ""


class TestCapabilities:
    """**頼めることの分類は 1 か所に持つ**（`app/capabilities.py`）。

    会話は `providers.py`、絵と音は `media_providers.py` と持ち主が分かれているので、
    分類まで散らすと「何が頼めるのか」を数える場所が無くなる。
    """

    def test_music_and_sfx_are_counted_separately(self, state):
        """job の kind はどちらも audio だが、**相手もモデルも別物**
        （Lyria は曲しか作れない）。分類は仕事の単位で切る。"""
        from app import capabilities

        assert capabilities.of_provider("gemini") == {
            capabilities.CHAT, capabilities.IMAGE, capabilities.MUSIC
        }
        assert capabilities.of_provider("elevenlabs") == {
            capabilities.MUSIC, capabilities.SFX
        }
        assert capabilities.of_provider("comfyui") == {
            capabilities.IMAGE, capabilities.MUSIC, capabilities.SFX
        }

    def test_a_chat_only_provider_has_only_chat(self, state):
        from app import capabilities

        assert capabilities.of_provider("openrouter") == {capabilities.CHAT}

    def test_unimplemented_kinds_are_still_listed(self, state):
        """**表から消さない。** 消すと「頼めるのか分からない」になり、
        聞かれるたびにコードを読み直すことになる。"""
        from app import capabilities

        items = {c["id"]: c for c in capabilities.overview({})}

        assert len(items) == 6
        assert items["voice"]["supported"] is False
        assert items["video"]["supported"] is False
        assert items["voice"]["state"] == "未対応"

    def test_it_separates_not_implemented_from_nobody_available(self, state):
        """**次にすることが違う。** 作れば直るのか、鍵を入れれば直るのか。"""
        from app import capabilities

        items = {c["id"]: c for c in capabilities.overview({"gemini": {capabilities.IMAGE}})}

        assert items["image"]["state"] == "使える"
        assert items["image"]["providers"] == ["gemini"]
        assert items["music"]["state"] == "相手がいない"
        assert items["video"]["state"] == "未対応"
