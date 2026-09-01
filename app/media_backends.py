"""相手ごとの「1 枚描く」実装。

この層は保存も記録もしない(受け取るのはプロンプト、返すのは画像のバイト列)。
ジョブの管理と保存は `app/media.py`。

絵と音で層の形は同じ(`ImageRequest` → `GeneratedImage` / `AudioRequest` →
`GeneratedAudio`)。分けてあるのは頼む語彙が違うからで、ジョブ・保存・掃除は
`app/media.py` が両方まとめて面倒を見る。

相手:

- ComfyUI —— 自前の GPU。API は「プロンプトを投げる口」ではなく**ノードのグラフを
  投げる口**なので、こちらでテンプレのグラフを持ち、プロンプト・seed・サイズを差し込む。
  音も同じ口で、チェックポイントの系統でグラフが変わる(Stable Audio Open / ACE-Step)。
- Gemini —— 外部。鍵は会話用に登録済みのものを流用する。絵も曲(Lyria 3)も
  同じ `interactions` の口で、違うのは `response_format` だけ。
- ElevenLabs —— 外部。効果音と曲で口が別(`/sound-generation` と `/music`)。
  会話の口が「先にエージェントを作る」形で `app/providers.py` の枠に入らないため、
  鍵を借りる先が無く、ここだけ鍵を自分で持つ。絵と動画は `/flows` の口で預かっている
  他社のモデルを回す(API から頼むには Pro プラン以上が要る)。
- OpenAI —— 外部。絵(gpt-image)に加えて動画(Sora)・読み上げ・文字起こし。

動画と声で層の形が増えたわけではない(`〜Request` → `Generated〜`)が、
動画はどの相手も非同期で、頼んだ時点では id しか返らない —— 待ち方は
`_await_remote` に 1 つだけ置いてある。
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import io
import json
import logging
import os
import random
import wave
from dataclasses import dataclass

import httpx
from fastapi import HTTPException

from app import media_providers, settings_store

log = logging.getLogger("chiezo.media")

# 1 枚あたりの上限。GPU でも SDXL は数秒〜数十秒かかる(混んでいれば待たされる)。
GENERATE_TIMEOUT = float(os.environ.get("CHIEZO_IMAGE_TIMEOUT", "300") or 300)


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
    """音を 1 つ作る頼み。効果音と曲を `sound` で分ける —— 相手によっては口が別で、
    自前の GPU でも読むチェックポイントが別物になる。"""

    prompt: str
    sound: str = media_providers.SOUND_SFX
    # 0 は「相手に任せる」。長さを指定できない相手(Lyria)では常に無視される
    seconds: float = 0.0
    # 歌詞。空なら器楽として頼む(ゲームの BGM は歌が入ると台詞と喧嘩する)
    lyrics: str = ""
    negative: str = ""
    seed: int = 0
    model: str = ""
    steps: int = 50
    # 繋いで鳴らせる素材にするか。効くのは ElevenLabs の効果音だけ
    loop: bool = False


@dataclass(frozen=True)
class GeneratedAudio:
    data: bytes
    mime: str
    seed: int
    model: str
    # 実際の長さ。頼んだ秒数と一致するとは限らない(相手が決める場合がある)ので、
    # 分からなければ 0 を入れる —— 嘘の数字を記録するより空のほうがよい。
    seconds: float = 0.0


# 長さを頼まなかったときの既定。効果音は短く、曲はひと回し。
DEFAULT_SECONDS = {media_providers.SOUND_SFX: 6.0, media_providers.SOUND_MUSIC: 30.0}


def resolve_seconds(spec: media_providers.MediaProvider, sound: str, seconds: float) -> float:
    """頼む長さを決める。上限を超えたら切り詰める(相手に断られるより手前で丸める)。

    上限が 0 の相手は「長さを指定できない」ので 0 を返す —— Lyria は尺がモデルで
    決まっていて、秒数を渡す口そのものが無い。
    """
    limit = media_providers.max_seconds_of(spec, sound)
    if limit <= 0:
        return 0.0
    want = seconds if seconds > 0 else DEFAULT_SECONDS.get(sound, 6.0)
    return max(1.0, min(want, limit))


def parse_size(size: str) -> tuple[int, int]:
    """`1024x1536` を画素に直す。8 の倍数に丸める(拡散モデルの制約)。"""
    try:
        width, height = (int(v) for v in size.lower().split("x", 1))
    except (ValueError, AttributeError):
        raise HTTPException(400, {"error": f"サイズの書き方が違います: {size}(例 1024x1024)"}) from None
    if not (256 <= width <= 2048 and 256 <= height <= 2048):
        raise HTTPException(400, {"error": f"サイズは 256〜2048 の範囲にしてください: {size}"})
    return width - width % 8, height - height % 8


def credential_of(spec: media_providers.MediaProvider) -> str:
    """その相手の鍵。借り先が決まっていればそちらから読む(鍵を 2 か所に持たない)。"""
    if spec.credential == media_providers.CRED_NONE:
        return ""
    source = spec.credential_from or spec.id
    return (settings_store.load(source).credential or "").strip()


# 鍵が無いときの理由。状態(403)と認証(401)を出し分けるために、文字列を定数で持つ
# —— 「鍵を入れれば直る」と「画面で有効にすれば直る」は、次にすることが違う。
NO_CREDENTIAL = "鍵が未登録"


def unusable_reason(spec: media_providers.MediaProvider) -> str:
    """使えない理由。使えるなら空。画面と道具で同じ判定を使う(食い違わせない)。

    無効にしてある相手には、絵も音も作らせない。 鍵を持っている相手を
    止めたのに片方だけ動き続けるのは、止めたつもりの人にとって事故になる。
    元栓(「答える」層)が停止中なら、相手によらず全部止める。

    理由は画面の 1 行に収まる短さにする。 管理画面では相手ごとに 1 行で、
    その行の「できること」の欄にそのまま出るため。
    """
    if not settings_store.answer_enabled():
        return "「答える」層が停止中"
    # on/off は相手ごとに 1 つ。 自分の行を持つ相手(自前の GPU・ElevenLabs)は
    # 自分の設定を、鍵を借りている相手は借り先の設定を見る
    if spec.owns_toggle:
        if not settings_store.load(spec.id).enabled:
            return "無効(この行で有効にする)"
    elif spec.credential_from and not settings_store.load(spec.credential_from).enabled:
        return "無効(この行で有効にする)"
    if spec.credential == media_providers.CRED_REQUIRED and not credential_of(spec):
        return NO_CREDENTIAL
    return ""


# 相手の応答本文をどこまで返すか。300 字では足りなかった。 429 のとき、
# どの枠が尽きたか(`Quota exceeded for metric: … limit: 0`)は前置きの後ろに来るので、
# ちょうど切れて「枠が無い」のか「使い切った」のか分からなかった —— 実際に 2 度踏んだ。
# 鍵は本文ではなくヘッダで送っているので、本文を長めに返しても秘密は載らない。
DETAIL_CHARS = 600


def remote_error(spec: media_providers.MediaProvider, res, what: str) -> HTTPException:
    """相手が返したエラーを、次の一手が分かる形にして返す。

    鍵は載せない(ログにも画面にも)。載せるのは状態と本文の頭だけ。
    """
    log.warning("%s %s error %s: %s", spec.id, what, res.status_code, res.text[:DETAIL_CHARS])
    detail = {
        "error": f"{spec.label} が {res.status_code} を返しました",
        "detail": res.text[:DETAIL_CHARS],
    }
    if res.status_code == 429:
        # 「使い切った」と「そもそも無い」は違う。 無料枠に含まれないモデルだと、
        # 待っても直らない(実際に Lyria がこれだった)。
        detail["hint"] = (
            "枠が足りません。時間をおいても直らない場合、そのモデルが無料枠に"
            "含まれていない可能性があります(本文の metric と limit を確認してください)"
        )
    return HTTPException(502, detail)


def _client(timeout: float) -> httpx.AsyncClient:
    """テストが差し替える口(`httpx.MockTransport` を挿す)。"""
    return httpx.AsyncClient(timeout=timeout)


# ---- 動画と声で足りるもの ----------------------------------------------------
#
# 層の形は絵・音と同じ(`〜Request` を受け取り `Generated〜` を返す。保存も記録も
# しない)。増えたのは 2 つだけ:
#
# - 動画は待ち時間の桁が違う。 絵は数十秒だが、動画は 8 秒ぶんで数分かかる。
#   絵と同じ上限で待つと、出来上がる前にこちらが切って GPU も枠も捨てることになる。
# - 文字起こしだけは job にならない。 送る側が既に音を持っていて、返るのは文字
#   (数 KB)なので、置き場も掃除も要らない —— その場で返すほうが呼ぶ側も楽になる。

# 動画 1 本あたりの上限。絵の 4 倍取ってある(Sora の 12 秒・Veo の 8 秒とも
# 実測で数分かかる)。相手側の待ち行列に入ると更に延びるので、環境変数で伸ばせる。
VIDEO_TIMEOUT = float(os.environ.get("CHIEZO_VIDEO_TIMEOUT", "1200") or 1200)


def timeout_for(kind: str) -> float:
    """その kind を待つ上限。動画だけ桁が違うので、ここで 1 つに寄せる。

    `media._reap_stale` が「もう誰も面倒を見ていない job」を畳む基準にも使うので、
    生成側と後始末側で別々の数字を持たせない。
    """
    return VIDEO_TIMEOUT if kind == media_providers.KIND_VIDEO else GENERATE_TIMEOUT


@dataclass(frozen=True)
class VideoRequest:
    """動画を 1 本作る頼み。尺は「相手が受け付ける値」しか入らない
    (`media.create_job` が一覧と突き合わせて弾いてある)。"""

    prompt: str
    negative: str = ""
    size: str = "1280x720"
    # 0 は「尺を指定できない相手」。それ以外は相手の一覧にある値
    seconds: float = 0.0
    seed: int = 0
    model: str = ""
    steps: int = 20
    # 音も一緒に作らせるか(効くのは Veo 系だけ。自前の GPU は無音)
    audio: bool = True


@dataclass(frozen=True)
class GeneratedVideo:
    data: bytes
    mime: str
    seed: int
    model: str
    seconds: float = 0.0


@dataclass(frozen=True)
class SpeechRequest:
    """文章を読み上げる頼み。`prompt` が読み上げる文章そのもので、絵や音のような
    「こういうものを作って」という指示ではない —— job の列を分けずに済ませるため
    同じ名前にしてあるが、中身の性格が違う。"""

    prompt: str
    # 声。空なら相手の既定(ElevenLabs は登録済みの先頭、他は決め打ちの先頭)
    voice: str = ""
    model: str = ""
    # 読む速さ。1.0 が等倍
    speed: float = 1.0
    # 言語(ISO 639-1)。空なら相手が文章から見当をつける
    language: str = ""
    # 読み方の指示(効くのは OpenAI の gpt-4o-mini-tts だけ)
    instructions: str = ""
    seed: int = 0


@dataclass(frozen=True)
class GeneratedSpeech:
    data: bytes
    mime: str
    seed: int
    model: str
    seconds: float = 0.0
    # 使われた声。頼んだ名前とは限らない(空で頼めば相手の既定が入る)
    voice: str = ""


@dataclass(frozen=True)
class TranscribeRequest:
    """文字起こしの頼み。こちらだけ入力がバイト列なので、他と口の形が違う。"""

    data: bytes
    filename: str = "audio"
    mime: str = "application/octet-stream"
    model: str = ""
    language: str = ""


@dataclass(frozen=True)
class Transcript:
    text: str
    model: str
    # 相手が見当をつけた言語。分からなければ空(嘘の値を入れない)
    language: str = ""


def resolve_video_seconds(spec: media_providers.MediaProvider, seconds: float) -> float:
    """頼む尺を決める。頼まれなければ一覧のいちばん短いもの。

    音の `resolve_seconds` と違って丸めない —— 動画は受け付ける値が飛び飛びなので、
    近い値へ寄せると「6 秒で頼んだのに 8 秒が返る」になる。範囲の検査は
    `media.create_job` が先に済ませてあり、ここへ来るのは通った値だけ。

    既定を最短にしてあるのは、間違えたときの損が小さいほうを既定にするため
    (動画は 1 本で数分と数十 MB を使う)。
    """
    if not spec.video_seconds:
        return 0.0
    return seconds if seconds > 0 else min(spec.video_seconds)


def nearest_size(sizes: tuple[str, ...], size: str) -> str:
    """並んでいる中から、縦横比が近く、面積の近いものを選ぶ。

    決まった組み合わせしか取らない相手(OpenAI・ElevenLabs)向け。
    引き伸ばしより解像度違いのほうが素材として使いやすいので、比率を優先する。
    """
    width, height = parse_size(size)
    ratio, area = width / height, width * height

    def distance(candidate: str) -> tuple[float, float]:
        cw, ch = (int(v) for v in candidate.split("x"))
        return abs(cw / ch - ratio), abs(cw * ch - area)

    return min(sizes, key=distance)


def _wide_aspect(size: str) -> str:
    """動画の比率。横か縦かの 2 つだけ —— 動画のモデルはどれもこの 2 つ
    (と正方形)しか取らないので、絵の `_aspect_of` のように細かく寄せられない。
    """
    width, height = parse_size(size)
    return "16:9" if width >= height else "9:16"


def _video_resolution(size: str) -> str:
    """相手の語彙(720p / 1080p)へ寄せる。短辺で決める(縦長でも同じ言葉になる)。"""
    width, height = parse_size(size)
    return "1080p" if min(width, height) >= 1080 else "720p"


def _require_key(spec: media_providers.MediaProvider, what: str) -> str:
    """鍵を取り出す。無ければ 401 と、どこへ入れるかを返す。

    401(鍵が無い)と 403(無効)を出し分ける約束は `unusable_reason` と同じで、
    受け取った側の次の一手が違うため。
    """
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": f"{spec.label} の API キーが未登録です",
                "hint": "管理画面(/admin の「AI の相手」)で鍵を登録してください"
                f"({what}でも同じ鍵を使います)",
            },
        )
    return key


async def _await_remote(check, *, every: float, timeout: float, what: str):
    """相手の非同期ジョブが仕上がるまで覗きに行く。`check()` は `(done, 値)` を返す。

    動画の相手はどこも非同期(頼んだ時点では id だけが返る)なので、待ち方は
    ここに 1 つだけ置く。間隔を相手ごとに変えられるようにしてあるのは、
    詰めて聞くと断られる相手がいるため(ElevenLabs は動画で 10 秒に 1 回まで)。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        done, value = await check()
        if done:
            return value
        if loop.time() > deadline:
            raise HTTPException(
                504,
                {
                    "error": f"{what}が {timeout:.0f} 秒で終わりませんでした",
                    "hint": "CHIEZO_VIDEO_TIMEOUT を伸ばすか、軽いモデルを選んでください",
                },
            )
        await asyncio.sleep(every)


# ---- ComfyUI ---------------------------------------------------------------
#
# グラフはここに持つ。 ComfyUI は画面で組んだワークフロー(ノードの JSON)を
# そのまま受け取る作りで、「プロンプトとサイズだけ渡す」口は無い。テンプレを 1 つ持ち、
# 差し込む値(モデル・プロンプト・除外プロンプト・サイズ・seed・ステップ)だけを埋める。
# ノード番号は文字列(ComfyUI の約束)。
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
        available = await comfy_image_models(url)
        if not available:
            raise HTTPException(
                502,
                {
                    "error": "ComfyUI に絵のチェックポイントが 1 つも置かれていません",
                    "hint": "models/checkpoints に .safetensors を置いてください"
                    "(名前に audio か ace を含むものは音のモデルとして扱います)",
                },
            )
        model = available[0]

    data, _ = await _comfy_execute(
        url, _comfy_graph(req, model, width, height, seed), "images", "画像"
    )
    return GeneratedImage(data, "image/png", seed, model)


async def _comfy_execute(
    url: str, graph: dict, output_key: str | tuple[str, ...], what: str,
    timeout: float = 0.0,
) -> tuple[bytes, str]:
    """グラフを投げて、出来た 1 つぶんのバイト列とファイル名を持ち帰る。

    絵と音と動画で違うのは出力の入れ物の名前だけ(`images` / `audio` / `videos`)
    なので、投げ方・待ち方・受け取り方はここに 1 つだけ置く。
    名前は複数受け取れる —— 動画の入れ物の名前は ComfyUI の版で変わるため
    (`videos` / `gifs` / `images`)、決め打ちにすると版が上がっただけで
    「返しませんでした」になる。

    `timeout` は待つ上限(0 なら絵と同じ)。動画だけ桁が違うので外から渡せる。
    """
    timeout = timeout or GENERATE_TIMEOUT
    keys = (output_key,) if isinstance(output_key, str) else tuple(output_key)
    async with _client(timeout) as client:
        queued = await client.post(f"{url}/prompt", json={"prompt": graph})
        if queued.status_code >= 400:
            # ComfyUI はグラフの不備を本文で教えてくれる(ノード名の綴り違い等)
            raise HTTPException(502, {"error": "ComfyUI がグラフを受け付けませんでした",
                                      "detail": queued.text[:500]})
        prompt_id = queued.json().get("prompt_id")
        if not prompt_id:
            raise HTTPException(502, {"error": "ComfyUI が prompt_id を返しませんでした"})

        # 完了は履歴で見る。 進捗の WebSocket もあるが、こちらは 1 つぶんの
        # 出来上がりだけが要るので、素の HTTP で足りる
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            history = await client.get(f"{url}/history/{prompt_id}")
            entry = history.json().get(prompt_id) if history.status_code < 400 else None
            if entry and entry.get("outputs"):
                break
            if entry and (entry.get("status", {}).get("status_str") == "error"):
                raise HTTPException(502, {"error": "ComfyUI の生成が失敗しました",
                                          "detail": json.dumps(entry.get("status"))[:500]})
            if asyncio.get_running_loop().time() > deadline:
                raise HTTPException(504, {"error": f"ComfyUI が {timeout:.0f} 秒で終わりませんでした"})
            await asyncio.sleep(1.0)

        found = [
            item
            for output in entry["outputs"].values()
            for key in keys
            for item in output.get(key, [])
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
# サイズは画素ではなく「比率 + 段階」なので、こちらの `幅x高さ` から比率へ寄せる。
# 相手が受け付ける比率(Gemini)。近いものへ寄せるので、細かく並べるほど元の
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


def _aspect_of(size: str, allowed: tuple[str, ...] = ()) -> str:
    """`size` にいちばん近い縦横比の名前。

    `allowed` を渡すと、その中からだけ選ぶ。**相手が受け付ける比は限られている**
    ので、こちらの表から近いものを選んで送ると 422 で断られる
    (ElevenLabs は 2:3 を受け付けず、`3:4` か `9:16` しか無い)。
    """
    width, height = parse_size(size)
    ratio = width / height
    table = _ASPECTS if not allowed else tuple(p for p in _ASPECTS if p[1] in allowed)
    if not table:
        table = _ASPECTS
    return min(table, key=lambda pair: abs(pair[0] - ratio))[1]


async def _gemini_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "Gemini の API キーが未登録です",
                "hint": "管理画面(/admin の「AI の相手」)で Gemini の鍵を登録してください"
                "(画像生成でも同じ鍵を使います)",
            },
        )

    model = req.model or spec.models[0]
    body = {
        "model": model,
        "input": [{"type": "text", "text": req.prompt}],
        # JPEG しか受け付けない。 png を頼むと 400 が返る
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
        raise remote_error(spec, res, "image")

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

    # seed は返らない。 相手が受け付けないので、こちらで振った値を記録だけしておく
    # (同じ seed で頼み直しても同じ絵にはならない —— 再現できるのは ComfyUI 側だけ)
    return GeneratedImage(base64.b64decode(data), "image/jpeg", seed, model)


# ---- OpenAI(gpt-image)------------------------------------------------------
#
# サイズは決まった組み合わせしか取らない(自由な画素数は投げられない)ので、
# 頼まれた `幅x高さ` から縦横比が近く、面積の近いものを選ぶ。
# 応答は base64 のみ(URL は返らない)。
def _openai_size(spec: media_providers.MediaProvider, size: str) -> str:
    # 比率を優先し、同じくらいなら面積が近いほうを選ぶ(引き伸ばしより解像度違いのほうが軽い)
    return nearest_size(spec.sizes, size)


async def _openai_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "OpenAI の API キーが未登録です",
                "hint": "管理画面(/admin の「AI の相手」)で OpenAI の鍵を登録してください"
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
        # 鍵は載せない。理由の頭だけ返す。403 が返ったら組織の本人確認を疑う ——
        # OpenAI の API は一部のモデルで開発者コンソールでの本人確認を求めることがある
        # (ChatGPT / Codex のサブスクとは別系統なので、あちらで使えていても関係しない)
        raise remote_error(spec, res, "image")

    data = next((item.get("b64_json") for item in res.json().get("data", []) if item.get("b64_json")), None)
    if not data:
        raise HTTPException(502, {"error": "OpenAI が画像を返しませんでした"})

    # seed は受け付けない。 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedImage(base64.b64decode(data), "image/png", seed, model)


# ---- CLI ブリッジ(サブスクの枠で描く)----------------------------------------
#
# ブリッジの `/v1/images/generations` に投げる。中では CLI が内蔵の画像ツールを回して
# PNG を書き、ブリッジがそれを base64 で返す。Codex と Antigravity が同じ形なので、
# 実装は 1 つで足りる(違うのは URL と、記録に残すモデル名だけ)。
# 鍵はこちらに無い —— Codex は管理画面で登録された auth.json、
# Antigravity はコンテナ内のサインイン結果を、ブリッジ側が読む。

# 記録に残すモデル名。相手が決めるので、こちらは名前しか知らない。
BRIDGE_IMAGE_MODELS = {"codex": "gpt-image-2", "antigravity": "antigravity-imagegen"}


async def _bridge_image_generate(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/images/generations",
            json={"prompt": req.prompt, "size": req.size, "n": 1},
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "image")

    data = next(
        (item.get("b64_json") for item in res.json().get("data", []) if item.get("b64_json")),
        None,
    )
    if not data:
        raise HTTPException(502, {"error": f"{spec.label} が画像を返しませんでした"})

    # seed は受け付けない。 記録だけしておく(再現できるのは ComfyUI 側だけ)
    model = BRIDGE_IMAGE_MODELS.get(spec.id, spec.id)
    return GeneratedImage(base64.b64decode(data), "image/png", seed, model)


# ---- ComfyUI(音)-----------------------------------------------------------
#
# 音も絵と同じ「グラフを投げる口」で作る。違うのは **チェックポイントの系統でグラフが
# 変わる**こと:
#
# - Stable Audio Open … 効果音・短い素材向き。text encoder(T5)を別に置く必要が
#   あり、`CLIPLoader`(type=stable_audio)で読む
# - ACE-Step … 曲向き。model / clip / vae が 1 つに入った all-in-one で、
#   歌詞を受け取る専用の encode ノード(`TextEncodeAceStepAudio`)を使う
#
# どちらかは名前で見分ける。 ComfyUI に「このチェックポイントは何の系統か」を
# 聞く口は無く(読み込んで初めて分かる)、GPU にモデルを載せてから間違いに気づくのは高い。
# 名前は置く人が決めるので確実ではないが、外したときは `model` で名指しできる。
_ACE_HINT = "ace"
_AUDIO_HINTS = ("audio", "ace")


def _is_ace(name: str) -> bool:
    return _ACE_HINT in name.lower()


def is_audio_checkpoint(name: str) -> bool:
    """音のチェックポイントらしい名前か(絵のものと同じ置き場に混ざっている)。"""
    return any(hint in name.lower() for hint in _AUDIO_HINTS)


async def comfy_image_models(url: str, timeout: float = 5.0) -> list[str]:
    """置いてある絵のチェックポイント。

    音のものを混ぜない。 置き場が同じ(`models/checkpoints`)なので、素の一覧には
    音のモデルも並ぶ —— そのまま使うと、モデルを指定しなかった絵の生成が一覧の先頭に
    来た音のモデルを掴む(`ace_step_…` が `sd_xl_…` より前に来る)。読み込んで初めて
    失敗するので、気づくのは遅い。
    """
    return [name for name in await comfy_models(url, timeout) if not is_audio_checkpoint(name)]


async def comfy_audio_models(url: str, timeout: float = 5.0) -> list[str]:
    """置いてある音のチェックポイント。曲(ACE-Step)を先に並べる。"""
    names = [name for name in await comfy_models(url, timeout) if is_audio_checkpoint(name)]
    return sorted(names, key=lambda name: (not _is_ace(name), name))


def pick_audio_model(names: list[str], sound: str) -> str:
    """その音に向いたチェックポイントを選ぶ。曲は ACE-Step 優先、効果音はその逆。

    どちらしか無ければそれを使う —— 「置いてあるのに使えない」より、
    向いていなくても鳴るほうがよい(向き不向きは出てきた音で分かる)。
    """
    ace = [n for n in names if _is_ace(n)]
    plain = [n for n in names if not _is_ace(n)]
    order = ace + plain if sound == media_providers.SOUND_MUSIC else plain + ace
    return order[0] if order else ""


async def comfy_text_encoders(url: str, timeout: float = 5.0) -> list[str]:
    """`CLIPLoader` に置いてある text encoder。Stable Audio Open にだけ要る
    (ACE-Step は all-in-one なので不要)。"""
    async with _client(timeout) as client:
        res = await client.get(f"{url}/object_info/CLIPLoader")
        res.raise_for_status()
        info = res.json()["CLIPLoader"]["input"]["required"]["clip_name"][0]
    return [str(name) for name in info]


def _comfy_stable_audio_graph(
    req: AudioRequest, model: str, clip: str, seconds: float, seed: int
) -> dict:
    """Stable Audio Open のグラフ。サンプラーの組み合わせは公式のテンプレどおり
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
    """ACE-Step のグラフ。歌詞を渡す口があるのがこちら(空なら器楽として頼む)。

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
        # Stable Audio Open は text encoder を別に読む。無ければグラフごと通らないので、
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
# 絵とまったく同じ `interactions` の口で、違うのは `response_format` だけ。
# 効果音は作れない(Lyria は曲のモデル)ので、頼まれたら断る —— 短い衝突音を
# 頼んで 30 秒の曲が返るほうが、呼んだ側にとっては分かりにくい。
# 尺は指定できない(モデルで決まる。clip = 30 秒ほど、pro = 3 分ほど)。
async def _gemini_audio(
    spec: media_providers.MediaProvider, req: AudioRequest, seed: int
) -> GeneratedAudio:
    key = credential_of(spec)
    if not key:
        raise HTTPException(
            401,
            {
                "error": "Gemini の API キーが未登録です",
                "hint": "管理画面(/admin の「AI の相手」)で Gemini の鍵を登録してください"
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
        raise remote_error(spec, res, "audio")

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

    # seed は受け付けない。 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedAudio(
        base64.b64decode(item["data"]), item.get("mime_type") or "audio/mpeg", seed, model, 0.0
    )


# ---- ElevenLabs(効果音・曲)--------------------------------------------------
#
# 効果音と曲で口が別(`/sound-generation` と `/music`)。返るのは JSON ではなく
# 音のバイト列そのものなので、失敗したときだけ本文が JSON になる。
# 鍵はこの相手のもの(会話ができないので借り先が無い)。
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
                "hint": "管理画面(/admin の「AI の相手」)で ElevenLabs の鍵を"
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
        raise remote_error(spec, res, "audio")
    if not res.content:
        raise HTTPException(502, {"error": "ElevenLabs が音を返しませんでした"})

    # seed は効果音の口には無い。 記録だけしておく(再現できるのは ComfyUI 側だけ)
    return GeneratedAudio(res.content, "audio/mpeg", seed, model, seconds)


# ---- ComfyUI(動画)----------------------------------------------------------
#
# 絵や音とモデルの置き場が違う。 動画のモデル(Wan)は UNet 単体で配られるので
# `models/diffusion_models` に置き、text encoder(umt5)と VAE を別に読む ——
# `CheckpointLoaderSimple` の一覧には出てこない。
#
# 対応するのは Wan 系だけ。 HunyuanVideo や LTX-Video はグラフの形が違い
# (text encoder が 2 本要る・専用の encode ノードがある)、1 つのテンプレでは賄えない。
# 名前で見分けて、それ以外は GPU を回す前に断る —— 通らないグラフを投げると
# ComfyUI 側は「clip_name が不正」としか言わず、原因に辿り着けない。
_VIDEO_HINTS = ("wan",)

# Wan の刻み。フレーム数は 4n+1 でなければ通らない(潜在空間が 4 フレームを
# 1 つにまとめるため)ので、秒数からフレーム数を作るときに必ず丸める。
COMFY_VIDEO_FPS = 16


def is_video_model(name: str) -> bool:
    """動画のモデルか。名前で見分ける —— `models/diffusion_models` には絵の UNet
    (FLUX 等)も同居するので、そのまま並べると絵のモデルで動画を作ろうとする。
    """
    lowered = name.lower()
    return any(hint in lowered for hint in _VIDEO_HINTS)


def comfy_video_length(seconds: float) -> int:
    """秒数 → フレーム数。4n+1 に丸める(Wan はそれ以外を受け付けない)。

    近いほうへ寄せる。 切り捨てると 2.0 秒が 29 フレーム(1.81 秒)になり、
    頼んだ尺より短いものが黙って返る —— 素材として並べたときに気づく類の食い違いなので、
    半端なら伸ばすほうを採る。
    """
    frames = max(1, round(seconds * COMFY_VIDEO_FPS))
    return max(5, (frames + 1) // 4 * 4 + 1)


async def _comfy_names(url: str, node: str, field: str, timeout: float = 5.0) -> list[str]:
    """`/object_info` の 1 ノードから、選べる名前の一覧を取る。

    動画は読むものが 3 つ(UNet・text encoder・VAE)あり、**どれが欠けても
    グラフごと通らない**ので、同じ形の問い合わせを 1 つにまとめてある。
    """
    async with _client(timeout) as client:
        res = await client.get(f"{url}/object_info/{node}")
        res.raise_for_status()
        info = res.json()[node]["input"]["required"][field][0]
    return [str(name) for name in info]


async def comfy_video_models(url: str, timeout: float = 5.0) -> list[str]:
    """置いてある動画のモデル(`models/diffusion_models`)。"""
    names = await _comfy_names(url, "UNETLoader", "unet_name", timeout)
    return [name for name in names if is_video_model(name)]


def _comfy_video_graph(
    req: VideoRequest, unet: str, clip: str, vae: str,
    width: int, height: int, length: int, seed: int,
) -> dict:
    """Wan の text-to-video。絵のグラフと骨格は同じで、違うのは潜在空間が
    フレーム方向を持つこと(`EmptyHunyuanLatentVideo`)と、出口で連番の画を
    動画に綴じること(`CreateVideo` → `SaveVideo`)。
    """
    return {
        "1": {"class_type": "UNETLoader",
              "inputs": {"unet_name": unet, "weight_dtype": "default"}},
        # type は "wan"(umt5 を Wan の作法で読む指定。ここを外すと通らない)
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": clip, "type": "wan"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": vae}},
        "4": {"class_type": "CLIPTextEncode", "inputs": {"text": req.prompt, "clip": ["2", 0]}},
        "5": {"class_type": "CLIPTextEncode", "inputs": {"text": req.negative, "clip": ["2", 0]}},
        "6": {"class_type": "EmptyHunyuanLatentVideo",
              "inputs": {"width": width, "height": height, "length": length, "batch_size": 1}},
        # Wan は shift を上げないと動きが出ない(公式のテンプレも 8.0)
        "7": {"class_type": "ModelSamplingSD3", "inputs": {"model": ["1", 0], "shift": 8.0}},
        "8": {"class_type": "KSampler",
              "inputs": {"model": ["7", 0], "positive": ["4", 0], "negative": ["5", 0],
                         "latent_image": ["6", 0], "seed": seed, "steps": req.steps,
                         "cfg": 6.0, "sampler_name": "uni_pc", "scheduler": "simple",
                         "denoise": 1.0}},
        "9": {"class_type": "VAEDecode", "inputs": {"samples": ["8", 0], "vae": ["3", 0]}},
        "10": {"class_type": "CreateVideo",
               "inputs": {"images": ["9", 0], "fps": COMFY_VIDEO_FPS}},
        "11": {"class_type": "SaveVideo",
               "inputs": {"video": ["10", 0], "filename_prefix": "chiezo/video",
                          "format": "mp4", "codec": "h264"}},
    }


async def _comfy_video(
    spec: media_providers.MediaProvider, req: VideoRequest, seed: int
) -> GeneratedVideo:
    url = media_providers.url_of(spec)
    width, height = parse_size(req.size)
    seconds = resolve_video_seconds(spec, req.seconds)

    model = req.model
    if not model:
        available = await comfy_video_models(url)
        model = available[0] if available else ""
    if not model:
        raise HTTPException(
            502,
            {
                "error": "ComfyUI に動画のモデルが置かれていません",
                "hint": "models/diffusion_models に wan2.2_t2v_*.safetensors を置いてください"
                "(名前で系統を見分けるので、名前は変えずに置くこと)。"
                "対応しているのは Wan 系だけです",
            },
        )

    # 読むものが 3 つあり、どれが欠けてもグラフごと通らない。 GPU を回す前に
    # 揃っているか確かめる —— ComfyUI 側のエラーは「名前が不正」としか出ないので、
    # 何を置けばよいのかが分からない。
    clips = await _comfy_names(url, "CLIPLoader", "clip_name")
    clip = next((name for name in clips if "umt5" in name.lower()), "")
    vaes = await _comfy_names(url, "VAELoader", "vae_name")
    vae = next((name for name in vaes if "wan" in name.lower()), "")
    missing = [
        label for label, found, where in (
            ("text encoder(umt5)", clip, "models/text_encoders"),
            ("VAE(wan)", vae, "models/vae"),
        ) if not found
    ]
    if missing:
        raise HTTPException(
            502,
            {
                "error": f"{model} を使うには {' と '.join(missing)} が要ります",
                "hint": "models/text_encoders に umt5_xxl_*.safetensors、"
                "models/vae に wan_2.1_vae.safetensors を置いてください",
            },
        )

    length = comfy_video_length(seconds)
    graph = _comfy_video_graph(req, model, clip, vae, width, height, length, seed)
    # 出口の入れ物の名前は ComfyUI の版で変わる(videos / gifs / images)ので、
    # 決め打ちにせず候補を並べて拾う —— 名前が変わっただけで「返しませんでした」に
    # なるのは、原因の分からない失敗になる。
    data, _name = await _comfy_execute(
        url, graph, ("videos", "gifs", "images"), "動画", timeout=VIDEO_TIMEOUT
    )
    return GeneratedVideo(data, "video/mp4", seed, model, length / COMFY_VIDEO_FPS)


# ---- Gemini(動画)-----------------------------------------------------------
#
# 口が 2 通りに分かれる。
#
# - Omni Flash …… 絵や曲とまったく同じ `interactions` の口。1 往復で返る
# - Veo …… `:predictLongRunning` に投げて、オペレーションが `done` になるまで
#   覗きに行き、最後に出来た動画の URI を鍵つきで取りに行く
#
# どちらかは名前で見分ける。 尺を渡す口があるのは Veo だけなので、
# ここを取り違えると「秒数を渡したのに無視された」が黙って起きる。
def _is_veo(name: str) -> bool:
    return name.lower().startswith("veo")


async def _gemini_video(
    spec: media_providers.MediaProvider, req: VideoRequest, seed: int
) -> GeneratedVideo:
    key = _require_key(spec, "動画の生成")
    model = req.model or spec.video_models[0]
    base = media_providers.url_of(spec)
    if _is_veo(model):
        return await _gemini_veo(spec, req, seed, key, model, base)

    body = {
        "model": model,
        "input": [{"type": "text", "text": req.prompt}],
        # base64 で受け取る。 uri で受けるとファイル API を経由して取りに行く手が
        # 増えるだけで、こちらは結局バイト列を保存する
        "response_format": {
            "type": "video",
            "aspect_ratio": _wide_aspect(req.size),
            "delivery": "base64",
        },
    }
    async with _client(VIDEO_TIMEOUT) as client:
        res = await client.post(
            f"{base}/interactions", headers={"x-goog-api-key": key}, json=body
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "video")

    item = next(
        (
            content
            for step in res.json().get("steps", [])
            for content in step.get("content", [])
            if content.get("type") == "video" and content.get("data")
        ),
        None,
    )
    if not item:
        raise HTTPException(502, {"error": "Gemini が動画を返しませんでした"})

    return GeneratedVideo(
        base64.b64decode(item["data"]), item.get("mime_type") or "video/mp4", seed, model, 0.0
    )


async def _gemini_veo(
    spec: media_providers.MediaProvider, req: VideoRequest, seed: int,
    key: str, model: str, base: str,
) -> GeneratedVideo:
    """Veo。頼む・待つ・取りに行くの 3 手に分かれている。"""
    seconds = resolve_video_seconds(spec, req.seconds)
    parameters = {
        "aspectRatio": _wide_aspect(req.size),
        "resolution": _video_resolution(req.size),
        # 文字列で渡す。 数値を渡すと 400 になる(相手の型が string)
        "durationSeconds": str(int(seconds)) if seconds else "8",
    }
    if req.negative:
        parameters["negativePrompt"] = req.negative

    headers = {"x-goog-api-key": key}
    async with _client(VIDEO_TIMEOUT) as client:
        res = await client.post(
            f"{base}/models/{model}:predictLongRunning",
            headers=headers,
            json={"instances": [{"prompt": req.prompt}], "parameters": parameters},
        )
        if res.status_code >= 400:
            raise remote_error(spec, res, "video")
        name = res.json().get("name", "")
        if not name:
            raise HTTPException(502, {"error": "Gemini がオペレーション名を返しませんでした"})

        async def look():
            got = await client.get(f"{base}/{name}", headers=headers)
            if got.status_code >= 400:
                raise remote_error(spec, got, "video")
            body = got.json()
            return bool(body.get("done")), body

        # 20 秒おき。 Veo は最短でも 1 分近くかかるので、詰めて聞いても
        # 相手の負担が増えるだけで早くはならない
        done = await _await_remote(look, every=20.0, timeout=VIDEO_TIMEOUT, what="Veo の生成")
        if error := done.get("error"):
            raise HTTPException(502, {"error": "Veo の生成が失敗しました",
                                      "detail": json.dumps(error, ensure_ascii=False)[:DETAIL_CHARS]})

        samples = (
            done.get("response", {}).get("generateVideoResponse", {}).get("generatedSamples", [])
        )
        uri = next((s.get("video", {}).get("uri") for s in samples if s.get("video")), "")
        if not uri:
            raise HTTPException(502, {"error": "Veo が動画の場所を返しませんでした"})

        # 取りに行くのにも鍵が要る(署名済み URL ではない)
        file = await client.get(uri, headers=headers, follow_redirects=True)
        if file.status_code >= 400:
            raise remote_error(spec, file, "video")

    return GeneratedVideo(file.content, "video/mp4", seed, model, seconds)

# ---- OpenAI(Sora / 動画)-----------------------------------------------------
#
# 頼むのは multipart で、返るのは JSON の job。仕上がったら別の口
# (`/videos/{id}/content`)からバイト列を取りに行く。
# 尺は 4 / 8 / 12 秒しか取らないので、`media.create_job` が一覧と突き合わせて弾く。
def _form(fields: dict) -> dict:
    """文字だけの multipart。httpx は `files=` を渡したときだけ multipart にするので、
    値の無いファイル欄の形(`(None, 値)`)で並べる。
    """
    return {name: (None, str(value)) for name, value in fields.items() if value != ""}


async def _openai_video(
    spec: media_providers.MediaProvider, req: VideoRequest, seed: int
) -> GeneratedVideo:
    key = _require_key(spec, "動画の生成")
    model = req.model or spec.video_models[0]
    seconds = resolve_video_seconds(spec, req.seconds)
    base = media_providers.url_of(spec)
    headers = {"Authorization": f"Bearer {key}"}

    async with _client(VIDEO_TIMEOUT) as client:
        res = await client.post(
            f"{base}/videos",
            headers=headers,
            files=_form({
                "model": model,
                "prompt": req.prompt,
                "size": nearest_size(spec.video_sizes, req.size),
                "seconds": str(int(seconds)) if seconds else "",
            }),
        )
        if res.status_code >= 400:
            raise remote_error(spec, res, "video")
        video_id = res.json().get("id", "")
        if not video_id:
            raise HTTPException(502, {"error": "OpenAI が動画の id を返しませんでした"})

        async def look():
            got = await client.get(f"{base}/videos/{video_id}", headers=headers)
            if got.status_code >= 400:
                raise remote_error(spec, got, "video")
            body = got.json()
            return body.get("status") in ("completed", "failed"), body

        done = await _await_remote(look, every=10.0, timeout=VIDEO_TIMEOUT, what="Sora の生成")
        if done.get("status") == "failed":
            raise HTTPException(
                502,
                {"error": "Sora の生成が失敗しました",
                 "detail": json.dumps(done.get("error"), ensure_ascii=False)[:DETAIL_CHARS]},
            )

        file = await client.get(f"{base}/videos/{video_id}/content", headers=headers,
                                follow_redirects=True)
        if file.status_code >= 400:
            raise remote_error(spec, file, "video")

    return GeneratedVideo(file.content, "video/mp4", seed, model, seconds)


# ---- ElevenLabs(flows / 絵と動画)---------------------------------------------
#
# 自社のモデルではなく他社のモデルを預かっている口(絵は gpt-image / Gemini /
# Seedream、動画は Veo / Seedance)。絵も動画も同じ作りで、違うのはパスと本文の
# 項目だけなので、実装は 1 つで足りる。
#
# どちらも非同期(頼むと id だけが返る)。仕上がると署名済みの `content_url` が
# 生えるので、そこから取りに行く —— 1 時間で切れるので、返ってきたその場で拾う。
# 覗きに行く間隔は相手が決めている(絵は 2 秒、動画は 10 秒に 1 回まで)。
def _elevenlabs_resolution(size: str) -> str:
    """絵の段階(1K / 2K / 4K)。長辺で決める。"""
    width, height = parse_size(size)
    long_side = max(width, height)
    if long_side <= 1024:
        return "1K"
    return "2K" if long_side <= 2048 else "4K"


async def _elevenlabs_flow(
    spec: media_providers.MediaProvider, flow: str, body: dict, *,
    every: float, timeout: float, what: str,
) -> tuple[bytes, str]:
    """flows の口へ頼んで、仕上がったバイト列と MIME を持ち帰る。絵も動画も同じ道。"""
    key = _require_key(spec, f"{what}の生成")
    base = media_providers.url_of(spec)
    headers = {"xi-api-key": key}

    async with _client(timeout) as client:
        res = await client.post(f"{base}/flows/{flow}", headers=headers, json=body)
        if res.status_code >= 400:
            raise remote_error(spec, res, flow)
        generation_id = res.json().get("id", "")
        if not generation_id:
            raise HTTPException(502, {"error": f"ElevenLabs が{what}の id を返しませんでした"})

        async def look():
            got = await client.get(f"{base}/flows/{flow}/{generation_id}", headers=headers)
            if got.status_code >= 400:
                raise remote_error(spec, got, flow)
            found = got.json()
            return found.get("status") in ("completed", "failed"), found

        done = await _await_remote(look, every=every, timeout=timeout,
                                   what=f"ElevenLabs の{what}生成")
        if done.get("status") == "failed":
            raise HTTPException(
                502,
                {"error": f"ElevenLabs の{what}生成が失敗しました",
                 "detail": json.dumps(done, ensure_ascii=False)[:DETAIL_CHARS]},
            )

        url = done.get("content_url", "")
        if not url:
            raise HTTPException(502, {"error": f"ElevenLabs が{what}の場所を返しませんでした"})
        # 署名済みの URL なので鍵は要らない(付けても害は無いが、外のホストへ
        # 出ていくことがあるので載せない)
        file = await client.get(url, follow_redirects=True)
        if file.status_code >= 400:
            raise remote_error(spec, file, flow)

    return file.content, str(done.get("content_mime_type") or "")


# ElevenLabs の画像が受け付ける縦横比。**こちらの表の近いものを送ると断られる**
# (1024x1536 は 2:3 だが、向こうに 2:3 は無く 422 になる)。
_ELEVENLABS_IMAGE_ASPECTS = ("1:1", "3:4", "4:3", "16:9", "9:16")


async def _elevenlabs_image(
    spec: media_providers.MediaProvider, req: ImageRequest, seed: int
) -> GeneratedImage:
    model = req.model or spec.models[0]
    body = {
        "model_id": model,
        "prompt": req.prompt,
        "aspect_ratio": _aspect_of(req.size, _ELEVENLABS_IMAGE_ASPECTS),
        "resolution": _elevenlabs_resolution(req.size),
        # seed を受け付ける数少ない外部の相手(同じ絵を作り直せる)
        "seed": seed,
    }
    try:
        data, mime = await _elevenlabs_flow(
            spec, "image", body, every=2.0, timeout=GENERATE_TIMEOUT, what="画像"
        )
    except HTTPException as err:
        # **seed を受け付けないモデルがある。** 弾かれたら seed を外して投げ直す
        # (gemini-3-pro-image は `extra_forbidden` を返す)。モデルの一覧を
        # 持たずに済むよう、断られてから外す形にしてある —— 向こうに
        # モデルが増えても、こちらを直さなくてよい
        if not _rejects_seed(err):
            raise
        body.pop("seed", None)
        data, mime = await _elevenlabs_flow(
            spec, "image", body, every=2.0, timeout=GENERATE_TIMEOUT, what="画像"
        )
        seed = 0  # 使わなかったので、作り直しの手掛かりとして残さない
    return GeneratedImage(data, mime or "image/png", seed, model)


def _rejects_seed(err: HTTPException) -> bool:
    """「seed は受け付けない」と断られたか。"""
    if err.status_code not in (400, 422):
        return False
    detail = json.dumps(err.detail, ensure_ascii=False) if err.detail else ""
    return "seed" in detail and ("extra_forbidden" in detail or "not permitted" in detail)


async def _elevenlabs_video(
    spec: media_providers.MediaProvider, req: VideoRequest, seed: int
) -> GeneratedVideo:
    model = req.model or spec.video_models[0]
    seconds = resolve_video_seconds(spec, req.seconds)
    body = {
        "model_id": model,
        "prompt": req.prompt,
        "aspect_ratio": _wide_aspect(req.size),
        "resolution": _video_resolution(req.size),
        "generate_audio": req.audio,
    }
    if seconds:
        body["duration_secs"] = int(seconds)
    data, mime = await _elevenlabs_flow(
        spec, "video", body, every=10.0, timeout=VIDEO_TIMEOUT, what="動画"
    )
    return GeneratedVideo(data, mime or "video/mp4", seed, model, seconds)

# ---- 読み上げ(TTS)------------------------------------------------------------
#
# 絵や音と違って「作るもの」が文章で決まっている。 頼むときに要るのは
# プロンプトではなく読み上げる文章と声なので、`SpeechRequest` を別に持っている。
#
# 自前の GPU は相手にできない。 ComfyUI 本体に TTS のノードが無く、あるのは
# 外部の拡張(TTS Audio Suite・F5-TTS 等)だけ —— 何を入れているかでノード名も
# 引数も変わるので、こちらからグラフを組み立てられない。

# 相手に「この形で返して」と頼む書式。mp3 で揃える —— どの相手も出せて、
# ブラウザでそのまま鳴らせる。
_SPEECH_FORMAT = "mp3_44100_128"


def _ensure_wav(data: bytes, mime: str) -> tuple[bytes, str]:
    """生の PCM が返ってきたら WAV の殻をかぶせる。

    Gemini は 24kHz・16bit・モノラルの生 PCM を返すことがある。 そのまま
    `.l16` や `.pcm` として保存すると、拡張子も中身も再生できないファイルになり、
    受け取った側は開くまで気づけない。殻は 44 バイトなので、迷ったら被せるほうがよい。
    """
    lowered = (mime or "").lower()
    if data[:4] == b"RIFF" or not ("l16" in lowered or "pcm" in lowered):
        return data, mime or "audio/wav"

    rate = 24000
    for part in lowered.split(";"):
        if part.strip().startswith("rate="):
            with contextlib.suppress(ValueError):
                rate = int(part.split("=", 1)[1])

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(rate)
        out.writeframes(data)
    return buffer.getvalue(), "audio/wav"


# 声の一覧を覚えておく時間(秒)。管理画面が開くたびに聞きに行かせない ——
# 相手が遅い日に、画面そのものが 10 秒待たされる。声はそう頻繁に増えない。
_VOICES_TTL = 300.0
_voices_cache: dict[str, tuple[float, list[dict]]] = {}


async def elevenlabs_voices(
    spec: media_providers.MediaProvider, timeout: float = 10.0
) -> list[dict]:
    """登録されている声を相手に聞く。人によって中身が違う(既定の声に加えて、
    自分で複製した声が並ぶ)ので、こちらで持たない。

    鍵ごとに覚える。 鍵を差し替えたら別の人の声になるので、鍵をキーにする
    (鍵そのものは持たず、突き合わせにだけ使う)。
    """
    key = credential_of(spec)
    if not key:
        return []

    now = asyncio.get_running_loop().time()
    if (found := _voices_cache.get(key)) and now - found[0] < _VOICES_TTL:
        return found[1]

    async with _client(timeout) as client:
        res = await client.get(
            f"{media_providers.url_of(spec)}/voices", headers={"xi-api-key": key}
        )
    if res.status_code >= 400:
        # 覚えない。 鍵を直した直後に 5 分間「声が無い」と言い続けることになる
        return []

    voices = [
        {"id": str(v.get("voice_id")), "label": str(v.get("name") or v.get("voice_id"))}
        for v in res.json().get("voices", [])
        if v.get("voice_id")
    ]
    _voices_cache[key] = (now, voices)
    return voices


async def _elevenlabs_voice_id(spec: media_providers.MediaProvider, wanted: str) -> str:
    """頼まれた声を id に直す。名前で頼めるようにする —— 画面や道具から見えるのは
    「Rachel」のような名前で、id を控えている人はいない。

    見つからなければそのまま id として渡す(相手が知っていれば通る。こちらが
    一覧を取り損ねただけ、という場合に頼みごと自体を潰さない)。
    """
    voices = await elevenlabs_voices(spec)
    if wanted:
        lowered = wanted.strip().lower()
        for voice in voices:
            if lowered in (voice["id"].lower(), voice["label"].lower()):
                return voice["id"]
        return wanted
    if not voices:
        raise HTTPException(
            502,
            {
                "error": "ElevenLabs に声が 1 つも登録されていません",
                "hint": "ElevenLabs の画面で声を選ぶか、voice に声の名前を指定してください",
            },
        )
    return voices[0]["id"]


async def _elevenlabs_speech(
    spec: media_providers.MediaProvider, req: SpeechRequest, seed: int
) -> GeneratedSpeech:
    key = _require_key(spec, "読み上げ")
    model = req.model or spec.speech_models[0]
    voice = await _elevenlabs_voice_id(spec, req.voice)

    body: dict = {"text": req.prompt, "model_id": model}
    if req.language:
        body["language_code"] = req.language
    if req.speed != 1.0:
        body["voice_settings"] = {"speed": req.speed}

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/text-to-speech/{voice}",
            headers={"xi-api-key": key},
            params={"output_format": _SPEECH_FORMAT},
            json=body,
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "speech")
    if not res.content:
        raise HTTPException(502, {"error": "ElevenLabs が音声を返しませんでした"})

    return GeneratedSpeech(res.content, "audio/mpeg", seed, model, 0.0, voice)


async def _openai_speech(
    spec: media_providers.MediaProvider, req: SpeechRequest, seed: int
) -> GeneratedSpeech:
    key = _require_key(spec, "読み上げ")
    model = req.model or spec.speech_models[0]
    voice = req.voice or spec.voices[0]

    body: dict = {
        "model": model, "input": req.prompt, "voice": voice,
        "response_format": "mp3", "speed": req.speed,
    }
    # 読み方の指示が効くのは gpt-4o-mini-tts だけ。 古い tts-1 に渡すと 400 になる
    if req.instructions and model.startswith("gpt-"):
        body["instructions"] = req.instructions

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/audio/speech",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "speech")
    if not res.content:
        raise HTTPException(502, {"error": "OpenAI が音声を返しませんでした"})

    return GeneratedSpeech(res.content, "audio/mpeg", seed, model, 0.0, voice)


async def _gemini_speech(
    spec: media_providers.MediaProvider, req: SpeechRequest, seed: int
) -> GeneratedSpeech:
    key = _require_key(spec, "読み上げ")
    model = req.model or spec.speech_models[0]
    voice = req.voice or spec.voices[0]

    # 曲(Lyria)と同じ `interactions` の口で、違うのは声の指定が付くことだけ
    body = {
        "model": model,
        "input": req.prompt,
        "response_format": {"type": "audio"},
        "generation_config": {"speech_config": [{"voice": voice}]},
    }
    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/interactions",
            headers={"x-goog-api-key": key},
            json=body,
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "speech")

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
        raise HTTPException(502, {"error": "Gemini が音声を返しませんでした"})

    data, mime = _ensure_wav(base64.b64decode(item["data"]), item.get("mime_type") or "")
    return GeneratedSpeech(data, mime, seed, model, 0.0, voice)


# ---- 文字起こし(STT)----------------------------------------------------------
#
# ここだけ job にしない。 送る側が既に音を持っていて、返るのは文字(数 KB)なので、
# 置き場も掃除も配信も要らない —— その場で返すほうが呼ぶ側の手数が少ない。
# そのぶん口の形も違う(バイト列を送って、文字が返る)。
async def _elevenlabs_transcribe(
    spec: media_providers.MediaProvider, req: TranscribeRequest
) -> Transcript:
    key = _require_key(spec, "文字起こし")
    model = req.model or spec.transcribe_models[0]

    fields: dict = {"model_id": (None, model)}
    if req.language:
        fields["language_code"] = (None, req.language)
    fields["file"] = (req.filename, req.data, req.mime)

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/speech-to-text",
            headers={"xi-api-key": key},
            files=fields,
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "transcribe")

    found = res.json()
    # 多チャンネルの音は `transcripts` に分かれて返る(1 本にまとめて返す)
    if not found.get("text") and found.get("transcripts"):
        text = "\n".join(str(t.get("text") or "") for t in found["transcripts"])
    else:
        text = str(found.get("text") or "")
    return Transcript(text, model, str(found.get("language_code") or ""))


async def _openai_transcribe(
    spec: media_providers.MediaProvider, req: TranscribeRequest
) -> Transcript:
    key = _require_key(spec, "文字起こし")
    model = req.model or spec.transcribe_models[0]

    fields: dict = {"model": (None, model)}
    if req.language:
        fields["language"] = (None, req.language)
    fields["file"] = (req.filename, req.data, req.mime)

    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/audio/transcriptions",
            headers={"Authorization": f"Bearer {key}"},
            files=fields,
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "transcribe")

    return Transcript(str(res.json().get("text") or ""), model, req.language)


# 文字起こしを頼むときの指示。「書き起こしだけ」と言い切る —— ふつうの Gemini に
# 音を渡す形なので、言わないと要約や感想が混じる(専用のモデルではない)。
_TRANSCRIBE_INSTRUCTION = (
    "この音声を一字一句そのまま書き起こしてください。"
    "要約・翻訳・注釈・話者の推測は入れず、聞こえた言葉だけを出力してください。"
)


async def _gemini_transcribe(
    spec: media_providers.MediaProvider, req: TranscribeRequest
) -> Transcript:
    key = _require_key(spec, "文字起こし")
    model = req.model or spec.transcribe_models[0]

    body = {
        "model": model,
        "input": [
            {"type": "audio", "mime_type": req.mime,
             "data": base64.b64encode(req.data).decode("ascii")},
            {"type": "text", "text": _TRANSCRIBE_INSTRUCTION},
        ],
    }
    async with _client(GENERATE_TIMEOUT) as client:
        res = await client.post(
            f"{media_providers.url_of(spec)}/interactions",
            headers={"x-goog-api-key": key},
            json=body,
        )
    if res.status_code >= 400:
        raise remote_error(spec, res, "transcribe")

    parts = [
        str(content.get("text") or "")
        for step in res.json().get("steps", [])
        if step.get("type") == "model_output"
        for content in step.get("content", [])
        if content.get("type") == "text"
    ]
    return Transcript("".join(parts).strip(), model, req.language)


# ---- 相手の登録表 ------------------------------------------------------------
#
# kind ごとに 1 つ。 「その相手に頼めるか」は `media_providers.kinds` にも
# 書いてあるが、実装があるかはここが正 —— 表に載っていない相手を選ばれたら
# 404 にする(`_ready`)。
GENERATORS = {
    "comfyui": _comfy_generate,
    "codex": _bridge_image_generate,
    "antigravity": _bridge_image_generate,
    "gemini": _gemini_generate,
    "openai": _openai_generate,
    "elevenlabs": _elevenlabs_image,
}


AUDIO_GENERATORS = {
    "comfyui": _comfy_audio,
    "gemini": _gemini_audio,
    "elevenlabs": _elevenlabs_audio,
}


VIDEO_GENERATORS = {
    "comfyui": _comfy_video,
    "gemini": _gemini_video,
    "openai": _openai_video,
    "elevenlabs": _elevenlabs_video,
}


# 自前の GPU がいない唯一の表。 ComfyUI 本体に TTS のノードが無いため
# (外部の拡張しか無く、入れたものでノード名も引数も変わる)。
SPEECH_GENERATORS = {
    "gemini": _gemini_speech,
    "openai": _openai_speech,
    "elevenlabs": _elevenlabs_speech,
}


TRANSCRIBERS = {
    "gemini": _gemini_transcribe,
    "openai": _openai_transcribe,
    "elevenlabs": _elevenlabs_transcribe,
}


def _ready(backend: str, table: dict, kind: str) -> media_providers.MediaProvider:
    """相手を引いて、頼める状態かまで確かめる。絵と音で同じ検査を通す。"""
    spec = media_providers.get(backend)
    if spec is None or spec.id not in table:
        raise HTTPException(
            404,
            {
                "error": f"unknown backend: {backend}",
                "backends": [p.id for p in media_providers.all_providers(kind)],
            },
        )

    # 止めてある相手には頼まない。 画面で無効にしたのに道具からは作れてしまう、
    # という食い違いを作らない
    if reason := unusable_reason(spec):
        # 場所を言わない案内は、受け取った側にとって次の一手にならない。
        # 相手ごとに 1 行なので、行き先は 1 つで済む。
        raise HTTPException(
            401 if reason == NO_CREDENTIAL else 403,
            {
                "error": f"{spec.label} は使えません: {reason}",
                "hint": "管理画面(/admin の「AI の相手」)で鍵を登録し、有効にしてください"
                "(話す・絵・音で同じ鍵と on/off を使います)",
            },
        )
    return spec


async def generate(backend: str, req: ImageRequest) -> GeneratedImage:
    """1 枚描く。相手を知らなければ 404(選べる相手つき)。"""
    spec = _ready(backend, GENERATORS, media_providers.KIND_IMAGE)

    # seed は必ず決めてから渡す。0 のまま相手任せにすると、あとで同じ絵を
    # 作り直せない(ゲーム素材は「同じキャラの別ポーズ」を作るので再現性が要る)
    seed = req.seed or random.randint(1, 2**31 - 1)

    return await GENERATORS[spec.id](spec, req, seed)


async def generate_audio(backend: str, req: AudioRequest) -> GeneratedAudio:
    """音を 1 つ作る。頼めない種類は先に断る(Lyria に効果音は頼めない)。"""
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


async def generate_video(backend: str, req: VideoRequest) -> GeneratedVideo:
    """動画を 1 本作る。待つのは分単位(相手の中でも待ち行列に並ぶ)。"""
    spec = _ready(backend, VIDEO_GENERATORS, media_providers.KIND_VIDEO)
    seed = req.seed or random.randint(1, 2**31 - 1)

    return await VIDEO_GENERATORS[spec.id](spec, req, seed)


async def generate_speech(backend: str, req: SpeechRequest) -> GeneratedSpeech:
    """文章を読み上げる。自前の GPU は選べない(TTS のノードが無い)。"""
    spec = _ready(backend, SPEECH_GENERATORS, media_providers.KIND_SPEECH)
    seed = req.seed or random.randint(1, 2**31 - 1)

    return await SPEECH_GENERATORS[spec.id](spec, req, seed)


async def transcribe(backend: str, req: TranscribeRequest) -> Transcript:
    """音を文字にする。job にしない(返るのが文字なので、その場で返す)。

    seed が要らない唯一の口 —— 同じ音を渡せば同じ文字が返る前提の仕事で、
    振り直す余地が無い。
    """
    spec = _ready(backend, TRANSCRIBERS, media_providers.KIND_TRANSCRIBE)

    return await TRANSCRIBERS[spec.id](spec, req)


async def generate_for(kind: str, backend: str, req):
    """job の kind で呼び分ける。`media._run` が中身を知らずに回せるようにするため。

    ここが無いと、進み具合を見る側が「絵か音か動画か」を型で見分けることになり、
    相手を 1 つ足すたびに分岐が増える。
    """
    return await {
        media_providers.KIND_AUDIO: generate_audio,
        media_providers.KIND_VIDEO: generate_video,
        media_providers.KIND_SPEECH: generate_speech,
    }.get(kind, generate)(backend, req)
