"""相手ごとの「1 枚描く」実装。

**この層は保存も記録もしない**(受け取るのはプロンプト、返すのは画像のバイト列)。
ジョブの管理と保存は `app/media.py`。

**絵と音で層の形は同じ**(`ImageRequest` → `GeneratedImage` / `AudioRequest` →
`GeneratedAudio`)。分けてあるのは頼む語彙が違うからで、ジョブ・保存・掃除は
`app/media.py` が両方まとめて面倒を見る。

相手:

- **ComfyUI** —— 自前の GPU。API は「プロンプトを投げる口」ではなく**ノードのグラフを
  投げる口**なので、こちらでテンプレのグラフを持ち、プロンプト・seed・サイズを差し込む。
  音も同じ口で、**チェックポイントの系統でグラフが変わる**(Stable Audio Open / ACE-Step)。
- **Gemini** —— 外部。鍵は「話す相手」に登録済みのものを流用する。絵も曲(Lyria 3)も
  同じ `interactions` の口で、違うのは `response_format` だけ。
- **ElevenLabs** —— 外部。**効果音と曲で口が別**(`/sound-generation` と `/music`)。
  会話ができない相手なので鍵を借りる先が無く、ここだけ鍵を自分で持つ。
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


@dataclass(frozen=True)
class AudioRequest:
    """音を 1 つ作る頼み。**効果音と曲を `sound` で分ける** —— 相手によっては口が別で、
    自前の GPU でも読むチェックポイントが別物になる。"""

    prompt: str
    sound: str = media_providers.SOUND_SFX
    # 0 は「相手に任せる」。**長さを指定できない相手(Lyria)では常に無視される**
    seconds: float = 0.0
    # 歌詞。空なら**器楽**として頼む(ゲームの BGM は歌が入ると台詞と喧嘩する)
    lyrics: str = ""
    negative: str = ""
    seed: int = 0
    model: str = ""
    steps: int = 50
    # 繋いで鳴らせる素材にするか。**効くのは ElevenLabs の効果音だけ**
    loop: bool = False


@dataclass(frozen=True)
class GeneratedAudio:
    data: bytes
    mime: str
    seed: int
    model: str
    # 実際の長さ。**頼んだ秒数と一致するとは限らない**(相手が決める場合がある)ので、
    # 分からなければ 0 を入れる —— 嘘の数字を記録するより空のほうがよい。
    seconds: float = 0.0


# 長さを頼まなかったときの既定。**効果音は短く、曲はひと回し**。
DEFAULT_SECONDS = {media_providers.SOUND_SFX: 6.0, media_providers.SOUND_MUSIC: 30.0}


def resolve_seconds(spec: media_providers.MediaProvider, sound: str, seconds: float) -> float:
    """頼む長さを決める。**上限を超えたら切り詰める**(相手に断られるより手前で丸める)。

    上限が 0 の相手は「長さを指定できない」ので 0 を返す —— Lyria は尺がモデルで
    決まっていて、秒数を渡す口そのものが無い。
    """
    limit = media_providers.max_seconds_of(spec, sound)
    if limit <= 0:
        return 0.0
    want = seconds if seconds > 0 else DEFAULT_SECONDS.get(sound, 6.0)
    return max(1.0, min(want, limit))


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

    data, _ = await _comfy_execute(
        url, _comfy_graph(req, model, width, height, seed), "images", "画像"
    )
    return GeneratedImage(data, "image/png", seed, model)


async def _comfy_execute(url: str, graph: dict, output_key: str, what: str) -> tuple[bytes, str]:
    """グラフを投げて、出来た 1 つぶんのバイト列とファイル名を持ち帰る。

    **絵と音で違うのは出力の入れ物の名前だけ**(`images` / `audio`)なので、
    投げ方・待ち方・受け取り方はここに 1 つだけ置く。
    """
    async with _client(GENERATE_TIMEOUT) as client:
        queued = await client.post(f"{url}/prompt", json={"prompt": graph})
        if queued.status_code >= 400:
            # ComfyUI はグラフの不備を本文で教えてくれる(ノード名の綴り違い等)
            raise HTTPException(502, {"error": "ComfyUI がグラフを受け付けませんでした",
                                      "detail": queued.text[:500]})
        prompt_id = queued.json().get("prompt_id")
        if not prompt_id:
            raise HTTPException(502, {"error": "ComfyUI が prompt_id を返しませんでした"})

        # **完了は履歴で見る。** 進捗の WebSocket もあるが、こちらは 1 つぶんの
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

        found = [
            item
            for output in entry["outputs"].values()
            for item in output.get(output_key, [])
            if item.get("filename")
        ]
        if not found:
            raise HTTPException(502, {"error": f"ComfyUI が{what}を返しませんでした"})

        first = found[0]
        got = await client.get(
            f"{url}/view",
            params={
                "filename": first["filename"],
                "subfolder": first.get("subfolder", ""),
                "type": first.get("type", "output"),
            },
        )
        got.raise_for_status()

    return got.content, str(first["filename"])


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
        # **JPEG しか受け付けない。** png を頼むと 400 が返る
        # (`'image/png' is not supported for 'response_format.mime_type'`)。
        # 透過の要る素材は自前の GPU 側で作る。
        "response_format": {
            "type": "image",
            "mime_type": "image/jpeg",
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
    return GeneratedImage(base64.b64decode(data), "image/jpeg", seed, model)


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


# ---- Codex CLI(ChatGPT のサブスク枠)----------------------------------------
#
# ブリッジ(chiezo-bridge-codex)の `/v1/images/generations` に投げる。中では
# `codex exec` が内蔵の image_gen を回して PNG を書き、ブリッジがそれを base64 で返す。
# **鍵はこちらに無い**(ブリッジが「話す相手」で登録された auth.json を読む)。
async def _codex_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/images/generations",
            json={"prompt": req.prompt, "size": req.size, "n": 1},
        )
    if res.status_code >= 400:
        log.warning("codex image error %s: %s", res.status_code, res.text[:300])
        raise HTTPException(502, {"error": f"Codex のブリッジが {res.status_code} を返しました",
                                  "detail": res.text[:300]})

    data = next(
        (item.get("b64_json") for item in res.json().get("data", []) if item.get("b64_json")),
        None,
    )
    if not data:
        raise HTTPException(502, {"error": "Codex が画像を返しませんでした"})

    # **seed は受け付けない。** 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedImage(base64.b64decode(data), "image/png", seed, "gpt-image-2")


# ---- ComfyUI(音)-----------------------------------------------------------
#
# 音も絵と同じ「グラフを投げる口」で作る。違うのは **チェックポイントの系統でグラフが
# 変わる**こと:
#
# - **Stable Audio Open** … 効果音・短い素材向き。text encoder(T5)を**別に置く**必要が
#   あり、`CLIPLoader`(type=stable_audio)で読む
# - **ACE-Step** … 曲向き。model / clip / vae が 1 つに入った all-in-one で、
#   歌詞を受け取る専用の encode ノード(`TextEncodeAceStepAudio`)を使う
#
# **どちらかは名前で見分ける。** ComfyUI に「このチェックポイントは何の系統か」を
# 聞く口は無く(読み込んで初めて分かる)、GPU にモデルを載せてから間違いに気づくのは高い。
# 名前は置く人が決めるので確実ではないが、外したときは `model` で名指しできる。
_ACE_HINT = "ace"
_AUDIO_HINTS = ("audio", "ace")


def _is_ace(name: str) -> bool:
    return _ACE_HINT in name.lower()


def is_audio_checkpoint(name: str) -> bool:
    """音のチェックポイントらしい名前か(絵のものと同じ置き場に混ざっている)。"""
    return any(hint in name.lower() for hint in _AUDIO_HINTS)


async def comfy_audio_models(url: str, timeout: float = 5.0) -> list[str]:
    """置いてある**音の**チェックポイント。曲(ACE-Step)を先に並べる。"""
    names = [name for name in await comfy_models(url, timeout) if is_audio_checkpoint(name)]
    return sorted(names, key=lambda name: (not _is_ace(name), name))


def pick_audio_model(names: list[str], sound: str) -> str:
    """その音に向いたチェックポイントを選ぶ。**曲は ACE-Step 優先、効果音はその逆**。

    どちらしか無ければそれを使う —— 「置いてあるのに使えない」より、
    向いていなくても鳴るほうがよい(向き不向きは出てきた音で分かる)。
    """
    ace = [n for n in names if _is_ace(n)]
    plain = [n for n in names if not _is_ace(n)]
    order = ace + plain if sound == media_providers.SOUND_MUSIC else plain + ace
    return order[0] if order else ""


async def comfy_text_encoders(url: str, timeout: float = 5.0) -> list[str]:
    """`CLIPLoader` に置いてある text encoder。**Stable Audio Open にだけ要る**
    (ACE-Step は all-in-one なので不要)。"""
    async with _client(timeout) as client:
        res = await client.get(f"{url}/object_info/CLIPLoader")
        res.raise_for_status()
        info = res.json()["CLIPLoader"]["input"]["required"]["clip_name"][0]
    return [str(name) for name in info]


def _comfy_stable_audio_graph(
    req: AudioRequest, model: str, clip: str, seconds: float, seed: int
) -> dict:
    """Stable Audio Open のグラフ。**サンプラーの組み合わせは公式のテンプレどおり**
    (dpmpp_3m_sde_gpu + exponential)—— 拡散の設定は絵の勘が効かないので変えない。"""
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "stable_audio"}},
        "3": {"class_type": "CLIPTextEncode", "inputs": {"text": req.prompt, "clip": ["2", 0]}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": req.negative, "clip": ["2", 0]}},
        "5": {
            "class_type": "EmptyLatentAudio",
            "inputs": {"seconds": seconds, "batch_size": 1},
        },
        "6": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": req.steps,
                "cfg": 4.98,
                "sampler_name": "dpmpp_3m_sde_gpu",
                "scheduler": "exponential",
                "denoise": 1.0,
                "model": ["1", 0],
                "positive": ["3", 0],
                "negative": ["4", 0],
                "latent_image": ["5", 0],
            },
        },
        "7": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["6", 0], "vae": ["1", 2]}},
        "8": {
            "class_type": "SaveAudioMP3",
            "inputs": {"filename_prefix": "chiezo", "audio": ["7", 0], "quality": "V0"},
        },
    }


def _comfy_ace_graph(req: AudioRequest, model: str, seconds: float, seed: int) -> dict:
    """ACE-Step のグラフ。**歌詞を渡す口がある**のがこちら(空なら器楽として頼む)。

    否定プロンプトの代わりに `ConditioningZeroOut` を負側に置くのは公式のテンプレどおり
    —— このモデルは「何を出さないか」を言葉で受け取らない。
    """
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": model}},
        "2": {
            "class_type": "TextEncodeAceStepAudio",
            "inputs": {
                "clip": ["1", 1],
                "tags": req.prompt,
                "lyrics": req.lyrics or "[instrumental]",
                "lyrics_strength": 0.99,
            },
        },
        "3": {"class_type": "ConditioningZeroOut", "inputs": {"conditioning": ["2", 0]}},
        "4": {
            "class_type": "EmptyAceStepLatentAudio",
            "inputs": {"seconds": seconds, "batch_size": 1},
        },
        "5": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 5.0}},
        "6": {"class_type": "LatentOperationTonemapReinhard", "inputs": {"multiplier": 1.0}},
        "7": {
            "class_type": "LatentApplyOperationCFG",
            "inputs": {"model": ["5", 0], "operation": ["6", 0]},
        },
        "8": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": req.steps,
                "cfg": 5.0,
                "sampler_name": "euler",
                "scheduler": "simple",
                "denoise": 1.0,
                "model": ["7", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
            },
        },
        "9": {"class_type": "VAEDecodeAudio", "inputs": {"samples": ["8", 0], "vae": ["1", 2]}},
        "10": {
            "class_type": "SaveAudioMP3",
            "inputs": {"filename_prefix": "chiezo", "audio": ["9", 0], "quality": "V0"},
        },
    }


async def _comfy_audio(
    spec: media_providers.MediaProvider, req: AudioRequest, seed: int
) -> GeneratedAudio:
    url = media_providers.url_of(spec)
    seconds = resolve_seconds(spec, req.sound, req.seconds)

    model = req.model
    if not model:
        model = pick_audio_model(await comfy_audio_models(url), req.sound)
    if not model:
        raise HTTPException(
            502,
            {
                "error": "ComfyUI に音のチェックポイントが置かれていません",
                "hint": "models/checkpoints に stable-audio-open-1.0.safetensors(効果音)や "
                "ace_step_v1_3.5b.safetensors(曲)を置いてください"
                "(名前で系統を見分けるので、名前は変えずに置くこと)",
            },
        )

    if _is_ace(model):
        graph = _comfy_ace_graph(req, model, seconds, seed)
    else:
        # Stable Audio Open は text encoder を別に読む。**無ければグラフごと通らない**ので、
        # GPU を回す前に断る(ComfyUI 側のエラーは「clip_name が不正」としか出ない)
        encoders = await comfy_text_encoders(url)
        clip = next((name for name in encoders if "t5" in name.lower()), "")
        if not clip:
            raise HTTPException(
                502,
                {
                    "error": f"{model} を使うには text encoder(T5)が要ります",
                    "hint": "models/text_encoders に t5-base.safetensors を置いてください"
                    "(ACE-Step のチェックポイントなら all-in-one なので不要です)",
                },
            )
        graph = _comfy_stable_audio_graph(req, model, clip, seconds, seed)

    data, _name = await _comfy_execute(url, graph, "audio", "音")
    return GeneratedAudio(data, "audio/mpeg", seed, model, seconds)


# ---- Gemini(Lyria 3 / 曲)----------------------------------------------------
#
# **絵とまったく同じ `interactions` の口**で、違うのは `response_format` だけ。
# **効果音は作れない**(Lyria は曲のモデル)ので、頼まれたら断る —— 短い衝突音を
# 頼んで 30 秒の曲が返るほうが、呼んだ側にとっては分かりにくい。
# **尺は指定できない**(モデルで決まる。clip = 30 秒ほど、pro = 3 分ほど)。
async def _gemini_audio(
    spec: media_providers.MediaProvider, req: AudioRequest, seed: int
) -> GeneratedAudio:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "Gemini の API キーが未登録です",
                "hint": "管理画面(/admin の「話す相手」)で Gemini の鍵を登録してください"
                "(音の生成でも同じ鍵を使います)",
            },
        )

    model = req.model or spec.audio_models[0]
    prompt = req.prompt if not req.lyrics else f"{req.prompt}\n\n[lyrics]\n{req.lyrics}"
    body = {
        "model": model,
        "input": [{"type": "text", "text": prompt}],
        "response_format": {"type": "audio"},
    }

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/interactions",
            headers={"x-goog-api-key": key},
            json=body,
        )
    if res.status_code >= 400:
        # 鍵と本文はログにも画面にも出さない(理由の頭だけ返す)
        log.warning("gemini audio error %s: %s", res.status_code, res.text[:300])
        raise HTTPException(502, {"error": f"Gemini が {res.status_code} を返しました",
                                  "detail": res.text[:300]})

    item = next(
        (
            content
            for step in res.json().get("steps", [])
            for content in step.get("content", [])
            if content.get("type") == "audio" and content.get("data")
        ),
        None,
    )
    if not item:
        raise HTTPException(502, {"error": "Gemini が音を返しませんでした"})

    # **seed は受け付けない。** 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedAudio(
        base64.b64decode(item["data"]), item.get("mime_type") or "audio/mpeg", seed, model, 0.0
    )


# ---- ElevenLabs(効果音・曲)--------------------------------------------------
#
# **効果音と曲で口が別**(`/sound-generation` と `/music`)。返るのは JSON ではなく
# **音のバイト列そのもの**なので、失敗したときだけ本文が JSON になる。
# 鍵はこの相手のもの(会話ができないので「話す相手」に借り先が無い)。
def _elevenlabs_body(req: AudioRequest, model: str, seconds: float) -> tuple[str, dict]:
    if req.sound == media_providers.SOUND_MUSIC:
        body: dict = {"prompt": req.prompt, "model_id": model}
        if req.lyrics:
            body["prompt"] = f"{req.prompt}\n\nLyrics:\n{req.lyrics}"
        else:
            # 歌詞を渡さないなら器楽で頼む(ゲームの BGM に歌が乗ると台詞と喧嘩する)
            body["force_instrumental"] = True
        if seconds > 0:
            body["music_length_ms"] = int(seconds * 1000)
        return "music", body

    body = {"text": req.prompt, "model_id": model, "loop": req.loop}
    if seconds > 0:
        body["duration_seconds"] = seconds
    return "sound-generation", body


async def _elevenlabs_audio(
    spec: media_providers.MediaProvider, req: AudioRequest, seed: int
) -> GeneratedAudio:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "ElevenLabs の API キーが未登録です",
                "hint": "管理画面(/admin の「絵と音を作る相手」)で ElevenLabs の鍵を"
                "登録してください",
            },
        )

    model = req.model or (
        "music_v2" if req.sound == media_providers.SOUND_MUSIC else "eleven_text_to_sound_v2"
    )
    seconds = resolve_seconds(spec, req.sound, req.seconds)
    path, body = _elevenlabs_body(req, model, seconds)

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/{path}",
            headers={"xi-api-key": key},
            json=body,
        )
    if res.status_code >= 400:
        log.warning("elevenlabs audio error %s: %s", res.status_code, res.text[:300])
        raise HTTPException(502, {"error": f"ElevenLabs が {res.status_code} を返しました",
                                  "detail": res.text[:300]})
    if not res.content:
        raise HTTPException(502, {"error": "ElevenLabs が音を返しませんでした"})

    # **seed は効果音の口には無い。** 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedAudio(res.content, "audio/mpeg", seed, model, seconds)


GENERATORS = {
    "comfyui": _comfy_generate,
    "codex": _codex_generate,
    "gemini": _gemini_generate,
    "openai": _openai_generate,
}


AUDIO_GENERATORS = {
    "comfyui": _comfy_audio,
    "gemini": _gemini_audio,
    "elevenlabs": _elevenlabs_audio,
}


def _ready(backend: str, table: dict, kind: str) -> media_providers.MediaProvider:
    """相手を引いて、頼める状態かまで確かめる。**絵と音で同じ検査を通す**。"""
    spec = media_providers.get(backend)
    if spec is None or spec.id not in table:
        raise HTTPException(
            404,
            {
                "error": f"unknown backend: {backend}",
                "backends": [p.id for p in media_providers.all_providers(kind)],
            },
        )

    # **止めてある相手には頼まない。** 画面で無効にしたのに道具からは作れてしまう、
    # という食い違いを作らない
    if reason := unusable_reason(spec):
        # **鍵をどこへ入れるかは相手によって違う。** 借り物の相手は「話す相手」、
        # 借り先の無い相手(ElevenLabs)は「絵と音を作る相手」の節。
        # 場所を言わない案内は、受け取った側にとって次の一手にならない。
        where = "話す相手" if spec.credential_from else "絵と音を作る相手"
        raise HTTPException(
            401 if reason == NO_CREDENTIAL else 403,
            {
                "error": f"{spec.label} は使えません: {reason}",
                "hint": f"管理画面(/admin の「{where}」)で鍵を登録し、有効にしてください"
                "(絵と音で同じ鍵と on/off を使います)",
            },
        )
    return spec


async def generate(backend: str, req: ImageRequest) -> GeneratedImage:
    """1 枚描く。相手を知らなければ 404(選べる相手つき)。"""
    spec = _ready(backend, GENERATORS, media_providers.KIND_IMAGE)

    # seed は**必ず決めてから渡す**。0 のまま相手任せにすると、あとで同じ絵を
    # 作り直せない(ゲーム素材は「同じキャラの別ポーズ」を作るので再現性が要る)
    seed = req.seed or random.randint(1, 2**31 - 1)

    return await GENERATORS[spec.id](spec, req, seed)


async def generate_audio(backend: str, req: AudioRequest) -> GeneratedAudio:
    """音を 1 つ作る。**頼めない種類は先に断る**(Lyria に効果音は頼めない)。"""
    spec = _ready(backend, AUDIO_GENERATORS, media_providers.KIND_AUDIO)

    if req.sound not in media_providers.sounds_of(spec):
        raise HTTPException(
            400,
            {
                "error": f"{spec.label} に {req.sound} は頼めません",
                "sounds": list(media_providers.sounds_of(spec)),
                "hint": "audio_backends で、その相手に頼める種類を確かめてください",
            },
        )

    seed = req.seed or random.randint(1, 2**31 - 1)

    return await AUDIO_GENERATORS[spec.id](spec, req, seed)
