"""相手ごとの「1 枚描く」実装。

**この層は保存も記録もしない**(受け取るのはプロンプト、返すのは画像のバイト列)。
ジョブの管理と保存は `app/media.py`。

相手は 2 つ:

- **ComfyUI** —— 自前の GPU。API は「プロンプトを投げる口」ではなく**ノードのグラフを
  投げる口**なので、こちらでテンプレのグラフを持ち、プロンプト・seed・サイズを差し込む。
- **Gemini** —— 外部。鍵は「話す相手」に登録済みのものを流用する。
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import random
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from app import media_providers, settings_store

log = logging.getLogger("chiezo.media")

# 1 枚あたりの上限。GPU でも SDXL は数秒〜数十秒かかる(混んでいれば待たされる)。
GENERATE_TIMEOUT = float(__import__("os").environ.get("CHIEZO_IMAGE_TIMEOUT", "300") or 300)


@dataclass(frozen=True)
class ImageRequest:
    prompt: str
    negative: str = ""
    size: str = "1024x1024"
    seed: int = 0
    model: str = ""
    steps: int = 25


@dataclass(frozen=True)
class GeneratedImage:
    data: bytes
    mime: str
    seed: int
    model: str


def parse_size(size: str) -> tuple[int, int]:
    """`1024x1536` を画素に直す。**8 の倍数に丸める**(拡散モデルの制約)。"""
    try:
        width, height = (int(v) for v in size.lower().split("x", 1))
    except (ValueError, AttributeError):
        raise HTTPException(400, {"error": f"サイズの書き方が違います: {size}(例 1024x1024)"}) from None
    if not (256 <= width <= 2048 and 256 <= height <= 2048):
        raise HTTPException(400, {"error": f"サイズは 256〜2048 の範囲にしてください: {size}"})
    return width - width % 8, height - height % 8


def credential_of(spec: media_providers.MediaProvider) -> str:
    """その相手の鍵。**借り先が決まっていればそちらから読む**(鍵を 2 か所に持たない)。"""
    if spec.credential == media_providers.CRED_NONE:
        return ""
    source = spec.credential_from or spec.id
    return (settings_store.load(source).credential or "").strip()


# 鍵が無いときの理由。**状態(403)と認証(401)を出し分ける**ために、文字列を定数で持つ
# —— 「鍵を入れれば直る」と「画面で有効にすれば直る」は、次にすることが違う。
NO_CREDENTIAL = "鍵が未登録"


def unusable_reason(spec: media_providers.MediaProvider) -> str:
    """使えない理由。使えるなら空。**画面と道具で同じ判定を使う**(食い違わせない)。

    **「話す相手」で無効にしてある相手は、絵も描かせない。** 鍵を持っている相手を
    止めたのに片方だけ動き続けるのは、止めたつもりの人にとって事故になる。
    元栓(「答える」層)が停止中なら、相手によらず全部止める。
    """
    if not settings_store.answer_enabled():
        return "「答える」層が停止中"
    # 自分の on/off を持つ相手(自前の GPU)は自分の行を、「話す相手」に対応がある相手は
    # あちらの行を見る。**同じものを 2 か所で切り替えさせない**
    if spec.owns_toggle:
        if not settings_store.load(spec.id).enabled:
            return "無効(この節の「使う」で有効にする)"
    elif spec.credential_from and not settings_store.load(spec.credential_from).enabled:
        return "「話す相手」で無効(先に話せるようにする)"
    if spec.credential == media_providers.CRED_REQUIRED and not credential_of(spec):
        return NO_CREDENTIAL
    return ""


def _client(timeout: float) -> httpx.AsyncClient:
    """テストが差し替える口(`httpx.MockTransport` を挿す)。"""
    return httpx.AsyncClient(timeout=timeout)


# ---- ComfyUI ---------------------------------------------------------------
#
# **グラフはここに持つ。** ComfyUI は画面で組んだワークフロー(ノードの JSON)を
# そのまま受け取る作りで、「プロンプトとサイズだけ渡す」口は無い。テンプレを 1 つ持ち、
# 差し込む値(モデル・プロンプト・除外プロンプト・サイズ・seed・ステップ)だけを埋める。
# **ノード番号は文字列**(ComfyUI の約束)。
def _comfy_graph(req: ImageRequest, model: str, width: int, height: int, seed: int) -> dict:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": req.prompt, "clip": ["1", 1]}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": req.negative, "clip": ["1", 1]}},
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": req.steps,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {"class_type": "SaveImage", "inputs": {"filename_prefix": "chiezo", "images": ["6", 0]}},
    }


async def comfy_models(url: str, timeout: float = 5.0) -> list[str]:
    """置いてあるチェックポイントを相手に聞く(置いたものは環境ごとに違う)。"""
    async with _client(timeout) as client:
        res = await client.get(f"{url}/object_info/CheckpointLoaderSimple")
        res.raise_for_status()
        info = res.json()["CheckpointLoaderSimple"]["input"]["required"]["ckpt_name"][0]
    return [str(name) for name in info]


async def _comfy_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    url = media_providers.url_of(spec)
    width, height = parse_size(req.size)

    model = req.model
    if not model:
        available = await comfy_models(url)
        if not available:
            raise HTTPException(
                502,
                {
                    "error": "ComfyUI にチェックポイントが 1 つも置かれていません",
                    "hint": "models/checkpoints に .safetensors を置いてください",
                },
            )
        model = available[0]

    async with _client(GENERATE_TIMEOUT) as client:
        queued = await client.post(
            f"{url}/prompt", json={"prompt": _comfy_graph(req, model, width, height, seed)}
        )
        if queued.status_code >= 400:
            # ComfyUI はグラフの不備を本文で教えてくれる(ノード名の綴り違い等)
            raise HTTPException(502, {"error": "ComfyUI がグラフを受け付けませんでした",
                                      "detail": queued.text[:500]})
        prompt_id = queued.json().get("prompt_id")
        if not prompt_id:
            raise HTTPException(502, {"error": "ComfyUI が prompt_id を返しませんでした"})

        # **完了は履歴で見る。** 進捗の WebSocket もあるが、こちらは 1 枚ごとの
        # 出来上がりだけが要るので、素の HTTP で足りる
        deadline = asyncio.get_running_loop().time() + GENERATE_TIMEOUT
        while True:
            history = await client.get(f"{url}/history/{prompt_id}")
            entry = history.json().get(prompt_id) if history.status_code < 400 else None
            if entry and entry.get("outputs"):
                break
            if entry and (entry.get("status", {}).get("status_str") == "error"):
                raise HTTPException(502, {"error": "ComfyUI の生成が失敗しました",
                                          "detail": json.dumps(entry.get("status"))[:500]})
            if asyncio.get_running_loop().time() > deadline:
                raise HTTPException(504, {"error": f"ComfyUI が {GENERATE_TIMEOUT:.0f} 秒で終わりませんでした"})
            await asyncio.sleep(1.0)

        images = [
            image
            for output in entry["outputs"].values()
            for image in output.get("images", [])
            if image.get("filename")
        ]
        if not images:
            raise HTTPException(502, {"error": "ComfyUI が画像を返しませんでした"})

        first = images[0]
        got = await client.get(
            f"{url}/view",
            params={
                "filename": first["filename"],
                "subfolder": first.get("subfolder", ""),
                "type": first.get("type", "output"),
            },
        )
        got.raise_for_status()

    return GeneratedImage(got.content, "image/png", seed, model)


# ---- Gemini ----------------------------------------------------------------
#
# 画像は `interactions` の口で、`response_format` に image を指定して頼む。
# **サイズは画素ではなく「比率 + 段階」**なので、こちらの `幅x高さ` から比率へ寄せる。
# 相手が受け付ける比率(Gemini)。**近いものへ寄せる**ので、細かく並べるほど元の
# 縦横比に近づく。1536x1024 のような 3:2 を 4:3 に丸めると、素材として使うときに
# 切り貼りが要る。
_ASPECTS = (
    (1.0, "1:1"),
    (4 / 3, "4:3"), (3 / 4, "3:4"),
    (3 / 2, "3:2"), (2 / 3, "2:3"),
    (5 / 4, "5:4"), (4 / 5, "4:5"),
    (16 / 9, "16:9"), (9 / 16, "9:16"),
    (21 / 9, "21:9"),
)


def _aspect_of(size: str) -> str:
    width, height = parse_size(size)
    ratio = width / height
    return min(_ASPECTS, key=lambda pair: abs(pair[0] - ratio))[1]


async def _gemini_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "Gemini の API キーが未登録です",
                "hint": "管理画面(/admin の「話す相手」)で Gemini の鍵を登録してください"
                "(画像生成でも同じ鍵を使います)",
            },
        )

    model = req.model or spec.models[0]
    body = {
        "model": model,
        "input": [{"type": "text", "text": req.prompt}],
        "response_format": {
            "type": "image",
            "mime_type": "image/png",
            "aspect_ratio": _aspect_of(req.size),
        },
    }

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/interactions",
            headers={"x-goog-api-key": key},
            json=body,
        )
    if res.status_code >= 400:
        # 鍵と本文はログにも画面にも出さない(理由の頭だけ返す)
        log.warning("gemini image error %s: %s", res.status_code, res.text[:300])
        raise HTTPException(502, {"error": f"Gemini が {res.status_code} を返しました",
                                  "detail": res.text[:300]})

    data = next(
        (
            item.get("data")
            for step in res.json().get("steps", [])
            for item in step.get("content", [])
            if item.get("type") == "image" and item.get("data")
        ),
        None,
    )
    if not data:
        raise HTTPException(502, {"error": "Gemini が画像を返しませんでした"})

    # **seed は返らない。** 相手が受け付けないので、こちらで振った値を記録だけしておく
    # (同じ seed で頼み直しても同じ絵にはならない —— 再現できるのは ComfyUI 側だけ)
    return GeneratedImage(base64.b64decode(data), "image/png", seed, model)


# ---- OpenAI(gpt-image)------------------------------------------------------
#
# **サイズは決まった組み合わせしか取らない**(自由な画素数は投げられない)ので、
# 頼まれた `幅x高さ` から**縦横比が近く、面積の近いもの**を選ぶ。
# 応答は base64 のみ(URL は返らない)。
def _openai_size(spec: media_providers.MediaProvider, size: str) -> str:
    width, height = parse_size(size)
    ratio, area = width / height, width * height

    def distance(candidate: str) -> tuple[float, float]:
        cw, ch = (int(v) for v in candidate.split("x"))
        # 比率を優先し、同じくらいなら面積が近いほうを選ぶ(引き伸ばしより解像度違いのほうが軽い)
        return abs(cw / ch - ratio), abs(cw * ch - area)

    return min(spec.sizes, key=distance)


async def _openai_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "OpenAI の API キーが未登録です",
                "hint": "管理画面(/admin の「話す相手」)で OpenAI の鍵を登録してください"
                "(画像生成でも同じ鍵を使います)",
            },
        )

    model = req.model or spec.models[0]
    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/images/generations",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": model,
                "prompt": req.prompt,
                "size": _openai_size(spec, req.size),
                "n": 1,
            },
        )
    if res.status_code >= 400:
        # 鍵は載せない。理由の頭だけ返す。**403 が返ったら組織の本人確認を疑う** ——
        # OpenAI の API は一部のモデルで開発者コンソールでの本人確認を求めることがある
        # (ChatGPT / Codex のサブスクとは別系統なので、あちらで使えていても関係しない)
        log.warning("openai image error %s: %s", res.status_code, res.text[:300])
        raise HTTPException(502, {"error": f"OpenAI が {res.status_code} を返しました",
                                  "detail": res.text[:300]})

    data = next((item.get("b64_json") for item in res.json().get("data", []) if item.get("b64_json")), None)
    if not data:
        raise HTTPException(502, {"error": "OpenAI が画像を返しませんでした"})

    # **seed は受け付けない。** 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedImage(base64.b64decode(data), "image/png", seed, model)


GENERATORS = {
    "comfyui": _comfy_generate,
    "gemini": _gemini_generate,
    "openai": _openai_generate,
}


async def generate(backend: str, req: ImageRequest) -> GeneratedImage:
    """1 枚描く。相手を知らなければ 404(選べる相手つき)。"""
    spec = media_providers.get(backend)
    if spec is None or spec.id not in GENERATORS:
        raise HTTPException(
            404,
            {
                "error": f"unknown backend: {backend}",
                "backends": [p.id for p in media_providers.all_providers()],
            },
        )

    # **止めてある相手には頼まない。** 画面で無効にしたのに道具からは描けてしまう、
    # という食い違いを作らない
    if reason := unusable_reason(spec):
        raise HTTPException(
            401 if reason == NO_CREDENTIAL else 403,
            {
                "error": f"{spec.label} は使えません: {reason}",
                "hint": "管理画面(/admin の「話す相手」)で鍵を登録し、有効にしてください"
                "(画像生成でも同じ鍵と on/off を使います)",
            },
        )

    # seed は**必ず決めてから渡す**。0 のまま相手任せにすると、あとで同じ絵を
    # 作り直せない(ゲーム素材は「同じキャラの別ポーズ」を作るので再現性が要る)
    seed = req.seed or random.randint(1, 2**31 - 1)

    return await GENERATORS[spec.id](spec, req, seed)
