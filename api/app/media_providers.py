"""絵と音を作る相手の一覧(URL・表示名・モデル候補はここに決め打ち)。

**「話す相手」(`app/providers.py`)と同じ考え方**で並べる —— URL は相手ごとに 1 つに
決まっていて、ユーザーが選ぶ余地は無い。決まっているものを設定にすると、
書き間違いの余地を増やすだけで得が無い。

**鍵は増やさない。** 外部サービスの相手は、既に「話す相手」で登録してある鍵を流用する
(`credential_from`)—— 同じ Gemini の鍵を 2 か所に入れさせても、片方だけ古くなるだけ。
**例外は「話す相手」に対応が無い相手だけ**(ElevenLabs は会話ができないので借り先が無く、
鍵も on/off も自分で持つ)。

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

# 作れるもの(job の kind と同じ語彙)。
KIND_IMAGE = "image"
KIND_AUDIO = "audio"

# 音の種類。**効果音と曲は同じ「音」でもモデルが別物**なので、頼むときに選ばせる ——
# 相手によっては口そのものが分かれている(ElevenLabs)し、自前の GPU でも
# 置くチェックポイントが違う(Stable Audio Open / ACE-Step)。
SOUND_SFX = "sfx"
SOUND_MUSIC = "music"
SOUNDS = (SOUND_SFX, SOUND_MUSIC)


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
    # **頼まれた画素をそのまま使う相手か。** ComfyUI はその画素で潜在空間を作るので、
    # モデルの学習解像度(SDXL なら 1024)を外れると絵が崩壊する —— それでも生成は
    # 成功として返り、気づけるのは出てきた絵を見たときだけ。そういう相手には
    # sizes 以外を渡さない。外部サービスは自分の語彙へ丸めてくれるので狭めなくてよい。
    exact_sizes: bool = False
    # 鍵を借りる相手(`app/providers.py` の ID)。空なら自前の鍵。
    credential_from: str = ""
    # **自分の on/off を持つか。** 「話す相手」に対応がある相手(外部サービス)は
    # あちらの on/off に従う —— 同じものを 2 か所で切り替えられると、どちらが効いて
    # いるのか画面から読めなくなる。自前の GPU はあちらに出てこないので自分で持つ。
    owns_toggle: bool = False
    # URL を上書きできる環境変数(コンテナ名で辿り着けない相手のための逃げ道)
    url_env: str = ""
    # 画面・一覧に出す順
    order: int = 0
    # **作れるもの。** 絵しか作れない相手・音しか作れない相手があるので、
    # 一覧はこれで絞る(頼めない相手を選ばせない)。
    kinds: tuple[str, ...] = (KIND_IMAGE,)
    # 音のモデル候補の控え(`models` は絵のもの)。相手に聞ける場合はそちらが正。
    audio_models: tuple[str, ...] = ()
    # **音の種類ごとの「頼める長さの上限(秒)」。** 並んでいない種類は頼めない
    # (Lyria は曲のモデルなので効果音は作れない)。**0 は「長さを指定できない」**
    # —— Lyria は尺がモデルで決まっていて、こちらから秒数を渡す口が無い。
    audio_limits: tuple[tuple[str, float], ...] = ()


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
        exact_sizes=True,
        url_env="CHIEZO_IMAGE_URL",
        owns_toggle=True,
        order=0,
        kinds=(KIND_IMAGE, KIND_AUDIO),
        # 音のチェックポイントも `models/checkpoints` に置く(絵と同じ場所)。
        # 何が置いてあるかは相手に聞くので、ここは控えを持たない。
        audio_models=(),
        # 効果音 = Stable Audio Open(既定 47.6 秒)、曲 = ACE-Step。
        # **上限は素材の作りやすさで切ってある** —— 長く頼むほど GPU を占有する。
        audio_limits=((SOUND_SFX, 47.0), (SOUND_MUSIC, 240.0)),
    ),
    MediaProvider(
        id="codex",
        label="Codex CLI(ChatGPT のサブスク枠)",
        # CLI ブリッジ(chiezo-bridge-codex)。**API ではなく ChatGPT のログインで動く**
        url="http://chiezo-bridge-codex:7013/v1",
        # 認証情報はブリッジが持つ(「話す相手」で登録した auth.json をそのまま使う)
        credential=CRED_REQUIRED,
        billing="ChatGPT のサブスクリプション(**画像は文字の 3〜5 倍の速さで枠を食う**)",
        setup="docker-compose.yml の chiezo-bridge-codex のコメントを外して起動し、"
        "管理画面(/admin の「AI の相手」)で Codex を有効にしてください。"
        "画像も同じログイン(ChatGPT のサブスク)で動くので、API キーは要りません。",
        # モデルは Codex の内蔵ツールが決める(いまは gpt-image-2)。指定は受け付けない
        models=(),
        # 画素の指定は「言葉で頼む」形なので、こちらの語彙をそのまま渡す
        sizes=("1024x1024", "1536x1024", "1024x1536"),
        credential_from="codex",
        order=15,
    ),
    MediaProvider(
        id="openai",
        label="OpenAI(gpt-image)",
        url="https://api.openai.com/v1",
        credential=CRED_REQUIRED,
        billing="従量課金(無料枠は無い)",
        setup="管理画面(/admin の「AI の相手」)で OpenAI の API キーを登録してください。"
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
        label="Gemini(画像生成・Lyria 3)",
        # **この URL の直下が interactions**(画像は Gemini の新しい口を使う)。
        url="https://generativelanguage.googleapis.com/v1beta",
        credential=CRED_REQUIRED,
        billing="無料枠(課金を有効にしなければ従量課金は発生しない)",
        setup="管理画面(/admin の「AI の相手」)で Gemini の API キーを登録してください。"
        "画像生成でも同じ鍵を使います。",
        # 速い順。**先頭が既定**(何も選ばなかったときに使われるので、ずらすと黙って変わる)。
        models=("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image", "gemini-3-pro-image"),
        credential_from="gemini",
        order=10,
        kinds=(KIND_IMAGE, KIND_AUDIO),
        # 曲は Lyria 3。**先頭が既定**(clip = 30 秒ほど、pro = 3 分ほど)。
        audio_models=("lyria-3-clip-preview", "lyria-3-pro-preview"),
        # **効果音は作れない**(Lyria は曲のモデル)。**尺も指定できない** ——
        # モデルごとに決まっていて、秒数を渡す口が無いので 0 にしてある。
        audio_limits=((SOUND_MUSIC, 0.0),),
    ),
    MediaProvider(
        id="elevenlabs",
        label="ElevenLabs(効果音・曲)",
        url="https://api.elevenlabs.io/v1",
        credential=CRED_REQUIRED,
        billing="無料枠あり(超えたぶんは従量課金)",
        setup="管理画面(/admin の「AI の相手」)で ElevenLabs の API キーを登録し、"
        "「使う」を押してください。",
        # **この相手だけ鍵を自分で持つ。** 「話す相手」に対応が無い(会話はできない)ので
        # 借りる先が無く、鍵と on/off をここに置くしかない。ComfyUI と同じ扱い。
        credential_from="",
        owns_toggle=True,
        order=30,
        kinds=(KIND_AUDIO,),
        # 効果音と曲で口もモデルも別。**先頭が既定**
        audio_models=("eleven_text_to_sound_v2", "music_v2"),
        # 相手の受け付ける範囲(効果音 0.5〜30 秒、曲 3〜600 秒)。
        audio_limits=((SOUND_SFX, 30.0), (SOUND_MUSIC, 600.0)),
    ),
)

BY_ID = {p.id: p for p in PROVIDERS}


def all_providers(kind: str = "") -> tuple[MediaProvider, ...]:
    """相手の一覧。`kind` を渡すと**それを作れる相手だけ**に絞る。

    絞らないと、絵の一覧に音しか作れない相手が並ぶ(逆も同じ)。
    頼めない相手を選ばせないための絞り込みで、順は `order` のまま。
    """
    found = [p for p in PROVIDERS if not kind or kind in p.kinds]
    return tuple(sorted(found, key=lambda p: p.order))


def sounds_of(spec: MediaProvider) -> tuple[str, ...]:
    """その相手に頼める音の種類(効果音・曲)。"""
    return tuple(sound for sound, _ in spec.audio_limits)


def max_seconds_of(spec: MediaProvider, sound: str) -> float:
    """頼める長さの上限(秒)。**0 は「長さを指定できない相手」**(Lyria)。

    頼めない種類は呼ぶ前に弾く前提なので、見つからなければ 0 を返す。
    """
    return dict(spec.audio_limits).get(sound, 0.0)


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


def default_backend(kind: str = KIND_IMAGE) -> str:
    """相手を指定されなかったときに使う既定。**その kind を作れる一覧の先頭**。

    絵も音も既定は自前の GPU になる —— 外へ出さず、枠も食わないため。
    """
    providers = all_providers(kind)
    return providers[0].id if providers else ""
