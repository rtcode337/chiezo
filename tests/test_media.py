"""絵と音の生成(/v1/media/*・MCP の image_* / audio_*)のテスト。

知識を引くのとは別の仕事だが、口は Chiezo にまとめてある(MCP の登録先を増やさない
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

    自前の GPU は既定で無効(話す相手と同じで、明示的に on にする)。
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
        """置いてあるモデルは環境ごとに違うので、相手に聞く。"""
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
        """鍵を 2 か所に持たない —— 「話す相手」に登録済みのものを流用する。"""
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
        """相手は決まった組み合わせしか取らないので、近いものへ寄せる。"""
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


class TestAntigravity:
    """Codex と同じブリッジの口（`/v1/images/generations`）で描く。

    鍵はこちらに無く、コンテナ内のサインイン結果をブリッジが使う。
    """

    def test_it_goes_through_its_own_bridge(self, state):
        settings_store.set_enabled("antigravity", True)

        def handler(request: httpx.Request) -> httpx.Response:
            handler.url = str(request.url)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(PNG).decode()}]})

        use(state, handler)

        image = asyncio.run(media_backends.generate(
            "antigravity", media_backends.ImageRequest(prompt="りんご", size="1024x1024")))

        assert image.data == PNG
        assert image.model == "antigravity-imagegen"
        # 鍵は要らない（サインイン結果をブリッジが使う）ので、自分のブリッジへ行く
        assert "chiezo-bridge-antigravity" in handler.url

    def test_it_follows_the_toggle_of_the_row_it_shares(self, state):
        """サインインは 1 つ。 「話す相手」として止めたら、絵も止まる。"""
        settings_store.set_enabled("antigravity", False)
        use(state, lambda request: httpx.Response(200, json={"data": []}))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate(
                "antigravity", media_backends.ImageRequest(prompt="りんご")))

        assert e.value.status_code == 403

    def test_it_cannot_make_sound(self, state):
        """内蔵ツールに音は無い（バイナリを見ても入力側の語しか無い）。"""
        from app import capabilities

        assert capabilities.of_provider("antigravity") == {
            capabilities.CHAT, capabilities.IMAGE
        }


class TestCodex:
    """ChatGPT のサブスク枠で gpt-image-2 を使う経路(API の従量課金とは別勘定)。"""

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
        """画像そのものは返さない(1 枚 1〜2MB でコンテキストが飛ぶ)。"""
        use(state, fake_comfy())
        job = media.create_job("猫", backend="comfyui", size="1024x1024")
        asyncio.run(media._run(
            job["id"], "comfyui", media_backends.ImageRequest(prompt="猫", size="1024x1024"), 1,
            media_providers.KIND_IMAGE))

        done = media.get_job(job["id"])
        assert done["state"] == "done"
        file = done["files"][0]
        assert file["url"].startswith("/media/")
        assert media.resolve(file["url"].removeprefix("/media/")).read_bytes() == PNG

    def test_jpeg_is_saved_as_jpg(self, state):
        """Gemini は JPEG しか返さない。 png 決め打ちで書くと、名前と中身が食い違う。"""
        async def one(*a, **k):
            return media_backends.GeneratedImage(PNG, "image/jpeg", 1, "m")

        with patch.object(media_backends, "generate", one):
            job = media.create_job("猫", backend="gemini")
            asyncio.run(media._run(
                job["id"], "gemini", media_backends.ImageRequest(prompt="猫"), 1,
                media_providers.KIND_IMAGE))

        assert media.get_job(job["id"])["files"][0]["url"].endswith(".jpg")

    def test_cancelled_job_does_not_stay_running(self, state):
        """中断は Exception ではない。 ここを書き残さないと、絵は描き上がっているのに
        image_status が running を返し続ける(実際に MCP の接続が切れて起きた)。"""
        async def scenario():
            async def forever(*a, **k):
                await asyncio.sleep(3600)

            with patch.object(media_backends, "generate", forever):
                job = media.create_job("猫", backend="comfyui")
                task = asyncio.create_task(
                    media._run(job["id"], "comfyui", media_backends.ImageRequest(prompt="猫"), 1,
                       media_providers.KIND_IMAGE))
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
        running のまま残った job は、読み出すときに畳む。"""
        job = media.create_job("猫", backend="comfyui")
        media._update(job["id"], state="running")
        old = (datetime.now(UTC) - timedelta(seconds=media.STALE_AFTER + 10)).isoformat()
        with media._connect() as conn:
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (old, job["id"]))

        assert media.get_job(job["id"])["state"] == "failed"

    def test_running_job_is_left_alone_until_it_goes_quiet(self, state):
        """動いている job を畳まない。 生成は数分かかることがある。"""
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
        asyncio.run(media._run(job["id"], "comfyui", media_backends.ImageRequest(prompt="猫"), 3,
                               media_providers.KIND_IMAGE))

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
        """崩れた絵は「成功」として返ってくる。 ComfyUI は頼まれた画素で潜在空間を
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
        """道具の定義は常時コンテキストに載るので、出す・出さないは 1 か所で決める。
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
        """形で弾く。 置き場は `<日付 8 桁>/<ファイル名>` の 2 段しかないので、
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
        """置き場が同じなので混ざる。 混ざったまま先頭を既定にすると、モデルを
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
# 絵と同じ層をそのまま使うので、ここで確かめるのは音でだけ違うところ ——
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
        """曲は ACE-Step 優先。 系統でグラフそのものが変わる(歌詞を渡す口がある)。"""
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
        """効果音は Stable Audio Open 側。 text encoder を別に読む必要がある。"""
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
        """絵と同じ口。 違うのは response_format だけ(鍵も「話す相手」と共通)。"""
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
        """口が別。 効果音は /sound-generation、曲は /music。"""
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
        """この相手だけ鍵を自分で持つ(会話ができないので借り先が無い)。"""
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
            media_backends.AudioRequest(prompt="剣", sound="sfx", seconds=4), 1,
            media_providers.KIND_AUDIO))

        done = media.get_job(job["id"])
        assert done["kind"] == "audio"
        assert done["files"][0]["url"].endswith(".mp3")
        assert done["files"][0]["seconds"] == 4
        assert media.resolve(done["files"][0]["url"].removeprefix("/media/")).read_bytes() == MP3

    def test_too_long_is_refused_instead_of_silently_shortened(self, state):
        """黙って丸めない。 短くして返すと、頼んだ尺で出来たと思われる。"""
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

    def test_the_default_backend_for_audio_is_the_one_that_sounds_best(self, state):
        """既定は「頼む順」の先頭(`media_providers.PREFERENCE`)。

        かつては自前の GPU(外へ出さず枠も食わない)を既定にしていたが、
        出来が違う —— 相手を名指ししない呼び出し(MCP の `audio_generate` など)が
        いちばん多いので、そこが良い相手へ行くようにする。ComfyUI は名指しすれば使える。
        """
        job = media.create_job("剣", kind="audio", sound="sfx")

        assert job["backend"] == "elevenlabs"
        assert job["sound"] == "sfx"


class TestPreferenceTable:
    """どれに頼むのがよいかの表(`media_providers.PREFERENCE`)。"""

    def test_every_backend_is_ranked_for_every_kind_it_can_do(self):
        """相手を足したら表にも足す。 抜けた相手は黙って最後尾に回るので、
        「新しく足した相手にいつまでも頼まれない」が起きても気づけない。"""
        from app import media_providers

        missing = {
            (kind, spec.id)
            for kind, ranked in media_providers.PREFERENCE.items()
            for spec in media_providers.PROVIDERS
            if kind in spec.kinds and spec.id not in ranked
        }

        assert not missing, f"頼む順の表に無い相手がいる: {sorted(missing)}"

    def test_the_table_only_names_backends_that_can_do_that_kind(self):
        """作れない相手を順位に入れない(直した気になって効かない)。"""
        from app import media_providers

        wrong = {
            (kind, pid)
            for kind, ranked in media_providers.PREFERENCE.items()
            for pid in ranked
            if (spec := media_providers.get(pid)) is None or kind not in spec.kinds
        }

        assert not wrong, f"その種類を作れない相手が順位に入っている: {sorted(wrong)}"

    def test_the_settings_table_keeps_its_own_order(self):
        """画面の並びは変えない。 設定を探すための並びで、用が違う
        (頼む順で並べ替えると、いつも同じ場所にあった行が動く)。"""
        from app import media_providers

        assert next(p.id for p in media_providers.all_providers()) == "comfyui"
        assert next(p.id for p in media_providers.all_providers("image")) == "codex"


class TestAudioBackendList:
    def test_only_lists_backends_that_can_make_sound(self, state):
        """混ぜると、頼めない相手が並んで見える(ElevenLabs に絵は描けない)。"""
        use(state, fake_comfy_audio())

        audio = {b["id"]: b for b in asyncio.run(media.backends("audio"))}
        image = {b["id"]: b for b in asyncio.run(media.backends("image"))}

        assert set(audio) == {"comfyui", "gemini", "elevenlabs"}
        # CLI ブリッジの相手は絵しか描けない(内蔵ツールに音が無い)
        assert "codex" not in audio and "antigravity" not in audio
        assert "codex" in image
        assert "openai" in image

    def test_says_what_each_backend_accepts(self, state):
        """頼める種類と長さの上限は相手ごとに違う。 0 は「指定できない」。"""
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
    """ElevenLabs だけ鍵を自分で持つ(「話す相手」に対応が無く、借り先がない)。"""

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
    """頼めることの分類は 1 か所に持つ（`app/capabilities.py`）。

    会話は `providers.py`、絵と音は `media_providers.py` と持ち主が分かれているので、
    分類まで散らすと「何が頼めるのか」を数える場所が無くなる。
    """

    def test_music_and_sfx_are_counted_separately(self, state):
        """job の kind はどちらも audio だが、相手もモデルも別物
        （Lyria は曲しか作れない）。分類は仕事の単位で切る。"""
        from app import capabilities

        assert capabilities.of_provider("gemini") == {
            capabilities.CHAT, capabilities.IMAGE, capabilities.MUSIC,
            capabilities.VIDEO, capabilities.SPEECH, capabilities.TRANSCRIBE,
        }
        # 話す相手としては出てこないが、それ以外はほぼ全部受け持つ
        assert capabilities.of_provider("elevenlabs") == {
            capabilities.MUSIC, capabilities.SFX, capabilities.IMAGE,
            capabilities.VIDEO, capabilities.SPEECH, capabilities.TRANSCRIBE,
        }
        # 自前の GPU に読み上げは無い(ComfyUI 本体に TTS のノードが無いため)
        assert capabilities.of_provider("comfyui") == {
            capabilities.IMAGE, capabilities.MUSIC, capabilities.SFX, capabilities.VIDEO
        }

    def test_a_chat_only_provider_has_only_chat(self, state):
        from app import capabilities

        assert capabilities.of_provider("openrouter") == {capabilities.CHAT}

    def test_every_kind_is_listed(self, state):
        """表から消さない。 消すと「頼めるのか分からない」になり、
        聞かれるたびにコードを読み直すことになる。

        読み上げと文字起こしは別に数える —— 同じ「声」でも仕事の向きが逆で、
        まとめると「読み上げはできるが文字起こしはできない」相手を
        「声が使える」と言うことになる。
        """
        from app import capabilities

        items = {c["id"]: c for c in capabilities.overview({})}

        assert list(items) == ["chat", "speech", "transcribe", "image", "video", "music", "sfx"]
        assert all(item["supported"] for item in items.values())

    def test_it_separates_not_implemented_from_nobody_available(self, state):
        """次にすることが違う。 作れば直るのか、鍵を入れれば直るのか。"""
        from app import capabilities

        items = {c["id"]: c for c in capabilities.overview({"gemini": {capabilities.IMAGE}})}

        assert items["image"]["state"] == "使える"
        assert items["image"]["providers"] == ["gemini"]
        assert items["music"]["state"] == "相手がいない"


class TestRemoteErrors:
    """相手のエラーは、次の一手が分かる形で返す。

    429 のとき「使い切った」のか「そもそも枠が無い」のかが分からず、2 度調べ直した。
    """

    def test_the_body_is_not_cut_before_the_reason(self, state):
        """どの枠が尽きたかは前置きの後ろに来る。300 字で切ると必ず落ちていた。"""
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", True)
        body = ("You exceeded your current quota. " + "x" * 200
                + "\n* Quota exceeded for metric: lyria, limit: 0")
        use(state, lambda request: httpx.Response(429, text=body))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_audio(
                "gemini", media_backends.AudioRequest(prompt="曲", sound="music")))

        assert "limit: 0" in e.value.detail["detail"]
        assert "無料枠" in e.value.detail["hint"]

    def test_it_does_not_hint_about_quota_for_other_failures(self, state):
        settings_store.set_credential("gemini", "AIza-test")
        settings_store.set_enabled("gemini", True)
        use(state, lambda request: httpx.Response(500, text="boom"))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_audio(
                "gemini", media_backends.AudioRequest(prompt="曲", sound="music")))

        assert "hint" not in e.value.detail


# ---- 動画 -------------------------------------------------------------------
#
# 絵と音で効いた検査をそのまま持ってくるだけでは足りない。 動画は
# 「待ち時間が桁で違う」「受け付ける尺が飛び飛び」「1 本が重い」の 3 つが加わり、
# どれも間違えても生成そのものは成功して返る(尺だけ違う動画が返る、
# 途中で job を畳んで取りに行けなくなる)ので、テストで押さえておく。

MP4 = b"\x00\x00\x00 ftypisom" + b"0" * 64


def fake_comfy_video(unets=("wan2.2_t2v_14B.safetensors", "flux1-dev.safetensors"),
                     clips=("umt5_xxl_fp8.safetensors",),
                     vaes=("wan_2.1_vae.safetensors",)):
    """動画を作れる ComfyUI を演じる。読むものが 3 つあるのがここの肝。"""
    lists = {
        "UNETLoader": ("unet_name", unets),
        "CLIPLoader": ("clip_name", clips),
        "VAELoader": ("vae_name", vaes),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        for node, (field, names) in lists.items():
            if path.endswith(f"/object_info/{node}"):
                return httpx.Response(200, json={
                    node: {"input": {"required": {field: [list(names)]}}}
                })
        if path.endswith("/prompt"):
            fake_comfy_video.sent = json.loads(request.content)
            return httpx.Response(200, json={"prompt_id": "p1"})
        if "/history/" in path:
            return httpx.Response(200, json={
                "p1": {"outputs": {"11": {"videos": [{"filename": "a.mp4", "type": "output"}]}}}
            })
        if path.endswith("/view"):
            return httpx.Response(200, content=MP4)
        return httpx.Response(404)

    return handler


class TestComfyUIVideo:
    def test_it_only_offers_models_that_can_make_video(self, state):
        """`models/diffusion_models` には絵の UNet も同居する。混ぜて先頭を掴むと、
        絵のモデルで動画を作ろうとして、読み込んで初めて失敗する。"""
        use(state, fake_comfy_video())

        names = asyncio.run(media_backends.comfy_video_models("http://x"))

        assert names == ["wan2.2_t2v_14B.safetensors"]

    def test_frames_are_rounded_to_the_shape_wan_accepts(self, state):
        """4n+1 でないと通らない。 秒数から素直に掛けると 32 フレームになり、
        グラフごと弾かれる。"""
        assert media_backends.comfy_video_length(2.0) == 33
        assert media_backends.comfy_video_length(3.0) == 49
        # 近いほうへ寄せる。 切り捨てると 2.0 秒が 29 フレーム(1.81 秒)になり、
        # 頼んだ尺より短いものが黙って返る
        assert media_backends.comfy_video_length(2.0) / 16 == pytest.approx(2.06, abs=0.01)
        assert [media_backends.comfy_video_length(s) % 4 for s in (2.0, 3.0, 5.0)] == [1, 1, 1]

    def test_it_loads_the_encoder_and_vae_the_model_needs(self, state):
        """絵と違って読むものが 3 つある。どれか 1 つでも取り違えると通らない。"""
        use(state, fake_comfy_video())

        video = asyncio.run(media_backends.generate_video(
            "comfyui",
            media_backends.VideoRequest(prompt="走る猫", size="848x480", seconds=2.0, seed=9),
        ))

        graph = fake_comfy_video.sent["prompt"]
        assert graph["1"]["inputs"]["unet_name"] == "wan2.2_t2v_14B.safetensors"
        assert graph["2"]["inputs"] == {"clip_name": "umt5_xxl_fp8.safetensors", "type": "wan"}
        assert graph["3"]["inputs"]["vae_name"] == "wan_2.1_vae.safetensors"
        assert graph["6"]["inputs"] == {"width": 848, "height": 480, "length": 33, "batch_size": 1}
        assert video.data == MP4
        assert video.mime == "video/mp4"

    def test_a_missing_encoder_is_refused_before_the_gpu_spins(self, state):
        """ComfyUI 側のエラーは「名前が不正」としか出ない。何を置けばよいかを
        こちらで言わないと、原因に辿り着けない。"""
        use(state, fake_comfy_video(clips=("clip_l.safetensors",)))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_video(
                "comfyui", media_backends.VideoRequest(prompt="猫", size="848x480")))

        assert "umt5" in e.value.detail["error"]
        assert "text_encoders" in e.value.detail["hint"]

    def test_it_says_what_to_place_when_no_video_model_exists(self, state):
        use(state, fake_comfy_video(unets=("flux1-dev.safetensors",)))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_video(
                "comfyui", media_backends.VideoRequest(prompt="猫", size="848x480")))

        assert "diffusion_models" in e.value.detail["hint"]

    def test_the_output_bucket_name_may_differ_between_versions(self, state):
        """出口の入れ物の名前は ComfyUI の版で変わる(videos / gifs / images)。
        決め打ちにすると、版が上がっただけで「返しませんでした」になる。"""
        handler = fake_comfy_video()

        def as_gifs(request: httpx.Request) -> httpx.Response:
            if "/history/" in request.url.path:
                return httpx.Response(200, json={
                    "p1": {"outputs": {"11": {"gifs": [{"filename": "a.mp4", "type": "output"}]}}}
                })
            return handler(request)

        use(state, as_gifs)

        video = asyncio.run(media_backends.generate_video(
            "comfyui", media_backends.VideoRequest(prompt="猫", size="848x480", seconds=2.0)))

        assert video.data == MP4


def fake_openai_video(status_sequence=("completed",)):
    """Sora を演じる。頼む → 覗く → 取りに行くの 3 手。"""
    seen = {"looks": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/videos") and request.method == "POST":
            fake_openai_video.sent = request.content
            return httpx.Response(200, json={"id": "vid_1", "status": "queued"})
        if path.endswith("/content"):
            return httpx.Response(200, content=MP4)
        if "/videos/" in path:
            index = min(seen["looks"], len(status_sequence) - 1)
            seen["looks"] += 1
            return httpx.Response(200, json={"id": "vid_1", "status": status_sequence[index]})
        return httpx.Response(404)

    fake_openai_video.looks = seen
    return handler


class TestOpenAIVideo:
    def test_it_asks_polls_then_downloads(self, state):
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)
        use(state, fake_openai_video())

        video = asyncio.run(media_backends.generate_video(
            "openai",
            media_backends.VideoRequest(prompt="海", size="1280x720", seconds=8.0, seed=3),
        ))

        assert video.data == MP4
        assert video.seconds == 8.0
        # multipart で送る(相手が JSON を取らない)
        assert b'name="prompt"' in fake_openai_video.sent

    def test_it_keeps_looking_until_the_video_is_ready(self, state):
        """1 回目で完成していることはまずない。待てることそのものを確かめる。"""
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)
        use(state, fake_openai_video(("queued", "in_progress", "completed")))
        state.setattr(media_backends.asyncio, "sleep", _no_wait)

        video = asyncio.run(media_backends.generate_video(
            "openai", media_backends.VideoRequest(prompt="海", size="1280x720", seconds=4.0)))

        assert video.data == MP4
        assert fake_openai_video.looks["looks"] == 3

    def test_a_failed_generation_is_reported_not_downloaded(self, state):
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)
        use(state, fake_openai_video(("failed",)))

        with pytest.raises(HTTPException) as e:
            asyncio.run(media_backends.generate_video(
                "openai", media_backends.VideoRequest(prompt="海", size="1280x720")))

        assert "失敗" in e.value.detail["error"]


async def _no_wait(_seconds):
    """待たずに次を覗く(テストを実時間で止めないため)。"""
    return None


def fake_gemini_video(veo=False):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(":predictLongRunning"):
            fake_gemini_video.sent = json.loads(request.content)
            return httpx.Response(200, json={"name": "models/veo/operations/1"})
        if path.endswith("/operations/1"):
            return httpx.Response(200, json={"done": True, "response": {
                "generateVideoResponse": {"generatedSamples": [
                    {"video": {"uri": "https://example.invalid/v.mp4"}}
                ]}
            }})
        if path.endswith("/v.mp4"):
            fake_gemini_video.download_key = request.headers.get("x-goog-api-key")
            return httpx.Response(200, content=MP4)
        if path.endswith("/interactions"):
            fake_gemini_video.sent = json.loads(request.content)
            return httpx.Response(200, json={"steps": [{"type": "model_output", "content": [
                {"type": "video", "mime_type": "video/mp4",
                 "data": base64.b64encode(MP4).decode()}
            ]}]})
        return httpx.Response(404)

    return handler


class TestGeminiVideo:
    def test_omni_goes_through_the_same_interactions_endpoint(self, state):
        """絵・曲と同じ口。違うのは response_format だけ。"""
        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)
        use(state, fake_gemini_video())

        video = asyncio.run(media_backends.generate_video(
            "gemini", media_backends.VideoRequest(prompt="花火", size="1280x720")))

        assert video.data == MP4
        assert fake_gemini_video.sent["response_format"]["type"] == "video"
        assert fake_gemini_video.sent["response_format"]["aspect_ratio"] == "16:9"

    def test_veo_takes_the_long_running_road_and_needs_the_key_to_download(self, state):
        """口が別(:predictLongRunning)で、出来た動画を取りに行くのにも鍵が要る
        —— 署名済みの URL ではないので、鍵を付け忘れると 403 で落ちる。"""
        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)
        use(state, fake_gemini_video())

        video = asyncio.run(media_backends.generate_video(
            "gemini",
            media_backends.VideoRequest(
                prompt="花火", model="veo-3.1-fast-generate-preview",
                size="1920x1080", seconds=6.0),
        ))

        assert video.data == MP4
        assert fake_gemini_video.download_key == "k"
        # 文字列で渡す(数値だと 400 になる)
        assert fake_gemini_video.sent["parameters"]["durationSeconds"] == "6"
        assert fake_gemini_video.sent["parameters"]["resolution"] == "1080p"


def fake_elevenlabs_flow(kind="video", status="completed"):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(f"/flows/{kind}") and request.method == "POST":
            fake_elevenlabs_flow.sent = json.loads(request.content)
            fake_elevenlabs_flow.key = request.headers.get("xi-api-key")
            return httpx.Response(200, json={"id": "g1", "status": "pending"})
        if path.endswith(f"/flows/{kind}/g1"):
            return httpx.Response(200, json={
                "id": "g1", "status": status,
                "content_url": "https://example.invalid/out.bin",
                "content_mime_type": "video/mp4" if kind == "video" else "image/png",
            })
        if path.endswith("/out.bin"):
            fake_elevenlabs_flow.download_key = request.headers.get("xi-api-key")
            return httpx.Response(200, content=MP4 if kind == "video" else PNG)
        return httpx.Response(404)

    return handler


class TestElevenLabsFlows:
    def test_video_is_created_then_fetched_from_the_signed_url(self, state):
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs_flow("video"))

        video = asyncio.run(media_backends.generate_video(
            "elevenlabs",
            media_backends.VideoRequest(prompt="波", size="1280x720", seconds=5.0),
        ))

        assert video.data == MP4
        assert fake_elevenlabs_flow.sent["duration_secs"] == 5
        assert fake_elevenlabs_flow.key == "el-k"
        # 署名済みの URL に鍵は載せない(外のホストへ出ていくことがある)
        assert fake_elevenlabs_flow.download_key is None

    def test_it_can_draw_now_that_the_service_hosts_image_models(self, state):
        """ここは事実が変わった。 「ElevenLabs に絵は描けない」は 2025 年までの話で、
        いまは他社のモデルを預かる flows の口がある。"""
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs_flow("image"))

        image = asyncio.run(media_backends.generate(
            "elevenlabs", media_backends.ImageRequest(prompt="城", size="1024x1024", seed=5)))

        assert image.data == PNG
        assert fake_elevenlabs_flow.sent["resolution"] == "1K"
        # seed を受け付ける数少ない外部の相手(同じ絵を作り直せる)
        assert fake_elevenlabs_flow.sent["seed"] == 5


class TestVideoRequests:
    def test_a_length_the_backend_does_not_take_is_refused_not_rounded(self, state):
        """丸めない。 受け付ける値が飛び飛びなので、寄せると「6 秒で頼んだのに
        8 秒が返る」になる —— 数分と数十 MB を使ってから気づくことになる。"""
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)

        with pytest.raises(HTTPException) as e:
            media.start_video_job("海", backend="openai", seconds=6.0)

        assert e.value.detail["seconds"] == [4.0, 8.0, 12.0]

    def test_a_backend_without_a_length_knob_refuses_seconds(self, state):
        """Omni Flash には尺を渡す口が無い。黙って無視すると、頼んだ長さで
        出来たと思われる。"""
        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)

        with pytest.raises(HTTPException) as e:
            media.start_video_job("花火", backend="gemini", seconds=8.0)

        assert "長さを指定できません" in e.value.detail["error"]

    def test_a_size_the_gpu_cannot_render_is_refused_before_starting(self, state):
        with pytest.raises(HTTPException) as e:
            media.start_video_job("猫", backend="comfyui", size="1024x1024")

        assert e.value.detail["sizes"] == ["848x480", "480x848", "1280x720", "720x1280"]

    def test_fewer_videos_can_be_asked_for_at_once_than_images(self, state):
        """1 本で数分と数十 MB。間違えたときの損が大きいほうを狭くする。"""
        with pytest.raises(HTTPException) as e:
            media.start_video_job("猫", backend="comfyui", size="848x480", count=4)

        assert "1〜2" in e.value.detail["error"]

    def test_the_shortest_length_is_the_default(self, state):
        """頼まれなければいちばん短いもの —— 動画は 1 本が高いので、
        既定は「間違えたときの損が小さいほう」にする。"""
        job = media.create_job("猫", backend="comfyui", size="848x480",
                               kind=media_providers.KIND_VIDEO)

        assert job["seconds"] == 2.0

    def test_a_video_job_is_saved_as_mp4(self, state):
        use(state, fake_comfy_video())

        job = media.create_job("猫", backend="comfyui", size="848x480",
                               kind=media_providers.KIND_VIDEO, seconds=2.0)
        asyncio.run(media._run(job["id"], "comfyui",
                               media_backends.VideoRequest(prompt="猫", size="848x480",
                                                           seconds=2.0),
                               1, media_providers.KIND_VIDEO))

        done = media.get_job(job["id"])
        assert done["state"] == "done"
        assert done["files"][0]["path"].endswith(".mp4")

    def test_a_running_video_is_not_reaped_on_the_image_schedule(self, state):
        """動画だけ猶予が長い。 絵と同じ基準で畳むと、まだ相手の中で作っている
        最中の job を「中断された」と書いてしまい、出来た動画を取りに行けなくなる。"""
        job = media.create_job("猫", backend="comfyui", size="848x480",
                               kind=media_providers.KIND_VIDEO, seconds=2.0)
        stale = (datetime.now(UTC) - timedelta(seconds=media.STALE_AFTER + 60)).isoformat()
        media._update(job["id"], state="running")
        with media._connect() as conn:
            conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (stale, job["id"]))

        assert media.get_job(job["id"])["state"] == "running"


# ---- 声(読み上げと文字起こし)------------------------------------------------
#
# 向きが逆の 2 つを同じ節にまとめてある。押さえるのは 3 つ:
# 声の名前を id に直せること、生の PCM をそのまま配らないこと、
# 文字起こしだけは job にしないこと。

WAV = b"RIFF" + b"0" * 60
PCM = b"\x01\x00" * 128


def fake_elevenlabs_voice(status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/voices"):
            return httpx.Response(200, json={"voices": [
                {"voice_id": "v-rachel", "name": "Rachel"},
                {"voice_id": "v-adam", "name": "Adam"},
            ]})
        if "/text-to-speech/" in path:
            fake_elevenlabs_voice.voice = path.rsplit("/", 1)[-1]
            fake_elevenlabs_voice.sent = json.loads(request.content)
            fake_elevenlabs_voice.query = dict(request.url.params)
            return httpx.Response(status, content=MP3)
        if path.endswith("/speech-to-text"):
            fake_elevenlabs_voice.upload = request.content
            return httpx.Response(200, json={"text": "こんにちは", "language_code": "ja"})
        return httpx.Response(404)

    return handler


class TestSpeech:
    def test_a_voice_can_be_asked_for_by_name(self, state):
        """id を控えている人はいない。 画面にも道具にも出るのは「Rachel」の側なので、
        名前で頼めないと、毎回一覧を引いてから頼むことになる。"""
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs_voice())

        speech = asyncio.run(media_backends.generate_speech(
            "elevenlabs", media_backends.SpeechRequest(prompt="やあ", voice="Rachel")))

        assert fake_elevenlabs_voice.voice == "v-rachel"
        assert fake_elevenlabs_voice.sent["text"] == "やあ"
        assert speech.data == MP3

    def test_an_unknown_name_is_passed_through_as_an_id(self, state):
        """一覧を引き損ねただけ、という場合に頼みごと自体を潰さない。"""
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs_voice())

        asyncio.run(media_backends.generate_speech(
            "elevenlabs", media_backends.SpeechRequest(prompt="やあ", voice="v-custom")))

        assert fake_elevenlabs_voice.voice == "v-custom"

    def test_openai_reads_with_one_of_its_fixed_voices(self, state):
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)
        sent = {}

        def handler(request: httpx.Request) -> httpx.Response:
            sent.update(json.loads(request.content))
            return httpx.Response(200, content=MP3)

        use(state, handler)
        asyncio.run(media_backends.generate_speech(
            "openai", media_backends.SpeechRequest(prompt="やあ", instructions="明るく")))

        assert sent["voice"] == "alloy"
        assert sent["response_format"] == "mp3"
        # 読み方の指示が効くのは gpt-4o-mini-tts だけ。既定がそれなので載る
        assert sent["instructions"] == "明るく"

    def test_raw_pcm_gets_a_wav_header_before_it_is_saved(self, state):
        """Gemini は生の PCM を返すことがある。 そのまま保存すると拡張子も中身も
        再生できないファイルになり、受け取った側は開くまで気づけない。"""
        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)

        def handler(request: httpx.Request) -> httpx.Response:
            fake = {"steps": [{"type": "model_output", "content": [
                {"type": "audio", "mime_type": "audio/L16;rate=24000",
                 "data": base64.b64encode(PCM).decode()}
            ]}]}
            handler.sent = json.loads(request.content)
            return httpx.Response(200, json=fake)

        use(state, handler)
        speech = asyncio.run(media_backends.generate_speech(
            "gemini", media_backends.SpeechRequest(prompt="やあ", voice="Puck")))

        assert speech.mime == "audio/wav"
        assert speech.data[:4] == b"RIFF"
        assert handler.sent["generation_config"]["speech_config"] == [{"voice": "Puck"}]

    def test_a_wav_that_already_has_a_header_is_left_alone(self, state):
        assert media_backends._ensure_wav(WAV, "audio/wav") == (WAV, "audio/wav")

    def test_the_local_gpu_is_not_offered_for_reading_aloud(self, state):
        """ComfyUI 本体に TTS のノードが無い(外部の拡張しか無く、入れたもので
        ノード名も引数も変わる)。選べる形にしておくほうが不親切。"""
        ids = [p.id for p in media_providers.all_providers(media_providers.KIND_SPEECH)]

        assert "comfyui" not in ids
        assert "comfyui" not in media_backends.SPEECH_GENERATORS

    def test_a_voice_the_backend_does_not_have_is_refused_before_starting(self, state):
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)

        with pytest.raises(HTTPException) as e:
            media.start_speech_job("やあ", backend="openai", voice="Rachel")

        assert "alloy" in e.value.detail["voices"]

    def test_a_backend_that_keeps_its_voices_remote_is_not_second_guessed(self, state):
        """ElevenLabs は登録した声が人によって違う。こちらに一覧が無いので素通しする
        —— 勝手に既定へ倒すと、頼んだ声と違う声で読み上げられる。"""
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)

        job = media.create_job("やあ", backend="elevenlabs",
                               kind=media_providers.KIND_SPEECH, voice="なんとかさん")

        assert job["voice"] == "なんとかさん"


class TestTranscribe:
    def test_it_returns_the_text_right_away_instead_of_a_job(self, state):
        """返るのは文字(数 KB)。 置き場も掃除も配信も要らないので、
        job にすると呼ぶ側の手数が増えるだけになる。"""
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        use(state, fake_elevenlabs_voice())

        got = asyncio.run(media.transcribe(
            data=MP3, filename="a.mp3", mime="audio/mpeg", backend="elevenlabs"))

        assert got["text"] == "こんにちは"
        assert got["language"] == "ja"
        assert media.recent_jobs() == []
        # multipart で送る(base64 に膨らませない)
        assert b'name="file"' in fake_elevenlabs_voice.upload

    def test_gemini_is_told_to_write_down_only_what_it_hears(self, state):
        """専用のモデルではないので、言わないと要約や感想が混じる。"""
        settings_store.set_credential("gemini", "k")
        settings_store.set_enabled("gemini", True)

        def handler(request: httpx.Request) -> httpx.Response:
            handler.sent = json.loads(request.content)
            return httpx.Response(200, json={"steps": [
                {"type": "thought", "content": [{"type": "text", "text": "考えた"}]},
                {"type": "model_output", "content": [{"type": "text", "text": "やあ"}]},
            ]})

        use(state, handler)
        got = asyncio.run(media.transcribe(data=MP3, mime="audio/mpeg", backend="gemini"))

        # 考えごとは拾わない(model_output だけ)
        assert got["text"] == "やあ"
        assert "書き起こし" in handler.sent["input"][1]["text"]

    def test_it_reads_only_from_the_media_directory(self, state):
        """chiezo はコンテナの中で動いていて、頼んだ人のディスクは見えない。
        受け取れるように見せると、あるはずのファイルが「見つからない」と返る。"""
        with pytest.raises(HTTPException) as e:
            asyncio.run(media.load_audio(path="../../etc/passwd"))

        assert e.value.status_code == 404

    def test_it_can_read_back_what_chiezo_itself_made(self, state):
        """自分で読み上げた音をそのまま文字起こしに回せる(確かめに使う)。"""
        day = datetime.now(UTC).strftime("%Y%m%d")
        directory = media.require_dir() / day
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "j-0.mp3").write_bytes(MP3)

        data, name, mime = asyncio.run(media.load_audio(path=f"/media/{day}/j-0.mp3"))

        assert (data, name, mime) == (MP3, "j-0.mp3", "audio/mpeg")

    def test_it_refuses_a_file_big_enough_to_take_the_process_down(self, state):
        state.setattr(media, "MAX_TRANSCRIBE_BYTES", 8)
        day = datetime.now(UTC).strftime("%Y%m%d")
        directory = media.require_dir() / day
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "big.mp3").write_bytes(b"0" * 64)

        with pytest.raises(HTTPException) as e:
            asyncio.run(media.load_audio(path=f"/media/{day}/big.mp3"))

        assert "大きすぎます" in e.value.detail["error"]


class TestNewBackendLists:
    def test_the_video_list_says_which_lengths_are_allowed(self, state):
        """上限ではなく一覧。 呼ぶ側が「4 と 8 の間は無い」と分かる形にする。"""
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)
        found = {e["id"]: e for e in asyncio.run(
            media.backends(media_providers.KIND_VIDEO))}

        assert found["openai"]["seconds"] == [4.0, 8.0, 12.0]
        # 尺を指定できない相手は空(モデルが決める)ではなく、Veo のぶんが並ぶ
        assert "gemini" in found

    def test_the_speech_list_asks_elevenlabs_for_its_voices(self, state):
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        settings_store.set_credential("openai", "sk-x")
        settings_store.set_enabled("openai", True)
        use(state, fake_elevenlabs_voice())

        found = {e["id"]: e for e in asyncio.run(
            media.backends(media_providers.KIND_SPEECH))}

        assert [v["label"] for v in found["elevenlabs"]["voices"]] == ["Rachel", "Adam"]
        # 決め打ちの相手はこちらの一覧をそのまま出す
        assert {"id": "alloy", "label": "alloy"} in found["openai"]["voices"]

    def test_the_new_tools_are_registered(self, state):
        from app import mcp_server
        from app.main import app as fastapi_app

        names = {tool.name for tool in asyncio.run(mcp_server.build_mcp(fastapi_app).list_tools())}

        assert {"video_generate", "video_status", "video_backends",
                "speech_generate", "speech_status", "transcribe",
                "voice_backends"} <= names

    def test_the_voice_list_is_remembered_for_a_while(self, state):
        """管理画面が開くたびに聞きに行かせない。 相手が遅い日に、画面そのものが
        10 秒待たされる —— 声はそう頻繁に増えない。"""
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        asked = {"n": 0}
        inner = fake_elevenlabs_voice()

        def counting(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/voices"):
                asked["n"] += 1
            return inner(request)

        use(state, counting)
        state.setattr(media_backends, "_voices_cache", {})
        spec = media_providers.get("elevenlabs")

        async def twice():
            return (await media_backends.elevenlabs_voices(spec),
                    await media_backends.elevenlabs_voices(spec))

        first, second = asyncio.run(twice())

        assert first == second and asked["n"] == 1


class TestElevenLabsImage:
    """ElevenLabs の画像は、こちらの都合をそのまま送ると断られる。

    どちらも実際に 422 を踏んで分かったもの:
    - 1024x1536 は 2:3 だが、向こうに 2:3 は無い
    - `gemini-3-pro-image` は `seed` を受け付けない(`extra_forbidden`)
    """

    def test_受け付ける縦横比の中から選ぶ(self):
        # 2:3 を送ると断られるので、近い 3:4 に寄せる
        got = media_backends._aspect_of(
            "1024x1536", media_backends._ELEVENLABS_IMAGE_ASPECTS)
        assert got in media_backends._ELEVENLABS_IMAGE_ASPECTS
        assert got == "3:4"

    def test_絞らなければ従来どおり(self):
        assert media_backends._aspect_of("1024x1536") == "2:3"

    def test_seedを断られたと分かる(self):
        # **remote_error が包んだ形で来る。** 相手が返した 422 ではなく 502 になり、
        # 本文は文字列として detail に入る —— ここを取り違えて一度直し損ねた
        err = HTTPException(502, {
            "error": "ElevenLabs(声・効果音・曲・絵・動画) が 422 を返しました",
            "detail": '{"detail":[{"type":"extra_forbidden",'
                      '"loc":["body","gpt-image-2","seed"],'
                      '"msg":"Extra inputs are not permitted","input":1808058667}]}',
        })
        assert media_backends._rejects_seed(err)

    def test_関係のないエラーはseedのせいにしない(self):
        assert not media_backends._rejects_seed(HTTPException(502, {
            "error": "ElevenLabs が 422 を返しました",
            "detail": '{"detail":[{"loc":["body","aspect_ratio"],"msg":"bad"}]}',
        }))
        assert not media_backends._rejects_seed(HTTPException(502, {
            "error": "ElevenLabs が 402 を返しました",
            "detail": '{"detail":{"code":"paid_plan_required"}}',
        }))


def fake_elevenlabs_seed_reject():
    """1 回目は seed を理由に 422、seed が無ければ通す相手。

    **本番で踏んだ形をそのまま真似る** —— 相手の 422 は remote_error が 502 に
    包み直すので、包む前の状態コードで判定していると再送に入らない。
    """
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/flows/image") and request.method == "POST":
            body = json.loads(request.content)
            seen.append(body)
            if "seed" in body:
                return httpx.Response(422, json={"detail": [{
                    "type": "extra_forbidden",
                    "loc": ["body", body.get("model_id", "gpt-image-2"), "seed"],
                    "msg": "Extra inputs are not permitted",
                }]})
            return httpx.Response(200, json={"id": "g1", "status": "pending"})
        if path.endswith("/flows/image/g1"):
            return httpx.Response(200, json={
                "id": "g1", "status": "completed",
                "content_url": "https://example.invalid/out.png",
                "content_mime_type": "image/png",
            })
        if path.endswith("/out.png"):
            return httpx.Response(200, content=PNG)
        return httpx.Response(404)

    handler.seen = seen
    return handler


class TestElevenLabsImageRetry:
    def test_seedを断られたら外して投げ直す(self, state):
        settings_store.set_credential("elevenlabs", "el-k")
        settings_store.set_enabled("elevenlabs", True)
        handler = fake_elevenlabs_seed_reject()
        use(state, handler)

        image = asyncio.run(media_backends.generate(
            "elevenlabs", media_backends.ImageRequest(prompt="test", size="1024x1536"),
        ))

        assert image.data == PNG
        # 2 回投げていて、2 回目には seed が無い
        assert len(handler.seen) == 2
        assert "seed" in handler.seen[0] and "seed" not in handler.seen[1]
        # 縦横比も相手が受け付ける値になっている（2:3 は無い）
        assert handler.seen[0]["aspect_ratio"] in media_backends._ELEVENLABS_IMAGE_ASPECTS
