"""絵・音・動画・声を扱う相手の一覧(URL・表示名・モデル候補はここに決め打ち)。

「話す相手」(`app/providers.py`)と同じ考え方で並べる —— URL は相手ごとに 1 つに
決まっていて、ユーザーが選ぶ余地は無い。決まっているものを設定にすると、
書き間違いの余地を増やすだけで得が無い。

鍵は増やさない。 外部サービスの相手は、既に「話す相手」で登録してある鍵を流用する
(`credential_from`)—— 同じ Gemini の鍵を 2 か所に入れさせても、片方だけ古くなるだけ。
例外は「話す相手」に対応が無い相手だけ(ElevenLabs は会話の口が「先にエージェントを
作って `agent_id` で話す」形で `app/providers.py` の枠に入らず、借り先が無い ——
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

# 枠の聞き方。 `GET /v1/user/subscription`(鍵だけで引ける)。
# 「話す相手」の USAGE_* と同じ役目だが、あちらは循環参照になるのでここに置く。
USAGE_ELEVENLABS = "elevenlabs"

# 作れるもの(job の kind と同じ語彙)。文字起こしだけは job にならない(即答する)が、
# 「その相手に何を頼めるか」は同じ表で持つ —— 分けると一覧の絞り込みが 2 通りになる。
KIND_IMAGE = "image"
KIND_AUDIO = "audio"
KIND_VIDEO = "video"
KIND_SPEECH = "speech"  # 読み上げ(TTS)
KIND_TRANSCRIBE = "transcribe"  # 文字起こし(STT)

# 頼んで後から引き取るものだけ。文字起こしは送ったその場で文字が返るので入らない。
JOB_KINDS = (KIND_IMAGE, KIND_AUDIO, KIND_VIDEO, KIND_SPEECH)

# 音の種類。効果音と曲は同じ「音」でもモデルが別物なので、頼むときに選ばせる ——
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
    # 頼めるサイズ。相手ごとに書き方が違う(ComfyUI は画素、Gemini は比率と段階)ので、
    # ここには「こちらの語彙」で並べ、変換は各バックエンドが受け持つ。
    sizes: tuple[str, ...] = ("1024x1024", "1024x1536", "1536x1024")
    # 頼まれた画素をそのまま使う相手か。 ComfyUI はその画素で潜在空間を作るので、
    # モデルの学習解像度(SDXL なら 1024)を外れると絵が崩壊する —— それでも生成は
    # 成功として返り、気づけるのは出てきた絵を見たときだけ。そういう相手には
    # sizes 以外を渡さない。外部サービスは自分の語彙へ丸めてくれるので狭めなくてよい。
    exact_sizes: bool = False
    # 鍵を借りる相手(`app/providers.py` の ID)。空なら自前の鍵。
    credential_from: str = ""
    # 自分の on/off を持つか。 「話す相手」に対応がある相手(外部サービス)は
    # あちらの on/off に従う —— 同じものを 2 か所で切り替えられると、どちらが効いて
    # いるのか画面から読めなくなる。自前の GPU はあちらに出てこないので自分で持つ。
    owns_toggle: bool = False
    # URL を上書きできる環境変数(コンテナ名で辿り着けない相手のための逃げ道)
    url_env: str = ""
    # 元の絵を渡して直せる相手か。 CLI ブリッジ越しの相手はエージェントなので、
    # 作業ディレクトリに置いた絵を開いて直せる。外部サービスは口が別で
    # (OpenAI は /images/edits、Gemini は入力画像のパート)、まだ対応していない ——
    # **黙って無視して一から描くと、直したつもりの絵が全部描き変わって返る**ので、
    # 出来ない相手には頼む前に断る。
    edits: bool = False
    # 参考の音を渡せる相手か。 **絵とは別に持つ** —— 同じ「元を渡せる」でも口も対応も
    # 別物で、1 つの印にまとめると、絵を直せない相手に絵を渡しても素通りしてしまう
    # (黙って一から描いた絵が「直した絵」として返る)。
    audio_reference: bool = False
    # 枠の聞き方(`app/usage.py`)。空なら「この相手は枠を出さない」。
    # 「話す相手」の側にも同じ欄があるが、こちらは絵と音だけの相手のためのもの。
    usage: str = ""
    # 画面・一覧に出す順
    order: int = 0
    # 作れるもの。 絵しか作れない相手・音しか作れない相手があるので、
    # 一覧はこれで絞る(頼めない相手を選ばせない)。
    kinds: tuple[str, ...] = (KIND_IMAGE,)
    # 音のモデル候補の控え(`models` は絵のもの)。相手に聞ける場合はそちらが正。
    audio_models: tuple[str, ...] = ()
    # 音の種類ごとの「頼める長さの上限(秒)」。 並んでいない種類は頼めない
    # (Lyria は曲のモデルなので効果音は作れない)。0 は「長さを指定できない」
    # —— Lyria は尺がモデルで決まっていて、こちらから秒数を渡す口が無い。
    audio_limits: tuple[tuple[str, float], ...] = ()

    # ---- 動画 ---------------------------------------------------------------
    # モデル候補の控え。相手に聞ける場合(ComfyUI)はそちらが正。
    video_models: tuple[str, ...] = ()
    # 頼める大きさ。絵と同じくこちらの語彙は `幅x高さ` で、比率しか取らない相手への
    # 変換は各バックエンドが受け持つ。
    video_sizes: tuple[str, ...] = ()
    # 選べる尺の一覧(秒)。空なら「尺を指定できない相手」。
    # 音は上限で持っているが、動画は決まった値しか取らない相手が多い
    # (Sora は 4/8/12、Veo は 4/6/8)。上限だけで持つと 5 秒を受け付けてしまい、
    # GPU も枠も使ったあとで相手に 400 を返される —— 一覧で持てば頼む前に断れる。
    video_seconds: tuple[float, ...] = ()

    # ---- 声 -----------------------------------------------------------------
    # 読み上げのモデル候補。
    speech_models: tuple[str, ...] = ()
    # 選べる声。空なら相手に聞く —— ElevenLabs は登録した声が人によって違うので、
    # こちらで並べると「持っていない声」を勧めることになる。
    voices: tuple[str, ...] = ()
    # 文字起こしのモデル候補。
    transcribe_models: tuple[str, ...] = ()


PROVIDERS: tuple[MediaProvider, ...] = (
    MediaProvider(
        id="comfyui",
        label="ComfyUI(自前の GPU)",
        # compose 同梱の chiezo-image。GPU が別マシンなら CHIEZO_IMAGE_URL で上書きする。
        url="http://chiezo-image:7014",
        credential=CRED_NONE,
        billing="自前(電気代のみ)",
        setup="docker-compose.comfyui.yml を重ねて "
        "`docker compose -f docker-compose.yml -f docker-compose.comfyui.yml --profile comfyui up -d` "
        "で立ち上げてください(NVIDIA の GPU が要ります)。GPU が別のマシンにあるなら、"
        "そちらで ComfyUI を動かして URL を CHIEZO_IMAGE_URL に設定します。",
        # チェックポイントは置いたものによるので、相手(`/object_info`)に聞く。
        models=(),
        exact_sizes=True,
        url_env="CHIEZO_IMAGE_URL",
        owns_toggle=True,
        order=0,
        kinds=(KIND_IMAGE, KIND_AUDIO, KIND_VIDEO),
        # 音のチェックポイントも `models/checkpoints` に置く(絵と同じ場所)。
        # 何が置いてあるかは相手に聞くので、ここは控えを持たない。
        audio_models=(),
        # 効果音 = Stable Audio Open(既定 47.6 秒)、曲 = ACE-Step。
        # 上限は素材の作りやすさで切ってある —— 長く頼むほど GPU を占有する。
        audio_limits=((SOUND_SFX, 47.0), (SOUND_MUSIC, 240.0)),
        # 動画のモデルは `models/checkpoints` ではなく `models/diffusion_models` に置く
        # (Wan・HunyuanVideo はどれも UNet 単体で配られ、text encoder と VAE を別に読む)。
        # 何が置いてあるかは相手に聞くので、ここも控えを持たない。
        video_models=(),
        # 学習解像度から外すと絵と同じく崩れるので、絵と同じく一覧の外は断る。
        video_sizes=("848x480", "480x848", "1280x720", "720x1280"),
        # 短くしてある。 動画は 1 秒ぶんで 16 フレームを潜在空間ごと持つので、
        # 尺を倍にすると VRAM も時間も倍を超えて増える。長いものは外の相手に頼む。
        video_seconds=(2.0, 3.0, 5.0),
        # 読み上げは持てない。 ComfyUI 本体に TTS のノードが無く、あるのは
        # 外部の拡張(TTS Audio Suite・F5-TTS 等)だけ。何を入れているかで
        # ノード名も引数も変わるので、こちらからグラフを組み立てられない。
    ),
    MediaProvider(
        id="codex",
        label="Codex CLI(ChatGPT のサブスク枠)",
        # CLI ブリッジ(chiezo-bridge-codex)。API ではなく ChatGPT のログインで動く
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
        # エージェントなので、作業ディレクトリに置いた絵を開いて直せる
        edits=True,
        order=15,
    ),
    MediaProvider(
        id="antigravity",
        label="Antigravity CLI(Google AI のサブスク枠)",
        # CLI ブリッジ(chiezo-bridge-antigravity)。API キーではなくサインインで動く
        url="http://chiezo-bridge-antigravity:7013/v1",
        # 認証情報はコンテナ内のサインイン結果。画面から登録する秘密は無い
        credential=CRED_NONE,
        billing="Google AI サブスクリプション(定額)",
        setup="ブリッジ(chiezo-bridge-antigravity)を立てて、コンテナ内で 1 回サインインすると"
        "使えるようになります(手順は「話す相手」としての行と同じ)。"
        "**絵を描けるのは内蔵ツールを持つ CLI だけ**で、Claude Code CLI は持ちません。",
        # モデルは内蔵ツールが決める(指定は受け付けない)
        models=(),
        # 画素の指定は「言葉で頼む」形なので、こちらの語彙をそのまま渡す
        sizes=("1024x1024", "1536x1024", "1024x1536"),
        # on/off は「話す相手」の行と共通。 同じサインインを使うので、
        # あちらを止めたら絵も止まる
        credential_from="antigravity",
        edits=True,
        order=17,
        kinds=(KIND_IMAGE,),
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
        # 相手が受け付けるのは決まった組み合わせだけ(自由な画素数は取らない)。
        # ここに並べたものへ、頼まれたサイズから近いものを選ぶ。
        sizes=("1024x1024", "1536x1024", "1024x1536", "2048x2048", "3840x2160", "2160x3840"),
        credential_from="openai",
        order=20,
        kinds=(KIND_IMAGE, KIND_VIDEO, KIND_SPEECH, KIND_TRANSCRIBE),
        # 先頭が既定(pro は同じ尺でも数倍かかる)。
        video_models=("sora-2", "sora-2-pro"),
        video_sizes=("1280x720", "720x1280", "1792x1024", "1024x1792"),
        # 相手が受け付ける尺(これ以外は 400 が返る)。
        video_seconds=(4.0, 8.0, 12.0),
        speech_models=("gpt-4o-mini-tts", "tts-1-hd", "tts-1"),
        # 声は決め打ちの名前(登録も複製もできないので、こちらで並べてよい)。
        voices=("alloy", "ash", "ballad", "coral", "echo", "fable",
                "nova", "onyx", "sage", "shimmer", "verse"),
        transcribe_models=("gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1"),
    ),
    MediaProvider(
        id="gemini",
        label="Gemini(画像生成・Lyria 3)",
        # この URL の直下が interactions(画像は Gemini の新しい口を使う)。
        url="https://generativelanguage.googleapis.com/v1beta",
        credential=CRED_REQUIRED,
        billing="無料枠(課金を有効にしなければ従量課金は発生しない)",
        setup="管理画面(/admin の「AI の相手」)で Gemini の API キーを登録してください。"
        "画像生成でも同じ鍵を使います。",
        # 速い順。先頭が既定(何も選ばなかったときに使われるので、ずらすと黙って変わる)。
        models=("gemini-3.1-flash-image", "gemini-3.1-flash-lite-image", "gemini-3-pro-image"),
        credential_from="gemini",
        order=10,
        kinds=(KIND_IMAGE, KIND_AUDIO, KIND_VIDEO, KIND_SPEECH, KIND_TRANSCRIBE),
        # 曲は Lyria 3。先頭が既定(clip = 30 秒ほど、pro = 3 分ほど)。
        audio_models=("lyria-3-clip-preview", "lyria-3-pro-preview"),
        # 効果音は作れない(Lyria は曲のモデル)。尺も指定できない ——
        # モデルごとに決まっていて、秒数を渡す口が無いので 0 にしてある。
        audio_limits=((SOUND_MUSIC, 0.0),),
        # 先頭が既定(omni は interactions で一発、Veo は長時間オペレーションを待つ)。
        # 口が 2 通りに分かれるので、名前で見分ける(`_is_veo`)。
        video_models=("gemini-omni-flash-preview",
                      "veo-3.1-fast-generate-preview", "veo-3.1-generate-preview"),
        video_sizes=("1280x720", "720x1280", "1920x1080", "1080x1920"),
        # Veo が受け付ける尺。omni には尺を渡す口が無いので、
        # omni を選んだうえで秒数を渡されたら断る(`_check_video`)。
        video_seconds=(4.0, 6.0, 8.0),
        speech_models=("gemini-3.1-flash-tts-preview",
                       "gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts"),
        # 決め打ちの 30 声。先頭が既定
        voices=("Kore", "Puck", "Zephyr", "Charon", "Fenrir", "Leda", "Orus", "Aoede",
                "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel", "Algieba",
                "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia", "Achernar",
                "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird", "Zubenelgenubi",
                "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat"),
        # 文字起こし専用のモデルは無い。 ふつうの Gemini に音を入力として渡す
        # (`providers.py` の会話用と同じ並びから、動くことを確かめたものを採る)。
        transcribe_models=("gemini-3.7-flash", "gemini-3.5-flash"),
    ),
    MediaProvider(
        id="elevenlabs",
        label="ElevenLabs(声・効果音・曲・絵・動画)",
        url="https://api.elevenlabs.io/v1",
        credential=CRED_REQUIRED,
        billing="無料枠あり(超えたぶんは従量課金。**絵と動画は API から頼むと Pro 以上**)",
        setup="管理画面(/admin の「AI の相手」)で ElevenLabs の API キーを登録し、"
        "「使う」を押してください。"
        "**絵と動画だけは API 利用に Pro プラン以上が要ります**(音と声は無料枠で頼めます)。",
        # この相手だけ鍵を自分で持つ。 「話す相手」に対応が無い(会話はできない)ので
        # 借りる先が無く、鍵と on/off をここに置くしかない。ComfyUI と同じ扱い。
        credential_from="",
        owns_toggle=True,
        # 枠を聞ける。 鍵だけで引ける口があり、生成も会話もしないので枠を食わない。
        usage=USAGE_ELEVENLABS,
        # 曲は参考音源を受け取れる(登録してから id で参照する)
        audio_reference=True,
        order=30,
        kinds=(KIND_AUDIO, KIND_SPEECH, KIND_TRANSCRIBE, KIND_IMAGE, KIND_VIDEO),
        # 絵と動画は他社のモデルを預かっているだけ(自社の絵のモデルは持っていない)。
        # 同じモデルを OpenAI / Gemini に直接頼めるなら、そちらのほうが枠の要件が緩い
        # —— ここを選ぶ利点は「1 つの鍵で全部そろう」ことだけ。
        models=("gpt-image-2", "gemini-3.1-flash-image", "gemini-3-pro-image",
                "bytedance-seedream-5-pro", "bytedance-seedream-5-lite"),
        # 相手の語彙は「比率 + 段階(1K/2K/4K)」なので、こちらの `幅x高さ` から寄せる。
        sizes=("1024x1024", "1536x1024", "1024x1536", "2048x2048"),
        # 効果音と曲で口もモデルも別。先頭が既定
        audio_models=("eleven_text_to_sound_v2", "music_v2"),
        # 相手の受け付ける範囲(効果音 0.5〜30 秒、曲 3〜600 秒)。
        audio_limits=((SOUND_SFX, 30.0), (SOUND_MUSIC, 600.0)),
        video_models=("veo-3.1-fast-generate-001", "veo-3.1-generate-001",
                      "bytedance-seedance-v2-fast", "bytedance-seedance-v2",
                      "bytedance-seedance-v2.5", "bytedance-seedance-v2-mini"),
        video_sizes=("1280x720", "720x1280", "1920x1080", "1080x1920"),
        # 受け付ける尺はモデルによって違う(ここは和集合)。相手が断ったときは
        # 本文をそのまま返すので、どれが駄目だったかは呼んだ側に伝わる。
        video_seconds=(4.0, 5.0, 6.0, 8.0, 10.0),
        speech_models=("eleven_v3", "eleven_multilingual_v2", "eleven_flash_v2_5"),
        # 声は相手に聞く。 既定の声も自分で複製した声も人によって違うので、
        # こちらで並べると「持っていない声」を勧めることになる。
        voices=(),
        transcribe_models=("scribe_v2", "scribe_v1"),
    ),
)

BY_ID = {p.id: p for p in PROVIDERS}


# どれに頼むのがよいか(先頭が既定)。 `order`(画面に並べる順)とは別に持つ ——
# あちらは設定を探すための並びで、用が違う。
#
# 種類ごとに違う。 相手ごとに 1 つの順位にすると、音で先頭にした相手が絵でも
# 先頭になる(ElevenLabs は曲がよくても絵の相手としては選びたくない)。
#
# 自前の GPU(ComfyUI)を後ろにしてあるのは出来の問題で、枠を食わない利点は残る ——
# 名指しすればこれまでどおり使える。ここに無い相手は listed の後ろ(画面の並びのまま)
# に回る。相手を足したらこの表にも足すこと(`tests/test_media.py` が欠けを見張っている)。
PREFERENCE: dict[str, tuple[str, ...]] = {
    KIND_IMAGE: ("codex", "antigravity", "gemini", "openai", "comfyui", "elevenlabs"),
    KIND_AUDIO: ("elevenlabs", "gemini", "comfyui"),
    KIND_VIDEO: ("gemini", "openai", "comfyui", "elevenlabs"),
    KIND_SPEECH: ("elevenlabs", "gemini", "openai"),
    KIND_TRANSCRIBE: ("gemini", "openai", "elevenlabs"),
}


def preference_of(provider_id: str, kind: str = "") -> int:
    """その種類で何番目に頼みたいか(小さいほど先)。表に無い相手は後ろへ。"""
    ranked = PREFERENCE.get(kind, ())
    return ranked.index(provider_id) if provider_id in ranked else len(ranked)


def all_providers(kind: str = "") -> tuple[MediaProvider, ...]:
    """相手の一覧。`kind` を渡すとそれを作れる相手だけを、頼む順で返す。

    絞らないと、絵の一覧に音しか作れない相手が並ぶ(逆も同じ)。
    並びは頼む順(`PREFERENCE`)—— この先頭が既定の相手になり、道具の一覧でも
    先に出るので、「よい相手から」に揃える。同点は画面の並びのまま(安定ソート)。
    kind を渡さないときは画面の並び(`order`)—— 設定の表はそちらで読む。
    """
    found = sorted((p for p in PROVIDERS if not kind or kind in p.kinds),
                   key=lambda p: p.order)
    if not kind:
        return tuple(found)
    return tuple(sorted(found, key=lambda p: preference_of(p.id, kind)))


def sounds_of(spec: MediaProvider) -> tuple[str, ...]:
    """その相手に頼める音の種類(効果音・曲)。"""
    return tuple(sound for sound, _ in spec.audio_limits)


def models_of(spec: MediaProvider, kind: str) -> tuple[str, ...]:
    """その kind のモデル候補。控えなので、相手に聞ける場合はそちらが正。"""
    return {
        KIND_IMAGE: spec.models,
        KIND_AUDIO: spec.audio_models,
        KIND_VIDEO: spec.video_models,
        KIND_SPEECH: spec.speech_models,
        KIND_TRANSCRIBE: spec.transcribe_models,
    }.get(kind, ())


def max_seconds_of(spec: MediaProvider, sound: str) -> float:
    """頼める長さの上限(秒)。0 は「長さを指定できない相手」(Lyria)。

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


def standalone_labels() -> dict[str, str]:
    """「話す相手」に出てこない相手(自前の GPU・ElevenLabs)の `{id: 表示名}`。

    鍵を借りている相手(`credential_from`)は「話す相手」の側に同じ行があるので出さない
    —— 同じ相手が 2 行に出ると、どちらの数字なのか読めなくなる。
    """
    from app import providers  # 循環参照を避けるためここで読む(定義には要らない)

    talk = {p.id for p in providers.all_providers()}
    return {
        spec.id: spec.label
        for spec in all_providers()
        if spec.id not in talk and not spec.credential_from
    }


def default_backend(kind: str = KIND_IMAGE) -> str:
    """相手を指定されなかったときに使う既定。その kind の「頼む順」の先頭(`PREFERENCE`)。

    かつては自前の GPU を既定にしていた(外へ出さず、枠も食わない)が、
    出来が違う —— 相手を名指ししない呼び出しがいちばん多いので、そこが良い相手へ
    行くようにしてある。枠を使いたくないときは `backend="comfyui"` と名指しする。
    """
    providers = all_providers(kind)
    return providers[0].id if providers else ""
