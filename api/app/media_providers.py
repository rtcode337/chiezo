"""絵を描く相手の一覧(URL・表示名・モデル候補はここに決め打ち)。

**「話す相手」(`app/providers.py`)と同じ考え方**で並べる —— URL は相手ごとに 1 つに
決まっていて、ユーザーが選ぶ余地は無い。決まっているものを設定にすると、
書き間違いの余地を増やすだけで得が無い。

**鍵は増やさない。** 外部サービスの相手は、既に「話す相手」で登録してある鍵を流用する
(`credential_from`)—— 同じ Gemini の鍵を 2 か所に入れさせても、片方だけ古くなるだけ。

URL だけは `url_env` を持つ相手に限り環境変数で上書きできる。**これは「別の URL を
選べるようにする設定」ではなく、コンテナ名で辿り着けない相手のための逃げ道**である
(GPU は別マシンに置くことが多く、その IP は環境ごとに違って決め打ちにできない)。
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# 認証情報の要りかた(「話す相手」と同じ語彙)。
CRED_REQUIRED = "required"  # 認証情報が無ければ使えない
CRED_NONE = "none"  # 渡すものが無い(LAN 内の自前サーバ)


@dataclass(frozen=True)
class MediaProvider:
    id: str
    label: str
    url: str
    credential: str
    # 使えるようにするまでの手順(画面と道具の説明に出す)
    setup: str
    # 課金の形(「自前」「無料枠」を一目で分かるように)
    billing: str
    # モデル候補の控え。相手に聞ける場合(ComfyUI)はそちらが正。
    models: tuple[str, ...] = ()
    # 頼めるサイズ。**相手ごとに書き方が違う**(ComfyUI は画素、Gemini は比率と段階)ので、
    # ここには「こちらの語彙」で並べ、変換は各バックエンドが受け持つ。
    sizes: tuple[str, ...] = ("1024x1024", "1024x1536", "1536x1024")
    # 鍵を借りる相手(`app/providers.py` の ID)。空なら自前の鍵。
    credential_from: str = ""
    # URL を上書きできる環境変数(コンテナ名で辿り着けない相手のための逃げ道)
    url_env: str = ""
    # 画面・一覧に出す順
    order: int = 0


PROVIDERS: tuple[MediaProvider, ...] = (
    MediaProvider(
        id="comfyui",
        label="ComfyUI(自前の GPU)",
        # compose 同梱の chiezo-image。GPU が別マシンなら CHIEZO_IMAGE_URL で上書きする。
        url="http://chiezo-image:7014",
        credential=CRED_NONE,
        billing="自前(電気代のみ)",
        setup="docker-compose.image.yml を重ねて "
        "`docker compose -f docker-compose.yml -f docker-compose.image.yml --profile image up -d` "
        "で立ち上げてください(NVIDIA の GPU が要ります)。GPU が別のマシンにあるなら、"
        "そちらで ComfyUI を動かして URL を CHIEZO_IMAGE_URL に設定します。",
        # チェックポイントは置いたものによるので、相手(`/object_info`)に聞く。
        models=(),
        url_env="CHIEZO_IMAGE_URL",
        order=0,
    ),
    MediaProvider(
        id="openai",
        label="OpenAI(gpt-image)",
        url="https://api.openai.com/v1",
        credential=CRED_REQUIRED,
        billing="従量課金(無料枠は無い)",
        setup="管理画面(/admin の「話す相手」)で OpenAI の API キーを登録してください。"
        "画像生成でも同じ鍵を使います(話す相手としては off のままで構いません)。",
        models=("gpt-image-2",),
        # **相手が受け付けるのは決まった組み合わせだけ**(自由な画素数は取らない)。
        # ここに並べたものへ、頼まれたサイズから近いものを選ぶ。
        sizes=("1024x1024", "1536x1024", "1024x1536", "2048x2048", "3840x2160", "2160x3840"),
        credential_from="openai",
        order=20,
    ),
    MediaProvider(
        id="gemini",
        label="Gemini(画像生成)",
        # **この URL の直下が interactions**(画像は Gemini の新しい口を使う)。
        url="https://generativelanguage.googleapis.com/v1beta",
        credential=CRED_REQUIRED,
        billing="無料枠(課金を有効にしなければ従量課金は発生しない)",
        setup="管理画面(/admin の「話す相手」)で Gemini の API キーを登録してください。"
        "画像生成でも同じ鍵を使います。",
        # 速い順。**先頭が既定**(何も選ばなかったときに使われるので、ずらすと黙って変わる)。
        models=("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image", "gemini-3-pro-image"),
        credential_from="gemini",
        order=10,
    ),
)

BY_ID = {p.id: p for p in PROVIDERS}


def all_providers() -> tuple[MediaProvider, ...]:
    return tuple(sorted(PROVIDERS, key=lambda p: p.order))


def get(provider_id: str) -> MediaProvider | None:
    return BY_ID.get((provider_id or "").strip().lower())


def url_of(spec: MediaProvider) -> str:
    """その相手の URL。`url_env` を持つ相手だけ、環境変数があればそちらが勝つ。"""
    if spec.url_env:
        override = os.environ.get(spec.url_env, "").strip()
        if override:
            return override.rstrip("/")
    return spec.url


def label_of(provider_id: str) -> str:
    """画面に出す名前。知らない ID はそのまま返す(過去の記録を消さないため)。"""
    spec = get(provider_id)
    return spec.label if spec else provider_id


def default_backend() -> str:
    """相手を指定されなかったときに使う既定。**一覧の先頭**(自前の GPU)。"""
    providers = all_providers()
    return providers[0].id if providers else ""
